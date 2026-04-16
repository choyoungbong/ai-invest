"""
Risk Manager — Phase 3/4/5/6 통합 + 미실현 손익 포함 + 블랙리스트

타임존 규칙:
  - DB 쿼리용 datetime: naive UTC (datetime.utcnow())
  - 화면/로직용 datetime: KST aware (datetime.now(KST))
  - DB의 created_at은 TIMESTAMP WITHOUT TIME ZONE (naive UTC) 로 저장됨

블랙리스트:
  - 손절 청산 후 BLACKLIST_DAYS(기본 3일)간 재매수 차단
  - DB의 stock_blacklist 테이블 사용
  - expires_at < 현재시각 이면 자동 해제

[버그 수정 v2]
  - _daily_limit_hit 인메모리 플래그 문제 수정
    기존: 서버 재시작(OOM, 배포 등) 시 플래그 초기화 → 당일 손실 한도 무력화
    수정: 매 호출 시 DB 실계산을 우선으로, 인메모리는 성능 캐시 용도로만 사용
          → 재시작 후 첫 호출에서 DB를 통해 당일 손실 상태를 정확히 복원
"""
import logging
import os
from datetime import datetime, time, timedelta, timezone

import pytz
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, desc

from api.models import Trade, StockBlacklist
from notification.service import send_message

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")
UTC = timezone.utc


# ── 타임존 헬퍼 ───────────────────────────────────────────────────────────────

def _kst_today_start_utc() -> datetime:
    """
    KST 오늘 00:00 를 naive UTC 로 변환.
    DB 쿼리에 사용합니다.
    KST = UTC+9 이므로 KST 00:00 = UTC 전날 15:00
    """
    now_kst   = datetime.now(KST)
    today_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today_kst.astimezone(pytz.utc).replace(tzinfo=None)
    return today_utc


def _utcnow() -> datetime:
    """naive UTC 현재 시각 (DB 쿼리용)"""
    return datetime.utcnow()


# ── 환경변수 파라미터 ──────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT     = int(os.getenv("DAILY_LOSS_LIMIT",     "30000"))
MAX_POSITIONS        = int(os.getenv("MAX_POSITIONS",         "5"))
TRADE_START_HOUR     = int(os.getenv("TRADE_START_HOUR",     "9"))
TRADE_START_MINUTE   = int(os.getenv("TRADE_START_MINUTE",   "5"))
TRADE_END_HOUR       = int(os.getenv("TRADE_END_HOUR",       "15"))
TRADE_END_MINUTE     = int(os.getenv("TRADE_END_MINUTE",     "20"))
SLIPPAGE_LIMIT_PCT   = float(os.getenv("SLIPPAGE_LIMIT_PCT", "0.005"))
REENTRY_MINUTES      = int(os.getenv("REENTRY_MINUTES",      "60"))
STOP_REENTRY_MINUTES = int(os.getenv("STOP_REENTRY_MINUTES", "30"))
MAX_DAILY_TRADES     = int(os.getenv("MAX_DAILY_TRADES",     "10"))
BUY_COMMISSION       = float(os.getenv("BUY_COMMISSION",     "0.00015"))
SELL_COMMISSION      = float(os.getenv("SELL_COMMISSION",    "0.00015"))
DEFAULT_SLIPPAGE     = float(os.getenv("DEFAULT_SLIPPAGE",   "0.0005"))
SIMULATION_MODE      = os.getenv("SIMULATION_MODE", "true").lower() == "true"
BLACKLIST_DAYS       = int(os.getenv("BLACKLIST_DAYS",        "3"))

# ── 일일 한도 인메모리 캐시 (성능 최적화 용도만 — 정합성은 DB 기준) ──────────
# [버그 수정]
#   기존: 이 플래그가 유일한 한도 판단 기준 → 재시작 시 초기화되어 한도 무력화
#   수정: DB 실계산이 항상 우선. 이 캐시는 "이미 한도 초과 확인된 당일" 중복 DB 조회
#         횟수를 줄이기 위한 성능 보조 캐시로만 사용.
#         재시작 후 첫 호출은 반드시 DB를 통해 당일 손실 상태를 복원함.
_daily_limit_cache: dict = {
    "hit": False,
    "date": None,
}


