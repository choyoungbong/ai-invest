"""
Strategy Guard – 전략 보호 필터

필터 적용 순서 (filter_signals 기준):
  1. 오후 시간 제한   : 14:30+ 전면 차단 / 14:00~14:29 엄격 조건
  2. 시장 상황 필터   : 코스피 MA20 + 최근 수익률 기반 (강화 v4)
  3. 쿨타임 필터      : 손절 후 N시간 동일 종목 재진입 금지
  4. DART 공시 필터   : 당일 악재 공시 종목 차단 (v4 신규)
  5. 수급 필터        : 외국인/기관 순매수 여부

[A단계 개선 — DART 공시 필터 (v4 신규)]
  문제: 기술적 신호 발생 직후 유상증자·감사의견 비적정 등 악재 공시로 급락
  해결: DART OpenAPI(opendart.fss.or.kr)로 당일 공시 조회, 악재 키워드 차단

  동작:
    - DART_API_KEY 환경변수 미설정 시 자동 비활성 (fail-open)
    - 당일 인메모리 캐시로 종목당 API 호출 1회 제한
    - 악재 키워드: 유상증자, 무상감자, 감사의견, 불성실공시, 상장폐지,
                  영업정지, 횡령, 배임, 조회공시
    - API 오류 시 fail-open (해당 종목 통과 — 기회 손실 방지)

  DART API 키 발급:
    https://opendart.fss.or.kr → 회원가입 → API 신청 (무료)

환경변수:
  DART_API_KEY             : DART OpenAPI 인증 키 (없으면 필터 비활성)
  DISCLOSURE_FILTER_ENABLED: DART 필터 활성화 여부 (기본 true, 키 있을 때만 동작)
"""
import logging
import os
from datetime import datetime, timedelta, date

import httpx
import pytz
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

import FinanceDataReader as fdr

from api.models import Trade, Signal
from notification.service import send_message

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# ── 파라미터 ──────────────────────────────────────────────────────────────────
MAX_HOLD_DAYS      = int(os.getenv("MAX_HOLD_DAYS",      "3"))
COOLTIME_HOURS     = int(os.getenv("COOLTIME_HOURS",     "24"))
MARKET_FILTER_DAYS = int(os.getenv("MARKET_FILTER_DAYS", "3"))

AFTERNOON_STRICT_HOUR    = 14
AFTERNOON_STRICT_MINUTE  = 0
AFTERNOON_BLOCK_HOUR     = 14
AFTERNOON_BLOCK_MINUTE   = 30
AFTERNOON_MIN_CONFIDENCE = float(os.getenv("AFTERNOON_MIN_CONFIDENCE", "0.65"))
AFTERNOON_MIN_CHANGE     = float(os.getenv("AFTERNOON_MIN_CHANGE",     "1.5"))

FILTER_INVESTOR_FLOW_ENABLED = os.getenv("FILTER_INVESTOR_FLOW_ENABLED", "false").lower() == "true"
INVESTOR_SCORE_MIN           = float(os.getenv("INVESTOR_SCORE_MIN", "0.3"))

DART_API_KEY              = os.getenv("DART_API_KEY", "")
DISCLOSURE_FILTER_ENABLED = os.getenv("DISCLOSURE_FILTER_ENABLED", "true").lower() == "true"

_BAD_DISCLOSURE_KEYWORDS = [
    "유상증자", "무상감자", "감사의견", "불성실공시",
    "상장폐지", "영업정지", "횡령", "배임", "조회공시",
]

# ── 당일 캐시 ─────────────────────────────────────────────────────────────────
_disclosure_cache:      dict[str, tuple[bool, str]] = {}
_disclosure_cache_date: date | None = None
_investor_flow_cache:      dict[str, dict] = {}
_investor_flow_cache_date: date | None = None


def _reset_caches_if_new_day() -> None:
    global _disclosure_cache, _disclosure_cache_date
    global _investor_flow_cache, _investor_flow_cache_date
    today = datetime.now(KST).date()
    if _disclosure_cache_date != today:
        _disclosure_cache      = {}
        _disclosure_cache_date = today
    if _investor_flow_cache_date != today:
        _investor_flow_cache      = {}
        _investor_flow_cache_date = today


# ── 1. 시장 상황 필터 (MA20 기반) ────────────────────────────────────────────

