"""
Auto Trader – 신호 발생 시 자동 매수 실행

[v4] skip_overtrading_check — 2차 분할매수 재진입 제한 차단 버그 수정
[A단계] ATR 기반 투자금 자동 조정
[B단계] allocation.py 전략별 자금 배분 실제 연결

투자금 결정 우선순위:
  1. allocation.get_order_amount(strategy, confidence) → 전략·신뢰도 기반 금액
  2. _calc_atr_adjusted_amount()                      → ATR 변동성 기반 추가 조정
  3. _phase1_amount()                                 → 분할매수 1차 비율 적용

예시 (TOTAL_BUDGET=3,000,000 / breakout=60% / 신뢰도 0.8):
  전략 예산   = 3,000,000 × 60% = 1,800,000원
  1회 최대    = 1,800,000 × 20% = 360,000원
  신뢰도 100% = 360,000원
  ATR 3.0%    = 360,000 × (2.0/3.0) = 240,000원  ← 최종 투자금
  1차 매수    = 240,000 × 60% = 144,000원
"""
import logging
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from api.models import Signal, Trade
from trader import kis_client as kis
from notification.service import send_message

logger = logging.getLogger(__name__)

# ── 환경변수 ───────────────────────────────────────────────────────────────────
AUTO_TRADE_ENABLED   = os.getenv("AUTO_TRADE_ENABLED",   "true").lower() == "true"
MAX_AMOUNT_PER_STOCK = int(os.getenv("MAX_AMOUNT_PER_STOCK", "300000"))   # fallback
TARGET_PROFIT_PCT    = float(os.getenv("TARGET_PROFIT_PCT",  "0.05"))
STOP_LOSS_PCT        = float(os.getenv("STOP_LOSS_PCT",      "-0.02"))

SPLIT_BUY_ENABLED     = os.getenv("SPLIT_BUY_ENABLED",     "true").lower() == "true"
SPLIT_BUY_RATIO       = float(os.getenv("SPLIT_BUY_RATIO",       "0.6"))
SPLIT_BUY_TRIGGER_PCT = float(os.getenv("SPLIT_BUY_TRIGGER_PCT", "0.003"))
SPLIT_BUY_MIN_MINUTES = int(os.getenv("SPLIT_BUY_MIN_MINUTES",   "5"))
SPLIT_BUY_MAX_MINUTES = int(os.getenv("SPLIT_BUY_MAX_MINUTES",   "45"))
MAX_PHASE2_RISE_PCT   = float(os.getenv("MAX_PHASE2_RISE_PCT",   "0.03"))

# ── ATR 투자금 조정 파라미터 (A단계) ──────────────────────────────────────────
ATR_AMOUNT_ADJUST_ENABLED = os.getenv("ATR_AMOUNT_ADJUST_ENABLED", "true").lower() == "true"
ATR_BASE_PCT              = float(os.getenv("ATR_BASE_PCT",  "2.0"))
ATR_MIN_RATIO             = float(os.getenv("ATR_MIN_RATIO", "0.5"))


# ── B단계: 전략 기반 투자금 ────────────────────────────────────────────────────

def _get_strategy_amount(strategy: str, confidence: float) -> int:
    """
    allocation.py의 get_order_amount()로 전략·신뢰도 기반 투자금을 반환합니다.
    import 실패 시 MAX_AMOUNT_PER_STOCK fallback.

    신뢰도 등급:
      0.7+  → 전략예산 × MAX_SINGLE_TRADE_PCT × 100%
      0.4~  → 전략예산 × MAX_SINGLE_TRADE_PCT × 60%
      ~0.4  → 전략예산 × MAX_SINGLE_TRADE_PCT × 30%
    """
    try:
        from trader.allocation import get_order_amount
        amount = get_order_amount(strategy, confidence)
        logger.debug(f"[allocation] {strategy} / 신뢰도 {confidence:.2f} → {amount:,}원")
        return amount
    except Exception as e:
        logger.warning(f"[allocation] import 실패 — fallback: {e}")
        return MAX_AMOUNT_PER_STOCK


# ── A단계: ATR 기반 투자금 조정 ──────────────────────────────────────────────

