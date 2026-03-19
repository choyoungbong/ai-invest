export function StatCard({
  label,
  value,
  sub,
  color = "indigo",
}: {
  label: string;
  value: string | number;
  sub?: string;
  color?: "indigo" | "green" | "red" | "yellow";
}) {
  const ring: Record<string, string> = {
    indigo: "border-indigo-500/40 bg-indigo-500/10",
    green:  "border-green-500/40  bg-green-500/10",
    red:    "border-red-500/40    bg-red-500/10",
    yellow: "border-yellow-500/40 bg-yellow-500/10",
  };
  const text: Record<string, string> = {
    indigo: "text-indigo-300",
    green:  "text-green-300",
    red:    "text-red-300",
    yellow: "text-yellow-300",
  };
  return (
    <div className={`rounded-xl border p-4 ${ring[color]}`}>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`text-2xl font-bold ${text[color]}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
    </div>
  );
}

export function Card({
  title,
  children,
  action,
}: {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800">
        <h2 className="text-sm font-semibold text-gray-300">{title}</h2>
        {action}
      </div>
      <div className="p-5">{children}</div>
    </div>
  );
}

export function Badge({ type }: { type: "BUY" | "SELL" | string }) {
  if (type === "BUY")
    return <span className="px-2 py-0.5 rounded text-xs font-bold bg-green-500/20 text-green-400">BUY</span>;
  if (type === "SELL")
    return <span className="px-2 py-0.5 rounded text-xs font-bold bg-red-500/20 text-red-400">SELL</span>;
  return <span className="px-2 py-0.5 rounded text-xs bg-gray-700 text-gray-300">{type}</span>;
}

export function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "bg-green-500" : pct >= 40 ? "bg-yellow-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 rounded-full bg-gray-700">
        <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-400 w-8 text-right">{pct}%</span>
    </div>
  );
}