async def is_market_bullish() -> bool:
    """
    코스피 MA20 + 최근 수익률 기반 상승 추세 판단.
    두 조건 모두 충족 시 상승 추세:
      (1) 현재 종가 >= 20일 이동평균
      (2) 최근 MARKET_FILTER_DAYS일 수익률 > -2.0%
    """
    try:
        end   = datetime.now(KST).date()
        start = end - timedelta(days=60)
        df    = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        if df is None or len(df) < 22:
            logger.warning("코스피 데이터 부족 — 시장 필터 통과")
            return True

        closes     = list(df["Close"])
        last_close = closes[-1]
        ma20       = sum(closes[-20:]) / 20
        above_ma20 = last_close >= ma20

        lookback      = min(MARKET_FILTER_DAYS + 1, len(closes))
        recent_return = (last_close / closes[-lookback] - 1) * 100 if closes[-lookback] else 0.0
        no_sharp_fall = recent_return > -2.0

        bullish = above_ma20 and no_sharp_fall

        recent = closes[-MARKET_FILTER_DAYS:] if len(closes) >= MARKET_FILTER_DAYS else closes
        up_days = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])

        logger.info(
            f"시장 필터 — 코스피 {last_close:,.0f}p / MA20 {ma20:,.0f}p "
            f"({'MA위' if above_ma20 else 'MA아래'}) / "
            f"최근{MARKET_FILTER_DAYS}일 {recent_return:+.1f}% "
            f"({'급락없음' if no_sharp_fall else '급락감지'}) / "
            f"상승{up_days}일 → {'📈 상승' if bullish else '📉 하락'} 추세"
        )
        return bullish

    except Exception as e:
        logger.warning(f"시장 필터 오류: {e} — 필터 통과")
        return True


# ── 2. 재매수 쿨타임 ─────────────────────────────────────────────────────────

async def is_in_cooltime(db: AsyncSession, code: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=COOLTIME_HOURS)
    recent_sell = (await db.execute(
        select(Trade).where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= cutoff,
        )).order_by(desc(Trade.created_at)).limit(1)
    )).scalars().first()

    if recent_sell:
        buy = (await db.execute(
            select(Trade).where(and_(
                Trade.code == code,
                Trade.order_type == "BUY",
                Trade.signal_id == recent_sell.signal_id,
            ))
        )).scalars().first()
        if buy and recent_sell.price < buy.price:
            elapsed = (datetime.utcnow() - recent_sell.created_at).total_seconds() / 3600
            logger.info(f"[{code}] 쿨타임 중 (잔여 약 {max(0, COOLTIME_HOURS - elapsed):.0f}시간)")
            return True
    return False


# ── 3. 최대 보유 기간 초과 청산 ──────────────────────────────────────────────

async def check_and_close_expired_positions(db: AsyncSession) -> list[dict]:
    from trader import kis_client as kis
    import uuid

    cutoff     = datetime.utcnow() - timedelta(days=MAX_HOLD_DAYS)
    old_trades = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
            Trade.created_at <= cutoff,
        ))
    )).scalars().all()

    closed = []
    for trade in old_trades:
        sold = (await db.execute(
            select(Trade).where(and_(
                Trade.code == trade.code,
                Trade.order_type == "SELL",
                Trade.signal_id == trade.signal_id,
            ))
        )).scalars().first()
        if sold:
            continue

        try:
            current_price = (await kis.get_current_price(trade.code))["price"]
        except Exception:
            current_price = trade.price

        try:
            result = await kis.sell_order(trade.code, trade.quantity, order_type="01")
        except Exception as e:
            logger.error(f"기간 만료 청산 실패 [{trade.code}]: {e}")
            continue

        pnl     = (current_price - trade.price) * trade.quantity
        pnl_pct = (current_price / trade.price - 1) * 100

        await db.execute(
            Trade.__table__.insert().values(
                id=str(uuid.uuid4()),
                signal_id=trade.signal_id,
                code=trade.code,
                name=trade.name,
                order_type="SELL",
                price=current_price,
                quantity=trade.quantity,
                amount=current_price * trade.quantity,
                status="FILLED" if result["success"] else "FAILED",
                broker_order_id=result.get("order_no", ""),
                filled_at=datetime.utcnow(),
            )
        )

        await send_message(
            f"⏰ <b>[AI INVEST] 보유기간 만료 청산</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{trade.name} ({trade.code})</b>\n"
            f"📅 {MAX_HOLD_DAYS}일 초과 / 매수가 {trade.price:,}원 → 매도가 {current_price:,}원\n"
            f"📊 수익률: {pnl_pct:+.2f}% / 손익: {pnl:+,.0f}원"
        )

        closed.append({"code": trade.code, "name": trade.name,
                       "pnl": round(pnl), "pnl_pct": round(pnl_pct, 2)})
        logger.info(f"기간 만료 청산: {trade.code} {trade.name} {pnl_pct:+.2f}%")

    await db.commit()
    return closed


