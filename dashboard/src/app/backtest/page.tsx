"use client";
import { useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface BacktestStats {
  total_trades: number; win_count: number; lose_count: number;
  win_rate: number; avg_profit_pct: number; avg_win_pct: number;
  avg_loss_pct: number; profit_factor: number;
  cumulative_pct: number; max_drawdown_pct: number;
}
interface BacktestResult {
  code: string; strategy: string; start_date: string; end_date: string;
  data_days: number; stats: BacktestStats;
  trades: { entry_date: string; exit_date: string; entry_price: number;
            exit_price: number; profit_pct: number; exit_reason: string }[];
}
interface GridResult {
  params: Record<string, number>;
  total_trades: number; win_rate: number; profit_factor: number;
  cumulative_pct: number; max_drawdown_pct: number; avg_profit_pct: number;
}
interface GridSearchResult {
  strategy: string; start_date: string; end_date: string;
  codes_tested: number; total_combinations: number;
  valid_results: number; top_results: GridResult[]; best: GridResult | null;
}

type TabType = "single" | "grid";

export default function BacktestPage() {
  const [tab,       setTab]       = useState<TabType>("single");

  // 단일 백테스트
  const [code,       setCode]      = useState("005930");
  const [strategy,   setStrategy]  = useState("breakout");
  const [startDate,  setStartDate] = useState("2024-01-01");
  const [endDate,    setEndDate]   = useState("2024-12-31");
  const [loading,    setLoading]   = useState(false);
  const [result,     setResult]    = useState<BacktestResult | null>(null);
  const [error,      setError]     = useState<string | null>(null);

  // 그리드 서치
  const [gCodes,    setGCodes]    = useState("005930,000660,035420");
  const [gStrategy, setGStrategy] = useState("breakout");
  const [gStart,    setGStart]    = useState("2023-01-01");
  const [gEnd,      setGEnd]      = useState("2024-12-31");
  const [gLoading,  setGLoading]  = useState(false);
  const [gResult,   setGResult]   = useState<GridSearchResult | null>(null);
  const [gError,    setGError]    = useState<string | null>(null);

  async function runSingle() {
    setLoading(true); setResult(null); setError(null);
    try {
      const url = `${BASE}/backtest?code=${code}&strategy=${strategy}&start_date=${startDate}&end_date=${endDate}`;
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "오류");
      setResult(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function runGrid() {
    setGLoading(true); setGResult(null); setGError(null);
    try {
      const codeList = gCodes.split(",").map(c => c.trim()).filter(Boolean);
      const params   = new URLSearchParams({
        strategy: gStrategy, start_date: gStart, end_date: gEnd,
      });
      codeList.forEach(c => params.append("codes", c));
      const res  = await fetch(`${BASE}/backtest/grid-search?${params.toString()}`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "오류");
      setGResult(data);
    } catch (e: unknown) {
      setGError(e instanceof Error ? e.message : String(e));
    } finally {
      setGLoading(false);
    }
  }

  const s = result?.stats;

  const paramLabel = (params: Record<string, number>) =>
    Object.entries(params)
      .map(([k, v]) => `${k}=${v}`)
      .join(" / ");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">🔬 백테스팅</h1>
        <p className="text-sm text-gray-500 mt-0.5">과거 데이터로 전략 수익률 검증 및 파라미터 최적화</p>
      </div>

      {/* 탭 */}
      <div className="flex gap-2 border-b border-gray-800 pb-0">
        {([["single", "단일 백테스트"], ["grid", "🔍 파라미터 최적화"]] as [TabType, string][]).map(([t, label]) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t
                ? "border-indigo-500 text-indigo-400"
                : "border-transparent text-gray-500 hover:text-gray-300"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* 단일 백테스트 */}
      {tab === "single" && (
        <>
          <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
            <h2 className="text-sm font-semibold text-gray-300 mb-4">⚙️ 설정</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
              {[
                { label: "종목코드", value: code, set: setCode, placeholder: "005930" },
              ].map(f => (
                <div key={f.label}>
                  <label className="text-xs text-gray-400 block mb-1">{f.label}</label>
                  <input value={f.value} onChange={e => f.set(e.target.value)}
                    placeholder={f.placeholder}
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
                </div>
              ))}
              <div>
                <label className="text-xs text-gray-400 block mb-1">전략</label>
                <select value={strategy} onChange={e => setStrategy(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
                  <option value="breakout">돌파매매</option>
                  <option value="ma_cross">이동평균 크로스</option>
                  <option value="rsi_reversal">RSI 과매도 반등</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">시작일</label>
                <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">종료일</label>
                <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
              </div>
            </div>
            <button onClick={runSingle} disabled={loading}
              className="px-5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-sm font-medium">
              {loading ? "실행 중..." : "🚀 백테스트 실행"}
            </button>
            {error && <p className="text-red-400 text-sm mt-3">❌ {error}</p>}
          </div>

          {result && s && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                {[
                  { label: "총 거래",       value: `${s.total_trades}건`,   color: "text-white" },
                  { label: "승률",          value: `${s.win_rate}%`,        color: s.win_rate >= 50 ? "text-green-300" : "text-red-300" },
                  { label: "평균 수익률",   value: `${s.avg_profit_pct >= 0 ? "+" : ""}${s.avg_profit_pct}%`, color: s.avg_profit_pct >= 0 ? "text-green-300" : "text-red-300" },
                  { label: "누적 수익률",   value: `${s.cumulative_pct >= 0 ? "+" : ""}${s.cumulative_pct}%`, color: s.cumulative_pct >= 0 ? "text-green-300" : "text-red-300" },
                  { label: "MDD",           value: `-${s.max_drawdown_pct}%`, color: "text-red-300" },
                ].map(stat => (
                  <div key={stat.label} className="rounded-xl border border-gray-800 bg-gray-900 p-4 text-center">
                    <p className="text-xs text-gray-500 mb-1">{stat.label}</p>
                    <p className={`text-xl font-bold ${stat.color}`}>{stat.value}</p>
                  </div>
                ))}
              </div>

              <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
                <h2 className="text-sm font-semibold text-gray-300 mb-4">
                  📊 {result.code} — {result.strategy} ({result.start_date} ~ {result.end_date}, {result.data_days}일)
                </h2>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                  {[
                    ["승 / 패", `${s.win_count} / ${s.lose_count}`],
                    ["평균 수익", `+${s.avg_win_pct}%`],
                    ["평균 손실", `${s.avg_loss_pct}%`],
                    ["수익 팩터", s.profit_factor >= 99 ? "∞" : s.profit_factor],
                  ].map(([label, value]) => (
                    <div key={label as string} className="bg-gray-800/50 rounded-lg p-3">
                      <p className="text-xs text-gray-500">{label}</p>
                      <p className="font-semibold text-white mt-1">{value}</p>
                    </div>
                  ))}
                </div>
              </div>

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
        </>
      )}

      {/* 그리드 서치 */}
      {tab === "grid" && (
        <>
          <div className="rounded-xl border border-yellow-500/30 bg-yellow-500/5 p-4 text-sm text-yellow-400">
            💡 여러 파라미터 조합을 자동으로 탐색해 <b>최적 설정값</b>을 찾습니다.
            현재 .env 설정이 데이터 기반 최적값인지 검증할 수 있습니다.
          </div>

          <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
            <h2 className="text-sm font-semibold text-gray-300 mb-4">⚙️ 그리드 서치 설정</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
              <div>
                <label className="text-xs text-gray-400 block mb-1">종목코드 (쉼표로 구분, 3~5개 권장)</label>
                <input value={gCodes} onChange={e => setGCodes(e.target.value)}
                  placeholder="005930,000660,035420"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">전략</label>
                <select value={gStrategy} onChange={e => setGStrategy(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
                  <option value="breakout">돌파매매 (108 조합)</option>
                  <option value="ma_cross">이동평균 크로스 (54 조합)</option>
                  <option value="rsi_reversal">RSI 과매도 반등 (36 조합)</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">시작일</label>
                <input type="date" value={gStart} onChange={e => setGStart(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">종료일</label>
                <input type="date" value={gEnd} onChange={e => setGEnd(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
              </div>
            </div>
            <button onClick={runGrid} disabled={gLoading}
              className="px-5 py-2 rounded-lg bg-yellow-600 hover:bg-yellow-500 disabled:opacity-50 text-sm font-medium text-white">
              {gLoading ? "탐색 중... (수십 초 소요)" : "🔍 최적 파라미터 탐색"}
            </button>
            {gError && <p className="text-red-400 text-sm mt-3">❌ {gError}</p>}
          </div>

          {gResult && (
            <>
              {/* 탐색 요약 */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {[
                  { label: "탐색 종목",   value: `${gResult.codes_tested}개` },
                  { label: "탐색 조합",   value: `${gResult.total_combinations}개` },
                  { label: "유효 결과",   value: `${gResult.valid_results}개` },
                  { label: "최고 수익팩터",
                    value: gResult.best ? (gResult.best.profit_factor >= 99 ? "∞" : String(gResult.best.profit_factor)) : "-",
                    color: "text-green-300" },
                ].map(stat => (
                  <div key={stat.label} className="rounded-xl border border-gray-800 bg-gray-900 p-4 text-center">
                    <p className="text-xs text-gray-500 mb-1">{stat.label}</p>
                    <p className={`text-xl font-bold ${stat.color ?? "text-white"}`}>{stat.value}</p>
                  </div>
                ))}
              </div>

              {/* 최적 파라미터 강조 */}
              {gResult.best && (
                <div className="rounded-xl border border-green-500/40 bg-green-500/5 p-5">
                  <h2 className="text-sm font-semibold text-green-400 mb-2">🏅 최적 파라미터</h2>
                  <p className="font-mono text-sm text-white mb-3">{paramLabel(gResult.best.params)}</p>
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
                    {[
                      { label: "거래 수",   value: `${gResult.best.total_trades}건` },
                      { label: "승률",      value: `${gResult.best.win_rate}%` },
                      { label: "수익팩터",  value: gResult.best.profit_factor >= 99 ? "∞" : String(gResult.best.profit_factor), color: "text-green-400" },
                      { label: "누적수익",  value: `${gResult.best.cumulative_pct >= 0 ? "+" : ""}${gResult.best.cumulative_pct}%`, color: gResult.best.cumulative_pct >= 0 ? "text-green-400" : "text-red-400" },
                      { label: "MDD",       value: `-${gResult.best.max_drawdown_pct}%`, color: "text-red-400" },
                    ].map(m => (
                      <div key={m.label} className="bg-gray-800/50 rounded-lg p-3 text-center">
                        <p className="text-xs text-gray-500">{m.label}</p>
                        <p className={`font-bold mt-1 ${m.color ?? "text-white"}`}>{m.value}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 상위 결과 테이블 */}
              <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
                <div className="px-5 py-3 border-b border-gray-800">
                  <h2 className="text-sm font-semibold text-gray-300">
                    상위 결과 (수익팩터 기준, {gResult.top_results.length}개)
                  </h2>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-gray-500 border-b border-gray-800">
                        {["순위","파라미터","거래수","승률","수익팩터","누적수익","MDD"].map(h => (
                          <th key={h} className="text-left px-4 py-2">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-800/60">
                      {gResult.top_results.map((r, i) => (
                        <tr key={i} className={`hover:bg-gray-800/40 ${i === 0 ? "bg-green-500/5" : ""}`}>
                          <td className="px-4 py-2 text-gray-400">{i + 1}</td>
                          <td className="px-4 py-2 font-mono text-xs text-gray-300">{paramLabel(r.params)}</td>
                          <td className="px-4 py-2">{r.total_trades}건</td>
                          <td className={`px-4 py-2 ${r.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>{r.win_rate}%</td>
                          <td className={`px-4 py-2 font-bold ${r.profit_factor >= 1.5 ? "text-green-400" : "text-red-400"}`}>
                            {r.profit_factor >= 99 ? "∞" : r.profit_factor}
                          </td>
                          <td className={`px-4 py-2 ${r.cumulative_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                            {r.cumulative_pct >= 0 ? "+" : ""}{r.cumulative_pct}%
                          </td>
                          <td className="px-4 py-2 text-red-400">-{r.max_drawdown_pct}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
