"""
Trader Service – 주문 실행 서비스

신호 기반 자동/반자동 주문 실행 + 리스크 관리
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from api.models import Signal, Trade
from trader import kis_client as kis
from notification.service import notify_trade

logger = logging.getLogger(__name__)

# ── 리스크 파라미터 ────────────────────────────────────────────────────────────
MAX_ORDER_AMOUNT   = 1_000_000    # 1회 최대 주문금액 (100만원)
MAX_DAILY_ORDERS   = 10           # 하루 최대 주문 횟수
DAILY_LOSS_LIMIT   = 0.03         # 일일 손실 한도 3%


# ── 수량 계산 ──────────────────────────────────────────────────────────────────

def calc_quantity(price: float, budget: int = MAX_ORDER_AMOUNT) -> int:
    """주문 예산과 현재가를 기반으로 주문 수량을 계산합니다."""
    if price <= 0:
        return 0
    qty = int(budget // price)
    return max(qty, 1)


# ── 오늘 주문 횟수 조회 ────────────────────────────────────────────────────────

async def _today_order_count(db: AsyncSession) -> int:
    from sqlalchemy import func
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count(Trade.id))
        .where(Trade.created_at >= today_start)
    )
    return (await db.execute(stmt)).scalar() or 0


# ── 메인 주문 실행 ─────────────────────────────────────────────────────────────

async def execute_order(
    db: AsyncSession,
    signal_id: str,
    quantity: Optional[int] = None,
    use_market_price: bool = True,
) -> dict:
    """
    신호 기반 주문을 실행합니다.

    1. 신호 조회
    2. 리스크 검사 (일일 주문 횟수, 최대 금액)
    3. KIS API 주문 실행
    4. Trade 레코드 저장
    5. 텔레그램 알림
    """
    # ── 1. 신호 조회 ─────────────────────────────────────────────────────────
    stmt  = select(Signal).where(Signal.id == signal_id)
    signal = (await db.execute(stmt)).scalars().first()
    if not signal:
        return {"success": False, "error": "신호를 찾을 수 없습니다"}

    if signal.is_executed:
        return {"success": False, "error": "이미 실행된 신호입니다"}

    # ── 2. 리스크 검사 ────────────────────────────────────────────────────────
    today_count = await _today_order_count(db)
    if today_count >= MAX_DAILY_ORDERS:
        return {"success": False, "error": f"일일 최대 주문 횟수({MAX_DAILY_ORDERS}회) 초과"}

    # ── 3. 현재가 조회 & 수량 결정 ───────────────────────────────────────────
    try:
        price_data = await kis.get_current_price(signal.code)
        live_price = price_data["price"] or int(signal.price)
    except Exception as e:
        logger.warning(f"현재가 조회 실패 ({signal.code}): {e} — 신호 가격 사용")
        live_price = int(signal.price)

    qty = quantity or calc_quantity(live_price)
    order_amount = live_price * qty

    if order_amount > MAX_ORDER_AMOUNT * 1.1:
        return {
            "success": False,
            "error":   f"주문금액({order_amount:,}원)이 최대 한도({MAX_ORDER_AMOUNT:,}원)를 초과합니다",
        }

    # ── 4. KIS API 주문 ───────────────────────────────────────────────────────
    order_type_code = "01" if use_market_price else "00"
    order_price     = 0 if use_market_price else live_price

    try:
        if signal.signal_type == "BUY":
            result = await kis.buy_order(signal.code, qty, order_price, order_type_code)
        else:
            result = await kis.sell_order(signal.code, qty, order_price, order_type_code)
    except Exception as e:
        logger.error(f"KIS 주문 오류: {e}")
        return {"success": False, "error": f"브로커 API 오류: {e}"}

    # ── 5. Trade 레코드 저장 ──────────────────────────────────────────────────
    trade_id = str(uuid.uuid4())
    status   = "FILLED" if result["success"] else "FAILED"

    await db.execute(
        Trade.__table__.insert().values(
            id=trade_id,
            signal_id=signal_id,
            code=signal.code,
            name=signal.name,
            order_type=signal.signal_type,
            price=live_price,
            quantity=qty,
            amount=order_amount,
            status=status,
            broker_order_id=result.get("order_no", ""),
            filled_at=datetime.utcnow() if result["success"] else None,
        )
    )

    # 신호 실행 표시
    await db.execute(
        Signal.__table__.update()
        .where(Signal.id == signal_id)
        .values(is_executed=True)
    )
    await db.commit()
            
    # ── 응답 데이터 ───────────────────────────────────────────────────────
    trade_data = {
        "trade_id":   trade_id,
        "code":       signal.code,
        "name":       signal.name,
        "order_type": signal.signal_type,
        "price":      live_price,
        "quantity":   qty,
        "amount":     order_amount,
        "status":     status,
        "broker_order_id": result.get("order_no", ""),
        "mock":       kis.IS_MOCK,
        **result,
    }

    # ── 6. 텔레그램 알림 ──────────────────────────────────────────────────────
    await notify_trade(trade_data)

    logger.info(
        f"주문 {'완료' if result['success'] else '실패'}: "
        f"{signal.code} {signal.signal_type} {qty}주 @ {live_price:,}원"
    )
    return trade_data


# ── 자동 손절 체크 ─────────────────────────────────────────────────────────────

async def check_stop_loss(db: AsyncSession) -> list[dict]:
    """
    보유 종목의 현재가가 손절가에 도달했는지 확인합니다.
    손절 조건 충족 시 매도 신호를 생성합니다. (반자동 모드)
    """
    from sqlalchemy import and_
    stmt = (
        select(Trade)
        .where(and_(Trade.order_type == "BUY", Trade.status == "FILLED"))
        .order_by(desc(Trade.created_at))
        .limit(20)
    )
    trades = (await db.execute(stmt)).scalars().all()

    alerts = []
    for trade in trades:
        # 연결된 신호의 손절가 조회
        sig_stmt = select(Signal).where(Signal.id == trade.signal_id)
        signal   = (await db.execute(sig_stmt)).scalars().first()
        if not signal or not signal.stop_loss:
            continue

        try:
            price_data = await kis.get_current_price(trade.code)
            current    = price_data["price"]
        except Exception:
            continue

        if current <= signal.stop_loss:
            alerts.append({
                "code":          trade.code,
                "name":          trade.name,
                "buy_price":     trade.price,
                "current_price": current,
                "stop_loss":     signal.stop_loss,
                "loss_pct":      (current / trade.price - 1) * 100,
                "trade_id":      trade.id,
                "signal_id":     trade.signal_id,
            })
            logger.warning(
                f"손절 도달: {trade.code} {trade.name} "
                f"현재가 {current:,} ≤ 손절가 {signal.stop_loss:,}"
            )

    return alerts
