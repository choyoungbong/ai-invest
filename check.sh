#!/bin/bash
TODAY=$(TZ='Asia/Seoul' date '+%Y-%m-%d')

python3 << 'PYEOF'
import urllib.request, json, subprocess
from datetime import datetime, timezone, timedelta


KST_TZ = timezone(timedelta(hours=9))
kst_now = datetime.now(KST_TZ).strftime('%Y-%m-%d %H:%M:%S')
today_str = datetime.now(KST_TZ).strftime('%Y-%m-%d')
PSQL = ["docker-compose","-f","/home/choyoungbong7/ai-invest/docker-compose.yml",
        "exec","-T","postgres","psql","-U","aiinvest","-d","aiinvest","-t","-A","-F","\t"]

def fetch(path):
    try:
        with urllib.request.urlopen(f"http://localhost:8000{path}", timeout=15) as r:
            return json.loads(r.read())
    except: return None

def db(sql):
    r = subprocess.run(PSQL+["-c",sql], capture_output=True, text=True)
    return [ln.split("\t") for ln in r.stdout.strip().splitlines() if ln.strip()]

def pbar(val, mx, w=18):
    if not mx: return "░"*w
    pct = min(abs(val)/abs(mx), 1.0)
    n = int(pct*w)
    return "█"*n + "░"*(w-n)

SEP = "━" * 52

# ── 데이터 수집 ────────────────────────────────────────────────
kr   = fetch("/trade/balance")
us   = fetch("/us-trading/balance")
risk = fetch("/risk/status")

# 보유 기간 (종목별 최초 BUY일)
now_utc = datetime.now(timezone.utc)
hdays = {}
for r in db("SELECT code, MIN(created_at) FROM trades WHERE order_type='BUY' AND status='FILLED' GROUP BY code"):
    if len(r)==2:
        try:
            dt = datetime.fromisoformat(r[1].replace(" ","T"))
            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
            hdays[r[0]] = (now_utc - dt).days
        except: pass

# 실현 손익 (SELL 체결 기준 - 부분 청산 포함)
real_rows = db("""
    SELECT code, ROUND(SUM(real_profit)::numeric) as pnl
    FROM trades
    WHERE order_type='SELL'
      AND status IN ('FILLED','CLOSED')
      AND real_profit IS NOT NULL
      
    GROUP BY code
    ORDER BY 2 DESC
""")
total_realized = sum(float(r[1]) for r in real_rows if len(r)==2)

# ── 🇰🇷 국내 현황 ────────────────────────────────────────────────
print(); print(SEP)
print(f"  🇰🇷 국내 현황   {kst_now}")
print(SEP)
if kr:
    hs = kr.get("holdings", [])
    unreal = sum(h.get("profit_loss",0) for h in hs)
    wins = sum(1 for h in hs if h.get("profit_loss",0)>0)
    loss = sum(1 for h in hs if h.get("profit_loss",0)<0)
    print(f"  총평가   {kr.get('total_eval',0):>14,}원  │  예수금 {kr.get('available_cash',0):>13,}원")
    print(f"  포지션   {len(hs)}/6개  │  수익 {wins}종목  손실 {loss}종목")
    print(f"  {'─'*48}")
    print(f"  {'종목':<10} {'수량':>4}  {'매수가':>8} {'현재가':>8}  {'수익률':>7}  {'손익':>11}  {'보유'}")
    print(f"  {'─'*48}")
    for h in hs:
        pnl = h.get("profit_loss",0)
        pct = h.get("profit_pct",0)
        icon = "🟢" if pnl>=0 else "🔴"
        trail = "🎯" if pct>=2.0 else "  "
        days = hdays.get(h["code"],0)
        name = h["name"][:9]
        print(f"  {icon}{trail}{name:<9} {h['quantity']:>3}주  "
              f"{h['avg_price']:>8,.0f} {h['current_price']:>8,}  "
              f"{pct:>+6.2f}%  {pnl:>+10,.0f}원  {'오늘' if days==0 else str(days)+'일'}")
    print(f"  {'─'*48}")
    print(f"  미실현 손익 합계{unreal:>+28,.0f}원")
else: print("  ⚠️  국내 잔고 조회 실패")