def _get_limit_cache() -> bool:
    """당일 손실 한도 캐시 조회. 날짜가 바뀌면 자동 초기화."""
    today_kst = datetime.now(KST).date()
    if _daily_limit_cache["date"] != today_kst:
        _daily_limit_cache["hit"] = False
        _daily_limit_cache["date"] = None
    return _daily_limit_cache["hit"]


def _set_limit_cache(hit: bool) -> None:
    """당일 손실 한도 캐시 저장."""
    _daily_limit_cache["hit"] = hit
    _daily_limit_cache["date"] = datetime.now(KST).date()


def reset_daily_flag():
    """하위 호환성 유지용 래퍼. 스케줄러 등에서 호출해도 안전."""
    today_kst = datetime.now(KST).date()
    if _daily_limit_cache["date"] != today_kst:
        _daily_limit_cache["hit"] = False
        _daily_limit_cache["date"] = None


# ── C. 장 운영 시간 ────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    now_kst = datetime.now(KST)
    if now_kst.weekday() >= 5:
        return False
    current    = now_kst.time()
    start_time = time(TRADE_START_HOUR, TRADE_START_MINUTE)
    end_time   = time(TRADE_END_HOUR, TRADE_END_MINUTE)
    return start_time <= current <= end_time


def require_market_open(func_name: str = "") -> bool:
    if not is_market_open():
        logger.debug(f"[{func_name}] 장 외 시간 — 건너뜀")
        return False
    return True


# ── BLACKLIST — 손절 종목 블랙리스트 ──────────────────────────────────────────

async def add_to_blacklist(
    db: AsyncSession,
    code: str,
    name: str,
    reason: str = "",
) -> None:
    """손절 청산 후 BLACKLIST_DAYS 동안 재진입 금지 등록"""
    now_utc    = _utcnow()
    expires_at = now_utc + timedelta(days=BLACKLIST_DAYS)

    # 이미 등록된 경우 expires_at 갱신
    existing = (await db.execute(
        select(StockBlacklist).where(StockBlacklist.code == code)
    )).scalars().first()

    if existing:
        await db.execute(
            StockBlacklist.__table__.update()
            .where(StockBlacklist.code == code)
            .values(
                reason=reason,
                blacklisted_at=now_utc,
                expires_at=expires_at,
            )
        )
    else:
        await db.execute(
            StockBlacklist.__table__.insert().values(
                code=code,
                name=name,
                reason=reason,
                blacklisted_at=now_utc,
                expires_at=expires_at,
            )
        )

    await db.commit()
    logger.info(f"블랙리스트 등록: {code} {name} ({BLACKLIST_DAYS}일, {reason})")


async def check_blacklist(db: AsyncSession, code: str) -> tuple[bool, str]:
    """해당 종목이 블랙리스트에 있는지 확인. (True, 사유) or (False, '')"""
    now_utc = _utcnow()
    entry = (await db.execute(
        select(StockBlacklist).where(and_(
            StockBlacklist.code == code,
            StockBlacklist.expires_at > now_utc,  # 아직 유효한 항목만
        ))
    )).scalars().first()

    if entry:
        remain_days = (entry.expires_at - now_utc).days
        return True, f"블랙리스트 ({remain_days}일 잔여, {entry.reason})"

    return False, ""


# ── A. 일일 손실 한도 (실현 + 미실현 포함) ────────────────────────────────────

