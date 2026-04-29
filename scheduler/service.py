"""
Scheduler – 자동 실행 스케줄러

스케줄 (KST 기준):
  08:50          – 종목 마스터 동기화
  09:05, 10:00, 11:00, 12:00, 13:00, 14:00, 15:10
                 – 시세수집 + 전략 + AI + 매수 (풀 실행)
  09:30, 10:30, 11:30, 12:30, 13:30, 14:30
                 – 수집 없이 스캔+전략+매수만 (빠른 실행, 10초 이내)
  장중 5분마다   – 손절/익절 체크
  장중 10분마다  – 2차 분할매수 체크
  15:40          – 일일 리포트
  매주 금요일 16:00 – 주간 리포트
  매시 정각      – 헬스체크

[버그 수정 v4 — 동일 종목 반복 알림 제거]
  기존: 30분마다 스캔 시 이미 오늘 알림을 보낸 종목도 매번 텔레그램 재발송
        → 같은 종목이 09:05, 09:30, 10:00, 10:30 … 계속 반복 알림
  수정: _notified_codes_today (인메모리 set) 로 당일 알림 발송 종목 추적
        → 오늘 처음 발생한 종목만 알림, 이미 알린 종목은 로그만 출력
        → 날짜가 바뀌면 자동 초기화 (다음 날 다시 알림 발송)
        → 실행(매수 시도)은 모든 신호 대상으로 유지 (알림만 중복 억제)
"""
import os
import logging
from datetime import datetime, date

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
from trader.risk_manager import sync_positions_with_kis

MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# ── 알림 중복 억제 상태 ────────────────────────────────────────────────────────
# "신호 없음" 알림 — 하루 1회
_no_signal_alerted_date: date | None = None

# [버그 수정 v4] 당일 알림 발송 종목 코드 추적
# 날짜가 바뀌면 자동 초기화, 이미 알림 보낸 종목은 당일 재알림 생략
_notified_codes_today:   set[str]    = set()
_notified_codes_date:    date | None = None


def _get_new_signal_codes(all_signals: list[dict]) -> list[dict]:
    """
    오늘 처음 발생한 신호(코드 기준)만 반환합니다.
    이미 알림을 보낸 종목은 제외하고, 새 종목만 추적 set에 추가합니다.

    실행(매수 시도) 대상은 all_signals 전체이며, 이 함수는 알림 필터링 전용입니다.
    """
    global _notified_codes_today, _notified_codes_date

    today_kst = datetime.now(KST).date()
    if _notified_codes_date != today_kst:
        _notified_codes_today = set()
        _notified_codes_date  = today_kst

    new_signals = [s for s in all_signals if s["code"] not in _notified_codes_today]

    # 이번 배치에서 새로 알린 종목을 set에 등록
    for s in new_signals:
        _notified_codes_today.add(s["code"])

    return new_signals


# ── KIS ↔ DB 포지션 싱크 ──────────────────────────────────────────────────────

async def job_sync_positions():
    """KIS 실제 잔고 ↔ DB 포지션 싱크"""
    try:
        async with AsyncSessionLocal() as db:
            result = await sync_positions_with_kis(db)
        logger.info(f"[스케줄러] 포지션 싱크 완료: {result}")
    except Exception as e:
        logger.error(f"[스케줄러] 포지션 싱크 오류: {e}")


# ── 공통 전략 실행 함수 ────────────────────────────────────────────────────────