# ── 🇺🇸 미국 현황 ────────────────────────────────────────────────
print(); print(SEP)
print(f"  🇺🇸 미국 현황")
print(SEP)
if us and us.get("success"):
    d = us["data"]
    hs = d.get("holdings",[])
    us_unreal = sum((h["current_price"]-h["avg_price"])*h["quantity"] for h in hs)
    wins = sum(1 for h in hs if h.get("pnl_pct",0)>0)
    loss = sum(1 for h in hs if h.get("pnl_pct",0)<0)
    print(f"  총자산   ${d.get('total_usd',0):>11.2f}        │  예수금 ${d.get('cash_usd',0):>9.2f}")
    print(f"  포지션   {len(hs)}/5개  │  수익 {wins}종목  손실 {loss}종목")
    print(f"  {'─'*48}")
    print(f"  {'종목':<7} {'수량':>4}  {'매수가':>7} {'현재가':>7}  {'수익률':>7}  {'손익':>8}  {'보유'}")
    print(f"  {'─'*48}")
    for h in hs:
        pct = h.get("pnl_pct",0)
        pnl = (h["current_price"]-h["avg_price"])*h["quantity"]
        icon = "🟢" if pnl>=0 else "🔴"
        trail = "🎯" if pct>=2.0 else "  "
        days = hdays.get(h["symbol"],0)
        print(f"  {icon}{trail}{h['symbol']:<6} {h['quantity']:>3}주  "
              f"${h['avg_price']:>6.2f} ${h['current_price']:>6.2f}  "
              f"{pct:>+6.2f}%  ${pnl:>+6.2f}  {'오늘' if days==0 else str(days)+'일'}")
    print(f"  {'─'*48}")
    print(f"  미실현 손익 합계{us_unreal:>+32.2f}")
else: print("  ⚠️  미국 잔고 조회 실패")

# ── 📊 리스크 & 실현손익 ─────────────────────────────────────────
print(); print(SEP)
print(f"  📊 리스크 현황")
print(SEP)
if risk:
    today_pnl  = risk.get("today_pnl",0)
    daily_lim  = risk.get("daily_loss_limit",-200000)
    pb = pbar(today_pnl, daily_lim)
    pct_u = abs(today_pnl)/abs(daily_lim)*100 if daily_lim else 0
    print(f"  매수가능   {'✅ 가능' if risk.get('can_buy') else '❌ 불가'}   │   장중: {'✅ 개장중' if risk.get('market_open') else '❌ 폐장'}")
    print(f"  오늘손익   {today_pnl:>+14,.0f}원")
    print(f"  일손실한도 [{pb}] {pct_u:.1f}%  ({today_pnl:+,.0f} / {daily_lim:,.0f}원)")

print(); print(SEP)
print(f"  💰 실현 손익 (청산 완료 종목)")
print(SEP)
if real_rows:
    for r in real_rows:
        if len(r)==2:
            pnl = float(r[1])
            print(f"  {'🟢' if pnl>=0 else '🔴'} {r[0]:<8}  {pnl:>+14,.0f}원")
    print(f"  {'─'*48}")
    print(f"  누적 실현 손익 합계{total_realized:>+26,.0f}원")
else:
    print("  (아직 청산 완료 종목 없음)")

PYEOF

# ── 오늘 거래 내역 ──────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📋 오늘 거래 내역 [$TODAY]"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
docker-compose -f ~/ai-invest/docker-compose.yml exec -T postgres \
  psql -U aiinvest -d aiinvest -c "
SELECT to_char(created_at AT TIME ZONE 'Asia/Seoul','HH24:MI') AS 시간,
       code AS 종목, order_type AS 구분, status AS 상태,
       to_char(price,'FM9,999,999') AS 가격, quantity AS 수량,
       to_char(ROUND(price*quantity::numeric),'FM9,999,999') AS 금액
FROM trades WHERE created_at >= NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;" 2>/dev/null

# ── 주요 이벤트 ─────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📡 오늘 주요 이벤트"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
# DB 기반 이벤트 (재빌드/재시작에 영향 없음)
docker-compose -f ~/ai-invest/docker-compose.yml exec -T postgres \
  psql -U aiinvest -d aiinvest -c "
SELECT to_char(created_at AT TIME ZONE 'Asia/Seoul','HH24:MI') AS 시간,
       strategy AS 전략,
       code AS 종목,
       ROUND(confidence::numeric*100,1) AS 신뢰도,
       to_char(price,'FM9,999,999') AS 신호가
FROM signals
WHERE created_at >= NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC LIMIT 15;" 2>/dev/null

echo ""
echo "  [최근 컨테이너 로그]"
docker logs aiinvest-api 2>&1 \
  | grep -vE "apscheduler|APScheduler|Added job|Running job|executed successfully|httpx|WebSocket" \
  | grep -E "(자동 청산|1차 매수|2차 매수|손절|익절|청산 완료|신호 발생|ERROR|WARNING)" \
  | tail -10
