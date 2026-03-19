"""
Strategy Extensions – 추가 전략 모듈

돌파매매 외 3가지 전략을 추가합니다.

  1. ma_cross     : 5일/20일 골든크로스
  2. rsi_reversal : RSI 30 이하 과매도 반등
  3. macd         : MACD 시그널 상향 돌파
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_

from api.models import MarketData, Signal

logger = logging.getLogger(__name__)

STOP_LOSS_PCT  = 0.02
TARGET_PCT     = 0.04


# ── 공통 데이터 로드 ───────────────────────────────────────────────────────────

async def _fetch(db: AsyncSession, code: str, days: int) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(days=days + 5)
    stmt = (
        select(MarketData)
        .where(and_(MarketData.code == code, MarketData.timestamp >= cutoff))
        .order_by(desc(MarketData.timestamp))
        .limit(days)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "date":  r.timestamp,
            "open":  r.open or 0,
            "high":  r.high or 0,
            "low":   r.low or 0,
            "close": r.close or 0,
            "volume": r.volume or 0,
            "trading_value": r.trading_value or 0,
            "change_rate": r.change_rate or 0,
        }
        for r in reversed(rows)   # 오래된 것부터
    ]


# ── 지표 계산 유틸 ─────────────────────────────────────────────────────────────

def _ema(prices: list[float], period: int) -> list[float | None]:
    result = [None] * (period - 1)
    sma = sum(prices[:period]) / period
    result.append(sma)
    k = 2 / (period + 1)
    for p in prices[period:]:
        result.append(result[-1] * (1 - k) + p * k)
    return result


def _rsi(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) < period + 1:
        return [None] * len(closes)
    result = [None] * period
    diffs  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    rs     = avg_g / avg_l if avg_l else 0
    result.append(100 - 100 / (1 + rs))
    for i in range(period, len(diffs)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs    = avg_g / avg_l if avg_l else 0
        result.append(100 - 100 / (1 + rs))
    return result


def _macd(closes: list[float], fast=12, slow=26, signal=9):
    ema_fast   = _ema(closes, fast)
    ema_slow   = _ema(closes, slow)
    macd_line  = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid      = [v for v in macd_line if v is not None]
    sig_raw    = _ema(valid, signal)
    # signal line을 macd_line 길이에 맞춰 패딩
    none_count = len(macd_line) - len(valid)
    sig_line   = [None] * (none_count + signal - 1) + sig_raw[signal - 1:]
    histogram  = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, sig_line)
    ]
    return macd_line, sig_line, histogram


# ── 전략 1: 이동평균 골든크로스 ───────────────────────────────────────────────

async def check_ma_cross(
    db: AsyncSession,
    code: str,
    name: str,
    short: int = 5,
    long_: int = 20,
) -> dict | None:
    data = await _fetch(db, code, days=long_ + 5)
    if len(data) < long_ + 2:
        return None

    closes = [d["close"] for d in data]

    def sma(period):
        return [
            sum(closes[i - period + 1: i + 1]) / period if i >= period - 1 else None
            for i in range(len(closes))
        ]

    sma_s = sma(short)
    sma_l = sma(long_)
    today, yesterday = -1, -2

    if any(v is None for v in [sma_s[today], sma_s[yesterday], sma_l[today], sma_l[yesterday]]):
        return None

    # 골든크로스 조건
    golden_cross = sma_s[yesterday] <= sma_l[yesterday] and sma_s[today] > sma_l[today]
    if not golden_cross:
        return None

    price     = data[-1]["close"]
    gap_pct   = (sma_s[today] - sma_l[today]) / sma_l[today] * 100

    return {
        "code":         code,
        "name":         name,
        "signal_type":  "BUY",
        "strategy":     "ma_cross",
        "price":        price,
        "target_price": round(price * (1 + TARGET_PCT)),
        "stop_loss":    round(price * (1 - STOP_LOSS_PCT)),
        "reason":       (
            f"{short}일 MA({sma_s[today]:,.0f})가 {long_}일 MA({sma_l[today]:,.0f})를 "
            f"상향 돌파 (골든크로스, 갭 {gap_pct:.2f}%)"
        ),
        "confidence":   round(min(gap_pct / 3, 1.0), 3),
    }


# ── 전략 2: RSI 과매도 반등 ────────────────────────────────────────────────────

async def check_rsi_reversal(
    db: AsyncSession,
    code: str,
    name: str,
    period: int = 14,
    oversold: float = 30,
) -> dict | None:
    data = await _fetch(db, code, days=period + 10)
    if len(data) < period + 2:
        return None

    closes   = [d["close"] for d in data]
    rsi_vals = _rsi(closes, period)

    if rsi_vals[-1] is None or rsi_vals[-2] is None:
        return None

    # 과매도 → 반등 돌파 조건
    if not (rsi_vals[-2] < oversold <= rsi_vals[-1]):
        return None

    price = data[-1]["close"]
    rsi_now = rsi_vals[-1]

    return {
        "code":         code,
        "name":         name,
        "signal_type":  "BUY",
        "strategy":     "rsi_reversal",
        "price":        price,
        "target_price": round(price * (1 + TARGET_PCT)),
        "stop_loss":    round(price * (1 - STOP_LOSS_PCT)),
        "reason":       (
            f"RSI({period}) {rsi_vals[-2]:.1f} → {rsi_now:.1f}로 "
            f"과매도({oversold}) 구간 상향 돌파 — 반등 신호"
        ),
        "confidence":   round(min((oversold - rsi_vals[-2]) / oversold, 1.0), 3),
    }


# ── 전략 3: MACD 시그널 돌파 ──────────────────────────────────────────────────

async def check_macd(
    db: AsyncSession,
    code: str,
    name: str,
) -> dict | None:
    data = await _fetch(db, code, days=60)
    if len(data) < 40:
        return None

    closes = [d["close"] for d in data]
    macd_line, sig_line, histogram = _macd(closes)

    if any(v is None for v in [macd_line[-1], macd_line[-2], sig_line[-1], sig_line[-2]]):
        return None

    # MACD가 시그널선을 하향→상향 돌파
    cross_up = macd_line[-2] <= sig_line[-2] and macd_line[-1] > sig_line[-1]
    if not cross_up:
        return None

    # MACD가 0선 아래에서 반등이면 더 강한 신호
    below_zero = macd_line[-1] < 0
    confidence = 0.7 if below_zero else 0.5

    price = data[-1]["close"]

    return {
        "code":         code,
        "name":         name,
        "signal_type":  "BUY",
        "strategy":     "macd",
        "price":        price,
        "target_price": round(price * (1 + TARGET_PCT)),
        "stop_loss":    round(price * (1 - STOP_LOSS_PCT)),
        "reason":       (
            f"MACD({macd_line[-1]:.2f})가 Signal({sig_line[-1]:.2f})을 상향 돌파 "
            f"({'0선 아래 — 강한 신호' if below_zero else '0선 위'})"
        ),
        "confidence":   confidence,
    }


# ── 통합 전략 실행 ─────────────────────────────────────────────────────────────

STRATEGY_FUNCS = {
    "ma_cross":     check_ma_cross,
    "rsi_reversal": check_rsi_reversal,
    "macd":         check_macd,
}


async def run_extended_strategy(
    db: AsyncSession,
    candidates: list[dict],
    strategies: list[str] | None = None,
) -> list[dict]:
    """
    스캐너 후보 종목에 확장 전략을 적용합니다.
    strategies 미입력 시 전략 전체 실행.
    """
    target_strategies = strategies or list(STRATEGY_FUNCS.keys())
    signals = []

    for item in candidates:
        code = item["code"]
        name = item.get("name", code)

        for strat_name in target_strategies:
            fn = STRATEGY_FUNCS.get(strat_name)
            if not fn:
                continue
            try:
                sig = await fn(db, code, name)
            except Exception as e:
                logger.error(f"전략 오류 [{strat_name}][{code}]: {e}")
                continue

            if sig is None:
                continue

            signal_id = str(uuid.uuid4())
            await db.execute(
                Signal.__table__.insert().values(id=signal_id, **sig)
            )
            sig["id"]         = signal_id
            sig["created_at"] = datetime.utcnow().isoformat()
            signals.append(sig)
            logger.info(f"신호 [{strat_name}] {code} {name} @ {sig['price']:,.0f}")

    await db.commit()
    return signals