async def calc_unrealized_pnl(db: AsyncSession) -> int:
    """현재 보유 포지션의 미실현 손익 계산"""
    # signal_id 기준으로 미청산 포지션 수집
    signal_ids = (await db.execute(
        select(Trade.signal_id).where(and_(
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
        )).distinct()
    )).scalars().all()

    unrealized = 0
    for sid in signal_ids:
        sold = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == sid,
                Trade.order_type == "SELL",
                Trade.status == "FILLED",
            ))
        )).scalars().first()
        if sold:
            continue

        buy_trades = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == sid,
                Trade.order_type == "BUY",
                Trade.status.in_(["FILLED", "PARTIAL"]),
            ))
        )).scalars().all()
        if not buy_trades:
            continue

        # 가중평균 매수가
        total_qty  = sum(t.quantity for t in buy_trades)
        avg_price  = sum(t.price * t.quantity for t in buy_trades) / total_qty
        code       = buy_trades[0].code

        if is_market_open():
            try:
                from trader import kis_client as kis
                price_data    = await kis.get_current_price(code)
                current_price = price_data.get("price", 0)
                if current_price > 0:
                    unrealized += (current_price - avg_price) * total_qty
            except Exception:
                pass

    return int(unrealized)


async def _calc_today_pnl(db: AsyncSession) -> tuple[int, int]:
    """
    오늘 실현 손익 + 미실현 손익을 각각 계산해 반환합니다.
    Returns: (realized_pnl, unrealized_pnl)
    """
    today_start_utc = _kst_today_start_utc()

    stmt = select(Trade).where(and_(
        Trade.order_type == "SELL",
        Trade.status == "FILLED",
        Trade.created_at >= today_start_utc,
    ))
    sells = (await db.execute(stmt)).scalars().all()

    realized_pnl = 0
    for sell in sells:
        buy_trades = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == sell.signal_id,
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            ))
        )).scalars().all()
        if buy_trades:
            total_qty = sum(t.quantity for t in buy_trades)
            avg_price = sum(t.price * t.quantity for t in buy_trades) / total_qty
            gross     = (sell.price - avg_price) * sell.quantity
            comm      = sum(t.commission or 0 for t in buy_trades) + (sell.commission or 0)
            realized_pnl += gross - comm

    unrealized_pnl = await calc_unrealized_pnl(db)
    return int(realized_pnl), int(unrealized_pnl)


async def check_daily_loss(db: AsyncSession) -> tuple[bool, int]:
    """
    오늘 실현 + 미실현 손익 합산해 한도 초과 여부 확인.

    [버그 수정]
      기존: 인메모리 _daily_limit_hit 플래그가 유일한 판단 기준
            → 서버 재시작 시 플래그 초기화로 당일 손실 한도 무력화
      수정: 항상 DB 실계산을 수행하여 정확한 손실 상태 확인
            인메모리 캐시는 "이미 한도 초과된 날" 반복 DB 조회를 줄이기 위한
            성능 보조 캐시로만 사용 (재시작 후 첫 호출은 반드시 DB 계산)
    """
    reset_daily_flag()

    # [버그 수정] 캐시가 True여도 DB 재계산으로 검증
    # 기존 코드는 캐시가 True면 바로 return → 재시작 후 초기화된 캐시는 False라
    # 당일 이미 한도 초과 상태였어도 첫 호출에서 차단되지 않는 문제 존재
    # 수정: 항상 DB 계산 수행, 캐시는 중복 알림 방지용으로만 사용
    cache_already_hit = _get_limit_cache()

    realized_pnl, unrealized_pnl = await _calc_today_pnl(db)
    total_pnl = realized_pnl + unrealized_pnl

    logger.debug(
        f"일일 손익 — 실현: {realized_pnl:+,}원 / "
        f"미실현: {unrealized_pnl:+,}원 / 합계: {total_pnl:+,}원"
    )

    if total_pnl <= -DAILY_LOSS_LIMIT:
        # 캐시가 False였다면 (재시작 후 복원 포함) 이번에 처음 감지한 것이므로 알림 발송
        if not cache_already_hit:
            logger.warning(f"일일 손실 한도 초과: {total_pnl:,}원")
            await send_message(
                f"🚨 <b>[AI INVEST] 일일 손실 한도 초과</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📉 실현 손익: {realized_pnl:+,}원\n"
                f"📊 미실현 손익: {unrealized_pnl:+,}원\n"
                f"💥 합계: <b>{total_pnl:+,}원</b>\n"
                f"🛑 한도: -{DAILY_LOSS_LIMIT:,}원\n"
                f"⛔ 오늘 신규 매수를 중단합니다."
            )
        else:
            logger.debug(f"일일 손실 한도 초과 유지 중: {total_pnl:,}원 (알림 생략)")

        _set_limit_cache(True)
        return True, total_pnl

    # 한도 미초과 — 캐시 초기화
    _set_limit_cache(False)
    return False, total_pnl


