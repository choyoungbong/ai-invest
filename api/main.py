"""
AI INVEST – FastAPI 메인 앱
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db, init_db, AsyncSessionLocal
from api.models import Signal, Trade

from collector.service import (
    CollectorService,
    sync_stock_master,
    collect_daily_ohlcv,
)
from scanner.service import run_scanner, get_top_volume_stocks
from strategy.service import run_strategy, get_signals
from notification.service import (
    notify_test,
    notify_signal,
    notify_signals_summary,
    notify_trade,
)
from ai.service import analyze_signal, analyze_all_new_signals
from trader.service import execute_order, check_stop_loss
from trader.kis_client import get_balance, get_current_price, IS_MOCK
from scheduler.service import create_scheduler
from backtest.service import run_backtest, run_multi_backtest
from strategy.extended import run_extended_strategy
from api.monitor import ErrorMonitorMiddleware, run_health_check_and_notify, get_recent_errors
from trader.allocation import get_allocation_summary, calc_quantity_by_budget
from trader.auto_stoploss import check_and_execute_stop_loss
from kis_verify.router import router as kis_verify_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 백그라운드 Collector ───────────────────────────────────────────────────────
collector_service = CollectorService(db_factory=AsyncSessionLocal)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI INVEST 서버 시작 중...")
    await init_db()
    logger.info("DB 초기화 완료")
    await collector_service.start()

    scheduler = create_scheduler()
    scheduler.start()
    logger.info("스케줄러 시작 완료")

    yield

    scheduler.shutdown(wait=False)
    await collector_service.stop()
    logger.info("AI INVEST 서버 종료")


# ── FastAPI 앱 ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AI INVEST",
    description="AI 기반 한국 주식 자동매매 시스템",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ErrorMonitorMiddleware)
app.include_router(kis_verify_router)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "service": "ai-invest"}


# ── Collector 엔드포인트 ───────────────────────────────────────────────────────
@app.post("/collector/sync-master", tags=["Collector"])
async def sync_master(db: AsyncSession = Depends(get_db)):
    """종목 마스터 동기화 (KOSPI + KOSDAQ)"""
    await sync_stock_master(db)
    return {"message": "종목 마스터 동기화 완료"}


@app.post("/collector/collect", tags=["Collector"])
async def collect_today(
    date: str | None = Query(None, description="YYYYMMDD, 미입력시 오늘"),
    db: AsyncSession = Depends(get_db),
):
    """당일(또는 지정일) 시세 수집"""
    rows = await collect_daily_ohlcv(db, target_date=date)
    return {"message": f"시세 수집 완료: {len(rows)}건"}


# ── Scanner 엔드포인트 ─────────────────────────────────────────────────────────
@app.get("/scanner/top-volume", tags=["Scanner"])
async def top_volume(
    top_n: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """거래대금 상위 종목 조회"""
    items = await get_top_volume_stocks(db, top_n=top_n)
    return {"count": len(items), "data": items}


@app.post("/scanner/run", tags=["Scanner"])
async def run_scan(
    top_n: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """스캐너 실행 후 결과 저장"""
    items = await run_scanner(db, top_n=top_n)
    return {"message": "스캔 완료", "count": len(items), "data": items}


# ── Strategy 엔드포인트 ────────────────────────────────────────────────────────
@app.post("/strategy/run", tags=["Strategy"])
async def strategy_run(
    top_n: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    스캔 → 전략 일괄 실행.
    거래대금 상위 종목을 스캔하고 돌파 조건을 적용합니다.
    """
    candidates = await run_scanner(db, top_n=top_n)
    signals = await run_strategy(db, candidates)

    # 신호 발생 시 AI 분석 + 텔레그램 알림
    if signals:
        await analyze_all_new_signals(db)

    # 텔레그램 알림
    await notify_signals_summary(signals)

    return {
        "message": "전략 실행 완료",
        "candidates": len(candidates),
        "signals": len(signals),
        "data": signals,
    }


@app.get("/signals", tags=["Strategy"])
async def list_signals(
    limit: int = Query(50, ge=1, le=200),
    signal_type: str | None = Query(None, description="BUY 또는 SELL"),
    db: AsyncSession = Depends(get_db),
):
    """신호 목록 조회"""
    data = await get_signals(db, limit=limit, signal_type=signal_type)
    return {"count": len(data), "data": data}


