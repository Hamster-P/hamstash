import {
  Calendar,
  Search,
  LibraryBig,
  DownloadCloud,
  Rss,
  Settings,
  type LucideIcon,
} from "lucide-react";

export type View =
  | "tracking"
  | "search"
  | "library"
  | "download"
  | "downloadManager"
  | "rss"
  | "settings";

interface SidebarProps {
  active: View;
  onChange: (view: View) => void;
}

const items: { key: View; label: string; icon: LucideIcon }[] = [
  { key: "tracking", label: "追更", icon: Calendar },
  { key: "search", label: "搜索", icon: Search },
  { key: "downloadManager", label: "下载", icon: DownloadCloud },
  { key: "rss", label: "RSS", icon: Rss },
  { key: "library", label: "影视库", icon: LibraryBig },
  { key: "settings", label: "设置", icon: Settings },
];

export default function Sidebar({ active, onChange }: SidebarProps) {
  return (
    <nav className="flex w-[76px] shrink-0 flex-col items-center gap-1 border-r border-border bg-surface py-4">
      {items.map(({ key, label, icon: Icon }) => {
        const isActive = active === key;
        return (
          <button
            key={key}
            onClick={() => onChange(key)}
            className={`relative flex w-full flex-col items-center gap-1.5 py-3 transition-colors ${
              isActive ? "text-vermillion" : "text-muted hover:text-paper"
            }`}
          >
            {isActive && (
              <span className="absolute bottom-2 left-0 top-2 w-[3px] rounded-r bg-vermillion" />
            )}
            <Icon size={20} strokeWidth={1.75} />
            <span className="font-display text-[11px] tracking-wide">
              {label}
            </span>
          </button>
        );
      })}
    </nav>
  );
}
