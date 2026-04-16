"""
Strategy Guard – 전략 보호 필터

1. 시장 상황 필터  : 코스피 하락 추세 시 매수 중단
2. 최대 보유 기간  : N일 초과 시 자동 청산
3. 재매수 쿨타임   : 동일 종목 손절 후 N시간 재매수 금지
4. 오후 진입 제한  : 14:30 이후 신규 진입 차단 / 14:00~14:29 엄격 조건 적용
5. 외국인/기관 수급 필터

[개선 v4]
  - is_market_bullish(): 단순 상승일 카운트 → MA20 + 최근 수익률 기반으로 강화
      기존: 최근 N일 중 상승일 ≥ 절반 → 등락률 크기 무시, 급락일에도 통과
      수정: (1) 코스피 종가 > 20일 이동평균  AND
            (2) 최근 MARKET_FILTER_DAYS일 수익률 > -2.0%
            양쪽 모두 충족 시 상승 추세 판정 → 급락장 대응 강화

  - filter_signals(): 오후 진입 시간 제한 추가
      14:00~14:29 KST: 신뢰도 ≥ 0.65 AND 등락률 ≥ 1.5% 신호만 허용
      14:30 KST 이후 : 신규 진입 전면 차단
      이유: 14:30 진입 포지션은 마감(15:20)까지 50분밖에 없어 당일 청산 불가 시
            익일 갭다운 리스크에 무방비 노출됨

  - _get_investor_flow_safe(): fail-open 방식 유지 + 캐시 적용
      기존: 매 종목마다 KIS API 호출
      수정: 당일 조회 결과를 인메모리 캐시에 저장, 동일 종목 재호출 방지
            장 시작 후 처음 조회 시만 API 호출, 이후는 캐시 반환
"""
import logging
import os
from datetime import datetime, timedelta, date

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

# ── 오후 진입 제한 파라미터 ────────────────────────────────────────────────────
AFTERNOON_STRICT_HOUR   = 14   # 14:00 이후 엄격 조건 적용
AFTERNOON_STRICT_MINUTE = 0
AFTERNOON_BLOCK_HOUR    = 14   # 14:30 이후 신규 진입 전면 차단
AFTERNOON_BLOCK_MINUTE  = 30
AFTERNOON_MIN_CONFIDENCE = float(os.getenv("AFTERNOON_MIN_CONFIDENCE", "0.65"))
AFTERNOON_MIN_CHANGE     = float(os.getenv("AFTERNOON_MIN_CHANGE",     "1.5"))

# ── 외국인/기관 수급 필터 파라미터 ────────────────────────────────────────────
FILTER_INVESTOR_FLOW_ENABLED = os.getenv("FILTER_INVESTOR_FLOW_ENABLED", "false").lower() == "true"
INVESTOR_SCORE_MIN           = float(os.getenv("INVESTOR_SCORE_MIN", "0.3"))

# ── 수급 데이터 당일 캐시 (종목당 KIS API 호출 1회로 제한) ─────────────────────
_investor_flow_cache: dict[str, dict] = {}
_investor_flow_cache_date: date | None = None


def _reset_investor_cache_if_new_day() -> None:
    global _investor_flow_cache, _investor_flow_cache_date
    today = datetime.now(KST).date()
    if _investor_flow_cache_date != today:
        _investor_flow_cache      = {}
        _investor_flow_cache_date = today


# ── 1. 시장 상황 필터 ─────────────────────────────────────────────────────────

