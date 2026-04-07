"""
Collector – 한국 주식 시세 수집기
FinanceDataReader 를 사용해 KRX 데이터를 수집합니다.
pykrx 대비 Docker 환경에서 안정적으로 동작합니다.

개선사항:
  - 종목별 개별 HTTP 요청 → 시장 전체 일괄 조회 (9분 → 1분 이내)
  - CollectorService 백그라운드 루프 장중에만 실행 (스케줄러와 이중 수집 방지)
  - COLLECT_LIMIT 환경변수 지원 (테스트용 수집 제한)
"""
import asyncio
import logging
import os
from datetime import datetime, date, timedelta
from typing import List, Dict

import FinanceDataReader as fdr
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.models import Stock, MarketData

logger = logging.getLogger(__name__)

COLLECT_LIMIT = int(os.getenv("COLLECT_LIMIT", "0"))  # 0=전체, N=N개 제한


def _prev_trading_day(d: date) -> str:
    """가장 최근 평일(영업일 추정)을 반환합니다."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _is_market_hours() -> bool:
    """현재 장중 여부 확인 (09:00 ~ 15:35 KST)"""
    import pytz
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time
    return time(9, 0) <= t <= time(15, 35)


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

    [개선] 종목별 개별 HTTP 요청 → 시장 전체 일괄 조회
      기존: KOSPI 926개 × HTTP 1회 + KOSDAQ 1784개 × HTTP 1회 = 2710회 요청 (9분)
      개선: fdr.DataReader("KOSPI") 1회 + fdr.DataReader("KOSDAQ") 1회 = 2회 요청 (1분)
    """
    if target_date:
        td = target_date
        td_date = datetime.strptime(td, "%Y%m%d").date()
    # 수정 — 장 시작 직후(09:05~09:30) 오늘 데이터 없으면 전날 사용
    else:
        import pytz
        kst = pytz.timezone("Asia/Seoul")
        now_kst = datetime.now(kst)
        td_date = now_kst.date()
        while td_date.weekday() >= 5:
            td_date -= timedelta(days=1)
        td = td_date.strftime("%Y%m%d")

    date_str = td_date.strftime("%Y-%m-%d")
    rows: List[Dict] = []

    for market, fdr_key in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
        try:
            # ── 시장 전체 일괄 조회 (기존 개별 조회 대체) ──────────────────
            df = fdr.DataReader(fdr_key, date_str, date_str)

            if df is None or df.empty:
                logger.warning(f"[{market}] {td} 데이터 없음")
                continue

            logger.info(f"[{market}] 컬럼: {list(df.columns)}, 행수: {len(df)}")

            # COLLECT_LIMIT 적용 (테스트용)
            if COLLECT_LIMIT > 0:
                df = df.head(COLLECT_LIMIT)
                logger.info(f"[{market}] COLLECT_LIMIT={COLLECT_LIMIT} 적용")

            logger.info(f"[{market}] {len(df)}개 종목 일괄 수집 시작")

            batch = []
            for code, row in df.iterrows():
                try:
                    code_str = str(code).zfill(6)
                    # 6자리 숫자가 아니면 건너뜀
                    if not code_str.isdigit() or len(code_str) != 6:
                        continue
                      
                    close = float(row.get("Close", 0) or 0)
                    if close <= 0:
                        continue

                    volume = int(row.get("Volume", 0) or 0)
                    batch.append({
                        "code":          code_str,
                        "open":          float(row.get("Open",   0) or 0),
                        "high":          float(row.get("High",   0) or 0),
                        "low":           float(row.get("Low",    0) or 0),
                        "close":         close,
                        "volume":        volume,
                        "trading_value": int(volume * close),
                        "change_rate":   float(row.get("Change",  0) or 0) * 100,
                        "timestamp":     datetime.strptime(td, "%Y%m%d"),
                    })
                except Exception as e:
                    logger.debug(f"[{code}] 행 처리 오류: {e}")
                    continue

            rows.extend(batch)
            logger.info(f"[{market}] {len(batch)}개 수집 완료")

        except Exception as e:
            logger.error(f"[{market}] 수집 오류: {repr(e)}")
            continue

    # 수정 — 오늘 데이터 없으면 전날 DB 데이터로 대체
    if not rows:
        logger.warning(f"{td} 최종 시세 데이터 없음 — 전날 데이터 사용")
        from sqlalchemy import select, func
        prev_ts = (await db.execute(
            select(func.max(MarketData.timestamp)).where(
                MarketData.timestamp < datetime.strptime(td, "%Y%m%d")
            )
        )).scalar()
        if prev_ts:
            logger.info(f"전날 데이터 사용: {prev_ts.date()}")
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
# [개선] 장중에만 실행 + 스케줄러와 이중 수집 방지를 위해 기본 비활성화
# main.py에서 CollectorService를 시작하지 않는 것을 권장합니다.
# 스케줄러(scheduler/service.py)가 하루 6회 수집을 담당합니다.

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
        """
        [개선] 장중에만 실행되도록 변경.
        스케줄러가 이미 6회 수집하므로 이 서비스는 main.py에서
        시작하지 않는 것을 권장합니다.
        """
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
