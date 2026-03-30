"""
Auto Trader – 신호 발생 시 자동 매수 실행 (분할매수 지원)

분할매수 전략:
  1차 매수: 신호 발생 즉시 예산의 SPLIT_BUY_RATIO(기본 50%) 만큼 매수
  2차 매수: 1차 매수 후 최소 SPLIT_BUY_MIN_MINUTES(10분) 경과 &
            현재가가 1차 매수가 대비 SPLIT_BUY_TRIGGER_PCT(+0.5%) 이상 상승 확인 시
            나머지 예산(50%)으로 추가 매수

환경변수:
  AUTO_TRADE_ENABLED    : true/false (기본 true)
  MAX_AMOUNT_PER_STOCK  : 종목당 최대 투자금액 (기본 300000원)
  SPLIT_BUY_ENABLED     : true/false — 분할매수 활성화 (기본 true)
  SPLIT_BUY_RATIO       : 1차 매수 비율 0.0~1.0 (기본 0.5 = 50%)
  SPLIT_BUY_TRIGGER_PCT : 2차 매수 트리거 상승률 (기본 0.005 = +0.5%)
  SPLIT_BUY_MIN_MINUTES : 2차 매수 최소 대기 시간 분 (기본 10)
  SPLIT_BUY_MAX_MINUTES : 2차 매수 유효 기간 분 — 초과 시 포기 (기본 60)
"""
import logging
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from api.models import Signal, Trade
from trader import kis_client as kis
from notification.service import send_message

logger = logging.getLogger(__name__)

# ── 환경변수 ───────────────────────────────────────────────────────────────────
AUTO_TRADE_ENABLED    = os.getenv("AUTO_TRADE_ENABLED",    "true").lower() == "true"
MAX_AMOUNT_PER_STOCK  = int(os.getenv("MAX_AMOUNT_PER_STOCK", "300000"))
TARGET_PROFIT_PCT     = float(os.getenv("TARGET_PROFIT_PCT",  "0.05"))
STOP_LOSS_PCT         = float(os.getenv("STOP_LOSS_PCT",      "-0.02"))

SPLIT_BUY_ENABLED     = os.getenv("SPLIT_BUY_ENABLED",     "true").lower() == "true"
SPLIT_BUY_RATIO       = float(os.getenv("SPLIT_BUY_RATIO",       "0.5"))   # 1차 매수 비율
SPLIT_BUY_TRIGGER_PCT = float(os.getenv("SPLIT_BUY_TRIGGER_PCT", "0.005")) # 2차 트리거 +0.5%
SPLIT_BUY_MIN_MINUTES = int(os.getenv("SPLIT_BUY_MIN_MINUTES",   "10"))    # 최소 대기
SPLIT_BUY_MAX_MINUTES = int(os.getenv("SPLIT_BUY_MAX_MINUTES",   "60"))    # 최대 유효기간


def calc_quantity(price: float, max_amount: int) -> int:
    """최대 금액 이내에서 최대 수량을 계산합니다."""
    if price <= 0:
        return 0
    qty = int(max_amount // price)
    return max(qty, 1)


def _phase1_amount() -> int:
    """1차 매수 금액"""
    if SPLIT_BUY_ENABLED:
        return int(MAX_AMOUNT_PER_STOCK * SPLIT_BUY_RATIO)
    return MAX_AMOUNT_PER_STOCK


def _phase2_amount() -> int:
    """2차 매수 금액 (나머지)"""
    return MAX_AMOUNT_PER_STOCK - _phase1_amount()


async def _has_open_position(db: AsyncSession, code: str) -> bool:
    """이미 해당 종목의 미청산 BUY 포지션이 존재하는지 확인"""
    buy = (await db.execute(
        select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
    )).scalars().first()

    if not buy:
        return False

    sell = (await db.execute(
        select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.signal_id == buy.signal_id,
        ))
    )).scalars().first()

    return sell is None


