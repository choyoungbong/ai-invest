"""
Strategy – 강화된 돌파매매 전략 엔진

기존 조건 (승률 28.9% → 개선 목표 45%+):
  1. 당일 고가 > N일 최고가 돌파
  2. 거래대금 > 평균 2배
  3. 등락률 >= +2%

추가 필터 (승률 강화):
  4. RSI 50~75 (상승 모멘텀, 과매수 제외)
  5. MACD 히스토그램 양수 (추세 확인)
  6. 5일 MA > 20일 MA (골든크로스)
  7. 볼린저밴드 중심선 위 (추세 강도)
  8. 최소 신뢰도 0.55 이상만 실행
"""
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_

from api.models import MarketData, Signal, Stock

logger = logging.getLogger(__name__)

# ── 전략 파라미터 (환경변수로 관리) ───────────────────────────────────────────
BREAKOUT_DAYS      = int(os.getenv("BREAKOUT_DAYS",      "20"))
VOLUME_MULTIPLIER  = float(os.getenv("VOLUME_MULTIPLIER", "2.0"))
MIN_CHANGE_RATE    = float(os.getenv("MIN_CHANGE_RATE",   "2.0"))
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT",    "-0.02"))
TARGET_PROFIT_PCT  = float(os.getenv("TARGET_PROFIT_PCT", "0.05"))
MIN_CONFIDENCE     = float(os.getenv("MIN_CONFIDENCE",    "0.55"))  # 최소 신뢰도

# 기술 필터 활성화 여부 (기본값 모두 활성)
FILTER_RSI_ENABLED  = os.getenv("FILTER_RSI_ENABLED",  "true").lower() == "true"
FILTER_MACD_ENABLED = os.getenv("FILTER_MACD_ENABLED", "true").lower() == "true"
FILTER_MA_ENABLED   = os.getenv("FILTER_MA_ENABLED",   "true").lower() == "true"
FILTER_BB_ENABLED   = os.getenv("FILTER_BB_ENABLED",   "true").lower() == "true"

# RSI 범위
RSI_MIN = float(os.getenv("RSI_MIN", "45"))   # 45 미만이면 모멘텀 부족
RSI_MAX = float(os.getenv("RSI_MAX", "78"))   # 78 초과면 과매수


# ── 기술 지표 계산 ─────────────────────────────────────────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    diffs  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(diffs)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)


def _calc_ema(prices: list[float], period: int) -> list[Optional[float]]:
    if len(prices) < period:
        return [None] * len(prices)
    result = [None] * (period - 1)
    sma    = sum(prices[:period]) / period
    result.append(sma)
    k = 2 / (period + 1)
    for p in prices[period:]:
        result.append(result[-1] * (1 - k) + p * k)
    return result


def _calc_macd(closes: list[float]) -> dict:
    """MACD(12,26,9) 계산. 히스토그램이 양수면 상승 추세."""
    if len(closes) < 35:
        return {"macd": None, "signal": None, "histogram": None}
    ema12 = _calc_ema(closes, 12)
    ema26 = _calc_ema(closes, 26)
    macd  = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema12, ema26)
    ]
    valid = [v for v in macd if v is not None]
    if len(valid) < 9:
        return {"macd": None, "signal": None, "histogram": None}
    sig_line = _calc_ema(valid, 9)
    hist     = valid[-1] - sig_line[-1] if sig_line[-1] is not None else None
    return {
        "macd":      round(valid[-1], 4),
        "signal":    round(sig_line[-1], 4) if sig_line[-1] else None,
        "histogram": round(hist, 4) if hist is not None else None,
    }


def _calc_ma(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def _calc_bollinger(closes: list[float], period: int = 20) -> dict:
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None}
    window = closes[-period:]
    mid    = sum(window) / period
    std    = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return {
        "upper":  round(mid + 2 * std, 2),
        "middle": round(mid, 2),
        "lower":  round(mid - 2 * std, 2),
    }


# ── 데이터 조회 ────────────────────────────────────────────────────────────────

async def _fetch_recent_data(
    db: AsyncSession,
    code: str,
    days: int = 60,
) -> list:
    cutoff = datetime.utcnow() - timedelta(days=days + 5)
    stmt   = (
        select(MarketData)
        .where(and_(
            MarketData.code == code,
            MarketData.timestamp >= cutoff,
        ))
        .order_by(desc(MarketData.timestamp))
        .limit(days + 1)
    )
    return (await db.execute(stmt)).scalars().all()


# ── 강화된 돌파 체크 ───────────────────────────────────────────────────────────

