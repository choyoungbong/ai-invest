"""
Strategy Guard – 전략 보호 필터

1. 시장 상황 필터: 코스피 하락 추세 시 매수 중단
2. 최대 보유 기간: N일 초과 시 자동 청산
3. 재매수 쿨타임: 동일 종목 손절 후 N시간 재매수 금지
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

import FinanceDataReader as fdr

from api.models import Trade, Signal
from notification.service import send_message

logger = logging.getLogger(__name__)

# ── 파라미터 ──────────────────────────────────────────────────────────────────
MAX_HOLD_DAYS      = int(os.getenv("MAX_HOLD_DAYS",   "3"))    # 최대 보유 3일
COOLTIME_HOURS     = int(os.getenv("COOLTIME_HOURS",  "24"))   # 손절 후 24시간 재매수 금지
MARKET_FILTER_DAYS = int(os.getenv("MARKET_FILTER_DAYS", "5")) # 코스피 N일 추세 확인


# ── 1. 시장 상황 필터 ─────────────────────────────────────────────────────────

async def is_market_bullish() -> bool:
    """
    코스피 지수가 상승 추세인지 확인합니다.
    최근 N일 중 종가가 이전보다 높은 날이 절반 이상이면 상승 추세.
    """
    try:
        from datetime import date
        end   = date.today()
        start = end - timedelta(days=MARKET_FILTER_DAYS * 2)
        df    = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        if df is None or len(df) < MARKET_FILTER_DAYS:
            logger.warning("코스피 데이터 부족 — 필터 통과")
            return True

        recent = df.tail(MARKET_FILTER_DAYS)
        closes = list(recent["Close"])
        up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        bullish = up_days >= len(closes) // 2

        logger.info(f"시장 필터: 최근 {MARKET_FILTER_DAYS}일 중 상승 {up_days}일 → {'상승' if bullish else '하락'} 추세")
        return bullish

    except Exception as e:
        logger.warning(f"시장 필터 오류: {e} — 필터 통과")
        return True


# ── 2. 재매수 쿨타임 확인 ─────────────────────────────────────────────────────

async def is_in_cooltime(db: AsyncSession, code: str) -> bool:
    """
    해당 종목이 최근 손절 후 쿨타임 내에 있는지 확인합니다.
    """
    cutoff = datetime.utcnow() - timedelta(hours=COOLTIME_HOURS)

    # 최근 손절 매도 조회
    stmt = (
        select(Trade)
        .where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= cutoff,
        ))
        .order_by(desc(Trade.created_at))
        .limit(1)
    )
    recent_sell = (await db.execute(stmt)).scalars().first()

    if recent_sell:
        # 손절인지 확인 (매도가 < 매수가)
        buy_stmt = (
            select(Trade)
            .where(and_(
                Trade.code == code,
                Trade.order_type == "BUY",
                Trade.signal_id == recent_sell.signal_id,
            ))
        )
        buy = (await db.execute(buy_stmt)).scalars().first()
        if buy and recent_sell.price < buy.price:
            remain = COOLTIME_HOURS - (datetime.utcnow() - recent_sell.created_at).seconds // 3600
            logger.info(f"[{code}] 쿨타임 중 (잔여 약 {remain}시간)")
            return True

    return False


# ── 3. 최대 보유 기간 초과 청산 ───────────────────────────────────────────────

async def check_and_close_expired_positions(db: AsyncSession) -> list[dict]:
    """
    MAX_HOLD_DAYS 초과 보유 중인 포지션을 자동 청산합니다.
    """
    from trader import kis_client as kis
    import uuid

    cutoff = datetime.utcnow() - timedelta(days=MAX_HOLD_DAYS)

    stmt = (
        select(Trade)
        .where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
            Trade.created_at <= cutoff,
        ))
    )
    old_trades = (await db.execute(stmt)).scalars().all()

    closed = []
    for trade in old_trades:
        # 이미 청산됐는지 확인
        sold = (await db.execute(
            select(Trade).where(and_(
                Trade.code == trade.code,
                Trade.order_type == "SELL",
                Trade.signal_id == trade.signal_id,
            ))
        )).scalars().first()
        if sold:
            continue

        # 현재가 조회
        try:
            price_data    = await kis.get_current_price(trade.code)
            current_price = price_data["price"]
        except Exception:
            current_price = trade.price

        # 시장가 매도
        try:
            result = await kis.sell_order(trade.code, trade.quantity, order_type="01")
        except Exception as e:
            logger.error(f"기간 만료 청산 실패 [{trade.code}]: {e}")
            continue

        trade_id = str(uuid.uuid4())
        pnl      = (current_price - trade.price) * trade.quantity
        pnl_pct  = (current_price / trade.price - 1) * 100

        await db.execute(
            Trade.__table__.insert().values(
                id=trade_id,
                signal_id=trade.signal_id,
                code=trade.code,
                name=trade.name,
                order_type="SELL",
                price=current_price,
                quantity=trade.quantity,
                amount=current_price * trade.quantity,
                status="FILLED" if result["success"] else "FAILED",
                broker_order_id=result.get("order_no", ""),
                filled_at=datetime.utcnow(),
            )
        )

        await send_message(
            f"⏰ <b>[AI INVEST] 보유기간 만료 청산</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{trade.name} ({trade.code})</b>\n"
            f"📅 보유기간: {MAX_HOLD_DAYS}일 초과\n"
            f"💰 매수가: {trade.price:,}원\n"
            f"💱 매도가: {current_price:,}원\n"
            f"📊 수익률: {pnl_pct:+.2f}%\n"
            f"💵 손익: {pnl:+,.0f}원"
        )

        closed.append({
            "code": trade.code, "name": trade.name,
            "pnl": round(pnl), "pnl_pct": round(pnl_pct, 2),
        })
        logger.info(f"기간 만료 청산: {trade.code} {trade.name} {pnl_pct:+.2f}%")

    await db.commit()
    return closed


# ── 통합 필터 ─────────────────────────────────────────────────────────────────

async def filter_signals(db: AsyncSession, signals: list[dict]) -> list[dict]:
    """
    신호 목록에 모든 필터를 적용해 유효한 신호만 반환합니다.
    """
    if not signals:
        return []

    # 1. 시장 상황 필터
    if not await is_market_bullish():
        logger.info("시장 하락 추세 — 전체 신호 필터링")
        await send_message(
            "⚠️ <b>[AI INVEST] 시장 필터 작동</b>\n"
            f"코스피 하락 추세 감지 — 오늘 매수 중단"
        )
        return []

    # 2. 쿨타임 + 기타 필터
    filtered = []
    for sig in signals:
        code = sig["code"]

        # 쿨타임 확인
        if await is_in_cooltime(db, code):
            logger.info(f"[{code}] 쿨타임 — 건너뜀")
            continue

        filtered.append(sig)

    logger.info(f"필터 적용: {len(signals)}개 → {len(filtered)}개")
    return filtered
