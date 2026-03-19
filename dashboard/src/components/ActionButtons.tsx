"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { useRouter } from "next/navigation";

export function RunStrategyButton() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const router = useRouter();

  async function run() {
    setLoading(true);
    setResult(null);
    try {
      const res = await api.runStrategy();
      setResult(`✅ 신호 ${res.signals}건 발생 (후보 ${res.candidates}개)`);
      router.refresh();
    } catch {
      setResult("❌ 전략 실행 실패");
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
        className="px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-xs font-medium transition-colors"
      >
        {loading ? "실행 중..." : "🚀 전략 실행"}
      </button>
    </div>
  );
}

export function CollectButton() {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const router = useRouter();

  async function run() {
    setLoading(true);
    setResult(null);
    try {
      const res = await api.collectData();
      setResult(`✅ ${res.message}`);
      router.refresh();
    } catch {
      setResult("❌ 수집 실패");
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
        className="px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-xs font-medium transition-colors"
      >
        {loading ? "수집 중..." : "📥 시세 수집"}
      </button>
    </div>
  );
}