# ── 4. DART 공시 필터 (v4 신규) ──────────────────────────────────────────────

async def _get_dart_corp_code(stock_code: str) -> str | None:
    """주식 종목 코드 → DART 법인 고유 코드 변환 (당일 캐시 적용)"""
    cache_key = f"_corpcode_{stock_code}"
    if cache_key in _disclosure_cache:
        stored = _disclosure_cache[cache_key]
        return stored[1] if stored[0] is None else None  # (None, corp_code) 형태 저장

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res  = await client.get(
                "https://opendart.fss.or.kr/api/company.json",
                params={"crtfc_key": DART_API_KEY, "stock_code": stock_code},
            )
            data = res.json()

        if data.get("status") == "000":
            corp_code = data["corp_code"]
            _disclosure_cache[cache_key] = (None, corp_code)  # (None=특수마커, corp_code)
            return corp_code

    except Exception as e:
        logger.debug(f"[{stock_code}] DART 법인코드 조회 실패: {e}")

    return None


async def _check_dart_disclosure(code: str) -> tuple[bool, str]:
    """
    당일 DART 공시에서 악재 키워드 포함 여부 확인.

    Returns: (has_bad: bool, reason: str)
    fail-open: 오류 시 항상 (False, "") 반환
    """
    _reset_caches_if_new_day()

    if code in _disclosure_cache:
        cached = _disclosure_cache[code]
        # 법인코드 캐시 항목 제외 (tuple[0]이 None인 경우)
        if cached[0] is not None:
            return cached

    if not DART_API_KEY:
        return False, ""

    corp_code = await _get_dart_corp_code(code)
    if not corp_code:
        result = (False, "")
        _disclosure_cache[code] = result
        return result

    try:
        today = datetime.now(KST).strftime("%Y%m%d")
        async with httpx.AsyncClient(timeout=5) as client:
            res  = await client.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key":  DART_API_KEY,
                    "corp_code":  corp_code,
                    "bgn_de":     today,
                    "end_de":     today,
                    "page_count": 20,
                },
            )
            data = res.json()

        if data.get("status") == "000":
            for d in data.get("list", []):
                report_nm = d.get("report_nm", "")
                for kw in _BAD_DISCLOSURE_KEYWORDS:
                    if kw in report_nm:
                        reason = f"당일 악재 공시: {report_nm[:40]}"
                        result = (True, reason)
                        _disclosure_cache[code] = result
                        logger.info(f"[{code}] DART 차단 — {reason}")
                        return result

        result = (False, "")
        _disclosure_cache[code] = result
        return result

    except Exception as e:
        logger.warning(f"[{code}] DART 공시 조회 실패 — 통과: {e}")
        result = (False, "")
        _disclosure_cache[code] = result
        return result


# ── 5. 수급 조회 (캐시 적용) ─────────────────────────────────────────────────

async def _get_investor_flow_safe(code: str) -> dict:
    _reset_caches_if_new_day()
    if code in _investor_flow_cache:
        return _investor_flow_cache[code]
    try:
        from trader import kis_client as kis
        flow = await kis.get_investor_flow(code)
        _investor_flow_cache[code] = flow
        return flow
    except Exception as e:
        logger.warning(f"[{code}] 수급 조회 실패 — 통과: {e}")
        fallback = {"foreign_net": 0, "institution_net": 0, "score": 1.0}
        _investor_flow_cache[code] = fallback
        return fallback


# ── 통합 필터 ─────────────────────────────────────────────────────────────────

