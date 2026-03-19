"""
Capital Allocation – 전략별 자금 배분

각 전략에 총 투자 예산의 비율을 할당합니다.
신호 발생 시 해당 전략의 배분 비율에 따라 주문 금액이 결정됩니다.

설정 예시:
  총 투자 예산:  5,000,000원
  breakout:     40% → 최대 2,000,000원
  ma_cross:     30% → 최대 1,500,000원
  rsi_reversal: 20% → 최대 1,000,000원
  macd:         10% → 최대   500,000원
"""
import logging
import os

logger = logging.getLogger(__name__)

# ── 전체 투자 예산 ─────────────────────────────────────────────────────────────
TOTAL_BUDGET = int(os.getenv("TOTAL_BUDGET", "5000000"))   # 기본 500만원

# ── 전략별 배분 비율 (합계 = 1.0) ─────────────────────────────────────────────
ALLOCATION: dict[str, float] = {
    "breakout":     float(os.getenv("ALLOC_BREAKOUT",     "0.40")),
    "ma_cross":     float(os.getenv("ALLOC_MA_CROSS",     "0.30")),
    "rsi_reversal": float(os.getenv("ALLOC_RSI_REVERSAL", "0.20")),
    "macd":         float(os.getenv("ALLOC_MACD",         "0.10")),
}

# ── 리스크 파라미터 ────────────────────────────────────────────────────────────
MAX_SINGLE_TRADE_PCT = float(os.getenv("MAX_SINGLE_TRADE_PCT", "0.20"))  # 전략 예산의 최대 20%
MIN_ORDER_AMOUNT     = 100_000   # 최소 주문금액 10만원


def get_strategy_budget(strategy: str) -> int:
    """전략에 할당된 예산(원)을 반환합니다."""
    ratio = ALLOCATION.get(strategy, 0.10)
    return int(TOTAL_BUDGET * ratio)


def get_order_amount(strategy: str, confidence: float = 0.5) -> int:
    """
    전략 + 신뢰도 기반 주문 금액을 계산합니다.

    신뢰도가 높을수록 더 많은 금액을 투자합니다.
    - confidence 0.0~0.4: 전략 예산의 30%
    - confidence 0.4~0.7: 전략 예산의 60%
    - confidence 0.7~1.0: 전략 예산의 100% (최대 20%)
    """
    budget = get_strategy_budget(strategy)

    if confidence >= 0.7:
        ratio = 1.0
    elif confidence >= 0.4:
        ratio = 0.6
    else:
        ratio = 0.3

    amount = int(budget * ratio * MAX_SINGLE_TRADE_PCT)
    return max(amount, MIN_ORDER_AMOUNT)


def calc_quantity_by_budget(strategy: str, price: float, confidence: float = 0.5) -> int:
    """전략/신뢰도/현재가를 기반으로 주문 수량을 계산합니다."""
    if price <= 0:
        return 0
    amount = get_order_amount(strategy, confidence)
    qty    = int(amount // price)
    return max(qty, 1)


def get_allocation_summary() -> dict:
    """현재 자금 배분 현황을 반환합니다."""
    total_ratio = sum(ALLOCATION.values())
    return {
        "total_budget":    TOTAL_BUDGET,
        "total_ratio":     round(total_ratio, 2),
        "is_valid":        abs(total_ratio - 1.0) < 0.01,
        "strategies": {
            name: {
                "ratio":      ratio,
                "budget":     int(TOTAL_BUDGET * ratio),
                "max_single": int(TOTAL_BUDGET * ratio * MAX_SINGLE_TRADE_PCT),
            }
            for name, ratio in ALLOCATION.items()
        },
    }
