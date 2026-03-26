"use client";
import { useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface RiskStatus {
  market_open: boolean;
  can_buy: boolean;
  block_reason: string;
  today_pnl: number;
  daily_loss_limit: number;
  positions: number;
  max_positions: number;
  daily_limit_hit: boolean;
}

export function RiskWidget() {
  const [status, setStatus] = useState<RiskStatus | null>(null);

  useEffect(() => {
    fetch(`${BASE}/risk/status`)
      .then(r => r.json())
      .then(setStatus)
      .catch(console.error);

    const interval = setInterval(() => {
      fetch(`${BASE}/risk/status`)
        .then(r => r.json())
        .then(setStatus)
        .catch(console.error);
    }, 30000);
    return () => clearInterval(interval);
  }, []);

  if (!status) return null;

  return (
    <div className={`rounded-xl border p-4 ${
      status.can_buy
        ? "border-green-500/40 bg-green-500/10"
        : "border-red-500/40 bg-red-500/10"
    }`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-300">🛡️ 리스크 상태</h3>
        <span className={`text-xs px-2 py-0.5 rounded font-bold ${
          status.can_buy
            ? "bg-green-500/20 text-green-400"
            : "bg-red-500/20 text-red-400"
        }`}>
          {status.can_buy ? "✅ 매수 가능" : "⛔ 매수 차단"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="flex justify-between">
          <span className="text-gray-500">장 운영</span>
          <span>{status.market_open ? "🟢 운영중" : "🔴 마감"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">보유 종목</span>
          <span className={status.positions >= status.max_positions ? "text-red-400" : "text-gray-300"}>
            {status.positions}/{status.max_positions}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">오늘 손익</span>
          <span className={status.today_pnl >= 0 ? "text-green-400" : "text-red-400"}>
            {status.today_pnl >= 0 ? "+" : ""}{status.today_pnl.toLocaleString()}원
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">손실 한도</span>
          <span className="text-gray-300">{status.daily_loss_limit.toLocaleString()}원</span>
        </div>
      </div>

      {!status.can_buy && status.block_reason && (
        <div className="mt-2 pt-2 border-t border-red-500/20">
          <p className="text-xs text-red-400">사유: {status.block_reason}</p>
        </div>
      )}

      {/* 손실 한도 프로그레스바 */}
      <div className="mt-3">
        <div className="flex justify-between text-xs text-gray-500 mb-1">
          <span>손실 한도 사용</span>
          <span>{Math.min(Math.abs(Math.min(status.today_pnl, 0)) / status.daily_loss_limit * 100, 100).toFixed(0)}%</span>
        </div>
        <div className="h-1.5 bg-gray-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              status.daily_limit_hit ? "bg-red-500" : "bg-yellow-500"
            }`}
            style={{
              width: `${Math.min(
                Math.abs(Math.min(status.today_pnl, 0)) / status.daily_loss_limit * 100,
                100
              )}%`
            }}
          />
        </div>
      </div>
    </div>
  );
}
