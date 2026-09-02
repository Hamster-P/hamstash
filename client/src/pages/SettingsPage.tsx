import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useState,
  type ChangeEvent,
} from "react";
import { AlertTriangle, CheckCircle2, XCircle, Loader2, Moon, Sun } from "lucide-react";
import { open, save } from "@tauri-apps/plugin-dialog";
import { writeFile } from "@tauri-apps/plugin-fs";
import { isTauri } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { useTheme } from "../theme/ThemeContext";

const API_BASE = "http://127.0.0.1:8080";
const POLL_MINUTES_MIN = 1;
const POLL_MINUTES_MAX = 1440; // 24小时,防呆用的合理上限

function clampPollMinutes(n: number): number {
  if (!Number.isFinite(n)) return POLL_MINUTES_MIN;
  return Math.min(Math.max(Math.trunc(n), POLL_MINUTES_MIN), POLL_MINUTES_MAX);
}

// PotPlayer可执行文件名的宽松校验:覆盖PotPlayer.exe/PotPlayerMini.exe/
// PotPlayerMini64.exe等常见命名变体,不做完全枚举匹配。
function looksLikePotPlayerExe(fullPath: string): boolean {
  const basename = fullPath.split(/[\\/]/).pop() ?? "";
  return /potplayer/i.test(basename) && /\.exe$/i.test(basename);
}

// 归一化路径用于比较(大小写不敏感、去掉结尾的斜杠),跟后端schemas.py::
// SettingsUpdate.validate_roots_distinct用同一套判断标准,保证前端拦截的范围
// 跟后端实际会拒绝的范围一致。
function normalizePath(path: string): string {
  return path.trim().toLowerCase().replace(/[\\/]+$/, "");
}

// 从FastAPI的错误响应里提取人话版错误信息(比如proxy_url格式校验失败时),
// 不然只会显示"保存失败,请检查后端连接"这种文不对题的提示,看不出是哪项设置填错了。
function extractValidationMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown } | undefined;
    if (first && typeof first.msg === "string") return first.msg;
  }
  return null;
}

interface SettingsPageProps {
  onReconfigureQbittorrent?: () => void;
}

export interface SettingsPageHandle {
  isDirty: () => boolean;
  save: () => Promise<void>;
}

// 取值跟 Sidebar 的 View key 一致,App.tsx 启动时直接 setView(default_home_view)
type DefaultHomeView = "tracking" | "search" | "library" | "movieLibrary";

// 设置页分类导航:"基础设置"是初次配置最常碰的几项(目录/代理/qBittorrent连接),
// 默认打开;软件更新单独置顶、不进任何分类,进页面就看得到。
type SettingsCategory = "basic" | "general" | "library" | "download" | "playback";

const SETTINGS_CATEGORIES: { id: SettingsCategory; label: string }[] = [
  { id: "basic", label: "基础设置" },
  { id: "general", label: "常规" },
  { id: "library", label: "媒体库" },
  { id: "download", label: "下载与整理" },
  { id: "playback", label: "播放" },
];

// 下载源配置(来自后端 GET /resources/sources):每个源的启用开关 + 可编辑的 URL 字段
interface SourceUrlField {
  key: string;
  label: string;
  default: string;
  value: string; // 用户覆盖值,空串=用 default
}
interface SourceConfig {
  id: string;
  label: string;
  enabled: boolean;
  urls: SourceUrlField[];
}

// 把源配置序列化成后端要存的 download_sources JSON(只放启用开关 + 非空 URL 覆盖)。
// 也用于"是否有未保存修改"的语义比较(避免默认态与空串直接字符串比对误判为 dirty)。
function serializeSources(configs: SourceConfig[]): string {
  const obj: Record<string, Record<string, unknown>> = {};
  for (const c of configs) {
    const entry: Record<string, unknown> = { enabled: c.enabled };
    for (const u of c.urls) {
      if (u.value.trim()) entry[u.key] = u.value.trim();
    }
    obj[c.id] = entry;
  }
  return JSON.stringify(obj);
}

interface SavedSnapshot {
  downloadRoot: string;
  libraryRoot: string;
  potplayerPath: string;
  playerMode: "builtin" | "external";
  pollMinutes: number;
  rssPollMinutes: number;
  defaultSource: string;
  defaultHomeView: DefaultHomeView;
  coverStrategy: string;
  unwatchedBadgeEnabled: boolean;
  proxyUrl: string;
  sourcesJson: string; // serializeSources 的结果,做 dirty 比较
}

