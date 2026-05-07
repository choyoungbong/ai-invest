#!/bin/bash

echo ""
docker exec aiinvest-api python -c "
import asyncio
from trader.kis_client import get_balance
from trader.us_kis_client import get_us_balance
from datetime import datetime
import pytz

async def t():
    KST = pytz.timezone('Asia/Seoul')
    now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

    # ── 국내 ──────────────────────────────────────────
    bal = await get_balance()
    cash = bal['available_cash']
    total = bal['total_eval']
    holdings = bal.get('holdings', [])
    total_pnl = sum((h['current_price'] - h['avg_price']) * h['quantity'] for h in holdings)

    print(f'========================================')
    print(f'  🇰🇷 국내 현황 [{now}]')
    print(f'========================================')
    print(f'예수금: {cash:,}원 | 총평가: {total:,}원')
    print(f'보유: {len(holdings)}종목 / 슬롯 6개 | 여유: {6-len(holdings)}개')
    print(f'────────────────────────────────────────')
    for h in holdings:
        pnl = (h[\"current_price\"] - h[\"avg_price\"]) * h[\"quantity\"]
        bar = '🟢' if pnl >= 0 else '🔴'
        print(f'{bar} {h[\"name\"]}({h[\"quantity\"]}주) 평균{h[\"avg_price\"]:,.0f}→{h[\"current_price\"]:,}원 {h[\"profit_pct\"]:+.2f}% {pnl:+,.0f}원')
    print(f'────────────────────────────────────────')
    print(f'미실현 손익: {total_pnl:+,.0f}원')

    # ── 미국 ──────────────────────────────────────────
    print()
    print(f'========================================')
    print(f'  🇺🇸 미국 현황')
    print(f'========================================')
    try:
        us = await get_us_balance()
        us_cash = us['cash_usd']
        us_total = us['total_usd']
        us_holdings = us.get('holdings', [])
        us_pnl = sum((h['current_price'] - h['avg_price']) * h['quantity'] for h in us_holdings)
        print(f'예수금: \${us_cash:.2f} | 총자산: \${us_total:.2f}')
        print(f'보유: {len(us_holdings)}종목 / 슬롯 6개 | 여유: {6-len(us_holdings)}개')
        print(f'────────────────────────────────────────')
        for h in us_holdings:
            pnl = (h['current_price'] - h['avg_price']) * h['quantity']
            bar = '🟢' if pnl >= 0 else '🔴'
            print(f'{bar} {h[\"symbol\"]}({h[\"quantity\"]}주) 평균\${h[\"avg_price\"]:.2f}→\${h[\"current_price\"]:.2f} {h[\"pnl_pct\"]:+.2f}% \${pnl:+.2f}')
        print(f'────────────────────────────────────────')
        print(f'미실현 손익: \${us_pnl:+.2f}')
    except Exception as e:
        print(f'미국 잔고 조회 실패: {e}')

asyncio.run(t())
"

echo ""
curl -s http://localhost:8000/risk/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'========================================')
print(f'  📊 리스크 현황')
print(f'========================================')
print(f'포지션: {d.get(\"positions\")}/{d.get(\"max_positions\")} | 슬롯여유: {d.get(\"max_positions\",0)-d.get(\"positions\",0)}개')
print(f'매수가능: {d.get(\"can_buy\")} | 장중: {d.get(\"market_open\")}')
print(f'오늘손익: {d.get(\"today_pnl\",0):,}원 | 일손실한도: {d.get(\"daily_loss_limit\",0):,}원')
"

echo ""
TODAY=$(date +%Y-%m-%d)
echo "========================================"
echo "  📋 오늘 거래 활동 [$TODAY]"
echo "========================================"
docker logs aiinvest-api 2>&1 | grep "$TODAY" | grep -E "자동 매수|자동 매도|익절|손절|풀 실행|빠른 스캔|🇺🇸.*매수|🇺🇸.*매도" | tail -15