async def check_breakout(
    db: AsyncSession,
    code: str,
    name: str,
) -> Optional[dict]:
    """
    기존 3가지 돌파 조건 + 기술지표 4가지 필터 적용.
    모든 조건 통과 시 신호 dict 반환. 미통과 시 None.
    """
    rows = await _fetch_recent_data(db, code, days=60)

    if len(rows) < max(BREAKOUT_DAYS + 1, 30):
        return None

    today   = rows[0]
    history = rows[1:]

    if not today.high or not today.close or today.close <= 0:
        return None

    # ── 기존 돌파 조건 3가지 ─────────────────────────────────────────────────
    past_highs = [r.high for r in history[:BREAKOUT_DAYS] if r.high]
    if not past_highs:
        return None
    n_day_high = max(past_highs)

    past_values = [r.trading_value for r in history[:BREAKOUT_DAYS] if r.trading_value]
    avg_value   = sum(past_values) / len(past_values) if past_values else 0

    cond_high   = today.high > n_day_high
    cond_volume = avg_value > 0 and today.trading_value >= avg_value * VOLUME_MULTIPLIER
    cond_change = today.change_rate >= MIN_CHANGE_RATE

    if not (cond_high and cond_volume and cond_change):
        return None

    # ── 기술지표 계산 ─────────────────────────────────────────────────────────
    all_rows = [today] + list(history)
    closes   = [r.close for r in reversed(all_rows) if r.close]

    rsi       = _calc_rsi(closes)
    macd_data = _calc_macd(closes)
    ma5       = _calc_ma(closes, 5)
    ma20      = _calc_ma(closes, 20)
    bb        = _calc_bollinger(closes)

    failed_filters = []

    # ── 필터 4: RSI 범위 확인 ────────────────────────────────────────────────
    if FILTER_RSI_ENABLED and rsi is not None:
        if rsi < RSI_MIN:
            failed_filters.append(f"RSI {rsi:.1f} < {RSI_MIN} (모멘텀 부족)")
        elif rsi > RSI_MAX:
            failed_filters.append(f"RSI {rsi:.1f} > {RSI_MAX} (과매수)")

    # ── 필터 5: MACD 히스토그램 양수 ─────────────────────────────────────────
    if FILTER_MACD_ENABLED and macd_data["histogram"] is not None:
        if macd_data["histogram"] <= 0:
            failed_filters.append(f"MACD 히스토그램 {macd_data['histogram']:.4f} ≤ 0 (하락 추세)")

    # ── 필터 6: 5일 MA > 20일 MA (골든크로스) ────────────────────────────────
    if FILTER_MA_ENABLED and ma5 is not None and ma20 is not None:
        if ma5 <= ma20:
            failed_filters.append(f"MA5({ma5:,.0f}) ≤ MA20({ma20:,.0f}) (골든크로스 미충족)")

    # ── 필터 7: 볼린저밴드 중심선 위 ─────────────────────────────────────────
    if FILTER_BB_ENABLED and bb["middle"] is not None:
        if today.close < bb["middle"]:
            failed_filters.append(f"현재가({today.close:,.0f}) < BB중심({bb['middle']:,.0f})")

    if failed_filters:
        logger.debug(
            f"[{code}] {name} 필터 탈락: {' / '.join(failed_filters)}"
        )
        return None

    # ── 신뢰도 계산 ───────────────────────────────────────────────────────────
    confidence = _calc_confidence(
        today, n_day_high, avg_value, rsi, macd_data, ma5, ma20
    )

    if confidence < MIN_CONFIDENCE:
        logger.debug(
            f"[{code}] {name} 신뢰도 미달: {confidence:.2f} < {MIN_CONFIDENCE}"
        )
        return None

    # ── 신호 생성 ──────────────────────────────────────────────────────────────
    price      = today.close
    stop_loss  = round(price * (1 + STOP_LOSS_PCT), 0)
    target     = round(price * (1 + TARGET_PROFIT_PCT), 0)

    reason_parts = [
        f"{BREAKOUT_DAYS}일 신고가 돌파 ({n_day_high:,.0f}→{today.high:,.0f})",
        f"거래대금 {today.trading_value / avg_value:.1f}배",
        f"등락률 {today.change_rate:.1f}%",
    ]
    if rsi is not None:
        reason_parts.append(f"RSI {rsi:.1f}")
    if macd_data["histogram"] is not None:
        reason_parts.append(f"MACD히스트 {macd_data['histogram']:+.4f}")

    return {
        "code":         code,
        "name":         name,
        "signal_type":  "BUY",
        "strategy":     "breakout",
        "price":        price,
        "target_price": target,
        "stop_loss":    stop_loss,
        "reason":       " | ".join(reason_parts),
        "confidence":   confidence,
        # 지표 스냅샷 저장
        "rsi":          rsi,
        "macd":         macd_data["macd"],
        "macd_signal":  macd_data["signal"],
        "bb_upper":     bb["upper"],
        "bb_lower":     bb["lower"],
    }