# ── B. 동시 보유 종목 수 ───────────────────────────────────────────────────────

async def check_max_positions(db: AsyncSession) -> tuple[bool, int]:
    """
    KIS 실제 잔고 기준으로 포지션 카운트 (Primary).
    KIS API 실패 시 DB 기반 fallback (Secondary).
    """
    try:
        from trader import kis_client as kis
        balance = await kis.get_balance()
        holdings = balance.get("holdings", [])
        active = len(holdings)
        logger.debug(f"[포지션] KIS 실잔고 기준: {active}개 {[h['code'] for h in holdings]}")
        return active >= MAX_POSITIONS, active

    except Exception as e:
        logger.warning(f"[포지션] KIS 잔고 조회 실패 → DB fallback: {e}")
        return await _check_max_positions_db(db)


async def _check_max_positions_db(db: AsyncSession) -> tuple[bool, int]:
    """DB 기반 포지션 카운트 (fallback용)"""
    open_codes = (await db.execute(
        select(Trade.code).where(and_(
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
        )).distinct()
    )).scalars().all()

    active_codes = []
    for code in open_codes:
        buy_sids = (await db.execute(
            select(Trade.signal_id).where(and_(
                Trade.code == code,
                Trade.order_type == "BUY",
                Trade.status.in_(["FILLED", "PARTIAL"]),
            )).distinct()
        )).scalars().all()

        all_sold = True
        for sid in buy_sids:
            sold = (await db.execute(
                select(Trade).where(and_(
                    Trade.signal_id == sid,
                    Trade.order_type == "SELL",
                    Trade.status.in_(["FILLED", "CLOSED"]),  # CLOSED도 청산 처리
                ))
            )).scalars().first()
            if not sold:
                all_sold = False
                break

        if not all_sold:
            active_codes.append(code)

    active = len(active_codes)
    return active >= MAX_POSITIONS, active


# ── D. 슬리피지 체크 ──────────────────────────────────────────────────────────

async def check_slippage(
    signal_price: float,
    current_price: float,
) -> tuple[bool, float]:
    if signal_price <= 0:
        return False, 0.0
    slippage_pct = abs(current_price - signal_price) / signal_price
    exceeded     = slippage_pct > SLIPPAGE_LIMIT_PCT
    if exceeded:
        logger.warning(
            f"슬리피지 초과: 신호가 {signal_price:,} → 현재가 {current_price:,} "
            f"({slippage_pct*100:.2f}% > {SLIPPAGE_LIMIT_PCT*100:.1f}%)"
        )
    return exceeded, round(slippage_pct, 6)


# ── E. 과매매 방지 ─────────────────────────────────────────────────────────────

async def check_overtrading(db: AsyncSession, code: str) -> tuple[bool, str]:
    now_utc         = _utcnow()
    today_start_utc = _kst_today_start_utc()

    # 1) 하루 최대 거래 횟수 (signal 기준)
    day_count = (await db.execute(
        select(func.count(Trade.signal_id.distinct())).where(and_(
            Trade.created_at >= today_start_utc,
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
            Trade.phase == 1,  # 1차 매수만 카운트 (분할은 1건으로)
        ))
    )).scalar() or 0

    if day_count >= MAX_DAILY_TRADES:
        return True, f"일일 최대 거래 횟수 초과 ({day_count}/{MAX_DAILY_TRADES})"

    # 2) 손절 후 재진입 제한
    stop_cutoff_utc = now_utc - timedelta(minutes=STOP_REENTRY_MINUTES)
    recent_sell = (await db.execute(
        select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= stop_cutoff_utc,
        )).order_by(desc(Trade.created_at)).limit(1)
    )).scalars().first()

    if recent_sell:
        buy_trades = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == recent_sell.signal_id,
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            ))
        )).scalars().all()
        if buy_trades:
            avg_buy = sum(t.price * t.quantity for t in buy_trades) / sum(t.quantity for t in buy_trades)
            if recent_sell.price < avg_buy:
                elapsed = (now_utc - recent_sell.created_at).total_seconds() / 60
                remain  = max(0, int(STOP_REENTRY_MINUTES - elapsed))
                return True, f"손절 후 재진입 제한 (잔여 {remain}분)"

    # 3) 일반 재진입 제한
    reentry_cutoff_utc = now_utc - timedelta(minutes=REENTRY_MINUTES)
    recent_buy = (await db.execute(
        select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
            Trade.created_at >= reentry_cutoff_utc,
        ))
    )).scalars().first()

    if recent_buy:
        elapsed = (now_utc - recent_buy.created_at).total_seconds() / 60
        remain  = max(0, int(REENTRY_MINUTES - elapsed))
        return True, f"동일 종목 재진입 제한 (잔여 {remain}분)"

    return False, ""


