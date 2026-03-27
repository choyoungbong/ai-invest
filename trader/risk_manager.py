"""
Risk Manager – 실전 투자 방어 시스템 (최종 수정본)
수정 사항: 
1. 타임존(KST) 적용으로 서버 시간 오류 해결
2. 공휴일 체크 로직 구조 추가
3. 코드 가독성 및 안정성 강화
"""
import logging
import os
from datetime import datetime, time, timedelta
import pytz  # 타임존 처리를 위해 필요

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from api.models import Trade
from notification.service import send_message

logger = logging.getLogger(__name__)

# ── 타임존 설정 ─────────────────────────────────────────────────────────────
KST = pytz.timezone('Asia/Seoul')

# ── 파라미터 (환경변수로 관리) ─────────────────────────────────────────────────

raw_value = os.getenv("DAILY_LOSS_LIMIT")
logger.info(f"DEBUG: ENV DAILY_LOSS_LIMIT RAW VALUE: '{raw_value}'")

raw_value2 = int(os.getenv("DAILY_LOSS_LIMIT"))
logger.info(f"DEBUG: ENV DAILY_LOSS_LIMIT RAW VALUE: '{raw_value2}'")

DAILY_LOSS_LIMIT   = int(os.getenv("DAILY_LOSS_LIMIT",   "30000"))
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS",       "5"))
TRADE_START_HOUR   = int(os.getenv("TRADE_START_HOUR",   "9"))
TRADE_START_MINUTE = int(os.getenv("TRADE_START_MINUTE", "5"))
TRADE_END_HOUR     = int(os.getenv("TRADE_END_HOUR",     "15"))
TRADE_END_MINUTE   = int(os.getenv("TRADE_END_MINUTE",   "20"))

# 일일 손실 한도 초과 시 당일 매매 중단 플래그
_daily_limit_hit = False
_limit_hit_date  = None

def get_now():
    """한국 표준시(KST) 기준 현재 시간 반환"""
    return datetime.now(KST)

def reset_daily_flag():
    """매일 자정 플래그 초기화 (KST 기준)"""
    global _daily_limit_hit, _limit_hit_date
    today = get_now().date()
    if _limit_hit_date != today:
        _daily_limit_hit = False
        _limit_hit_date  = None

# ── A. 일일 손실 한도 ──────────────────────────────────────────────────────────

async def check_daily_loss(db: AsyncSession) -> tuple[bool, int]:
    global _daily_limit_hit, _limit_hit_date

    reset_daily_flag()

    if _daily_limit_hit:
        return True, -DAILY_LOSS_LIMIT

    # 오늘 시작 시각 (KST 기준)
    now_kst = get_now()
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(Trade)
        .where(and_(
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= today_start.replace(tzinfo=None), # DB가 naive datetime인 경우 대응
        ))
    )
    sell_trades = (await db.execute(stmt)).scalars().all()

    today_pnl = 0
    for sell in sell_trades:
        buy_stmt = select(Trade).where(and_(
            Trade.code == sell.code,
            Trade.order_type == "BUY",
            Trade.signal_id == sell.signal_id,
        ))
        buy = (await db.execute(buy_stmt)).scalars().first()
        if buy:
            today_pnl += (sell.price - buy.price) * sell.quantity

    if today_pnl <= -DAILY_LOSS_LIMIT:
        _daily_limit_hit = True
        _limit_hit_date  = now_kst.date()

        logger.warning(f"일일 손실 한도 초과: {today_pnl:,}원 (한도: -{DAILY_LOSS_LIMIT:,}원)")
        await send_message(
            f"🚨 <b>[AI INVEST] 일일 손실 한도 초과</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📉 오늘 손익: <b>{today_pnl:+,}원</b>\n"
            f"🛑 한도: -{DAILY_LOSS_LIMIT:,}원\n"
            f"⛔ 오늘 신규 매수를 중단합니다."
        )
        return True, today_pnl

    return False, today_pnl

# ── B. 동시 보유 종목 수 제한 ──────────────────────────────────────────────────

async def check_max_positions(db: AsyncSession) -> tuple[bool, int]:
    stmt = (
        select(Trade.code)
        .where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
        .distinct()
    )
    buy_codes = (await db.execute(stmt)).scalars().all()

    active = 0
    for code in buy_codes:
        sell_stmt = select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
        ))
        sold = (await db.execute(sell_stmt)).scalars().first()
        if not sold:
            active += 1

    exceeded = active >= MAX_POSITIONS
    return exceeded, active

# ── C. 장 운영 시간 확인 ───────────────────────────────────────────────────────

def is_market_open() -> bool:
    """KST 기준 장 운영 시간 및 공휴일 확인"""
    now = get_now()

    # 1. 주말 체크
    if now.weekday() >= 5:
        return False

    # 2. 공휴일 체크 (필요 시 추가 개발)
    # is_holiday = check_holiday(now.date()) 
    # if is_holiday: return False

    # 3. 시간대 체크
    current_time = now.time()
    start_time   = time(TRADE_START_HOUR, TRADE_START_MINUTE)
    end_time     = time(TRADE_END_HOUR,   TRADE_END_MINUTE)

    return start_time <= current_time <= end_time

def require_market_open(func_name: str = "") -> bool:
    if not is_market_open():
        logger.debug(f"[{func_name}] 장 외 시간 — KIS API 호출 건너뜀")
        return False
    return True

# ── 통합 방어 체크 ─────────────────────────────────────────────────────────────

async def can_buy(db: AsyncSession) -> tuple[bool, str]:
    if not is_market_open():
        return False, "장 외 시간"

    loss_hit, today_pnl = await check_daily_loss(db)
    if loss_hit:
        return False, f"일일 손실 한도 초과 ({today_pnl:+,}원)"

    pos_hit, pos_count = await check_max_positions(db)
    if pos_hit:
        return False, f"최대 보유 종목 수 초과 ({pos_count}/{MAX_POSITIONS})"

    return True, ""

async def get_risk_status(db: AsyncSession) -> dict:
    _, today_pnl    = await check_daily_loss(db)
    _, pos_count    = await check_max_positions(db)
    market_open     = is_market_open()
    buyable, reason = await can_buy(db)

    return {
        "market_open":      market_open,
        "can_buy":          buyable,
        "block_reason":     reason,
        "today_pnl":        today_pnl,
        "daily_loss_limit": -DAILY_LOSS_LIMIT,
        "positions":        pos_count,
        "max_positions":    MAX_POSITIONS,
        "daily_limit_hit":  _daily_limit_hit,
    }
