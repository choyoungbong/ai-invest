"""
Auto Exit – 손절(-2%) 및 목표수익(+3%) 도달 시 자동 매도 실행

check_and_execute_auto_exit() 를 스케줄러에서 주기적으로 호출합니다.
"""
import logging
import os
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from api.models import Signal, Trade
from trader import kis_client as kis
from notification.service import send_message

logger = logging.getLogger(__name__)

# ── 자동 청산 파라미터 ─────────────────────────────────────────────────────────
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "-0.02"))  # -2% 손절
TARGET_PROFIT_PCT = float(os.getenv("TARGET_PROFIT_PCT", "0.03"))  # +3% 목표수익


async def _save_sell_trade(
    db: AsyncSession,
    trade: Trade,
    current_price: int,
    reason: str,
) -> dict:
    """매도 주문 실행 + Trade 기록 저장"""
    try:
        result = await kis.sell_order(trade.code, trade.quantity, order_type="01")
    except Exception as e:
        logger.error(f"자동 매도 실패 [{trade.code}]: {e}")
        return {}

    trade_id = str(uuid.uuid4())
    profit_pct = (current_price / trade.price - 1) * 100
    amount = current_price * trade.quantity

    await db.execute(
        Trade.__table__.insert().values(
            id=trade_id,
            signal_id=trade.signal_id,
            code=trade.code,
            name=trade.name,
            order_type="SELL",
            price=current_price,
            quantity=trade.quantity,
            amount=amount,
            status="FILLED" if result["success"] else "FAILED",
            broker_order_id=result.get("order_no", ""),
            filled_at=datetime.utcnow() if result["success"] else None,
        )
    )
    await db.commit()

    # 텔레그램 알림
    emoji = "✅" if profit_pct > 0 else "🔴"
    await send_message(
        f"{emoji} <b>[AI INVEST] 자동 {'익절' if profit_pct > 0 else '손절'} 실행</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 종목: <b>{trade.name} ({trade.code})</b>\n"
        f"📋 사유: {reason}\n"
        f"💰 매수가: {trade.price:,}원\n"
        f"💱 매도가: {current_price:,}원\n"
        f"📊 수익률: {profit_pct:+.2f}%\n"
        f"🔢 수량: {trade.quantity}주\n"
        f"💵 손익: {(current_price - trade.price) * trade.quantity:+,.0f}원\n"
        f"{'✅ 주문 성공' if result['success'] else '❌ 주문 실패'}"
    )

    return {
        "code":        trade.code,
        "name":        trade.name,
        "buy_price":   trade.price,
        "sell_price":  current_price,
        "quantity":    trade.quantity,
        "profit_pct":  round(profit_pct, 2),
        "reason":      reason,
        "success":     result["success"],
    }


async def check_and_execute_auto_exit(db: AsyncSession) -> list[dict]:
    """
    체결된 BUY 포지션을 순회하며 자동 청산 조건을 확인합니다.

    청산 조건:
      - 손절: 현재가 ≤ 매수가 × (1 + STOP_LOSS_PCT)    기본 -2%
      - 익절: 현재가 ≥ 매수가 × (1 + TARGET_PROFIT_PCT) 기본 +3%
    """
    stmt = (
        select(Trade)
        .where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
        .order_by(desc(Trade.created_at))
        .limit(30)
    )
    buy_trades = (await db.execute(stmt)).scalars().all()

    executed = []

    for trade in buy_trades:
        # 이미 청산된 포지션 건너뜀
        sell_exists = (await db.execute(
            select(Trade).where(and_(
                Trade.code == trade.code,
                Trade.order_type == "SELL",
                Trade.signal_id == trade.signal_id,
            ))
        )).scalars().first()
        if sell_exists:
            continue

        # 현재가 조회
        try:
            price_data    = await kis.get_current_price(trade.code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"현재가 조회 실패 [{trade.code}]: {e}")
            continue

        stop_loss_price = trade.price * (1 + STOP_LOSS_PCT)
        target_price    = trade.price * (1 + TARGET_PROFIT_PCT)
        profit_pct      = (current_price / trade.price - 1) * 100

        # ── 손절 조건 ───────────────────────────────────────────────────────
        if current_price <= stop_loss_price:
            logger.warning(
                f"손절 실행: {trade.code} {trade.name} "
                f"현재가 {current_price:,} ≤ 손절가 {stop_loss_price:,.0f} ({profit_pct:.2f}%)"
            )
            result = await _save_sell_trade(
                db, trade, current_price,
                f"손절 ({profit_pct:.2f}%) — 손절가 {stop_loss_price:,.0f}원 도달"
            )
            if result:
                executed.append(result)

        # ── 익절 조건 ───────────────────────────────────────────────────────
        elif current_price >= target_price:
            logger.info(
                f"익절 실행: {trade.code} {trade.name} "
                f"현재가 {current_price:,} ≥ 목표가 {target_price:,.0f} ({profit_pct:.2f}%)"
            )
            result = await _save_sell_trade(
                db, trade, current_price,
                f"목표수익 달성 ({profit_pct:.2f}%) — 목표가 {target_price:,.0f}원 도달"
            )
            if result:
                executed.append(result)

        else:
            logger.debug(
                f"유지: {trade.code} {trade.name} "
                f"현재가 {current_price:,} ({profit_pct:+.2f}%) "
                f"[손절 {stop_loss_price:,.0f} / 목표 {target_price:,.0f}]"
            )

    if executed:
        logger.info(f"자동 청산 실행 완료: {len(executed)}건")

    return executed


# 기존 함수명 호환성 유지
check_and_execute_stop_loss = check_and_execute_auto_exit
