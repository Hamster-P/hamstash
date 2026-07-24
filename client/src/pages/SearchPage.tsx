import { useState, useEffect } from "react";
import { Search as SearchIcon } from "lucide-react";
import { proxiedImageUrl } from "../utils/proxiedImage";

interface BangumiSubject {
  id: number;
  name: string;
  name_cn: string;
  date?: string;
  eps?: number;
  images?: { large?: string; common?: string };
  rating?: { score?: number };
}

interface SearchPageProps {
  onSelectAnime: (bgmId: number) => void;
  manualMatchFolder?: string | null;
}

const API_BASE = "http://127.0.0.1:8080";

// 搜索页从追更/搜索点进详情页时会整个卸载(App.tsx用selectedBgmId整页切换出DetailPage),
// 回来就是重新mount——关键词、年份/季度筛选、结果列表、翻页状态都要整体存起来一起恢复,
// 不然只存了results会出现"列表还在,但搜索框/筛选条件/加载更多状态都回到初始值"这种半吊子情况。
interface SearchSessionState {
  keyword: string;
  selectedYear: string;
  selectedQuarter: string;
  results: BangumiSubject[];
  offset: number;
  hasMore: boolean;
  hideGuochan: boolean;
}

const SEARCH_SESSION_KEY = "search_session_state_v1";

