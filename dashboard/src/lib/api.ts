const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    next: { revalidate: 0 },   // 항상 최신 데이터
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
  return res.json() as Promise<T>;
}

// ── 타입 ─────────────────────────────────────────────────────────────────────

export interface Signal {
  id: string;
  code: string;
  name: string;
  signal_type: "BUY" | "SELL";
  strategy: string;
  price: number;
  target_price: number;
  stop_loss: number;
  reason: string;
  confidence: number;
  is_executed: boolean;
  created_at: string;
}

export interface ScanItem {
  rank: number;
  code: string;
  name: string;
  close: number;
  volume: number;
  trading_value: number;
  change_rate: number;
  timestamp: string;
}

export interface Trade {
  id: string;
  signal_id: string;
  code: string;
  name: string;
  order_type: "BUY" | "SELL";
  price: number;
  quantity: number;
  amount: number;
  status: string;
  created_at: string;
}

// ── API 함수 ──────────────────────────────────────────────────────────────────

export const api = {
  health: () =>
    apiFetch<{ status: string }>("/health"),

  signals: (limit = 50) =>
    apiFetch<{ count: number; data: Signal[] }>(`/signals?limit=${limit}`),

  topVolume: (topN = 30) =>
    apiFetch<{ count: number; data: ScanItem[] }>(`/scanner/top-volume?top_n=${topN}`),

  trades: (limit = 50) =>
    apiFetch<{ count: number; data: Trade[] }>(`/trades?limit=${limit}`),

  runStrategy: () =>
    apiFetch<{ message: string; candidates: number; signals: number; data: Signal[] }>(
      "/strategy/run",
      { method: "POST" }
    ),

  collectData: () =>
    apiFetch<{ message: string }>("/collector/collect", { method: "POST" }),

  notifyTest: () =>
    apiFetch<{ message: string }>("/notification/test", { method: "POST" }),
};
