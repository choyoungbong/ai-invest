"""
Strategy – 돌파매매 전략 엔진

돌파 조건:
  1. 당일 고가가 N일 최고가를 돌파
  2. 거래대금이 평균 대비 V배 이상
  3. 등락률이 MIN_CHANGE_RATE% 이상
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_

from api.models import MarketData, Signal, Stock

logger = logging.getLogger(__name__)

# ── 전략 파라미터 ──────────────────────────────────────────────────────────────
BREAKOUT_DAYS = 20          # N일 최고가 돌파 기준
VOLUME_MULTIPLIER = 2.0     # 평균 거래대금 대비 배수
MIN_CHANGE_RATE = 2.0       # 최소 등락률 %
STOP_LOSS_PCT = 0.02        # 손절 2%
TARGET_PROFIT_PCT = 0.04    # 목표수익 4%


# ── 개별 종목 돌파 판단 ────────────────────────────────────────────────────────

async def _fetch_recent_data(db: AsyncSession, code: str, days: int) -> List[dict]:
    """최근 N일 시세를 가져옵니다."""
    cutoff = datetime.utcnow() - timedelta(days=days + 5)

    stmt = (
        select(MarketData)
        .where(
            and_(
                MarketData.code == code,
                MarketData.timestamp >= cutoff,
            )
        )
        .order_by(desc(MarketData.timestamp))
        .limit(days + 1)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return rows


async def check_breakout(db: AsyncSession, code: str, name: str) -> Optional[dict]:
    """
    돌파 조건을 확인하고 신호 dict를 반환합니다.
    조건 미충족 시 None 반환.
    """
    rows = await _fetch_recent_data(db, code, BREAKOUT_DAYS)
    if len(rows) < 5:
        return None  # 데이터 부족

    today = rows[0]         # 최신 데이터 = 당일
    history = rows[1:]      # 이전 데이터

    if not today.high or not today.close:
        return None

    # 1) N일 최고가
    past_highs = [r.high for r in history if r.high]
    if not past_highs:
        return None
    n_day_high = max(past_highs)

    # 2) 평균 거래대금
    past_values = [r.trading_value for r in history if r.trading_value]
    avg_value = sum(past_values) / len(past_values) if past_values else 0

    # ── 돌파 조건 평가 ──
    cond_high    = today.high > n_day_high                        # 신고가 돌파
    cond_volume  = avg_value > 0 and today.trading_value >= avg_value * VOLUME_MULTIPLIER
    cond_change  = today.change_rate >= MIN_CHANGE_RATE

    if not (cond_high and cond_volume and cond_change):
        return None

    price      = today.close
    stop_loss  = round(price * (1 - STOP_LOSS_PCT), 0)
    target     = round(price * (1 + TARGET_PROFIT_PCT), 0)

    confidence = _calc_confidence(today, n_day_high, avg_value)

    return {
        "code":         code,
        "name":         name,
        "signal_type":  "BUY",
        "strategy":     "breakout",
        "price":        price,
        "target_price": target,
        "stop_loss":    stop_loss,
        "reason": (
            f"{BREAKOUT_DAYS}일 신고가 돌파 (이전고가 {n_day_high:,.0f}→당일고가 {today.high:,.0f}), "
            f"거래대금 평균대비 {today.trading_value / avg_value:.1f}배, "
            f"등락률 {today.change_rate:.1f}%"
        ),
        "confidence": confidence,
    }


def _calc_confidence(today, n_day_high: float, avg_value: float) -> float:
    """
    간단한 신뢰도 점수 (0~1).
    - 고가 돌파 폭 (최대 0.4)
    - 거래대금 배수 (최대 0.4)
    - 등락률 (최대 0.2)
    """
    score = 0.0

    # 고가 돌파 폭
    if n_day_high > 0:
        pct = (today.high - n_day_high) / n_day_high
        score += min(pct * 10, 0.4)

    # 거래대금 배수
    if avg_value > 0:
        multiplier = today.trading_value / avg_value
        score += min((multiplier - 1) * 0.1, 0.4)

    # 등락률
    score += min(today.change_rate / 20, 0.2)

    return round(min(score, 1.0), 3)


# ── 전략 엔진 메인 ─────────────────────────────────────────────────────────────

async def run_strategy(
    db: AsyncSession,
    candidates: List[dict],
) -> List[dict]:
    """
    스캐너에서 넘겨받은 후보 종목에 대해 돌파 전략을 적용합니다.
    신호가 발생하면 DB에 저장하고 목록을 반환합니다.
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
        await db.execute(
            Signal.__table__.insert().values(
                id=signal_id,
                **sig,
            )
        )
        sig["id"] = signal_id
        sig["created_at"] = datetime.utcnow().isoformat()
        signals.append(sig)
        logger.info(f"신호 발생 [{code} {name}] {sig['signal_type']} @ {sig['price']:,.0f}")

    await db.commit()
    logger.info(f"전략 실행 완료: {len(candidates)}개 후보 → {len(signals)}개 신호")
    return signals


# ── 신호 목록 조회 ─────────────────────────────────────────────────────────────

async def get_signals(
    db: AsyncSession,
    limit: int = 50,
    signal_type: Optional[str] = None,
) -> List[dict]:
    """최근 신호 목록을 반환합니다."""
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
            "is_executed":  r.is_executed,
            "created_at":   r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
