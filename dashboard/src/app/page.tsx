import { api } from "@/lib/api";
import { StatCard, Card, Badge, ConfidenceBar } from "@/components/Card";
import { RunStrategyButton, CollectButton } from "@/components/ActionButtons";

function fmt(n: number) {
  if (n >= 1_000_000_000_000) return `${(n / 1_000_000_000_000).toFixed(1)}조`;
  if (n >= 100_000_000)        return `${(n / 100_000_000).toFixed(0)}억`;
  if (n >= 10_000)             return `${(n / 10_000).toFixed(0)}만`;
  return n.toLocaleString();
}

export default async function HomePage() {
  const [signalsRes, scanRes, tradesRes] = await Promise.allSettled([
    api.signals(10),
    api.topVolume(5),
    api.trades(5),
  ]);

  const signals = signalsRes.status === "fulfilled" ? signalsRes.value.data : [];
  const scan    = scanRes.status === "fulfilled"    ? scanRes.value.data    : [];
  const trades  = tradesRes.status === "fulfilled"  ? tradesRes.value.data  : [];

  const buyCount  = signals.filter(s => s.signal_type === "BUY").length;
  const execCount = signals.filter(s => s.is_executed).length;

  return (
    <div className="space-y-6">
      {/* 헤더 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">AI INVEST 대시보드</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {new Date().toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric", weekday: "short" })}
          </p>
        </div>
        <div className="flex gap-2">
          <CollectButton />
          <RunStrategyButton />
        </div>
      </div>

      {/* 통계 카드 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="전체 신호" value={signals.length} sub="최근 수신" color="indigo" />
        <StatCard label="BUY 신호" value={buyCount} sub="매수 기회" color="green" />
        <StatCard label="실행된 신호" value={execCount} sub="주문 완료" color="yellow" />
        <StatCard label="거래대금 상위" value={`${scan.length}종목`} sub="스캔 결과" color="indigo" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 최근 신호 */}
        <Card title="🟢 최근 신호">
          {signals.length === 0 ? (
            <p className="text-sm text-gray-500">신호 없음 — 전략을 실행하세요</p>
          ) : (
            <div className="space-y-3">
              {signals.map(s => (
                <div key={s.id} className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <Badge type={s.signal_type} />
                      <span className="text-sm font-medium truncate">{s.name}</span>
                      <span className="text-xs text-gray-500">{s.code}</span>
                    </div>
                    <ConfidenceBar value={s.confidence} />
                  </div>
                  <div className="text-right shrink-0">
                    <p className="text-sm font-semibold">{s.price.toLocaleString()}원</p>
                    <p className="text-xs text-green-400">→ {s.target_price.toLocaleString()}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* 거래대금 상위 */}
        <Card title="🔍 거래대금 상위 5">
          {scan.length === 0 ? (
            <p className="text-sm text-gray-500">데이터 없음 — 시세를 수집하세요</p>
          ) : (
            <div className="space-y-2">
              {scan.map(item => (
                <div key={item.code} className="flex items-center gap-3">
                  <span className="text-xs text-gray-500 w-4">{item.rank}</span>
                  <div className="flex-1">
                    <span className="text-sm font-medium">{item.name}</span>
                    <span className="text-xs text-gray-500 ml-1">{item.code}</span>
                  </div>
                  <div className="text-right">
                    <p className="text-sm">{item.close.toLocaleString()}원</p>
                    <p className={`text-xs font-medium ${item.change_rate >= 0 ? "text-red-400" : "text-blue-400"}`}>
                      {item.change_rate >= 0 ? "+" : ""}{item.change_rate.toFixed(2)}%
                    </p>
                  </div>
                  <div className="text-right w-16">
                    <p className="text-xs text-gray-400">{fmt(item.trading_value)}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {/* 최근 체결 */}
      <Card title="🛒 최근 체결 내역">
        {trades.length === 0 ? (
          <p className="text-sm text-gray-500">체결 내역 없음</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-2">종목</th>
                  <th className="text-left pb-2">유형</th>
                  <th className="text-right pb-2">가격</th>
                  <th className="text-right pb-2">수량</th>
                  <th className="text-right pb-2">총액</th>
                  <th className="text-right pb-2">상태</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {trades.map(t => (
                  <tr key={t.id}>
                    <td className="py-2">{t.name} <span className="text-gray-500">{t.code}</span></td>
                    <td className="py-2"><Badge type={t.order_type} /></td>
                    <td className="py-2 text-right">{t.price.toLocaleString()}</td>
                    <td className="py-2 text-right">{t.quantity}</td>
                    <td className="py-2 text-right">{t.amount.toLocaleString()}</td>
                    <td className="py-2 text-right">
                      <span className="text-xs text-yellow-400">{t.status}</span>
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
