"""
AI Analysis – Claude AI 기반 신호 분석 엔진

신호 데이터 + 최근 시세를 Claude에게 전달하고
투자 판단에 도움이 되는 상세 분석 리포트를 생성합니다.
"""
import logging
import os
import httpx
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_

from api.models import Signal, MarketData, Stock

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-sonnet-4-20250514"


# ── Claude API 호출 ────────────────────────────────────────────────────────────

async def _call_claude(prompt: str) -> str:
    """Claude API를 호출하고 텍스트 응답을 반환합니다."""
    if not ANTHROPIC_API_KEY:
        return "⚠️ ANTHROPIC_API_KEY 가 설정되지 않았습니다. .env 파일을 확인하세요."

    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      MODEL,
        "max_tokens": 1024,
        "messages":   [{"role": "user", "content": prompt}],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)
            res.raise_for_status()
            data = res.json()
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude API 오류: {e}")
        return f"AI 분석 실패: {e}"


# ── 시세 히스토리 조회 ─────────────────────────────────────────────────────────

async def _get_price_history(db: AsyncSession, code: str, days: int = 20) -> list[dict]:
    """최근 N일 시세를 조회합니다."""
    cutoff = datetime.utcnow() - timedelta(days=days + 5)
    stmt = (
        select(MarketData)
        .where(and_(MarketData.code == code, MarketData.timestamp >= cutoff))
        .order_by(desc(MarketData.timestamp))
        .limit(days)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "date":          r.timestamp.strftime("%Y-%m-%d"),
            "open":          r.open,
            "high":          r.high,
            "low":           r.low,
            "close":         r.close,
            "volume":        r.volume,
            "trading_value": r.trading_value,
            "change_rate":   r.change_rate,
        }
        for r in rows
    ]


# ── 프롬프트 생성 ──────────────────────────────────────────────────────────────

def _build_prompt(signal: dict, history: list[dict]) -> str:
    today = history[0] if history else {}
    past  = history[1:] if len(history) > 1 else []

    past_highs  = [r["high"]  for r in past if r["high"]]
    past_values = [r["trading_value"] for r in past if r["trading_value"]]

    n_day_high  = max(past_highs)  if past_highs  else 0
    avg_value   = sum(past_values) / len(past_values) if past_values else 0
    value_ratio = today.get("trading_value", 0) / avg_value if avg_value else 0

    history_text = "\n".join(
        f"  {r['date']}: 종가 {r['close']:,.0f}원  등락 {r['change_rate']:+.1f}%  "
        f"거래대금 {r['trading_value']/100_000_000:.0f}억"
        for r in history[:10]
    )

    return f"""당신은 한국 주식 시장 전문 애널리스트입니다.
아래 데이터를 바탕으로 이 종목의 투자 신호를 분석해주세요.

## 신호 정보
- 종목: {signal['name']} ({signal['code']})
- 신호 유형: {signal['signal_type']}
- 전략: {signal['strategy']} (돌파매매)
- 신호 발생가: {signal['price']:,.0f}원
- 목표가: {signal['target_price']:,.0f}원 (+{(signal['target_price']/signal['price']-1)*100:.1f}%)
- 손절가: {signal['stop_loss']:,.0f}원 ({(signal['stop_loss']/signal['price']-1)*100:.1f}%)
- 전략 시스템 신뢰도: {signal['confidence']:.0%}

## 핵심 지표
- 당일 종가: {today.get('close', 0):,.0f}원
- 당일 고가: {today.get('high', 0):,.0f}원
- 20일 최고가: {n_day_high:,.0f}원
- 신고가 돌파 폭: +{(today.get('high',0)/n_day_high-1)*100:.1f}% (신고가 대비)
- 당일 거래대금: {today.get('trading_value',0)/100_000_000:.0f}억원
- 20일 평균 거래대금: {avg_value/100_000_000:.0f}억원
- 거래대금 배수: {value_ratio:.1f}배
- 당일 등락률: {today.get('change_rate', 0):+.1f}%

## 최근 10일 시세
{history_text}

## 분석 요청
다음 항목을 한국어로 간결하게 작성해주세요 (총 400자 이내):

1. **신호 강도** (강/중/약): 이 신호가 얼마나 신뢰할 수 있는지 한 줄 평가
2. **진입 근거**: 지금 매수를 고려할 수 있는 핵심 이유 2가지
3. **주의 사항**: 이 신호의 리스크 또는 주의할 점 1~2가지
4. **단기 시나리오**: 향후 3~5일 예상 흐름 (간략히)

투기적 표현을 피하고 데이터 기반으로 객관적으로 작성하세요."""


# ── 메인 분석 함수 ─────────────────────────────────────────────────────────────

async def analyze_signal(db: AsyncSession, signal_id: str) -> dict:
    """
    신호 ID를 받아 AI 분석 리포트를 생성합니다.
    결과는 Signal.reason 필드에 업데이트되고 dict로 반환됩니다.
    """
    # 신호 조회
    stmt = select(Signal).where(Signal.id == signal_id)
    row = (await db.execute(stmt)).scalars().first()
    if not row:
        return {"error": "신호를 찾을 수 없습니다"}

    sig = {
        "id":           row.id,
        "code":         row.code,
        "name":         row.name or row.code,
        "signal_type":  row.signal_type,
        "strategy":     row.strategy,
        "price":        row.price,
        "target_price": row.target_price,
        "stop_loss":    row.stop_loss,
        "confidence":   row.confidence or 0,
    }

    # 시세 히스토리
    history = await _get_price_history(db, row.code, days=20)

    if not history:
        return {"error": "시세 데이터 없음 — 먼저 시세를 수집하세요", "signal": sig}

    # Claude 분석
    prompt   = _build_prompt(sig, history)
    analysis = await _call_claude(prompt)

    # DB 업데이트 (reason 필드에 AI 분석 내용 저장)
    await db.execute(
        Signal.__table__.update()
        .where(Signal.id == signal_id)
        .values(reason=analysis)
    )
    await db.commit()

    return {
        "signal_id": signal_id,
        "code":      sig["code"],
        "name":      sig["name"],
        "analysis":  analysis,
        "analyzed_at": datetime.utcnow().isoformat(),
    }


async def analyze_all_new_signals(db: AsyncSession) -> list[dict]:
    """
    아직 AI 분석이 없는 최근 신호들을 일괄 분석합니다.
    reason 필드가 짧거나 기본 전략 텍스트인 것을 대상으로 합니다.
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    stmt = (
        select(Signal)
        .where(Signal.created_at >= cutoff)
        .order_by(desc(Signal.created_at))
        .limit(10)
    )
    rows = (await db.execute(stmt)).scalars().all()

    results = []
    for row in rows:
        # 이미 AI 분석된 것은 건너뜀 (AI 분석 결과는 "**신호 강도**"로 시작)
        if row.reason and "**신호 강도**" in row.reason:
            continue
        logger.info(f"AI 분석 중: {row.code} {row.name}")
        result = await analyze_signal(db, row.id)
        results.append(result)

    return results
