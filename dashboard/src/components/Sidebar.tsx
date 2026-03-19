"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const nav = [
  { href: "/",           label: "📊 대시보드" },
  { href: "/signals",    label: "🟢 신호 목록" },
  { href: "/scanner",    label: "🔍 거래대금 스캔" },
  { href: "/trades",     label: "🛒 체결 내역" },
  { href: "/charts",     label: "📈 차트 분석" },
  { href: "/backtest",   label: "🔬 백테스팅" },
  { href: "/allocation", label: "💰 자금 배분" },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="w-52 flex-shrink-0 bg-gray-900 border-r border-gray-800 flex flex-col">
      {/* 로고 */}
      <div className="px-5 py-5 border-b border-gray-800">
        <span className="text-xl font-bold text-indigo-400">AI INVEST</span>
        <p className="text-xs text-gray-500 mt-0.5">자동매매 대시보드</p>
      </div>

      {/* 메뉴 */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {nav.map(({ href, label }) => {
          const active = href === "/" ? path === "/" : path.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`block px-3 py-2 rounded-lg text-sm transition-colors ${
                active
                  ? "bg-indigo-600 text-white font-medium"
                  : "text-gray-400 hover:bg-gray-800 hover:text-white"
              }`}
            >
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="px-4 py-3 border-t border-gray-800 text-xs text-gray-600">
        v2.0.0 · All Phases
      </div>
    </aside>
  );
}
