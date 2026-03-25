"use client";
import { useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface Holding {
  code: string;
  name: string;
  quantity: number;
  avg_price: number;
  current_price: number;
  eval_amount: number;
  profit_loss: number;
}

interface Balance {
  total_eval: number;
  available_cash: number;
  total_profit: number;
  holdings: Holding[];
}

interface Trade {
  id: string;
  code: string;
  name: string;
  order_type: string;
  price: number;
  quantity: number;
  amount: number;
  status: string;
  created_at: string;
}

export default function PortfolioPage() {
  const [balance,  setBalance]  = useState<Balance | null>(null);
  const [trades,   setTrades]   = useState<Trade[]>([]);
  const [loading,  setLoading]  = useState(true);
  const [lastUpdate, setLastUpdate] = useState<string>("");

  async function fetchData() {
    try {
      const [balRes, tradeRes] = await Promise.all([
        fetch(`${BASE}/trade/balance`),
        fetch(`${BASE}/trades?limit=100`),
      ]);
      if (balRes.ok) setBalance(await balRes.json());
      if (tradeRes.ok) {
        const t = await tradeRes.json();
        setTrades(t.data ?? []);
      }
      setLastUpdate(new Date().toLocaleTimeString("ko-KR"));
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchData();
    // 30초마다 자동 갱신
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  // 오늘 거래만 필터
  const today = new Date().toLocaleDateString("ko-KR");
  const todayTrades = trades.filter(t =>
    new Date(t.created_at).toLocaleDateString("ko-KR") === today
  );
  const todayBuy  = todayTrades.filter(t => t.order_type === "BUY"  && t.status === "FILLED");
  const todaySell = todayTrades.filter(t => t.order_type === "SELL" && t.status === "FILLED");

  // 오늘 손익 계산
  const todayPnl = todaySell.reduce((sum, t) => {
    const buyTrade = trades.find(b =>
      b.code === t.code && b.order_type === "BUY" && b.status === "FILLED"
    );
    if (!buyTrade) return sum;
    return sum + (t.price - buyTrade.price) * t.quantity;
  }, 0);

  if (loading) return (
    <div className="flex items-center justify-center h-64 text-gray-500">
      잔고 조회 중...
    </div>
  );

  return (
    <div className="space-y-6">
      {/* 헤더 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">💼 포트폴리오</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            KIS 모의투자 실시간 잔고
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-500">마지막 갱신: {lastUpdate}</span>
          <button
            onClick={fetchData}
            className="px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-xs font-medium"
          >
            🔄 새로고침
          </button>
        </div>
      </div>

      {/* 계좌 요약 */}
      {balance && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            {
              label: "총 평가금액",
              value: `${balance.total_eval.toLocaleString()}원`,
              color: "text-white",
            },
            {
              label: "예수금 (익일)",
              value: `${balance.available_cash.toLocaleString()}원`,
              color: "text-indigo-300",
            },
            {
              label: "오늘 손익",
              value: `${todayPnl >= 0 ? "+" : ""}${todayPnl.toLocaleString()}원`,
              color: todayPnl >= 0 ? "text-green-400" : "text-red-400",
            },
            {
              label: "보유 종목",
              value: `${balance.holdings.length}개`,
              color: "text-yellow-300",
            },
          ].map(stat => (
            <div key={stat.label} className="rounded-xl border border-gray-800 bg-gray-900 p-4">
              <p className="text-xs text-gray-500 mb-1">{stat.label}</p>
              <p className={`text-xl font-bold ${stat.color}`}>{stat.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* 보유 종목 */}
      <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
        <div className="px-5 py-3 border-b border-gray-800">
          <h2 className="text-sm font-semibold text-gray-300">
            📊 보유 종목 ({balance?.holdings.length ?? 0}개)
          </h2>
        </div>
        <div className="p-5">
          {!balance || balance.holdings.length === 0 ? (
            <p className="text-sm text-gray-500">보유 종목 없음</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-500 border-b border-gray-800">
                    <th className="text-left pb-3">종목</th>
                    <th className="text-right pb-3">수량</th>
                    <th className="text-right pb-3">매수평균가</th>
                    <th className="text-right pb-3">현재가</th>
                    <th className="text-right pb-3">평가금액</th>
                    <th className="text-right pb-3">손익률</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {balance.holdings.map(h => (
                    <tr key={h.code} className="hover:bg-gray-800/40">
                      <td className="py-3">
                        <span className="font-medium">{h.name}</span>
                        <span className="text-xs text-gray-500 ml-1">{h.code}</span>
                      </td>
                      <td className="py-3 text-right">{h.quantity}주</td>
                      <td className="py-3 text-right font-mono">
                        {h.avg_price.toLocaleString()}
                      </td>
                      <td className="py-3 text-right font-mono">
                        {h.current_price.toLocaleString()}
                      </td>
                      <td className="py-3 text-right font-mono">
                        {h.eval_amount.toLocaleString()}
                      </td>
                      <td className={`py-3 text-right font-bold ${
                        h.profit_loss >= 0 ? "text-red-400" : "text-blue-400"
                      }`}>
                        {h.profit_loss >= 0 ? "+" : ""}{h.profit_loss.toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* 오늘 거래 내역 */}
      <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
        <div className="px-5 py-3 border-b border-gray-800">
          <h2 className="text-sm font-semibold text-gray-300">
            🛒 오늘 거래 내역 (매수 {todayBuy.length}건 / 매도 {todaySell.length}건)
          </h2>
        </div>
        <div className="p-5">
          {todayTrades.length === 0 ? (
            <p className="text-sm text-gray-500">오늘 거래 없음</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-500 border-b border-gray-800">
                    <th className="text-left pb-3">시간</th>
                    <th className="text-left pb-3">종목</th>
                    <th className="text-left pb-3">구분</th>
                    <th className="text-right pb-3">가격</th>
                    <th className="text-right pb-3">수량</th>
                    <th className="text-right pb-3">금액</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {todayTrades.map(t => (
                    <tr key={t.id} className="hover:bg-gray-800/40">
                      <td className="py-2 text-xs text-gray-500">
                        {new Date(t.created_at).toLocaleTimeString("ko-KR", {
                          hour: "2-digit", minute: "2-digit"
                        })}
                      </td>
                      <td className="py-2">
                        <span className="font-medium">{t.name}</span>
                        <span className="text-xs text-gray-500 ml-1">{t.code}</span>
                      </td>
                      <td className="py-2">
                        <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                          t.order_type === "BUY"
                            ? "bg-green-500/20 text-green-400"
                            : "bg-red-500/20 text-red-400"
                        }`}>
                          {t.order_type === "BUY" ? "매수" : "매도"}
                        </span>
                      </td>
                      <td className="py-2 text-right font-mono">{t.price.toLocaleString()}</td>
                      <td className="py-2 text-right">{t.quantity}주</td>
                      <td className="py-2 text-right font-mono">{t.amount.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
