"""
Report – 수익 리포트 서비스

일별/주별/월별 손익을 집계해 텔레그램으로 발송합니다.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from api.models import Trade, Signal
from notification.service import send_message

logger = logging.getLogger(__name__)

def now_kst_naive():
    """KST 기준 timezone 제거된 datetime 반환"""
    from pytz import timezone
    KST = timezone("Asia/Seoul")
    return datetime.now(KST).replace(tzinfo=None)

async def calc_pnl(db: AsyncSession, start: datetime, end: datetime) -> dict:
    """기간별 손익 계산"""
    # 매도 거래 조회
    stmt = (
        select(Trade)
        .where(and_(
            Trade.order_type == "SELL",
            Trade.status == "FILLED",
            Trade.created_at >= start,
            Trade.created_at <= end,
        ))
    )
    sell_trades = (await db.execute(stmt)).scalars().all()

    total_pnl    = 0.0
    win_count    = 0
    lose_count   = 0
    total_trades = len(sell_trades)
    details      = []

    for sell in sell_trades:
        # 연결된 매수 거래 조회
        buy_stmt = (
            select(Trade)
            .where(and_(
                Trade.code == sell.code,
                Trade.order_type == "BUY",
                Trade.status == "FILLED",
                Trade.signal_id == sell.signal_id,
            ))
        )
        buy = (await db.execute(buy_stmt)).scalars().first()
        if not buy:
            continue

        pnl     = (sell.price - buy.price) * sell.quantity
        pnl_pct = (sell.price / buy.price - 1) * 100
        total_pnl += pnl

        if pnl > 0:
            win_count += 1
        else:
            lose_count += 1

        details.append({
            "code":    sell.code,
            "name":    sell.name,
            "buy":     buy.price,
            "sell":    sell.price,
            "qty":     sell.quantity,
            "pnl":     round(pnl),
            "pnl_pct": round(pnl_pct, 2),
        })

    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0

    return {
        "total_pnl":    round(total_pnl),
        "total_trades": total_trades,
        "win_count":    win_count,
        "lose_count":   lose_count,
        "win_rate":     round(win_rate, 1),
        "details":      details,
    }


async def send_daily_report(db: AsyncSession):
    """일일 수익 리포트 텔레그램 발송"""
    now   = now_kst_naive()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = now

    pnl = await calc_pnl(db, today, end)

    # 신호 수
    sig_count = (await db.execute(
        select(func.count(Signal.id)).where(Signal.created_at >= today)
    )).scalar() or 0

    emoji = "📈" if pnl["total_pnl"] >= 0 else "📉"

    lines = [
        f"{emoji} <b>[AI INVEST] 일일 리포트</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"📅 {today.strftime('%Y-%m-%d')}",
        f"🟢 발생 신호: {sig_count}건",
        f"🛒 체결 거래: {pnl['total_trades']}건",
        f"🏆 승률: {pnl['win_rate']}% ({pnl['win_count']}승 {pnl['lose_count']}패)",
        f"💰 오늘 손익: <b>{pnl['total_pnl']:+,}원</b>",
    ]

    # 상위 거래 3건
    if pnl["details"]:
        lines.append("━━━━━━━━━━━━━━━━━━")
        sorted_details = sorted(pnl["details"], key=lambda x: abs(x["pnl"]), reverse=True)
        for d in sorted_details[:3]:
            e = "✅" if d["pnl"] >= 0 else "❌"
            lines.append(f"{e} {d['name']} {d['pnl_pct']:+.1f}% ({d['pnl']:+,}원)")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("✅ 내일도 화이팅!")

    await send_message("\n".join(lines))
    logger.info(f"일일 리포트 발송: {pnl['total_pnl']:+,}원")
    return pnl


async def send_weekly_report(db: AsyncSession):
    """주간 수익 리포트 텔레그램 발송"""
    end   = now_kst_naive()
    start = end - timedelta(days=7)

    pnl = await calc_pnl(db, start, end)

    emoji = "📈" if pnl["total_pnl"] >= 0 else "📉"

    msg = (
        f"{emoji} <b>[AI INVEST] 주간 리포트</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 {start.strftime('%m/%d')} ~ {end.strftime('%m/%d')}\n"
        f"🛒 총 거래: {pnl['total_trades']}건\n"
        f"🏆 승률: {pnl['win_rate']}% ({pnl['win_count']}승 {pnl['lose_count']}패)\n"
        f"💰 주간 손익: <b>{pnl['total_pnl']:+,}원</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    await send_message(msg)
    logger.info(f"주간 리포트 발송: {pnl['total_pnl']:+,}원")
    return pnl


async def get_monthly_stats(db: AsyncSession) -> dict:
    """월별 손익 통계"""
    end   = now_kst_naive()
    start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return await calc_pnl(db, start, end)
