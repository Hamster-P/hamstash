import {
  Calendar,
  Search,
  LibraryBig,
  Film,
  DownloadCloud,
  Rss,
  Settings,
  type LucideIcon,
} from "lucide-react";

export type View =
  | "tracking"
  | "search"
  | "library"
  | "movieLibrary"
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
  { key: "movieLibrary", label: "剧场版", icon: Film },
];

const settingsItem: { key: View; label: string; icon: LucideIcon } = {
  key: "settings",
  label: "设置",
  icon: Settings,
};

function NavButton({
  item,
  active,
  onChange,
}: {
  item: { key: View; label: string; icon: LucideIcon };
  active: View;
  onChange: (view: View) => void;
}) {
  const isActive = active === item.key;
  const Icon = item.icon;
  return (
    <button
      onClick={() => onChange(item.key)}
      className={`relative mx-2 flex w-[calc(100%-16px)] flex-col items-center gap-1 rounded-md py-2.5 transition-colors ${
        isActive ? "bg-vermillion/10 text-vermillion" : "text-muted hover:bg-surface-hover hover:text-paper"
      }`}
    >
      {isActive && <span className="absolute bottom-2 left-0 top-2 w-[3px] rounded-r bg-vermillion" />}
      <Icon size={20} strokeWidth={1.75} />
      <span className="text-[11px] leading-4">{item.label}</span>
    </button>
  );
}

export default function Sidebar({ active, onChange }: SidebarProps) {
  return (
    <nav className="flex w-[76px] shrink-0 flex-col gap-1 border-r border-border bg-surface py-3">
      {items.map((item) => (
        <NavButton key={item.key} item={item} active={active} onChange={onChange} />
      ))}
      <div className="flex-1" />
      <NavButton item={settingsItem} active={active} onChange={onChange} />
    </nav>
  );
}
