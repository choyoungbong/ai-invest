"""
KIS WebSocket – 실시간 주식 체결가 수신

모의투자: ws://ops.koreainvestment.com:31000
실전투자: ws://ops.koreainvestment.com:21000

TR: H0STCNT0 (주식 체결)
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

IS_MOCK   = os.getenv("KIS_MOCK", "true").lower() == "true"
WS_URL    = "ws://ops.koreainvestment.com:31000" if IS_MOCK else "ws://ops.koreainvestment.com:21000"
APP_KEY   = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")

# 실시간 현재가 캐시 {code: price}
_price_cache: dict[str, float] = {}
_ws_running = False
_subscribed_codes: set[str] = set()


def get_cached_price(code: str) -> float | None:
    """캐시된 실시간 현재가 반환. 없으면 None."""
    return _price_cache.get(code)


def update_subscribed_codes(codes: list[str]):
    """구독할 종목 코드 목록 업데이트"""
    global _subscribed_codes
    _subscribed_codes = set(codes)
    logger.info(f"[WebSocket] 구독 종목 업데이트: {len(_subscribed_codes)}개")


# ── 암호화 키 발급 ─────────────────────────────────────────────────────────────

async def _get_approval_key() -> str:
    """WebSocket 접속키 발급"""
    import httpx
    BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_MOCK else "https://openapi.koreainvestment.com:9443"
    url  = f"{BASE_URL}/oauth2/Approval"
    body = {
        "grant_type": "client_credentials",
        "appkey":     APP_KEY,
        "secretkey":  APP_SECRET,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, json=body)
        res.raise_for_status()
        return res.json()["approval_key"]


# ── 구독 메시지 생성 ───────────────────────────────────────────────────────────

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
        }
    })


# ── 체결 데이터 파싱 ───────────────────────────────────────────────────────────

def _parse_execution(data: str) -> dict | None:
    """
    H0STCNT0 체결 데이터 파싱
    파이프(|) 구분자로 분리
    """
    try:
        parts = data.split("|")
        if len(parts) < 4:
            return None

        # 실시간 데이터: 0|H0STCNT0|001|데이터
        if parts[0] != "0":
            return None

        fields = parts[3].split("^")
        if len(fields) < 3:
            return None

        code  = fields[0]
        price = float(fields[2])   # 현재가

        return {"code": code, "price": price}
    except Exception:
        return None


# ── WebSocket 메인 루프 ────────────────────────────────────────────────────────

async def run_websocket(on_price_update: Callable[[str, float], None] | None = None):
    """
    KIS WebSocket 연결 유지 루프.
    가격 업데이트 시 on_price_update(code, price) 콜백 호출.
    """
    global _ws_running

    if not APP_KEY or not APP_SECRET:
        logger.warning("[WebSocket] KIS_APP_KEY/SECRET 미설정 — WebSocket 비활성화")
        return

    _ws_running = True
    logger.info(f"[WebSocket] 시작 — {'모의' if IS_MOCK else '실전'}투자 {WS_URL}")

    while _ws_running:
        try:
            approval_key = await _get_approval_key()
            logger.info("[WebSocket] 접속키 발급 완료")

            async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                logger.info("[WebSocket] 연결 성공")

                # 현재 구독 종목 등록
                for code in list(_subscribed_codes):
                    await ws.send(_subscribe_msg(approval_key, code, subscribe=True))
                    logger.info(f"[WebSocket] 구독 등록: {code}")

                last_codes = set(_subscribed_codes)

                async for message in ws:
                    if not _ws_running:
                        break

                    # 구독 종목 변경 감지
                    current_codes = set(_subscribed_codes)
                    added   = current_codes - last_codes
                    removed = last_codes - current_codes

                    for code in added:
                        await ws.send(_subscribe_msg(approval_key, code, subscribe=True))
                        logger.info(f"[WebSocket] 구독 추가: {code}")
                    for code in removed:
                        await ws.send(_subscribe_msg(approval_key, code, subscribe=False))
                        logger.info(f"[WebSocket] 구독 해제: {code}")
                    last_codes = current_codes

                    # PINGPONG 응답
                    if message == "PINGPONG":
                        await ws.send("PINGPONG")
                        continue

                    # 체결 데이터 파싱
                    result = _parse_execution(message)
                    if result:
                        code  = result["code"]
                        price = result["price"]
                        _price_cache[code] = price

                        if on_price_update:
                            on_price_update(code, price)

        except Exception as e:
            logger.error(f"[WebSocket] 연결 오류: {e} — 5초 후 재연결")
            await asyncio.sleep(5)

    logger.info("[WebSocket] 종료")


async def stop_websocket():
    global _ws_running
    _ws_running = False


# ── WebSocket 기반 실시간 손절/익절 모니터 ────────────────────────────────────

class RealTimeMonitor:
    """
    WebSocket 가격 수신 → 즉시 손절/익절 체크
    """
    def __init__(self, db_factory):
        self.db_factory  = db_factory
        self._task       = None
        self._ws_task    = None
        self._price_queue: asyncio.Queue = asyncio.Queue()

    async def start(self):
        """WebSocket + 모니터 동시 시작"""
        self._ws_task    = asyncio.create_task(
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

    def _on_price(self, code: str, price: float):
        """가격 수신 시 큐에 추가 (non-blocking)"""
        try:
            self._price_queue.put_nowait((code, price))
        except asyncio.QueueFull:
            pass

    async def _process_loop(self):
        """큐에서 가격을 꺼내 손절/익절 체크"""
        from trader.auto_stoploss import check_and_execute_auto_exit
        from sqlalchemy import select, and_
        from api.models import Trade, Signal

        while True:
            try:
                code, price = await self._price_queue.get()

                async with self.db_factory() as db:
                    # 해당 종목 보유 포지션 확인
                    stmt = (
                        select(Trade)
                        .where(and_(
                            Trade.code == code,
                            Trade.order_type == "BUY",
                            Trade.status == "FILLED",
                        ))
                    )
                    trades = (await db.execute(stmt)).scalars().all()

                    for trade in trades:
                        # 이미 청산됐는지 확인
                        sold = (await db.execute(
                            select(Trade).where(and_(
                                Trade.code == code,
                                Trade.order_type == "SELL",
                                Trade.signal_id == trade.signal_id,
                            ))
                        )).scalars().first()
                        if sold:
                            continue

                        # 손절/익절 조건 확인
                        stop_pct   = float(os.getenv("STOP_LOSS_PCT",   "-0.02"))
                        target_pct = float(os.getenv("TARGET_PROFIT_PCT", "0.05"))

                        stop_price   = trade.price * (1 + stop_pct)
                        target_price = trade.price * (1 + target_pct)

                        if price <= stop_price or price >= target_price:
                            logger.info(
                                f"[RealTimeMonitor] 청산 조건 감지: {code} "
                                f"현재가 {price:,} / 손절 {stop_price:,.0f} / 목표 {target_price:,.0f}"
                            )
                            await check_and_execute_auto_exit(db)
                            # 구독 해제 (청산 완료)
                            _subscribed_codes.discard(code)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RealTimeMonitor] 처리 오류: {e}")

    async def subscribe_holdings(self):
        """현재 보유 종목을 WebSocket 구독에 추가"""
        from sqlalchemy import select, and_
        from api.models import Trade

        async with self.db_factory() as db:
            stmt = select(Trade.code).where(and_(
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            )).distinct()
            rows = (await db.execute(stmt)).scalars().all()

        update_subscribed_codes(list(rows))
        logger.info(f"[RealTimeMonitor] 보유 종목 구독: {rows}")