async def _execute_buy(
    db: AsyncSession,
    code: str,
    name: str,
    signal_id: str,
    current_price: int,
    amount: int,
    phase: int = 1,
    parent_trade_id: str | None = None,
) -> dict:
    """
    실제 KIS 매수 주문을 실행하고 Trade 레코드를 저장합니다.
    Returns: trade_data dict (성공 시) or {} (실패 시)
    """
    quantity = calc_quantity(current_price, amount)
    if quantity <= 0:
        logger.warning(f"[{code}] 수량 계산 0 — 매수 건너뜀 (price={current_price}, amount={amount})")
        return {}

    actual_amount = current_price * quantity

    try:
        result = await kis.buy_order(code, quantity, order_type="01")  # 시장가
    except Exception as e:
        logger.error(f"[{code}] {phase}차 매수 주문 실패: {e}")
        return {}

    trade_id = str(uuid.uuid4())
    status   = "FILLED" if result["success"] else "FAILED"

    # ── 수수료 계산 ──────────────────────────────────────────────────────────
    from trader.risk_manager import calc_commission
    commission = calc_commission(current_price, quantity, is_buy=True)

    await db.execute(
        Trade.__table__.insert().values(
            id=trade_id,
            signal_id=signal_id,
            code=code,
            name=name,
            order_type="BUY",
            price=current_price,
            quantity=quantity,
            amount=actual_amount,
            commission=commission,
            phase=phase,
            parent_trade_id=parent_trade_id,
            status=status,
            broker_order_id=result.get("order_no", ""),
            filled_at=datetime.utcnow() if result["success"] else None,
        )
    )

    return {
        "trade_id":  trade_id,
        "code":      code,
        "name":      name,
        "price":     current_price,
        "quantity":  quantity,
        "amount":    actual_amount,
        "phase":     phase,
        "status":    status,
        "order_no":  result.get("order_no", ""),
        "success":   result["success"],
    }


