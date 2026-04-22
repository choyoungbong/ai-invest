"""
US Strategy — 미국 ETF 자동매매 전략 엔진

대상 ETF 및 손익 설정:
  SPLG (S&P500): 익절 +1.2% / 손절 -0.8%  — 안정형
  TQQQ (나스닥3x): 익절 +1.8% / 손절 -1.2% — 레버리지
  SOXL (반도체3x): 익절 +2.5% / 손절 -1.5% — 레버리지

신호 조건 (30분봉):
  1. EMA5 > EMA20 (단기 상승 추세)
  2. RSI 40~65 (모멘텀 확인, 과매수 제외)
  3. 거래량 평균 1.5배 이상
  4. 현재가 >= EMA20 × 0.995

리스크 관리:
  - 연속 손절 2회 시 해당 종목 당일 거래 중단
  - 전체 일일 손실 -3% 초과 시 당일 전체 중단
  - 일일 최대 5회 거래 제한
"""
import logging
import os
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# ── ETF 설정 ──────────────────────────────────────────────────────────────────

US_ETF_CONFIG: dict[str, dict] = {
    "SPLG": {
        "name":          "SPDR Portfolio S&P 500 ETF",
        "exchange":      "NYSE",
        "target_profit": float(os.getenv("US_SPLG_TARGET_PCT",  "0.012")),
        "stop_loss":     float(os.getenv("US_SPLG_STOP_PCT",   "-0.008")),
        "budget_ratio":  float(os.getenv("US_SPLG_BUDGET_RATIO", "0.33")),
    },
    "TQQQ": {
        "name":          "ProShares UltraPro QQQ (3x Nasdaq)",
        "exchange":      "NASD",
        "target_profit": float(os.getenv("US_TQQQ_TARGET_PCT",  "0.018")),
        "stop_loss":     float(os.getenv("US_TQQQ_STOP_PCT",   "-0.012")),
        "budget_ratio":  float(os.getenv("US_TQQQ_BUDGET_RATIO", "0.33")),
    },
    "SOXL": {
        "name":          "Direxion Daily Semiconductor Bull 3X",
        "exchange":      "AMEX",
        "target_profit": float(os.getenv("US_SOXL_TARGET_PCT",  "0.025")),
        "stop_loss":     float(os.getenv("US_SOXL_STOP_PCT",   "-0.015")),
        "budget_ratio":  float(os.getenv("US_SOXL_BUDGET_RATIO", "0.34")),
    },
}

# ── 전역 파라미터 ─────────────────────────────────────────────────────────────
US_MAX_DAILY_TRADES      = int(os.getenv("US_MAX_DAILY_TRADES",       "5"))
US_MAX_CONSEC_LOSSES     = int(os.getenv("US_MAX_CONSEC_LOSSES",      "2"))
US_DAILY_LOSS_LIMIT_PCT  = float(os.getenv("US_DAILY_LOSS_LIMIT_PCT", "3.0"))
US_MIN_VOL_MULT          = float(os.getenv("US_MIN_VOLUME_MULTIPLIER", "1.5"))

# ── 당일 상태 (인메모리) ──────────────────────────────────────────────────────
_state: dict = {
    "date":              None,
    "total_trades":      0,
    "realized_pnl_usd":  0.0,
    "initial_value_usd": 0.0,   # 당일 시작 시점 총자산 (손실률 계산용)
    "consec_losses":     {},
    "blocked_symbols":   set(),
    "daily_blocked":     False,
    "liquidation_done":  False,  # 당일 비대상 종목 청산 완료 여부
}


def _reset_if_new_day() -> None:
    today = datetime.now(KST).date()
    if _state["date"] != today:
        _state.update({
            "date":              today,
            "total_trades":      0,
            "realized_pnl_usd":  0.0,
            "initial_value_usd": 0.0,
            "consec_losses":     {},
            "blocked_symbols":   set(),
            "daily_blocked":     False,
            "liquidation_done":  False,
        })
        logger.info(f"[US 전략] 당일 상태 초기화: {today}")


def set_initial_value(total_usd: float) -> None:
    """당일 시작 시 총자산 기록 (손실률 계산 기준점)"""
    _reset_if_new_day()
    if _state["initial_value_usd"] == 0.0:
        _state["initial_value_usd"] = total_usd
        logger.info(f"[US 전략] 당일 기준 총자산: ${total_usd:.2f}")