async def _run_strategy_and_trade(db, now_str: str) -> tuple[list, list]:
    """
    스캔 → 메인전략 + 확장전략 → AI → 매수 공통 로직.
    수집(collect) 여부와 무관하게 재사용.

    [버그 수정 v4] 신호 알림 중복 억제
      - 실행(auto_execute_signals)은 all_signals 전체 대상 유지
        (execution 레이어에서 open_position / blacklist / overtrading 등으로 이미 보호됨)
      - 텔레그램 알림은 오늘 처음 등장한 종목 코드만 발송
        (이미 알린 종목 재발생 시 로그만 출력하고 알림 생략)
    """
    global _no_signal_alerted_date

    candidates   = await run_scanner(db, top_n=150)
    main_signals = await run_strategy(db, candidates)

    try:
        from strategy.extended import run_extended_strategy
        ext_signals = await run_extended_strategy(db, candidates)
    except Exception as e:
        logger.warning(f"확장 전략 실행 오류: {e}")
        ext_signals = []

    seen_codes  = {s["code"] for s in main_signals}
    ext_unique  = [s for s in ext_signals if s["code"] not in seen_codes]

    # ── MAX_POSITIONS 초과 방지 ───────────────────────────────────────────────
    from trader.risk_manager import check_max_positions
    pos_hit, current_cnt = await check_max_positions(db)
    available_slots = max(0, MAX_POSITIONS - current_cnt)

    # 신뢰도 높은 순으로 정렬 후 슬롯 수만큼만 허용
    all_candidates = main_signals + ext_unique
    all_candidates.sort(key=lambda s: s.get("confidence", 0), reverse=True)
    all_signals = all_candidates[:available_slots] if available_slots < len(all_candidates) else all_candidates

    if len(all_candidates) > available_slots:
        logger.info(f"[슬롯 제한] 신호 {len(all_candidates)}건 → {available_slots}건 (슬롯 {current_cnt}/{MAX_POSITIONS})")

    if all_signals:
        await analyze_all_new_signals(db)

    today_kst = datetime.now(KST).date()

    if all_signals:
        _no_signal_alerted_date = None

        # [버그 수정 v4] 당일 처음 발생한 종목만 알림 발송
        new_for_notify = _get_new_signal_codes(all_signals)
        repeat_count   = len(all_signals) - len(new_for_notify)

        if new_for_notify:
            await _notify_signals_summary(new_for_notify, main_signals, ext_unique, now_str)
        
        if repeat_count > 0:
            # 이미 알림 보낸 종목은 로그만 출력 (텔레그램 알림 생략)
            repeat_codes = [s["code"] for s in all_signals if s["code"] not in {n["code"] for n in new_for_notify}]
            logger.info(
                f"[스케줄러] {now_str} 반복 신호 {repeat_count}건 알림 생략 "
                f"(오늘 이미 발송된 종목: {repeat_codes})"
            )

    else:
        # 신호 없음 — 하루 1회만 알림
        if _no_signal_alerted_date != today_kst:
            _no_signal_alerted_date = today_kst
            await send_message(
                f"📭 <b>[AI INVEST] 오늘 첫 스캔 — 신호 없음</b>\n"
                f"시각: {now_str}\n"
                f"후속 스캔 결과는 신호 발생 시에만 알림 발송됩니다."
            )
        else:
            logger.info(f"[스케줄러] {now_str} 신호 없음 — 알림 생략")

    # 실행은 all_signals 전체 대상 (알림 필터와 무관)
    orders = []
    if all_signals:
        filtered = await filter_signals(db, all_signals)
        logger.info(f"전략 신호: {len(all_signals)} / 필터 후: {len(filtered)}")
        orders = await auto_execute_signals(db, filtered)

    return all_signals, orders


# ── 풀 실행 (수집 + 전략 + 매수) ──────────────────────────────────────────────

async def job_collect_and_run():
    """
    [풀 실행] 시세 수집 → 스캔 → 전략 → AI → 매수 → 기간만료 청산
    하루 7회: 09:05, 10:00, 11:00, 12:00, 13:00, 14:00, 15:10
    """
    now     = datetime.now(KST)
    now_str = now.strftime("%H:%M")
    logger.info(f"[스케줄러] {now_str} 풀 실행 시작")

    all_signals, orders = [], []

    try:
        async with AsyncSessionLocal() as db:
            await collect_daily_ohlcv(db)
            all_signals, orders = await _run_strategy_and_trade(db, now_str)
            await check_and_close_expired_positions(db)

    except Exception as e:
        err_msg = repr(e) if str(e) == "" else str(e)
        logger.error(f"[스케줄러] {now_str} 풀 실행 오류: {err_msg}", exc_info=True)
        await send_message(
            f"⚠️ <b>[AI INVEST] 스케줄러 오류</b>\n"
            f"시각: {now_str}\n오류: {err_msg[:200]}"
        )

    logger.info(
        f"[스케줄러] {now_str} 풀 실행 완료 "
        f"— 신호 {len(all_signals)}건, 매수 {len(orders)}건"
    )


