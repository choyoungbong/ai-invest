"""
Strategy – 강화된 돌파매매 전략 엔진

기존 조건:
  1. 당일 고가 > N일 최고가 돌파
  2. 거래대금 > 평균 2배
  3. 등락률 >= +MIN_CHANGE_RATE%

기술 지표 필터:
  4. RSI 범위 (RSI_MIN ~ RSI_MAX)
  5. MACD 히스토그램 양수
  6. 5일 MA > 20일 MA (골든크로스, 선택)
  7. 볼린저밴드 중심선 위
  8. 최소 신뢰도 MIN_CONFIDENCE 이상

[A단계 개선]
  RSI 정확도 향상 (v4):
    기존: period+1 (15개) 데이터로 계산 → SMA 시드 영향 과도, HTS와 미세 차이
    수정: 최소 period×3 (42개) 데이터 요구 → Wilder's 스무딩이 충분히 수렴
          SMA 시드의 영향이 희석되어 실제 HTS RSI 값에 근접

  ATR 기반 변동성 필터 (v4 신규):
    - 14일 ATR(Average True Range)을 현재가 대비 %로 계산
    - ATR% > ATR_FILTER_MAX_PCT(기본 5%) 종목 진입 차단
      → 손절폭(-1.5%)이 너무 좁아 노이즈에 걸리는 고변동성 종목 사전 배제
    - ATR 값을 신호에 포함 → auto_trader에서 투자금 자동 조정에 활용
    - Signal.atr 컬럼에 저장 (마이그레이션 불필요, 기존 컬럼 활용)
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

# ── 전략 파라미터 ──────────────────────────────────────────────────────────────
BREAKOUT_DAYS     = int(os.getenv("BREAKOUT_DAYS",      "20"))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "2.0"))
MIN_CHANGE_RATE   = float(os.getenv("MIN_CHANGE_RATE",   "2.0"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",     "-0.02"))
TARGET_PROFIT_PCT = float(os.getenv("TARGET_PROFIT_PCT", "0.05"))
MIN_CONFIDENCE    = float(os.getenv("MIN_CONFIDENCE",    "0.55"))

# ── 기술 필터 활성화 여부 ──────────────────────────────────────────────────────
FILTER_RSI_ENABLED  = os.getenv("FILTER_RSI_ENABLED",  "true").lower() == "true"
FILTER_MACD_ENABLED = os.getenv("FILTER_MACD_ENABLED", "true").lower() == "true"
FILTER_MA_ENABLED   = os.getenv("FILTER_MA_ENABLED",   "true").lower() == "true"
FILTER_BB_ENABLED   = os.getenv("FILTER_BB_ENABLED",   "true").lower() == "true"

RSI_MIN = float(os.getenv("RSI_MIN", "45"))
RSI_MAX = float(os.getenv("RSI_MAX", "78"))

# ── ATR 변동성 필터 파라미터 (v4 신규) ────────────────────────────────────────
ATR_FILTER_ENABLED  = os.getenv("ATR_FILTER_ENABLED",  "true").lower() == "true"
ATR_PERIOD          = int(os.getenv("ATR_PERIOD",        "14"))
ATR_FILTER_MAX_PCT  = float(os.getenv("ATR_FILTER_MAX_PCT", "5.0"))  # ATR% > 5% → 진입 차단


# ── 기술 지표 계산 ─────────────────────────────────────────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """
    True Wilder's Smoothed RSI.

    [v4 개선] 최소 데이터 요구량 period+1 → period×3 으로 상향
      기존: 15개 데이터로 계산 → SMA 시드(첫 period개 평균)의 영향이 최종값에 크게 남음
            → 데이터가 짧을수록 HTS RSI와 오차 발생 (실제로 1~3pt 차이)
      수정: 최소 42개(period=14 기준) 요구 → Wilder's 스무딩이 28번 반복 적용
            → SMA 시드 영향이 충분히 희석, HTS 값에 근접
      효과: 신호 발생 타이밍 정확도 향상, 과매수 구간 조기 탈락 감소
    """
    min_required = period * 3  # 충분한 워밍업 보장 (기본 42개)
    if len(closes) < min_required:
        return None

    diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]

    # SMA 시드 (표준 방식 유지, 충분한 데이터로 희석)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    # Wilder's 스무딩 전 구간 적용
    for i in range(period, len(diffs)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)


def _calc_atr(rows_asc: list, period: int = 14) -> Optional[float]:
    """
    ATR (Average True Range) — Wilder's Smoothing (v4 신규).

    True Range = max(고가-저가, |고가-전일종가|, |저가-전일종가|)
    ATR = Wilder's 이동평균(TR, period)

    Args:
        rows_asc: MarketData 객체 리스트, 시간 오름차순 (오래된 것 먼저)
        period:   ATR 계산 기간 (기본 14일)

    Returns:
        ATR 값 (원), 데이터 부족 시 None
    """
    if len(rows_asc) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(rows_asc)):
        h = rows_asc[i].high or 0
        l = rows_asc[i].low or 0
        pc = rows_asc[i - 1].close or 0
        if not (h and l and pc):
            continue
        tr = max(h - l, abs(h - pc), abs(l - pc))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    # SMA 시드
    atr = sum(true_ranges[:period]) / period

    # Wilder's 스무딩
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return round(atr, 2)


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
    """MACD(12,26,9). 히스토그램 양수 = 상승 추세."""
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
    """
    최근 N일 시세 데이터 조회.
    반환: 내림차순 (최신 → 오래된 순)
    RSI 정확도를 위해 days를 넉넉하게 요청 (기본 60일 → _calc_rsi의 period×3=42 충족)
    """
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
    돌파 조건 3가지 + 기술지표 4가지 + ATR 변동성 필터 적용.
    모든 조건 통과 시 신호 dict 반환. 미통과 시 None.
    """
    rows = await _fetch_recent_data(db, code, days=60)

    if len(rows) < max(BREAKOUT_DAYS + 1, 30):
        return None

    today   = rows[0]
    history = rows[1:]

    if not today.high or not today.close or today.close <= 0:
        return None

    # ── 기본 돌파 조건 3가지 ──────────────────────────────────────────────────
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
    # rows는 내림차순 → 역순 정렬로 시간 오름차순 변환
    rows_asc = list(reversed([today] + list(history)))
    closes   = [r.close for r in rows_asc if r.close]

    rsi       = _calc_rsi(closes)
    macd_data = _calc_macd(closes)
    ma5       = _calc_ma(closes, 5)
    ma20      = _calc_ma(closes, 20)
    bb        = _calc_bollinger(closes)

    # ── ATR 계산 (v4 신규) ────────────────────────────────────────────────────
    atr_val = _calc_atr(rows_asc, period=ATR_PERIOD)
    atr_pct = round(atr_val / today.close * 100, 2) if (atr_val and today.close) else None

    failed_filters = []

    # ── 필터 4: RSI 범위 ──────────────────────────────────────────────────────
    if FILTER_RSI_ENABLED and rsi is not None:
        if rsi < RSI_MIN:
            failed_filters.append(f"RSI {rsi:.1f} < {RSI_MIN} (모멘텀 부족)")
        elif rsi > RSI_MAX:
            failed_filters.append(f"RSI {rsi:.1f} > {RSI_MAX} (과매수)")

    # ── 필터 5: MACD 히스토그램 양수 ─────────────────────────────────────────
    if FILTER_MACD_ENABLED and macd_data["histogram"] is not None:
        if macd_data["histogram"] <= 0:
            failed_filters.append(f"MACD 히스트 {macd_data['histogram']:.4f} ≤ 0 (하락 추세)")

    # ── 필터 6: MA 골든크로스 ─────────────────────────────────────────────────
    if FILTER_MA_ENABLED and ma5 is not None and ma20 is not None:
        if ma5 <= ma20:
            failed_filters.append(f"MA5({ma5:,.0f}) ≤ MA20({ma20:,.0f})")

    # ── 필터 7: 볼린저밴드 중심선 위 ─────────────────────────────────────────
    if FILTER_BB_ENABLED and bb["middle"] is not None:
        if today.close < bb["middle"]:
            failed_filters.append(f"현재가({today.close:,.0f}) < BB중심({bb['middle']:,.0f})")

    # ── 필터 8: ATR 변동성 필터 (v4 신규) ────────────────────────────────────
    # ATR% > 임계값이면 손절폭(-1.5%)이 너무 좁아 노이즈에 걸릴 위험이 높음
    if ATR_FILTER_ENABLED and atr_pct is not None:
        if atr_pct > ATR_FILTER_MAX_PCT:
            failed_filters.append(
                f"ATR {atr_pct:.2f}% > {ATR_FILTER_MAX_PCT}% (고변동성 — 손절 노이즈 위험)"
            )

    if failed_filters:
        logger.debug(f"[{code}] {name} 필터 탈락: {' / '.join(failed_filters)}")
        return None

    # ── 신뢰도 계산 ───────────────────────────────────────────────────────────
    confidence = _calc_confidence(
        today, n_day_high, avg_value, rsi, macd_data, ma5, ma20
    )

    if confidence < MIN_CONFIDENCE:
        logger.debug(f"[{code}] {name} 신뢰도 미달: {confidence:.2f} < {MIN_CONFIDENCE}")
        return None

    # ── 신호 생성 ─────────────────────────────────────────────────────────────
    price     = today.close
    stop_loss = round(price * (1 + STOP_LOSS_PCT), 0)
    target    = round(price * (1 + TARGET_PROFIT_PCT), 0)

    reason_parts = [
        f"{BREAKOUT_DAYS}일 신고가 돌파 ({n_day_high:,.0f}→{today.high:,.0f})",
        f"거래대금 {today.trading_value / avg_value:.1f}배",
        f"등락률 {today.change_rate:.1f}%",
    ]
    if rsi is not None:
        reason_parts.append(f"RSI {rsi:.1f}")
    if macd_data["histogram"] is not None:
        reason_parts.append(f"MACD히스트 {macd_data['histogram']:+.4f}")
    if atr_pct is not None:
        reason_parts.append(f"ATR {atr_pct:.2f}%")

    logger.info(
        f"신호 발생 [{code} {name}] BUY @ {price:,.0f} "
        f"신뢰도:{confidence:.2f} RSI:{rsi} ATR:{atr_pct}%"
    )

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
        # 지표 스냅샷
        "rsi":          rsi,
        "macd":         macd_data["macd"],
        "macd_signal":  macd_data["signal"],
        "bb_upper":     bb["upper"],
        "bb_lower":     bb["lower"],
        # ATR (v4 신규) — auto_trader에서 투자금 조정에 활용
        "atr":          atr_val,
        "atr_pct":      atr_pct,
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
    """신뢰도 계산 (0~1)."""
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

    # RSI 보너스: 55~70 구간 최적 (최대 0.15)
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
    """스캐너 후보 종목에 강화된 돌파 전략을 적용합니다."""
    signals = []

    # 오늘 이미 breakout 신호 발생한 종목 사전 조회 (중복 방지)
    from trader.risk_manager import _kst_today_start_utc
    today_start_utc = _kst_today_start_utc()
    already_today = set(
        (await db.execute(
            select(Signal.code).where(and_(
                Signal.strategy == "breakout",
                Signal.created_at >= today_start_utc,
            ))
        )).scalars().all()
    )

    for item in candidates:
        code = item["code"]
        name = item.get("name", code)

        if code in already_today:
            logger.debug(f"[{code}] 오늘 breakout 신호 중복 — 건너뜀")
            continue

        try:
            sig = await check_breakout(db, code, name)
        except Exception as e:
            logger.error(f"전략 오류 [{code}]: {e}")
            continue

        if sig is None:
            continue

        # DB 저장
        signal_id = str(uuid.uuid4())
        await db.execute(
            Signal.__table__.insert().values(
                id=signal_id,
                code=sig["code"],
                name=sig["name"],
                signal_type=sig["signal_type"],
                strategy=sig["strategy"],
                price=sig["price"],
                target_price=sig["target_price"],
                stop_loss=sig["stop_loss"],
                reason=sig["reason"],
                confidence=sig["confidence"],
                rsi=sig.get("rsi"),
                macd=sig.get("macd"),
                macd_signal=sig.get("macd_signal"),
                bb_upper=sig.get("bb_upper"),
                bb_lower=sig.get("bb_lower"),
                atr=sig.get("atr"),           # v4 신규
            )
        )
        sig["id"]         = signal_id
        sig["created_at"] = datetime.utcnow().isoformat()
        signals.append(sig)
        already_today.add(code)

    await db.commit()
    logger.info(
        f"전략 실행 완료: {len(candidates)}개 후보 → {len(signals)}개 신호"
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
            "atr":          r.atr,
            "is_executed":  r.is_executed,
            "created_at":   r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