# ── F. 수수료 계산 ─────────────────────────────────────────────────────────────

def calc_commission(price: float, quantity: int, is_buy: bool) -> float:
    rate = BUY_COMMISSION if is_buy else SELL_COMMISSION
    return round(price * quantity * rate, 0)


def calc_net_profit(buy_price: float, sell_price: float, quantity: int) -> dict:
    gross     = (sell_price - buy_price) * quantity
    buy_comm  = calc_commission(buy_price,  quantity, is_buy=True)
    sell_comm = calc_commission(sell_price, quantity, is_buy=False)
    slip_cost = sell_price * quantity * DEFAULT_SLIPPAGE
    net       = gross - buy_comm - sell_comm - slip_cost
    base      = buy_price * quantity
    return {
        "theory_profit":   round(gross, 0),
        "buy_commission":  round(buy_comm, 0),
        "sell_commission": round(sell_comm, 0),
        "slippage_cost":   round(slip_cost, 0),
        "net_profit":      round(net, 0),
        "net_profit_pct":  round(net / base * 100, 2) if base > 0 else 0,
    }


# ── G. KIS 실잔고 ↔ DB 포지션 싱크 ────────────────────────────────────────────

async def sync_positions_with_kis(db: AsyncSession) -> dict:
    """
    KIS 실제 보유 잔고와 DB 미청산 포지션을 비교해 좀비 포지션을 자동 정리.
    scheduler/service.py 에서 장 시작(09:05) / 장 종료(15:25) 시 호출 권장.
    """
    try:
        from trader import kis_client as kis
        balance = await kis.get_balance()
        holdings = balance.get("holdings", [])
    except Exception as e:
        logger.error(f"[SYNC] KIS 잔고 조회 실패: {e}")
        return {"error": str(e)}

    kis_codes = {h["code"] for h in holdings}

    # DB 미청산 종목 조회
    open_codes_result = (await db.execute(
        select(Trade.code).where(and_(
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
        )).distinct()
    )).scalars().all()

    db_open_codes = set()
    for code in open_codes_result:
        # 실제 미청산인지 재확인
        buy_sids = (await db.execute(
            select(Trade.signal_id).where(and_(
                Trade.code == code,
                Trade.order_type == "BUY",
                Trade.status.in_(["FILLED", "PARTIAL"]),
            )).distinct()
        )).scalars().all()

        for sid in buy_sids:
            sold = (await db.execute(
                select(Trade).where(and_(
                    Trade.signal_id == sid,
                    Trade.order_type == "SELL",
                    Trade.status.in_(["FILLED", "CLOSED"]),
                ))
            )).scalars().first()
            if not sold:
                db_open_codes.add(code)
                break

    # ① DB엔 보유 중 / KIS엔 없음 → 좀비 포지션 정리
    zombie_codes = db_open_codes - kis_codes
    fixed = []
    for code in zombie_codes:
        # 해당 종목의 SELL FAILED를 CLOSED로 보정
        await db.execute(
            Trade.__table__.update()
            .where(and_(
                Trade.code == code,
                Trade.order_type == "SELL",
                Trade.status == "FAILED",
            ))
            .values(status="CLOSED", notes="KIS 실잔고 기준 자동 보정")
        )
        await db.commit()
        fixed.append(code)
        logger.warning(f"[SYNC] 좀비 포지션 정리: {code} → SELL FAILED→CLOSED 보정")

    # ② KIS엔 있음 / DB엔 없음 → 미추적 포지션 알림
    untracked = kis_codes - db_open_codes
    if untracked:
        logger.error(f"[SYNC] 미추적 포지션 발견: {untracked}")

    result = {
        "kis_holdings":   list(kis_codes),
        "db_open":        list(db_open_codes),
        "zombie_fixed":   fixed,
        "untracked":      list(untracked),
    }

    if fixed or untracked:
        msg_lines = ["⚙️ <b>[AI INVEST] KIS-DB 포지션 싱크 완료</b>\n━━━━━━━━━━━━━━━━━━"]
        if fixed:
            msg_lines.append(f"✅ 좀비 정리: {', '.join(fixed)}")
        if untracked:
            msg_lines.append(f"⚠️ 미추적 포지션: {', '.join(untracked)} (수동 확인 필요)")
        await send_message("\n".join(msg_lines))

    return result