# ── 빠른 실행 (수집 없이 스캔 + 전략 + 매수) ──────────────────────────────────

async def job_scan_only():
    """
    [빠른 실행] 수집 없이 DB 기존 시세로 스캔+전략+매수만 실행
    10초 이내 완료. 하루 6회: 09:30, 10:30, 11:30, 12:30, 13:30, 14:30
    """
    now     = datetime.now(KST)
    now_str = now.strftime("%H:%M")
    logger.info(f"[스케줄러] {now_str} 빠른 스캔 시작")

    all_signals, orders = [], []

    try:
        async with AsyncSessionLocal() as db:
            all_signals, orders = await _run_strategy_and_trade(db, now_str)

    except Exception as e:
        err_msg = repr(e) if str(e) == "" else str(e)
        logger.error(f"[스케줄러] {now_str} 빠른 스캔 오류: {err_msg}", exc_info=True)

    logger.info(
        f"[스케줄러] {now_str} 빠른 스캔 완료 "
        f"— 신호 {len(all_signals)}건, 매수 {len(orders)}건"
    )


# ── 알림 헬퍼 ─────────────────────────────────────────────────────────────────

async def _notify_signals_summary(
    new_signals: list,   # [버그 수정 v4] 오늘 처음 발생한 신호만 전달받음
    main_signals: list,
    ext_signals: list,
    now_str: str = "",
) -> None:
    """
    신호 요약 텔레그램 알림.
    new_signals: 오늘 처음 발생한 종목만 포함 (중복 종목 제외됨)
    """
    if not now_str:
        now_str = datetime.now(KST).strftime("%H:%M")

    try:
        from notification.service import notify_signals_summary
        await notify_signals_summary(new_signals)
    except Exception:
        # notification.service에 notify_signals_summary가 없는 경우 fallback
        new_main = [s for s in new_signals if s in main_signals]
        new_ext  = [s for s in new_signals if s in ext_signals]

        lines = [f"📊 <b>[AI INVEST] 신규 신호 발생</b> ({now_str})\n━━━━━━━━━━━━━━━━━━"]
        if new_main:
            lines.append(f"🎯 돌파전략: {len(new_main)}건")
        if new_ext:
            lines.append(f"📈 확장전략: {len(new_ext)}건")
        for s in new_signals[:5]:
            lines.append(f"  • {s['name']} ({s['code']}) @ {s['price']:,}원")
        if len(new_signals) > 5:
            lines.append(f"  ... 외 {len(new_signals)-5}건")
        lines.append("※ 오늘 처음 발생한 종목만 표시됩니다.")
        await send_message("\n".join(lines))


# ── 기타 작업 ─────────────────────────────────────────────────────────────────

async def job_sync_master():
    """종목 마스터 동기화"""
    logger.info("[스케줄러] 종목 마스터 동기화 시작")
    async with AsyncSessionLocal() as db:
        await sync_stock_master(db)
    logger.info("[스케줄러] 종목 마스터 동기화 완료")


async def job_phase2_check():
    """2차 분할매수 조건 체크 — 장중 10분마다"""
    try:
        async with AsyncSessionLocal() as db:
            result = await check_and_execute_phase2(db)
        if result:
            logger.info(f"[스케줄러] 2차 분할매수 실행: {len(result)}건")
    except Exception as e:
        logger.error(f"[스케줄러] 2차 매수 체크 오류: {e}")


async def job_stop_loss_check():
    """손절/익절/트레일링 스탑 체크 — 장중 5분마다"""
    try:
        async with AsyncSessionLocal() as db:
            executed = await check_and_execute_stop_loss(db)
        if executed:
            logger.warning(f"[스케줄러] 자동 청산 실행: {len(executed)}건")
    except Exception as e:
        logger.error(f"[스케줄러] 손절 체크 오류: {e}")


