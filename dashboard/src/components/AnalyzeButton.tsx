"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function AnalyzeButton({ signalId }: { signalId: string }) {
  const [loading, setLoading] = useState(false);
  const [done, setDone]       = useState(false);
  const router = useRouter();

  async function run() {
    setLoading(true);
    try {
      await fetch(`${BASE}/ai/analyze/${signalId}`, { method: "POST" });
      setDone(true);
      router.refresh();
    } catch {
      alert("AI 분석 실패");
    } finally {
      setLoading(false);
    }
  }

  if (done) return <span className="text-xs text-green-400">✅ 분석 완료</span>;

  return (
    <button
      onClick={run}
      disabled={loading}
      className="px-2 py-1 rounded text-xs bg-purple-600/30 hover:bg-purple-600/60 text-purple-300 disabled:opacity-40 transition-colors"
    >
      {loading ? "분석 중..." : "🤖 AI 분석"}
    </button>
  );
}

export function AnalyzeAllButton() {
  const [loading, setLoading] = useState(false);
  const [result, setResult]   = useState<string | null>(null);
  const router = useRouter();

  async function run() {
    setLoading(true);
    setResult(null);
    try {
      const res = await fetch(`${BASE}/ai/analyze-all`, { method: "POST" });
      const data = await res.json();
      setResult(`✅ ${data.message}`);
      router.refresh();
    } catch {
      setResult("❌ 실패");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center gap-3">
      {result && <span className="text-xs text-gray-400">{result}</span>}
      <button
        onClick={run}
        disabled={loading}
        className="px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-xs font-medium transition-colors"
      >
        {loading ? "분석 중..." : "🤖 AI 일괄 분석"}
      </button>
    </div>
  );
}
