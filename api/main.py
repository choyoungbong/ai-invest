"""
AI INVEST – FastAPI 메인 앱

[B단계] 신규 엔드포인트 추가:
  GET  /performance/by-strategy  — 전략별 실제 거래 성과 집계
  POST /backtest/grid-search     — 파라미터 최적화 그리드 서치
"""
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from api.database import get_db, init_db, AsyncSessionLocal
from api.models import Signal, Trade

from collector.service import sync_stock_master, collect_daily_ohlcv
from scanner.service import run_scanner, get_top_volume_stocks
from strategy.service import run_strategy, get_signals
from notification.service import (
    notify_test, notify_signal, notify_signals_summary,
    notify_trade, send_message,
)
from ai.service import analyze_signal, analyze_all_new_signals
from trader.service import execute_order, check_stop_loss
from trader.kis_client import get_balance, get_current_price, IS_MOCK
from trader import kis_client as kis
from scheduler.service import create_scheduler
from backtest.service import run_backtest, run_multi_backtest, run_grid_search
from strategy.extended import run_extended_strategy
from api.monitor import ErrorMonitorMiddleware, run_health_check_and_notify, get_recent_errors
from trader.allocation import get_allocation_summary, calc_quantity_by_budget
from trader.auto_stoploss import check_and_execute_stop_loss
from trader.auto_trader import auto_execute_signals
from trader.ws_client import RealTimeMonitor
from report.service import send_daily_report, send_weekly_report, get_monthly_stats
from strategy.guard import filter_signals, check_and_close_expired_positions
from trader.risk_manager import get_risk_status, can_buy
from kis_verify.router import router as kis_verify_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

rt_monitor = RealTimeMonitor(db_factory=AsyncSessionLocal)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI INVEST 서버 시작 중...")
    await init_db()
    logger.info("DB 초기화 완료")

    scheduler = create_scheduler()
    scheduler.start()
    logger.info("스케줄러 시작 완료")

    await rt_monitor.start()
    await rt_monitor.subscribe_holdings()
    logger.info("실시간 WebSocket 모니터 시작")

    yield

    await rt_monitor.stop()
    scheduler.shutdown(wait=False)
    logger.info("AI INVEST 서버 종료")


app = FastAPI(
    title="AI INVEST",
    description="AI 기반 한국 주식 자동매매 시스템",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ErrorMonitorMiddleware)
app.include_router(kis_verify_router)


# ── 헬스체크 ──────────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "ai-invest", "mock": IS_MOCK}


# ── Collector ─────────────────────────────────────────────────────────────────
@app.post("/collector/sync-master", tags=["Collector"])
async def sync_master(db: AsyncSession = Depends(get_db)):
    await sync_stock_master(db)
    return {"message": "종목 마스터 동기화 완료"}


@app.post("/collector/collect", tags=["Collector"])
async def collect_today(
    date: str | None = Query(None, description="YYYYMMDD 형식"),
    db: AsyncSession = Depends(get_db),
):
    rows = await collect_daily_ohlcv(db, target_date=date)
    return {"message": f"시세 수집 완료: {len(rows) if rows else 0}건"}


# ── Scanner ───────────────────────────────────────────────────────────────────
@app.get("/scanner/top-volume", tags=["Scanner"])
async def scanner_top_volume(
    top_n: int = Query(80, ge=1, le=150),
    db: AsyncSession = Depends(get_db),
):
    results = await get_top_volume_stocks(db, top_n=top_n)
    return {"count": len(results), "data": results}