async def job_conditional_sell_eod():
    """
    15:10 조건부 청산
    - 수익 +0.5% 미만 포지션 → 당일 청산 (익일 갭다운 리스크 방지)
    - 수익 +0.5% 이상 포지션 → 익일 보유 허용 (트레일링 스탑 계속 동작)
    """
    from sqlalchemy import select, and_
    from api.models import Trade
    from trader import kis_client as kis
    from trader.auto_stoploss import _get_open_position, _execute_sell

    async with AsyncSessionLocal() as db:
        signal_ids = (await db.execute(
            select(Trade.signal_id).where(and_(
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            )).distinct()
        )).scalars().all()

        closed, kept = [], []
        for sid in signal_ids:
            position = await _get_open_position(db, sid)
            if not position:
                continue

            try:
                price_data    = await kis.get_current_price(position["code"])
                current_price = price_data["price"]
            except Exception:
                continue

            profit_pct = (current_price / position["avg_buy_price"] - 1) * 100

            if profit_pct >= 0.5:
                kept.append(f"{position['name']} ({profit_pct:+.1f}%)")
                logger.info(f"[{position['code']}] 익일 보유 유지: {profit_pct:+.1f}%")
            else:
                result = await _execute_sell(
                    db, position, current_price,
                    f"15:10 조건부 청산 ({profit_pct:+.1f}%)"
                )
                if result:
                    closed.append(f"{position['name']} ({profit_pct:+.1f}%)")

        msg_lines = ["🔔 <b>[AI INVEST] 15:10 조건부 청산</b>"]
        if closed:
            msg_lines.append(f"📤 청산: {', '.join(closed)}")
        if kept:
            msg_lines.append(f"📌 익일 보유: {', '.join(kept)}")
        if not closed and not kept:
            msg_lines.append("보유 종목 없음")

        await send_message("\n".join(msg_lines))


