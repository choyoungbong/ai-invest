"""
Scheduler – 자동 실행 스케줄러

스케줄 (KST 기준):
  08:50                             – 종목 마스터 동기화
  09:05, 10:00, 11:00, 13:00, 14:00, 15:10
                                    – 시세수집 + 메인전략 + 확장전략 + AI + 매수
  장중 5분마다                        – 손절/익절 체크 (빠른 반응)
  장중 10분마다                       – 2차 분할매수 체크
  15:40                             – 일일 리포트
  매주 금요일 16:00                   – 주간 리포트
  매시 정각                           – 헬스체크

개선사항 (v2):
  ✅ 분할매수 2차 체크 스케줄 추가 (10분마다)
  ✅ 확장 전략(MA/RSI/MACD) 자동 실행 연동
  ✅ 신호 없음 알림 하루 1회로 축소 (중복 발송 방지)
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
from notification.service import send_message
from trader.auto_stoploss import check_and_execute_stop_loss
from trader.auto_trader import auto_execute_signals, check_and_execute_phase2
from report.service import send_daily_report

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# 하루 1회 "신호 없음" 알림 억제 플래그
_no_signal_alerted_date = None


# ── 개별 작업 ──────────────────────────────────────────────────────────────────

async def job_sync_master():
    """종목 마스터 동기화"""
    logger.info("[스케줄러] 종목 마스터 동기화 시작")
    async with AsyncSessionLocal() as db:
        await sync_stock_master(db)
    logger.info("[스케줄러] 종목 마스터 동기화 완료")


async def job_collect_and_run():
    """
    시세 수집 → 스캔 → 메인전략 + 확장전략 → AI → 알림 → 매수 → 기간만료 청산

    [개선] 확장 전략(MA크로스/RSI반등/MACD) 자동 실행 연동
    [개선] 신호 없음 알림 하루 1회로 축소
    """
    global _no_signal_alerted_date

    now     = datetime.now(KST)
    now_str = now.strftime("%H:%M")
    logger.info(f"[스케줄러] {now_str} 자동 실행 시작")

    all_signals = []
    orders      = []

    try:
        async with AsyncSessionLocal() as db:
            await collect_daily_ohlcv(db)
            candidates = await run_scanner(db, top_n=30)

            # ── 메인 전략 (돌파매매) ──────────────────────────────────────
            main_signals = await run_strategy(db, candidates)

            # ── 확장 전략 (MA크로스/RSI반등/MACD) 자동 실행 ──────────────
            try:
                from strategy.extended import run_extended_strategy
                ext_signals = await run_extended_strategy(db, candidates)
            except Exception as e:
                logger.warning(f"확장 전략 실행 오류: {e}")
                ext_signals = []

            # 중복 코드 제거 후 합산
            seen_codes  = {s["code"] for s in main_signals}
            ext_unique  = [s for s in ext_signals if s["code"] not in seen_codes]
            all_signals = main_signals + ext_unique

            if all_signals:
                await analyze_all_new_signals(db)

            # ── 신호 없음 알림 억제: 하루 1회만 발송 ────────────────────
            today_kst = now.date()
            if all_signals:
                _no_signal_alerted_date = None  # 신호 있으면 플래그 초기화
                await _notify_signals_summary(all_signals, main_signals, ext_unique)
            else:
                if _no_signal_alerted_date != today_kst:
                    _no_signal_alerted_date = today_kst
                    await send_message(
                        f"📭 <b>[AI INVEST] 오늘 첫 스캔 — 신호 없음</b>\n"
                        f"시각: {now_str}\n"
                        f"후속 스캔 결과는 신호 발생 시에만 알림 발송됩니다."
                    )
                else:
                    logger.info(f"[스케줄러] {now_str} 신호 없음 — 알림 생략 (오늘 이미 발송)")

            # ── 매수 실행 ─────────────────────────────────────────────────
            if all_signals:
                filtered = await filter_signals(db, all_signals)
                orders   = await auto_execute_signals(db, filtered)

            await check_and_close_expired_positions(db)

    except Exception as e:
        logger.error(f"[스케줄러] {now_str} 자동 실행 오류: {e}")
        await send_message(
            f"⚠️ <b>[AI INVEST] 스케줄러 오류</b>\n"
            f"시각: {now_str}\n오류: {str(e)[:200]}"
        )

    logger.info(
        f"[스케줄러] {now_str} 자동 실행 완료 "
        f"— 신호 {len(all_signals)}건, 매수 {len(orders)}건"
    )


async def _notify_signals_summary(
    all_signals: list,
    main_signals: list,
    ext_signals: list,
) -> None:
    """신호 요약 알림 (메인+확장 전략 구분)"""
    try:
        from notification.service import notify_signals_summary
        # 기존 함수 호출 시도
        await notify_signals_summary(all_signals)
    except Exception:
        # fallback: 직접 알림
        now_str = datetime.now(KST).strftime("%H:%M")
        lines = [f"📊 <b>[AI INVEST] 신호 발생</b> ({now_str})\n━━━━━━━━━━━━━━━━━━"]
        if main_signals:
            lines.append(f"🎯 돌파전략: {len(main_signals)}건")
        if ext_signals:
            lines.append(f"📈 확장전략: {len(ext_signals)}건")
        for s in all_signals[:5]:
            lines.append(f"  • {s['name']} ({s['code']}) @ {s['price']:,}원")
        if len(all_signals) > 5:
            lines.append(f"  ... 외 {len(all_signals)-5}건")
        await send_message("\n".join(lines))


async def job_phase2_check():
    """
    2차 분할매수 조건 체크 — 장중 10분마다 실행.
    1차 매수 후 MIN_MINUTES 경과 & 상승 추세 확인 시 2차 매수 실행.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await check_and_execute_phase2(db)
        if result:
            logger.info(f"[스케줄러] 2차 분할매수 실행: {len(result)}건")
    except Exception as e:
        logger.error(f"[스케줄러] 2차 매수 체크 오류: {e}")


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

    # ── 손절/익절 체크: 5분마다 ──────────────────────────────────────────────
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
        max_instances=1,
        coalesce=True,
    )

    # ── 2차 분할매수 체크: 10분마다 ──────────────────────────────────────────
    scheduler.add_job(
        job_phase2_check,
        CronTrigger(
            hour="9-15",
            minute="*/10",
            day_of_week="mon-fri",
            timezone=KST,
        ),
        id="phase2_check",
        name="2차 분할매수 체크 (10분)",
        max_instances=1,
        coalesce=True,
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