def mark_liquidation_done() -> None:
    """비대상 종목 청산 완료 표시"""
    _reset_if_new_day()
    _state["liquidation_done"] = True


def is_liquidation_done() -> bool:
    _reset_if_new_day()
    return _state["liquidation_done"]


def record_trade_result(symbol: str, pnl_usd: float) -> None:
    """거래 결과 기록 및 리스크 체크"""
    _reset_if_new_day()
    _state["total_trades"]     += 1
    _state["realized_pnl_usd"] += pnl_usd

    if pnl_usd < 0:
        _state["consec_losses"][symbol] = _state["consec_losses"].get(symbol, 0) + 1
        cnt = _state["consec_losses"][symbol]
        if cnt >= US_MAX_CONSEC_LOSSES:
            _state["blocked_symbols"].add(symbol)
            logger.warning(f"[US 전략] {symbol} 연속 손절 {cnt}회 → 당일 거래 중단")
    else:
        _state["consec_losses"][symbol] = 0

    # 전체 일일 손실 한도 체크
    if _state["initial_value_usd"] > 0 and _state["realized_pnl_usd"] < 0:
        loss_pct = abs(_state["realized_pnl_usd"]) / _state["initial_value_usd"] * 100
        if loss_pct >= US_DAILY_LOSS_LIMIT_PCT:
            _state["daily_blocked"] = True
            logger.warning(
                f"[US 전략] 일일 손실 한도 초과: "
                f"${_state['realized_pnl_usd']:.2f} ({loss_pct:.1f}%) → 당일 전체 중단"
            )


def can_trade(symbol: str) -> tuple[bool, str]:
    """매수 가능 여부 확인"""
    _reset_if_new_day()

    from trader.us_kis_client import is_us_market_open
    if not is_us_market_open():
        return False, "미국 장 외 시간"

    if not is_liquidation_done():
        return False, "비대상 종목 청산 미완료"

    if _state["daily_blocked"]:
        return False, f"일일 손실 한도 초과 (${_state['realized_pnl_usd']:.2f})"

    if _state["total_trades"] >= US_MAX_DAILY_TRADES:
        return False, f"일일 최대 거래 횟수 초과 ({_state['total_trades']}/{US_MAX_DAILY_TRADES})"

    if symbol in _state["blocked_symbols"]:
        return False, f"연속 손절로 당일 거래 중단"

    return True, ""


def get_daily_status() -> dict:
    _reset_if_new_day()
    return {
        "date":              str(_state["date"]),
        "total_trades":      _state["total_trades"],
        "max_daily_trades":  US_MAX_DAILY_TRADES,
        "realized_pnl_usd":  round(_state["realized_pnl_usd"], 2),
        "initial_value_usd": round(_state["initial_value_usd"], 2),
        "daily_blocked":     _state["daily_blocked"],
        "blocked_symbols":   list(_state["blocked_symbols"]),
        "consec_losses":     dict(_state["consec_losses"]),
        "liquidation_done":  _state["liquidation_done"],
    }


# ── 지표 계산 ─────────────────────────────────────────────────────────────────