# ── 통합 매수 가능 체크 ────────────────────────────────────────────────────────

async def can_buy(db: AsyncSession, code: str = "") -> tuple[bool, str]:
    if not is_market_open():
        return False, "장 외 시간"

    loss_hit, pnl = await check_daily_loss(db)
    if loss_hit:
        return False, f"일일 손실 한도 초과 ({pnl:+,}원)"

    pos_hit, cnt = await check_max_positions(db)
    if pos_hit:
        return False, f"최대 보유 종목 수 초과 ({cnt}/{MAX_POSITIONS})"

    if code:
        # 블랙리스트 체크
        is_bl, bl_reason = await check_blacklist(db, code)
        if is_bl:
            return False, bl_reason

        over, reason = await check_overtrading(db, code)
        if over:
            return False, reason

    return True, ""


async def get_risk_status(db: AsyncSession) -> dict:
    """현재 리스크 상태 요약 (미실현 손익 + 블랙리스트 포함)"""
    today_start_utc = _kst_today_start_utc()

    stmt = select(Trade).where(and_(
        Trade.order_type == "SELL",
        Trade.status == "FILLED",
        Trade.created_at >= today_start_utc,
    ))
    sells = (await db.execute(stmt)).scalars().all()

    realized = 0
    for sell in sells:
        buy_trades = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == sell.signal_id,
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            ))
        )).scalars().all()
        if buy_trades:
            total_qty = sum(t.quantity for t in buy_trades)
            avg_price = sum(t.price * t.quantity for t in buy_trades) / total_qty
            realized += (sell.price - avg_price) * sell.quantity - sum(
                (t.commission or 0) for t in buy_trades
            ) - (sell.commission or 0)

    # 블랙리스트 현황
    now_utc = _utcnow()
    bl_count = (await db.execute(
        select(func.count(StockBlacklist.id)).where(
            StockBlacklist.expires_at > now_utc
        )
    )).scalar() or 0

    unrealized      = await calc_unrealized_pnl(db)
    _, pos_cnt      = await check_max_positions(db)
    market_open     = is_market_open()
    buyable, reason = await can_buy(db)

    return {
        "market_open":        market_open,
        "can_buy":            buyable,
        "block_reason":       reason,
        "today_pnl":          int(realized + unrealized),
        "realized_pnl":       int(realized),
        "unrealized_pnl":     int(unrealized),
        "daily_loss_limit":   -DAILY_LOSS_LIMIT,
        "positions":          pos_cnt,
        "max_positions":      MAX_POSITIONS,
        "daily_limit_hit":    _get_limit_cache(),
        "max_daily_trades":   MAX_DAILY_TRADES,
        "slippage_limit":     f"{SLIPPAGE_LIMIT_PCT*100:.1f}%",
        "commission_rate":    f"{BUY_COMMISSION*100:.3f}%",
        "simulation_mode":    SIMULATION_MODE,
        "blacklisted_stocks": bl_count,
        "blacklist_days":     BLACKLIST_DAYS,
    }