async def is_market_bullish() -> bool:
    """
    코스피 지수 상승 추세 여부 확인 (강화된 판단 로직).

    [개선 v4] MA20 + 최근 수익률 기반으로 변경
      기존: 최근 N일 중 상승일 수 ≥ N/2 (등락률 크기 무시)
            → 코스피 -3% 급락일 + 나머지 소폭 상승이면 통과하는 문제
      수정: 두 조건을 모두 만족해야 상승 추세로 판단
            (1) 현재 종가 > 20일 단순 이동평균 (중기 추세 확인)
            (2) 최근 MARKET_FILTER_DAYS일 수익률 > -2.0% (단기 급락 감지)
            둘 중 하나라도 실패하면 하락 추세로 판정 → 매수 중단
    """
    try:
        end   = datetime.now(KST).date()
        start = end - timedelta(days=60)  # MA20 계산에 충분한 데이터 확보
        df    = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        if df is None or len(df) < 22:
            logger.warning("코스피 데이터 부족 — 시장 필터 통과 처리")
            return True

        closes     = list(df["Close"])
        last_close = closes[-1]

        # 조건 1: 현재 종가 > MA20
        ma20       = sum(closes[-20:]) / 20
        above_ma20 = last_close >= ma20

        # 조건 2: 최근 N일 등락률 > -2.0% (급락 감지)
        lookback = min(MARKET_FILTER_DAYS + 1, len(closes))
        base_close    = closes[-lookback]
        recent_return = (last_close / base_close - 1) * 100 if base_close > 0 else 0.0
        no_sharp_fall = recent_return > -2.0

        # 최종 판단
        bullish = above_ma20 and no_sharp_fall

        # 보조 정보 (로그용)
        recent_closes = closes[-MARKET_FILTER_DAYS:] if len(closes) >= MARKET_FILTER_DAYS else closes
        up_days = sum(1 for i in range(1, len(recent_closes)) if recent_closes[i] > recent_closes[i-1])

        logger.info(
            f"시장 필터 — 코스피 {last_close:,.0f}p / MA20 {ma20:,.0f}p "
            f"({'MA위' if above_ma20 else 'MA아래'}) / "
            f"최근{MARKET_FILTER_DAYS}일 {recent_return:+.1f}% "
            f"({'급락없음' if no_sharp_fall else '급락감지'}) / "
            f"상승{up_days}일 → {'📈 상승' if bullish else '📉 하락'} 추세"
        )
        return bullish

    except Exception as e:
        logger.warning(f"시장 필터 오류: {e} — 필터 통과 처리")
        return True


# ── 2. 재매수 쿨타임 확인 ─────────────────────────────────────────────────────

async def is_in_cooltime(db: AsyncSession, code: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=COOLTIME_HOURS)

    stmt = (
        select(Trade)
        .where(and_(
            Trade.code == code,
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= cutoff,
        ))
        .order_by(desc(Trade.created_at))
        .limit(1)
    )
    recent_sell = (await db.execute(stmt)).scalars().first()

    if recent_sell:
        buy_stmt = (
            select(Trade)
            .where(and_(
                Trade.code == code,
                Trade.order_type == "BUY",
                Trade.signal_id == recent_sell.signal_id,
            ))
        )
        buy = (await db.execute(buy_stmt)).scalars().first()
        if buy and recent_sell.price < buy.price:
            elapsed = (datetime.utcnow() - recent_sell.created_at).total_seconds() / 3600
            remain  = max(0, COOLTIME_HOURS - elapsed)
            logger.info(f"[{code}] 쿨타임 중 (잔여 약 {remain:.0f}시간)")
            return True

    return False


# ── 3. 최대 보유 기간 초과 청산 ───────────────────────────────────────────────