# ── Strategy ──────────────────────────────────────────────────────────────────
@app.post("/strategy/run", tags=["Strategy"])
async def strategy_run(
    top_n: int = Query(80, ge=1, le=150),
    db: AsyncSession = Depends(get_db),
):
    candidates = await run_scanner(db, top_n=top_n)
    signals    = await run_strategy(db, candidates)
    # AI 분석 비활성화 (ANTHROPIC_API_KEY 크레딧 부족)
    # if signals:
    #     await analyze_all_new_signals(db)
    await notify_signals_summary(signals)

    orders = []
    if signals:
        filtered = await filter_signals(db, signals)
        orders   = await auto_execute_signals(db, filtered)
        if orders:
            from trader.ws_client import update_subscribed_codes, _subscribed_codes
            update_subscribed_codes(list(_subscribed_codes) + [o["code"] for o in orders])

    return {
        "message": "전략 실행 완료",
        "candidates": len(candidates),
        "signals": len(signals),
        "orders": len(orders),
        "data": signals,
    }


@app.get("/signals", tags=["Strategy"])
async def list_signals(
    limit: int = Query(50, ge=1, le=200),
    signal_type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    data = await get_signals(db, limit=limit, signal_type=signal_type)
    return {"count": len(data), "data": data}


@app.get("/signals/{signal_id}", tags=["Strategy"])
async def get_signal_detail(signal_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="신호를 찾을 수 없습니다")
    return {
        "id": row.id, "code": row.code, "name": row.name,
        "signal_type": row.signal_type, "strategy": row.strategy,
        "price": row.price, "target_price": row.target_price,
        "stop_loss": row.stop_loss, "reason": row.reason,
        "confidence": row.confidence, "is_executed": row.is_executed,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ── Risk Manager ──────────────────────────────────────────────────────────────
@app.get("/risk/status", tags=["Risk"])
async def risk_status(db: AsyncSession = Depends(get_db)):
    return await get_risk_status(db)


@app.post("/risk/reset-daily", tags=["Risk"])
async def reset_daily_limit():
    from trader.risk_manager import reset_daily_flag
    reset_daily_flag()
    return {"message": "일일 손실 한도 플래그 초기화 완료"}


# ── 긴급 전체 청산 ────────────────────────────────────────────────────────────
@app.post("/trade/emergency-close-all", tags=["Trade"])
async def emergency_close_all(
    confirm: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    if not confirm:
        return {"message": "confirm=true 를 붙여야 실행됩니다"}

    buy_trades = (await db.execute(
        select(Trade).where(and_(Trade.order_type == "BUY", Trade.status.in_(["FILLED", "PARTIAL"])))
    )).scalars().all()

    closed, failed = [], []
    for trade in buy_trades:
        # 미국 주식(영문 코드) 제외 — 국내 KIS API로 매도 불가
        if not trade.code.isdigit():
            continue
        sold = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == trade.signal_id,
                Trade.order_type == "SELL", Trade.status == "FILLED",
            ))
        )).scalars().first()
        if sold:
            continue

        try:
            current_price = (await kis.get_current_price(trade.code))["price"]
            result        = await kis.sell_order(trade.code, trade.quantity, order_type="01")
            status        = "FILLED" if result["success"] else "FAILED"
            await db.execute(Trade.__table__.insert().values(
                id=str(uuid.uuid4()), signal_id=trade.signal_id,
                code=trade.code, name=trade.name,
                order_type="SELL", price=current_price,
                quantity=trade.quantity, amount=current_price * trade.quantity,
                status=status, broker_order_id=result.get("order_no", ""),
                is_simulation=trade.is_simulation,
            ))
            await db.commit()
            if result["success"]:
                pnl = (current_price - trade.price) * trade.quantity
                closed.append({"code": trade.code, "name": trade.name,
                               "quantity": trade.quantity, "net_profit": pnl})
            else:
                failed.append({"code": trade.code, "name": trade.name, "reason": "주문 실패"})
        except Exception as e:
            failed.append({"code": trade.code, "name": trade.name, "reason": repr(e)})

    await send_message(
        f"🚨 <b>[AI INVEST] 긴급 전체 청산</b>\n"
        f"✅ 성공: {len(closed)}건 / ❌ 실패: {len(failed)}건"
    )
    return {"message": f"청산 완료: 성공 {len(closed)}건 / 실패 {len(failed)}건",
            "closed": closed, "failed": failed}


