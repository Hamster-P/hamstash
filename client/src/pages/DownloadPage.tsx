import { useEffect, useMemo, useState } from "react";
import { ArrowLeft } from "lucide-react";

interface ResourceItem {
  source: string;
  provider: string | null;
  title: string;
  fansub_name: string;
  magnet: string;
  size: number | null;
  created_at: string | null;
  bgm_id: number | null;
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
// 数据源:不提供"全部"/"不限",强制单选,保证搜索结果和RSS订阅内容一致
const SOURCE_OPTIONS: { value: "dmhy" | "animegarden" | "nyaa"; label: string }[] = [
  { value: "dmhy", label: "dmhy(全量历史)" },
  { value: "animegarden", label: "AnimeGarden(近期资源)" },
  { value: "nyaa", label: "nyaa.si(英语圈综合站)" },
];
// 1. 新增：细化的字幕语言与文件格式选项
const SUBTITLE_OPTIONS = ["不限", "纯简体", "繁体", "简繁", "简日", "繁日", "RAW", "日文/无字"];
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

export default function DownloadPage({
  initialKeyword,
  initialBgmId = null,
  initialSubscribe = false,
  onBack,
}: DownloadPageProps) {
  const [searchBox, setSearchBox] = useState(initialKeyword ?? "");
  const [bgmId] = useState<number | null>(initialBgmId);
  const [fansubFilter, setFansubFilter] = useState("全部");
  const [source, setSource] = useState<"dmhy" | "animegarden" | "nyaa">("dmhy");

  // 2. 状态扩展：全面控管筛选条件
  const [quality, setQuality] = useState("不限");
  const [subtitle, setSubtitle] = useState("不限");
  const [format, setFormat] = useState("不限");
  const [releaseType, setReleaseType] = useState("不限");

  const [results, setResults] = useState<ResourceItem[]>([]);
  const [loading, setLoading] = useState(false);
  // 是否已经执行过至少一次检索,用来判断"还没搜"和"搜了但真的没有结果"
  const [hasSearched, setHasSearched] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const [subscribe, setSubscribe] = useState(initialSubscribe);
  const [autoRename, setAutoRename] = useState(true);
  const [previews, setPreviews] = useState<RenamePreview[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [resultMessage, setResultMessage] = useState<string | null>(null);

  const handleSearch = async (keywordOverride?: string, sourceOverride?: typeof source) => {
    const kw = keywordOverride ?? searchBox;
    if (!kw.trim()) return;
    const src = sourceOverride ?? source;
    setLoading(true);
    setHasSearched(true);
    setSelected(new Set());
    setResultMessage(null);
    try {
      const res = await fetch(
        `${API_BASE}/resources/search?keyword=${encodeURIComponent(kw)}&source=${src}${
          bgmId ? `&bgm_id=${bgmId}` : ""
        }`,
      );
      const data = await res.json();
      setResults(data);
    } catch (err) {
      console.error("检索失败", err);
    } finally {
      setLoading(false);
    }
  };

  // 挂载时先拿设置里配置的默认数据源,再(如果是从详情页带关键词跳转过来的)
  // 用这个刚拿到的值发起自动搜索——不能先用旧的source state搜一次再等设置回来,
  // 否则从详情页跳转这次自动搜索会抢跑,用到还没来得及更新的默认值。
  useEffect(() => {
    fetch(`${API_BASE}/settings`)
      .then((res) => res.json())
      .then((data) => {
        const validSources = ["dmhy", "animegarden", "nyaa"] as const;
        const fetchedSource = validSources.includes(data.default_source)
          ? (data.default_source as typeof source)
          : "dmhy";
        setSource(fetchedSource);
        if (initialKeyword) {
          handleSearch(initialKeyword, fetchedSource);
        }
      })
      .catch(() => {
        if (initialKeyword) handleSearch(initialKeyword);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fansubOptions = useMemo(() => {
    const names = new Set(results.map((r) => r.fansub_name));
    return ["全部", ...Array.from(names)];
  }, [results]);

  // 3. 核心重构：多维度、不区分大小写的高级本地过滤
  const filteredResults = useMemo(() => {
    return results.filter((item) => {
      const titleLower = item.title.toLowerCase();

      // 字幕组过滤[cite: 3]
      if (fansubFilter !== "全部" && item.fansub_name !== fansubFilter) {
        return false;
      }
      // 分辨率过滤（修复大小写问题）
      if (quality !== "不限" && !titleLower.includes(quality.toLowerCase())) {
        return false;
      }
      // 文件格式过滤
      if (format !== "不限" && !titleLower.includes(format.toLowerCase())) {
        return false;
      }

      // 字幕语言过滤（映射常见发布命名习惯）
      if (subtitle !== "不限") {
        let matched = false;
        
        // 基础特征定义
        const hasSimplified = ["简", "chs", "gb"].some(k => titleLower.includes(k));
        const hasTraditional = ["繁", "cht", "big5"].some(k => titleLower.includes(k));
        const hasJapanese = ["日", "jp", "japanese"].some(k => titleLower.includes(k));
        const isRawKeyword = ["raw", "bilibili", "baha", "cr", "crunchyroll", "web-dl"].some(k => titleLower.includes(k));

        if (subtitle === "纯简体") {
          // 包含简体，但绝不能包含繁体、日文标志或RAW标志
          matched = hasSimplified && !hasTraditional && !titleLower.includes("简日") && !isRawKeyword;
        }
        else if (subtitle === "繁体") {
          // 包含繁体，但绝不能包含简体、日文标志或RAW标志
          matched = hasTraditional && !hasSimplified && !titleLower.includes("繁日") && !isRawKeyword;
        }
        else if (subtitle === "简繁") {
          // 显式包含简繁，或者同时撞了简体和繁体的关键词
          matched = titleLower.includes("简繁") || (hasSimplified && hasTraditional);
        }
        else if (subtitle === "简日") {
          // 显式包含简日，或者同时撞了简体和日语关键词（排除繁体）
          matched = ["简日", "chs_jp", "chs&jpg", "jp_ch"].some(k => titleLower.includes(k)) || (hasSimplified && hasJapanese && !hasTraditional);
        }
        else if (subtitle === "繁日") {
          // 显式包含繁日，或者同时撞了繁体和日语关键词（排除简体）
          matched = ["繁日", "cht_jp"].some(k => titleLower.includes(k)) || (hasTraditional && hasJapanese && !hasSimplified);
        }
        else if (subtitle === "RAW") {
          // 只要命中 RAW 发行特征
          matched = isRawKeyword;
        }
        else if (subtitle === "日文/无字") {
          // 包含日语特征，但必须干净地排除中文简体和繁体特征，且排除RAW
          matched = hasJapanese && !hasSimplified && !hasTraditional && !isRawKeyword;
        }
        
        if (!matched) return false;
      }

      // 发布类型过滤：单集还是全集合集（根据特征词以及是否含有连字符[01-12]判断）[cite: 5]
      const isBatch = ["合集", "全集", "batch", "pack", "fin"].some(k => titleLower.includes(k)) || /\d+-\d+/.test(titleLower);
      if (releaseType === "单集(追更)" && isBatch) return false;
      if (releaseType === "合集/全集(完结)" && !isBatch) return false;

      return true;
    });
  }, [results, fansubFilter, quality, subtitle, format, releaseType]);

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

  useEffect(() => {
    if (!autoRename || selectedItems.length === 0) {
      setPreviews([]);
      return;
    }
    const animeTitle = searchBox.trim() || "未命名番剧";
    fetch(`${API_BASE}/resources/preview-rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        anime_title: animeTitle,
        bgm_id: bgmId,
        titles: selectedItems.map((i) => i.title),
      }),
    })
      .then((res) => res.json())
      .then(setPreviews)
      .catch(() => setPreviews([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRename, selected, searchBox, bgmId]);

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
          items: selectedItems.map((item) => ({
            title: item.title,
            magnet: item.magnet,
            fansub_name: item.fansub_name,
          })),
        }),
      });
      const data = await res.json();
      setResultMessage(
        data.rss_managed
          ? "已建立RSS订阅规则,qBittorrent会自动抓取并下载匹配的种子"
          : `已提交 ${data.tasks.length} 个下载任务`,
      );setSelected(new Set());
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
          className="mb-4 flex items-center gap-1.5 font-mono text-xs text-muted transition-colors hover:text-paper"
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
            const next = e.target.value as "dmhy" | "animegarden" | "nyaa";
            setSource(next);
            setResults([]);
            setSelected(new Set());
            setPreviews([]);
            setResultMessage(null);
          }}
          className={controlClass}
        >
          {SOURCE_OPTIONS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>

        {/* 字幕组 */}
        <select value={fansubFilter} onChange={(e) => setFansubFilter(e.target.value)} className={controlClass}>
          {fansubOptions.map((name) => <option key={name} value={name}>{name === "全部" ? "所有字幕组" : name}</option>)}
        </select>

        {/* 分辨率 */}
        <select value={quality} onChange={(e) => setQuality(e.target.value)} className={controlClass}>
          {QUALITY_OPTIONS.map((q) => <option key={q} value={q}>{q === "不限" ? "画质: 不限" : q}</option>)}
        </select>

        {/* 字幕语言 */}
        <select value={subtitle} onChange={(e) => setSubtitle(e.target.value)} className={controlClass}>
          {SUBTITLE_OPTIONS.map((s) => <option key={s} value={s}>{s === "不限" ? "语言: 不限" : s}</option>)}
        </select>

        {/* 文件格式 */}
        <select value={format} onChange={(e) => setFormat(e.target.value)} className={controlClass}>
          {FORMAT_OPTIONS.map((f) => <option key={f} value={f}>{f === "不限" ? "格式: 不限" : f}</option>)}
        </select>

        {/* 发布类型 */}
        <select value={releaseType} onChange={(e) => setReleaseType(e.target.value)} className={controlClass}>
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
          className="rounded-md border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink"
        >
          检索
        </button>
      </div>
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
          <div className="flex flex-col divide-y divide-border px-3">
            {filteredResults.map((item, index) => (
              <label
                key={index}
                className="flex cursor-pointer items-center gap-3 py-2.5 hover:bg-surface"
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
              </label>
            ))}
          </div>
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
          <ToggleRow
            label="自动改名"
            description="下载后自动整理为刮削友好的文件夹结构(优先用Bangumi官方译名)"
            checked={autoRename}
            onChange={setAutoRename}
          />

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
