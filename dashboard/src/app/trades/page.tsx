import { api } from "@/lib/api";
import { Card, Badge } from "@/components/Card";

export default async function TradesPage() {
  const res = await api.trades(100).catch(() => ({ count: 0, data: [] }));
  const trades = res.data;

  const totalAmount = trades.reduce((s, t) => s + t.amount, 0);
  const buyCount  = trades.filter(t => t.order_type === "BUY").length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">🛒 체결 내역</h1>
        <p className="text-sm text-gray-500 mt-0.5">시뮬레이션 주문 기록</p>
      </div>

      {/* 요약 */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: "전체 주문", value: trades.length },
          { label: "매수 주문", value: buyCount },
          { label: "총 거래금액", value: totalAmount.toLocaleString() + "원" },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-xl border border-gray-800 bg-gray-900 p-4">
            <p className="text-xs text-gray-500">{label}</p>
            <p className="text-xl font-bold text-white mt-1">{value}</p>
          </div>
        ))}
      </div>

      <Card title={`전체 ${trades.length}건`}>
        {trades.length === 0 ? (
          <p className="text-gray-500 text-sm">체결 내역 없음</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-3">종목</th>
                  <th className="text-left pb-3">유형</th>
                  <th className="text-right pb-3">가격</th>
                  <th className="text-right pb-3">수량</th>
                  <th className="text-right pb-3">총액</th>
                  <th className="text-left pb-3 pl-4">상태</th>
                  <th className="text-right pb-3">시간</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {trades.map(t => (
                  <tr key={t.id} className="hover:bg-gray-800/40 transition-colors">
                    <td className="py-3">
                      <span className="font-medium">{t.name}</span>
                      <span className="text-xs text-gray-500 ml-1">{t.code}</span>
                    </td>
                    <td className="py-3"><Badge type={t.order_type} /></td>
                    <td className="py-3 text-right font-mono">{t.price.toLocaleString()}</td>
                    <td className="py-3 text-right">{t.quantity}주</td>
                    <td className="py-3 text-right font-mono font-medium">{t.amount.toLocaleString()}</td>
                    <td className="py-3 pl-4">
                      <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400">{t.status}</span>
                    </td>
                    <td className="py-3 text-right text-xs text-gray-500">
                      {new Date(t.created_at).toLocaleString("ko-KR")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