@app.get("/signals/{signal_id}", tags=["Strategy"])
async def get_signal_detail(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
):
    """신호 상세 조회"""
    from sqlalchemy import select
    stmt = select(Signal).where(Signal.id == signal_id)
    row = (await db.execute(stmt)).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="신호를 찾을 수 없습니다")
    return {
        "id":           row.id,
        "code":         row.code,
        "name":         row.name,
        "signal_type":  row.signal_type,
        "strategy":     row.strategy,
        "price":        row.price,
        "target_price": row.target_price,
        "stop_loss":    row.stop_loss,
        "reason":       row.reason,
        "confidence":   row.confidence,
        "is_executed":  row.is_executed,
        "created_at":   row.created_at.isoformat() if row.created_at else None,
    }


# ── 자동 손절 엔드포인트 (G) ─────────────────────────────────────────────────
@app.post("/trade/auto-stoploss", tags=["Trade"])
async def auto_stoploss(db: AsyncSession = Depends(get_db)):
    """체결된 매수 포지션 중 손절가 도달 종목 자동 매도"""
    executed = await check_and_execute_stop_loss(db)
    return {"message": f"자동 손절 실행: {len(executed)}건", "data": executed}


# ── 모니터링 엔드포인트 (H) ───────────────────────────────────────────────────
@app.get("/monitor/health", tags=["Monitor"])
async def full_health_check():
    """DB / Redis / KIS API 헬스체크 (이상 시 텔레그램 알림)"""
    import os
    result = await run_health_check_and_notify(
        AsyncSessionLocal,
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
    )
    return result


@app.get("/monitor/errors", tags=["Monitor"])
async def recent_errors():
    """최근 서버 에러 목록 조회 (최대 50건)"""
    return {"errors": get_recent_errors()}


# ── 자금 배분 엔드포인트 (I) ──────────────────────────────────────────────────
@app.get("/allocation", tags=["Allocation"])
async def get_allocation():
    """전략별 자금 배분 현황 조회"""
    return get_allocation_summary()


@app.get("/allocation/calc", tags=["Allocation"])
async def calc_order_size(
    strategy:   str   = Query("breakout"),
    price:      float = Query(..., description="현재가"),
    confidence: float = Query(0.5, ge=0.0, le=1.0),
):
    """전략/신뢰도/현재가 기반 적정 주문 수량 계산"""
    from trader.allocation import get_order_amount
    qty    = calc_quantity_by_budget(strategy, price, confidence)
    amount = get_order_amount(strategy, confidence)
    return {
        "strategy":   strategy,
        "price":      price,
        "confidence": confidence,
        "quantity":   qty,
        "amount":     amount,
        "total_cost": qty * price,
    }


