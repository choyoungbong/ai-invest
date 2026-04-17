"""
Backtest – 전략 백테스팅 엔진

[B단계] 파라미터 그리드 서치 추가 (run_grid_search)
  기존: 단일 파라미터셋으로만 백테스트 실행
  수정: 주요 파라미터 조합을 자동 탐색해 최적값 도출
        → 현재 .env 설정이 데이터 기반으로 최적인지 검증 가능
        → 결과를 profit_factor 기준으로 랭킹 정렬

  탐색 파라미터 (breakout 기준):
    n_days (BREAKOUT_DAYS): [3, 5, 10, 20]
    vol_mult (VOLUME_MULTIPLIER): [1.5, 2.0, 3.0]
    stop_pct (STOP_LOSS_PCT): [1.0, 1.5, 2.0] (%)
    target_pct (TARGET_PROFIT_PCT): [3.0, 5.0, 7.0] (%)
  총 4×3×3×3 = 108 조합 × 종목수 — 시간이 걸리므로 top 20 결과만 반환

지원 전략: breakout / ma_cross / rsi_reversal
"""
import logging
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from api.models import MarketData

logger = logging.getLogger(__name__)

StrategyType = Literal["breakout", "ma_cross", "rsi_reversal"]

# ── 기본 파라미터 ──────────────────────────────────────────────────────────────
STOP_LOSS_PCT  = 0.02
TARGET_PCT     = 0.04
MAX_HOLD_DAYS  = 10


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
        diff     = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
        rs       = avg_gain / avg_loss if avg_loss else 0
        result.append(100 - 100 / (1 + rs))

    return result


# ── 신호 생성 ──────────────────────────────────────────────────────────────────

def _signals_breakout(
    data: list[dict],
    n_days: int = 20,
    vol_mult: float = 2.0,
    min_change: float = 2.0,
) -> list[int]:
    highs     = [d["high"] for d in data]
    trad_vals = [d["trading_value"] for d in data]
    signals   = []
    for i in range(n_days, len(data)):
        past_highs = highs[i - n_days: i]
        past_vals  = trad_vals[i - n_days: i]
        n_high     = max(past_highs) if past_highs else 0
        avg_val    = sum(past_vals) / len(past_vals) if past_vals else 0
        if (
            highs[i] > n_high
            and avg_val > 0
            and trad_vals[i] >= avg_val * vol_mult
            and data[i]["change_rate"] >= min_change
        ):
            signals.append(i)
    return signals


def _signals_ma_cross(data: list[dict], short: int = 5, long_: int = 20) -> list[int]:
    closes  = [d["close"] for d in data]
    sma_s   = _sma(closes, short)
    sma_l   = _sma(closes, long_)
    signals = []
    for i in range(1, len(data)):
        if any(v is None for v in [sma_s[i], sma_l[i], sma_s[i-1], sma_l[i-1]]):
            continue
        if sma_s[i-1] <= sma_l[i-1] and sma_s[i] > sma_l[i]:
            signals.append(i)
    return signals


def _signals_rsi_reversal(
    data: list[dict],
    period: int = 14,
    oversold: float = 30,
) -> list[int]:
    closes   = [d["close"] for d in data]
    rsi_vals = _rsi(closes, period)
    signals  = []
    for i in range(1, len(data)):
        if rsi_vals[i] is None or rsi_vals[i-1] is None:
            continue
        if rsi_vals[i-1] < oversold <= rsi_vals[i]:
            signals.append(i)
    return signals


# ── 트레이드 시뮬레이션 ────────────────────────────────────────────────────────

def _simulate_trades(
    data: list[dict],
    signal_indices: list[int],
    stop_pct: float = STOP_LOSS_PCT,
    target_pct: float = TARGET_PCT,
    max_hold: int = MAX_HOLD_DAYS,
) -> list[dict]:
    trades       = []
    used_indices = set()

    for idx in signal_indices:
        if idx in used_indices:
            continue

        entry_price = data[idx]["close"]
        target      = entry_price * (1 + target_pct)
        stop        = entry_price * (1 - stop_pct)
        entry_date  = data[idx]["date"]

        exit_price  = entry_price
        exit_date   = entry_date
        exit_reason = "기간 만료"

        for j in range(idx + 1, min(idx + max_hold + 1, len(data))):
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
            idx_end    = min(idx + max_hold, len(data) - 1)
            exit_price = data[idx_end]["close"]
            exit_date  = data[idx_end]["date"]

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
        return {"total_trades": 0, "win_rate": 0, "profit_factor": 0,
                "cumulative_pct": 0, "max_drawdown_pct": 0}

    profits    = [t["profit_pct"] for t in trades]
    win_trades = [p for p in profits if p > 0]
    lose_trades = [p for p in profits if p <= 0]

    win_rate      = len(win_trades) / len(profits) * 100
    avg_profit    = sum(profits) / len(profits)
    avg_win       = sum(win_trades) / len(win_trades) if win_trades else 0
    avg_loss      = sum(lose_trades) / len(lose_trades) if lose_trades else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss else float("inf")

    cumulative = 1.0
    for p in profits:
        cumulative *= (1 + p / 100)
    cumulative_pct = (cumulative - 1) * 100

    peak, mdd, cur = 1.0, 0.0, 1.0
    for p in profits:
        cur  *= (1 + p / 100)
        peak  = max(peak, cur)
        mdd   = max(mdd, (peak - cur) / peak * 100)

    return {
        "total_trades":     len(trades),
        "win_count":        len(win_trades),
        "lose_count":       len(lose_trades),
        "win_rate":         round(win_rate, 1),
        "avg_profit_pct":   round(avg_profit, 2),
        "avg_win_pct":      round(avg_win, 2),
        "avg_loss_pct":     round(avg_loss, 2),
        "profit_factor":    round(profit_factor, 2) if profit_factor != float("inf") else 99.0,
        "cumulative_pct":   round(cumulative_pct, 2),
        "max_drawdown_pct": round(mdd, 2),
    }


