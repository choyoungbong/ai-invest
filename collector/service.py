"""
Collector – 한국 주식 시세 수집기

수집 전략:
  - 풀 실행 시: 당일 데이터만 추가 수집 (1~2분)
  - DB에 누적 보존 (삭제 없이 upsert)
  - 최초 실행 또는 누락 시: COLLECT_DAYS(기본 60)일치 소급 수집
  - 전략 계산에 충분한 누적 데이터 확보
"""
import asyncio
import logging
import os
from datetime import datetime, date, timedelta
from typing import List, Dict

import FinanceDataReader as fdr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.models import Stock, MarketData

logger = logging.getLogger(__name__)

COLLECT_LIMIT = int(os.getenv("COLLECT_LIMIT", "0"))   # 0=전체, N=N개 제한
COLLECT_DAYS  = int(os.getenv("COLLECT_DAYS",  "60"))  # 소급 수집 일수


def _is_market_hours() -> bool:
    import pytz
    from datetime import time
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    if now.weekday() >= 5:
        return False
    return time(9, 0) <= now.time() <= time(15, 35)


# ── 종목 마스터 동기화 ──────────────────────────────────────────────────────────

async def sync_stock_master(db: AsyncSession):
    """KOSPI + KOSDAQ 종목 마스터를 DB에 저장합니다."""
    records = []
    for market, fdr_key in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
        try:
            df = fdr.StockListing(fdr_key)
            if df is None or df.empty:
                continue
            code_col = next((c for c in df.columns if c in ["Code", "Symbol", "종목코드", "ISU_SRT_CD"]), None)
            name_col = next((c for c in df.columns if c in ["Name", "종목명", "ISU_ABBRV"]), None)
            if not code_col:
                continue
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                name = str(row[name_col]).strip() if name_col else code
                if len(code) == 6 and code.isdigit():
                    records.append({"code": code, "name": name, "market": market})
        except Exception as e:
            logger.error(f"종목 마스터 오류 [{market}]: {e}")

    if not records:
        return
    stmt = pg_insert(Stock).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={"name": stmt.excluded.name, "market": stmt.excluded.market},
    )
    await db.execute(stmt)
    await db.commit()
    logger.info(f"종목 마스터 동기화 완료: {len(records)}개")


# ── 당일 시세 수집 ─────────────────────────────────────────────────────────────