# ── Backtest 엔드포인트 ───────────────────────────────────────────────────────
@app.get("/backtest", tags=["Backtest"])
async def backtest_single(
    code:       str = Query(..., description="종목코드 (예: 005930)"),
    strategy:   str = Query("breakout", description="breakout / ma_cross / rsi_reversal"),
    start_date: str = Query(..., description="시작일 YYYY-MM-DD"),
    end_date:   str = Query(..., description="종료일 YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    """단일 종목 백테스트"""
    result = await run_backtest(db, code, strategy, start_date, end_date)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/backtest/multi", tags=["Backtest"])
async def backtest_multi(
    codes:      list[str] = Query(..., description="종목코드 목록"),
    strategy:   str       = Query("breakout"),
    start_date: str       = Query(...),
    end_date:   str       = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """여러 종목 일괄 백테스트"""
    return await run_multi_backtest(db, codes, strategy, start_date, end_date)


# ── Extended Strategy 엔드포인트 ──────────────────────────────────────────────
@app.post("/strategy/extended", tags=["Strategy"])
async def strategy_extended(
    top_n:      int       = Query(30),
    strategies: list[str] = Query(["ma_cross", "rsi_reversal", "macd"],
                                   description="사용할 전략 목록"),
    db: AsyncSession = Depends(get_db),
):
    """
    확장 전략 실행 (MA크로스 / RSI반등 / MACD).
    스캐너 상위 종목에 대해 지정한 전략들을 적용합니다.
    """
    candidates = await run_scanner(db, top_n=top_n)
    signals    = await run_extended_strategy(db, candidates, strategies)
    await notify_signals_summary(signals)
    return {
        "message":    "확장 전략 실행 완료",
        "candidates": len(candidates),
        "signals":    len(signals),
        "data":       signals,
    }


# ── Scheduler 엔드포인트 ──────────────────────────────────────────────────────
@app.get("/scheduler/jobs", tags=["Scheduler"])
async def list_jobs():
    """등록된 스케줄 목록 조회"""
    from scheduler.service import create_scheduler
    sch = create_scheduler()
    return {
        "jobs": [
            {
                "id":       j.id,
                "name":     j.name,
                "next_run": str(j.next_run_time) if hasattr(j, "next_run_time") else None,
            }
            for j in sch.get_jobs()
        ]
    }


@app.post("/scheduler/run-now", tags=["Scheduler"])
async def run_now(db: AsyncSession = Depends(get_db)):
    """스케줄 즉시 수동 실행 (테스트용)"""
    from scheduler.service import job_collect_and_run
    await job_collect_and_run()
    return {"message": "수동 실행 완료"}


# ── AI Analysis 엔드포인트 ────────────────────────────────────────────────────
@app.post("/ai/analyze/{signal_id}", tags=["AI Analysis"])
async def ai_analyze_signal(signal_id: str, db: AsyncSession = Depends(get_db)):
    """
    특정 신호에 대해 Claude AI 분석을 실행합니다.
    분석 결과는 Signal.reason 필드에 저장됩니다.
    """
    result = await analyze_signal(db, signal_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/ai/analyze-all", tags=["AI Analysis"])
async def ai_analyze_all(db: AsyncSession = Depends(get_db)):
    """최근 24시간 신호 중 AI 분석이 없는 것을 일괄 분석합니다."""
    results = await analyze_all_new_signals(db)
    return {"message": f"AI 분석 완료: {len(results)}건", "data": results}


# ── Notification 엔드포인트 ───────────────────────────────────────────────────
@app.post("/notification/test", tags=["Notification"])
async def notification_test():
    """텔레그램 연결 테스트"""
    ok = await notify_test()
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="전송 실패 — .env 파일의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 확인하세요",
        )
    return {"message": "텔레그램 테스트 메시지 전송 완료"}


@app.post("/notification/signal/{signal_id}", tags=["Notification"])
async def notify_signal_by_id(signal_id: str, db: AsyncSession = Depends(get_db)):
    """특정 신호를 텔레그램으로 수동 전송"""
    from sqlalchemy import select
    stmt = select(Signal).where(Signal.id == signal_id)
    row = (await db.execute(stmt)).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="신호를 찾을 수 없습니다")
    sig = {
        "code": row.code, "name": row.name,
        "signal_type": row.signal_type, "strategy": row.strategy,
        "price": row.price, "target_price": row.target_price,
        "stop_loss": row.stop_loss, "reason": row.reason,
        "confidence": row.confidence,
    }
    ok = await notify_signal(sig)
    return {"message": "전송 완료" if ok else "전송 실패"}


# ── Trade 엔드포인트 ───────────────────────────────────────────────────────────
@app.post("/trade/order", tags=["Trade"])
async def create_order(
    signal_id: str,
    quantity: int | None = Query(None, description="수량 미입력 시 예산 기반 자동 계산"),
    use_market_price: bool = Query(True, description="True=시장가, False=지정가"),
    db: AsyncSession = Depends(get_db),
):
    """
    신호 기반 KIS 주문 실행.
    KIS_MOCK=true(기본값) 이면 모의투자 도메인으로 요청합니다.
    """
    result = await execute_order(db, signal_id, quantity, use_market_price)
    if not result.get("success") and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/trade/balance", tags=["Trade"])
async def get_account_balance():
    """KIS 계좌 잔고 조회"""
    try:
        return await get_balance()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/trade/price/{code}", tags=["Trade"])
async def get_stock_price(code: str):
    """종목 현재가 조회"""
    try:
        return await get_current_price(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/trade/stop-loss-check", tags=["Trade"])
async def stop_loss_check(db: AsyncSession = Depends(get_db)):
    """보유 종목 손절가 도달 여부 확인"""
    alerts = await check_stop_loss(db)
    return {
        "alert_count": len(alerts),
        "mode": "모의투자" if IS_MOCK else "실전투자",
        "data": alerts,
    }


@app.get("/trades", tags=["Trade"])
async def list_trades(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """체결 내역 조회"""
    from sqlalchemy import select, desc
    stmt = (
        select(Trade)
        .order_by(desc(Trade.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "mode": "모의투자" if IS_MOCK else "실전투자",
        "data": [
            {
                "id":              r.id,
                "signal_id":       r.signal_id,
                "code":            r.code,
                "name":            r.name,
                "order_type":      r.order_type,
                "price":           r.price,
                "quantity":        r.quantity,
                "amount":          r.amount,
                "status":          r.status,
                "broker_order_id": r.broker_order_id,
                "created_at":      r.created_at.isoformat() if r.created_at else None,
                "filled_at":       r.filled_at.isoformat() if r.filled_at else None,
            }
            for r in rows
        ],
    }
