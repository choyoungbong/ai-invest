"""
Monitor – 서버 에러 모니터링 + 텔레그램 자동 알림

FastAPI 미들웨어로 등록해 모든 500 에러를 캐치합니다.
추가로 DB / Redis / KIS API 헬스체크를 주기적으로 수행합니다.
"""
import logging
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from notification.service import send_message

logger = logging.getLogger(__name__)

# 에러 쿨다운: 같은 경로는 10분에 1번만 알림
_error_cooldown: dict[str, datetime] = {}
COOLDOWN_MINUTES = 10

# 최근 에러 로그 (메모리)
_recent_errors: deque = deque(maxlen=50)


def _should_notify(path: str) -> bool:
    last = _error_cooldown.get(path)
    if last and datetime.utcnow() - last < timedelta(minutes=COOLDOWN_MINUTES):
        return False
    _error_cooldown[path] = datetime.utcnow()
    return True


class ErrorMonitorMiddleware(BaseHTTPMiddleware):
    """500 에러를 잡아 텔레그램으로 알립니다."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        try:
            response = await call_next(request)

            # 5xx 에러 감지
            if response.status_code >= 500 and _should_notify(request.url.path):
                elapsed = time.time() - start
                err = {
                    "time":    datetime.utcnow().isoformat(),
                    "path":    request.url.path,
                    "method":  request.method,
                    "status":  response.status_code,
                    "elapsed": round(elapsed, 2),
                }
                _recent_errors.appendleft(err)
                await send_message(
                    f"⚠️ <b>[AI INVEST] 서버 에러</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🔴 상태코드: {response.status_code}\n"
                    f"📍 경로: {request.method} {request.url.path}\n"
                    f"⏱️ 응답시간: {elapsed:.2f}s\n"
                    f"🕐 시각: {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )
            return response

        except Exception as exc:
            elapsed = time.time() - start
            tb = traceback.format_exc()[-500:]   # 마지막 500자
            err = {
                "time":    datetime.utcnow().isoformat(),
                "path":    request.url.path,
                "error":   str(exc),
                "elapsed": round(elapsed, 2),
            }
            _recent_errors.appendleft(err)
            logger.error(f"Unhandled exception [{request.url.path}]: {exc}\n{tb}")

            if _should_notify(request.url.path):
                await send_message(
                    f"🚨 <b>[AI INVEST] 서버 예외 발생</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 경로: {request.method} {request.url.path}\n"
                    f"❌ 오류: {str(exc)[:200]}\n"
                    f"🕐 시각: {datetime.utcnow().strftime('%H:%M:%S')} UTC"
                )
            return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


# ── 헬스체크 함수들 ────────────────────────────────────────────────────────────

async def check_db_health(db_factory) -> dict:
    try:
        async with db_factory() as db:
            await db.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def check_redis_health(redis_url: str) -> dict:
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url)
        await r.ping()
        await r.aclose()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def check_kis_health() -> dict:
    try:
        from trader.kis_client import get_access_token
        await get_access_token()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def run_health_check_and_notify(db_factory, redis_url: str):
    """전체 헬스체크 실행 — 이상 시 텔레그램 알림"""
    db_h    = await check_db_health(db_factory)
    redis_h = await check_redis_health(redis_url)
    kis_h   = await check_kis_health()

    all_ok = all(h["status"] == "ok" for h in [db_h, redis_h, kis_h])

    if not all_ok:
        lines = ["🔴 <b>[AI INVEST] 헬스체크 이상 감지</b>\n━━━━━━━━━━━━━━━━━━"]
        for name, result in [("PostgreSQL", db_h), ("Redis", redis_h), ("KIS API", kis_h)]:
            icon = "✅" if result["status"] == "ok" else "❌"
            line = f"{icon} {name}"
            if result["status"] != "ok":
                line += f": {result.get('detail', '')[:80]}"
            lines.append(line)
        await send_message("\n".join(lines))
        logger.error(f"헬스체크 이상: DB={db_h}, Redis={redis_h}, KIS={kis_h}")

    return {"db": db_h, "redis": redis_h, "kis": kis_h, "all_ok": all_ok}


def get_recent_errors() -> list:
    return list(_recent_errors)
