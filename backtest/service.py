"""
Backtest – 전략 백테스팅 엔진

DB에 저장된 과거 시세 데이터를 사용해 전략의 수익률을 검증합니다.

지원 전략:
  - breakout   : 돌파매매 (N일 신고가 + 거래대금 배수)
  - ma_cross   : 이동평균 크로스 (단기 > 장기)
  - rsi_reversal: RSI 과매도 반등
"""
import logging
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from api.models import MarketData

logger = logging.getLogger(__name__)

StrategyType = Literal["breakout", "ma_cross", "rsi_reversal"]

# ── 공통 파라미터 ──────────────────────────────────────────────────────────────
STOP_LOSS_PCT   = 0.02
TARGET_PCT      = 0.04
MAX_HOLD_DAYS   = 10      # 최대 보유 기간 (영업일)


# ── 데이터 로드 ────────────────────────────────────────────────────────────────

async def _load_ohlcv(
    db: AsyncSession,
    code: str,
    start_date: datetime,
    end_date: datetime,
) -> list[dict]:
    stmt = (
        select(MarketData)
        .where(and_(
            MarketData.code == code,
            MarketData.timestamp >= start_date,
            MarketData.timestamp <= end_date,
        ))
        .order_by(MarketData.timestamp)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "date":          r.timestamp,
            "open":          r.open or 0,
            "high":          r.high or 0,
            "low":           r.low or 0,
            "close":         r.close or 0,
            "volume":        r.volume or 0,
            "trading_value": r.trading_value or 0,
            "change_rate":   r.change_rate or 0,
        }
        for r in rows
    ]


# ── 지표 계산 ──────────────────────────────────────────────────────────────────

def _sma(prices: list[float], period: int) -> list[float | None]:
    result = []
    for i in range(len(prices)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(prices[i - period + 1: i + 1]) / period)
    return result


def _rsi(closes: list[float], period: int = 14) -> list[float | None]:
    result = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period, len(closes)):
        diff    = closes[i] - closes[i - 1]
        gain    = max(diff, 0)
        loss    = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs       = avg_gain / avg_loss if avg_loss else 0
        result.append(100 - 100 / (1 + rs))

    return result


# ── 신호 생성 ──────────────────────────────────────────────────────────────────

def _signals_breakout(data: list[dict], n_days: int = 20, vol_mult: float = 2.0) -> list[int]:
    """돌파매매 신호 인덱스 목록"""
    signals = []
    closes        = [d["close"] for d in data]
    highs         = [d["high"]  for d in data]
    trad_vals     = [d["trading_value"] for d in data]

    for i in range(n_days, len(data)):
        past_highs  = highs[i - n_days: i]
        past_vals   = trad_vals[i - n_days: i]
        n_high      = max(past_highs) if past_highs else 0
        avg_val     = sum(past_vals) / len(past_vals) if past_vals else 0

        if (
            highs[i] > n_high
            and avg_val > 0
            and trad_vals[i] >= avg_val * vol_mult
            and data[i]["change_rate"] >= 2.0
        ):
            signals.append(i)
    return signals


def _signals_ma_cross(data: list[dict], short: int = 5, long_: int = 20) -> list[int]:
    """이동평균 골든크로스 신호 인덱스 목록"""
    closes  = [d["close"] for d in data]
    sma_s   = _sma(closes, short)
    sma_l   = _sma(closes, long_)
    signals = []

    for i in range(1, len(data)):
        if sma_s[i] is None or sma_l[i] is None:
            continue
        if sma_s[i - 1] is None or sma_l[i - 1] is None:
            continue
        # 단기 MA가 장기 MA를 상향 돌파
        if sma_s[i - 1] <= sma_l[i - 1] and sma_s[i] > sma_l[i]:
            signals.append(i)
    return signals


def _signals_rsi_reversal(data: list[dict], period: int = 14, oversold: float = 30) -> list[int]:
    """RSI 과매도 반등 신호 인덱스 목록"""
    closes  = [d["close"] for d in data]
    rsi_vals = _rsi(closes, period)
    signals  = []

    for i in range(1, len(data)):
        if rsi_vals[i] is None or rsi_vals[i - 1] is None:
            continue
        # RSI가 과매도 구간에서 상향 돌파
        if rsi_vals[i - 1] < oversold <= rsi_vals[i]:
            signals.append(i)
    return signals


# ── 트레이드 시뮬레이션 ────────────────────────────────────────────────────────

