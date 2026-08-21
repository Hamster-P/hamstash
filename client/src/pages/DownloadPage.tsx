import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ExternalLink, Loader2 } from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";

interface ResourceItem {
  source: string;
  provider: string | null;
  title: string;
  fansub_name: string;
  magnet: string;
  size: number | null;
  created_at: string | null;
  bgm_id: number | null;
  // 种子在源站的详情页地址,目前只有dmhy/nyaa有(AnimeGarden是聚合站,
  // 没有自己的规范详情页,后端固定给null,这里为null时不渲染详情按钮)。
  detail_url: string | null;
}

interface RenamePreview {
  original_title: string;
  media_type: string;
  parsed_episode: string;
  parsed_resolution: string;
  parsed_fansub: string;
  target_folder: string;
  target_filename: string;
  target_full_path: string;
}

interface DownloadPageProps {
  initialKeyword?: string;
  initialBgmId?: number | null;
  initialSubscribe?: boolean;
  onBack?: () => void;
}

const API_BASE = "http://127.0.0.1:8080";
const QUALITY_OPTIONS = ["不限", "1080p", "720p", "2160p"];
// 数据源清单不再前端硬编码,改由后端 GET /resources/sources 动态下发(见 sources state),
// 新增/停用/改地址都在设置页配置,前端只渲染 enabled 的那些。
// 1. 新增：细化的字幕语言与文件格式选项
const SUBTITLE_OPTIONS = ["不限", "简体", "繁体", "简繁", "简日", "繁日", "RAW", "日文/无字"];
const FORMAT_OPTIONS = ["不限", "MKV", "MP4"];
const TYPE_OPTIONS = ["不限", "单集(追更)", "合集/全集(完结)"];

// 统一的表单控件样式:自定义焦点环颜色,避免浏览器默认焦点框和主题不搭
const controlClass =
  "rounded border border-border bg-surface px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion focus:ring-1 focus:ring-vermillion";
  
