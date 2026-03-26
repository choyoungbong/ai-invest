"""
Auto Trader – 신호 발생 시 자동 매수 실행

설정:
  AUTO_TRADE_ENABLED: true/false (기본 true)
  MAX_AMOUNT_PER_STOCK: 종목당 최대 투자금액 (기본 300000원)
"""
import logging
import os
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from api.models import Signal, Trade
from trader import kis_client as kis
from notification.service import send_message

logger = logging.getLogger(__name__)

AUTO_TRADE_ENABLED   = os.getenv("AUTO_TRADE_ENABLED", "true").lower() == "true"
MAX_AMOUNT_PER_STOCK = int(os.getenv("MAX_AMOUNT_PER_STOCK", "300000"))


def calc_quantity(price: float, max_amount: int = MAX_AMOUNT_PER_STOCK) -> int:
    """최대 금액 이내에서 최대 수량을 계산합니다."""
    if price <= 0:
        return 0
    qty = int(max_amount // price)
    return max(qty, 1)


async def auto_execute_signals(db: AsyncSession, signals: list[dict]) -> list[dict]:
    """
    신호 목록을 받아 자동 매수 주문을 실행합니다.

    - 종목당 최대 MAX_AMOUNT_PER_STOCK 원 이내
    - 이미 보유 중인 종목은 중복 매수 안 함
    - AUTO_TRADE_ENABLED=false 이면 주문 없이 로그만 출력
    """
    if not signals:
        return []

    if not AUTO_TRADE_ENABLED:
        logger.info("AUTO_TRADE_ENABLED=false — 자동 주문 비활성화 상태")
        return []

    # ── 리스크 매니저 통합 체크 ───────────────────────────────────────────────
    from trader.risk_manager import can_buy
    buyable, reason = await can_buy(db)
    if not buyable:
        logger.info(f"매수 차단: {reason}")
        await send_message(
            f"⛔ <b>[AI INVEST] 매수 차단</b>\n"
            f"사유: {reason}"
        )
        return []

    executed = []

    for sig in signals:
        code       = sig["code"]
        name       = sig["name"]
        signal_id  = sig["id"]
        price      = sig["price"]

        # ── 이미 보유 중인지 확인 ──────────────────────────────────────────
        already = (await db.execute(
            select(Trade).where(and_(
                Trade.code == code,
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
            ))
        )).scalars().first()

        # 이미 보유 중이면 매도됐는지 확인
        if already:
            sold = (await db.execute(
                select(Trade).where(and_(
                    Trade.code == code,
                    Trade.order_type == "SELL",
                    Trade.signal_id == already.signal_id,
                ))
            )).scalars().first()
            if not sold:
                logger.info(f"[{code}] 이미 보유 중 — 중복 매수 건너뜀")
                continue

        # ── 현재가 조회 ───────────────────────────────────────────────────
        try:
            price_data    = await kis.get_current_price(code)
            current_price = price_data["price"] or int(price)
        except Exception as e:
            logger.warning(f"[{code}] 현재가 조회 실패: {e} — 신호가 사용")
            current_price = int(price)

        # ── 최대 금액 초과 종목 제외 ──────────────────────────────────────
        if current_price > MAX_AMOUNT_PER_STOCK:
            logger.info(
                f"[{code}] {name} 현재가 {current_price:,}원 > "
                f"최대금액 {MAX_AMOUNT_PER_STOCK:,}원 — 건너뜀"
            )
            await send_message(
                f"⏭️ <b>[AI INVEST] 매수 건너뜀</b>\n"
                f"📌 {name} ({code})\n"
                f"💰 현재가 {current_price:,}원 > 최대 {MAX_AMOUNT_PER_STOCK:,}원"
            )
            continue

        # ── 수량 계산 ─────────────────────────────────────────────────────
        quantity = calc_quantity(current_price)
        if quantity <= 0:
            continue

        amount = current_price * quantity

        # ── KIS 매수 주문 ─────────────────────────────────────────────────
        try:
            result = await kis.buy_order(code, quantity, order_type="01")  # 시장가
        except Exception as e:
            logger.error(f"[{code}] 매수 주문 실패: {e}")
            continue

        # ── Trade 기록 저장 ───────────────────────────────────────────────
        trade_id = str(uuid.uuid4())
        status   = "FILLED" if result["success"] else "FAILED"

        await db.execute(
            Trade.__table__.insert().values(
                id=trade_id,
                signal_id=signal_id,
                code=code,
                name=name,
                order_type="BUY",
                price=current_price,
                quantity=quantity,
                amount=amount,
                status=status,
                broker_order_id=result.get("order_no", ""),
                filled_at=datetime.utcnow() if result["success"] else None,
            )
        )

        # 신호 실행 표시
        await db.execute(
            Signal.__table__.update()
            .where(Signal.id == signal_id)
            .values(is_executed=True)
        )

        trade_data = {
            "trade_id":  trade_id,
            "code":      code,
            "name":      name,
            "price":     current_price,
            "quantity":  quantity,
            "amount":    amount,
            "status":    status,
            "order_no":  result.get("order_no", ""),
        }
        executed.append(trade_data)

        # ── 텔레그램 매수 알림 ────────────────────────────────────────────
        target_price = round(current_price * (1 + float(os.getenv("TARGET_PROFIT_PCT", "0.03"))))
        stop_price   = round(current_price * (1 + float(os.getenv("STOP_LOSS_PCT", "-0.02"))))

        emoji = "✅" if result["success"] else "❌"
        await send_message(
            f"{emoji} <b>[AI INVEST] 자동 매수</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{name} ({code})</b>\n"
            f"💰 매수가: {current_price:,}원\n"
            f"🔢 수량: {quantity}주\n"
            f"💵 투자금액: {amount:,}원\n"
            f"🎯 목표가: {target_price:,}원 (+{float(os.getenv('TARGET_PROFIT_PCT', '0.03'))*100:.0f}%)\n"
            f"🛑 손절가: {stop_price:,}원 ({float(os.getenv('STOP_LOSS_PCT', '-0.02'))*100:.0f}%)\n"
            f"{'✅ 주문 성공' if result['success'] else '❌ 주문 실패'}"
        )

        logger.info(
            f"자동 매수: {code} {name} "
            f"{quantity}주 @ {current_price:,}원 = {amount:,}원"
        )

    await db.commit()
    logger.info(f"자동 매수 완료: {len(executed)}건")
    return executed
