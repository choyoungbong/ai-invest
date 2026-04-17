"""
Auto Trader – 신호 발생 시 자동 매수 실행 (분할매수 지원)

분할매수 전략:
  1차 매수: 신호 발생 즉시 예산의 SPLIT_BUY_RATIO(기본 60%) 만큼 매수
  2차 매수: 1차 매수 후 SPLIT_BUY_MIN_MINUTES 경과 &
            SPLIT_BUY_TRIGGER_PCT(+0.3%) 이상 상승 확인 시 나머지 매수

[버그 수정 v4] 2차 분할매수 재진입 제한 차단 문제 수정
  문제: 2차 매수에서 can_buy(skip_position_check=True) 호출 시에도
        check_overtrading()의 일반 재진입 제한(REENTRY_MINUTES)에 걸려 차단됨
        "[035420] 2차 매수 차단: 동일 종목 재진입 제한 (잔여 97분)"
  원인: 2차 매수는 신규 종목 진입이 아닌 기존 포지션 추가인데,
        1차 매수 후 REENTRY_MINUTES(120분)가 경과하지 않아 차단
  수정: can_buy(skip_position_check=True, skip_overtrading_check=True)
        → 포지션 수 체크 + 재진입 제한 모두 건너뜀
        → 일일 손실 한도 / 블랙리스트는 그대로 적용

[이전 수정 이력]
  v3: skip_position_check로 MAX_POSITIONS 충돌 해결
  v2: 기존 버그 수정
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

SPLIT_BUY_ENABLED      = os.getenv("SPLIT_BUY_ENABLED",      "true").lower() == "true"
SPLIT_BUY_RATIO        = float(os.getenv("SPLIT_BUY_RATIO",        "0.6"))
SPLIT_BUY_TRIGGER_PCT  = float(os.getenv("SPLIT_BUY_TRIGGER_PCT",  "0.003"))
SPLIT_BUY_MIN_MINUTES  = int(os.getenv("SPLIT_BUY_MIN_MINUTES",    "5"))
SPLIT_BUY_MAX_MINUTES  = int(os.getenv("SPLIT_BUY_MAX_MINUTES",    "30"))
MAX_PHASE2_RISE_PCT    = float(os.getenv("MAX_PHASE2_RISE_PCT",    "0.03"))


def calc_quantity(price: float, max_amount: int) -> int:
    if price <= 0:
        return 0
    qty = int(max_amount // price)
    return max(qty, 1)


def _phase1_amount() -> int:
    if SPLIT_BUY_ENABLED:
        return int(MAX_AMOUNT_PER_STOCK * SPLIT_BUY_RATIO)
    return MAX_AMOUNT_PER_STOCK


def _phase2_amount() -> int:
    return MAX_AMOUNT_PER_STOCK - _phase1_amount()


async def _has_open_position(db: AsyncSession, code: str) -> bool:
    buy_trades = (await db.execute(
        select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
    )).scalars().all()

    if not buy_trades:
        return False

    for buy in buy_trades:
        sell = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == buy.signal_id,
                Trade.order_type == "SELL",
                Trade.status == "FILLED",
            ))
        )).scalars().first()
        if not sell:
            return True
    return False


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
    quantity = calc_quantity(current_price, amount)
    if quantity <= 0:
        logger.warning(f"[{code}] 수량 계산 0 — 매수 건너뜀 (price={current_price}, amount={amount})")
        return {}

    actual_amount = current_price * quantity

    try:
        result = await kis.buy_order(code, quantity, order_type="01")
    except Exception as e:
        logger.error(f"[{code}] {phase}차 매수 주문 실패: {e}")
        return {}

    trade_id   = str(uuid.uuid4())
    status     = "FILLED" if result["success"] else "FAILED"

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
    1차 매수 — 모든 리스크 체크 적용 (포지션 수 + 재진입 제한 포함)
    """
    if not signals:
        return []

    if not AUTO_TRADE_ENABLED:
        logger.info("AUTO_TRADE_ENABLED=false — 자동 주문 비활성화 상태")
        return []

    from trader.risk_manager import can_buy, check_slippage
    # 1차 매수: skip_position_check=False, skip_overtrading_check=False (기본 — 모든 체크)
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

        if await _has_open_position(db, code):
            logger.info(f"[{code}] 이미 보유 중 — 중복 매수 건너뜀")
            continue

        from trader.risk_manager import check_blacklist
        is_blacklisted, bl_reason = await check_blacklist(db, code)
        if is_blacklisted:
            logger.info(f"[{code}] 블랙리스트 — {bl_reason}")
            continue

        try:
            price_data    = await kis.get_current_price(code)
            current_price = price_data["price"] or int(sig_price)
        except Exception as e:
            logger.warning(f"[{code}] 현재가 조회 실패: {e} — 신호가 사용")
            current_price = int(sig_price)

        slip_exceeded, slip_pct = await check_slippage(sig_price, current_price)
        if slip_exceeded:
            logger.info(f"[{code}] 슬리피지 초과 ({slip_pct*100:.2f}%) — 건너뜀")
            continue

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

        trade_data = await _execute_buy(
            db, code, name, signal_id,
            current_price, phase1_amt,
            phase=1,
        )

        if not trade_data:
            continue

        await db.execute(
            Signal.__table__.update()
            .where(Signal.id == signal_id)
            .values(is_executed=True)
        )

        executed.append(trade_data)

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

    [버그 수정 v4] skip_overtrading_check=True 추가
      기존: can_buy(db, code=t1.code, skip_position_check=True)
            → check_overtrading()이 여전히 실행되어 REENTRY_MINUTES 제한에 걸림
            → "[035420] 2차 매수 차단: 동일 종목 재진입 제한 (잔여 97분)"
      수정: can_buy(db, code=t1.code, skip_position_check=True, skip_overtrading_check=True)
            → 포지션 수 체크 + 재진입 제한 모두 건너뜀
            → 일일 손실 한도 / 블랙리스트는 그대로 적용
    """
    if not SPLIT_BUY_ENABLED:
        return []

    from trader.risk_manager import can_buy, is_market_open
    if not is_market_open():
        return []

    now_utc     = datetime.utcnow()
    min_cutoff  = now_utc - timedelta(minutes=SPLIT_BUY_MIN_MINUTES)
    max_cutoff  = now_utc - timedelta(minutes=SPLIT_BUY_MAX_MINUTES)

    phase1_trades = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
            Trade.phase == 1,
            Trade.created_at <= min_cutoff,
            Trade.created_at >= max_cutoff,
        ))
    )).scalars().all()

    executed = []

    for t1 in phase1_trades:
        # 이미 청산됐는지
        sell_exists = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == t1.signal_id,
                Trade.order_type == "SELL",
            ))
        )).scalars().first()
        if sell_exists:
            continue

        # 이미 2차 매수 됐는지
        phase2_exists = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == t1.signal_id,
                Trade.order_type == "BUY",
                Trade.phase == 2,
            ))
        )).scalars().first()
        if phase2_exists:
            continue

        # ── [버그 수정 v4] skip_position_check=True + skip_overtrading_check=True ──
        # 2차 매수는 기존 포지션 추가 → 포지션 수 체크 & 재진입 제한 모두 불필요
        # 일일 손실 한도 / 블랙리스트는 항상 체크
        buyable, reason = await can_buy(
            db,
            code=t1.code,
            skip_position_check=True,
            skip_overtrading_check=True,  # ← 핵심 수정
        )
        if not buyable:
            logger.info(f"[{t1.code}] 2차 매수 차단: {reason}")
            continue

        # 현재가 조회
        try:
            price_data    = await kis.get_current_price(t1.code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"[{t1.code}] 2차 매수 현재가 조회 실패: {e}")
            continue

        # 조건 2: 트리거 상승 확인
        trigger_price = t1.price * (1 + SPLIT_BUY_TRIGGER_PCT)
        if current_price < trigger_price:
            logger.debug(
                f"[{t1.code}] 2차 매수 조건 미충족: "
                f"현재가 {current_price:,} < 트리거 {trigger_price:,.0f}"
            )
            continue

        # 급등 보호
        max_rise_price = t1.price * (1 + MAX_PHASE2_RISE_PCT)
        if current_price > max_rise_price:
            logger.info(
                f"[{t1.code}] 2차 매수 포기: 급등 감지 "
                f"현재가 {current_price:,} > 상한 {max_rise_price:,.0f} "
                f"(+{MAX_PHASE2_RISE_PCT*100:.0f}% 초과)"
            )
            continue

        # 목표가 너무 근접
        near_target = t1.price * (1 + TARGET_PROFIT_PCT) * 0.9
        if current_price >= near_target:
            logger.info(f"[{t1.code}] 2차 매수 포기: 목표가 근접")
            continue

        # 손절 근접
        stop_price = t1.price * (1 + STOP_LOSS_PCT)
        if current_price <= stop_price:
            logger.info(f"[{t1.code}] 2차 매수 중단: 손절 근접")
            continue

        # 2차 매수 금액
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
            logger.warning(
                f"[{t1.code}] {t1.name} 2차 매수 실패 "
                f"(1차 매수가: {t1.price:,}원, 현재가: {current_price:,}원)"
            )
            continue

        await db.commit()
        executed.append(trade_data)

        total_qty    = t1.quantity + trade_data["quantity"]
        avg_price    = (t1.price * t1.quantity + current_price * trade_data["quantity"]) / total_qty
        total_amount = t1.amount + trade_data["amount"]

        await send_message(
            f"✅ <b>[AI INVEST] 자동 매수 (2차)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{t1.name} ({t1.code})</b>\n"
            f"💰 2차 매수가: {current_price:,}원\n"
            f"📈 1차 대비: +{(current_price/t1.price-1)*100:.2f}%\n"
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
