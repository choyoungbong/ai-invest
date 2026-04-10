"""
Collector – 한국 주식 시세 수집기
FinanceDataReader 를 사용해 KRX 데이터를 수집합니다.

[근본 수정]
  기존 배치 수집 방식(fdr.DataReader("KOSPI"))은 지수 1행만 반환 → 종목 데이터 없음
  → 종목별 개별 수집으로 복원 + 전략에 필요한 누적 데이터(60일) 유지 방식으로 변경

수집 전략:
  - 스케줄러 풀 실행 시: 당일 신규 데이터만 추가 (중복 없이)
  - 종목별 fdr.DataReader(code, start, end) 사용
  - DB에 누적 저장 (삭제 없이 upsert) → 전략 계산에 충분한 데이터 확보
  - COLLECT_LIMIT으로 수집 종목 수 제한 가능
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

COLLECT_LIMIT = int(os.getenv("COLLECT_LIMIT", "0"))  # 0=전체, N=N개 제한
COLLECT_DAYS  = int(os.getenv("COLLECT_DAYS",  "60")) # 수집할 과거 일수


def _is_market_hours() -> bool:
    """현재 장중 여부 확인 (09:00 ~ 15:35 KST)"""
    import pytz
    from datetime import time
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    if now.weekday() >= 5:
        return False
    return time(9, 0) <= now.time() <= time(15, 35)


def _get_date_range(target_date: str | None = None) -> tuple[str, str, str]:
    """
    수집 기간 계산.
    Returns: (td_str, start_str, end_str)
      td_str    : 당일 YYYYMMDD (DB timestamp용)
      start_str : 시작일 YYYY-MM-DD (60일 전)
      end_str   : 종료일 YYYY-MM-DD (당일)
    """
    if target_date:
        td_date = datetime.strptime(target_date, "%Y%m%d").date()
    else:
        td_date = date.today()
        while td_date.weekday() >= 5:
            td_date -= timedelta(days=1)

    end_date   = td_date
    start_date = end_date - timedelta(days=COLLECT_DAYS)

    return (
        td_date.strftime("%Y%m%d"),
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    )


# ── 종목 마스터 동기화 ──────────────────────────────────────────────────────────

async def sync_stock_master(db: AsyncSession):
    """KOSPI + KOSDAQ 종목 마스터를 DB에 저장합니다."""
    records = []

    for market, fdr_key in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
        try:
            df = fdr.StockListing(fdr_key)
            if df is None or df.empty:
                logger.warning(f"[{market}] 종목 목록 없음")
                continue

            code_col = next((c for c in df.columns if c in ["Code", "Symbol", "종목코드", "ISU_SRT_CD"]), None)
            name_col = next((c for c in df.columns if c in ["Name", "종목명", "ISU_ABBRV"]), None)

            if not code_col:
                logger.error(f"[{market}] 종목코드 컬럼 없음: {list(df.columns)}")
                continue

            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                name = str(row[name_col]).strip() if name_col else code
                if len(code) == 6 and code.isdigit():
                    records.append({"code": code, "name": name, "market": market})

        except Exception as e:
            logger.error(f"종목 마스터 오류 [{market}]: {e}")

    if not records:
        logger.warning("종목 마스터 데이터 없음")
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
    당일 OHLCV를 수집해 DB에 추가합니다.

    [수정] 배치 수집 → 종목별 개별 수집으로 복원
      - fdr.DataReader(code, start, end)로 60일치 데이터 수집
      - 이미 있는 날짜는 upsert(중복 무시)로 처리
      - 전략 계산에 필요한 누적 데이터 확보
    """
    td, start_str, end_str = _get_date_range(target_date)
    td_date = datetime.strptime(td, "%Y%m%d")

    # DB에서 종목 목록 조회
    stock_rows = (await db.execute(
        select(Stock.code, Stock.name)
    )).all()

    if not stock_rows:
        logger.warning("종목 마스터 없음 — sync_stock_master 먼저 실행하세요")
        return []

    # COLLECT_LIMIT 적용
    if COLLECT_LIMIT > 0:
        stock_rows = stock_rows[:COLLECT_LIMIT]
        logger.info(f"COLLECT_LIMIT={COLLECT_LIMIT} 적용")

    logger.info(f"시세 수집 시작: {td} ({len(stock_rows)}개 종목, {start_str}~{end_str})")

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

        # 100개마다 중간 저장 (메모리 관리)
        if len(rows) >= 5000:
            await _upsert_rows(db, rows)
            logger.info(f"  중간 저장: {i+1}/{len(stock_rows)} 종목 처리 중...")
            rows = []

    # 나머지 저장
    if rows:
        await _upsert_rows(db, rows)

    # 당일 데이터로 스냅샷 카운트
    from sqlalchemy import func, and_
    today_count = (await db.execute(
        select(func.count(MarketData.id)).where(
            MarketData.timestamp == td_date
        )
    )).scalar() or 0

    logger.info(f"시세 수집 완료: {td} — 오늘 {today_count}개 종목 (오류 {errors}개)")

    # 당일 데이터 반환
    today_rows = (await db.execute(
        select(MarketData).where(MarketData.timestamp == td_date)
    )).scalars().all()

    return today_rows


async def _upsert_rows(db: AsyncSession, rows: List[Dict]):
    """중복 없이 시세 데이터 저장 (code + timestamp 기준)"""
    if not rows:
        return
    batch_size = 1000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        stmt = pg_insert(MarketData).values(batch)
        stmt = stmt.on_conflict_do_nothing()  # 중복 무시
        await db.execute(stmt)
    await db.commit()


# ── 분봉 수집 (장중 갱신용) ────────────────────────────────────────────────────

async def collect_intraday_snapshot(db: AsyncSession):
    rows = await collect_daily_ohlcv(db)
    count = len(rows) if rows else 0
    logger.info(f"장중 스냅샷: {count}건")
    return rows


# ── 백그라운드 서비스 (비활성화 권장) ─────────────────────────────────────────
# 스케줄러(scheduler/service.py)가 수집을 담당합니다.
# main.py에서 CollectorService.start()를 호출하지 마세요.

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