async def filter_signals(db: AsyncSession, signals: list[dict]) -> list[dict]:
    """
    신호 목록에 모든 필터를 순서대로 적용합니다.

    순서: 오후제한 → 시장필터 → 쿨타임 → DART공시 → 수급
    """
    if not signals:
        return []

    _reset_caches_if_new_day()

    # ── 1. 오후 시간 제한 ─────────────────────────────────────────────────────
    now_kst  = datetime.now(KST)
    now_hour, now_min = now_kst.hour, now_kst.minute

    if now_hour > AFTERNOON_BLOCK_HOUR or (
        now_hour == AFTERNOON_BLOCK_HOUR and now_min >= AFTERNOON_BLOCK_MINUTE
    ):
        logger.info(f"[오후차단] {now_kst.strftime('%H:%M')} — {len(signals)}건 전면 차단")
        await send_message(
            f"🕒 <b>[AI INVEST] 오후 진입 차단</b>\n"
            f"시각: {now_kst.strftime('%H:%M')} KST (14:30 이후)\n"
            f"신호 {len(signals)}건 무효화 — 익일 갭다운 리스크 방지"
        )
        return []

    if now_hour >= AFTERNOON_STRICT_HOUR:
        before  = len(signals)
        signals = [
            s for s in signals
            if (s.get("confidence", 0) >= AFTERNOON_MIN_CONFIDENCE
                and s.get("change_rate", 0) >= AFTERNOON_MIN_CHANGE)
        ]
        blocked = before - len(signals)
        if blocked:
            logger.info(f"[오후엄격] {now_kst.strftime('%H:%M')} — {before}건 → {len(signals)}건 ({blocked}건 차단)")
        if not signals:
            return []

    # ── 2. 시장 필터 ──────────────────────────────────────────────────────────
    if not await is_market_bullish():
        await send_message(
            f"⚠️ <b>[AI INVEST] 시장 필터 작동</b>\n"
            f"코스피 하락 추세 (MA20 이하 또는 최근 급락)\n"
            f"신호 {len(signals)}건 차단 — 오늘 신규 매수 중단"
        )
        return []

    # ── 3. 쿨타임 필터 ────────────────────────────────────────────────────────
    after_cooltime = [s for s in signals if not await is_in_cooltime(db, s["code"])]
    if not after_cooltime:
        return []

    # ── 4. DART 공시 필터 ─────────────────────────────────────────────────────
    dart_active = DISCLOSURE_FILTER_ENABLED and bool(DART_API_KEY)
    if dart_active:
        after_dart   = []
        dart_blocked = []

        for sig in after_cooltime:
            has_bad, reason = await _check_dart_disclosure(sig["code"])
            if has_bad:
                dart_blocked.append(f"{sig['name']}({sig['code']}): {reason}")
            else:
                after_dart.append(sig)

        if dart_blocked:
            await send_message(
                f"📋 <b>[AI INVEST] DART 공시 필터 작동</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🚫 차단 {len(dart_blocked)}건:\n"
                + "\n".join(f"  • {b}" for b in dart_blocked[:5])
                + (f"\n  외 {len(dart_blocked)-5}건" if len(dart_blocked) > 5 else "")
            )

        logger.info(
            f"DART 공시 필터: {len(after_dart) + len(dart_blocked)}건 → "
            f"{len(after_dart)}건 통과 / {len(dart_blocked)}건 차단"
        )
        after_cooltime = after_dart
        if not after_cooltime:
            return []
    else:
        if DISCLOSURE_FILTER_ENABLED and not DART_API_KEY:
            logger.debug("DART 공시 필터: DART_API_KEY 미설정 — 비활성")

    # ── 5. 수급 필터 ──────────────────────────────────────────────────────────
    if FILTER_INVESTOR_FLOW_ENABLED:
        flow_passed = []
        blocked_cnt = 0

        for sig in after_cooltime:
            code = sig["code"]
            flow = await _get_investor_flow_safe(code)

            try:
                await db.execute(
                    Signal.__table__.update()
                    .where(Signal.id == sig["id"])
                    .values(
                        foreign_net_buy=flow["foreign_net"],
                        institution_net_buy=flow["institution_net"],
                        investor_score=flow["score"],
                    )
                )
            except Exception as e:
                logger.warning(f"[{code}] 수급 Signal 저장 실패: {e}")

            if flow["score"] >= INVESTOR_SCORE_MIN:
                flow_passed.append(sig)
                logger.info(f"[{code}] ✅ 수급 통과 점수:{flow['score']:.1f}")
            else:
                blocked_cnt += 1
                logger.info(f"[{code}] ❌ 수급 탈락 점수:{flow['score']:.1f} < {INVESTOR_SCORE_MIN}")

        try:
            await db.flush()
        except Exception:
            pass

        if blocked_cnt:
            await send_message(
                f"📊 <b>[AI INVEST] 수급 필터</b>\n"
                f"검사 {len(after_cooltime)}건 / 통과 {len(flow_passed)}건 / 탈락 {blocked_cnt}건\n"
                f"기준: 수급점수 ≥ {INVESTOR_SCORE_MIN}"
            )
        filtered = flow_passed
    else:
        filtered = after_cooltime

    logger.info(
        f"필터 최종: {len(signals)}건 → {len(filtered)}건 통과 "
        f"(DART={'활성' if dart_active else '비활성'}, "
        f"수급={'활성' if FILTER_INVESTOR_FLOW_ENABLED else '비활성'})"
    )
    return filtered
