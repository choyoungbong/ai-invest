"""
Notification – 텔레그램 알림 서비스

신호 발생 시 텔레그램 봇으로 메시지를 전송합니다.
"""
import logging
import os
import httpx

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE  = "https://api.telegram.org"


# ── 저수준 전송 ────────────────────────────────────────────────────────────────

async def send_message(text: str) -> bool:
    """텔레그램 메시지를 전송합니다. 성공 시 True 반환."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("텔레그램 설정 없음 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            logger.info("텔레그램 전송 성공")
            return True
    except Exception as e:
        logger.error(f"텔레그램 전송 실패: {e}")
        return False


# ── 신호 알림 ──────────────────────────────────────────────────────────────────

async def notify_signal(signal: dict) -> bool:
    """BUY/SELL 신호를 텔레그램으로 전송합니다."""
    emoji = "🟢" if signal.get("signal_type") == "BUY" else "🔴"
    confidence = signal.get("confidence", 0)
    stars = "⭐" * round(confidence * 5)

    text = (
        f"{emoji} <b>[AI INVEST] {signal.get('signal_type')} 신호</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 종목: <b>{signal.get('name')} ({signal.get('code')})</b>\n"
        f"💰 현재가: <b>{signal.get('price', 0):,.0f}원</b>\n"
        f"🎯 목표가: {signal.get('target_price', 0):,.0f}원\n"
        f"🛑 손절가: {signal.get('stop_loss', 0):,.0f}원\n"
        f"📊 전략: {signal.get('strategy', '')}\n"
        f"🔮 신뢰도: {stars} ({confidence:.0%})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {signal.get('reason', '')}"
    )
    return await send_message(text)


async def notify_signals_summary(signals: list) -> bool:
    """전략 실행 결과 요약을 전송합니다."""
    if not signals:
        return await send_message("🔍 <b>[AI INVEST]</b> 스캔 완료 — 신호 없음")

    lines = [f"📣 <b>[AI INVEST] 신호 {len(signals)}건 발생</b>\n━━━━━━━━━━━━━━━━━━"]
    for s in signals:
        emoji = "🟢" if s.get("signal_type") == "BUY" else "🔴"
        lines.append(
            f"{emoji} {s.get('name')} ({s.get('code')})  "
            f"{s.get('price', 0):,.0f}원  "
            f"→ 목표 {s.get('target_price', 0):,.0f}원"
        )
    return await send_message("\n".join(lines))


async def notify_trade(trade: dict) -> bool:
    """주문 생성 알림을 전송합니다."""
    emoji = "🛒" if trade.get("order_type") == "BUY" else "💸"
    text = (
        f"{emoji} <b>[AI INVEST] 주문 생성</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 종목: <b>{trade.get('name')} ({trade.get('code')})</b>\n"
        f"📋 유형: {trade.get('order_type')}\n"
        f"💰 가격: {trade.get('price', 0):,.0f}원\n"
        f"🔢 수량: {trade.get('quantity')}주\n"
        f"💵 총액: {trade.get('amount', 0):,.0f}원\n"
        f"🆔 주문ID: {trade.get('trade_id', '')[:8]}..."
    )
    return await send_message(text)


async def notify_test() -> bool:
    """연결 테스트 메시지를 전송합니다."""
    return await send_message(
        "✅ <b>[AI INVEST]</b> 텔레그램 알림 연결 성공!\n"
        "신호 발생 시 이 채널로 알림이 전송됩니다."
    )
