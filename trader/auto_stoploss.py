"""
Auto Exit – 손절 / 익절 / 하드스탑 자동 매도 실행

[분할매수 대응]
  signal_id 기준으로 모든 BUY 포지션(phase 1 + 2)을 집계하여
  가중평균 매수가로 손절/익절 판단 후 총 수량 일괄 청산합니다.

[블랙리스트 연동]
  손절 청산 시 해당 종목을 BLACKLIST_DAYS 동안 재진입 금지 처리합니다.

환경변수:
  STOP_LOSS_PCT      : 손절 비율 (기본 -0.02)
  HARD_STOP_PCT      : 하드 손절 비율 (기본 -0.03)
  TARGET_PROFIT_PCT  : 익절 비율 (기본 0.05)
  BLACKLIST_DAYS     : 손절 후 블랙리스트 기간 (기본 3일)
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
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",      "-0.02"))
HARD_STOP_PCT     = float(os.getenv("HARD_STOP_PCT",      "-0.03"))
TARGET_PROFIT_PCT = float(os.getenv("TARGET_PROFIT_PCT",   "0.05"))
BLACKLIST_DAYS    = int(os.getenv("BLACKLIST_DAYS",         "3"))


async def _get_open_position(db: AsyncSession, signal_id: str) -> dict | None:
    """
    signal_id 기준으로 미청산 BUY 포지션을 집계합니다.
    분할매수(phase 1 + 2) 포지션을 통합하여 반환합니다.

    Returns:
        {
            code, name, signal_id,
            total_quantity,
            avg_buy_price,     # 가중평균 매수가
            total_amount,
            phases: [trade, ...]  # 원본 Trade 객체 목록
        }
        또는 None (포지션 없음 / 이미 청산)
    """
    # 해당 signal_id에 SELL 이미 있으면 청산 완료
    sell_exists = (await db.execute(
        select(Trade).where(and_(
            Trade.signal_id == signal_id,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
        ))
    )).scalars().first()
    if sell_exists:
        return None

    # 모든 BUY 포지션 수집 (phase 1 + 2)
    buy_trades = (await db.execute(
        select(Trade).where(and_(
            Trade.signal_id == signal_id,
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
    )).scalars().all()

    if not buy_trades:
        return None

    total_quantity = sum(t.quantity for t in buy_trades)
    total_cost     = sum(t.price * t.quantity for t in buy_trades)
    avg_buy_price  = total_cost / total_quantity if total_quantity > 0 else 0
    total_amount   = sum(t.amount for t in buy_trades)

    return {
        "code":           buy_trades[0].code,
        "name":           buy_trades[0].name,
        "signal_id":      signal_id,
        "total_quantity": total_quantity,
        "avg_buy_price":  avg_buy_price,
        "total_amount":   total_amount,
        "phases":         buy_trades,
    }


async def _execute_sell(
    db: AsyncSession,
    position: dict,
    current_price: int,
    reason: str,
) -> dict:
    """
    매도 주문 실행 + Trade 기록 저장.
    분할매수된 포지션은 total_quantity 전량 일괄 매도합니다.
    """
    code           = position["code"]
    name           = position["name"]
    signal_id      = position["signal_id"]
    total_quantity = position["total_quantity"]
    avg_buy_price  = position["avg_buy_price"]

    try:
        result = await kis.sell_order(code, total_quantity, order_type="01")
    except Exception as e:
        logger.error(f"자동 매도 실패 [{code}]: {e}")
        return {}

    trade_id   = str(uuid.uuid4())
    profit_pct = (current_price / avg_buy_price - 1) * 100 if avg_buy_price > 0 else 0
    sell_amount = current_price * total_quantity

    # ── 수수료/수익 계산 ──────────────────────────────────────────────────────
    from trader.risk_manager import calc_net_profit, calc_commission
    pnl        = calc_net_profit(avg_buy_price, current_price, total_quantity)
    sell_comm  = calc_commission(current_price, total_quantity, is_buy=False)
    total_comm = sum((t.commission or 0) for t in position["phases"]) + sell_comm

    await db.execute(
        Trade.__table__.insert().values(
            id=trade_id,
            signal_id=signal_id,
            code=code,
            name=name,
            order_type="SELL",
            price=current_price,
            quantity=total_quantity,
            amount=sell_amount,
            commission=sell_comm,
            theory_profit=pnl["theory_profit"],
            real_profit=pnl["net_profit"],
            status="FILLED" if result["success"] else "FAILED",
            broker_order_id=result.get("order_no", ""),
            filled_at=datetime.utcnow() if result["success"] else None,
        )
    )
    await db.commit()

    # ── 손절 시 블랙리스트 등록 ───────────────────────────────────────────────
    is_loss = profit_pct < 0
    if is_loss and result["success"]:
        from trader.risk_manager import add_to_blacklist
        await add_to_blacklist(
            db, code, name,
            reason=f"손절 청산 ({profit_pct:.2f}%) — {BLACKLIST_DAYS}일 재진입 금지",
        )

    # ── 텔레그램 알림 ─────────────────────────────────────────────────────────
    emoji = "✅" if profit_pct > 0 else "🔴"
    phase_info = ""
    if len(position["phases"]) > 1:
        phase_info = (
            f"\n📦 분할매수: {len(position['phases'])}회 평균 {avg_buy_price:,.0f}원"
        )

    await send_message(
        f"{emoji} <b>[AI INVEST] 자동 {'익절' if profit_pct > 0 else '손절'} 실행</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 종목: <b>{name} ({code})</b>\n"
        f"📋 사유: {reason}\n"
        f"💰 평균 매수가: {avg_buy_price:,.0f}원{phase_info}\n"
        f"💱 매도가: {current_price:,}원\n"
        f"📊 수익률: {profit_pct:+.2f}%\n"
        f"🔢 수량: {total_quantity}주\n"
        f"💵 총 손익: {pnl['net_profit']:+,.0f}원 (수수료 {total_comm:,.0f}원 포함)\n"
        f"{'✅ 주문 성공' if result['success'] else '❌ 주문 실패'}"
        + (f"\n🚫 {BLACKLIST_DAYS}일 블랙리스트 등록" if is_loss and result["success"] else "")
    )

    return {
        "code":       code,
        "name":       name,
        "avg_price":  round(avg_buy_price, 0),
        "sell_price": current_price,
        "quantity":   total_quantity,
        "profit_pct": round(profit_pct, 2),
        "net_profit": pnl["net_profit"],
        "reason":     reason,
        "success":    result["success"],
    }


async def check_and_execute_auto_exit(db: AsyncSession) -> list[dict]:
    """
    체결된 BUY 포지션을 순회하며 자동 청산 조건을 확인합니다.
    분할매수 포지션은 signal_id 기준으로 통합하여 처리합니다.

    청산 조건:
      - 하드 손절: 현재가 ≤ 평균 매수가 × (1 + HARD_STOP_PCT)   기본 -3%
      - 일반 손절: 현재가 ≤ 평균 매수가 × (1 + STOP_LOSS_PCT)   기본 -2%
      - 익절:      현재가 ≥ 평균 매수가 × (1 + TARGET_PROFIT_PCT) 기본 +5%
    """
    from trader.risk_manager import require_market_open
    if not require_market_open("auto_exit"):
        return []

    # signal_id 기준으로 미청산 포지션 목록 조회 (중복 없이)
    signal_ids_q = (await db.execute(
        select(Trade.signal_id).where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        )).distinct()
    )).scalars().all()

    executed = []

    for signal_id in signal_ids_q:
        position = await _get_open_position(db, signal_id)
        if not position:
            continue

        code      = position["code"]
        name      = position["name"]
        avg_price = position["avg_buy_price"]

        # 현재가 조회
        try:
            price_data    = await kis.get_current_price(code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"현재가 조회 실패 [{code}]: {e}")
            continue

        hard_stop_price = avg_price * (1 + HARD_STOP_PCT)
        stop_loss_price = avg_price * (1 + STOP_LOSS_PCT)
        target_price    = avg_price * (1 + TARGET_PROFIT_PCT)
        profit_pct      = (current_price / avg_price - 1) * 100

        # ── 하드 손절 (-3%) ──────────────────────────────────────────────
        if current_price <= hard_stop_price:
            logger.warning(
                f"하드 손절: {code} {name} "
                f"현재가 {current_price:,} ≤ 하드손절가 {hard_stop_price:,.0f} ({profit_pct:.2f}%)"
            )
            result = await _execute_sell(
                db, position, current_price,
                f"🚨 하드 손절 ({profit_pct:.2f}%) — 하드손절가 {hard_stop_price:,.0f}원 도달"
            )
            if result:
                executed.append(result)

        # ── 일반 손절 (-2%) ──────────────────────────────────────────────
        elif current_price <= stop_loss_price:
            logger.warning(
                f"손절: {code} {name} "
                f"현재가 {current_price:,} ≤ 손절가 {stop_loss_price:,.0f} ({profit_pct:.2f}%)"
            )
            result = await _execute_sell(
                db, position, current_price,
                f"손절 ({profit_pct:.2f}%) — 손절가 {stop_loss_price:,.0f}원 도달"
            )
            if result:
                executed.append(result)

        # ── 익절 (+5%) ────────────────────────────────────────────────────
        elif current_price >= target_price:
            logger.info(
                f"익절: {code} {name} "
                f"현재가 {current_price:,} ≥ 목표가 {target_price:,.0f} ({profit_pct:.2f}%)"
            )
            result = await _execute_sell(
                db, position, current_price,
                f"목표수익 달성 ({profit_pct:.2f}%) — 목표가 {target_price:,.0f}원 도달"
            )
            if result:
                executed.append(result)

        else:
            logger.debug(
                f"유지: {code} {name} "
                f"현재가 {current_price:,} ({profit_pct:+.2f}%) "
                f"[손절 {stop_loss_price:,.0f} / 목표 {target_price:,.0f}]"
            )

    if executed:
        logger.info(f"자동 청산 완료: {len(executed)}건")

    return executed


# 기존 함수명 호환성 유지
check_and_execute_stop_loss = check_and_execute_auto_exit