def _simulate_trades(data: list[dict], signal_indices: list[int]) -> list[dict]:
    """
    신호 발생 시점에 매수 → 목표가/손절가/최대보유기간 중 먼저 도달한 조건에 매도
    """
    trades = []
    used_indices = set()

    for idx in signal_indices:
        if idx in used_indices:
            continue

        entry_price = data[idx]["close"]
        target      = entry_price * (1 + TARGET_PCT)
        stop        = entry_price * (1 - STOP_LOSS_PCT)
        entry_date  = data[idx]["date"]

        exit_price  = entry_price
        exit_date   = entry_date
        exit_reason = "기간 만료"

        for j in range(idx + 1, min(idx + MAX_HOLD_DAYS + 1, len(data))):
            used_indices.add(j)
            d = data[j]
            if d["high"] >= target:
                exit_price  = target
                exit_date   = d["date"]
                exit_reason = "목표가 달성"
                break
            if d["low"] <= stop:
                exit_price  = stop
                exit_date   = d["date"]
                exit_reason = "손절"
                break
        else:
            exit_price = data[min(idx + MAX_HOLD_DAYS, len(data) - 1)]["close"]
            exit_date  = data[min(idx + MAX_HOLD_DAYS, len(data) - 1)]["date"]

        profit_pct = (exit_price / entry_price - 1) * 100
        trades.append({
            "entry_date":  entry_date.strftime("%Y-%m-%d"),
            "exit_date":   exit_date.strftime("%Y-%m-%d") if hasattr(exit_date, "strftime") else str(exit_date),
            "entry_price": round(entry_price, 0),
            "exit_price":  round(exit_price, 0),
            "profit_pct":  round(profit_pct, 2),
            "exit_reason": exit_reason,
        })

    return trades


# ── 통계 계산 ──────────────────────────────────────────────────────────────────

def _calc_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"total_trades": 0}

    profits   = [t["profit_pct"] for t in trades]
    win_trades = [p for p in profits if p > 0]
    lose_trades = [p for p in profits if p <= 0]

    win_rate       = len(win_trades) / len(profits) * 100
    avg_profit     = sum(profits) / len(profits)
    avg_win        = sum(win_trades) / len(win_trades) if win_trades else 0
    avg_loss       = sum(lose_trades) / len(lose_trades) if lose_trades else 0
    profit_factor  = abs(avg_win / avg_loss) if avg_loss else float("inf")

    # 누적 수익률 (복리)
    cumulative = 1.0
    for p in profits:
        cumulative *= (1 + p / 100)
    cumulative_pct = (cumulative - 1) * 100

    # MDD (최대 낙폭)
    peak = 1.0
    mdd  = 0.0
    cur  = 1.0
    for p in profits:
        cur  *= (1 + p / 100)
        peak  = max(peak, cur)
        dd    = (peak - cur) / peak * 100
        mdd   = max(mdd, dd)

    return {
        "total_trades":    len(trades),
        "win_count":       len(win_trades),
        "lose_count":      len(lose_trades),
        "win_rate":        round(win_rate, 1),
        "avg_profit_pct":  round(avg_profit, 2),
        "avg_win_pct":     round(avg_win, 2),
        "avg_loss_pct":    round(avg_loss, 2),
        "profit_factor":   round(profit_factor, 2),
        "cumulative_pct":  round(cumulative_pct, 2),
        "max_drawdown_pct": round(mdd, 2),
    }


# ── 메인 백테스트 함수 ─────────────────────────────────────────────────────────

async def run_backtest(
    db: AsyncSession,
    code: str,
    strategy: StrategyType,
    start_date: str,
    end_date: str,
) -> dict:
    """
    단일 종목 백테스트를 실행합니다.

    Args:
        code:       종목코드 (예: "005930")
        strategy:   전략 이름 ("breakout" / "ma_cross" / "rsi_reversal")
        start_date: 시작일 (YYYY-MM-DD)
        end_date:   종료일 (YYYY-MM-DD)
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")

    data = await _load_ohlcv(db, code, start, end)
    if len(data) < 30:
        return {"error": f"데이터 부족 ({len(data)}일) — 시세를 먼저 수집하세요"}

    # 전략별 신호 생성
    if strategy == "breakout":
        signal_indices = _signals_breakout(data)
    elif strategy == "ma_cross":
        signal_indices = _signals_ma_cross(data)
    elif strategy == "rsi_reversal":
        signal_indices = _signals_rsi_reversal(data)
    else:
        return {"error": f"지원하지 않는 전략: {strategy}"}

    trades = _simulate_trades(data, signal_indices)
    stats  = _calc_stats(trades)

    return {
        "code":       code,
        "strategy":   strategy,
        "start_date": start_date,
        "end_date":   end_date,
        "data_days":  len(data),
        "stats":      stats,
        "trades":     trades,
    }


async def run_multi_backtest(
    db: AsyncSession,
    codes: list[str],
    strategy: StrategyType,
    start_date: str,
    end_date: str,
) -> dict:
    """여러 종목에 대해 백테스트를 실행하고 합산 통계를 반환합니다."""
    all_trades = []
    results    = []

    for code in codes:
        res = await run_backtest(db, code, strategy, start_date, end_date)
        if "error" not in res:
            all_trades.extend(res["trades"])
            results.append({"code": code, "stats": res["stats"]})

    return {
        "strategy":    strategy,
        "start_date":  start_date,
        "end_date":    end_date,
        "codes_tested": len(results),
        "total_stats": _calc_stats(all_trades),
        "per_code":    results,
    }