def _calc_confidence(
    today,
    n_day_high: float,
    avg_value: float,
    rsi: Optional[float],
    macd_data: dict,
    ma5: Optional[float],
    ma20: Optional[float],
) -> float:
    """
    강화된 신뢰도 계산 (0~1).
    기존: 고가돌파폭(0.4) + 거래대금(0.4) + 등락률(0.2)
    강화: 각 항목 비중 조정 + 기술지표 보너스
    """
    score = 0.0

    # 고가 돌파 폭 (최대 0.25)
    if n_day_high > 0:
        pct = (today.high - n_day_high) / n_day_high
        score += min(pct * 8, 0.25)

    # 거래대금 배수 (최대 0.25)
    if avg_value > 0:
        mult = today.trading_value / avg_value
        score += min((mult - 1) * 0.08, 0.25)

    # 등락률 (최대 0.15)
    score += min(today.change_rate / 25, 0.15)

    # RSI 보너스: 55~70 구간이 최적 (최대 0.15)
    if rsi is not None:
        if 55 <= rsi <= 70:
            score += 0.15
        elif 50 <= rsi < 55 or 70 < rsi <= 75:
            score += 0.08

    # MACD 히스토그램 보너스 (최대 0.10)
    if macd_data["histogram"] is not None and macd_data["histogram"] > 0:
        score += min(macd_data["histogram"] * 0.5, 0.10)

    # MA 골든크로스 보너스 (최대 0.10)
    if ma5 is not None and ma20 is not None and ma5 > ma20:
        gap_pct = (ma5 - ma20) / ma20
        score += min(gap_pct * 5, 0.10)

    return round(min(score, 1.0), 3)


# ── 전략 엔진 메인 ─────────────────────────────────────────────────────────────

async def run_strategy(
    db: AsyncSession,
    candidates: List[dict],
) -> List[dict]:
    """
    스캐너 후보 종목에 강화된 돌파 전략을 적용합니다.
    """
    signals = []

    for item in candidates:
        code = item["code"]
        name = item.get("name", code)

        try:
            sig = await check_breakout(db, code, name)
        except Exception as e:
            logger.error(f"전략 오류 [{code}]: {e}")
            continue

        if sig is None:
            continue

        # DB 저장
        signal_id = str(uuid.uuid4())
        insert_values = {
            "id":          signal_id,
            "code":        sig["code"],
            "name":        sig["name"],
            "signal_type": sig["signal_type"],
            "strategy":    sig["strategy"],
            "price":       sig["price"],
            "target_price":sig["target_price"],
            "stop_loss":   sig["stop_loss"],
            "reason":      sig["reason"],
            "confidence":  sig["confidence"],
            "rsi":         sig.get("rsi"),
            "macd":        sig.get("macd"),
            "macd_signal": sig.get("macd_signal"),
            "bb_upper":    sig.get("bb_upper"),
            "bb_lower":    sig.get("bb_lower"),
        }

        await db.execute(Signal.__table__.insert().values(**insert_values))
        sig["id"]         = signal_id
        sig["created_at"] = datetime.utcnow().isoformat()
        signals.append(sig)

        logger.info(
            f"신호 발생 [{code} {name}] BUY @ {sig['price']:,.0f} "
            f"신뢰도:{sig['confidence']:.2f} RSI:{sig.get('rsi', 'N/A')}"
        )

    await db.commit()
    logger.info(
        f"전략 실행 완료: {len(candidates)}개 후보 → {len(signals)}개 신호 "
        f"(필터 적용)"
    )
    return signals


# ── 신호 목록 조회 ─────────────────────────────────────────────────────────────

async def get_signals(
    db: AsyncSession,
    limit: int = 50,
    signal_type: Optional[str] = None,
) -> List[dict]:
    stmt = (
        select(Signal)
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    if signal_type:
        stmt = stmt.where(Signal.signal_type == signal_type.upper())

    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id":           r.id,
            "code":         r.code,
            "name":         r.name,
            "signal_type":  r.signal_type,
            "strategy":     r.strategy,
            "price":        r.price,
            "target_price": r.target_price,
            "stop_loss":    r.stop_loss,
            "reason":       r.reason,
            "confidence":   r.confidence,
            "rsi":          r.rsi,
            "macd":         r.macd,
            "is_executed":  r.is_executed,
            "created_at":   r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