def _calc_atr_adjusted_amount(base_amount: int, atr_pct: float | None, code: str = "") -> int:
    """
    ATR%에 따라 투자금을 동적으로 조정합니다.
    ATR% ≤ ATR_BASE_PCT: 전액 / 초과 시 비례 축소 (최소 ATR_MIN_RATIO 보장)
    """
    if not ATR_AMOUNT_ADJUST_ENABLED or atr_pct is None or atr_pct <= 0:
        return base_amount
    if atr_pct <= ATR_BASE_PCT:
        return base_amount

    ratio    = max(min(ATR_BASE_PCT / atr_pct, 1.0), ATR_MIN_RATIO)
    adjusted = int(base_amount * ratio)
    logger.info(
        f"[{code}] ATR 투자금 조정: {base_amount:,}원 × {ratio:.2f} = {adjusted:,}원 "
        f"(ATR% {atr_pct:.2f}%)"
    )
    return adjusted


# ── 수량 / 분할매수 금액 ──────────────────────────────────────────────────────

def calc_quantity(price: float, max_amount: int) -> int:
    if price <= 0:
        return 0
    return max(int(max_amount // price), 1)


def _phase1_amount(total: int) -> int:
    return int(total * SPLIT_BUY_RATIO) if SPLIT_BUY_ENABLED else total


def _phase2_amount(total: int) -> int:
    return total - _phase1_amount(total)


# ── 포지션 보유 확인 ──────────────────────────────────────────────────────────

async def _has_open_position(db: AsyncSession, code: str) -> bool:
    """수량 기반 오픈 포지션 확인 (signal_id NULL 버그 수정)"""
    from sqlalchemy import func
    bought = (await db.execute(
        select(func.coalesce(func.sum(Trade.quantity), 0)).where(and_(
            Trade.code == code,
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
    )).scalar() or 0
    sold = (await db.execute(
        select(func.coalesce(func.sum(Trade.quantity), 0)).where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status.in_(["FILLED", "CLOSED"]),
        ))
    )).scalar() or 0
    return bought > sold


# ── 매수 실행 ─────────────────────────────────────────────────────────────────

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
        logger.warning(f"[{code}] 수량 0 — 매수 건너뜀 (price={current_price}, amount={amount})")
        return {}

    actual_amount = current_price * quantity

    try:
        result = await kis.buy_order(code, quantity, order_type="01")
    except Exception as e:
        logger.error(f"[{code}] {phase}차 매수 실패: {e}")
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
        "trade_id": trade_id, "code": code, "name": name,
        "price": current_price, "quantity": quantity, "amount": actual_amount,
        "phase": phase, "status": status,
        "order_no": result.get("order_no", ""), "success": result["success"],
    }


# ── 신호 자동 매수 ────────────────────────────────────────────────────────────

async def auto_execute_signals(db: AsyncSession, signals: list[dict]) -> list[dict]:
    """
    신호 목록을 받아 자동 매수 주문을 실행합니다.

    투자금 결정:
      [B단계] allocation.get_order_amount(strategy, confidence) → 전략·신뢰도 기반
      [A단계] _calc_atr_adjusted_amount() → ATR 변동성 추가 조정
    """
    if not signals:
        return []
    if not AUTO_TRADE_ENABLED:
        logger.info("AUTO_TRADE_ENABLED=false")
        return []

    from trader.risk_manager import can_buy, check_slippage
    buyable, reason = await can_buy(db)
    if not buyable:
        logger.info(f"매수 차단: {reason}")
        # 같은 사유 30분 내 중복 알림 방지
        import time as _time
        _now = _time.time()
        _last = getattr(auto_execute_signals, "_last_block_alert", 0)
        _last_reason = getattr(auto_execute_signals, "_last_block_reason", "")
        if _now - _last > 1800 or _last_reason != reason:
            await send_message(f"⛔ <b>[AI INVEST] 매수 차단</b>\n사유: {reason}")
            auto_execute_signals._last_block_alert = _now
            auto_execute_signals._last_block_reason = reason
        return []

    # ── 시장 레짐 필터 ──────────────────────────────────────────────────
    from trader.market_regime import get_market_regime
    regime, breadth = await get_market_regime(db)
    if regime == "bear":
        logger.info(f"[레짐] 하락장 감지 ({breadth:.1%}) — 신규 매수 전체 차단")
        return []
    executed = []

    for sig in signals:
        code       = sig["code"]
        name       = sig["name"]
        signal_id  = sig["id"]
        sig_price  = sig["price"]
        strategy   = sig.get("strategy", "breakout")
        confidence = sig.get("confidence", 0.5)
        atr_pct    = sig.get("atr_pct")

        if await _has_open_position(db, code):
            logger.info(f"[{code}] 이미 보유 중 — 건너뜀")
            continue

        from trader.risk_manager import check_blacklist
        is_bl, bl_reason = await check_blacklist(db, code)
        if is_bl:
            logger.info(f"[{code}] 블랙리스트 — {bl_reason}")
            continue

        try:
            current_price = (await kis.get_current_price(code))["price"] or int(sig_price)
        except Exception as e:
            logger.warning(f"[{code}] 현재가 조회 실패: {e}")
            current_price = int(sig_price)

        # 신호 유효시간 체크 (45분 초과 신호 제외 — 시스템 다운 후 묵은 신호 방지)
        signal_age_minutes = (datetime.utcnow() - datetime.fromisoformat(sig.get("created_at", datetime.utcnow().isoformat()))).total_seconds() / 60
        if signal_age_minutes > 45:
            logger.info(f"[{code}] 신호 만료 ({signal_age_minutes:.0f}분 경과) — 건너뜀")
            continue

        slip_exceeded, slip_pct = await check_slippage(sig_price, current_price)
        if slip_exceeded:
            logger.info(f"[{code}] 슬리피지 초과 ({slip_pct*100:.2f}%)")
            continue

        # ── 투자금 결정: B단계(전략배분) → A단계(ATR조정) ─────────────────
        strategy_amount = _get_strategy_amount(strategy, confidence)
        adjusted_total  = _calc_atr_adjusted_amount(strategy_amount, atr_pct, code)
        phase1_amt      = _phase1_amount(adjusted_total)

        # 현재가가 1차금액보다 비싸도 최소 1주는 매수 (대형주 스킵 방지)
        if current_price > phase1_amt:
            if current_price > adjusted_total:
                logger.info(f"[{code}] 현재가 {current_price:,} > 총배분금액 {adjusted_total:,} — 건너뜀")
                continue
            # 1주만 매수
            phase1_amt = current_price
            logger.info(f"[{code}] 현재가 > 1차금액 → 1주 매수로 조정 ({current_price:,}원)")

        trade_data = await _execute_buy(
            db, code, name, signal_id, current_price, phase1_amt, phase=1
        )
        if not trade_data:
            continue

        await db.execute(
            Signal.__table__.update()
            .where(Signal.id == signal_id)
            .values(is_executed=True)
        )
        executed.append(trade_data)

        # 알림
        target_price = round(current_price * (1 + TARGET_PROFIT_PCT))
        stop_price   = round(current_price * (1 + STOP_LOSS_PCT))
        phase2_note  = (
            f"\n🔄 2차 매수: {SPLIT_BUY_MIN_MINUTES}분 후 +{SPLIT_BUY_TRIGGER_PCT*100:.1f}% 상승 시"
            if SPLIT_BUY_ENABLED else ""
        )
        alloc_note = f"\n💼 {strategy.upper()} {confidence:.0%} | 배분 {adjusted_total:,}원"
        atr_note   = (
            f" (ATR {atr_pct:.1f}%→{adjusted_total/strategy_amount*100:.0f}% 적용)"
            if ATR_AMOUNT_ADJUST_ENABLED and atr_pct and adjusted_total < strategy_amount else ""
        )

        emoji = "✅" if trade_data["success"] else "❌"
        await send_message(
            f"{emoji} <b>[AI INVEST] 자동 매수 (1차)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{name} ({code})</b>\n"
            f"💰 매수가: {current_price:,}원\n"
            f"🔢 수량: {trade_data['quantity']}주\n"
            f"💵 투자금: {trade_data['amount']:,}원{alloc_note}{atr_note}\n"
            f"🎯 목표가: {target_price:,}원 / 🛑 손절가: {stop_price:,}원"
            f"{phase2_note}\n"
            f"{'✅ 주문 성공' if trade_data['success'] else '❌ 주문 실패'}"
        )
        logger.info(
            f"1차 매수: {code} {name} {trade_data['quantity']}주 @ {current_price:,}원 "
            f"({strategy} / 신뢰도 {confidence:.2f} / 배분 {adjusted_total:,}원)"
        )

    await db.commit()
    logger.info(f"자동 매수 완료: {len(executed)}건")
    return executed


# ── 2차 분할매수 체크 ─────────────────────────────────────────────────────────

async def check_and_execute_phase2(db: AsyncSession) -> list[dict]:
    """
    2차 매수 조건 체크 및 실행 (장중 10분마다 호출).

    [v4] skip_position_check=True + skip_overtrading_check=True
      2차 매수는 기존 포지션 추가이므로:
      - MAX_POSITIONS 체크 불필요
      - 재진입 제한(REENTRY_MINUTES) 불필요
      - 일일 손실 한도 / 블랙리스트는 항상 적용
    """
    if not SPLIT_BUY_ENABLED:
        return []

    from trader.risk_manager import can_buy, is_market_open
    if not is_market_open():
        return []

    now_utc    = datetime.utcnow()
    min_cutoff = now_utc - timedelta(minutes=SPLIT_BUY_MIN_MINUTES)
    max_cutoff = now_utc - timedelta(minutes=SPLIT_BUY_MAX_MINUTES)

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
        if (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == t1.signal_id, Trade.order_type == "SELL",
            ))
        )).scalars().first():
            continue

        # 이미 2차 됐는지
        if (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == t1.signal_id, Trade.order_type == "BUY", Trade.phase == 2,
            ))
        )).scalars().first():
            continue

        # [v4] skip_position_check=True + skip_overtrading_check=True
        buyable, reason = await can_buy(
            db, code=t1.code,
            skip_position_check=True,
            skip_overtrading_check=True,
        )
        if not buyable:
            logger.info(f"[{t1.code}] 2차 차단: {reason}")
            continue

        try:
            current_price = (await kis.get_current_price(t1.code))["price"]
        except Exception as e:
            logger.warning(f"[{t1.code}] 2차 현재가 실패: {e}")
            continue

        trigger_price = t1.price * (1 + SPLIT_BUY_TRIGGER_PCT)
        if current_price < trigger_price:
            logger.debug(f"[{t1.code}] 2차 트리거 미충족: {current_price:,} < {trigger_price:,.0f}")
            continue

        if current_price > t1.price * (1 + MAX_PHASE2_RISE_PCT):
            logger.info(f"[{t1.code}] 2차 포기: 급등")
            continue

        if current_price >= t1.price * (1 + TARGET_PROFIT_PCT) * 0.9:
            logger.info(f"[{t1.code}] 2차 포기: 목표가 근접")
            continue

        if current_price <= t1.price * (1 + STOP_LOSS_PCT):
            logger.info(f"[{t1.code}] 2차 중단: 손절 근접")
            continue

        # 1차 매수 총액 역산으로 2차 금액 계산
        inferred_total = int(t1.amount / SPLIT_BUY_RATIO) if SPLIT_BUY_ENABLED else MAX_AMOUNT_PER_STOCK
        phase2_amt     = _phase2_amount(inferred_total)
        if phase2_amt <= 0:
            continue

        trade_data = await _execute_buy(
            db, t1.code, t1.name, t1.signal_id,
            current_price, phase2_amt, phase=2, parent_trade_id=t1.id
        )
        if not trade_data:
            logger.warning(f"[{t1.code}] 2차 매수 실패")
            continue

        await db.commit()
        executed.append(trade_data)

        total_qty    = t1.quantity + trade_data["quantity"]
        avg_price    = (t1.price * t1.quantity + current_price * trade_data["quantity"]) / total_qty
        total_amount = t1.amount + trade_data["amount"]

        await send_message(
            f"✅ <b>[AI INVEST] 자동 매수 (2차)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{t1.name} ({t1.code})</b>\n"
            f"💰 2차 매수가: {current_price:,}원 / 1차 대비 +{(current_price/t1.price-1)*100:.2f}%\n"
            f"🔢 2차: {trade_data['quantity']}주 / {trade_data['amount']:,}원\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 평균가: {avg_price:,.0f}원 / 총 {total_qty}주 / {total_amount:,}원"
        )
        logger.info(
            f"2차 매수: {t1.code} {t1.name} @ {current_price:,}원 (평균 {avg_price:,.0f}원)"
        )

    if executed:
        logger.info(f"2차 매수 완료: {len(executed)}건")
    return executed
