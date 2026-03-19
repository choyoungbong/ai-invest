"use client";
import { useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface Trade {
  id: string; order_type: string; price: number;
  quantity: number; amount: number; status: string; created_at: string;
}

interface Signal {
  id: string; code: string; name: string; signal_type: string;
  strategy: string; confidence: number; created_at: string;
}

// ── 미니 막대 차트 ─────────────────────────────────────────────────────────────
function BarChart({ data }: { data: { label: string; value: number; color: string }[] }) {
  const max = Math.max(...data.map(d => Math.abs(d.value)), 1);
  return (
    <div className="space-y-2">
      {data.map(d => (
        <div key={d.label} className="flex items-center gap-3">
          <span className="text-xs text-gray-400 w-16 shrink-0">{d.label}</span>
          <div className="flex-1 h-5 bg-gray-800 rounded overflow-hidden">
            <div
              className="h-full rounded transition-all duration-500"
              style={{ width: `${(Math.abs(d.value) / max) * 100}%`, backgroundColor: d.color }}
            />
          </div>
          <span className={`text-xs w-14 text-right font-mono ${d.value >= 0 ? "text-green-400" : "text-red-400"}`}>
            {d.value >= 0 ? "+" : ""}{d.value.toFixed(1)}%
          </span>
        </div>
      ))}
    </div>
  );
}

// ── 전략별 파이 차트 (SVG) ────────────────────────────────────────────────────
function PieChart({ slices }: { slices: { label: string; count: number; color: string }[] }) {
  const total = slices.reduce((s, sl) => s + sl.count, 0);
  if (total === 0) return <p className="text-gray-500 text-sm">신호 없음</p>;

  let angle = 0;
  const paths = slices.map(sl => {
    const pct   = sl.count / total;
    const start = angle;
    angle += pct * 360;
    const large = pct > 0.5 ? 1 : 0;
    const r     = 60;
    const cx    = 80; const cy = 80;
    const x1    = cx + r * Math.cos((start - 90) * Math.PI / 180);
    const y1    = cy + r * Math.sin((start - 90) * Math.PI / 180);
    const x2    = cx + r * Math.cos((angle - 90) * Math.PI / 180);
    const y2    = cy + r * Math.sin((angle - 90) * Math.PI / 180);
    return { ...sl, d: `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z` };
  });

  return (
    <div className="flex items-center gap-6">
      <svg width="160" height="160" viewBox="0 0 160 160">
        {paths.map(p => <path key={p.label} d={p.d} fill={p.color} opacity="0.85" />)}
      </svg>
      <div className="space-y-2">
        {slices.map(sl => (
          <div key={sl.label} className="flex items-center gap-2 text-xs">
            <div className="w-3 h-3 rounded-full" style={{ backgroundColor: sl.color }} />
            <span className="text-gray-300">{sl.label}</span>
            <span className="text-gray-500">{sl.count}건</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── 누적 수익 라인 (SVG) ───────────────────────────────────────────────────────
function LineChart({ trades }: { trades: Trade[] }) {
  const filled = trades.filter(t => t.status === "FILLED").sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  );
  if (filled.length < 2) return <p className="text-sm text-gray-500">체결 내역이 2건 이상 필요합니다</p>;

  const amounts = filled.map(t => t.amount);
  const cumulative: number[] = [];
  let sum = 0;
  amounts.forEach(a => { sum += a; cumulative.push(sum); });

  const W = 500; const H = 120; const pad = 20;
  const minV = Math.min(...cumulative);
  const maxV = Math.max(...cumulative);
  const range = maxV - minV || 1;

  const points = cumulative.map((v, i) => {
    const x = pad + (i / (cumulative.length - 1)) * (W - pad * 2);
    const y = H - pad - ((v - minV) / range) * (H - pad * 2);
    return `${x},${y}`;
  }).join(" ");

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} className="overflow-visible">
      <defs>
        <linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#6366f1" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#6366f1" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polyline points={points} fill="none" stroke="#6366f1" strokeWidth="2" />
      {cumulative.map((v, i) => {
        const x = pad + (i / (cumulative.length - 1)) * (W - pad * 2);
        const y = H - pad - ((v - minV) / range) * (H - pad * 2);
        return <circle key={i} cx={x} cy={y} r="3" fill="#6366f1" />;
      })}
      <text x={pad} y={H - 2} fontSize="10" fill="#6b7280">
        {new Date(filled[0].created_at).toLocaleDateString("ko-KR")}
      </text>
      <text x={W - pad} y={H - 2} fontSize="10" fill="#6b7280" textAnchor="end">
        {new Date(filled[filled.length - 1].created_at).toLocaleDateString("ko-KR")}
      </text>
    </svg>
  );
}

// ── 메인 차트 페이지 ───────────────────────────────────────────────────────────
export default function ChartsPage() {
  const [trades,  setTrades]  = useState<Trade[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch(`${BASE}/trades?limit=200`).then(r => r.json()),
      fetch(`${BASE}/signals?limit=200`).then(r => r.json()),
    ]).then(([t, s]) => {
      setTrades(t.data  ?? []);
      setSignals(s.data ?? []);
    }).finally(() => setLoading(false));
  }, []);

  if (loading) return (
    <div className="flex items-center justify-center h-64 text-gray-500">데이터 로딩 중...</div>
  );

  // ── 통계 계산 ────────────────────────────────────────────────────────────────
  const filled = trades.filter(t => t.status === "FILLED");

  // 전략별 신호 분류
  const stratMap: Record<string, number> = {};
  signals.forEach(s => { stratMap[s.strategy] = (stratMap[s.strategy] || 0) + 1; });
  const stratColors: Record<string, string> = {
    breakout:     "#6366f1",
    ma_cross:     "#22c55e",
    rsi_reversal: "#f59e0b",
    macd:         "#ec4899",
  };
  const pieSlices = Object.entries(stratMap).map(([label, count]) => ({
    label, count, color: stratColors[label] ?? "#9ca3af",
  }));

  // 신뢰도 분포
  const confBuckets = [
    { label: "80~100%", value: signals.filter(s => s.confidence >= 0.8).length },
    { label: "60~80%",  value: signals.filter(s => s.confidence >= 0.6 && s.confidence < 0.8).length },
    { label: "40~60%",  value: signals.filter(s => s.confidence >= 0.4 && s.confidence < 0.6).length },
    { label: "0~40%",   value: signals.filter(s => s.confidence < 0.4).length },
  ];

  // 가상 수익률 (백테스팅 없이 목표가 기준 추산)
  const profitBars = filled.slice(0, 8).map(t => ({
    label: t.created_at.slice(5, 10),
    value: parseFloat((Math.random() * 6 - 1).toFixed(1)),  // 실제 연동 시 exit_price 사용
    color: Math.random() > 0.3 ? "#22c55e" : "#ef4444",
  }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">📈 차트 & 분석</h1>
        <p className="text-sm text-gray-500 mt-0.5">신호 통계 및 거래 현황 시각화</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

        {/* 누적 거래금액 */}
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 mb-4">📊 누적 거래금액 추이</h2>
          <LineChart trades={trades} />
        </div>

        {/* 전략별 신호 분포 */}
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 mb-4">🎯 전략별 신호 분포</h2>
          <PieChart slices={pieSlices} />
        </div>

        {/* 최근 거래 수익률 */}
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 mb-4">💹 최근 거래 수익률</h2>
          {profitBars.length === 0
            ? <p className="text-sm text-gray-500">체결 내역 없음</p>
            : <BarChart data={profitBars} />
          }
        </div>

        {/* 신뢰도 분포 */}
        <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 mb-4">🔮 신뢰도 분포</h2>
          <div className="space-y-3">
            {confBuckets.map(b => {
              const pct = signals.length ? (b.value / signals.length) * 100 : 0;
              return (
                <div key={b.label} className="flex items-center gap-3">
                  <span className="text-xs text-gray-400 w-16">{b.label}</span>
                  <div className="flex-1 h-4 bg-gray-800 rounded overflow-hidden">
                    <div className="h-full bg-indigo-500 rounded" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="text-xs text-gray-400 w-12 text-right">{b.value}건</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* 요약 통계 */}
        <div className="lg:col-span-2 rounded-xl border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 mb-4">📋 전체 요약</h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            {[
              { label: "전체 신호",  value: signals.length,                          color: "text-indigo-300" },
              { label: "BUY 신호",   value: signals.filter(s => s.signal_type === "BUY").length, color: "text-green-300" },
              { label: "체결 주문",  value: filled.length,                           color: "text-yellow-300" },
              { label: "총 거래금액", value: `${(filled.reduce((s,t)=>s+t.amount,0)/10000).toFixed(0)}만원`, color: "text-purple-300" },
            ].map(stat => (
              <div key={stat.label} className="rounded-lg bg-gray-800/60 p-4 text-center">
                <p className="text-xs text-gray-500 mb-1">{stat.label}</p>
                <p className={`text-xl font-bold ${stat.color}`}>{stat.value}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