async def check_and_close_expired_positions(db: AsyncSession) -> list[dict]:
    """MAX_HOLD_DAYS 초과 보유 포지션을 자동 청산합니다."""
    from trader import kis_client as kis
    import uuid

    cutoff = datetime.utcnow() - timedelta(days=MAX_HOLD_DAYS)

    stmt = (
        select(Trade)
        .where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
            Trade.created_at <= cutoff,
        ))
    )
    old_trades = (await db.execute(stmt)).scalars().all()

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
            price_data    = await kis.get_current_price(trade.code)
            current_price = price_data["price"]
        except Exception:
            current_price = trade.price

        try:
            result = await kis.sell_order(trade.code, trade.quantity, order_type="01")
        except Exception as e:
            logger.error(f"기간 만료 청산 실패 [{trade.code}]: {e}")
            continue

        trade_id = str(uuid.uuid4())
        pnl      = (current_price - trade.price) * trade.quantity
        pnl_pct  = (current_price / trade.price - 1) * 100

        await db.execute(
            Trade.__table__.insert().values(
                id=trade_id,
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
            f"📌 종목: <b>{trade.name} ({trade.code})</b>\n"
            f"📅 보유기간: {MAX_HOLD_DAYS}일 초과\n"
            f"💰 매수가: {trade.price:,}원\n"
            f"💱 매도가: {current_price:,}원\n"
            f"📊 수익률: {pnl_pct:+.2f}%\n"
            f"💵 손익: {pnl:+,.0f}원"
        )

        closed.append({
            "code": trade.code, "name": trade.name,
            "pnl": round(pnl), "pnl_pct": round(pnl_pct, 2),
        })
        logger.info(f"기간 만료 청산: {trade.code} {trade.name} {pnl_pct:+.2f}%")

    await db.commit()
    return closed


# ── 4. 외국인/기관 수급 조회 (캐시 적용) ─────────────────────────────────────

async def _get_investor_flow_safe(code: str) -> dict:
    """
    외국인/기관 수급 데이터를 안전하게 조회합니다.

    [개선 v4] 당일 인메모리 캐시 적용
      기존: 매 종목마다 매번 KIS API 호출
      수정: 당일 첫 조회 후 캐시 저장, 이후 캐시 반환
            → 동일 종목이 여러 스캔에서 반복 등장해도 API 호출 1회로 제한
            → 날짜가 바뀌면 캐시 자동 초기화

    fail-open: API 오류 발생 시 score=1.0 반환 → 해당 종목 필터 통과 처리
               (데이터 없음으로 인한 매수 기회 손실 방지)
    """
    _reset_investor_cache_if_new_day()

    # 캐시 히트
    if code in _investor_flow_cache:
        cached = _investor_flow_cache[code]
        logger.debug(f"[{code}] 수급 캐시 사용: 점수={cached['score']:.1f}")
        return cached

    # 캐시 미스 → API 호출
    try:
        from trader import kis_client as kis
        flow = await kis.get_investor_flow(code)
        _investor_flow_cache[code] = flow
        return flow
    except Exception as e:
        logger.warning(f"[{code}] 수급 조회 실패 — 필터 통과 처리: {e}")
        # fail-open: API 오류 시 통과 (score=1.0)
        fallback = {"foreign_net": 0, "institution_net": 0, "score": 1.0}
        _investor_flow_cache[code] = fallback
        return fallback


# ── 5. 통합 필터 ──────────────────────────────────────────────────────────────

async def filter_signals(db: AsyncSession, signals: list[dict]) -> list[dict]:
    """
    신호 목록에 모든 필터를 순서대로 적용해 유효한 신호만 반환합니다.

    필터 적용 순서:
      1. 오후 시간 제한  (14:00~14:29 엄격 / 14:30+ 전면 차단)
      2. 시장 상황 필터  (코스피 MA20 + 급락 감지)
      3. 쿨타임 필터     (손절 후 N시간 재진입 금지)
      4. 수급 필터       (외국인/기관 순매수 여부, 활성화 시)
    """
    if not signals:
        return []

    # ── 필터 1: 오후 진입 시간 제한 ──────────────────────────────────────────
    now_kst  = datetime.now(KST)
    now_hour = now_kst.hour
    now_min  = now_kst.minute

    # 14:30 이후: 전면 차단
    if now_hour > AFTERNOON_BLOCK_HOUR or (
        now_hour == AFTERNOON_BLOCK_HOUR and now_min >= AFTERNOON_BLOCK_MINUTE
    ):
        logger.info(
            f"[오후진입제한] {now_kst.strftime('%H:%M')} KST — "
            f"14:30 이후 신규 진입 전면 차단 ({len(signals)}건 신호 무효화)"
        )
        await send_message(
            f"🕒 <b>[AI INVEST] 오후 진입 차단</b>\n"
            f"시각: {now_kst.strftime('%H:%M')} KST\n"
            f"사유: 14:30 이후 당일 청산 시간 부족 — 익일 갭다운 리스크 방지\n"
            f"신호 {len(signals)}건 무효화"
        )
        return []

    # 14:00~14:29: 엄격 조건 (고신뢰도 + 강한 등락률 신호만)
    if now_hour >= AFTERNOON_STRICT_HOUR:
        before_strict = len(signals)
        signals = [
            s for s in signals
            if (s.get("confidence", 0) >= AFTERNOON_MIN_CONFIDENCE
                and s.get("change_rate", 0) >= AFTERNOON_MIN_CHANGE)
        ]
        blocked = before_strict - len(signals)
        if blocked > 0:
            logger.info(
                f"[오후진입제한] {now_kst.strftime('%H:%M')} KST — "
                f"엄격 조건 적용 (신뢰도≥{AFTERNOON_MIN_CONFIDENCE} AND "
                f"등락률≥{AFTERNOON_MIN_CHANGE}%): "
                f"{before_strict}건 → {len(signals)}건 ({blocked}건 차단)"
            )
        if not signals:
            return []

    # ── 필터 2: 시장 상황 필터 ────────────────────────────────────────────────
    if not await is_market_bullish():
        logger.info("시장 하락 추세 — 전체 신호 필터링")
        await send_message(
            f"⚠️ <b>[AI INVEST] 시장 필터 작동</b>\n"
            f"코스피 하락 추세 감지 (MA20 이하 또는 최근 급락)\n"
            f"신호 {len(signals)}건 차단 — 오늘 신규 매수 중단"
        )
        return []

    # ── 필터 3: 쿨타임 필터 ──────────────────────────────────────────────────
    after_cooltime = []
    for sig in signals:
        code = sig["code"]
        if await is_in_cooltime(db, code):
            logger.info(f"[{code}] 쿨타임 — 건너뜀")
            continue
        after_cooltime.append(sig)

    if not after_cooltime:
        return []

    # ── 필터 4: 외국인/기관 수급 필터 ────────────────────────────────────────
    if FILTER_INVESTOR_FLOW_ENABLED:
        flow_passed = []
        blocked_cnt = 0

        for sig in after_cooltime:
            code = sig["code"]
            flow = await _get_investor_flow_safe(code)

            # Signal DB에 수급 데이터 기록 (분석용)
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
                logger.warning(f"[{code}] 수급 Signal 저장 실패 (무시): {e}")

            if flow["score"] >= INVESTOR_SCORE_MIN:
                flow_passed.append(sig)
                logger.info(
                    f"[{code}] ✅ 수급 통과 — "
                    f"외국인 {flow['foreign_net']:+,}주 / "
                    f"기관 {flow['institution_net']:+,}주 / "
                    f"점수 {flow['score']:.1f}"
                )
            else:
                blocked_cnt += 1
                logger.info(
                    f"[{code}] ❌ 수급 탈락 — "
                    f"외국인 {flow['foreign_net']:+,}주 / "
                    f"기관 {flow['institution_net']:+,}주 / "
                    f"점수 {flow['score']:.1f} < {INVESTOR_SCORE_MIN}"
                )

        try:
            await db.flush()
        except Exception as e:
            logger.warning(f"수급 flush 실패 (무시): {e}")

        if blocked_cnt > 0:
            await send_message(
                f"📊 <b>[AI INVEST] 수급 필터 적용</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔍 검사: {len(after_cooltime)}개 종목\n"
                f"✅ 통과: {len(flow_passed)}개\n"
                f"❌ 탈락: {blocked_cnt}개 (외국인·기관 매도 우위)\n"
                f"기준: 수급점수 ≥ {INVESTOR_SCORE_MIN}"
            )

        filtered = flow_passed
        logger.info(
            f"필터 최종: {len(signals)}건 → "
            f"시간제한 후 {len(after_cooltime) + blocked_cnt}건 → "
            f"수급 후 {len(filtered)}건"
        )

    else:
        filtered = after_cooltime
        logger.info(
            f"필터 최종: {len(signals)}건 → 쿨타임 후 {len(filtered)}건 "
            f"(수급 필터 비활성)"
        )

    return filtered
