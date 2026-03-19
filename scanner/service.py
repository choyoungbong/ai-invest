"""
Scanner – 거래대금 상위 종목 스캐너
DB에 저장된 최신 시세를 바탕으로 거래대금 상위 N개 종목을 추출합니다.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_

from api.models import MarketData, ScanResult, Stock

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 30
MIN_TRADING_VALUE = 5_000_000_000   # 거래대금 최소 50억
MIN_CLOSE_PRICE = 1_000              # 동전주 제외 (1,000원 이상)


# ── 거래대금 상위 조회 ─────────────────────────────────────────────────────────

async def get_top_volume_stocks(
    db: AsyncSession,
    top_n: int = DEFAULT_TOP_N,
    min_value: int = MIN_TRADING_VALUE,
) -> List[dict]:
    """
    가장 최근 타임스탬프 기준 거래대금 상위 종목 목록을 반환합니다.
    """
    # 최신 타임스탬프
    latest_ts_q = select(func.max(MarketData.timestamp))
    latest_ts = (await db.execute(latest_ts_q)).scalar()

    if not latest_ts:
        logger.warning("시세 데이터 없음")
        return []

    stmt = (
        select(
            MarketData.code,
            Stock.name,
            MarketData.close,
            MarketData.volume,
            MarketData.trading_value,
            MarketData.change_rate,
            MarketData.timestamp,
        )
        .join(Stock, Stock.code == MarketData.code, isouter=True)
        .where(
            and_(
                MarketData.timestamp == latest_ts,
                MarketData.trading_value >= min_value,
                MarketData.close >= MIN_CLOSE_PRICE,
            )
        )
        .order_by(desc(MarketData.trading_value))
        .limit(top_n)
    )

    rows = (await db.execute(stmt)).all()

    result = []
    for i, row in enumerate(rows, start=1):
        result.append({
            "rank":          i,
            "code":          row.code,
            "name":          row.name or row.code,
            "close":         row.close,
            "volume":        row.volume,
            "trading_value": row.trading_value,
            "change_rate":   row.change_rate,
            "timestamp":     row.timestamp.isoformat() if row.timestamp else None,
        })

    return result


# ── 스캔 결과 저장 ──────────────────────────────────────────────────────────────

async def save_scan_result(db: AsyncSession, items: List[dict]):
    """스캔 결과를 DB에 기록합니다."""
    if not items:
        return

    now = datetime.utcnow()
    rows = [
        {
            "scan_time":     now,
            "code":          it["code"],
            "name":          it.get("name"),
            "rank":          it["rank"],
            "close":         it.get("close"),
            "volume":        it.get("volume"),
            "trading_value": it.get("trading_value"),
            "change_rate":   it.get("change_rate"),
        }
        for it in items
    ]
    await db.execute(ScanResult.__table__.insert(), rows)
    await db.commit()
    logger.info(f"스캔 결과 저장: {len(rows)}건 (scan_time={now})")


# ── 통합 실행 ──────────────────────────────────────────────────────────────────

async def run_scanner(db: AsyncSession, top_n: int = DEFAULT_TOP_N) -> List[dict]:
    """스캔 실행 후 결과를 반환합니다."""
    items = await get_top_volume_stocks(db, top_n=top_n)
    await save_scan_result(db, items)
    logger.info(f"스캔 완료: 상위 {len(items)}개 종목")
    return items