const SettingsPage = forwardRef<SettingsPageHandle, SettingsPageProps>(function SettingsPage(
  { onReconfigureQbittorrent } = {},
  ref,
) {
  const [downloadRoot, setDownloadRoot] = useState("");
  const [libraryRoot, setLibraryRoot] = useState("");
  // 新增：PotPlayer 路径状态
  const [potplayerPath, setPotplayerPath] = useState("");
  const [playerMode, setPlayerMode] = useState<"builtin" | "external">("external");
  const [defaultSource, setDefaultSource] = useState<string>("dmhy");
  // 下载源配置(启用开关 + URL 覆盖),从 GET /resources/sources 加载
  const [sourceConfigs, setSourceConfigs] = useState<SourceConfig[]>([]);
  const [defaultHomeView, setDefaultHomeView] = useState<DefaultHomeView>("tracking");
  // 媒体库默认封面策略: latest_tv / first_season / matched
  const [coverStrategy, setCoverStrategy] = useState<string>("latest_tv");
  // 媒体库卡片"未看集数"角标开关
  const [unwatchedBadgeEnabled, setUnwatchedBadgeEnabled] = useState(true);
  const [proxyUrl, setProxyUrl] = useState("");
  const [pollMinutes, setPollMinutes] = useState(5);
  const [rssPollMinutes, setRssPollMinutes] = useState(30);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [potplayerPathError, setPotplayerPathError] = useState<string | null>(null);
  // 下载暂存目录/媒体库根目录选成同一个文件夹时的报错——跟后端schemas.py::
  // SettingsUpdate.validate_roots_distinct是同一条业务规则的前端拦截,选中的
  // 那一刻就拒绝、不写入state,不用等点"应用"才发现。
  const [dirConflictError, setDirConflictError] = useState<string | null>(null);
  // 上一次成功加载/保存时的快照,跟当前表单值比对得出是否有未保存的修改
  const [savedSnapshot, setSavedSnapshot] = useState<SavedSnapshot | null>(null);

  const [qbStatus, setQbStatus] = useState<"checking" | "connected" | "failed">(
    "checking",
  );
  const [activeCategory, setActiveCategory] = useState<SettingsCategory>("basic");

  useEffect(() => {
    Promise.all([
      fetch(`${API_BASE}/settings`).then((r) => r.json()),
      fetch(`${API_BASE}/resources/sources`).then((r) => r.json()).catch(() => ({ sources: [] })),
    ])
      .then(([data, srcData]) => {
        const configs: SourceConfig[] = (srcData.sources ?? []).map((s: any) => ({
          id: s.id,
          label: s.label,
          enabled: s.enabled !== false,
          urls: (s.urls ?? []).map((u: any) => ({
            key: u.key, label: u.label, default: u.default, value: u.value ?? "",
          })),
        }));
        setSourceConfigs(configs);
        const sourcesJson = serializeSources(configs);
        // 默认源:后端存的 default_source 若已不在(启用的)源里,退回第一个源
        const validIds = configs.map((c) => c.id);
        const next: SavedSnapshot = {
          downloadRoot: data.download_root ?? "",
          libraryRoot: data.library_root ?? "",
          potplayerPath:
            data.potplayer_path ?? "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayer64.exe",
          playerMode: data.player_mode === "builtin" ? "builtin" : "external",
          pollMinutes: clampPollMinutes(
            Math.round((data.rename_poll_interval_seconds ?? 300) / 60),
          ),
          rssPollMinutes: clampPollMinutes(
            Math.round((data.rss_poll_interval_seconds ?? 1800) / 60),
          ),
          defaultSource: validIds.includes(data.default_source)
            ? data.default_source
            : (validIds[0] ?? "dmhy"),
          defaultHomeView: (
            ["tracking", "search", "library", "movieLibrary"] as const
          ).includes(data.default_home_view)
            ? data.default_home_view
            : "tracking",
          coverStrategy: (["latest_tv", "first_season", "matched"] as const).includes(
            data.library_cover_strategy,
          )
            ? data.library_cover_strategy
            : "latest_tv",
          unwatchedBadgeEnabled: data.library_unwatched_badge_enabled !== false,
          proxyUrl: data.proxy_url ?? "",
          sourcesJson,
        };
        setDownloadRoot(next.downloadRoot);
        setLibraryRoot(next.libraryRoot);
        setPotplayerPath(next.potplayerPath);
        setPlayerMode(next.playerMode);
        setPollMinutes(next.pollMinutes);
        setRssPollMinutes(next.rssPollMinutes);
        setDefaultSource(next.defaultSource);
        setDefaultHomeView(next.defaultHomeView);
        setCoverStrategy(next.coverStrategy);
        setUnwatchedBadgeEnabled(next.unwatchedBadgeEnabled);
        setProxyUrl(next.proxyUrl);
        setSavedSnapshot(next);
      })
      .catch(() => {});

    checkQbStatus();
  }, []);

  const isDirty =
    savedSnapshot !== null &&
    (downloadRoot !== savedSnapshot.downloadRoot ||
      libraryRoot !== savedSnapshot.libraryRoot ||
      potplayerPath !== savedSnapshot.potplayerPath ||
      playerMode !== savedSnapshot.playerMode ||
      pollMinutes !== savedSnapshot.pollMinutes ||
      rssPollMinutes !== savedSnapshot.rssPollMinutes ||
      defaultSource !== savedSnapshot.defaultSource ||
      defaultHomeView !== savedSnapshot.defaultHomeView ||
      coverStrategy !== savedSnapshot.coverStrategy ||
      unwatchedBadgeEnabled !== savedSnapshot.unwatchedBadgeEnabled ||
      proxyUrl !== savedSnapshot.proxyUrl ||
      serializeSources(sourceConfigs) !== savedSnapshot.sourcesJson);

  const checkQbStatus = () => {
    setQbStatus("checking");
    fetch(`${API_BASE}/qbittorrent/status`)
      .then((res) => res.json())
      .then((data) => setQbStatus(data.connected ? "connected" : "failed"))
      .catch(() => setQbStatus("failed"));
  };

  const handleBrowseDirectory = async (which: "download" | "library") => {
    if (!(await isTauri())) return;
    const dir = await open({ directory: true });
    if (typeof dir !== "string") return;
    const other = which === "download" ? libraryRoot : downloadRoot;
    if (other && normalizePath(dir) === normalizePath(other)) {
      // 报错 + 退回原样:不写入state,选中的这次操作直接作废,原来的值保持不变。
      setDirConflictError("下载暂存目录和媒体库根目录不能设置成同一个文件夹,请重新选择");
      return;
    }
    setDirConflictError(null);
    if (which === "download") setDownloadRoot(dir);
    else setLibraryRoot(dir);
  };

  const handleBrowsePotplayer = async () => {
    if (!(await isTauri())) return;
    const file = await open({
      directory: false,
      filters: [{ name: "PotPlayer可执行文件", extensions: ["exe"] }],
    });
    if (typeof file !== "string") return;
    if (!looksLikePotPlayerExe(file)) {
      setPotplayerPathError("请选择PotPlayer的可执行文件(文件名需包含potplayer,例如PotPlayerMini64.exe)");
      return;
    }
    setPotplayerPathError(null);
    setPotplayerPath(file);
  };

  const handleSave = async () => {
    // 正常情况下handleBrowseDirectory已经在选中的那一刻拦掉了冲突,这里是兜底
    // (比如设置刚加载进来时数据本身就相同这种极端情况)——报错并把两个目录都
    // 退回上一次保存成功的值,不把冲突状态留在表单里。
    if (
      downloadRoot &&
      libraryRoot &&
      normalizePath(downloadRoot) === normalizePath(libraryRoot)
    ) {
      setDirConflictError("下载暂存目录和媒体库根目录不能设置成同一个文件夹,请重新选择");
      if (savedSnapshot) {
        setDownloadRoot(savedSnapshot.downloadRoot);
        setLibraryRoot(savedSnapshot.libraryRoot);
      }
      return;
    }
    setSaving(true);
    setSaveMessage(null);
    try {
      const res = await fetch(`${API_BASE}/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          download_root: downloadRoot,
          library_root: libraryRoot,
          potplayer_path: potplayerPath, // 新增：提交给后端的播放器路径
          player_mode: playerMode,
          rename_poll_interval_seconds: clampPollMinutes(pollMinutes) * 60,
          rss_poll_interval_seconds: clampPollMinutes(rssPollMinutes) * 60,
          default_source: defaultSource,
          default_home_view: defaultHomeView,
          library_cover_strategy: coverStrategy,
          library_unwatched_badge_enabled: unwatchedBadgeEnabled,
          proxy_url: proxyUrl.trim(),
          download_sources: serializeSources(sourceConfigs),
        }),
      });
      if (!res.ok) {
        const errorBody = await res.json().catch(() => null);
        throw new Error(extractValidationMessage(errorBody) ?? `HTTP ${res.status}`);
      }
      const data = await res.json();
      setSaveMessage(
        data.restart_required
          ? "已保存 — 媒体库目录已变更,请重启程序以完成数据库迁移"
          : "已保存",
      );
      setSavedSnapshot({
        downloadRoot,
        libraryRoot,
        potplayerPath,
        playerMode,
        pollMinutes,
        rssPollMinutes,
        defaultSource,
        defaultHomeView,
        coverStrategy,
        unwatchedBadgeEnabled,
        proxyUrl: proxyUrl.trim(),
        sourcesJson: serializeSources(sourceConfigs),
      });
    } catch (err) {
      setSaveMessage(err instanceof Error ? err.message : "保存失败,请检查后端连接");
      console.error(err);
    } finally {
      setSaving(false);
    }
  };

  useImperativeHandle(ref, () => ({
    isDirty: () => isDirty,
    save: handleSave,
  }));

  return (
    <div className="max-w-4xl p-8">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h1 className="font-display text-2xl tracking-tight">设置</h1>
        <div className="flex items-center gap-3">
          {isDirty && !saving && (
            <span className="font-mono text-[11px] text-vermillion">
              有未保存的修改
            </span>
          )}
          {saveMessage && (
            <span className="font-mono text-[11px] text-muted">{saveMessage}</span>
          )}
          <button
            onClick={handleSave}
            disabled={saving}
            className="min-w-[84px] rounded-md border border-vermillion px-4 py-2 text-center font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
          >
            {saving ? "保存中..." : "应用"}
          </button>
        </div>
      </div>

      {/* 软件更新单独置顶、不进任何分类的条件渲染:版本检查优先级高,不希望被
          "得先点进某个分类"挡住。 */}
      <UpdateSection />

      <div className="flex items-start gap-6">
        <nav className="flex w-40 shrink-0 flex-col gap-1">
          {SETTINGS_CATEGORIES.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => setActiveCategory(c.id)}
              className={`rounded-md border px-3 py-2 text-left font-mono text-xs transition-colors ${
                activeCategory === c.id
                  ? "border-vermillion text-vermillion"
                  : "border-transparent text-muted hover:border-border hover:text-paper"
              }`}
            >
              {c.label}
            </button>
          ))}
        </nav>

        <div className="min-w-0 flex-1">
      {activeCategory === "basic" && (
        <>
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">下载暂存目录</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          qBittorrent下载时的临时存放位置,按番剧分子文件夹(不分季),
          下载完成后由后台整理任务搬进媒体库,例如 D:\AnimeDownloads
        </p>
        <div className="flex gap-2">
          <input
            value={downloadRoot}
            readOnly
            placeholder="D:\AnimeDownloads"
            className="w-full rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none placeholder:text-muted/60"
          />
          <button
            type="button"
            onClick={() => handleBrowseDirectory("download")}
            className="shrink-0 rounded-md border border-border px-3 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
          >
            浏览...
          </button>
        </div>
      </div>

      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">媒体库根目录</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          最终整理归档的位置,必须是真实存在、有写入权限的路径,例如
          D:\AnimeLibrary,不要跟下载暂存目录用同一个文件夹
        </p>
        <div className="flex gap-2">
          <input
            value={libraryRoot}
            readOnly
            placeholder="D:\AnimeLibrary"
            className="w-full rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none placeholder:text-muted/60"
          />
          <button
            type="button"
            onClick={() => handleBrowseDirectory("library")}
            className="shrink-0 rounded-md border border-border px-3 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
          >
            浏览...
          </button>
        </div>
        {dirConflictError && (
          <p className="mt-2 font-mono text-[11px] text-vermillion">{dirConflictError}</p>
        )}
      </div>
        </>
      )}

      {activeCategory === "general" && (
        <>
      <AppearanceSection />

      {/* 默认首页:软件启动时默认显示的页面 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">默认首页</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          软件启动时默认显示的页面,之后仍然可以在侧边栏随时切换。
        </p>
        <select
          value={defaultHomeView}
          onChange={(e) => setDefaultHomeView(e.target.value as DefaultHomeView)}
          className="rounded border border-border bg-ink px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion"
        >
          <option value="tracking">追更页面</option>
          <option value="search">搜索页面</option>
          <option value="library">媒体库页面</option>
          <option value="movieLibrary">剧场版页面</option>
        </select>
      </div>

      <BackupSection />
        </>
      )}

      {activeCategory === "library" && (
        <>
      {/* 媒体库默认封面:未手动选图的番按此策略自动选封面 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">媒体库默认封面</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          未手动指定封面的番剧,按此策略从系列家族里自动选图(手动选过的不受影响)。无 TV 季的纯剧场版一律用匹配条目本身的图。
        </p>
        <select
          value={coverStrategy}
          onChange={(e) => setCoverStrategy(e.target.value)}
          className="rounded border border-border bg-ink px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion"
        >
          <option value="latest_tv">最新一季(TV)的封面</option>
          <option value="first_season">第一季的封面</option>
          <option value="matched">保持匹配条目本身的封面</option>
        </select>
      </div>

      {/* 媒体库卡片"未看集数"角标 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">未看集数角标</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          媒体库卡片右上角显示还有多少集没看过,不包含Other分类下的pv,op,ed等内容。
        </p>
        <label className="flex items-center gap-2 font-mono text-xs">
          <input
            type="checkbox"
            checked={unwatchedBadgeEnabled}
            onChange={(e) => setUnwatchedBadgeEnabled(e.target.checked)}
            className="accent-vermillion"
          />
          在媒体库卡片上显示角标
        </label>
      </div>

      <LibraryRepairSection />
        </>
      )}

      {activeCategory === "playback" && (
        <>
      {/* 新增：播放方式选择 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">播放方式</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          内置播放器mpv;外置播放器暂时只支持Potplayer。
        </p>
        <div className="flex gap-3">
          <label className="flex items-center gap-2 font-mono text-xs">
            <input
              type="radio"
              checked={playerMode === "builtin"}
              onChange={() => setPlayerMode("builtin")}
              className="accent-vermillion"
            />
            内置mpv
          </label>
          <label className="flex items-center gap-2 font-mono text-xs">
            <input
              type="radio"
              checked={playerMode === "external"}
              onChange={() => setPlayerMode("external")}
              className="accent-vermillion"
            />
            外置播放器
          </label>
        </div>
      </div>

      {/* PotPlayer 路径输入框:只有外置模式才需要,紧跟在播放方式后面,两者关联性强 */}
      {playerMode === "external" && (
        <div className="mb-6 rounded-md border border-border bg-surface p-4">
          <div className="mb-1 text-sm">本地播放器路径 (PotPlayer)</div>
          <p className="mb-3 font-mono text-[11px] text-muted">
            本地播放视频时调用的外置播放器,只能选择文件名包含"potplayer"的.exe可执行文件。
          </p>
          <div className="flex gap-2">
            <input
              value={potplayerPath}
              readOnly
              placeholder="C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe"
              className="w-full rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none placeholder:text-muted/60"
            />
            <button
              type="button"
              onClick={handleBrowsePotplayer}
              className="shrink-0 rounded-md border border-border px-3 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
            >
              浏览...
            </button>
          </div>
          {potplayerPathError && (
            <p className="mt-2 font-mono text-[11px] text-vermillion">{potplayerPathError}</p>
          )}
        </div>
      )}
        </>
      )}

      {activeCategory === "download" && (
        <>
      {/* 新增：默认下载数据源 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">默认下载数据源</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          打开下载页时默认选中的数据源,之后仍然可以在下载页临时切换。
        </p>
        <select
          value={defaultSource}
          onChange={(e) => setDefaultSource(e.target.value)}
          className="rounded border border-border bg-ink px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion"
        >
          {sourceConfigs.filter((s) => s.enabled).map((s) => (
            <option key={s.id} value={s.id}>{s.label}</option>
          ))}
        </select>
      </div>

      {/* 下载源管理:启用/停用某个源、修改站点地址(换域名/换镜像时用)。
          停用只是从下载页下拉隐藏并不再新建订阅,已有订阅继续轮询。 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">下载源</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          管理搜索/RSS订阅使用的数据源:可停用某个源(从下载页下拉隐藏,已有订阅仍继续轮询),
          或在站点换域名/换镜像时修改地址。留空表示使用内置默认地址。新增全新的源需要在后端代码里加适配器。
          <span className="text-vermillion"> 至少需要启用一个源。</span>
        </p>
        <div className="flex flex-col gap-3">
          {sourceConfigs.map((s) => (
            <div key={s.id} className="rounded border border-border bg-ink p-3">
              <label className="flex items-center justify-between gap-3">
                <span className="font-mono text-xs text-paper">{s.label}</span>
                <input
                  type="checkbox"
                  checked={s.enabled}
                  // 不允许取消最后一个启用的源(至少保留一个),此时把这个复选框禁用掉
                  disabled={s.enabled && sourceConfigs.filter((c) => c.enabled).length === 1}
                  onChange={(e) =>
                    setSourceConfigs((prev) =>
                      prev.map((c) => (c.id === s.id ? { ...c, enabled: e.target.checked } : c)),
                    )
                  }
                  className="h-4 w-4 accent-vermillion disabled:opacity-40"
                />
              </label>
              <div className="mt-2 flex flex-col gap-2">
                {s.urls.map((u) => (
                  <div key={u.key} className="flex flex-col gap-1">
                    <span className="font-mono text-[10px] text-muted">{u.label}</span>
                    <input
                      value={u.value}
                      placeholder={u.default}
                      onChange={(e) =>
                        setSourceConfigs((prev) =>
                          prev.map((c) =>
                            c.id === s.id
                              ? { ...c, urls: c.urls.map((x) => (x.key === u.key ? { ...x, value: e.target.value } : x)) }
                              : c,
                          ),
                        )
                      }
                      className="w-full rounded border border-border bg-surface px-2 py-1.5 font-mono text-[11px] text-paper outline-none placeholder:text-muted/50 focus:border-vermillion focus:ring-1 focus:ring-vermillion"
                    />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
        </>
      )}

      {activeCategory === "basic" && (
        <>
      {/* 新增：访问外部动漫资源站(Bangumi/dmhy/AnimeGarden/nyaa)用的代理 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">网络代理</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          只用于访问Bangumi/dmhy/AnimeGarden/nyaa等外部站点(以及详情页内嵌的Bangumi页面),
          不影响本地qBittorrent连接。填 HTTP 代理地址,例如
          http://127.0.0.1:8000(Clash等工具的本地端口)。
          留空时会自动沿用系统代理(Windows"设置-网络和Internet-代理"里配的那个);
          按进程分流(比如Proxifier)或TUN/增强模式探测不到,前者需要在这里手动填,
          后者本来就是透明转发、留空直连即可。
        </p>
        <input
          value={proxyUrl}
          onChange={(e) => setProxyUrl(e.target.value)}
          placeholder="http://127.0.0.1:8000"
          className="w-full rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none placeholder:text-muted/60 focus:border-vermillion focus:ring-1 focus:ring-vermillion"
        />
        <ProxyTestSection
          proxyUrl={proxyUrl}
          sourceIds={sourceConfigs.filter((s) => s.enabled).map((s) => s.id)}
        />
      </div>
        </>
      )}

      {activeCategory === "download" && (
        <>
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">整理任务轮询间隔</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          后台每隔这么久检查一次下载暂存目录,把已完成的种子整理进媒体库(1~{POLL_MINUTES_MAX}分钟)
        </p>
        <div className="flex items-center gap-2">
          <input
            type="number"
            inputMode="numeric"
            min={POLL_MINUTES_MIN}
            max={POLL_MINUTES_MAX}
            step={1}
            value={pollMinutes}
            onKeyDown={(e) => {
              // 拦截原生number输入框仍然放行的科学计数法/正负号/小数点按键
              if (["e", "E", "+", "-", "."].includes(e.key)) e.preventDefault();
            }}
            onChange={(e) => {
              const digitsOnly = e.target.value.replace(/[^0-9]/g, "");
              if (digitsOnly === "") {
                setPollMinutes(POLL_MINUTES_MIN);
                return;
              }
              setPollMinutes(clampPollMinutes(parseInt(digitsOnly, 10)));
            }}
            className="w-24 rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none focus:border-vermillion focus:ring-1 focus:ring-vermillion"
          />
          <span className="font-mono text-xs text-muted">分钟</span>
        </div>
      </div>

      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">RSS订阅轮询间隔</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          后台每隔这么久检查一次RSS订阅有没有匹配的新种子并自动下载(1~{POLL_MINUTES_MAX}分钟)
        </p>
        <div className="flex items-center gap-2">
          <input
            type="number"
            inputMode="numeric"
            min={POLL_MINUTES_MIN}
            max={POLL_MINUTES_MAX}
            step={1}
            value={rssPollMinutes}
            onKeyDown={(e) => {
              if (["e", "E", "+", "-", "."].includes(e.key)) e.preventDefault();
            }}
            onChange={(e) => {
              const digitsOnly = e.target.value.replace(/[^0-9]/g, "");
              if (digitsOnly === "") {
                setRssPollMinutes(POLL_MINUTES_MIN);
                return;
              }
              setRssPollMinutes(clampPollMinutes(parseInt(digitsOnly, 10)));
            }}
            className="w-24 rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none focus:border-vermillion focus:ring-1 focus:ring-vermillion"
          />
          <span className="font-mono text-xs text-muted">分钟</span>
        </div>
      </div>
        </>
      )}

      {activeCategory === "basic" && (
        <>
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">qBittorrent 连接状态</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          连接信息在首次引导向导里配置并保存，这里只做连接检测
        </p>
        <div className="flex items-center gap-2">
          {qbStatus === "checking" && (
            <>
              <Loader2 size={16} className="animate-spin text-muted" />
              <span className="font-mono text-xs text-muted">检测中...</span>
            </>
          )}
          {qbStatus === "connected" && (
            <>
              <CheckCircle2 size={16} className="text-gold" />
              <span className="font-mono text-xs text-gold">已连接</span>
            </>
          )}
          {qbStatus === "failed" && (
            <>
              <XCircle size={16} className="text-vermillion" />
              <span className="font-mono text-xs text-vermillion">
                连接失败，请重新配置连接
              </span>
            </>
          )}
          <button
            onClick={checkQbStatus}
            className="ml-auto font-mono text-[11px] text-muted underline hover:text-paper"
          >
            重新检测
          </button>
        </div>
        {onReconfigureQbittorrent && (
          <button
            onClick={onReconfigureQbittorrent}
            className="mt-3 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
          >
            重新配置qBittorrent连接
          </button>
        )}
      </div>
        </>
      )}
        </div>
      </div>
    </div>
  );
});

export default SettingsPage;

interface ProxyCheck {
  name: string;
  url: string;
  note: string;
  level: "ok" | "warn" | "fail";
  status: number | null;
  elapsed_ms: number;
  detail: string;
}

interface ProxyTestResult {
  proxy_in_use: string;
  proxy_source: "manual" | "system" | "none";
  system_proxy_detected: string;
  checks: ProxyCheck[];
}

const PROXY_SOURCE_LABEL: Record<ProxyTestResult["proxy_source"], string> = {
  manual: "设置页手填",
  system: "自动探测到的系统代理",
  none: "直连(没有手填,也没探测到系统代理)",
};

// 把结果拍平成纯文本,方便一键复制整段贴给别人看——排查网络问题基本都要贴日志,
// 让用户对着界面一条条手抄不现实。
function formatProxyReport(result: ProxyTestResult): string {
  const lines = [
    `[代理诊断] ${new Date().toLocaleString()}`,
    `实际使用: ${result.proxy_in_use || "(直连)"}  来源: ${PROXY_SOURCE_LABEL[result.proxy_source]}`,
    `探测到的系统代理: ${result.system_proxy_detected || "(无)"}`,
    "",
  ];
  for (const c of result.checks) {
    const mark = c.level === "ok" ? "OK  " : c.level === "warn" ? "WARN" : "FAIL";
    lines.push(`${mark} ${c.name} (${c.elapsed_ms}ms) ${c.detail}`);
    lines.push(`     ${c.url}`);
  }
  return lines.join("\n");
}

function ProxyTestSection({ proxyUrl, sourceIds }: { proxyUrl: string; sourceIds: string[] }) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<ProxyTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const handleTest = async () => {
    setTesting(true);
    setError(null);
    setResult(null);
    setCopied(false);
    try {
      const res = await fetch(`${API_BASE}/settings/proxy-test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // 只探当前勾选启用的下载源(能反映还没保存的改动),Bangumi 相关的固定项后端总会探
        body: JSON.stringify({ proxy_url: proxyUrl.trim(), sources: sourceIds }),
      });
      const body = await res.json();
      if (!res.ok) {
        throw new Error(extractValidationMessage(body) ?? `HTTP ${res.status}`);
      }
      setResult(body as ProxyTestResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : "测试失败,请检查后端连接");
      console.error(err);
    } finally {
      setTesting(false);
    }
  };

  const handleCopy = async () => {
    if (!result) return;
    await navigator.clipboard.writeText(formatProxyReport(result));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="mt-3">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={handleTest}
          disabled={testing}
          className="flex min-w-[104px] items-center justify-center gap-1.5 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
        >
          {testing && <Loader2 size={14} className="animate-spin" />}
          {testing ? "测试中..." : "测试代理"}
        </button>
        <span className="font-mono text-[11px] text-muted">
          测的是输入框里当前的值,不用先保存
        </span>
        {result && (
          <button
            type="button"
            onClick={handleCopy}
            className="ml-auto font-mono text-[11px] text-muted underline hover:text-paper"
          >
            {copied ? "已复制" : "复制日志"}
          </button>
        )}
      </div>

      {error && (
        <p className="mt-2 font-mono text-[11px] text-vermillion">{error}</p>
      )}

      {result && (
        <div className="mt-3 rounded border border-border bg-ink p-3">
          <div className="mb-2 border-b border-border pb-2 font-mono text-[11px] text-muted">
            <div>
              实际使用:{" "}
              <span className="text-paper">{result.proxy_in_use || "(直连)"}</span>
              {"  ·  "}
              {PROXY_SOURCE_LABEL[result.proxy_source]}
            </div>
            <div>
              探测到的系统代理:{" "}
              <span className="text-paper">
                {result.system_proxy_detected || "(无)"}
              </span>
            </div>
          </div>
          <div className="space-y-1.5">
            {result.checks.map((c) => (
              <div key={c.url} className="font-mono text-[11px] leading-snug">
                <div className="flex items-baseline gap-2">
                  {c.level === "ok" && <CheckCircle2 size={12} className="shrink-0 translate-y-0.5 text-gold" />}
                  {c.level === "warn" && <AlertTriangle size={12} className="shrink-0 translate-y-0.5 text-gold" />}
                  {c.level === "fail" && <XCircle size={12} className="shrink-0 translate-y-0.5 text-vermillion" />}
                  <span className="text-paper">{c.name}</span>
                  <span className="text-muted">{c.elapsed_ms}ms</span>
                  <span
                    className={`min-w-0 break-all ${
                      c.level === "fail" ? "text-vermillion" : "text-muted"
                    }`}
                  >
                    {c.detail}
                  </span>
                </div>
                <div className="pl-[18px] text-muted/70">{c.note}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AppearanceSection() {
  const { theme, setTheme } = useTheme();

  return (
    <div className="mb-6 rounded-md border border-border bg-surface p-4">
      <div className="mb-1 text-sm">外观</div>
      <p className="mb-3 font-mono text-[11px] text-muted">
        切换界面的浅色/深色配色,立即生效,选择会保存在本机
      </p>
      <div className="flex gap-3">
        <button
          type="button"
          onClick={() => setTheme("dark")}
          className={`flex items-center gap-2 rounded-md border px-3 py-2 font-mono text-xs transition-colors ${
            theme === "dark"
              ? "border-vermillion text-vermillion"
              : "border-border text-muted hover:border-vermillion hover:text-vermillion"
          }`}
        >
          <Moon size={14} />
          深色
        </button>
        <button
          type="button"
          onClick={() => setTheme("light")}
          className={`flex items-center gap-2 rounded-md border px-3 py-2 font-mono text-xs transition-colors ${
            theme === "light"
              ? "border-vermillion text-vermillion"
              : "border-border text-muted hover:border-vermillion hover:text-vermillion"
          }`}
        >
          <Sun size={14} />
          浅色
        </button>
      </div>
    </div>
  );
}

// 应用内更新:常驻显示当前版本(getVersion,应用内唯一能确认版本号的地方),
// 手动点"检查更新"走Tauri updater的check()->downloadAndInstall()流程。仅在Tauri
// 环境渲染(浏览器预览没有updater API)。
function UpdateSection() {
  const [currentVersion, setCurrentVersion] = useState<string | null>(null);
  const [inTauri, setInTauri] = useState<boolean | null>(null);
  // idle: 未操作 / checking: 查询中 / latest: 已是最新 / available: 有新版待安装
  // / downloading: 下载安装中 / error: 出错
  const [phase, setPhase] = useState<
    "idle" | "checking" | "latest" | "available" | "downloading" | "error"
  >("idle");
  const [newVersion, setNewVersion] = useState("");
  const [releaseNotes, setReleaseNotes] = useState("");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // "当前版本"按钮点开的版本更新历史弹窗
  const [historyOpen, setHistoryOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const tauri = await isTauri();
      if (cancelled) return;
      setInTauri(tauri);
      if (!tauri) return;
      try {
        const v = await getVersion();
        if (!cancelled) setCurrentVersion(v);
      } catch {
        // 读版本号失败不影响检查更新按钮可用,静默忽略。
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // GitHub 端点/安装包国内默认访问不了,复用设置页里那个"网络代理"(proxy_url,本来
  // 给后端访问Bangumi/dmhy用)——GitHub同属外部站点。检查时现读,拿到用户最新填的值;
  // 后端没起/读失败就静默降级为不带代理(走直连,TUN/全局模式下本就透明)。
  const getProxyOptions = async (): Promise<{ proxy?: string } | undefined> => {
    try {
      const res = await fetch(`${API_BASE}/settings`);
      if (!res.ok) return undefined;
      const data = await res.json();
      const proxy = (data?.proxy_url ?? "").trim();
      return proxy ? { proxy } : undefined;
    } catch {
      return undefined;
    }
  };

  // 跳级升级(比如落后好几个版本)时,updater自带的update.body只含最新一个版本的说明。
  // 这里向后端要"当前版本到目标版本之间"的完整区间,拼成多版本更新说明;拿不到/为空
  // 时fallback回单版本的fallbackBody,保证"检查更新"本身不因为这条增强逻辑而失败。
  const buildReleaseNotes = async (
    current: string | null,
    target: string,
    fallbackBody: string,
  ): Promise<string> => {
    if (!current) return fallbackBody;
    try {
      const res = await fetch(
        `${API_BASE}/update/changelog?current=${encodeURIComponent(current)}&target=${encodeURIComponent(target)}`,
      );
      if (!res.ok) return fallbackBody;
      const data = await res.json();
      const versions = (data?.versions ?? []) as { version: string; date: string | null; body: string }[];
      if (versions.length === 0) return fallbackBody;
      return versions
        .map((v) => `v${v.version}${v.date ? ` (${v.date})` : ""}\n${v.body}`)
        .join("\n\n");
    } catch {
      return fallbackBody;
    }
  };

  const handleCheck = async () => {
    setPhase("checking");
    setError(null);
    try {
      // check()传入的proxy会一并作用于后续downloadAndInstall的下载(见CheckOptions.proxy)。
      const update = await check(await getProxyOptions());
      if (!update) {
        setPhase("latest");
        return;
      }
      setNewVersion(update.version);
      setReleaseNotes(await buildReleaseNotes(currentVersion, update.version, update.body ?? ""));
      setPhase("available");
    } catch (err) {
      setError(
        `检查更新失败:${
          err instanceof Error ? err.message : "无法连接更新服务器,请检查网络"
        }`,
      );
      setPhase("error");
      console.error(err);
    }
  };

  const handleDownloadAndInstall = async () => {
    setPhase("downloading");
    setProgress(0);
    setError(null);
    try {
      const update = await check(await getProxyOptions());
      if (!update) {
        // 罕见:刚才还有,现在没了(比如中途撤了Release),当作已是最新。
        setPhase("latest");
        return;
      }
      let downloaded = 0;
      let contentLength = 0;
      await update.downloadAndInstall((event) => {
        switch (event.event) {
          case "Started":
            contentLength = event.data.contentLength ?? 0;
            break;
          case "Progress":
            downloaded += event.data.chunkLength;
            if (contentLength > 0) {
              setProgress(Math.round((downloaded / contentLength) * 100));
            }
            break;
          case "Finished":
            setProgress(100);
            break;
        }
      });
      // Windows下NSIS安装器会接管并重启应用;relaunch作为兜底/跨平台一致性调用。
      await relaunch();
    } catch (err) {
      setError(
        `下载安装失败:${
          err instanceof Error ? err.message : "请检查网络后重试"
        }`,
      );
      setPhase("error");
      console.error(err);
    }
  };

  // 非Tauri环境(浏览器预览)不渲染此区块。
  if (inTauri === false) return null;

  const busy = phase === "checking" || phase === "downloading";

  return (
    <div className="mb-6 rounded-md border border-border bg-surface p-4">
      <div className="mb-1 text-sm">软件更新</div>
      <p className="mb-3 font-mono text-[11px] text-muted">
        手动检查是否有新版本,有则下载并安装(安装过程需要管理员授权,完成后自动重启)。
      </p>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={phase === "available" ? handleDownloadAndInstall : handleCheck}
          disabled={busy}
          className="flex min-w-[104px] items-center justify-center gap-1.5 rounded-md border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
        >
          {busy && <Loader2 size={14} className="animate-spin" />}
          {phase === "checking"
            ? "检查中..."
            : phase === "downloading"
              ? `下载中 ${progress}%`
              : phase === "available"
                ? "下载并安装"
                : "检查更新"}
        </button>
        <button
          type="button"
          onClick={() => setHistoryOpen(true)}
          disabled={!currentVersion}
          className="font-mono text-[11px] text-muted underline transition-colors hover:text-paper disabled:no-underline disabled:opacity-60"
        >
          {currentVersion ? `当前版本 v${currentVersion}` : "当前版本 —"}
        </button>
      </div>

      {historyOpen && <VersionHistoryModal onClose={() => setHistoryOpen(false)} />}

      {phase === "latest" && (
        <p className="mt-3 font-mono text-[11px] text-gold">
          已是最新版本{currentVersion ? ` (v${currentVersion})` : ""}
        </p>
      )}

      {phase === "available" && (
        <div className="mt-3 rounded border border-border bg-ink p-3">
          <div className="font-mono text-[11px] text-paper">
            发现新版本 v{newVersion}
          </div>
          {releaseNotes && (
            <p className="mt-1.5 max-h-56 overflow-y-auto whitespace-pre-wrap font-mono text-[11px] leading-snug text-muted">
              {releaseNotes}
            </p>
          )}
        </div>
      )}

      {phase === "error" && error && (
        <p className="mt-3 font-mono text-[11px] text-vermillion">{error}</p>
      )}
    </div>
  );
}

// 版本更新历史弹窗:点"当前版本"按钮打开,拉 GET /update/changelog/all
// (从 GitHub 现读 CHANGELOG.md),按版本倒序展示全部正式版本的更新说明。
function VersionHistoryModal({ onClose }: { onClose: () => void }) {
  // loading: 加载中 / ok: 拿到列表 / empty: 网络或代理不通、拉不到
  const [state, setState] = useState<"loading" | "ok" | "empty">("loading");
  const [versions, setVersions] = useState<
    { version: string; date: string | null; body: string }[]
  >([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/update/changelog/all`);
        const data = await res.json();
        const list = (data?.versions ?? []) as typeof versions;
        if (cancelled) return;
        setVersions(list);
        setState(list.length > 0 ? "ok" : "empty");
      } catch {
        if (!cancelled) setState("empty");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Esc 关闭,跟点遮罩等价
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/60 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-md border border-border bg-surface"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="text-sm">版本更新历史</div>
          <button
            type="button"
            onClick={onClose}
            className="font-mono text-xs text-muted transition-colors hover:text-paper"
          >
            关闭
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {state === "loading" && (
            <div className="flex items-center gap-2 font-mono text-[11px] text-muted">
              <Loader2 size={14} className="animate-spin" />
              加载中...
            </div>
          )}
          {state === "empty" && (
            <p className="font-mono text-[11px] text-muted">
              暂时拉不到更新日志,请检查网络或代理后重新打开。
            </p>
          )}
          {state === "ok" &&
            versions.map((v) => (
              <div key={v.version} className="mb-4 last:mb-0">
                <div className="mb-1 font-mono text-xs text-paper">
                  v{v.version}
                  {v.date && <span className="ml-2 text-muted">{v.date}</span>}
                </div>
                <p className="whitespace-pre-wrap font-mono text-[11px] leading-snug text-muted">
                  {v.body}
                </p>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}

function BackupSection() {
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const handleExport = async () => {
    setExporting(true);
    setMessage(null);
    try {
      const res = await fetch(`${API_BASE}/backup/export`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const disposition = res.headers.get("Content-Disposition") ?? "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match?.[1] ?? "hamstash_backup.db";

      if (await isTauri()) {
        // 让用户自己选保存位置,不默认扔进下载目录。
        const targetPath = await save({ defaultPath: filename });
        if (!targetPath) {
          setMessage(null); // 用户取消了保存对话框,不算失败
          return;
        }
        const bytes = new Uint8Array(await res.arrayBuffer());
        await writeFile(targetPath, bytes);
        setMessage(`已导出到 ${targetPath}`);
      } else {
        // 非Tauri环境(浏览器预览)没有原生保存对话框,退回浏览器默认下载。
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
        setMessage("导出成功");
      }
    } catch (err) {
      setMessage("导出失败，请检查后端连接");
      console.error(err);
    } finally {
      setExporting(false);
    }
  };

  const handleImport = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // 允许连续两次选同一个文件也能触发onChange
    if (!file) return;

    setImporting(true);
    setMessage(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch(`${API_BASE}/backup/import`, {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setMessage(data.message ?? "导入成功，请重启应用使其生效");
    } catch (err: any) {
      setMessage(`导入失败: ${err.message ?? "未知错误"}`);
      console.error(err);
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="rounded-md border border-border bg-surface p-4">
      <div className="mb-1 text-sm">备份与迁移</div>
      <p className="mb-3 font-mono text-[11px] text-muted">
        导出包含观看记录、RSS订阅、各项设置在内的完整备份；换机时在新环境导入即可，无需重新配置。
      </p>
      <div className="flex items-center gap-3">
        <button
          onClick={handleExport}
          disabled={exporting}
          className="min-w-[88px] rounded-md border border-border px-3 py-1.5 text-center font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
        >
          {exporting ? "导出中..." : "导出备份"}
        </button>
        <label className="min-w-[88px] cursor-pointer rounded-md border border-border px-3 py-1.5 text-center font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion">
          {importing ? "导入中..." : "导入备份"}
          <input
            type="file"
            accept=".db"
            onChange={handleImport}
            disabled={importing}
            className="hidden"
          />
        </label>
      </div>
      {message && <p className="mt-3 font-mono text-[11px] text-muted">{message}</p>}
    </div>
  );
}

interface RenameMismatch {
  folder_name: string;
  current_relative_path: string;
  proposed_relative_path: string;
  blocked: boolean;
  block_reason: string | null;
}

interface FamilyMerge {
  folder_name: string;
  current_relative_path: string;
  proposed_relative_path: string;
  blocked: boolean;
  block_reason: string | null;
  source_folder: string;
  target_folder: string;
}

interface OrphanedRenamedFile {
  id: number;
  torrent_hash: string;
  target_relative_path: string;
}

interface OrphanedAnimeFolder {
  id: number;
  staging_folder: string;
}

interface RepairScanReport {
  rename_mismatches: RenameMismatch[];
  family_merges: FamilyMerge[];
  orphaned_renamed_files: OrphanedRenamedFile[];
  orphaned_anime_folders: OrphanedAnimeFolder[];
}

interface RepairApplyResult {
  renames?: {
    succeeded: { from: string; to: string }[];
    skipped: { path: string; reason: string | null }[];
    failed: { path: string; error: string }[];
  };
  family_merges?: {
    succeeded: { from: string; to: string }[];
    skipped: { path: string; reason: string | null }[];
    failed: { path: string; error: string }[];
    removed_folders: string[];
  };
  local_media?: { added: string[]; current_total: number };
  orphans?: { removed_renamed_files: number; removed_anime_folders: number };
}

function LibraryRepairSection() {
  const [scanning, setScanning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [report, setReport] = useState<RepairScanReport | null>(null);
  const [applyResult, setApplyResult] = useState<RepairApplyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 改名建议里未被阻塞的条目,默认全选;用户可以取消勾选个别项
  const [selectedRenames, setSelectedRenames] = useState<Set<string>>(new Set());
  const [selectedFamilyMerges, setSelectedFamilyMerges] = useState<Set<string>>(new Set());
  const [cleanLocalMedia, setCleanLocalMedia] = useState(true);
  const [cleanRenamedFiles, setCleanRenamedFiles] = useState(true);
  const [cleanAnimeFolders, setCleanAnimeFolders] = useState(true);

  // 季度编号规则升级后的一次性提示(后端在真的重算过家族缓存时才返回)
  const [repairNotice, setRepairNotice] = useState("");
  useEffect(() => {
    fetch(`${API_BASE}/library/repair/notice`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => setRepairNotice(data?.notice ?? ""))
      .catch(() => {
        /* 后端没起就不提示,不打扰 */
      });
  }, []);

  const dismissRepairNotice = async () => {
    setRepairNotice("");
    try {
      await fetch(`${API_BASE}/library/repair/notice/dismiss`, { method: "POST" });
    } catch {
      /* 清不掉没关系,下次进页面再提示一遍,不会丢信息 */
    }
  };

  const handleScan = async () => {
    setScanning(true);
    setError(null);
    setApplyResult(null);
    setRepairNotice("");  // 扫了就是在做我们建议的事,横幅不用再挂着
    try {
      const res = await fetch(`${API_BASE}/library/repair/scan`);
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(extractValidationMessage(body) ?? `HTTP ${res.status}`);
      }
      const data = (await res.json()) as RepairScanReport;
      setReport(data);
      setSelectedRenames(
        new Set(
          data.rename_mismatches
            .filter((m) => !m.blocked)
            .map((m) => m.current_relative_path),
        ),
      );
      setSelectedFamilyMerges(
        new Set(
          data.family_merges
            .filter((m) => !m.blocked)
            .map((m) => m.current_relative_path),
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "扫描失败,请检查后端连接");
      console.error(err);
    } finally {
      setScanning(false);
    }
  };

  const toggleRename = (path: string) => {
    setSelectedRenames((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const toggleFamilyMerge = (path: string) => {
    setSelectedFamilyMerges((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const renameItems = report?.rename_mismatches ?? [];
  const familyMergeItems = report?.family_merges ?? [];
  const hasOrphans =
    (report?.orphaned_renamed_files.length ?? 0) > 0 ||
    (report?.orphaned_anime_folders.length ?? 0) > 0;
  const canApply =
    report !== null &&
    (selectedRenames.size > 0 ||
      selectedFamilyMerges.size > 0 ||
      cleanLocalMedia ||
      cleanRenamedFiles ||
      cleanAnimeFolders);

  const handleApply = async () => {
    if (!report) return;
    setApplying(true);
    setError(null);
    setApplyResult(null);
    try {
      const res = await fetch(`${API_BASE}/library/repair/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fix_renames: selectedRenames.size > 0,
          rename_paths: Array.from(selectedRenames),
          fix_family_merges: selectedFamilyMerges.size > 0,
          family_merge_paths: Array.from(selectedFamilyMerges),
          clean_local_media: cleanLocalMedia,
          clean_renamed_files: cleanRenamedFiles,
          clean_anime_folders: cleanAnimeFolders,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(extractValidationMessage(body) ?? `HTTP ${res.status}`);
      }
      const data = (await res.json()) as RepairApplyResult;
      setApplyResult(data);
      setReport(null); // 应用完成后报告已经过期,强制用户重新扫描才能看到最新状态
    } catch (err) {
      setError(err instanceof Error ? err.message : "应用修复失败,请检查后端连接");
      console.error(err);
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="mb-6 rounded-md border border-border bg-surface p-4">
      <div className="mb-1 text-sm">修复媒体库</div>
      <p className="mb-3 font-mono text-[11px] text-muted">
        按当前改名规则重新核对媒体库里每个文件的命名/路径,并清理指向已不存在文件的数据库记录。
        涉及文件改名/移动和数据删除,请先扫描确认再应用。
      </p>

      {/* 季度编号规则升级后的一次性提示:缓存重算只让"以后的下载"用新编号,
          已经落地的文件不会自己重排,得靠用户跑一次修复。用 gold 而不是红色,
          这是建议不是故障。 */}
      {repairNotice && (
        <div className="mb-3 flex items-start justify-between gap-3 rounded-md border border-gold/40 bg-ink p-3 font-mono text-[11px] text-gold">
          <span>{repairNotice}</span>
          <button
            type="button"
            onClick={dismissRepairNotice}
            className="shrink-0 text-muted transition-colors hover:text-paper"
          >
            知道了
          </button>
        </div>
      )}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={handleScan}
          disabled={scanning || applying}
          className="min-w-[88px] rounded-md border border-border px-3 py-1.5 text-center font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
        >
          {scanning ? (
            <span className="flex items-center justify-center gap-1.5">
              <Loader2 size={14} className="animate-spin" />
              扫描中...
            </span>
          ) : (
            "扫描"
          )}
        </button>
        <button
          type="button"
          onClick={handleApply}
          disabled={!canApply || applying || scanning}
          className="min-w-[88px] rounded-md border border-vermillion px-3 py-1.5 text-center font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
        >
          {applying ? "应用中..." : "应用修复"}
        </button>
      </div>

      {error && <p className="mt-3 font-mono text-[11px] text-vermillion">{error}</p>}

      {report && (
        <div className="mt-3 space-y-3 rounded border border-border bg-ink p-3">
          {renameItems.length === 0 && familyMergeItems.length === 0 && !hasOrphans && (
            <p className="font-mono text-[11px] text-muted">没有发现需要修复的问题</p>
          )}

          {familyMergeItems.length > 0 && (
            <div>
              <div className="mb-1.5 font-mono text-[11px] text-paper">
                重复文件夹合并({familyMergeItems.length})
              </div>
              <p className="mb-1.5 font-mono text-[11px] text-muted">
                以下文件所在的文件夹跟另一个文件夹属于同一部番,建议合并到主文件夹;
                合并后如果原文件夹被搬空,会自动删除该文件夹。
              </p>
              <div className="max-h-56 space-y-1 overflow-y-auto">
                {familyMergeItems.map((m) => (
                  <label
                    key={m.current_relative_path}
                    className={`flex items-start gap-2 font-mono text-[11px] leading-snug ${
                      m.blocked ? "text-muted/60" : "text-muted"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5 accent-vermillion"
                      disabled={m.blocked}
                      checked={selectedFamilyMerges.has(m.current_relative_path)}
                      onChange={() => toggleFamilyMerge(m.current_relative_path)}
                    />
                    <span className="min-w-0 break-all">
                      <span className="text-paper">
                        {m.source_folder} → {m.target_folder}
                      </span>
                      <br />
                      {m.current_relative_path}
                      <br />→ {m.proposed_relative_path}
                      {m.blocked && (
                        <>
                          <br />
                          <span className="text-vermillion">未处理: {m.block_reason}</span>
                        </>
                      )}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {renameItems.length > 0 && (
            <div>
              <div className="mb-1.5 font-mono text-[11px] text-paper">
                命名/路径不一致({renameItems.length})
              </div>
              <div className="max-h-56 space-y-1 overflow-y-auto">
                {renameItems.map((m) => (
                  <label
                    key={m.current_relative_path}
                    className={`flex items-start gap-2 font-mono text-[11px] leading-snug ${
                      m.blocked ? "text-muted/60" : "text-muted"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5 accent-vermillion"
                      disabled={m.blocked}
                      checked={selectedRenames.has(m.current_relative_path)}
                      onChange={() => toggleRename(m.current_relative_path)}
                    />
                    <span className="min-w-0 break-all">
                      <span className="text-paper">{m.folder_name}</span>
                      <br />
                      {m.current_relative_path}
                      <br />→ {m.proposed_relative_path}
                      {m.blocked && (
                        <>
                          <br />
                          <span className="text-vermillion">未处理: {m.block_reason}</span>
                        </>
                      )}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {hasOrphans && (
            <div className="space-y-1.5 border-t border-border pt-2">
              <label className="flex items-center gap-2 font-mono text-[11px] text-muted">
                <input
                  type="checkbox"
                  className="accent-vermillion"
                  checked={cleanLocalMedia}
                  onChange={(e) => setCleanLocalMedia(e.target.checked)}
                />
                同步清理已不存在的媒体库文件夹记录(等同于"影视库"页的扫描)
              </label>
              {report.orphaned_renamed_files.length > 0 && (
                <label className="flex items-center gap-2 font-mono text-[11px] text-muted">
                  <input
                    type="checkbox"
                    className="accent-vermillion"
                    checked={cleanRenamedFiles}
                    onChange={(e) => setCleanRenamedFiles(e.target.checked)}
                  />
                  清理指向已不存在文件的整理记录({report.orphaned_renamed_files.length})
                </label>
              )}
              {report.orphaned_anime_folders.length > 0 && (
                <label className="flex items-center gap-2 font-mono text-[11px] text-muted">
                  <input
                    type="checkbox"
                    className="accent-vermillion"
                    checked={cleanAnimeFolders}
                    onChange={(e) => setCleanAnimeFolders(e.target.checked)}
                  />
                  清理指向已不存在暂存目录的记录({report.orphaned_anime_folders.length})
                </label>
              )}
            </div>
          )}
        </div>
      )}

      {applyResult && (
        <div className="mt-3 rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
          {applyResult.renames && (
            <div>
              改名: 成功{applyResult.renames.succeeded.length} / 跳过
              {applyResult.renames.skipped.length} / 失败{applyResult.renames.failed.length}
              {applyResult.renames.failed.map((f) => (
                <div key={f.path} className="mt-1 break-all text-vermillion">
                  {f.path}: {f.error}
                </div>
              ))}
            </div>
          )}
          {applyResult.family_merges && (
            <div>
              合并: 成功{applyResult.family_merges.succeeded.length} / 跳过
              {applyResult.family_merges.skipped.length} / 失败
              {applyResult.family_merges.failed.length} / 已删除空文件夹
              {applyResult.family_merges.removed_folders.length}
              {applyResult.family_merges.failed.map((f) => (
                <div key={f.path} className="mt-1 break-all text-vermillion">
                  {f.path}: {f.error}
                </div>
              ))}
            </div>
          )}
          {applyResult.orphans && (
            <div>
              清理: 整理记录 {applyResult.orphans.removed_renamed_files} 条 / 暂存记录{" "}
              {applyResult.orphans.removed_anime_folders} 条
            </div>
          )}
          {applyResult.local_media && (
            <div>媒体库文件夹: 当前共 {applyResult.local_media.current_total} 个</div>
          )}
        </div>
      )}
    </div>
  );
}