function loadSearchSession(): SearchSessionState | null {
  try {
    const raw = sessionStorage.getItem(SEARCH_SESSION_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as SearchSessionState;
  } catch {
    return null;
  }
}

function saveSearchSession(state: SearchSessionState) {
  try {
    sessionStorage.setItem(SEARCH_SESSION_KEY, JSON.stringify(state));
  } catch {
    // 存储满/被禁用时降级为纯内存,不影响本次渲染
  }
}

// 统一的下拉/输入控件样式,跟下载页/下载管理页保持一致
const controlClass =
  "rounded border border-border bg-surface px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion focus:ring-1 focus:ring-vermillion";

const YEAR_OPTIONS = ["不限", ...Array.from({ length: 8 }, (_, i) => String(2026 - i))];
const QUARTER_OPTIONS = [
  { label: "不限", value: "" },
  { label: "1月冬季番", value: "1" },
  { label: "4月春季番", value: "4" },
  { label: "7月夏季番", value: "7" },
  { label: "10月秋季番", value: "10" },
];

export default function SearchPage({ onSelectAnime, manualMatchFolder }: SearchPageProps) {
  // 1. 初始化时整体恢复上一次的搜索会话(关键词/筛选/结果/翻页状态),
  // 没有缓存就是全新状态,等用户自己点检索,不自动搜索
  const [initialSession] = useState(loadSearchSession);
  const [keyword, setKeyword] = useState(initialSession?.keyword ?? "");
  const [selectedYear, setSelectedYear] = useState(initialSession?.selectedYear ?? "不限");
  const [selectedQuarter, setSelectedQuarter] = useState(initialSession?.selectedQuarter ?? "");
  const [results, setResults] = useState<BangumiSubject[]>(initialSession?.results ?? []);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [offset, setOffset] = useState(initialSession?.offset ?? 0);
  const [hasMore, setHasMore] = useState(initialSession?.hasMore ?? false);
  // 隐藏国漫:默认开启,判断依据跟追更页一致——name_cn留空或者跟name完全一样,
  // 说明Bangumi没有另外记录一个不同的译名,基本就是国产动画本身就是中文标题。
  const [hideGuochan, setHideGuochan] = useState(initialSession?.hideGuochan ?? true);
  // 是否已经执行过至少一次搜索,用来区分"还没搜索"和"搜索了但没结果"两种空状态
  const [hasSearched, setHasSearched] = useState(initialSession !== null);

  // 2. 联动逻辑
  useEffect(() => {
    if (selectedYear === "不限") {
      setSelectedQuarter("");
    }
  }, [selectedYear]);

  // 3. 搜索函数;append=true 时是点击"加载更多",在当前 offset 之后追加结果
  const handleSearch = async (currentKeyword: string = keyword, append: boolean = false) => {
    const currentOffset = append ? offset : 0;
    if (append) {
      setLoadingMore(true);
    } else {
      setLoading(true);
      setHasSearched(true);
    }
    try {
      const params = new URLSearchParams();
      params.append("keyword", currentKeyword.trim());

      if (selectedYear !== "不限") params.append("year", selectedYear);
      if (selectedQuarter) params.append("month", selectedQuarter);
      params.append("offset", String(currentOffset));

      const res = await fetch(`${API_BASE}/bangumi/search?${params.toString()}`);
      const data = await res.json();

      const newResults = data?.data ?? [];
      const rawCount = data?.raw_count ?? newResults.length;
      const nextOffset = currentOffset + rawCount;
      // Bangumi 单次请求实际固定只返回一小批，不管传的 limit 是多少；
      // 真正的"还有没有下一页"要用它自己报的 bangumi_total 和已经翻到的位置比,
      // 不能拿 rawCount 和我们请求的 limit 比(rawCount 基本永远够不到 limit)。
      const bangumiTotal = data?.bangumi_total ?? nextOffset;
      const combined = append ? [...results, ...newResults] : newResults;
      const stillHasMore = rawCount > 0 && nextOffset < bangumiTotal;

      setResults(combined);
      setOffset(nextOffset);
      setHasMore(stillHasMore);
      saveSearchSession({
        keyword: currentKeyword,
        selectedYear,
        selectedQuarter,
        results: combined,
        offset: nextOffset,
        hasMore: stillHasMore,
        hideGuochan,
      });
    } catch (err) {
      console.error("搜索失败", err);
    } finally {
      setLoading(false);
      setLoadingMore(false);
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

  // 隐藏国漫开关本身不需要重新请求接口,现有results里直接筛,但要顺手更新一下
  // 会话缓存,不然从详情页返回后开关状态又会跳回默认值。
  const handleToggleHideGuochan = (checked: boolean) => {
    setHideGuochan(checked);
    saveSearchSession({
      keyword,
      selectedYear,
      selectedQuarter,
      results,
      offset,
      hasMore,
      hideGuochan: checked,
    });
  };

  // name_cn留空,或者跟name完全一样,基本就是国产动画本身就是中文标题——
  // 跟追更页(routers/tracking.py)用的是同一套判断口径。
  const visibleResults = hideGuochan
    ? results.filter((item) => item.name_cn && item.name_cn !== item.name)
    : results;

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
      <div className="grid grid-cols-[1fr_auto_auto_auto_auto] items-center gap-3 rounded-md border border-border p-3">
        <div className="relative w-full">
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="输入番剧名称（留空按推荐搜索）..."
            className="w-full rounded border border-border bg-surface py-1.5 pl-8 pr-3 text-sm text-paper outline-none placeholder:text-muted/60 focus:border-vermillion focus:ring-1 focus:ring-vermillion"
          />
          <SearchIcon className="absolute left-2.5 top-2.5 h-4 w-4 text-muted" />
        </div>

        <select
          value={selectedYear}
          onChange={(e) => setSelectedYear(e.target.value)}
          className={controlClass}
        >
          {YEAR_OPTIONS.map((y) => (
            <option key={y} value={y}>
              {y === "不限" ? "年份: 不限" : y}
            </option>
          ))}
        </select>

        <select
          value={selectedQuarter}
          onChange={(e) => setSelectedQuarter(e.target.value)}
          disabled={selectedYear === "不限"}
          className={`${controlClass} disabled:opacity-40`}
        >
          {QUARTER_OPTIONS.map((q) => (
            <option key={q.value} value={q.value}>
              {selectedYear === "不限" && q.value !== ""
                ? "需先选年份"
                : q.value === ""
                  ? "季度: 不限"
                  : q.label}
            </option>
          ))}
        </select>

        <label className="flex cursor-pointer items-center gap-1.5 font-mono text-xs text-muted select-none">
          <input
            type="checkbox"
            checked={hideGuochan}
            onChange={(e) => handleToggleHideGuochan(e.target.checked)}
            className="h-3.5 w-3.5 cursor-pointer accent-vermillion"
          />
          隐藏国漫
        </label>

        <button
          onClick={() => handleSearch()}
          disabled={loading}
          className="min-w-[84px] rounded-md border border-vermillion px-4 py-1.5 text-center font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
        >
          {loading ? "检索中..." : "检索"}
        </button>
      </div>

      {/* 列表渲染 */}
      <div className="flex-1 overflow-y-auto space-y-2">
        {visibleResults.map((item) => (
          <div
            key={item.id}
            onClick={() => onSelectAnime(item.id)}
            className="flex cursor-pointer items-center gap-4 rounded-md border border-border p-3 transition-colors hover:border-vermillion"
          >
            <div className="h-12 w-16 shrink-0 rounded bg-muted overflow-hidden">
              {item.images?.common && (
                <img
                  src={proxiedImageUrl(item.images.common)}
                  className="h-full w-full object-cover"
                />
              )}
            </div>
            <div className="flex-1 min-w-0">
              <div className="truncate text-sm">{item.name_cn || item.name}</div>
              <div className="truncate font-mono text-[11px] text-muted">{item.name}</div>
            </div>
            <div className="flex shrink-0 items-center gap-3 text-right">
              {item.rating?.score !== undefined && item.rating.score !== null && (
                <div className="font-mono text-xs font-bold text-amber-500">
                  ⭐ {item.rating.score.toFixed(1)}
                </div>
              )}
              <div className="font-mono text-xs text-muted w-24">{item.date || "—"}</div>
            </div>
          </div>
        ))}
        {hasMore && results.length > 0 && (
          <div className="flex justify-center py-3">
            <button
              onClick={() => handleSearch(keyword, true)}
              disabled={loadingMore}
              className="rounded-md border border-border px-4 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
            >
              {loadingMore ? "加载中..." : "加载更多"}
            </button>
          </div>
        )}
        {!loading && visibleResults.length === 0 && (
          <div className="text-center py-10 text-muted font-mono text-xs">
            {!hasSearched
              ? "输入番剧名称,点击检索开始查找"
              : results.length > 0
                ? "结果都是国漫,已被隐藏(可取消勾选查看)"
                : "未找到结果"}
          </div>
        )}
      </div>
    </div>
  );
}