async def auto_execute_signals(db: AsyncSession, signals: list[dict]) -> list[dict]:
    """
    신호 목록을 받아 자동 매수 주문을 실행합니다.

    분할매수 활성화 시:
      1차: 신호가 대비 현재가 슬리피지 확인 후 SPLIT_BUY_RATIO 금액으로 즉시 매수
      2차: check_and_execute_phase2() 로 별도 실행 (scheduler 호출)

    분할매수 비활성화 시:
      기존대로 MAX_AMOUNT_PER_STOCK 전액 즉시 매수
    """
    if not signals:
        return []

    if not AUTO_TRADE_ENABLED:
        logger.info("AUTO_TRADE_ENABLED=false — 자동 주문 비활성화 상태")
        return []

    # ── 리스크 매니저 통합 체크 ───────────────────────────────────────────────
    from trader.risk_manager import can_buy, check_slippage
    buyable, reason = await can_buy(db)
    if not buyable:
        logger.info(f"매수 차단: {reason}")
        await send_message(
            f"⛔ <b>[AI INVEST] 매수 차단</b>\n"
            f"사유: {reason}"
        )
        return []

    executed = []

    for sig in signals:
        code      = sig["code"]
        name      = sig["name"]
        signal_id = sig["id"]
        sig_price = sig["price"]

        # ── 이미 보유 중인지 확인 ─────────────────────────────────────────
        if await _has_open_position(db, code):
            logger.info(f"[{code}] 이미 보유 중 — 중복 매수 건너뜀")
            continue

        # ── 블랙리스트 확인 ───────────────────────────────────────────────
        from trader.risk_manager import check_blacklist
        is_blacklisted, bl_reason = await check_blacklist(db, code)
        if is_blacklisted:
            logger.info(f"[{code}] 블랙리스트 — {bl_reason}")
            continue

        # ── 현재가 조회 ───────────────────────────────────────────────────
        try:
            price_data    = await kis.get_current_price(code)
            current_price = price_data["price"] or int(sig_price)
        except Exception as e:
            logger.warning(f"[{code}] 현재가 조회 실패: {e} — 신호가 사용")
            current_price = int(sig_price)

        # ── 슬리피지 체크 ─────────────────────────────────────────────────
        slip_exceeded, slip_pct = await check_slippage(sig_price, current_price)
        if slip_exceeded:
            logger.info(f"[{code}] 슬리피지 초과 ({slip_pct*100:.2f}%) — 건너뜀")
            continue

        # ── 최대 금액 초과 종목 제외 ──────────────────────────────────────
        phase1_amt = _phase1_amount()
        if current_price > phase1_amt:
            logger.info(
                f"[{code}] {name} 현재가 {current_price:,}원 > "
                f"1차 매수금액 {phase1_amt:,}원 — 건너뜀"
            )
            await send_message(
                f"⏭️ <b>[AI INVEST] 매수 건너뜀</b>\n"
                f"📌 {name} ({code})\n"
                f"💰 현재가 {current_price:,}원 > 1차 매수금액 {phase1_amt:,}원"
            )
            continue

        # ── 1차 매수 실행 ─────────────────────────────────────────────────
        trade_data = await _execute_buy(
            db, code, name, signal_id,
            current_price, phase1_amt,
            phase=1,
        )

        if not trade_data:
            continue

        # 신호 실행 표시
        await db.execute(
            Signal.__table__.update()
            .where(Signal.id == signal_id)
            .values(is_executed=True)
        )

        executed.append(trade_data)

        # ── 텔레그램 1차 매수 알림 ────────────────────────────────────────
        target_price = round(current_price * (1 + TARGET_PROFIT_PCT))
        stop_price   = round(current_price * (1 + STOP_LOSS_PCT))
        phase2_note  = (
            f"\n🔄 2차 매수 예정: {SPLIT_BUY_MIN_MINUTES}분 후 "
            f"+{SPLIT_BUY_TRIGGER_PCT*100:.1f}% 상승 확인 시"
            if SPLIT_BUY_ENABLED else ""
        )

        emoji = "✅" if trade_data["success"] else "❌"
        await send_message(
            f"{emoji} <b>[AI INVEST] 자동 매수 (1차)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{name} ({code})</b>\n"
            f"💰 매수가: {current_price:,}원\n"
            f"🔢 수량: {trade_data['quantity']}주\n"
            f"💵 투자금액: {trade_data['amount']:,}원"
            f"  ({SPLIT_BUY_RATIO*100:.0f}%/{MAX_AMOUNT_PER_STOCK:,}원)\n"
            f"🎯 목표가: {target_price:,}원 (+{TARGET_PROFIT_PCT*100:.0f}%)\n"
            f"🛑 손절가: {stop_price:,}원 ({STOP_LOSS_PCT*100:.0f}%)"
            f"{phase2_note}\n"
            f"{'✅ 주문 성공' if trade_data['success'] else '❌ 주문 실패'}"
        )

        logger.info(
            f"1차 매수: {code} {name} "
            f"{trade_data['quantity']}주 @ {current_price:,}원 = {trade_data['amount']:,}원"
        )

    await db.commit()
    logger.info(f"자동 매수 완료: {len(executed)}건 (분할매수 {'활성' if SPLIT_BUY_ENABLED else '비활성'})")
    return executed


