"""
Auto Exit – 손절 / 익절 / 트레일링 스탑 / 하드스탑 자동 매도 실행

[분할매수 대응]
  signal_id 기준으로 모든 BUY 포지션(phase 1 + 2)을 집계하여
  가중평균 매수가로 손절/익절 판단 후 총 수량 일괄 청산합니다.

[블랙리스트 연동]
  손절 청산 시 해당 종목을 BLACKLIST_DAYS 동안 재진입 금지 처리합니다.

[트레일링 스탑 — v4 신규]
  기존 고정 익절(+5%)의 단점을 보완합니다.
  - 수익이 TRAILING_STOP_ACTIVATION_PCT(기본 +3%) 이상이 되면 트레일링 모드 전환
  - 이후 가격이 고점 대비 TRAILING_STOP_PCT(기본 -1.5%) 이하로 내려오면 즉시 청산
  - 트레일링 모드 중에는 고정 익절(+5%)이 아닌 트레일링이 우선 적용됨
  - 예시: 평균매수가 10,000원
      +3%(10,300원) 도달 → 트레일링 활성, 고점=10,300원
      10,500원 갱신 → 고점=10,500, 트레일링 청산선=10,500×0.985=10,343원
      10,340원으로 하락 → 청산선(10,343원) 이탈 → 매도! (+3.4% 수익 확정)

환경변수:
  STOP_LOSS_PCT               : 손절 비율 (기본 -0.015)
  HARD_STOP_PCT               : 하드 손절 비율 (기본 -0.025)
  TARGET_PROFIT_PCT           : 고정 익절 비율 (기본 0.05)
  TRAILING_STOP_ENABLED       : 트레일링 스탑 활성화 (기본 true)
  TRAILING_STOP_ACTIVATION_PCT: 트레일링 활성화 수익률 (기본 0.03 = +3%)
  TRAILING_STOP_PCT           : 고점 대비 하락 허용폭 (기본 0.015 = -1.5%)
  BLACKLIST_DAYS              : 손절 후 블랙리스트 기간 (기본 3일)

[버그 수정 이력]
  v2: pending_ids 미사용 버그 수정
  v3: risk_manager DB기반 일일 손실 플래그 연동
  v4: 트레일링 스탑 추가
"""
import logging
import os
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from api.models import Signal, Trade
from trader import kis_client as kis
from notification.service import send_message

logger = logging.getLogger(__name__)

# ── 청산 파라미터 ──────────────────────────────────────────────────────────────
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",      "-0.015"))
HARD_STOP_PCT     = float(os.getenv("HARD_STOP_PCT",      "-0.025"))
TARGET_PROFIT_PCT = float(os.getenv("TARGET_PROFIT_PCT",   "0.05"))

# ── 손절 보호 설정 ────────────────────────────────────────────────────────────
STOP_LOSS_SKIP_MINUTES  = int(os.getenv("STOP_LOSS_SKIP_MINUTES", "30"))  # 장 초반 손절 유예 (분)
STOP_LOSS_CONFIRM_COUNT = int(os.getenv("STOP_LOSS_CONFIRM_COUNT", "2"))  # 연속 확인 횟수

# 연속 손절 확인 카운터 (인메모리)
_stop_loss_counter: dict[str, int] = {}  # signal_id → 연속 손절 확인 횟수
BLACKLIST_DAYS    = int(os.getenv("BLACKLIST_DAYS",         "3"))

# ── 트레일링 스탑 파라미터 ─────────────────────────────────────────────────────
TRAILING_STOP_ENABLED        = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
TRAILING_STOP_ACTIVATION_PCT = float(os.getenv("TRAILING_STOP_ACTIVATION_PCT", "0.03"))
TRAILING_STOP_PCT            = float(os.getenv("TRAILING_STOP_PCT",            "0.015"))

# ── 트레일링 고점 인메모리 상태 ────────────────────────────────────────────────
# key: signal_id  /  value: 관측된 최고가
# 재시작 시 초기화되지만 손절/하드스탑이 항상 동작하므로 하방 리스크는 보호됨
_trailing_high: dict[str, float] = {}


def _update_trailing_high(signal_id: str, current_price: float) -> float:
    prev = _trailing_high.get(signal_id, 0.0)
    if current_price > prev:
        _trailing_high[signal_id] = current_price
    return _trailing_high[signal_id]


def _clear_trailing_state(signal_id: str) -> None:
    _trailing_high.pop(signal_id, None)