async def job_force_sell_eod():
    """장 종료 전 미청산 포지션 전량 청산 (현재 비활성)"""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://localhost:8000/trade/emergency-close-all?confirm=true",
                timeout=30
            )
        logger.warning(f"[스케줄러] 장 종료 전 청산 실행: {r.text[:100]}")
        await send_message(
            "🔔 <b>[AI INVEST] 장 종료 전 청산</b>\n"
            "15:15 당일 미청산 포지션 전량 청산 실행"
        )
    except Exception as e:
        logger.error(f"[스케줄러] 장 종료 전 청산 오류: {e}")


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
    scheduler = AsyncIOScheduler(
        timezone=KST,
        job_defaults={
            "coalesce":            True,
            "max_instances":       1,
            "misfire_grace_time":  300,
        }
    )

    # 08:50 종목 마스터 동기화
    scheduler.add_job(
        job_sync_master,
        CronTrigger(hour=8, minute=50, timezone=KST),
        id="sync_master", name="종목 마스터 동기화",
    )

    # ── 풀 실행: 하루 7회 (수집 + 전략 + 매수) ──────────────────────────────
    for hour, minute in [(9, 5), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 10)]:
        scheduler.add_job(
            job_collect_and_run,
            CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri", timezone=KST),
            id=f"run_{hour:02d}{minute:02d}",
            name=f"{hour:02d}:{minute:02d} 풀 실행",
        )

    # ── 빠른 스캔: 하루 6회 (수집 없이 전략 + 매수) ─────────────────────────
    for hour, minute in [(9, 30), (10, 30), (11, 30), (12, 30), (13, 30), (14, 30), (15, 0)]:
        scheduler.add_job(
            job_scan_only,
            CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri", timezone=KST),
            id=f"scan_{hour:02d}{minute:02d}",
            name=f"{hour:02d}:{minute:02d} 빠른 스캔",
        )

    # ── 포지션 싱크: 장 시작 / 장 종료 ─────────────────────────────────────
    scheduler.add_job(
        job_sync_positions,
        CronTrigger(hour=9, minute=5, timezone=KST),
        id="sync_positions_open", name="포지션 싱크 (장 시작)",
    )
    scheduler.add_job(
        job_sync_positions,
        CronTrigger(hour=15, minute=25, timezone=KST),
        id="sync_positions_close", name="포지션 싱크 (장 종료)",
    )
    # ── 포지션 싱크: 장 중 30분마다 ────────────────────────────────────────
    scheduler.add_job(
        job_sync_positions,
        CronTrigger(hour="9-15", minute="*/30", day_of_week="mon-fri", timezone=KST),
        id="sync_positions_intraday",
        name="포지션 싱크 (장 중 30분)",
        max_instances=1,
        coalesce=True,
    )


    # ── 손절/익절/트레일링 체크: 5분마다 ────────────────────────────────────
    scheduler.add_job(
        job_stop_loss_check,
        CronTrigger(hour="9-15", minute="*/5", day_of_week="mon-fri", timezone=KST),
        id="stop_loss_check",
        name="손절/익절 체크 (5분)",
        max_instances=1,
        coalesce=True,
    )

    # ── 2차 분할매수 체크: 10분마다 ─────────────────────────────────────────
    scheduler.add_job(
        job_phase2_check,
        CronTrigger(hour="9-15", minute="*/10", day_of_week="mon-fri", timezone=KST),
        id="phase2_check",
        name="2차 분할매수 체크 (10분)",
        max_instances=1,
        coalesce=True,
    )

    # ── 15:10 조건부 청산 (수동매도 전략으로 비활성화) ──────────────────────
    # scheduler.add_job(
    #     job_conditional_sell_eod,
    #     CronTrigger(hour=15, minute=10, day_of_week="mon-fri", timezone=KST),
    #     id="conditional_sell_eod",
    #     name="15:10 조건부 청산",
    # )

    # ── 15:15 당일 전체 강제 청산 (현재 비활성 — 필요 시 주석 해제) ──────────
    # scheduler.add_job(
    #     job_force_sell_eod,
    #     CronTrigger(hour=15, minute=15, day_of_week="mon-fri", timezone=KST),
    #     id="force_sell_eod",
    #     name="15:15 당일 강제 청산",
    # )

    # ── 15:40 일일 리포트 ────────────────────────────────────────────────────
    scheduler.add_job(
        job_daily_report,
        CronTrigger(hour=15, minute=40, day_of_week="mon-fri", timezone=KST),
        id="daily_report", name="일일 리포트",
    )

    # ── 매주 금요일 16:00 주간 리포트 ───────────────────────────────────────
    scheduler.add_job(
        job_weekly_report,
        CronTrigger(hour=16, minute=0, day_of_week="fri", timezone=KST),
        id="weekly_report", name="주간 리포트",
    )

    # ── 매시 정각 헬스체크 ───────────────────────────────────────────────────
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

    # ── 🇺🇸 미국 ETF 자동매매 스케줄 ────────────────────────────────────────
    # US_TRADING_ENABLED=false(기본값)이면 각 job 내부에서 즉시 return — 안전
    # .env에서 US_TRADING_ENABLED=true 로 변경 시 활성화

    # 22:30 개장 직전: 비대상 종목 청산 + 당일 기준 총자산 기록
    scheduler.add_job(
        job_us_market_open,
        CronTrigger(hour=22, minute=30, day_of_week="mon-fri", timezone=KST),
        id="us_market_open",
        name="🇺🇸 미국장 개장 (청산+시작)",
    )

    # 22:35 ~ 04:35: 30분 신호 스캔 (정각+5분, 정각+35분)
    for h, m in [
        (22, 35), (23, 5), (23, 35),
        (0,  5),  (0, 35), (1,  5),  (1, 35),
        (2,  5),  (2, 35), (3,  5),  (3, 35),
        (4,  5),  (4, 35),
    ]:
        scheduler.add_job(
            job_us_scan,
            CronTrigger(hour=h, minute=m, day_of_week="mon-sat", timezone=KST),
            id=f"us_scan_{h:02d}{m:02d}",
            name=f"🇺🇸 {h:02d}:{m:02d} 미국 스캔",
        )

    # 손절/익절 체크: 야간 5분마다 (22:35~23:55)
    scheduler.add_job(
        job_us_position_check,
        CronTrigger(hour="22-23", minute="*/5",
                    day_of_week="mon-fri", timezone=KST),
        id="us_pos_night", name="🇺🇸 미국 포지션 체크 (야간)",
        max_instances=1, coalesce=True,
    )
    # 손절/익절 체크: 새벽 5분마다 (00:00~04:50)
    scheduler.add_job(
        job_us_position_check,
        CronTrigger(hour="0-4", minute="*/5",
                    day_of_week="tue-sat", timezone=KST),
        id="us_pos_dawn", name="🇺🇸 미국 포지션 체크 (새벽)",
        max_instances=1, coalesce=True,
    )

    # 04:55: 미국장 마감 + 일일 결과
    scheduler.add_job(
        job_us_market_close,
        CronTrigger(hour=4, minute=55, day_of_week="tue-sat", timezone=KST),
        id="us_market_close",
        name="🇺🇸 미국장 마감",
    )
    return scheduler