async def check_and_execute_phase2(db: AsyncSession) -> list[dict]:
    """
    2차 매수 조건 체크 및 실행.
    scheduler에서 장중 10분마다 호출합니다.

    조건 (모두 충족 시 실행):
      1. 1차 매수(phase=1, FILLED) 후 MIN_MINUTES 이상 ~ MAX_MINUTES 미만 경과
      2. 현재가 >= 1차 매수가 × (1 + SPLIT_BUY_TRIGGER_PCT) — 상승 추세 확인
      3. 현재가 < 1차 매수가 × (1 + TARGET_PROFIT_PCT) × 0.9 — 목표가 너무 근접하지 않음
      4. 2차 매수 미실행 (해당 signal_id에 phase=2 BUY 없음)
      5. 청산 미완료 (해당 signal_id에 SELL 없음)
      6. 손절 조건 미도달 (현재가 > 손절가)
    """
    if not SPLIT_BUY_ENABLED:
        return []

    from trader.risk_manager import can_buy, is_market_open
    if not is_market_open():
        return []

    now_utc     = datetime.utcnow()
    min_cutoff  = now_utc - timedelta(minutes=SPLIT_BUY_MIN_MINUTES)
    max_cutoff  = now_utc - timedelta(minutes=SPLIT_BUY_MAX_MINUTES)

    # phase=1 FILLED 매수 중 MIN~MAX 범위 내
    phase1_trades = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
            Trade.phase == 1,
            Trade.created_at <= min_cutoff,   # 최소 대기 경과
            Trade.created_at >= max_cutoff,   # 최대 유효기간 내
        ))
    )).scalars().all()

    executed = []

    for t1 in phase1_trades:
        # ── 이미 청산됐는지 확인 ──────────────────────────────────────────
        sell_exists = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == t1.signal_id,
                Trade.order_type == "SELL",
            ))
        )).scalars().first()
        if sell_exists:
            continue

        # ── 이미 2차 매수 실행됐는지 확인 ────────────────────────────────
        phase2_exists = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == t1.signal_id,
                Trade.order_type == "BUY",
                Trade.phase == 2,
            ))
        )).scalars().first()
        if phase2_exists:
            continue

        # ── 2차 매수 가능 여부 (전체 리스크 체크) ────────────────────────
        buyable, reason = await can_buy(db, code=t1.code)
        if not buyable:
            logger.info(f"[{t1.code}] 2차 매수 차단: {reason}")
            continue

        # ── 현재가 조회 ───────────────────────────────────────────────────
        try:
            price_data    = await kis.get_current_price(t1.code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"[{t1.code}] 2차 매수 현재가 조회 실패: {e}")
            continue

        # ── 조건 2: 트리거 상승 확인 ──────────────────────────────────────
        trigger_price = t1.price * (1 + SPLIT_BUY_TRIGGER_PCT)
        if current_price < trigger_price:
            logger.debug(
                f"[{t1.code}] 2차 매수 조건 미충족: "
                f"현재가 {current_price:,} < 트리거 {trigger_price:,.0f}"
            )
            continue

        # ── 조건 3: 목표가 너무 근접하지 않음 ────────────────────────────
        near_target = t1.price * (1 + TARGET_PROFIT_PCT) * 0.9
        if current_price >= near_target:
            logger.info(
                f"[{t1.code}] 2차 매수 포기: 목표가 근접 "
                f"(현재가 {current_price:,} ≥ {near_target:,.0f})"
            )
            continue

        # ── 조건 6: 손절 조건 미도달 ─────────────────────────────────────
        stop_price = t1.price * (1 + STOP_LOSS_PCT)
        if current_price <= stop_price:
            logger.info(f"[{t1.code}] 2차 매수 중단: 손절 근접")
            continue

        # ── 2차 매수 실행 ─────────────────────────────────────────────────
        phase2_amt = _phase2_amount()
        if phase2_amt <= 0:
            logger.debug(f"[{t1.code}] 2차 매수 금액 0 — 전액 1차 매수")
            continue

        trade_data = await _execute_buy(
            db, t1.code, t1.name, t1.signal_id,
            current_price, phase2_amt,
            phase=2,
            parent_trade_id=t1.id,
        )

        if not trade_data:
            continue

        await db.commit()
        executed.append(trade_data)

        # ── 가중평균 매수가 계산 ──────────────────────────────────────────
        total_qty = t1.quantity + trade_data["quantity"]
        avg_price = (
            (t1.price * t1.quantity + current_price * trade_data["quantity"]) / total_qty
        )
        total_amount = t1.amount + trade_data["amount"]

        await send_message(
            f"✅ <b>[AI INVEST] 자동 매수 (2차)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{t1.name} ({t1.code})</b>\n"
            f"💰 2차 매수가: {current_price:,}원\n"
            f"🔢 2차 수량: {trade_data['quantity']}주\n"
            f"💵 2차 금액: {trade_data['amount']:,}원\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 평균 매수가: {avg_price:,.0f}원\n"
            f"🔢 총 보유 수량: {total_qty}주\n"
            f"💵 총 투자금액: {total_amount:,}원"
        )

        logger.info(
            f"2차 매수 완료: {t1.code} {t1.name} "
            f"{trade_data['quantity']}주 @ {current_price:,}원 "
            f"(평균가 {avg_price:,.0f}원)"
        )

    if executed:
        logger.info(f"2차 매수 완료: {len(executed)}건")

    return executed