async def collect_daily_ohlcv(db: AsyncSession, target_date: str | None = None):
    """
    OHLCV 수집 — 당일 데이터만 추가 (누락 시 소급 수집)

    핵심 로직:
      1. DB에서 가장 최신 시세 날짜 확인
      2. 최신 날짜 다음날부터 target_date까지만 수집
      3. DB가 비어있으면 COLLECT_DAYS일치 소급 수집
    """
    # 목표 날짜 결정
    if target_date:
        td_date = datetime.strptime(target_date, "%Y%m%d").date()
    else:
        td_date = date.today()
        while td_date.weekday() >= 5:
            td_date -= timedelta(days=1)

    td = td_date.strftime("%Y%m%d")
    end_str = td_date.strftime("%Y-%m-%d")

    # DB 최신 날짜 확인
    from sqlalchemy import func
    latest_ts = (await db.execute(
        select(func.max(MarketData.timestamp))
    )).scalar()

    if latest_ts is None:
        # DB 비어있음 → 전체 소급 수집
        start_date = td_date - timedelta(days=COLLECT_DAYS)
        logger.info(f"DB 비어있음 → {COLLECT_DAYS}일치 소급 수집 시작")
    else:
        latest_date = latest_ts.date()
        if latest_date >= td_date:
            # 이미 오늘 데이터 있음
            logger.info(f"시세 최신 상태 ({latest_date}) — 수집 건너뜀")
            today_rows = (await db.execute(
                select(MarketData).where(
                    MarketData.timestamp == datetime.strptime(td, "%Y%m%d")
                )
            )).scalars().all()
            return today_rows
        else:
            # 다음날부터 오늘까지만 수집
            start_date = latest_date + timedelta(days=1)
            gap_days = (td_date - latest_date).days
            logger.info(f"누락 {gap_days}일 수집: {start_date} ~ {td_date}")

    start_str = start_date.strftime("%Y-%m-%d")

    # 종목 목록 조회
    stock_rows = (await db.execute(select(Stock.code, Stock.name))).all()
    if not stock_rows:
        logger.warning("종목 마스터 없음")
        return []

    if COLLECT_LIMIT > 0:
        stock_rows = stock_rows[:COLLECT_LIMIT]

    logger.info(f"시세 수집 시작: {start_str}~{end_str} ({len(stock_rows)}개 종목)")

    rows: List[Dict] = []
    errors = 0

    for i, (code, name) in enumerate(stock_rows):
        try:
            df = fdr.DataReader(code, start_str, end_str)
            if df is None or df.empty:
                continue
            for ts, row in df.iterrows():
                close = float(row.get("Close", 0) or 0)
                if close <= 0:
                    continue
                volume = int(row.get("Volume", 0) or 0)
                rows.append({
                    "code":          code,
                    "open":          float(row.get("Open",   0) or 0),
                    "high":          float(row.get("High",   0) or 0),
                    "low":           float(row.get("Low",    0) or 0),
                    "close":         close,
                    "volume":        volume,
                    "trading_value": int(volume * close),
                    "change_rate":   float(row.get("Change", 0) or 0) * 100,
                    "timestamp":     ts.to_pydatetime().replace(tzinfo=None),
                })
        except Exception as e:
            errors += 1
            logger.debug(f"[{code}] 수집 오류: {e}")
            continue

        # 5000건마다 중간 저장
        if len(rows) >= 5000:
            await _upsert_rows(db, rows)
            logger.info(f"  중간 저장: {i+1}/{len(stock_rows)} 종목 처리 중...")
            rows = []

    if rows:
        await _upsert_rows(db, rows)

    # 당일 데이터 카운트
    td_dt = datetime.strptime(td, "%Y%m%d")
    today_count = (await db.execute(
        select(func.count(MarketData.id)).where(MarketData.timestamp == td_dt)
    )).scalar() or 0

    logger.info(f"시세 수집 완료: {td} — 오늘 {today_count}개 종목 (오류 {errors}개)")

    today_rows = (await db.execute(
        select(MarketData).where(MarketData.timestamp == td_dt)
    )).scalars().all()
    return today_rows


async def _upsert_rows(db: AsyncSession, rows: List[Dict]):
    """중복 없이 시세 데이터 저장"""
    if not rows:
        return
    batch_size = 1000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        stmt = pg_insert(MarketData).values(batch)
        stmt = stmt.on_conflict_do_nothing()
        await db.execute(stmt)
    await db.commit()


# ── 분봉 수집 (장중 갱신용) ────────────────────────────────────────────────────

async def collect_intraday_snapshot(db: AsyncSession):
    rows = await collect_daily_ohlcv(db)
    count = len(rows) if rows else 0
    logger.info(f"장중 스냅샷: {count}건")
    return rows


# ── 백그라운드 서비스 (비활성화 권장) ─────────────────────────────────────────

class CollectorService:
    def __init__(self, db_factory):
        self.db_factory = db_factory
        self._task: asyncio.Task | None = None
        self.running = False

    async def start(self):
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Collector 서비스 시작")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
        logger.info("Collector 서비스 중지")

    async def _loop(self):
        while self.running:
            try:
                if _is_market_hours():
                    async with self.db_factory() as db:
                        await collect_intraday_snapshot(db)
                else:
                    logger.debug("[Collector] 장 외 시간 — 수집 건너뜀")
            except Exception as e:
                logger.error(f"Collector 루프 오류: {repr(e)}")
            await asyncio.sleep(300)