# ── Report ────────────────────────────────────────────────────────────────────
@app.get("/report/daily", tags=["Report"])
async def daily_report(db: AsyncSession = Depends(get_db)):
    return await send_daily_report(db)


@app.get("/report/weekly", tags=["Report"])
async def weekly_report(db: AsyncSession = Depends(get_db)):
    return await send_weekly_report(db)


@app.get("/report/monthly", tags=["Report"])
async def monthly_report(db: AsyncSession = Depends(get_db)):
    return await get_monthly_stats(db)


@app.post("/trade/close-expired", tags=["Trade"])
async def close_expired(db: AsyncSession = Depends(get_db)):
    closed = await check_and_close_expired_positions(db)
    return {"message": f"{len(closed)}건 청산", "data": closed}


@app.post("/trade/auto-stoploss", tags=["Trade"])
async def auto_stoploss(db: AsyncSession = Depends(get_db)):
    executed = await check_and_execute_stop_loss(db)
    return {"message": f"자동 손절 실행: {len(executed)}건", "data": executed}


# ── Monitor ───────────────────────────────────────────────────────────────────
@app.get("/monitor/health", tags=["Monitor"])
async def full_health_check():
    import os
    return await run_health_check_and_notify(
        AsyncSessionLocal, os.getenv("REDIS_URL", "redis://redis:6379/0")
    )


@app.get("/monitor/errors", tags=["Monitor"])
async def recent_errors():
    return {"errors": get_recent_errors()}


# ── Allocation ────────────────────────────────────────────────────────────────
@app.get("/allocation", tags=["Allocation"])
async def get_allocation():
    return get_allocation_summary()


@app.get("/allocation/calc", tags=["Allocation"])
async def calc_order_size(
    strategy:   str   = Query("breakout"),
    price:      float = Query(...),
    confidence: float = Query(0.5, ge=0.0, le=1.0),
):
    from trader.allocation import get_order_amount
    qty    = calc_quantity_by_budget(strategy, price, confidence)
    amount = get_order_amount(strategy, confidence)
    return {"strategy": strategy, "price": price, "confidence": confidence,
            "quantity": qty, "amount": amount, "total_cost": qty * price}


