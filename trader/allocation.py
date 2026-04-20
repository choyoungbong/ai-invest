"""
Capital Allocation – 종목당 투자금 배분

종목당 최대 투자금(MAX_AMOUNT_PER_STOCK)을 기준으로,
신뢰도에 따라 비율을 조정합니다.

  신뢰도 ≥ 0.7 : MAX_AMOUNT_PER_STOCK × 100%
  신뢰도 0.4~0.7: MAX_AMOUNT_PER_STOCK × 60%
  신뢰도 < 0.4  : MAX_AMOUNT_PER_STOCK × 30%

분할매수 적용 시 (SPLIT_BUY_RATIO=0.6):
  1차 매수 = 위 금액 × 60%
  2차 매수 = 위 금액 × 40%
"""
import logging
import os

logger = logging.getLogger(__name__)

TOTAL_BUDGET         = int(os.getenv("TOTAL_BUDGET",         "3000000"))
MAX_AMOUNT_PER_STOCK = int(os.getenv("MAX_AMOUNT_PER_STOCK", "1000000"))
MIN_ORDER_AMOUNT     = 100_000  # 최소 주문금액 10만원


def get_order_amount(strategy: str, confidence: float = 0.5) -> int:
    """
    신뢰도 기반 주문 금액 반환.
    전략 무관하게 MAX_AMOUNT_PER_STOCK 기준으로 계산.
    """
    if confidence >= 0.7:
        ratio = 1.0
    elif confidence >= 0.4:
        ratio = 0.6
    else:
        ratio = 0.3

    amount = int(MAX_AMOUNT_PER_STOCK * ratio)
    return max(amount, MIN_ORDER_AMOUNT)


def get_strategy_budget(strategy: str) -> int:
    """하위 호환성 유지용 — MAX_AMOUNT_PER_STOCK 반환"""
    return MAX_AMOUNT_PER_STOCK


def calc_quantity_by_budget(strategy: str, price: float, confidence: float = 0.5) -> int:
    if price <= 0:
        return 0
    amount = get_order_amount(strategy, confidence)
    return max(int(amount // price), 1)


def get_allocation_summary() -> dict:
    return {
        "total_budget":         TOTAL_BUDGET,
        "max_amount_per_stock": MAX_AMOUNT_PER_STOCK,
        "confidence_tiers": {
            "high (≥0.7)":   MAX_AMOUNT_PER_STOCK,
            "mid (0.4~0.7)": int(MAX_AMOUNT_PER_STOCK * 0.6),
            "low (<0.4)":    int(MAX_AMOUNT_PER_STOCK * 0.3),
        },
    }