# ── 포지션 조회 ────────────────────────────────────────────────────────────────

async def _get_open_position(db: AsyncSession, signal_id: str) -> dict | None:
    sell_filled = (await db.execute(
        select(Trade).where(and_(
            Trade.signal_id == signal_id,
            Trade.order_type == "SELL",
            Trade.status.in_(["FILLED", "PARTIAL", "CLOSED"]),
        ))
    )).scalars().first()
    if sell_filled:
        return None

    failed_count = (await db.execute(
        select(func.count(Trade.id)).where(and_(
            Trade.signal_id == signal_id,
            Trade.order_type == "SELL",
            Trade.status == "FAILED",
        ))
    )).scalar() or 0

    if failed_count >= 3:
        logger.warning(f"[{signal_id[:8]}] 매도 FAILED {failed_count}회 — 건너뜀")
        return None

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

    return {
        "code":           buy_trades[0].code,
        "name":           buy_trades[0].name,
        "signal_id":      signal_id,
        "total_quantity": total_quantity,
        "avg_buy_price":  avg_buy_price,
        "total_amount":   sum(t.amount for t in buy_trades),
        "phases":         buy_trades,
    }


# ── 매도 실행 ──────────────────────────────────────────────────────────────────

async def _execute_sell(
    db: AsyncSession,
    position: dict,
    current_price: int,
    reason: str,
) -> dict:
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

    trade_id    = str(uuid.uuid4())
    profit_pct  = (current_price / avg_buy_price - 1) * 100 if avg_buy_price > 0 else 0
    sell_amount = current_price * total_quantity

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

    # 청산 성공 시 트레일링 상태 제거
    if result["success"]:
        _clear_trailing_state(signal_id)

    # 손절 시 블랙리스트 등록
    is_loss = profit_pct < 0
    if is_loss and result["success"]:
        from trader.risk_manager import add_to_blacklist
        await add_to_blacklist(
            db, code, name,
            reason=f"손절 청산 ({profit_pct:.2f}%) — {BLACKLIST_DAYS}일 재진입 금지",
        )

    emoji      = "✅" if profit_pct > 0 else "🔴"
    phase_info = ""
    if len(position["phases"]) > 1:
        phase_info = f"\n📦 분할매수: {len(position['phases'])}회 평균 {avg_buy_price:,.0f}원"

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


# ── 자동 청산 메인 루프 ────────────────────────────────────────────────────────

