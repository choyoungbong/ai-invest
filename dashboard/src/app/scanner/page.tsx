import { api } from "@/lib/api";
import { Card } from "@/components/Card";
import { CollectButton } from "@/components/ActionButtons";

function fmtValue(n: number) {
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(0)}억`;
  if (n >= 10_000)      return `${(n / 10_000).toFixed(0)}만`;
  return n.toLocaleString();
}

export default async function ScannerPage() {
  const res = await api.topVolume(50).catch(() => ({ count: 0, data: [] }));
  const items = res.data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">🔍 거래대금 스캔</h1>
          <p className="text-sm text-gray-500 mt-0.5">KOSPI + KOSDAQ 거래대금 상위 종목</p>
        </div>
        <CollectButton />
      </div>

      <Card title={`거래대금 상위 ${items.length}종목`}>
        {items.length === 0 ? (
          <p className="text-gray-500 text-sm">데이터 없음 — 시세 수집 버튼을 눌러주세요</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 border-b border-gray-800">
                  <th className="text-center pb-3 w-10">순위</th>
                  <th className="text-left pb-3">종목</th>
                  <th className="text-right pb-3">현재가</th>
                  <th className="text-right pb-3">등락률</th>
                  <th className="text-right pb-3">거래량</th>
                  <th className="text-right pb-3">거래대금</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {items.map(item => (
                  <tr key={item.code} className="hover:bg-gray-800/40 transition-colors">
                    <td className="py-2.5 text-center text-gray-500">{item.rank}</td>
                    <td className="py-2.5">
                      <span className="font-medium">{item.name}</span>
                      <span className="text-xs text-gray-500 ml-1">{item.code}</span>
                    </td>
                    <td className="py-2.5 text-right font-mono">{item.close.toLocaleString()}</td>
                    <td className={`py-2.5 text-right font-medium ${item.change_rate >= 0 ? "text-red-400" : "text-blue-400"}`}>
                      {item.change_rate >= 0 ? "+" : ""}{item.change_rate.toFixed(2)}%
                    </td>
                    <td className="py-2.5 text-right text-gray-400">{fmtValue(item.volume)}</td>
                    <td className="py-2.5 text-right text-indigo-300 font-medium">{fmtValue(item.trading_value)}</td>
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
