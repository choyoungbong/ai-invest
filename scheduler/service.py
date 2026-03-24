"""
Scheduler – 자동 실행 스케줄러

APScheduler를 사용해 장 시작/중/종료 시 자동으로 작업을 실행합니다.

스케줄 (KST 기준):
  08:50 – 종목 마스터 동기화
  09:05 – 시세 수집 + 전략 실행 (장 시작 직후)
  10:00 – 시세 수집 + 전략 실행
  11:00 – 시세 수집 + 전략 실행
  13:00 – 시세 수집 + 전략 실행
  14:00 – 시세 수집 + 전략 실행
  15:10 – 시세 수집 + 전략 실행 (장 마감 직전)
  15:40 – 일일 리포트 발송
"""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from api.database import AsyncSessionLocal
from collector.service import sync_stock_master, collect_daily_ohlcv
from scanner.service import run_scanner
from strategy.service import run_strategy
from ai.service import analyze_all_new_signals
from notification.service import notify_signals_summary, send_message
from trader.auto_stoploss import check_and_execute_stop_loss
from trader.auto_trader import auto_execute_signals

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")


# ── 개별 작업 ──────────────────────────────────────────────────────────────────

async def job_sync_master():
    """종목 마스터 동기화"""
    logger.info("[스케줄러] 종목 마스터 동기화 시작")
    async with AsyncSessionLocal() as db:
        await sync_stock_master(db)
    logger.info("[스케줄러] 종목 마스터 동기화 완료")


async def job_collect_and_run():
    """시세 수집 → 스캔 → 전략 → AI 분석 → 알림"""
    now = datetime.now(KST).strftime("%H:%M")
    logger.info(f"[스케줄러] {now} 자동 실행 시작")

    async with AsyncSessionLocal() as db:
        # 1. 시세 수집
        await collect_daily_ohlcv(db)

        # 2. 스캔 + 전략
        candidates = await run_scanner(db, top_n=30)
        signals    = await run_strategy(db, candidates)

        # 3. AI 분석
        if signals:
            await analyze_all_new_signals(db)

        # 4. 텔레그램 알림
        await notify_signals_summary(signals)

        # 5. 자동 매수 실행 ← 핵심 추가
        orders = []
        if signals:
            orders = await auto_execute_signals(db, signals)

    logger.info(f"[스케줄러] {now} 자동 실행 완료 — 신호 {len(signals)}건, 매수 {len(orders)}건")


async def job_stop_loss_check():
    """손절가 도달 시 자동 매도 실행"""
    async with AsyncSessionLocal() as db:
        executed = await check_and_execute_stop_loss(db)
    if executed:
        logger.warning(f"[스케줄러] 자동 손절 실행: {len(executed)}건")


async def job_daily_report():
    """장 마감 후 일일 리포트"""
    from sqlalchemy import select, func
    from api.models import Signal, Trade

    today_start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)

    async with AsyncSessionLocal() as db:
        # 오늘 신호 수
        sig_count = (await db.execute(
            select(func.count(Signal.id)).where(Signal.created_at >= today_start)
        )).scalar() or 0

        # 오늘 주문 수 / 총액
        trade_rows = (await db.execute(
            select(Trade).where(Trade.created_at >= today_start)
        )).scalars().all()

    total_amount = sum(t.amount for t in trade_rows)
    filled = sum(1 for t in trade_rows if t.status == "FILLED")

    msg = (
        f"📊 <b>[AI INVEST] 일일 리포트</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 {datetime.now(KST).strftime('%Y-%m-%d')}\n"
        f"🟢 발생 신호: {sig_count}건\n"
        f"🛒 실행 주문: {filled}건\n"
        f"💵 총 거래금액: {total_amount:,.0f}원\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ 내일도 화이팅!"
    )
    await send_message(msg)
    logger.info("[스케줄러] 일일 리포트 발송 완료")


# ── 스케줄러 팩토리 ────────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=KST)

    # 08:50 종목 마스터 동기화
    scheduler.add_job(job_sync_master, CronTrigger(hour=8, minute=50, timezone=KST),
                      id="sync_master", name="종목 마스터 동기화")

    # 장중 6회 자동 실행 (월~금)
    for hour, minute in [(9, 5), (10, 0), (11, 0), (13, 0), (14, 0), (15, 10)]:
        scheduler.add_job(
            job_collect_and_run,
            CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri", timezone=KST),
            id=f"run_{hour:02d}{minute:02d}",
            name=f"{hour:02d}:{minute:02d} 자동 실행",
        )

    # 장중 30분마다 손절 체크
    scheduler.add_job(
        job_stop_loss_check,
        CronTrigger(hour="9-15", minute="*/5", day_of_week="mon-fri", timezone=KST),
        id="stop_loss_check", name="손절 체크",
    )

    # 15:40 일일 리포트
    scheduler.add_job(job_daily_report, CronTrigger(hour=15, minute=40, day_of_week="mon-fri", timezone=KST),
                      id="daily_report", name="일일 리포트")

    # 매시 정각 헬스체크
    async def job_health():
        from api.monitor import run_health_check_and_notify
        import os
        await run_health_check_and_notify(AsyncSessionLocal, os.getenv("REDIS_URL", "redis://redis:6379/0"))

    scheduler.add_job(
        job_health,
        CronTrigger(minute=0, timezone=KST),
        id="health_check", name="헬스체크",
    )

    return scheduler