# ── 메인 백테스트 ──────────────────────────────────────────────────────────────

async def run_backtest(
    db: AsyncSession,
    code: str,
    strategy: StrategyType,
    start_date: str,
    end_date: str,
) -> dict:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")
    data  = await _load_ohlcv(db, code, start, end)

    if len(data) < 30:
        return {"error": f"데이터 부족 ({len(data)}일) — 시세를 먼저 수집하세요"}

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
        "code": code, "strategy": strategy,
        "start_date": start_date, "end_date": end_date,
        "data_days": len(data), "stats": stats, "trades": trades,
    }


async def run_multi_backtest(
    db: AsyncSession,
    codes: list[str],
    strategy: StrategyType,
    start_date: str,
    end_date: str,
) -> dict:
    all_trades, results = [], []
    for code in codes:
        res = await run_backtest(db, code, strategy, start_date, end_date)
        if "error" not in res:
            all_trades.extend(res["trades"])
            results.append({"code": code, "stats": res["stats"]})

    return {
        "strategy": strategy, "start_date": start_date, "end_date": end_date,
        "codes_tested": len(results),
        "total_stats": _calc_stats(all_trades),
        "per_code": results,
    }


# ── 그리드 서치 (B단계 신규) ──────────────────────────────────────────────────

async def run_grid_search(
    db: AsyncSession,
    codes: list[str],
    strategy: StrategyType,
    start_date: str,
    end_date: str,
    top_n: int = 20,
) -> dict:
    """
    파라미터 조합별 백테스트를 실행해 최적값을 탐색합니다.

    탐색 파라미터:
      breakout:    n_days × vol_mult × stop_pct × target_pct = 108 조합
      ma_cross:    short × long_ × stop_pct × target_pct     =  54 조합
      rsi_reversal: period × oversold × stop_pct × target_pct = 36 조합

    반환: profit_factor 내림차순으로 정렬된 top_n 조합 + 현재 설정 기준 성과
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")

    # 모든 종목 데이터 미리 로드
    all_data: dict[str, list] = {}
    for code in codes:
        data = await _load_ohlcv(db, code, start, end)
        if len(data) >= 30:
            all_data[code] = data

    if not all_data:
        return {"error": "유효한 데이터가 있는 종목이 없습니다"}

    # 파라미터 그리드 정의
    stop_targets = [
        (s, t)
        for s in [0.01, 0.015, 0.02]
        for t in [0.03, 0.05, 0.07]
    ]

    if strategy == "breakout":
        param_combinations = [
            {"n_days": n, "vol_mult": v, "stop_pct": s, "target_pct": t}
            for n in [3, 5, 10, 20]
            for v in [1.5, 2.0, 3.0]
            for s, t in stop_targets
        ]
    elif strategy == "ma_cross":
        param_combinations = [
            {"short": sh, "long_": lo, "stop_pct": s, "target_pct": t}
            for sh in [3, 5]
            for lo in [10, 20, 30]
            for s, t in stop_targets
        ]
    elif strategy == "rsi_reversal":
        param_combinations = [
            {"period": p, "oversold": o, "stop_pct": s, "target_pct": t}
            for p in [7, 14]
            for o in [25, 30, 35]
            for s, t in stop_targets
        ]
    else:
        return {"error": f"지원하지 않는 전략: {strategy}"}

    results = []

    for params in param_combinations:
        all_trades = []
        for code, data in all_data.items():
            # 파라미터별 신호 생성
            if strategy == "breakout":
                indices = _signals_breakout(
                    data,
                    n_days=params["n_days"],
                    vol_mult=params["vol_mult"],
                )
            elif strategy == "ma_cross":
                indices = _signals_ma_cross(
                    data,
                    short=params["short"],
                    long_=params["long_"],
                )
            else:
                indices = _signals_rsi_reversal(
                    data,
                    period=params["period"],
                    oversold=params["oversold"],
                )

            trades = _simulate_trades(
                data, indices,
                stop_pct=params["stop_pct"],
                target_pct=params["target_pct"],
            )
            all_trades.extend(trades)

        stats = _calc_stats(all_trades)
        if stats["total_trades"] < 3:  # 너무 적은 거래는 제외
            continue

        results.append({
            "params":          params,
            "total_trades":    stats["total_trades"],
            "win_rate":        stats["win_rate"],
            "profit_factor":   stats["profit_factor"],
            "cumulative_pct":  stats["cumulative_pct"],
            "max_drawdown_pct": stats["max_drawdown_pct"],
            "avg_profit_pct":  stats["avg_profit_pct"],
        })

    # profit_factor 내림차순 정렬
    results.sort(key=lambda x: x["profit_factor"], reverse=True)

    return {
        "strategy":          strategy,
        "start_date":        start_date,
        "end_date":          end_date,
        "codes_tested":      len(all_data),
        "total_combinations": len(param_combinations),
        "valid_results":     len(results),
        "top_results":       results[:top_n],
        "best":              results[0] if results else None,
    }