function formatSize(bytes: number | null) {
  if (!bytes) return "—";
  const gb = bytes / 1024 / 1024 / 1024;
  return gb >= 1
    ? `${gb.toFixed(2)} GB`
    : `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

// 后端下发的下载源元信息(GET /resources/sources 里 enabled 的那些)
interface SourceMeta {
  id: string;
  label: string;
  supports_subject: boolean;
  rate_limit_cooldown_ms: number | null;
  rss_window_cap: number | null;
}

export default function DownloadPage({
  initialKeyword,
  initialBgmId = null,
  initialSubscribe = false,
  onBack,
}: DownloadPageProps) {
  const [searchBox, setSearchBox] = useState(initialKeyword ?? "");
  const [bgmId] = useState<number | null>(initialBgmId);
  const [fansubFilter, setFansubFilter] = useState("全部");
  // 可选数据源(后端下发,只含 enabled 的);source 是当前选中的源 id
  const [sources, setSources] = useState<SourceMeta[]>([]);
  const [source, setSource] = useState<string>("dmhy");

  // 2. 状态扩展：全面控管筛选条件
  const [quality, setQuality] = useState("不限");
  const [subtitle, setSubtitle] = useState("不限");
  const [format, setFormat] = useState("不限");
  const [releaseType, setReleaseType] = useState("不限");

  const [results, setResults] = useState<ResourceItem[]>([]);
  // 当前数据源RSS/Feed接口的固定窗口上限——配合results.length(已经翻页累加到手的
  // 真实条数)判断"开RSS订阅是不是覆盖不到全部历史存量",见下面超限提示。
  const [rssWindowCap, setRssWindowCap] = useState<number | null>(null);
  // 翻页状态:每次搜索/切源都从第1页重新开始,点"加载更多"在当前page之后追加一页——
  // 服务端不会自己预取多页(dmhy有防刷机制,一次性拉太多页会被直接拉黑一段时间,
  // 吃过一次亏),翻页必须由用户主动点击驱动,天然把请求间隔开。
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loading, setLoading] = useState(false);
  // 有的源(dmhy)有防刷机制,短时间内多次搜索会被直接限流一段时间(之前吃过亏,连浏览器
  // 手动搜都不行)——服务端只做到"不再一次性预取多页",防不住用户自己在"检索"/
  // "加载更多"之间快速来回点。这里提前在按钮层面禁用几秒,冷却时长由源自己的
  // rate_limit_cooldown_ms 元信息决定(后端下发),没配这个值的源(nyaa/AnimeGarden)不冷却。
  const [coolingDown, setCoolingDown] = useState(false);
  const cooldownTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (cooldownTimerRef.current) clearTimeout(cooldownTimerRef.current);
    };
  }, []);
  // 当前选中源的冷却时长(毫秒),null=不冷却
  const currentCooldownMs = sources.find((s) => s.id === source)?.rate_limit_cooldown_ms ?? null;
  // 是否已经执行过至少一次检索,用来判断"还没搜"和"搜了但真的没有结果"
  const [hasSearched, setHasSearched] = useState(false);
  // 字幕组下拉框选项,只从"没按字幕组过滤"的查询结果里采集(见handleSearch里的
  // 更新逻辑),不能直接拿当前results算,不然选中某个字幕组之后下拉框就只剩它自己了。
  const [fansubOptionNames, setFansubOptionNames] = useState<string[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // 记录最近一次点开的详情页地址(用detail_url本身当key,天然跨源唯一),
  // 让对应那一行的"详情"按钮高亮,方便用户从浏览器切回来后找到自己刚才点的是哪条
  const [lastOpenedDetail, setLastOpenedDetail] = useState<string | null>(null);

  const [subscribe, setSubscribe] = useState(initialSubscribe);
  // 是否并进这部作品所属的Bangumi家族(高达/柯南这类系列的最早那个条目)。
  // 打开(默认)=同系列共用一个媒体库文件夹、按第几季组织,与历史行为一致;
  // 关闭=这一部独立成一部,在媒体库单独成卡(比如高达SEED不想跟初代高达混在一起)。
  const [mergeToFamily, setMergeToFamily] = useState(true);
  const [autoRename, setAutoRename] = useState(true);
  const [previews, setPreviews] = useState<RenamePreview[]>([]);
  // 改名规则(季度序号)可能还在后台算——不在预览请求里同步现算阻塞交互,
  // 命中缓存才是"ready",没命中是"pending",UI据此显示"计算中"而不是空白/报错
  const [previewStatus, setPreviewStatus] = useState<"ready" | "pending">("ready");
  const [submitting, setSubmitting] = useState(false);
  const [resultMessage, setResultMessage] = useState<string | null>(null);

  // append=true 是点"加载更多",在当前page之后追加一页,不重置结果/勾选状态。
  // filterOverrides是给"切数据源"这类"改了筛选项要立刻用新值重新搜"的场景用的——
  // React状态更新是异步的,onChange里setFansubFilter(val)之后紧接着调用
  // handleSearch(),这里读到的fansubFilter闭包变量还是旧值,必须显式把新值传进来。
  const handleSearch = async (
    keywordOverride?: string,
    sourceOverride?: typeof source,
    append: boolean = false,
    filterOverrides?: { fansub?: string; quality?: string; subtitle?: string; format?: string; release?: string },
  ) => {
    const kw = keywordOverride ?? searchBox;
    if (!kw.trim()) return;
    const src = sourceOverride ?? source;
    const cooldownMs = sources.find((s) => s.id === src)?.rate_limit_cooldown_ms ?? null;
    // 冷却中直接拒绝这次请求,不管是不是append——防的就是"检索"和"加载更多"
    // 来回快速点绕开单个按钮自身的disabled保护。
    if (cooldownMs && coolingDown) return;
    const currentPage = append ? page + 1 : 1;
    if (append) {
      setLoadingMore(true);
    } else {
      setLoading(true);
      setHasSearched(true);
      setSelected(new Set());
      setResultMessage(null);
    }
    // 请求一发出去就立刻进入冷却,不等响应回来——防的是请求频率本身,不是响应结果,
    // 哪怕这次请求本身很慢,也不能让用户在等待期间再点一次触发第二个并发请求。
    if (cooldownMs) {
      setCoolingDown(true);
      if (cooldownTimerRef.current) clearTimeout(cooldownTimerRef.current);
      cooldownTimerRef.current = setTimeout(() => setCoolingDown(false), cooldownMs);
    }
    try {
      // 字幕组/画质/字幕语言/格式现在是真正发给站点服务端的查询条件,不是"拿到
      // 结果之后本地筛"——同一个选项在本地filteredResults里还会照旧再筛一遍,
      // 服务端这层只负责把搜索范围从"一页"扩大到站点全量历史,不追求100%精确。
      // "全部"/"不限"是前端内部的哨兵值,不发给后端(后端没有对应的语义)。
      const fansub = filterOverrides?.fansub ?? fansubFilter;
      const q = filterOverrides?.quality ?? quality;
      const sub = filterOverrides?.subtitle ?? subtitle;
      const fmt = filterOverrides?.format ?? format;
      const rel = filterOverrides?.release ?? releaseType;
      const params = new URLSearchParams({ keyword: kw, source: src, page: String(currentPage) });
      if (bgmId) params.set("bgm_id", String(bgmId));
      if (fansub !== "全部") params.set("fansub_name", fansub);
      if (q !== "不限") params.set("quality", q);
      if (sub !== "不限") params.set("subtitle", sub);
      if (fmt !== "不限") params.set("format", fmt);
      // 发布类型(单集/合集)现在也交给后端权威过滤(matches_criteria),与 RSS 订阅同一判定,
      // 保证"看到=下到";不再靠前端本地筛。
      if (rel !== "不限") params.set("release_type", rel);

      const res = await fetch(`${API_BASE}/resources/search?${params.toString()}`);
      const data = await res.json();
      const newResults: ResourceItem[] = data.results ?? [];
      setResults((prev) => {
        if (!append) return newResults;
        // AnimeGarden源在bgm_id存在时,服务端会把"按subject查"和"按文本查"两路结果
        // union合并(见resource_client.py::search_by_source),两路各自独立翻页、
        // 页码不同步,同一个磁力链接可能在不同的"加载更多"批次里各出现一次——
        // 单次请求内的去重覆盖不到跨页的情况,这里按magnet过滤掉已经在列表里的条目。
        const seenMagnets = new Set(prev.map((item) => item.magnet));
        return [...prev, ...newResults.filter((item) => !seenMagnets.has(item.magnet))];
      });
      setPage(currentPage);
      setHasMore(Boolean(data.has_more));
      setRssWindowCap(data.rss_window_cap ?? null);
      // 字幕组下拉框的选项来源,只在"没有按字幕组过滤"的查询(fansub==="全部")时
      // 采集/累加——一旦选中了某个具体字幕组,这次请求本身就是按它过滤过的,结果
      // 里天然只剩这一个字幕组,不能拿去覆盖下拉框选项,不然选完就没法再切回别的
      // 字幕组了。同一批"全部"查询翻页(加载更多)时是累加,换关键词/数据源发起
      // 全新的"全部"查询时才重置。
      if (fansub === "全部") {
        setFansubOptionNames((prev) => {
          const names = new Set(append ? prev : []);
          newResults.forEach((r) => names.add(r.fansub_name));
          return Array.from(names);
        });
      }
    } catch (err) {
      console.error("检索失败", err);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  };

  // 挂载时先拿设置里配置的默认数据源,再(如果是从详情页带关键词跳转过来的)
  // 用这个刚拿到的值发起自动搜索——不能先用旧的source state搜一次再等设置回来,
  // 否则从详情页跳转这次自动搜索会抢跑,用到还没来得及更新的默认值。
  useEffect(() => {
    Promise.all([
      fetch(`${API_BASE}/resources/sources`).then((r) => r.json()).catch(() => ({ sources: [] })),
      fetch(`${API_BASE}/settings`).then((r) => r.json()).catch(() => ({})),
    ]).then(([srcData, settingsData]) => {
      const enabled: SourceMeta[] = (srcData.sources ?? []).filter((s: SourceMeta & { enabled?: boolean }) => s.enabled);
      setSources(enabled);
      const ids = enabled.map((s) => s.id);
      // 默认源:设置里配的 default_source 若在启用集合里就用它,否则退回第一个启用源
      // (某个源被停用后,旧的 default_source 可能已经失效,这里兜底)。
      const fetchedSource = ids.includes(settingsData.default_source)
        ? settingsData.default_source
        : (ids[0] ?? "dmhy");
      setSource(fetchedSource);
      if (initialKeyword) handleSearch(initialKeyword, fetchedSource);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 下载页一打开、带着bgmId进来(比如从详情页跳转过来),就让后端在后台把这部番
  // 的改名规则(季度序号)提前算好——用户还在选字幕组/画质/种子的这段时间里
  // 计算已经在悄悄进行,等真正点开预览时大概率已经是热缓存,不用再等。
  // bgmId是从initialBgmId初始化后就不再变的常量(见上面的useState),这个effect
  // 依赖数组只写[bgmId]本质就是"仅挂载时触发一次",不需要每次搜索/切换都重发。
  useEffect(() => {
    if (!bgmId) return;
    fetch(`${API_BASE}/resources/prefetch-rename-cache`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        anime_title: searchBox.trim() || "未命名番剧",
        bgm_id: bgmId,
      }),
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bgmId]);

  const fansubOptions = useMemo(() => ["全部", ...fansubOptionNames], [fansubOptionNames]);

  // 过滤已经全部下沉到后端权威谓词 matches_criteria(见 server/sources/base.py),
  // 搜索接口返回的就是过滤后的结果——与 RSS 订阅轮询走同一套判定,保证"看到=下到"。
  // 前端不再本地二次过滤,直接展示后端结果(改任一筛选项时会带着新条件重新请求)。
  const filteredResults = results;

  const allChecked =
    filteredResults.length > 0 &&
    filteredResults.every((_, i) => selected.has(i));

  const toggleAll = () => {
    if (allChecked) {
      setSelected(new Set());
    } else {
      setSelected(new Set(filteredResults.map((_, i) => i)));
    }
  };

  const toggleOne = (index: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const selectedItems = filteredResults.filter((_, i) => selected.has(i));

  // 后端最终会用哪个bgm_id(跟handleSubmit里传的那个保持一致)——"合并到主系列"
  // 靠它写归属覆盖,拿不到bgm_id时这个开关没有作用对象,不渲染。
  const effectiveBgmId = bgmId ?? selectedItems[0]?.bgm_id ?? null;

  // 改名规则可能还在后台算(比如首次遇到这部番,家族解析要联网爬关联图谱)——
  // 后端命中缓存才会返回status="ready",没命中返回"pending"且顺手在后台补一次
  // 预热。这里遇到pending就隔几秒自己再问一次,直到变成ready或者依赖变化/组件
  // 卸载(用cancelled旗标+清timeout,避免旧的轮询在新一轮请求发出后还继续跑)。
  useEffect(() => {
    if (!autoRename || selectedItems.length === 0) {
      setPreviews([]);
      setPreviewStatus("ready");
      return;
    }
    const animeTitle = searchBox.trim() || "未命名番剧";
    const titles = selectedItems.map((i) => i.title);
    let cancelled = false;
    let retryTimer: number | undefined;

    const fetchPreview = () => {
      fetch(`${API_BASE}/resources/preview-rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          anime_title: animeTitle,
          bgm_id: bgmId,
          titles,
          merge_to_family: mergeToFamily,
        }),
      })
        .then((res) => res.json())
        .then((data: { status: "ready" | "pending"; previews: RenamePreview[] }) => {
          if (cancelled) return;
          setPreviewStatus(data.status);
          setPreviews(data.previews ?? []);
          if (data.status === "pending") {
            retryTimer = window.setTimeout(fetchPreview, 3000);
          }
        })
        .catch(() => {
          if (cancelled) return;
          setPreviewStatus("ready"); // 请求失败就不再重试,退回空预览而不是一直转圈
          setPreviews([]);
        });
    };

    fetchPreview();
    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
    };
    // mergeToFamily也在依赖里:它决定文件夹归属(合并进系列根 vs 独立成一部),
    // 改了必须重算预览,否则预览显示的目标路径跟提交后真正落地的对不上。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRename, selected, searchBox, bgmId, mergeToFamily]);

  const handleSubmit = async () => {
    if (selectedItems.length === 0) return;
    setSubmitting(true);
    setResultMessage(null);
    try {
      const res = await fetch(`${API_BASE}/download/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          anime_title: searchBox.trim() || "未命名番剧",
          source,
          bgm_id: bgmId ?? selectedItems[0]?.bgm_id ?? null,
          keyword: searchBox.trim(),
          fansub_name: fansubFilter !== "全部" ? fansubFilter : null,
          // 传递给后端用于生成复杂 RSS 规则的参数
          quality: quality !== "不限" ? quality : null,
          subtitle: subtitle !== "不限" ? subtitle : null,
          format: format !== "不限" ? format : null,
          release_type: releaseType !== "不限" ? releaseType : null,
          subscribe,
          auto_rename: autoRename,
          merge_to_family: mergeToFamily,
          items: selectedItems.map((item) => ({
            title: item.title,
            magnet: item.magnet,
            fansub_name: item.fansub_name,
          })),
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        // 后端按bgm_id/keyword检测到这部番已有一条生效中的订阅时返回409,
        // detail里带着具体的来源/字幕组文案——原样展示,不要走下面的通用报错。
        setResultMessage(data.detail ?? `提交失败(HTTP ${res.status})`);
        return;
      }
      setResultMessage(
        data.rss_managed
          ? "已建立RSS订阅规则,qBittorrent会自动抓取并下载匹配的种子"
          : `已提交 ${data.tasks.length} 个下载任务`,
      );
      setSelected(new Set());
    } catch (err) {
      setResultMessage("提交失败,请检查后端和qBittorrent连接");
      console.error(err);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="p-8">
      {initialBgmId !== null && onBack && (
        <button
          onClick={onBack}
          className="mb-4 flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
        >
          <ArrowLeft size={14} />
          返回详情
        </button>
      )}
      <h1 className="mb-6 font-display text-2xl tracking-tight">下载</h1>

      {/* 5. UI 重构：全部替换为规整的下拉菜单 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        {/* 数据源:强制二选一 */}
        <select
          value={source}
          onChange={(e) => {
            const next = e.target.value;
            setSource(next);
            // 换数据源等于换了一批完全不同的结果,之前基于旧结果选的筛选条件
            // 大概率对不上新数据源(比如字幕组名不一样),干脆一起重置
            setFansubFilter("全部");
            setQuality("不限");
            setSubtitle("不限");
            setFormat("不限");
            setReleaseType("不限");
            setFansubOptionNames([]);
            setResults([]);
            setSelected(new Set());
            setPreviews([]);
            setResultMessage(null);
            // 这几个setXxx要下一次渲染才会反映到闭包里的state变量,handleSearch
            // 却是马上同步调用的——必须显式把刚重置的值传进去,不然这次请求
            // 用的还是切源之前的旧筛选值(界面显示"不限"但实际按旧值查了)。
            handleSearch(undefined, next, false, {
              fansub: "全部", quality: "不限", subtitle: "不限", format: "不限", release: "不限",
            });
          }}
          className={controlClass}
        >
          {sources.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>

        {/* 字幕组:现在是真查询条件,选了之后要重新搜(不是纯本地筛),
            用overrides显式传新值,避免拿到setState还没生效的旧闭包值 */}
        <select
          value={fansubFilter}
          onChange={(e) => {
            const next = e.target.value;
            setFansubFilter(next);
            handleSearch(undefined, undefined, false, { fansub: next });
          }}
          className={controlClass}
        >
          {fansubOptions.map((name) => <option key={name} value={name}>{name === "全部" ? "所有字幕组" : name}</option>)}
        </select>

        {/* 分辨率:同样是真查询条件 */}
        <select
          value={quality}
          onChange={(e) => {
            const next = e.target.value;
            setQuality(next);
            handleSearch(undefined, undefined, false, { quality: next });
          }}
          className={controlClass}
        >
          {QUALITY_OPTIONS.map((q) => <option key={q} value={q}>{q === "不限" ? "画质: 不限" : q}</option>)}
        </select>

        {/* 字幕语言:同样是真查询条件(只取一个最有代表性的词拼进搜索关键词,
            精确的包含/排除组合还是靠下面本地过滤兜底) */}
        <select
          value={subtitle}
          onChange={(e) => {
            const next = e.target.value;
            setSubtitle(next);
            handleSearch(undefined, undefined, false, { subtitle: next });
          }}
          className={controlClass}
        >
          {SUBTITLE_OPTIONS.map((s) => <option key={s} value={s}>{s === "不限" ? "语言: 不限" : s}</option>)}
        </select>

        {/* 文件格式:同样是真查询条件 */}
        <select
          value={format}
          onChange={(e) => {
            const next = e.target.value;
            setFormat(next);
            handleSearch(undefined, undefined, false, { format: next });
          }}
          className={controlClass}
        >
          {FORMAT_OPTIONS.map((f) => <option key={f} value={f}>{f === "不限" ? "格式: 不限" : f}</option>)}
        </select>

        {/* 发布类型:单集/合集靠标题特征(有没有"合集"/"全集"/连续集数范围这类词)判断。
            站点搜索表达不了"不包含什么",所以不拼进关键词;但现在由后端权威谓词
            matches_criteria 统一判定(与 RSS 订阅同一套),所以改这个也要带条件重新请求。 */}
        <select
          value={releaseType}
          onChange={(e) => {
            const next = e.target.value;
            setReleaseType(next);
            handleSearch(undefined, undefined, false, { release: next });
          }}
          className={controlClass}
        >
          {TYPE_OPTIONS.map((t) => <option key={t} value={t}>{t === "不限" ? "类型: 不限" : t}</option>)}
        </select>
      </div>

      <div className="mb-2 flex items-center gap-2">
        <input
          value={searchBox}
          onChange={(e) => setSearchBox(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder="输入中文动漫名或简称,例如:芙莉莲"
          className="flex-1 rounded-md border border-border bg-surface px-3 py-2 text-sm text-paper outline-none placeholder:text-muted/60 focus:border-vermillion focus:ring-1 focus:ring-vermillion"
        />
        <button
          onClick={() => handleSearch()}
          disabled={loading || Boolean(currentCooldownMs && coolingDown)}
          className="rounded-md border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-vermillion"
        >
          {currentCooldownMs && coolingDown && !loading ? "请稍候..." : "检索"}
        </button>
      </div>
      {currentCooldownMs && coolingDown && (
        <p className="mb-2 font-mono text-[11px] text-vermillion">
          {source} 有防刷限流(短时间内搜太多次会被暂时拦截),已自动限制几秒钟再让你继续搜索
        </p>
      )}
      <p className="mb-4 font-mono text-[11px] text-muted">
        搜索结果和RSS订阅都来自你选择的数据源,不会自动切换——dmhy覆盖全量历史,AnimeGarden仅覆盖近期资源,
        nyaa.si是英语圈综合站点(同样仅覆盖近期资源),以英文/罗马音标题为主,中文关键词可能搜不到结果
      </p>

      {loading && (
        <div className="mb-4 font-mono text-xs text-muted">检索中...</div>
      )}

      {/* 结果列表 */}
      {filteredResults.length > 0 && (
      <div className="mb-6 max-h-[420px] overflow-y-auto rounded-md border border-border">
        <div className="sticky top-0 z-10 flex items-center gap-3 border-b border-border bg-ink px-3 py-2">
            <input
              type="checkbox"
              checked={allChecked}
              onChange={toggleAll}
              className="h-4 w-4 accent-vermillion"
            />
            <span className="font-mono text-xs text-muted">
              全选({selected.size}/{filteredResults.length})
            </span>
          </div>
          <div className="flex flex-col divide-y divide-border">
            {filteredResults.map((item, index) => (
              <label
                key={index}
                className="flex cursor-pointer items-center gap-3 px-3 py-2.5 hover:bg-surface"
              >
                <input
                  type="checkbox"
                  checked={selected.has(index)}
                  onChange={() => toggleOne(index)}
                  className="h-4 w-4 shrink-0 accent-vermillion"
                />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm">{item.title}</div>
                  <div className="mt-0.5 flex gap-3 font-mono text-[11px] text-muted">
                    <span className="text-vermillion">
                      {item.fansub_name}
                    </span>
                    <span>{formatSize(item.size)}</span>
                    <span className="uppercase">{item.source}</span>
                  </div>
                </div>
                {item.detail_url && (
                  <button
                    type="button"
                    onClick={(e) => {
                      // 整行是个<label>,点击默认会转发给里面的checkbox触发勾选——
                      // 这个按钮要单独响应,不能让选中状态跟着变。
                      e.preventDefault();
                      e.stopPropagation();
                      setLastOpenedDetail(item.detail_url);
                      openUrl(item.detail_url!);
                    }}
                    className={`flex shrink-0 items-center gap-1 rounded border px-2 py-1 font-mono text-[11px] transition-colors ${
                      lastOpenedDetail === item.detail_url
                        ? "border-vermillion bg-vermillion text-ink"
                        : "border-border text-muted hover:border-vermillion hover:text-vermillion"
                    }`}
                  >
                    <ExternalLink size={12} />
                    详情
                  </button>
                )}
              </label>
            ))}
          </div>
          {hasMore && (
            <div className="border-t border-border p-2 text-center">
              <button
                type="button"
                onClick={() => handleSearch(undefined, undefined, true)}
                disabled={loadingMore || Boolean(currentCooldownMs && coolingDown)}
                className="font-mono text-xs text-muted transition-colors hover:text-vermillion disabled:opacity-50"
              >
                {loadingMore
                  ? "加载中..."
                  : currentCooldownMs && coolingDown
                    ? "请稍候..."
                    : "加载更多"}
              </button>
            </div>
          )}
        </div>
      )}

      {!loading && hasSearched && filteredResults.length === 0 && (
        <div className="mb-6 rounded-md border border-border bg-surface p-6 text-center font-mono text-xs text-muted">
          没有搜索到对应内容,试试更换关键词或调整筛选条件
        </div>
      )}

      {/* 订阅与改名开关 */}
      {filteredResults.length > 0 && (
        <div className="mb-6 flex flex-col gap-3 rounded-md border border-border bg-surface p-4">
          <ToggleRow
            label="RSS订阅"
            description="按当前关键词/字幕组/画质持续订阅,以后新种子自动下载"
            checked={subscribe}
            onChange={setSubscribe}
          />
          {subscribe && fansubFilter === "全部" && (
            <div className="rounded border border-vermillion/40 bg-ink p-3 font-mono text-[11px] text-vermillion">
              还没选择字幕组——RSS规则会匹配所有字幕组发布的种子,同一集可能被多个字幕组重复下载,建议先在上面选定一个字幕组再开启订阅
            </div>
          )}
          {subscribe && rssWindowCap !== null && results.length > rssWindowCap && (
            <div className="rounded border border-vermillion/40 bg-ink p-3 font-mono text-[11px] text-vermillion">
              当前关键词在{sources.find((s) => s.id === source)?.label ?? source}已经翻页加载到 {results.length} 条,
              超出该数据源 RSS 只能追踪的最近 {rssWindowCap} 条——订阅只会自动下载以后新发布的资源,
              更早的历史存量不会被订阅自动补下载,需要的话请在上面的列表里手动勾选下载
            </div>
          )}
          {effectiveBgmId !== null && (
            <>
              <ToggleRow
                label="合并到主系列"
                description="是否自动合并到最早的主系列中"
                checked={mergeToFamily}
                onChange={setMergeToFamily}
              />
              {!mergeToFamily && (
                <div className="rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
                  已关闭合并——这一部会作为独立的一部入库,在媒体库单独显示一张卡片,
                  不并进所属系列的主文件夹。之后可以在媒体库详情页里改回来。
                </div>
              )}
            </>
          )}

          <ToggleRow
            label="自动改名"
            description="下载后自动整理为刮削友好的文件夹结构(优先用Bangumi官方译名)"
            checked={autoRename}
            onChange={setAutoRename}
          />

          {autoRename && selectedItems.length > 0 && previewStatus === "pending" && (
            <div className="mt-2 flex items-center gap-2 rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
              <Loader2 size={12} className="animate-spin" />
              改名规则计算中(首次识别这部番需要联网查询关联季度信息)…不等也没关系,提交下载后台会继续算
            </div>
          )}

          {autoRename && previews.length > 0 && (
            <div className="mt-2 rounded border border-border bg-ink p-3">
              <div className="mb-2 font-mono text-[11px] text-muted">
                改名预览
              </div>
              <div className="flex flex-col gap-2">
                {previews.map((p, i) => (
                  <div
                    key={i}
                    className="font-mono text-[11px] leading-relaxed"
                  >
                    <div className="text-muted line-through opacity-60">
                      {p.original_title}
                    </div>
                    <div className="text-vermillion">
                      [{p.media_type}] {p.target_full_path}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <button
            onClick={handleSubmit}
            disabled={selected.size === 0 || submitting}
            className="mt-1 self-start rounded-md border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-vermillion"
          >
            {submitting
              ? "提交中..."
              : `${subscribe ? "订阅并下载" : "单次下载"}(已选${
                  selected.size
                }项)`}
          </button>

          {resultMessage && (
            <div className="font-mono text-[11px] text-muted">
              {resultMessage}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ToggleRow({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <div className="text-sm">{label}</div>
        <div className="font-mono text-[11px] text-muted">{description}</div>
      </div>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        aria-pressed={checked}
        className={`relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 ${
          checked ? "bg-toggle-on" : "bg-border"
        }`}
      >
        <span
          className="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200"
          style={{
            transform: checked ? "translateX(20px)" : "translateX(0px)",
          }}
        />
      </button>
    </div>
  );
}
