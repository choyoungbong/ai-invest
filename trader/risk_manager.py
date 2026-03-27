"""
Risk Manager – 실전 투자 방어 시스템

A. 일일 손실 한도: 하루 N원 손실 시 자동 매매 중단
B. 동시 보유 종목 수 제한: 최대 N종목만 보유
C. 장 외 시간 API 호출 차단: 09:00~15:30 외 KIS API 차단
"""
import logging
import os
from datetime import datetime, time

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from api.models import Trade
from notification.service import send_message

logger = logging.getLogger(__name__)


# ── 파라미터 (환경변수로 관리) ─────────────────────────────────────────────────
DAILY_LOSS_LIMIT   = int(os.getenv("DAILY_LOSS_LIMIT",   "30000"))   # 일일 최대 손실 (원)
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS",       "5"))       # 최대 동시 보유 종목
TRADE_START_HOUR   = int(os.getenv("TRADE_START_HOUR",   "9"))        # 매수 시작 시간
TRADE_START_MINUTE = int(os.getenv("TRADE_START_MINUTE", "5"))        # 매수 시작 분
TRADE_END_HOUR     = int(os.getenv("TRADE_END_HOUR",     "15"))       # 매수 종료 시간
TRADE_END_MINUTE   = int(os.getenv("TRADE_END_MINUTE",   "20"))       # 매수 종료 분

# 일일 손실 한도 초과 시 당일 매매 중단 플래그
_daily_limit_hit = False
_limit_hit_date  = None


def reset_daily_flag():
    """매일 자정 플래그 초기화"""
    global _daily_limit_hit, _limit_hit_date
    today = datetime.now().date()
    if _limit_hit_date != today:
        _daily_limit_hit = False
        _limit_hit_date  = None


# ── A. 일일 손실 한도 ──────────────────────────────────────────────────────────

async def check_daily_loss(db: AsyncSession) -> tuple[bool, int]:
    """
    오늘 실현 손익을 계산합니다.
    손실이 DAILY_LOSS_LIMIT 초과 시 True 반환.

    Returns:
        (한도초과여부, 오늘손익)
    """
    global _daily_limit_hit, _limit_hit_date

    reset_daily_flag()

    if _daily_limit_hit:
        return True, -DAILY_LOSS_LIMIT

    today_start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # 오늘 체결된 매도 거래 조회
    stmt = (
        select(Trade)
        .where(and_(
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= today_start,
        ))
    )
    sell_trades = (await db.execute(stmt)).scalars().all()

    today_pnl = 0
    for sell in sell_trades:
        # 연결된 매수 조회
        buy_stmt = select(Trade).where(and_(
            Trade.code == sell.code,
            Trade.order_type == "BUY",
            Trade.signal_id == sell.signal_id,
        ))
        buy = (await db.execute(buy_stmt)).scalars().first()
        if buy:
            today_pnl += (sell.price - buy.price) * sell.quantity

    # 손실 한도 초과 확인
    if today_pnl <= -DAILY_LOSS_LIMIT:
        _daily_limit_hit = True
        _limit_hit_date  = datetime.now().date()

        logger.warning(f"일일 손실 한도 초과: {today_pnl:,}원 (한도: -{DAILY_LOSS_LIMIT:,}원)")

        await send_message(
            f"🚨 <b>[AI INVEST] 일일 손실 한도 초과</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📉 오늘 손익: <b>{today_pnl:+,}원</b>\n"
            f"🛑 한도: -{DAILY_LOSS_LIMIT:,}원\n"
            f"⛔ 오늘 신규 매수를 중단합니다.\n"
            f"내일 장 시작 시 자동으로 재개됩니다."
        )
        return True, today_pnl

    return False, today_pnl


# ── B. 동시 보유 종목 수 제한 ──────────────────────────────────────────────────

async def check_max_positions(db: AsyncSession) -> tuple[bool, int]:
    """
    현재 보유 중인 종목 수가 MAX_POSITIONS 이상이면 True 반환.

    Returns:
        (한도초과여부, 현재보유수)
    """
    # 체결된 매수 포지션 중 아직 매도 안 된 것
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
        # 해당 종목 매도 여부 확인
        sell_stmt = select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
        ))
        sold = (await db.execute(sell_stmt)).scalars().first()
        if not sold:
            active += 1

    exceeded = active >= MAX_POSITIONS
    if exceeded:
        logger.info(f"최대 보유 종목 수 초과: {active}/{MAX_POSITIONS}")

    return exceeded, active


# ── C. 장 운영 시간 확인 ───────────────────────────────────────────────────────

def is_market_open() -> bool:
    """
    현재 시각이 장 운영 시간(기본 09:05~15:20) 내인지 확인합니다.
    주말이면 False 반환.
    """
    now = datetime.now()

    # 주말 체크
    if now.weekday() >= 5:
        return False

    current     = now.time()
    start_time  = time(TRADE_START_HOUR, TRADE_START_MINUTE)
    end_time    = time(TRADE_END_HOUR,   TRADE_END_MINUTE)

    return start_time <= current <= end_time


def require_market_open(func_name: str = "") -> bool:
    """
    장 외 시간이면 경고 로그 출력 후 False 반환.
    장 중이면 True 반환.
    """
    if not is_market_open():
        logger.debug(f"[{func_name}] 장 외 시간 — KIS API 호출 건너뜀")
        return False
    return True


# ── 통합 방어 체크 ─────────────────────────────────────────────────────────────

async def can_buy(db: AsyncSession) -> tuple[bool, str]:
    """
    매수 가능 여부를 종합적으로 판단합니다.

    Returns:
        (매수가능여부, 불가사유)
    """
    # C. 장 운영 시간
    if not is_market_open():
        return False, "장 외 시간"

    # A. 일일 손실 한도
    loss_hit, today_pnl = await check_daily_loss(db)
    if loss_hit:
        return False, f"일일 손실 한도 초과 ({today_pnl:+,}원)"

    # B. 최대 보유 종목 수
    pos_hit, pos_count = await check_max_positions(db)
    if pos_hit:
        return False, f"최대 보유 종목 수 초과 ({pos_count}/{MAX_POSITIONS})"

    return True, ""


async def get_risk_status(db: AsyncSession) -> dict:
    """현재 리스크 상태 요약"""
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
