"""
US Auto Trader — 미국 ETF 자동매매 실행기

실행 흐름:
  1. 미국장 개장 시 비대상 종목(TORO, SNDL 등) 자동 청산
  2. 잔고 확인 → 당일 기준 총자산 기록
  3. 30분마다: 각 ETF 신호 생성 → 신호 있으면 매수
  4. 5분마다: 보유 포지션 손절/익절 체크

안정성:
  - US_TRADING_ENABLED=false 이면 모든 함수 즉시 return
  - 주문 실패 시 최대 2회 재시도
  - 재시도 모두 실패해도 다음 종목 계속 진행
  - DB는 기존 Trade 테이블 재사용 (code 필드로 구분)
"""
import logging
import os
import uuid
from datetime import datetime

import pytz
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from api.models import Trade
from notification.service import send_message

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

US_TRADING_ENABLED = os.getenv("US_TRADING_ENABLED", "false").lower() == "true"
US_TOTAL_BUDGET    = float(os.getenv("US_TOTAL_BUDGET_USD", "200"))


# ── 포지션 확인 ───────────────────────────────────────────────────────────────

async def _has_open_position(db: AsyncSession, symbol: str) -> bool:
    """해당 ETF의 미청산 매수 포지션 존재 여부"""
    buys = (await db.execute(
        select(Trade).where(and_(
            Trade.code == symbol,
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
        ))
    )).scalars().all()

    for buy in buys:
        sell = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == buy.signal_id,
                Trade.order_type == "SELL",
                Trade.status.in_(["FILLED", "CLOSED"]),
            ))
        )).scalars().first()
        if not sell:
            return True
    return False


# ── 투자금/수량 계산 ──────────────────────────────────────────────────────────

