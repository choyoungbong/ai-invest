"""
Collector – 한국 주식 시세 수집기
FinanceDataReader 를 사용해 KRX 데이터를 수집합니다.
pykrx 대비 Docker 환경에서 안정적으로 동작합니다.
"""
import asyncio
import logging
from datetime import datetime, date, timedelta
from typing import List, Dict

import FinanceDataReader as fdr
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.models import Stock, MarketData

logger = logging.getLogger(__name__)


def _prev_trading_day(d: date) -> str:
    """가장 최근 평일(영업일 추정)을 반환합니다."""
    while d.weekday() >= 5:  # 토(5), 일(6)
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


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

            logger.info(f"[{market}] 컬럼: {list(df.columns)}")

            # 컬럼명 유연하게 처리
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
    지정일(또는 당일) OHLCV + 거래대금을 수집해 DB에 저장합니다.
    FinanceDataReader로 KOSPI/KOSDAQ 전 종목을 수집합니다.
    """
    if target_date:
        td = target_date  # YYYYMMDD
        td_date = datetime.strptime(td, "%Y%m%d").date()
    else:
        td_date = date.today()
        # 주말이면 가장 최근 평일로
        while td_date.weekday() >= 5:
            td_date -= timedelta(days=1)
        td = td_date.strftime("%Y%m%d")

    date_str = td_date.strftime("%Y-%m-%d")
    rows: List[Dict] = []

    for market, fdr_key in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
        try:
            # 전 종목 시세 (해당일 ~ 해당일)
            df = fdr.DataReader(fdr_key, date_str, date_str)

            if df is None or df.empty:
                logger.warning(f"[{market}] {td} 데이터 없음")
                continue

            logger.info(f"[{market}] 컬럼: {list(df.columns)}, 행수: {len(df)}")

        except Exception as e:
            logger.error(f"[{market}] 지수 조회 오류: {e}")
            continue

        # 개별 종목 수집 — 마스터에서 코드 목록 가져오기
        from sqlalchemy import select
        stmt = select(Stock.code, Stock.name).where(Stock.market == market)
        stock_rows = (await db.execute(stmt)).all()

        if not stock_rows:
            logger.warning(f"[{market}] 종목 마스터 없음 — sync-master 먼저 실행하세요")
            continue

        logger.info(f"[{market}] {len(stock_rows)}개 종목 시세 수집 시작")

        batch = []
        for code, name in stock_rows:
            try:
                sdf = fdr.DataReader(code, date_str, date_str)
                if sdf is None or sdf.empty:
                    continue

                row = sdf.iloc[-1]
                close = float(row.get("Close", 0) or 0)
                if close <= 0:
                    continue

                batch.append({
                    "code":          code,
                    "open":          float(row.get("Open",   0) or 0),
                    "high":          float(row.get("High",   0) or 0),
                    "low":           float(row.get("Low",    0) or 0),
                    "close":         close,
                    "volume":        int(row.get("Volume",   0) or 0),
                    "trading_value": int(row.get("Volume",   0) * close),
                    "change_rate":   float(row.get("Change",  0) or 0) * 100,
                    "timestamp":     datetime.strptime(td, "%Y%m%d"),
                })
            except Exception as e:
                logger.debug(f"[{code}] 시세 오류: {e}")
                continue

        rows.extend(batch)
        logger.info(f"[{market}] {len(batch)}개 수집 완료")

    if not rows:
        logger.warning(f"{td} 최종 시세 데이터 없음")
        return []

    # 중복 방지: 같은 날짜 삭제 후 재삽입
    ts = datetime.strptime(td, "%Y%m%d")
    await db.execute(MarketData.__table__.delete().where(MarketData.timestamp == ts))
    await db.execute(MarketData.__table__.insert(), rows)
    await db.commit()
    logger.info(f"시세 수집 완료: {td} – {len(rows)}개 종목")
    return rows


# ── 빠른 시세 수집 (거래대금 상위 종목만) ────────────────────────────────────────

async def collect_top_stocks_ohlcv(db: AsyncSession, top_n: int = 100):
    """
    KOSPI200 구성 종목 기준으로 빠르게 시세를 수집합니다.
    전 종목 수집이 오래 걸릴 때 사용합니다.
    """
    today = date.today()
    while today.weekday() >= 5:
        today -= timedelta(days=1)
    td = today.strftime("%Y%m%d")
    date_str = today.strftime("%Y-%m-%d")

    try:
        # KOSPI200 구성 종목
        df = fdr.StockListing("KRX")
        if df is None or df.empty:
            return []

        code_col = next((c for c in df.columns if c in ["Code", "Symbol"]), None)
        if not code_col:
            return []

        codes = [str(r[code_col]).zfill(6) for _, r in df.iterrows()][:top_n]
    except Exception as e:
        logger.error(f"종목 목록 오류: {e}")
        return []

    rows = []
    for code in codes:
        try:
            sdf = fdr.DataReader(code, date_str, date_str)
            if sdf is None or sdf.empty:
                continue
            row   = sdf.iloc[-1]
            close = float(row.get("Close", 0) or 0)
            if close <= 0:
                continue
            rows.append({
                "code":          code,
                "open":          float(row.get("Open",   0) or 0),
                "high":          float(row.get("High",   0) or 0),
                "low":           float(row.get("Low",    0) or 0),
                "close":         close,
                "volume":        int(row.get("Volume",   0) or 0),
                "trading_value": int(row.get("Volume",   0) * close),
                "change_rate":   float(row.get("Change",  0) or 0) * 100,
                "timestamp":     datetime.strptime(td, "%Y%m%d"),
            })
        except Exception:
            continue

    if rows:
        ts = datetime.strptime(td, "%Y%m%d")
        await db.execute(MarketData.__table__.delete().where(MarketData.timestamp == ts))
        await db.execute(MarketData.__table__.insert(), rows)
        await db.commit()
        logger.info(f"빠른 수집 완료: {td} – {len(rows)}개")
    return rows


# ── 분봉 수집 (장중 갱신용) ────────────────────────────────────────────────────

async def collect_intraday_snapshot(db: AsyncSession):
    rows = await collect_daily_ohlcv(db)
    logger.info(f"장중 스냅샷: {len(rows) if rows else 0}건")
    return rows


# ── 백그라운드 서비스 ─────────────────────────────────────────────────────────

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
                async with self.db_factory() as db:
                    await collect_intraday_snapshot(db)
            except Exception as e:
                logger.error(f"Collector 루프 오류: {e}")
            await asyncio.sleep(300)
