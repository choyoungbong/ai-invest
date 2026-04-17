"use client";
import { useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface StrategyStats {
  total_trades:     number;
  win_count:        number;
  lose_count:       number;
  win_rate:         number;
  avg_profit_pct:   number;
  avg_win_pct:      number;
  avg_loss_pct:     number;
  profit_factor:    number;
  total_net_profit: number;
  avg_hold_days:    number;
}

interface PerformanceData {
  period_days:              number;
  total_trades:             number;
  total_win_rate:           number;
  total_net_profit:         number;
  overall_profit_factor:    number;
  by_strategy:              Record<string, StrategyStats>;
}

const STRATEGY_LABELS: Record<string, string> = {
  breakout:     "📈 돌파매매",
  ma_cross:     "🔀 MA 크로스",
  rsi_reversal: "🔄 RSI 반등",
  macd:         "📊 MACD",
  unknown:      "❓ 미분류",
};

const ALLOCATION: Record<string, string> = {
  breakout:     "40%",
  ma_cross:     "30%",
  rsi_reversal: "20%",
  macd:         "10%",
};

export default function PerformancePage() {
  const [days,    setDays]    = useState(30);
  const [data,    setData]    = useState<PerformanceData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  async function fetchData(d: number) {
    setLoading(true);
    setError(null);
    try {
      const res  = await fetch(`${BASE}/performance/by-strategy?days=${d}`);
      const json = await res.json();
      if (!res.ok) throw new Error(json.detail ?? "오류 발생");
      setData(json);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchData(days); }, [days]);

  const strategyEntries = data
    ? Object.entries(data.by_strategy).sort(
        (a, b) => b[1].total_net_profit - a[1].total_net_profit
      )
    : [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">🏆 전략별 성과</h1>
          <p className="text-sm text-gray-500 mt-0.5">실제 체결 내역 기반 전략 수익률 분석</p>
        </div>
        <div className="flex gap-2">
          {[7, 14, 30, 90].map(d => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                days === d
                  ? "bg-indigo-600 text-white font-medium"
                  : "bg-gray-800 text-gray-400 hover:text-white"
              }`}
            >
              {d}일
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-400 text-sm">
          ❌ {error}
        </div>
      )}

      {loading && (
        <div className="text-center text-gray-500 py-12">데이터 로딩 중...</div>
      )}

      {data && !loading && (
        <>
          {/* 전체 요약 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[
              { label: "총 거래",       value: `${data.total_trades}건`,
                color: "text-white" },
              { label: "전체 승률",     value: `${data.total_win_rate}%`,
                color: data.total_win_rate >= 50 ? "text-green-300" : "text-red-300" },
              { label: "총 순손익",     value: `${data.total_net_profit >= 0 ? "+" : ""}${data.total_net_profit.toLocaleString()}원`,
                color: data.total_net_profit >= 0 ? "text-green-300" : "text-red-300" },
              { label: "전체 수익팩터", value: data.overall_profit_factor >= 99 ? "∞" : String(data.overall_profit_factor),
                color: data.overall_profit_factor >= 1.5 ? "text-green-300" : "text-red-300" },
            ].map(stat => (
              <div key={stat.label} className="rounded-xl border border-gray-800 bg-gray-900 p-4 text-center">
                <p className="text-xs text-gray-500 mb-1">{stat.label}</p>
                <p className={`text-xl font-bold ${stat.color}`}>{stat.value}</p>
              </div>
            ))}
          </div>

          {/* 전략별 카드 */}
          {strategyEntries.length === 0 ? (
            <div className="rounded-xl border border-gray-800 bg-gray-900 p-8 text-center">
              <p className="text-gray-500 text-sm">최근 {days}일 내 체결 내역이 없습니다.</p>
            </div>
          ) : (
            <div className="space-y-4">
              {strategyEntries.map(([strategy, s]) => {
                const isProfitable = s.total_net_profit >= 0;
                const label        = STRATEGY_LABELS[strategy] ?? strategy;
                const alloc        = ALLOCATION[strategy] ?? "-";

                return (
                  <div key={strategy} className="rounded-xl border border-gray-800 bg-gray-900 p-5">
                    {/* 헤더 */}
                    <div className="flex items-center justify-between mb-4">
                      <div className="flex items-center gap-3">
                        <h2 className="text-base font-semibold text-white">{label}</h2>
                        <span className="text-xs bg-indigo-900/50 text-indigo-400 px-2 py-0.5 rounded">
                          예산 배분 {alloc}
                        </span>
                      </div>
                      <div className={`text-lg font-bold ${isProfitable ? "text-green-400" : "text-red-400"}`}>
                        {isProfitable ? "+" : ""}{s.total_net_profit.toLocaleString()}원
                      </div>
                    </div>

                    {/* 핵심 지표 */}
                    <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
                      {[
                        { label: "거래",     value: `${s.total_trades}건` },
                        { label: "승률",     value: `${s.win_rate}%`,
                          color: s.win_rate >= 50 ? "text-green-400" : "text-red-400" },
                        { label: "수익팩터", value: s.profit_factor >= 99 ? "∞" : String(s.profit_factor),
                          color: s.profit_factor >= 1.5 ? "text-green-400" : "text-red-400" },
                        { label: "평균수익", value: `${s.avg_profit_pct >= 0 ? "+" : ""}${s.avg_profit_pct}%`,
                          color: s.avg_profit_pct >= 0 ? "text-green-400" : "text-red-400" },
                        { label: "평균이익", value: `+${s.avg_win_pct}%`,  color: "text-green-400" },
                        { label: "평균손실", value: `${s.avg_loss_pct}%`,  color: "text-red-400" },
                      ].map(m => (
                        <div key={m.label} className="bg-gray-800/50 rounded-lg p-3 text-center">
                          <p className="text-xs text-gray-500 mb-1">{m.label}</p>
                          <p className={`font-bold text-sm ${m.color ?? "text-white"}`}>{m.value}</p>
                        </div>
                      ))}
                    </div>

                    {/* 승/패 + 평균 보유일 */}
                    <div className="flex items-center gap-4 mt-3 text-xs text-gray-500">
                      <span>✅ 이익 {s.win_count}건</span>
                      <span>🔴 손실 {s.lose_count}건</span>
                      <span>📅 평균 보유 {s.avg_hold_days}일</span>
                    </div>

                    {/* 승률 바 */}
                    <div className="mt-3 h-2 bg-gray-800 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-gradient-to-r from-green-600 to-green-500 rounded-full"
                        style={{ width: `${Math.min(s.win_rate, 100)}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* 자금 배분 현황 */}
          <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
            <h2 className="text-sm font-semibold text-gray-300 mb-3">💰 현재 자금 배분 설정</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {Object.entries(ALLOCATION).map(([strategy, ratio]) => {
                const stats = data.by_strategy[strategy];
                return (
                  <div key={strategy} className="bg-gray-800/50 rounded-lg p-3">
                    <p className="text-xs text-gray-400">{STRATEGY_LABELS[strategy] ?? strategy}</p>
                    <p className="text-lg font-bold text-indigo-400 mt-1">{ratio}</p>
                    {stats ? (
                      <p className={`text-xs mt-1 ${stats.total_net_profit >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {stats.total_net_profit >= 0 ? "+" : ""}{stats.total_net_profit.toLocaleString()}원
                      </p>
                    ) : (
                      <p className="text-xs text-gray-600 mt-1">거래 없음</p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
