"use client";
import { useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface BacktestStats {
  total_trades: number;
  win_count: number;
  lose_count: number;
  win_rate: number;
  avg_profit_pct: number;
  avg_win_pct: number;
  avg_loss_pct: number;
  profit_factor: number;
  cumulative_pct: number;
  max_drawdown_pct: number;
}

interface BacktestTrade {
  entry_date: string;
  exit_date: string;
  entry_price: number;
  exit_price: number;
  profit_pct: number;
  exit_reason: string;
}

interface BacktestResult {
  code: string;
  strategy: string;
  start_date: string;
  end_date: string;
  data_days: number;
  stats: BacktestStats;
  trades: BacktestTrade[];
}

export default function BacktestPage() {
  const [code,      setCode]      = useState("005930");
  const [strategy,  setStrategy]  = useState("breakout");
  const [startDate, setStartDate] = useState("2024-01-01");
  const [endDate,   setEndDate]   = useState("2024-12-31");
  const [loading,   setLoading]   = useState(false);
  const [result,    setResult]    = useState<BacktestResult | null>(null);
  const [error,     setError]     = useState<string | null>(null);

  async function runBacktest() {
    setLoading(true);
    setResult(null);
    setError(null);
    try {
      const url = `${BASE}/backtest?code=${code}&strategy=${strategy}&start_date=${startDate}&end_date=${endDate}`;
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "오류 발생");
      setResult(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const s = result?.stats;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">🔬 백테스팅</h1>
        <p className="text-sm text-gray-500 mt-0.5">과거 데이터로 전략 수익률 검증</p>
      </div>

      {/* 입력 폼 */}
      <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
        <h2 className="text-sm font-semibold text-gray-300 mb-4">⚙️ 백테스트 설정</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="text-xs text-gray-400 block mb-1">종목코드</label>
            <input
              value={code}
              onChange={e => setCode(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500"
              placeholder="005930"
            />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">전략</label>
            <select
              value={strategy}
              onChange={e => setStrategy(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500"
            >
              <option value="breakout">돌파매매</option>
              <option value="ma_cross">이동평균 크로스</option>
              <option value="rsi_reversal">RSI 과매도 반등</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">시작일</label>
            <input
              type="date"
              value={startDate}
              onChange={e => setStartDate(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">종료일</label>
            <input
              type="date"
              value={endDate}
              onChange={e => setEndDate(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500"
            />
          </div>
        </div>
        <button
          onClick={runBacktest}
          disabled={loading}
          className="px-5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-sm font-medium transition-colors"
        >
          {loading ? "실행 중..." : "🚀 백테스트 실행"}
        </button>
        {error && <p className="text-red-400 text-sm mt-3">❌ {error}</p>}
      </div>

      {/* 결과 */}
      {result && s && (
        <>
          {/* 핵심 지표 */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            {[
              { label: "총 거래",       value: `${s.total_trades}건`,          color: "text-white" },
              { label: "승률",          value: `${s.win_rate}%`,               color: s.win_rate >= 50 ? "text-green-300" : "text-red-300" },
              { label: "평균 수익률",   value: `${s.avg_profit_pct > 0 ? "+" : ""}${s.avg_profit_pct}%`, color: s.avg_profit_pct >= 0 ? "text-green-300" : "text-red-300" },
              { label: "누적 수익률",   value: `${s.cumulative_pct > 0 ? "+" : ""}${s.cumulative_pct}%`, color: s.cumulative_pct >= 0 ? "text-green-300" : "text-red-300" },
              { label: "최대 낙폭(MDD)", value: `-${s.max_drawdown_pct}%`,     color: "text-red-300" },
            ].map(stat => (
              <div key={stat.label} className="rounded-xl border border-gray-800 bg-gray-900 p-4 text-center">
                <p className="text-xs text-gray-500 mb-1">{stat.label}</p>
                <p className={`text-xl font-bold ${stat.color}`}>{stat.value}</p>
              </div>
            ))}
          </div>

          {/* 세부 통계 */}
          <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
            <h2 className="text-sm font-semibold text-gray-300 mb-4">
              📊 {result.code} — {result.strategy} ({result.start_date} ~ {result.end_date}, {result.data_days}일)
            </h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              {[
                ["승 / 패", `${s.win_count} / ${s.lose_count}`],
                ["평균 수익", `+${s.avg_win_pct}%`],
                ["평균 손실", `${s.avg_loss_pct}%`],
                ["수익 팩터", s.profit_factor === Infinity ? "∞" : s.profit_factor],
              ].map(([label, value]) => (
                <div key={label as string} className="bg-gray-800/50 rounded-lg p-3">
                  <p className="text-xs text-gray-500">{label}</p>
                  <p className="font-semibold text-white mt-1">{value}</p>
                </div>
              ))}
            </div>
          </div>

          {/* 거래 내역 */}
          <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
            <div className="px-5 py-3 border-b border-gray-800">
              <h2 className="text-sm font-semibold text-gray-300">거래 내역 ({result.trades.length}건)</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-500 border-b border-gray-800">
                    {["진입일","청산일","진입가","청산가","수익률","청산 이유"].map(h => (
                      <th key={h} className="text-left px-4 py-2">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {result.trades.map((t, i) => (
                    <tr key={i} className="hover:bg-gray-800/40">
                      <td className="px-4 py-2 text-gray-400">{t.entry_date}</td>
                      <td className="px-4 py-2 text-gray-400">{t.exit_date}</td>
                      <td className="px-4 py-2 font-mono">{t.entry_price.toLocaleString()}</td>
                      <td className="px-4 py-2 font-mono">{t.exit_price.toLocaleString()}</td>
                      <td className={`px-4 py-2 font-bold ${t.profit_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {t.profit_pct >= 0 ? "+" : ""}{t.profit_pct}%
                      </td>
                      <td className="px-4 py-2">
                        <span className={`text-xs px-2 py-0.5 rounded ${
                          t.exit_reason === "목표가 달성" ? "bg-green-500/20 text-green-400" :
                          t.exit_reason === "손절" ? "bg-red-500/20 text-red-400" :
                          "bg-gray-700 text-gray-400"
                        }`}>{t.exit_reason}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
