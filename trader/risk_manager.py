"""
Risk Manager — Phase 3/4/5/6 통합 + 미실현 손익 포함 + 블랙리스트

타임존 규칙:
  - DB 쿼리용 datetime: naive UTC (datetime.utcnow())
  - 화면/로직용 datetime: KST aware (datetime.now(KST))

[버그 수정 v2] _daily_limit_hit 인메모리 → DB 기반으로 전환
[개선 v3]     can_buy skip_position_check / WebSocket 캐시
[버그 수정 v4] can_buy에 skip_overtrading_check 파라미터 추가
  문제: 2차 분할매수가 REENTRY_MINUTES(120분) 제한에 걸려 차단됨
        "[035420] 2차 매수 차단: 동일 종목 재진입 제한 (잔여 97분)"
  원인: can_buy() → check_overtrading() → 일반 재진입 제한 적용
        2차 매수는 신규 진입이 아닌 기존 포지션 추가이므로 재진입 제한 불필요
  수정: skip_overtrading_check=True 시 check_overtrading() 건너뜀
        → 1차 매수: skip_overtrading_check=False (기본, 재진입 제한 적용)
        → 2차 매수: skip_overtrading_check=True  (기존 포지션 추가, 제한 없음)
        단, 일일 손실 한도 / 블랙리스트 체크는 2차 매수에도 항상 적용
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
    now_kst   = datetime.now(KST)
    today_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today_kst.astimezone(pytz.utc).replace(tzinfo=None)
    return today_utc


def _utcnow() -> datetime:
    return datetime.utcnow()


# ── 환경변수 ──────────────────────────────────────────────────────────────────
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

# ── 일일 한도 캐시 ────────────────────────────────────────────────────────────
_daily_limit_cache: dict = {"hit": False, "date": None}


def _get_limit_cache() -> bool:
    today = datetime.now(KST).date()
    if _daily_limit_cache["date"] != today:
        _daily_limit_cache["hit"]  = False
        _daily_limit_cache["date"] = None
    return _daily_limit_cache["hit"]


def _set_limit_cache(hit: bool) -> None:
    _daily_limit_cache["hit"]  = hit
    _daily_limit_cache["date"] = datetime.now(KST).date()


def reset_daily_flag():
    today = datetime.now(KST).date()
    if _daily_limit_cache["date"] != today:
        _daily_limit_cache["hit"]  = False
        _daily_limit_cache["date"] = None


# ── 장 운영 시간 ──────────────────────────────────────────────────────────────

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


# ── 블랙리스트 ────────────────────────────────────────────────────────────────

async def add_to_blacklist(
    db: AsyncSession,
    code: str,
    name: str,
    reason: str = "",
) -> None:
    now_utc    = _utcnow()
    expires_at = now_utc + timedelta(days=BLACKLIST_DAYS)

    existing = (await db.execute(
        select(StockBlacklist).where(StockBlacklist.code == code)
    )).scalars().first()

    if existing:
        await db.execute(
            StockBlacklist.__table__.update()
            .where(StockBlacklist.code == code)
            .values(reason=reason, blacklisted_at=now_utc, expires_at=expires_at)
        )
    else:
        await db.execute(
            StockBlacklist.__table__.insert().values(
                code=code, name=name, reason=reason,
                blacklisted_at=now_utc, expires_at=expires_at,
            )
        )
    await db.commit()
    logger.info(f"블랙리스트 등록: {code} {name} ({BLACKLIST_DAYS}일, {reason})")


async def check_blacklist(db: AsyncSession, code: str) -> tuple[bool, str]:
    now_utc = _utcnow()
    entry   = (await db.execute(
        select(StockBlacklist).where(and_(
            StockBlacklist.code == code,
            StockBlacklist.expires_at > now_utc,
        ))
    )).scalars().first()

    if entry:
        remain_days = (entry.expires_at - now_utc).days
        return True, f"블랙리스트 ({remain_days}일 잔여, {entry.reason})"
    return False, ""


# ── 일일 손실 한도 ────────────────────────────────────────────────────────────

async def calc_unrealized_pnl(db: AsyncSession) -> int:
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
                Trade.status.in_(["FILLED", "CLOSED"]),   # ← CLOSED 추가
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

        total_qty = sum(t.quantity for t in buy_trades)
        avg_price = sum(t.price * t.quantity for t in buy_trades) / total_qty
        code      = buy_trades[0].code

        if is_market_open():
            current_price = 0
            try:
                from trader.ws_client import get_cached_price
                cached = get_cached_price(code)
                if cached and cached > 0:
                    current_price = cached
                    logger.debug(f"[미실현손익] {code} WS캐시: {current_price:,}원")
            except Exception:
                pass
            if current_price <= 0:
                try:
                    from trader import kis_client as kis
                    current_price = (await kis.get_current_price(code)).get("price", 0)
                    logger.debug(f"[미실현손익] {code} REST fallback: {current_price:,}원")
                except Exception as e:
                    logger.warning(f"[미실현손익] {code} 현재가 조회 실패: {e}")
            if current_price > 0:
                unrealized += (current_price - avg_price) * total_qty

    return int(unrealized)


async def _calc_today_pnl(db: AsyncSession) -> tuple[int, int]:
    today_start_utc = _kst_today_start_utc()
    sells = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= today_start_utc,
        ))
    )).scalars().all()

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
    reset_daily_flag()
    cache_already_hit = _get_limit_cache()

    realized_pnl, unrealized_pnl = await _calc_today_pnl(db)
    total_pnl = realized_pnl + unrealized_pnl

    logger.debug(
        f"일일 손익 — 실현: {realized_pnl:+,}원 / "
        f"미실현: {unrealized_pnl:+,}원 / 합계: {total_pnl:+,}원"
    )

    if total_pnl <= -DAILY_LOSS_LIMIT:
        if not cache_already_hit:
            logger.warning(f"일일 손실 한도 초과: {total_pnl:,}원")
            await send_message(
                f"🚨 <b>[AI INVEST] 일일 손실 한도 초과</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📉 실현: {realized_pnl:+,}원 / 📊 미실현: {unrealized_pnl:+,}원\n"
                f"💥 합계: <b>{total_pnl:+,}원</b> / 한도: -{DAILY_LOSS_LIMIT:,}원\n"
                f"⛔ 오늘 신규 매수를 중단합니다."
            )
        else:
            logger.debug(f"일일 손실 한도 초과 유지 중: {total_pnl:,}원 (알림 생략)")
        _set_limit_cache(True)
        return True, total_pnl

    _set_limit_cache(False)
    return False, total_pnl


# ── 동시 보유 종목 수 ─────────────────────────────────────────────────────────

async def check_max_positions(db: AsyncSession) -> tuple[bool, int]:
    try:
        from trader import kis_client as kis
        balance  = await kis.get_balance()
        holdings = balance.get("holdings", [])
        active   = len(holdings)
        logger.debug(f"[포지션] KIS 실잔고: {active}개 {[h['code'] for h in holdings]}")
        return active >= MAX_POSITIONS, active
    except Exception as e:
        logger.warning(f"[포지션] KIS 잔고 조회 실패 → DB fallback: {e}")
        return await _check_max_positions_db(db)


async def _check_max_positions_db(db: AsyncSession) -> tuple[bool, int]:
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
                    Trade.status.in_(["FILLED", "CLOSED"]),
                ))
            )).scalars().first()
            if not sold:
                all_sold = False
                break

        if not all_sold:
            active_codes.append(code)

    active = len(active_codes)
    return active >= MAX_POSITIONS, active


# ── 슬리피지 체크 ─────────────────────────────────────────────────────────────

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


# ── 과매매 방지 ───────────────────────────────────────────────────────────────

async def check_overtrading(db: AsyncSession, code: str) -> tuple[bool, str]:
    now_utc         = _utcnow()
    today_start_utc = _kst_today_start_utc()

    # 1) 하루 최대 거래 횟수 (1차 매수만 카운트)
    day_count = (await db.execute(
        select(func.count(Trade.signal_id.distinct())).where(and_(
            Trade.created_at >= today_start_utc,
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
            Trade.phase == 1,
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


# ── 수수료 계산 ───────────────────────────────────────────────────────────────

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


# ── KIS 실잔고 ↔ DB 포지션 싱크 ──────────────────────────────────────────────

async def sync_positions_with_kis(db: AsyncSession) -> dict:
    """
    KIS 실잔고 ↔ DB 포지션 동기화.

    처리 케이스:
      1. 좀비 포지션 (DB OPEN, KIS 없음)
         - SELL 레코드 있고 FAILED → CLOSED 보정
         - SELL 레코드 없음 → KIS 평균단가 기준 SELL CLOSED 삽입 + 수익 계산
      2. 미추적 포지션 (KIS 있음, DB OPEN 없음) → 텔레그램 경고
    """
    import uuid as _uuid
    try:
        from trader import kis_client as kis
        balance  = await kis.get_balance()
        holdings = balance.get("holdings", [])
    except Exception as e:
        logger.error(f"[SYNC] KIS 잔고 조회 실패: {e}")
        return {"error": str(e)}

    kis_map   = {h["code"]: h for h in holdings}   # code → holding info
    kis_codes = set(kis_map.keys())

    # DB에서 미청산 포지션 수집
    open_codes_result = (await db.execute(
        select(Trade.code).where(and_(
            Trade.order_type == "BUY",
            Trade.status.in_(["FILLED", "PARTIAL"]),
        )).distinct()
    )).scalars().all()

    db_open_codes = set()
    db_open_sids: dict[str, list] = {}   # code → [signal_id, ...]

    for code in open_codes_result:
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
                db_open_sids.setdefault(code, []).append(sid)

    zombie_codes = db_open_codes - kis_codes
    fixed = []

    for code in zombie_codes:
        for sid in db_open_sids.get(code, []):
            # 1) SELL FAILED 레코드가 있으면 → CLOSED 보정
            failed_sell = (await db.execute(
                select(Trade).where(and_(
                    Trade.signal_id == sid,
                    Trade.order_type == "SELL",
                    Trade.status == "FAILED",
                ))
            )).scalars().first()

            if failed_sell:
                await db.execute(
                    Trade.__table__.update()
                    .where(and_(
                        Trade.signal_id == sid,
                        Trade.order_type == "SELL",
                        Trade.status == "FAILED",
                    ))
                    .values(status="CLOSED", notes="KIS 실잔고 기준 자동 보정")
                )
                logger.warning(f"[SYNC] SELL FAILED → CLOSED 보정: {code} (signal={sid[:8]})")

            else:
                # 2) SELL 레코드 자체가 없음 → 수동 매도된 것
                #    KIS 현재가(혹은 직전 평균단가)로 수익 계산 후 SELL CLOSED 삽입
                buy_trades = (await db.execute(
                    select(Trade).where(and_(
                        Trade.signal_id == sid,
                        Trade.order_type == "BUY",
                        Trade.status == "FILLED",
                    ))
                )).scalars().all()

                if not buy_trades:
                    continue

                total_qty   = sum(t.quantity for t in buy_trades)
                total_cost  = sum(t.price * t.quantity for t in buy_trades)
                avg_buy     = total_cost / total_qty if total_qty else 0

                # KIS 현재가 조회 시도 (장 시간 외 실패 시 매수가 사용)
                try:
                    price_data   = await kis.get_current_price(code)
                    sell_price   = price_data.get("price") or int(avg_buy)
                except Exception:
                    sell_price   = int(avg_buy)

                sell_amount  = sell_price * total_qty
                pnl          = (sell_price - avg_buy) * total_qty
                profit_pct   = (sell_price / avg_buy - 1) * 100 if avg_buy else 0

                await db.execute(
                    Trade.__table__.insert().values(
                        id=str(_uuid.uuid4()),
                        signal_id=sid,
                        code=code,
                        name=buy_trades[0].name,
                        order_type="SELL",
                        status="CLOSED",
                        price=sell_price,
                        quantity=total_qty,
                        amount=sell_amount,
                        real_profit=round(pnl, 0),
                        filled_at=datetime.utcnow(),
                        notes=f"수동매도 자동감지 (추정가 {sell_price:,}원 / 수익 {profit_pct:+.2f}%)",
                    )
                )
                logger.warning(
                    f"[SYNC] 수동매도 감지 → SELL CLOSED 삽입: {code} "
                    f"{total_qty}주 추정가 {sell_price:,}원 ({profit_pct:+.2f}%)"
                )

        await db.commit()
        fixed.append(code)

    untracked = kis_codes - db_open_codes
    if untracked:
        logger.error(f"[SYNC] 미추적 포지션 발견: {untracked}")

    result = {
        "kis_holdings": list(kis_codes),
        "db_open":      list(db_open_codes),
        "zombie_fixed": fixed,
        "untracked":    list(untracked),
    }

    if fixed or untracked:
        msg_lines = ["⚙️ <b>[AI INVEST] KIS-DB 포지션 싱크</b>\n━━━━━━━━━━━━━━━━━━"]
        if fixed:
            msg_lines.append(f"✅ 좀비 정리: {', '.join(fixed)}")
        if untracked:
            msg_lines.append(f"⚠️ 미추적 포지션: {', '.join(untracked)} (수동 확인 필요)")
        await send_message("\n".join(msg_lines))

    return result


async def can_buy(
    db: AsyncSession,
    code: str = "",
    skip_position_check: bool = False,
    skip_overtrading_check: bool = False,
) -> tuple[bool, str]:
    """
    매수 가능 여부 통합 체크.

    Args:
        skip_position_check:    True이면 MAX_POSITIONS 체크 건너뜀
                                → 2차 분할매수는 기존 포지션 추가이므로 불필요
        skip_overtrading_check: True이면 과매매 체크(재진입 제한) 건너뜀
                                → 2차 분할매수는 신규 진입이 아니므로 재진입 제한 불필요
                                → 일일 손실 한도 / 블랙리스트는 항상 체크

    호출 패턴:
        1차 매수: can_buy(db)                              — 모든 체크
        2차 매수: can_buy(db, code, True, True)            — 포지션·과매매 체크 건너뜀
    """
    if not is_market_open():
        return False, "장 외 시간"

    # 일일 손실 한도 — 2차 매수에도 항상 적용
    loss_hit, pnl = await check_daily_loss(db)
    if loss_hit:
        return False, f"일일 손실 한도 초과 ({pnl:+,}원)"

    # 포지션 수 체크 — skip_position_check=True 시 건너뜀 (2차 매수용)
    if not skip_position_check:
        pos_hit, cnt = await check_max_positions(db)
        if pos_hit:
            return False, f"최대 보유 종목 수 초과 ({cnt}/{MAX_POSITIONS})"

    if code:
        # 블랙리스트 — 2차 매수에도 항상 적용
        is_bl, bl_reason = await check_blacklist(db, code)
        if is_bl:
            return False, bl_reason

        # 과매매 방지(재진입 제한) — skip_overtrading_check=True 시 건너뜀 (2차 매수용)
        if not skip_overtrading_check:
            over, reason = await check_overtrading(db, code)
            if over:
                return False, reason

    return True, ""


async def get_risk_status(db: AsyncSession) -> dict:
    today_start_utc = _kst_today_start_utc()

    sells = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= today_start_utc,
        ))
    )).scalars().all()

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

    now_utc  = _utcnow()
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