# ── Backtest ──────────────────────────────────────────────────────────────────
@app.get("/backtest", tags=["Backtest"])
async def backtest_single(
    code:       str = Query(...),
    strategy:   str = Query("breakout"),
    start_date: str = Query(...),
    end_date:   str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    result = await run_backtest(db, code, strategy, start_date, end_date)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/backtest/multi", tags=["Backtest"])
async def backtest_multi(
    codes:      list[str] = Query(...),
    strategy:   str       = Query("breakout"),
    start_date: str       = Query(...),
    end_date:   str       = Query(...),
    db: AsyncSession = Depends(get_db),
):
    return await run_multi_backtest(db, codes, strategy, start_date, end_date)


@app.post("/backtest/grid-search", tags=["Backtest"])
async def backtest_grid_search(
    codes:      list[str] = Query(...,    description="백테스트 종목 코드 목록"),
    strategy:   str       = Query("breakout"),
    start_date: str       = Query(...),
    end_date:   str       = Query(...),
    top_n:      int       = Query(20, ge=5, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    파라미터 그리드 서치 — 최적 파라미터 조합 탐색.

    breakout 기준 108개, ma_cross 54개, rsi_reversal 36개 조합을 탐색하고
    profit_factor 상위 top_n 결과를 반환합니다.

    ⚠️ 종목 수 × 조합 수만큼 시간이 소요됩니다. 3~5종목 권장.
    """
    result = await run_grid_search(db, codes, strategy, start_date, end_date, top_n)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Performance (B단계 신규) ───────────────────────────────────────────────────
@app.get("/performance/by-strategy", tags=["Performance"])
async def performance_by_strategy(
    days: int = Query(30, ge=1, le=365, description="최근 N일"),
    db: AsyncSession = Depends(get_db),
):
    """
    전략별 실제 거래 성과를 집계합니다.

    DB의 실제 SELL 체결 내역을 Signal.strategy로 그룹핑하여
    전략별 승률·평균 수익률·총 손익·평균 보유일 등을 반환합니다.
    """
    from datetime import datetime, timedelta
    import pytz
    KST = pytz.timezone("Asia/Seoul")

    now_kst  = datetime.now(KST)
    today_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_kst = today_kst - timedelta(days=days - 1)
    cutoff_utc = cutoff_kst.astimezone(pytz.utc).replace(tzinfo=None)

    # 기간 내 모든 SELL 체결
    sells = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= cutoff_utc,
        ))
    )).scalars().all()

    strategy_data: dict[str, list] = {}

    for sell in sells:
        # 해당 signal의 strategy 조회
        sig = (await db.execute(
            select(Signal).where(Signal.id == sell.signal_id)
        )).scalars().first()
        strategy = sig.strategy if sig else "unknown"

        # 매수 이력으로 평균 매수가 계산
        buy_trades = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == sell.signal_id,
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            ))
        )).scalars().all()

        if not buy_trades:
            continue

        total_qty = sum(t.quantity for t in buy_trades)
        avg_buy   = sum(t.price * t.quantity for t in buy_trades) / total_qty
        buy_comm  = sum(t.commission or 0 for t in buy_trades)
        profit_pct   = (sell.price / avg_buy - 1) * 100
        net_profit   = (sell.price - avg_buy) * sell.quantity - buy_comm - (sell.commission or 0)
        entry_date   = min(t.created_at for t in buy_trades)
        hold_hours   = (sell.created_at - entry_date).total_seconds() / 3600
        hold_days    = hold_hours / 24

        if strategy not in strategy_data:
            strategy_data[strategy] = []
        strategy_data[strategy].append({
            "profit_pct": profit_pct,
            "net_profit": net_profit,
            "hold_days":  hold_days,
            "exit_reason": "익절" if profit_pct > 0 else "손절",
        })

    # 전략별 집계
    result = {}
    for strategy, trades in strategy_data.items():
        profits   = [t["profit_pct"] for t in trades]
        wins      = [p for p in profits if p > 0]
        losses    = [p for p in profits if p <= 0]
        net_total = sum(t["net_profit"] for t in trades)
        avg_hold  = sum(t["hold_days"] for t in trades) / len(trades)

        avg_win  = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        pf       = abs(avg_win / avg_loss) if avg_loss else 99.0

        result[strategy] = {
            "total_trades":    len(trades),
            "win_count":       len(wins),
            "lose_count":      len(losses),
            "win_rate":        round(len(wins) / len(profits) * 100, 1) if profits else 0,
            "avg_profit_pct":  round(sum(profits) / len(profits), 2) if profits else 0,
            "avg_win_pct":     round(avg_win, 2),
            "avg_loss_pct":    round(avg_loss, 2),
            "profit_factor":   round(pf, 2),
            "total_net_profit": round(net_total, 0),
            "avg_hold_days":   round(avg_hold, 1),
        }

    # 전체 합산
    all_profits   = [t["profit_pct"] for ts in strategy_data.values() for t in ts]
    all_net       = sum(t["net_profit"] for ts in strategy_data.values() for t in ts)
    all_wins      = [p for p in all_profits if p > 0]
    all_losses    = [p for p in all_profits if p <= 0]
    overall_pf    = abs((sum(all_wins)/len(all_wins)) / (sum(all_losses)/len(all_losses))) \
                    if all_losses and all_wins else 99.0

    return {
        "period_days":     days,
        "total_trades":    len(all_profits),
        "total_win_rate":  round(len(all_wins) / len(all_profits) * 100, 1) if all_profits else 0,
        "total_net_profit": round(all_net, 0),
        "overall_profit_factor": round(overall_pf, 2),
        "by_strategy":     result,
    }


# ── Extended Strategy ─────────────────────────────────────────────────────────
@app.post("/strategy/extended", tags=["Strategy"])
async def strategy_extended(
    top_n: int = Query(80, ge=1, le=150),
    strategies: list[str] = Query(["ma_cross", "rsi_reversal", "macd"]),
    db: AsyncSession = Depends(get_db),
):
    candidates = await run_scanner(db, top_n=top_n)
    signals    = await run_extended_strategy(db, candidates, strategies)
    await notify_signals_summary(signals)
    return {"message": "확장 전략 실행 완료",
            "candidates": len(candidates), "signals": len(signals), "data": signals}


# ── Scheduler ─────────────────────────────────────────────────────────────────
@app.get("/scheduler/jobs", tags=["Scheduler"])
async def list_jobs():
    sch = create_scheduler()
    return {"jobs": [{"id": j.id, "name": j.name,
                      "next_run": str(j.next_run_time) if hasattr(j, "next_run_time") else None}
                     for j in sch.get_jobs()]}


@app.post("/scheduler/run-now", tags=["Scheduler"])
async def run_now(db: AsyncSession = Depends(get_db)):
    from scheduler.service import job_collect_and_run
    await job_collect_and_run()
    return {"message": "수동 실행 완료"}


# ── AI Analysis ───────────────────────────────────────────────────────────────
@app.post("/ai/analyze/{signal_id}", tags=["AI Analysis"])
async def ai_analyze_signal(signal_id: str, db: AsyncSession = Depends(get_db)):
    result = await analyze_signal(db, signal_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/ai/analyze-all", tags=["AI Analysis"])
async def ai_analyze_all(db: AsyncSession = Depends(get_db)):
    results = await analyze_all_new_signals(db)
    return {"message": f"AI 분석 완료: {len(results)}건", "data": results}


# ── Notification ──────────────────────────────────────────────────────────────
@app.post("/notification/test", tags=["Notification"])
async def notification_test():
    ok = await notify_test()
    if not ok:
        raise HTTPException(status_code=400, detail="전송 실패 — 토큰/ChatID 확인")
    return {"message": "텔레그램 테스트 전송 완료"}


@app.post("/notification/signal/{signal_id}", tags=["Notification"])
async def notify_signal_by_id(signal_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="신호를 찾을 수 없습니다")
    ok = await notify_signal({
        "code": row.code, "name": row.name, "signal_type": row.signal_type,
        "strategy": row.strategy, "price": row.price, "target_price": row.target_price,
        "stop_loss": row.stop_loss, "reason": row.reason, "confidence": row.confidence,
    })
    return {"message": "전송 완료" if ok else "전송 실패"}


# ── Trade ─────────────────────────────────────────────────────────────────────
@app.post("/trade/order", tags=["Trade"])
async def create_order(
    signal_id: str,
    quantity: int | None = Query(None),
    use_market_price: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    result = await execute_order(db, signal_id, quantity, use_market_price)
    if not result.get("success") and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/trade/balance", tags=["Trade"])
async def get_account_balance():
    try:
        return await get_balance()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/trade/price/{code}", tags=["Trade"])
async def get_stock_price(code: str):
    try:
        return await get_current_price(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/trade/stop-loss-check", tags=["Trade"])
async def stop_loss_check(db: AsyncSession = Depends(get_db)):
    alerts = await check_stop_loss(db)
    return {"alert_count": len(alerts), "mode": "모의투자" if IS_MOCK else "실전투자", "data": alerts}


@app.get("/trades", tags=["Trade"])
async def list_trades(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Trade).order_by(Trade.created_at.desc()).limit(limit)
    )).scalars().all()
    return {
        "count": len(rows),
        "mode": "모의투자" if IS_MOCK else "실전투자",
        "data": [
            {
                "id": r.id, "signal_id": r.signal_id, "code": r.code, "name": r.name,
                "order_type": r.order_type, "price": r.price,
                "quantity": r.quantity, "amount": r.amount,
                "commission": r.commission, "real_profit": r.real_profit,
                "status": r.status, "is_simulation": r.is_simulation,
                "broker_order_id": r.broker_order_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "filled_at": r.filled_at.isoformat() if r.filled_at else None,
            }
            for r in rows
        ],
    }


# ── 🇺🇸 미국 ETF 자동매매 엔드포인트 ─────────────────────────────────────────

@app.get("/us-trading/status", tags=["US Trading"])
async def us_trading_status():
    """
    미국 자동매매 현재 상태 조회.
    - 장 운영 여부, 당일 거래 현황, 리스크 상태 포함
    """
    from trader.us_kis_client import is_us_market_open, get_us_market_schedule
    from strategy.us_strategy import get_daily_status, US_ETF_CONFIG
    import os

    schedule = get_us_market_schedule()
    daily    = get_daily_status()

    return {
        "us_trading_enabled": os.getenv("US_TRADING_ENABLED", "false"),
        "market_schedule":    schedule,
        "daily_status":       daily,
        "target_etfs": {
            sym: {
                "name":          cfg["name"],
                "exchange":      cfg["exchange"],
                "target_profit": f"+{cfg['target_profit']*100:.1f}%",
                "stop_loss":     f"{cfg['stop_loss']*100:.1f}%",
                "budget_ratio":  f"{cfg['budget_ratio']*100:.0f}%",
            }
            for sym, cfg in US_ETF_CONFIG.items()
        },
    }


@app.get("/us-trading/balance", tags=["US Trading"])
async def us_trading_balance():
    """
    해외 계좌 잔고 조회.
    보유 종목 목록, 예수금, 총자산 포함.
    """
    try:
        from trader.us_kis_client import get_us_balance
        balance = await get_us_balance()
        return {"success": True, "data": balance}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/us-trading/liquidate", tags=["US Trading"])
async def us_liquidate(
    confirm: bool = Query(False, description="반드시 true 전달"),
):
    """
    비대상 종목 수동 청산.
    SPLG / TQQQ / SOXL 이외의 보유 종목을 전량 시장가 매도합니다.

    ⚠️ confirm=true 필수. 손실 확정될 수 있습니다.
    """
    if not confirm:
        return {
            "message": "confirm=true 를 붙여야 실행됩니다.",
            "example": "/us-trading/liquidate?confirm=true",
        }
    try:
        from trader.us_auto_trader import auto_liquidate_on_open
        results = await auto_liquidate_on_open()
        return {
            "success": True,
            "count":   len(results),
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/us-trading/scan-now", tags=["US Trading"])
async def us_scan_now(db: AsyncSession = Depends(get_db)):
    """
    미국 ETF 수동 스캔 및 매수 실행.
    스케줄 외 즉시 신호 탐색이 필요할 때 사용합니다.
    """
    from trader.us_kis_client import is_us_market_open
    if not is_us_market_open():
        return {"message": "미국 장 외 시간입니다.", "market_open": False}
    try:
        from trader.us_auto_trader import run_us_trading
        results = await run_us_trading(db)
        return {
            "message": f"스캔 완료: {len(results)}건 매수",
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/us-trading/trades", tags=["US Trading"])
async def us_trading_trades(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """미국 ETF 체결 내역 조회"""
    from strategy.us_strategy import US_ETF_CONFIG
    us_symbols = list(US_ETF_CONFIG.keys())

    rows = (await db.execute(
        select(Trade)
        .where(Trade.code.in_(us_symbols))
        .order_by(Trade.created_at.desc())
        .limit(limit)
    )).scalars().all()

    return {
        "count": len(rows),
        "data": [
            {
                "id":          r.id,
                "code":        r.code,
                "name":        r.name,
                "order_type":  r.order_type,
                "price":       r.price,
                "quantity":    r.quantity,
                "amount":      r.amount,
                "real_profit": r.real_profit,
                "status":      r.status,
                "created_at":  r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