# ── 🇺🇸 미국 ETF Job 함수들 ──────────────────────────────────────────────────

async def job_us_market_open():
    """
    미국장 개장 (22:30 KST).
    1. 비대상 종목(TORO, SNDL 등) 자동 청산
    2. 당일 기준 총자산 기록
    3. 개장 알림 발송
    """
    if os.getenv("US_TRADING_ENABLED", "false").lower() != "true":
        return
    try:
        from trader.us_auto_trader import auto_liquidate_on_open
        await auto_liquidate_on_open()
    except Exception as e:
        logger.error(f"[스케줄러] 🇺🇸 미국장 개장 처리 오류: {e}", exc_info=True)
        await send_message(
            f"⚠️ <b>[AI INVEST 🇺🇸] 개장 처리 오류</b>\n{str(e)[:200]}"
        )


async def job_us_scan():
    """미국 ETF 30분 신호 스캔 + 매수 실행"""
    if os.getenv("US_TRADING_ENABLED", "false").lower() != "true":
        return
    now_kst = datetime.now(KST).strftime("%H:%M")
    try:
        async with AsyncSessionLocal() as db:
            from trader.us_auto_trader import run_us_trading
            results = await run_us_trading(db)
        if results:
            logger.info(f"[스케줄러] 🇺🇸 {now_kst} 미국 매수 {len(results)}건")
        else:
            logger.info(f"[스케줄러] 🇺🇸 {now_kst} 미국 신호 없음")
    except Exception as e:
        logger.error(f"[스케줄러] 🇺🇸 미국 스캔 오류: {e}", exc_info=True)
        await send_message(
            f"⚠️ <b>[AI INVEST 🇺🇸] 스캔 오류</b>\n"
            f"시각: {now_kst}\n{str(e)[:200]}"
        )


async def job_us_position_check():
    """미국 ETF 포지션 손절/익절 체크 (5분마다)"""
    if os.getenv("US_TRADING_ENABLED", "false").lower() != "true":
        return
    try:
        async with AsyncSessionLocal() as db:
            from trader.us_auto_trader import check_us_positions
            executed = await check_us_positions(db)
        if executed:
            logger.info(f"[스케줄러] 🇺🇸 미국 청산 {len(executed)}건")
    except Exception as e:
        logger.error(f"[스케줄러] 🇺🇸 미국 포지션 체크 오류: {e}")


async def job_us_market_close():
    """미국장 마감 (04:55 KST) + 일일 결과 알림"""
    if os.getenv("US_TRADING_ENABLED", "false").lower() != "true":
        return
    try:
        from strategy.us_strategy import get_daily_status
        status = get_daily_status()
        pnl    = status["realized_pnl_usd"]
        emoji  = "📈" if pnl >= 0 else "📉"

        blocked = ""
        if status["blocked_symbols"]:
            blocked = f"\n🚫 연속손절 중단: {', '.join(status['blocked_symbols'])}"

        await send_message(
            f"🇺🇸 <b>[AI INVEST] 미국장 마감</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} 실현 손익: ${pnl:+.2f}\n"
            f"📋 거래 횟수: {status['total_trades']}회"
            f"{blocked}"
        )
    except Exception as e:
        logger.error(f"[스케줄러] 🇺🇸 미국장 마감 알림 오류: {e}")
