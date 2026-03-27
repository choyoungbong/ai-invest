"""
KIS WebSocket – 실시간 주식 체결가 수신

개선사항:
- 장 외 시간(09:00~15:40 외)에는 재연결 시도 안 함
- 장 시작/종료 시 자동 연결/해제
- 재연결 지수 백오프 (최대 60초)
"""
import asyncio
import json
import logging
import os
from datetime import datetime, time as dtime
from typing import Callable

import pytz

logger = logging.getLogger(__name__)

IS_MOCK    = os.getenv("KIS_MOCK", "true").lower() == "true"
WS_URL     = "ws://ops.koreainvestment.com:31000" if IS_MOCK else "ws://ops.koreainvestment.com:21000"
APP_KEY    = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KST        = pytz.timezone("Asia/Seoul")

# 실시간 현재가 캐시
_price_cache: dict[str, float] = {}
_ws_running  = False
_subscribed_codes: set[str] = set()


def get_cached_price(code: str) -> float | None:
    return _price_cache.get(code)


def update_subscribed_codes(codes: list[str]):
    global _subscribed_codes
    _subscribed_codes = set(codes)
    logger.info(f"[WebSocket] 구독 종목 업데이트: {len(_subscribed_codes)}개")


# ── 장 운영 시간 확인 ──────────────────────────────────────────────────────────

def _is_ws_active_time() -> bool:
    """
    WebSocket 활성화 시간 확인.
    장 시작 5분 전(08:55) ~ 장 마감 10분 후(15:40) 사이에만 연결 유지.
    주말은 비활성.
    """
    now = datetime.now(KST)
    if now.weekday() >= 5:  # 토/일
        return False
    t = now.time()
    return dtime(8, 55) <= t <= dtime(15, 40)


def _seconds_until_market_open() -> int:
    """다음 장 시작까지 남은 초"""
    now = datetime.now(KST)

    # 주말이면 월요일 08:55까지
    days_ahead = 0
    if now.weekday() == 5:   # 토
        days_ahead = 2
    elif now.weekday() == 6:  # 일
        days_ahead = 1

    if days_ahead > 0:
        return days_ahead * 86400

    # 평일이면 당일 08:55까지
    target = now.replace(hour=8, minute=55, second=0, microsecond=0)
    if now >= target:
        # 이미 지났으면 다음날 08:55
        import datetime as dt
        target = target + dt.timedelta(days=1)
        # 다음날이 주말이면 건너뜀
        while target.weekday() >= 5:
            target = target + dt.timedelta(days=1)

    diff = (target - now).total_seconds()
    return max(int(diff), 60)


# ── WebSocket 접속키 발급 ─────────────────────────────────────────────────────

async def _get_approval_key() -> str:
    import httpx
    BASE = (
        "https://openapivts.koreainvestment.com:29443"
        if IS_MOCK
        else "https://openapi.koreainvestment.com:9443"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(
            f"{BASE}/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey":     APP_KEY,
                "secretkey":  APP_SECRET,
            },
        )
        res.raise_for_status()
        return res.json()["approval_key"]


# ── 구독 메시지 ───────────────────────────────────────────────────────────────

def _subscribe_msg(approval_key: str, code: str, subscribe: bool = True) -> str:
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype":     "P",
            "tr_type":      "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id":  "H0STCNT0",
                "tr_key": code,
            }
        },
    })


# ── 체결 데이터 파싱 ───────────────────────────────────────────────────────────

def _parse_execution(data: str) -> dict | None:
    try:
        parts = data.split("|")
        if len(parts) < 4 or parts[0] != "0":
            return None
        fields = parts[3].split("^")
        if len(fields) < 3:
            return None
        return {"code": fields[0], "price": float(fields[2])}
    except Exception:
        return None


# ── WebSocket 메인 루프 ────────────────────────────────────────────────────────

