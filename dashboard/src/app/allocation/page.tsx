"use client";
import { useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface StratAlloc {
  ratio: number;
  budget: number;
  max_single: number;
}

interface AllocSummary {
  total_budget: number;
  total_ratio: number;
  is_valid: boolean;
  strategies: Record<string, StratAlloc>;
}

const STRATEGY_LABELS: Record<string, string> = {
  breakout:     "🚀 돌파매매",
  ma_cross:     "📈 MA 크로스",
  rsi_reversal: "📉 RSI 반등",
  macd:         "🌊 MACD",
};
const COLORS = ["#6366f1", "#22c55e", "#f59e0b", "#ec4899"];

export default function AllocationPage() {
  const [alloc,   setAlloc]   = useState<AllocSummary | null>(null);
  const [price,   setPrice]   = useState("75000");
  const [conf,    setConf]    = useState("0.7");
  const [strat,   setStrat]   = useState("breakout");
  const [calcRes, setCalcRes] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    fetch(`${BASE}/allocation`)
      .then(r => r.json())
      .then(setAlloc);
  }, []);

  async function calcOrder() {
    const res = await fetch(
      `${BASE}/allocation/calc?strategy=${strat}&price=${price}&confidence=${conf}`
    );
    setCalcRes(await res.json());
  }

  if (!alloc) return (
    <div className="flex items-center justify-center h-64 text-gray-500">로딩 중...</div>
  );

  const entries = Object.entries(alloc.strategies);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">💰 자금 배분</h1>
        <p className="text-sm text-gray-500 mt-0.5">전략별 투자 예산 배분 현황</p>
      </div>

      {/* 총 예산 */}
      <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-gray-300">총 투자 예산</h2>
          <span className={`text-xs px-2 py-1 rounded ${alloc.is_valid ? "bg-green-500/20 text-green-400" : "bg-red-500/20 text-red-400"}`}>
            {alloc.is_valid ? "✅ 배분 정상" : "⚠️ 합계 오류"}
          </span>
        </div>
        <p className="text-3xl font-bold text-white">{alloc.total_budget.toLocaleString()}원</p>

        {/* 비율 바 */}
        <div className="mt-4 flex h-4 rounded-full overflow-hidden">
          {entries.map(([key, val], i) => (
            <div
              key={key}
              style={{ width: `${val.ratio * 100}%`, backgroundColor: COLORS[i] }}
              title={`${STRATEGY_LABELS[key]}: ${val.ratio * 100}%`}
            />
          ))}
        </div>
        <div className="mt-2 flex flex-wrap gap-3">
          {entries.map(([key], i) => (
            <div key={key} className="flex items-center gap-1 text-xs text-gray-400">
              <div className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: COLORS[i] }} />
              {STRATEGY_LABELS[key]}
            </div>
          ))}
        </div>
      </div>

      {/* 전략별 카드 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {entries.map(([key, val], i) => (
          <div key={key} className="rounded-xl border border-gray-800 bg-gray-900 p-4">
            <div className="flex items-center gap-2 mb-3">
              <div className="w-3 h-3 rounded-full" style={{ backgroundColor: COLORS[i] }} />
              <span className="text-sm font-medium text-gray-200">{STRATEGY_LABELS[key]}</span>
            </div>
            <p className="text-xs text-gray-500">배분 비율</p>
            <p className="text-2xl font-bold text-white">{(val.ratio * 100).toFixed(0)}%</p>
            <div className="mt-2 space-y-1 text-xs text-gray-400">
              <div className="flex justify-between">
                <span>전략 예산</span>
                <span className="text-gray-200">{val.budget.toLocaleString()}원</span>
              </div>
              <div className="flex justify-between">
                <span>1회 최대</span>
                <span className="text-indigo-300">{val.max_single.toLocaleString()}원</span>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* 주문 계산기 */}
      <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
        <h2 className="text-sm font-semibold text-gray-300 mb-4">🧮 주문 수량 계산기</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="text-xs text-gray-400 block mb-1">전략</label>
            <select value={strat} onChange={e => setStrat(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white">
              {entries.map(([key]) => <option key={key} value={key}>{STRATEGY_LABELS[key]}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">현재가 (원)</label>
            <input value={price} onChange={e => setPrice(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white" />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">신뢰도 (0~1)</label>
            <input value={conf} onChange={e => setConf(e.target.value)} type="number" min="0" max="1" step="0.1"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white" />
          </div>
          <div className="flex items-end">
            <button onClick={calcOrder}
              className="w-full px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-sm font-medium">
              계산
            </button>
          </div>
        </div>

        {calcRes && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-2">
            {[
              ["주문 수량", `${calcRes.quantity}주`],
              ["주문 금액", `${Number(calcRes.amount).toLocaleString()}원`],
              ["총 비용",   `${Number(calcRes.total_cost).toLocaleString()}원`],
              ["신뢰도",    `${(Number(calcRes.confidence) * 100).toFixed(0)}%`],
            ].map(([label, value]) => (
              <div key={label as string} className="bg-gray-800/50 rounded-lg p-3 text-center">
                <p className="text-xs text-gray-500">{label}</p>
                <p className="font-bold text-indigo-300 mt-1">{value}</p>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* .env 설정 안내 */}
      <div className="rounded-xl border border-gray-700/50 bg-gray-800/30 p-5">
        <h2 className="text-sm font-semibold text-gray-300 mb-3">⚙️ 배분 비율 변경 방법</h2>
        <p className="text-xs text-gray-400 mb-2">.env 파일에서 수정 후 서버 재시작</p>
        <pre className="text-xs text-green-300 bg-gray-900 rounded-lg p-3 overflow-x-auto">{`TOTAL_BUDGET=5000000
ALLOC_BREAKOUT=0.40
ALLOC_MA_CROSS=0.30
ALLOC_RSI_REVERSAL=0.20
ALLOC_MACD=0.10`}</pre>
      </div>
    </div>
  );
}
