import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { Search as SearchIcon, ArrowLeft } from "lucide-react";
import BangumiResultsList, { type BangumiSubject } from "../components/BangumiResultsList";

interface SearchPageProps {
  onSelectAnime: (bgmId: number) => void;
  manualMatchFolder?: string | null;
  // 手动匹配模式下点"返回":取消绑定、退回影视库
  onCancelManualMatch?: () => void;
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
}

const SEARCH_SESSION_KEY = "search_session_state_v1";
// 列表滚动位置单独存:它会脱离"搜索"这个动作独立变化(用户只是滚动、没重新检索),
// 所以不塞进 SearchSessionState,单独一个 key。搜索页点进详情会整页卸载重挂(见
// App.tsx 的 selectedBgmId 三元),必须存到组件外才能在返回时恢复。
const SEARCH_SCROLL_KEY = "search_scroll_top_v1";

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

export default function SearchPage({ onSelectAnime, manualMatchFolder, onCancelManualMatch }: SearchPageProps) {
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
  // 是否已经执行过至少一次搜索,用来区分"还没搜索"和"搜索了但没结果"两种空状态
  const [hasSearched, setHasSearched] = useState(initialSession !== null);

  // 结果列表滚动容器:进详情再返回时恢复到离开时的滚动位置
  const listRef = useRef<HTMLDivElement>(null);
  const scrollSaveRaf = useRef<number | null>(null);

  // mount 后恢复上次的滚动位置。结果来自 initialSession(同步 state),首帧列表高度
  // 就已存在,useLayoutEffect 在 paint 前赋值 scrollTop,不会闪一下再跳回去。
  useLayoutEffect(() => {
    const saved = Number(sessionStorage.getItem(SEARCH_SCROLL_KEY) ?? 0);
    if (listRef.current && saved > 0) listRef.current.scrollTop = saved;
  }, []);

  // 滚动时把位置记下来(rAF 节流,避免每个滚动事件都同步写 sessionStorage 造成卡顿)
  const handleListScroll = () => {
    if (scrollSaveRaf.current !== null) return;
    scrollSaveRaf.current = requestAnimationFrame(() => {
      scrollSaveRaf.current = null;
      sessionStorage.setItem(SEARCH_SCROLL_KEY, String(listRef.current?.scrollTop ?? 0));
    });
  };

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
        <div className="flex items-center justify-between gap-3 rounded border border-vermillion/50 bg-vermillion/10 px-3 py-2 font-mono text-xs text-vermillion">
          <span className="min-w-0 truncate">
            正在为文件夹「{manualMatchFolder}」选择匹配的番剧
          </span>
          {onCancelManualMatch && (
            <button
              onClick={onCancelManualMatch}
              className="flex shrink-0 items-center gap-1 rounded border border-vermillion/60 px-2 py-1 text-vermillion transition-colors hover:bg-vermillion hover:text-ink"
            >
              <ArrowLeft size={12} />
              返回影视库
            </button>
          )}
        </div>
      )}

      {/* 搜索控制栏 */}
      <div className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-3 rounded-md border border-border p-3">
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

        <button
          onClick={() => handleSearch()}
          disabled={loading}
          className="min-w-[84px] rounded-md border border-vermillion px-4 py-1.5 text-center font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
        >
          {loading ? "检索中..." : "检索"}
        </button>
      </div>

      {/* 列表渲染 */}
      <div ref={listRef} onScroll={handleListScroll} className="flex-1 overflow-y-auto space-y-2">
        <BangumiResultsList
          results={results}
          onSelect={onSelectAnime}
          emptyText={!loading ? (hasSearched ? "未找到结果" : "输入番剧名称,点击检索开始查找") : ""}
        />
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
      </div>
    </div>
  );
}