async def run_websocket(on_price_update: Callable[[str, float], None] | None = None):
    """
    KIS WebSocket 연결 유지 루프.
    장 외 시간에는 슬립하고 장 시작 시에만 연결합니다.
    """
    global _ws_running

    if not APP_KEY or not APP_SECRET:
        logger.warning("[WebSocket] KIS_APP_KEY/SECRET 미설정 — 비활성화")
        return

    _ws_running = True
    retry_count = 0
    max_retry_delay = 60  # 최대 60초 대기

    logger.info(f"[WebSocket] 시작 ({'모의' if IS_MOCK else '실전'}투자)")

    while _ws_running:
        # 장 외 시간이면 슬립
        if not _is_ws_active_time():
            wait = min(_seconds_until_market_open(), 300)  # 최대 5분마다 확인
            logger.info(f"[WebSocket] 장 외 시간 — {wait}초 후 재확인")
            await asyncio.sleep(wait)
            continue

        try:
            import websockets
            approval_key = await _get_approval_key()
            logger.info("[WebSocket] 접속키 발급 완료 — 연결 시도")

            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                retry_count = 0  # 연결 성공 시 재시도 카운터 초기화
                logger.info("[WebSocket] 연결 성공")

                # 구독 등록
                for code in list(_subscribed_codes):
                    await ws.send(_subscribe_msg(approval_key, code))
                    logger.info(f"[WebSocket] 구독: {code}")

                last_codes = set(_subscribed_codes)

                async for message in ws:
                    if not _ws_running:
                        break

                    # 장 외 시간 감지 시 연결 종료
                    if not _is_ws_active_time():
                        logger.info("[WebSocket] 장 마감 감지 — 연결 종료")
                        break

                    # 구독 종목 변경 감지
                    current = set(_subscribed_codes)
                    for code in current - last_codes:
                        await ws.send(_subscribe_msg(approval_key, code, True))
                        logger.info(f"[WebSocket] 구독 추가: {code}")
                    for code in last_codes - current:
                        await ws.send(_subscribe_msg(approval_key, code, False))
                        logger.info(f"[WebSocket] 구독 해제: {code}")
                    last_codes = current

                    # PINGPONG
                    if message == "PINGPONG":
                        await ws.send("PINGPONG")
                        continue

                    # 체결 데이터 처리
                    result = _parse_execution(message)
                    if result:
                        code  = result["code"]
                        price = result["price"]
                        _price_cache[code] = price
                        if on_price_update:
                            on_price_update(code, price)

        except Exception as e:
            if _ws_running and _is_ws_active_time():
                retry_count += 1
                delay = min(5 * (2 ** min(retry_count - 1, 3)), max_retry_delay)
                logger.warning(
                    f"[WebSocket] 연결 오류 (시도 {retry_count}): {e} "
                    f"— {delay}초 후 재연결"
                )
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(10)

    logger.info("[WebSocket] 종료")


async def stop_websocket():
    global _ws_running
    _ws_running = False


# ── 실시간 손절/익절 모니터 ────────────────────────────────────────────────────

class RealTimeMonitor:
    def __init__(self, db_factory):
        self.db_factory    = db_factory
        self._task         = None
        self._ws_task      = None
        self._price_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def start(self):
        self._ws_task = asyncio.create_task(
            run_websocket(on_price_update=self._on_price)
        )
        self._task = asyncio.create_task(self._process_loop())
        logger.info("[RealTimeMonitor] 시작")

    async def stop(self):
        await stop_websocket()
        if self._task:
            self._task.cancel()
        if self._ws_task:
            self._ws_task.cancel()
        logger.info("[RealTimeMonitor] 종료")

    def _on_price(self, code: str, price: float):
        try:
            self._price_queue.put_nowait((code, price))
        except asyncio.QueueFull:
            pass

    async def _process_loop(self):
        from trader.auto_stoploss import check_and_execute_auto_exit
        from sqlalchemy import select, and_
        from api.models import Trade
        from trader.risk_manager import is_market_open, STOP_LOSS_PCT, TARGET_PCT

        # STOP_LOSS_PCT, TARGET_PCT 가져오기
        import os
        stop_pct   = float(os.getenv("STOP_LOSS_PCT",    "-0.02"))
        target_pct = float(os.getenv("TARGET_PROFIT_PCT", "0.05"))
        hard_pct   = float(os.getenv("HARD_STOP_PCT",    "-0.03"))

        while True:
            try:
                code, price = await self._price_queue.get()

                # 장 외 시간이면 무시
                if not is_market_open():
                    continue

                async with self.db_factory() as db:
                    stmt = (
                        select(Trade)
                        .where(and_(
                            Trade.code == code,
                            Trade.order_type == "BUY",
                            Trade.status.in_(["FILLED", "PARTIAL"]),
                        ))
                    )
                    trades = (await db.execute(stmt)).scalars().all()

                    for trade in trades:
                        sold = (await db.execute(
                            select(Trade).where(and_(
                                Trade.code == code,
                                Trade.order_type == "SELL",
                                Trade.signal_id == trade.signal_id,
                            ))
                        )).scalars().first()
                        if sold:
                            continue

                        stop_price   = trade.price * (1 + stop_pct)
                        hard_stop    = trade.price * (1 + hard_pct)
                        target_price = trade.price * (1 + target_pct)

                        if price <= hard_stop or price <= stop_price or price >= target_price:
                            logger.info(
                                f"[RealTimeMonitor] 청산 조건: {code} "
                                f"현재가 {price:,} / 손절 {stop_price:,.0f} / 목표 {target_price:,.0f}"
                            )
                            await check_and_execute_auto_exit(db)
                            _subscribed_codes.discard(code)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RealTimeMonitor] 처리 오류: {e}")
                await asyncio.sleep(1)

    async def subscribe_holdings(self):
        """현재 보유 종목 구독"""
        from sqlalchemy import select, and_
        from api.models import Trade

        async with self.db_factory() as db:
            stmt = select(Trade.code).where(and_(
                Trade.order_type == "BUY",
                Trade.status.in_(["FILLED", "PARTIAL"]),
            )).distinct()
            codes = (await db.execute(stmt)).scalars().all()

        if codes:
            update_subscribed_codes(list(codes))
            logger.info(f"[RealTimeMonitor] 보유 종목 구독: {list(codes)}")
