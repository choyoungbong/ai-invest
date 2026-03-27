"""
Scheduler – 자동 실행 스케줄러

스케줄 (KST 기준):
  08:50 – 종목 마스터 동기화
  09:05, 10:00, 11:00, 13:00, 14:00, 15:10 – 시세수집 + 전략 + AI + 매수
  장중 5분마다 – 손절/익절 체크 (빠른 반응)
  15:40 – 일일 리포트
  매주 금요일 16:00 – 주간 리포트
  매시 정각 – 헬스체크
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
from strategy.guard import filter_signals, check_and_close_expired_positions
from ai.service import analyze_all_new_signals
from notification.service import notify_signals_summary, send_message
from trader.auto_stoploss import check_and_execute_stop_loss
from trader.auto_trader import auto_execute_signals
from report.service import send_daily_report

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
    """시세 수집 → 스캔 → 전략 → AI → 알림 → 매수 → 기간만료 청산"""
    now = datetime.now(KST).strftime("%H:%M")
    logger.info(f"[스케줄러] {now} 자동 실행 시작")

    signals = []
    orders  = []

    try:
        async with AsyncSessionLocal() as db:
            await collect_daily_ohlcv(db)
            candidates = await run_scanner(db, top_n=30)
            signals    = await run_strategy(db, candidates)

            if signals:
                await analyze_all_new_signals(db)

            await notify_signals_summary(signals)

            if signals:
                filtered = await filter_signals(db, signals)
                orders   = await auto_execute_signals(db, filtered)

            await check_and_close_expired_positions(db)

    except Exception as e:
        logger.error(f"[스케줄러] {now} 자동 실행 오류: {e}")
        await send_message(
            f"⚠️ <b>[AI INVEST] 스케줄러 오류</b>\n"
            f"시각: {now}\n오류: {str(e)[:200]}"
        )

    logger.info(
        f"[스케줄러] {now} 자동 실행 완료 "
        f"— 신호 {len(signals)}건, 매수 {len(orders)}건"
    )


async def job_stop_loss_check():
    """
    손절/익절 체크 — 장중 5분마다 실행
    빠른 반응으로 -2% 이내 손절 보장
    """
    try:
        async with AsyncSessionLocal() as db:
            executed = await check_and_execute_stop_loss(db)
        if executed:
            logger.warning(f"[스케줄러] 자동 청산 실행: {len(executed)}건")
    except Exception as e:
        logger.error(f"[스케줄러] 손절 체크 오류: {e}")


async def job_daily_report():
    """장 마감 후 일일 리포트"""
    try:
        async with AsyncSessionLocal() as db:
            await send_daily_report(db)
        logger.info("[스케줄러] 일일 리포트 발송 완료")
    except Exception as e:
        logger.error(f"[스케줄러] 일일 리포트 오류: {e}")


async def job_weekly_report():
    """매주 금요일 주간 리포트"""
    try:
        from report.service import send_weekly_report
        async with AsyncSessionLocal() as db:
            await send_weekly_report(db)
        logger.info("[스케줄러] 주간 리포트 발송 완료")
    except Exception as e:
        logger.error(f"[스케줄러] 주간 리포트 오류: {e}")


# ── 스케줄러 팩토리 ────────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=KST)

    # 08:50 종목 마스터 동기화
    scheduler.add_job(
        job_sync_master,
        CronTrigger(hour=8, minute=50, timezone=KST),
        id="sync_master", name="종목 마스터 동기화",
    )

    # 장중 6회 자동 실행 (월~금)
    for hour, minute in [(9, 5), (10, 0), (11, 0), (13, 0), (14, 0), (15, 10)]:
        scheduler.add_job(
            job_collect_and_run,
            CronTrigger(
                hour=hour, minute=minute,
                day_of_week="mon-fri", timezone=KST
            ),
            id=f"run_{hour:02d}{minute:02d}",
            name=f"{hour:02d}:{minute:02d} 자동 실행",
        )

    # ── 핵심 개선: 손절 체크 30분 → 5분으로 단축 ──────────────────────────────
    # 09:05 ~ 15:25 사이 5분마다 실행 (장중에만)
    scheduler.add_job(
        job_stop_loss_check,
        CronTrigger(
            hour="9-15",
            minute="*/5",
            day_of_week="mon-fri",
            timezone=KST,
        ),
        id="stop_loss_check",
        name="손절/익절 체크 (5분)",
        max_instances=1,          # 중복 실행 방지
        coalesce=True,            # 밀린 실행 합치기
    )

    # 15:40 일일 리포트
    scheduler.add_job(
        job_daily_report,
        CronTrigger(hour=15, minute=40, day_of_week="mon-fri", timezone=KST),
        id="daily_report", name="일일 리포트",
    )

    # 매주 금요일 16:00 주간 리포트
    scheduler.add_job(
        job_weekly_report,
        CronTrigger(hour=16, minute=0, day_of_week="fri", timezone=KST),
        id="weekly_report", name="주간 리포트",
    )

    # 매시 정각 헬스체크
    async def job_health():
        try:
            from api.monitor import run_health_check_and_notify
            import os
            await run_health_check_and_notify(
                AsyncSessionLocal,
                os.getenv("REDIS_URL", "redis://redis:6379/0")
            )
        except Exception as e:
            logger.error(f"[스케줄러] 헬스체크 오류: {e}")

    scheduler.add_job(
        job_health,
        CronTrigger(minute=0, timezone=KST),
        id="health_check", name="헬스체크",
    )

    return scheduler