async def check_and_execute_auto_exit(db: AsyncSession) -> list[dict]:
    """
    체결된 BUY 포지션을 순회하며 자동 청산 조건을 확인합니다.

    청산 우선순위:
      1. 하드 손절  : 현재가 ≤ 평균매수가 × (1 + HARD_STOP_PCT)         [-2.5%]
      2. 일반 손절  : 현재가 ≤ 평균매수가 × (1 + STOP_LOSS_PCT)         [-1.5%]
      3. 트레일링   : 수익 ≥ +3% 진입 후 고점 대비 -1.5% 이탈           (활성 시)
      4. 고정 익절  : 현재가 ≥ 평균매수가 × (1 + TARGET_PROFIT_PCT)     [+5%]
                      (트레일링 미활성 또는 활성화 전 구간에서만 동작)
    """
    from trader.risk_manager import require_market_open
    if not require_market_open("auto_exit"):
        return []

    # 이미 청산된 signal_id 사전 수집
    sold_sids = set((await db.execute(
        select(Trade.signal_id).where(and_(
            Trade.order_type == "SELL",
            Trade.status.in_(["FILLED", "PARTIAL", "CLOSED"]),
        )).distinct()
    )).scalars().all())

    # SELL FAILED 3회 이상 → CLOSED 보정
    failed_rows = (await db.execute(
        select(Trade.signal_id, Trade.code, Trade.name,
               func.count(Trade.id).label("cnt"))
        .where(and_(Trade.order_type == "SELL", Trade.status == "FAILED"))
        .group_by(Trade.signal_id, Trade.code, Trade.name)
        .having(func.count(Trade.id) >= 3)
    )).all()

    for row in failed_rows:
        if row.signal_id in sold_sids:
            continue
        logger.error(f"[{row.code}] {row.name} 매도 FAILED {row.cnt}회 — CLOSED 보정")
        await db.execute(
            Trade.__table__.update()
            .where(and_(
                Trade.signal_id == row.signal_id,
                Trade.order_type == "SELL",
                Trade.status == "FAILED",
            ))
            .values(status="CLOSED")
        )
        await send_message(
            f"🚨 <b>[AI INVEST] 매도 반복 실패 — 수동 확인 필요</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{row.name} ({row.code})</b>\n"
            f"실패 횟수: {row.cnt}회\n"
            f"⚙️ DB CLOSED 처리 완료 — 이후 알람 없음\n"
            f"⚠️ KIS에서 실제 보유 여부 수동 확인 필요"
        )
        sold_sids.add(row.signal_id)

    await db.commit()

    # ── SELL FAILED 자동 재시도 ──────────────────────────────────────────────
    retry_rows = (await db.execute(
        select(Trade.signal_id, Trade.code, Trade.name,
               func.count(Trade.id).label("cnt"))
        .where(and_(Trade.order_type == "SELL", Trade.status == "FAILED"))
        .group_by(Trade.signal_id, Trade.code, Trade.name)
        .having(func.count(Trade.id) < 3)   # 3회 미만만 재시도
    )).all()

    for row in retry_rows:
        if row.signal_id in sold_sids:
            continue
        position = await _get_open_position(db, row.signal_id)
        if not position:
            continue
        try:
            price_data    = await kis.get_current_price(row.code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"[재시도] 현재가 조회 실패 [{row.code}]: {e}")
            continue
        logger.warning(
            f"[재시도] SELL FAILED {row.cnt}회 → 재주문: {row.code} {row.name} "
            f"@ {current_price:,}원"
        )
        result = await _execute_sell(
            db, position, current_price,
            f"🔄 매도 재시도 ({row.cnt+1}차) — 이전 FAILED {row.cnt}회"
        )
        if result.get("success"):
            sold_sids.add(row.signal_id)
            logger.info(f"[재시도] 재주문 성공: {row.code}")
        else:
            logger.warning(f"[재시도] 재주문 실패: {row.code}")


    # 미청산 BUY signal_id 조회
    signal_ids_q = (await db.execute(
        select(Trade.signal_id).where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        )).distinct()
    )).scalars().all()

    # [버그 수정 v2] pending_ids 사용 (이미 청산된 것 제외)
    pending_ids = [sid for sid in signal_ids_q if sid not in sold_sids]

    executed = []

    for signal_id in pending_ids:
        position = await _get_open_position(db, signal_id)
        if not position:
            continue

        code      = position["code"]
        name      = position["name"]
        avg_price = position["avg_buy_price"]

        try:
            price_data    = await kis.get_current_price(code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"현재가 조회 실패 [{code}]: {e}")
            continue

        hard_stop_price = avg_price * (1 + HARD_STOP_PCT)
        stop_loss_price = avg_price * (1 + STOP_LOSS_PCT)
        target_price    = avg_price * (1 + TARGET_PROFIT_PCT)
        profit_ratio    = current_price / avg_price - 1
        profit_pct_val  = profit_ratio * 100

        # ── 장 초반 손절 유예 (A) ────────────────────────────────────────────
        import pytz as _pytz
        _kst = _pytz.timezone("Asia/Seoul")
        _now_kst = datetime.now(_kst)
        _market_open = _now_kst.replace(hour=9, minute=5, second=0, microsecond=0)
        _elapsed_min = (_now_kst - _market_open).total_seconds() / 60
        _in_early_session = 0 <= _elapsed_min < STOP_LOSS_SKIP_MINUTES

        # ── 0순위: 상한가 자동 익절 (수동매도 전략으로 비활성화) ────────────
        if False and profit_ratio >= 0.29:
            logger.info(
                f"상한가 익절: {code} 현재가 {current_price:,} "
                f"({profit_pct_val:.2f}%) — 상한가 도달 자동 청산"
            )
            result = await _execute_sell(
                db, position, current_price,
                f"🚀 상한가 자동 익절 ({profit_pct_val:.2f}%) — "
                f"상한가 도달 전량 청산"
            )
            if result:
                executed.append(result)
            continue

        # ── 1순위: 하드 손절 (장 초반 유예 없음 — 하드는 항상 적용) ──────────
        if current_price <= hard_stop_price:
            logger.warning(
                f"하드 손절: {code} 현재가 {current_price:,} ≤ {hard_stop_price:,.0f} "
                f"({profit_pct_val:.2f}%)"
            )
            result = await _execute_sell(
                db, position, current_price,
                f"🚨 하드 손절 ({profit_pct_val:.2f}%) — "
                f"하드손절가 {hard_stop_price:,.0f}원 도달"
            )
            if result:
                executed.append(result)
            _stop_loss_counter.pop(signal_id, None)
            continue

        # ── 2순위: 일반 손절 (A: 장 초반 유예 / C: 연속 확인) ──────────────
        if current_price <= stop_loss_price:
            # A: 장 초반 30분 이내 → 연속 확인 횟수 2배 필요
            confirm_needed = STOP_LOSS_CONFIRM_COUNT * 2 if _in_early_session else STOP_LOSS_CONFIRM_COUNT
            _stop_loss_counter[signal_id] = _stop_loss_counter.get(signal_id, 0) + 1
            cnt = _stop_loss_counter[signal_id]

            if cnt < confirm_needed:
                logger.info(
                    f"손절 대기 [{code}] {cnt}/{confirm_needed}회 확인 중 "
                    f"현재가 {current_price:,} ({profit_pct_val:.2f}%) "
                    f"{'[장 초반 유예 중]' if _in_early_session else ''}"
                )
                continue

            logger.warning(
                f"손절: {code} 현재가 {current_price:,} ≤ {stop_loss_price:,.0f} "
                f"({profit_pct_val:.2f}%) [{cnt}회 확인 완료]"
            )
            result = await _execute_sell(
                db, position, current_price,
                f"손절 ({profit_pct_val:.2f}%) — {cnt}회 확인 후 청산"
            )
            if result:
                executed.append(result)
            _stop_loss_counter.pop(signal_id, None)
            continue
        else:
            # 손절선 이탈 아님 → 카운터 리셋
            _stop_loss_counter.pop(signal_id, None)

        # ── 3·4순위: 트레일링 스탑 / 고정 익절 ──────────────────────────────
        if TRAILING_STOP_ENABLED:
            if profit_ratio >= TRAILING_STOP_ACTIVATION_PCT:
                # 트레일링 모드: 고점 갱신 후 청산 조건 확인
                high_price  = _update_trailing_high(signal_id, current_price)
                trail_price = high_price * (1 - TRAILING_STOP_PCT)

                logger.debug(
                    f"[트레일링] {code} — 현재가 {current_price:,} / "
                    f"고점 {high_price:,.0f} / 청산선 {trail_price:,.0f}"
                )

                if current_price <= trail_price:
                    gain_from_high = (current_price / high_price - 1) * 100
                    logger.info(
                        f"트레일링 스탑: {code} 현재가 {current_price:,} ≤ "
                        f"청산선 {trail_price:,.0f} "
                        f"(고점 {high_price:,.0f} 대비 {gain_from_high:.2f}%)"
                    )
                    result = await _execute_sell(
                        db, position, current_price,
                        f"🎯 트레일링 스탑 ({profit_pct_val:.2f}%) — "
                        f"고점 {high_price:,.0f}원 대비 -{TRAILING_STOP_PCT*100:.1f}% 이탈"
                    )
                    if result:
                        executed.append(result)
                # 트레일링 모드: 고정 익절 적용 안 함 (계속 상승 가능성 유지)

            else:
                # 트레일링 미활성 구간: 고정 익절 체크
                if current_price >= target_price:
                    logger.info(
                        f"익절: {code} 현재가 {current_price:,} ≥ "
                        f"목표가 {target_price:,.0f} ({profit_pct_val:.2f}%)"
                    )
                    result = await _execute_sell(
                        db, position, current_price,
                        f"목표수익 달성 ({profit_pct_val:.2f}%) — "
                        f"목표가 {target_price:,.0f}원 도달"
                    )
                    if result:
                        executed.append(result)

        else:
            # 트레일링 비활성: 고정 익절만
            if current_price >= target_price:
                logger.info(
                    f"익절: {code} 현재가 {current_price:,} ≥ "
                    f"목표가 {target_price:,.0f} ({profit_pct_val:.2f}%)"
                )
                result = await _execute_sell(
                    db, position, current_price,
                    f"목표수익 달성 ({profit_pct_val:.2f}%) — "
                    f"목표가 {target_price:,.0f}원 도달"
                )
                if result:
                    executed.append(result)

    if executed:
        logger.info(f"자동 청산 완료: {len(executed)}건")

    return executed


# 기존 함수명 호환성 유지
check_and_execute_stop_loss = check_and_execute_auto_exit
