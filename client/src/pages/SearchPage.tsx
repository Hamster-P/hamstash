import { useState, useEffect } from "react";
import { Search as SearchIcon } from "lucide-react";

interface BangumiSubject {
  id: number;
  name: string;
  name_cn: string;
  date?: string;
  eps?: number;
  images?: { large?: string; common?: string };
}

interface SearchPageProps {
  onSelectAnime: (bgmId: number) => void;
  manualMatchFolder?: string | null;
}

const API_BASE = "http://127.0.0.1:8080";

const YEAR_OPTIONS = ["不限", ...Array.from({ length: 8 }, (_, i) => String(2026 - i))];
const QUARTER_OPTIONS = [
  { label: "不限", value: "" },
  { label: "1月冬季番", value: "1" },
  { label: "4月春季番", value: "4" },
  { label: "7月夏季番", value: "7" },
  { label: "10月秋季番", value: "10" },
];

export default function SearchPage({ onSelectAnime, manualMatchFolder }: SearchPageProps) {
  const [keyword, setKeyword] = useState("");
  const [selectedYear, setSelectedYear] = useState("不限");
  const [selectedQuarter, setSelectedQuarter] = useState("");
  const [results, setResults] = useState<BangumiSubject[]>([]);
  const [loading, setLoading] = useState(false);
  // 是否已经执行过至少一次搜索,用来区分"还没搜索"和"搜索了但没结果"两种空状态
  const [hasSearched, setHasSearched] = useState(false);

  // 1. 初始化时尝试读取缓存,没有缓存就等用户自己点检索,不自动搜索
  useEffect(() => {
    const saved = sessionStorage.getItem("last_search_results");
    if (saved) {
      try {
        setResults(JSON.parse(saved));
        setHasSearched(true);
      } catch (e) {
        console.error("缓存解析失败", e);
      }
    }
  }, []);

  // 2. 联动逻辑
  useEffect(() => {
    if (selectedYear === "不限") {
      setSelectedQuarter("");
    }
  }, [selectedYear]);

  // 3. 搜索函数
  const handleSearch = async (currentKeyword: string = keyword) => {
    setLoading(true);
    setHasSearched(true);
    try {
      const params = new URLSearchParams();
      params.append("keyword", currentKeyword.trim());
      
      if (selectedYear !== "不限") params.append("year", selectedYear);
      if (selectedQuarter) params.append("month", selectedQuarter);

      const res = await fetch(`${API_BASE}/bangumi/search?${params.toString()}`);
      const data = await res.json();
      
      const newResults = data?.data ?? [];
      setResults(newResults);
      sessionStorage.setItem("last_search_results", JSON.stringify(newResults));
    } catch (err) {
      console.error("搜索失败", err);
    } finally {
      setLoading(false);
    }
  };

  // 4. 手动匹配模式:带着文件夹名进来时,自动填入关键词并立即检索一次
  useEffect(() => {
    if (manualMatchFolder) {
      setKeyword(manualMatchFolder);
      handleSearch(manualMatchFolder);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manualMatchFolder]);

  return (
    <div className="flex flex-col h-full space-y-4 p-4">
      {/* 顶部标题 + 说明,跟追更/下载页保持一致 */}
      <div>
        <h1 className="mb-1 font-display text-2xl tracking-tight">搜索</h1>
        <p className="font-mono text-xs text-muted">
          搜索历史番剧,支持按年份、季度筛选
        </p>
      </div>

      {manualMatchFolder && (
        <div className="rounded border border-vermillion/50 bg-vermillion/10 px-3 py-2 font-mono text-xs text-vermillion">
          正在为文件夹「{manualMatchFolder}」选择匹配的番剧
        </div>
      )}

      {/* 搜索控制栏 */}
      <div className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-4 bg-transparent p-3 rounded border border-border">
        <div className="relative w-full">
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="输入番剧名称（留空按推荐搜索）..."
            className="w-full rounded border border-border bg-transparent py-1.5 pl-8 pr-3 font-mono text-sm outline-none focus:border-vermillion placeholder:text-muted"
          />
          <SearchIcon className="absolute left-2.5 top-2.5 h-4 w-4 text-muted" />
        </div>

        <select
          value={selectedYear}
          onChange={(e) => setSelectedYear(e.target.value)}
          className="rounded border border-border bg-ink px-2 py-1.5 font-mono text-sm h-[34px] cursor-pointer"
        >
          {YEAR_OPTIONS.map((y) => (
            <option key={y} value={y} className="bg-ink">
              {y === "不限" ? "年份: 不限" : y}
            </option>
          ))}
        </select>

        <select
          value={selectedQuarter}
          onChange={(e) => setSelectedQuarter(e.target.value)}
          disabled={selectedYear === "不限"}
          className="rounded border border-border bg-ink px-2 py-1.5 font-mono text-sm h-[34px] cursor-pointer disabled:opacity-40"
        >
          {QUARTER_OPTIONS.map((q) => (
            <option key={q.value} value={q.value} className="bg-ink">
              {selectedYear === "不限" && q.value !== ""
                ? "需先选年份"
                : q.value === ""
                  ? "季度: 不限"
                  : q.label}
            </option>
          ))}
        </select>

        <button
          onClick={() => handleSearch()}
          disabled={loading}
          className="rounded bg-vermillion px-5 h-[34px] font-mono text-sm text-white font-semibold hover:bg-opacity-90 disabled:opacity-50"
        >
          {loading ? "检索中..." : "检索"}
        </button>
      </div>

      {/* 列表渲染 */}
      <div className="flex-1 overflow-y-auto space-y-2">
        {results.map((item) => (
          <div
            key={item.id}
            onClick={() => onSelectAnime(item.id)}
            className="flex cursor-pointer items-center gap-4 rounded border border-border p-3 hover:border-vermillion transition-colors"
          >
            <div className="h-12 w-16 shrink-0 rounded bg-muted overflow-hidden">
              {item.images?.common && <img src={item.images.common} className="h-full w-full object-cover" />}
            </div>
            <div className="flex-1 min-w-0">
              <div className="truncate text-sm">{item.name_cn || item.name}</div>
              <div className="truncate font-mono text-[11px] text-muted">{item.name}</div>
            </div>
            <div className="text-right font-mono text-xs text-muted w-24">{item.date || "—"}</div>
          </div>
        ))}
        {!loading && results.length === 0 && (
          <div className="text-center py-10 text-muted font-mono text-xs">
            {hasSearched ? "未找到结果" : "输入番剧名称,点击检索开始查找"}
          </div>
        )}
      </div>
    </div>
  );
}