def _ema(prices: list[float], period: int) -> list[float | None]:
    if len(prices) < period:
        return [None] * len(prices)
    result = [None] * (period - 1)
    sma    = sum(prices[:period]) / period
    result.append(sma)
    k = 2 / (period + 1)
    for p in prices[period:]:
        result.append(result[-1] * (1 - k) + p * k)
    return result


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period * 2:
        return None
    diffs  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    ag     = sum(gains[:period]) / period
    al     = sum(losses[:period]) / period
    for i in range(period, len(diffs)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0


# ── 30분봉 데이터 조회 ────────────────────────────────────────────────────────

async def fetch_ohlcv(symbol: str, bars: int = 30) -> list[dict]:
    """
    yfinance로 미국 ETF 30분봉 조회.
    SPLG는 yfinance에서 조회 불가 → SPY로 대체 (동일 지수 추종)
    """
    try:
        import yfinance as yf
        # SPLG fallback: yfinance에서 데이터 없음 → SPY로 대체
        yf_symbol = "SPY" if symbol == "SPLG" else symbol
        df = yf.Ticker(yf_symbol).history(period="5d", interval="30m")
        if df.empty:
            return []
        rows = []
        for ts, row in df.tail(bars).iterrows():
            rows.append({
                "close":  float(row["Close"]),
                "volume": int(row["Volume"]),
            })
        return rows
    except ImportError:
        logger.warning("[US 전략] yfinance 미설치 — OHLCV 조회 불가")
        return []
    except Exception as e:
        logger.warning(f"[US 전략] {symbol} OHLCV 조회 실패: {e}")
        return []


# ── 신호 생성 ─────────────────────────────────────────────────────────────────

async def generate_signal(symbol: str) -> Optional[dict]:
    """
    30분봉 기반 매수 신호 생성.

    조건:
      1. EMA5 > EMA20 (상승 추세)
      2. RSI 40~65 (모멘텀 적정 구간)
      3. 거래량 10봉 평균 대비 1.5배 이상
      4. 현재가 >= EMA20 × 0.995

    데이터 부족 시 현재가만으로 최소 신호 생성 (fallback).
    """
    tradable, reason = can_trade(symbol)
    if not tradable:
        logger.debug(f"[US 전략] {symbol} 거래 불가: {reason}")
        return None

    cfg = US_ETF_CONFIG.get(symbol)
    if not cfg:
        return None

    from trader.us_kis_client import get_us_price
    price_data    = await get_us_price(symbol)
    current_price = price_data["price"]
    if current_price <= 0:
        return None

    bars   = await fetch_ohlcv(symbol, bars=30)
    target = round(current_price * (1 + cfg["target_profit"]), 4)
    stop   = round(current_price * (1 + cfg["stop_loss"]), 4)

    if len(bars) < 20:
        # fallback: OHLCV 부족 시 현재가 기반 신호
        logger.info(f"[US 전략] {symbol} OHLCV 부족 — fallback 신호")
        return {
            "symbol":       symbol,
            "name":         cfg["name"],
            "exchange":     cfg["exchange"],
            "price":        current_price,
            "target_price": target,
            "stop_loss":    stop,
            "target_pct":   cfg["target_profit"],
            "stop_pct":     cfg["stop_loss"],
            "budget_ratio": cfg["budget_ratio"],
            "rsi":          None,
            "ema5":         None,
            "ema20":        None,
            "vol_mult":     None,
        }

    closes  = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    ema5_list  = _ema(closes, 5)
    ema20_list = _ema(closes, 20)
    rsi        = _rsi(closes)

    ema5  = ema5_list[-1]
    ema20 = ema20_list[-1]
    if ema5 is None or ema20 is None:
        return None

    avg_vol  = sum(volumes[-10:]) / 10
    vol_mult = volumes[-1] / avg_vol if avg_vol > 0 else 0

    cond_trend  = ema5 > ema20
    cond_rsi    = rsi is not None and 40 <= rsi <= 65
    cond_vol    = vol_mult >= US_MIN_VOL_MULT
    cond_price  = current_price >= ema20 * 0.995

    if not (cond_trend and cond_rsi and cond_vol and cond_price):
        logger.debug(
            f"[US 전략] {symbol} 신호 없음 | "
            f"추세:{'✅' if cond_trend else '❌'} "
            f"RSI:{rsi}({'✅' if cond_rsi else '❌'}) "
            f"거래량:{vol_mult:.1f}x({'✅' if cond_vol else '❌'}) "
            f"가격:{'✅' if cond_price else '❌'}"
        )
        return None

    logger.info(
        f"[US 전략] 신호: {symbol} @ ${current_price:.2f} | "
        f"목표 ${target:.2f} / 손절 ${stop:.2f} | "
        f"RSI:{rsi} EMA5:{ema5:.2f} EMA20:{ema20:.2f} Vol:{vol_mult:.1f}x"
    )

    return {
        "symbol":       symbol,
        "name":         cfg["name"],
        "exchange":     cfg["exchange"],
        "price":        current_price,
        "target_price": target,
        "stop_loss":    stop,
        "target_pct":   cfg["target_profit"],
        "stop_pct":     cfg["stop_loss"],
        "budget_ratio": cfg["budget_ratio"],
        "rsi":          rsi,
        "ema5":         round(ema5, 4),
        "ema20":        round(ema20, 4),
        "vol_mult":     round(vol_mult, 2),
    }