async def _calc_order_qty(symbol: str, budget_ratio: float) -> tuple[float, int]:
    """
    실잔고 기반 매수 수량 계산.
    예수금의 budget_ratio 비율로 투자, 최대 US_TOTAL_BUDGET × ratio.
    """
    from trader.us_kis_client import get_us_balance, get_us_price

    balance    = await get_us_balance()
    cash_usd   = balance.get("cash_usd", 0)
    price_data = await get_us_price(symbol)
    price      = price_data["price"]

    if price <= 0 or cash_usd <= 0:
        return 0.0, 0

    # 실잔고 기준 배분 (수수료 5% 여유 확보)
    alloc    = min(cash_usd * budget_ratio, US_TOTAL_BUDGET * budget_ratio) * 0.95
    quantity = int(alloc // price)

    if quantity <= 0:
        logger.warning(
            f"[US] {symbol} 수량 부족 — "
            f"예수금 ${cash_usd:.2f} × {budget_ratio:.0%} = ${alloc:.2f} "
            f"/ 현재가 ${price:.2f}"
        )
        return 0.0, 0

    return round(price * quantity, 2), quantity


# ── 매수 실행 ─────────────────────────────────────────────────────────────────

async def _execute_buy(db: AsyncSession, signal: dict) -> dict:
    """매수 주문 실행 (재시도 2회)"""
    from trader.us_kis_client import us_buy_order

    symbol       = signal["symbol"]
    budget_ratio = signal.get("budget_ratio", 0.33)

    amount_usd, quantity = await _calc_order_qty(symbol, budget_ratio)
    if quantity <= 0:
        return {}

    result = {}
    for attempt in range(1, 3):
        try:
            result = await us_buy_order(symbol, quantity)
            if result.get("success"):
                break
            logger.warning(f"[US] {symbol} 매수 재시도 {attempt}: {result.get('message')}")
        except Exception as e:
            logger.error(f"[US] {symbol} 매수 오류 {attempt}: {e}")

    if not result.get("success"):
        await send_message(
            f"❌ <b>[AI INVEST 🇺🇸] 미국 매수 실패</b>\n"
            f"📌 {symbol} {quantity}주\n"
            f"사유: {result.get('message', '알 수 없음')}"
        )
        return {}

    signal_id = str(uuid.uuid4())
    trade_id  = str(uuid.uuid4())
    price     = signal["price"]

    await db.execute(
        Trade.__table__.insert().values(
            id=trade_id,
            signal_id=signal_id,
            code=symbol,
            name=signal["name"],
            order_type="BUY",
            price=price,
            quantity=quantity,
            amount=amount_usd,
            commission=round(amount_usd * 0.00015, 4),
            phase=1,
            status="FILLED",
            broker_order_id=result.get("order_no", ""),
            filled_at=datetime.utcnow(),
            is_simulation=False,
        )
    )
    await db.commit()

    now_kst = datetime.now(KST).strftime("%H:%M")
    await send_message(
        f"✅ <b>[AI INVEST 🇺🇸] 미국 ETF 매수</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>{symbol}</b> — {signal['name']}\n"
        f"💰 매수가: ${price:.2f}\n"
        f"🔢 수량: {quantity}주 / 투자금: ${amount_usd:.2f}\n"
        f"🎯 목표가: ${signal['target_price']:.2f} (+{signal['target_pct']*100:.1f}%)\n"
        f"🛑 손절가: ${signal['stop_loss']:.2f} ({signal['stop_pct']*100:.1f}%)\n"
        f"📊 RSI:{signal.get('rsi')} Vol:{signal.get('vol_mult')}x\n"
        f"🕐 {now_kst} KST"
    )

    logger.info(
        f"[US] 매수: {symbol} {quantity}주 @ ${price:.2f} = ${amount_usd:.2f}"
    )
    return {
        "symbol": symbol, "price": price,
        "quantity": quantity, "amount": amount_usd,
        "signal_id": signal_id, "success": True,
    }


# ── 비대상 종목 자동 청산 ─────────────────────────────────────────────────────

async def auto_liquidate_on_open() -> list[dict]:
    """
    미국장 개장 시 자동 실행.
    자동매매 대상(SPLG, TQQQ, SOXL)이 아닌 기존 보유 종목을 전량 청산합니다.
    청산 완료 후 당일 기준 총자산을 기록합니다.
    """
    from trader.us_kis_client import (
        get_us_balance,
        liquidate_non_target_holdings,
    )
    from strategy.us_strategy import (
        US_ETF_CONFIG,
        set_initial_value,
        mark_liquidation_done,
        is_liquidation_done,
    )

    if not US_TRADING_ENABLED:
        return []

    if is_liquidation_done():
        logger.info("[US] 이미 오늘 청산 완료 — 건너뜀")
        return []

    balance = await get_us_balance()
    set_initial_value(balance["total_usd"])

    target_symbols = list(US_ETF_CONFIG.keys())
    results        = await liquidate_non_target_holdings(target_symbols)

    mark_liquidation_done()

    if results:
        sold    = [r for r in results if r["success"]]
        failed  = [r for r in results if not r["success"]]
        pnl_str = "\n".join(
            f"  • {r['symbol']} {r['quantity']}주 | 손익 {r['pnl_pct']:+.1f}%"
            for r in sold
        )
        failed_str = (
            "\n⚠️ 실패: " + ", ".join(r["symbol"] for r in failed)
            if failed else ""
        )
        await send_message(
            f"🧹 <b>[AI INVEST 🇺🇸] 비대상 종목 청산 완료</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_str or '없음'}"
            f"{failed_str}\n"
            f"💵 잔여 예수금: ${balance['cash_usd']:.2f}\n"
            f"📊 이후 SPLG / TQQQ / SOXL 자동매매 시작"
        )
    else:
        await send_message(
            f"🇺🇸 <b>[AI INVEST] 미국장 개장</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ 청산할 비대상 종목 없음\n"
            f"💵 예수금: ${balance['cash_usd']:.2f}\n"
            f"📊 SPLG / TQQQ / SOXL 자동매매 시작"
        )

    return results


# ── 전체 ETF 스캔 + 매수 ──────────────────────────────────────────────────────

async def run_us_trading(db: AsyncSession) -> list[dict]:
    """
    미국 ETF 전체 스캔 및 매수 실행.
    스케줄러에서 30분마다 호출합니다.
    """
    from trader.us_kis_client import is_us_market_open
    from strategy.us_strategy import US_ETF_CONFIG, generate_signal

    if not US_TRADING_ENABLED:
        return []

    if not is_us_market_open():
        return []

    executed = []

    for symbol in US_ETF_CONFIG.keys():
        if await _has_open_position(db, symbol):
            logger.debug(f"[US] {symbol} 보유 중 — 건너뜀")
            continue

        try:
            signal = await generate_signal(symbol)
        except Exception as e:
            logger.error(f"[US] {symbol} 신호 생성 오류: {e}")
            continue

        if not signal:
            continue

        result = await _execute_buy(db, signal)
        if result:
            executed.append(result)

    return executed


# ── 손절/익절 체크 ────────────────────────────────────────────────────────────

async def check_us_positions(db: AsyncSession) -> list[dict]:
    """
    보유 미국 ETF 포지션 손절/익절 체크.
    스케줄러에서 5분마다 호출합니다.
    """
    from trader.us_kis_client import get_us_price, us_sell_order, is_us_market_open
    from strategy.us_strategy import US_ETF_CONFIG, record_trade_result

    if not US_TRADING_ENABLED or not is_us_market_open():
        return []

    us_symbols = list(US_ETF_CONFIG.keys())
    buy_trades = (await db.execute(
        select(Trade).where(and_(
            Trade.order_type == "BUY",
            Trade.status == "FILLED",
            Trade.code.in_(us_symbols),
        ))
    )).scalars().all()

    executed = []

    for trade in buy_trades:
        sold = (await db.execute(
            select(Trade).where(and_(
                Trade.signal_id == trade.signal_id,
                Trade.order_type == "SELL",
                Trade.status.in_(["FILLED", "CLOSED"]),
            ))
        )).scalars().first()
        if sold:
            continue

        cfg = US_ETF_CONFIG.get(trade.code)
        if not cfg:
            continue

        try:
            price_data    = await get_us_price(trade.code)
            current_price = price_data["price"]
        except Exception as e:
            logger.warning(f"[US] {trade.code} 현재가 실패: {e}")
            continue

        pnl_pct = (current_price / trade.price - 1)

        should_sell = pnl_pct >= cfg["target_profit"] or pnl_pct <= cfg["stop_loss"]
        if not should_sell:
            logger.debug(
                f"[US] {trade.code} 보유: ${trade.price:.2f}→${current_price:.2f} "
                f"({pnl_pct*100:+.2f}%) | "
                f"익절 +{cfg['target_profit']*100:.1f}% / 손절 {cfg['stop_loss']*100:.1f}%"
            )
            continue

        sell_reason = "익절" if pnl_pct > 0 else "손절"

        # 매도 실행 (재시도 2회)
        result = {}
        for attempt in range(1, 3):
            try:
                result = await us_sell_order(trade.code, trade.quantity)
                if result.get("success"):
                    break
                logger.warning(f"[US] {trade.code} 매도 재시도 {attempt}")
            except Exception as e:
                logger.error(f"[US] {trade.code} 매도 오류: {e}")

        status  = "FILLED" if result.get("success") else "FAILED"
        pnl_usd = (current_price - trade.price) * trade.quantity

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
                commission=round(current_price * trade.quantity * 0.00015, 4),
                real_profit=round(pnl_usd, 4),
                status=status,
                broker_order_id=result.get("order_no", ""),
                filled_at=datetime.utcnow() if result.get("success") else None,
                is_simulation=False,
            )
        )
        await db.commit()

        record_trade_result(trade.code, pnl_usd)

        executed.append({
            "symbol":  trade.code,
            "reason":  sell_reason,
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
        })

        emoji = "🎯" if pnl_pct > 0 else "🛑"
        now_kst = datetime.now(KST).strftime("%H:%M")
        await send_message(
            f"{emoji} <b>[AI INVEST 🇺🇸] {sell_reason}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{trade.code}</b>\n"
            f"💰 ${trade.price:.2f} → ${current_price:.2f}\n"
            f"📊 {pnl_pct*100:+.2f}% / ${pnl_usd:+.2f}\n"
            f"🕐 {now_kst} KST"
        )

        logger.info(
            f"[US] {sell_reason}: {trade.code} "
            f"{pnl_pct*100:+.2f}% / ${pnl_usd:+.2f}"
        )

    return executed
