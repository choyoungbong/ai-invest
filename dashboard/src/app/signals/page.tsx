import { api } from "@/lib/api";
import { Card, Badge, ConfidenceBar } from "@/components/Card";
import { RunStrategyButton } from "@/components/ActionButtons";
import { AnalyzeAllButton, AnalyzeButton } from "@/components/AnalyzeButton";

export default async function SignalsPage() {
  const res = await api.signals(100).catch(() => ({ count: 0, data: [] }));
  const signals = res.data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">🟢 신호 목록</h1>
          <p className="text-sm text-gray-500 mt-0.5">돌파매매 전략 신호 전체</p>
        </div>
        <div className="flex gap-2">
          <AnalyzeAllButton />
          <RunStrategyButton />
        </div>
      </div>

      <Card title={`전체 신호 ${signals.length}건`}>
        {signals.length === 0 ? (
          <p className="text-gray-500 text-sm">신호 없음 — 우측 상단 전략 실행 버튼을 눌러주세요</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-3">종목</th>
                  <th className="text-left pb-3">유형</th>
                  <th className="text-right pb-3">현재가</th>
                  <th className="text-right pb-3">목표가</th>
                  <th className="text-right pb-3">손절가</th>
                  <th className="text-left pb-3 pl-4">신뢰도</th>
                  <th className="text-left pb-3 pl-4">AI 분석 / 이유</th>
                  <th className="text-right pb-3">시간</th>
                  <th className="text-center pb-3">분석</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {signals.map(s => (
                  <tr key={s.id} className="hover:bg-gray-800/40 transition-colors">
                    <td className="py-3">
                      <span className="font-medium">{s.name}</span>
                      <span className="text-gray-500 text-xs ml-1">{s.code}</span>
                    </td>
                    <td className="py-3"><Badge type={s.signal_type} /></td>
                    <td className="py-3 text-right font-mono">{s.price.toLocaleString()}</td>
                    <td className="py-3 text-right font-mono text-green-400">{s.target_price.toLocaleString()}</td>
                    <td className="py-3 text-right font-mono text-red-400">{s.stop_loss.toLocaleString()}</td>
                    <td className="py-3 pl-4 w-32"><ConfidenceBar value={s.confidence} /></td>
                    <td className="py-3 pl-4 max-w-xs">
                      {s.reason?.includes("**신호 강도**") ? (
                        <details className="cursor-pointer">
                          <summary className="text-xs text-purple-400 hover:text-purple-300">🤖 AI 분석 보기</summary>
                          <p className="text-xs text-gray-300 mt-1 whitespace-pre-wrap">{s.reason}</p>
                        </details>
                      ) : (
                        <p className="text-xs text-gray-400 truncate" title={s.reason}>{s.reason}</p>
                      )}
                    </td>
                    <td className="py-3 text-right text-xs text-gray-500 whitespace-nowrap">
                      {new Date(s.created_at).toLocaleString("ko-KR")}
                    </td>
                    <td className="py-3 text-center">
                      <AnalyzeButton signalId={s.id} />
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
