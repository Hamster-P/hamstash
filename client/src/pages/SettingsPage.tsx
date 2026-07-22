import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useState,
  type ChangeEvent,
} from "react";
import { CheckCircle2, XCircle, Loader2, Moon, Sun } from "lucide-react";
import { open, save } from "@tauri-apps/plugin-dialog";
import { writeFile } from "@tauri-apps/plugin-fs";
import { isTauri } from "@tauri-apps/api/core";
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

interface SettingsPageProps {
  onReconfigureQbittorrent?: () => void;
}

export interface SettingsPageHandle {
  isDirty: () => boolean;
  save: () => Promise<void>;
}

interface SavedSnapshot {
  downloadRoot: string;
  libraryRoot: string;
  potplayerPath: string;
  playerMode: "builtin" | "external";
  pollMinutes: number;
  defaultSource: "dmhy" | "animegarden" | "nyaa";
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
  const [defaultSource, setDefaultSource] = useState<"dmhy" | "animegarden" | "nyaa">("dmhy");
  const [pollMinutes, setPollMinutes] = useState(5);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [potplayerPathError, setPotplayerPathError] = useState<string | null>(null);
  // 上一次成功加载/保存时的快照,跟当前表单值比对得出是否有未保存的修改
  const [savedSnapshot, setSavedSnapshot] = useState<SavedSnapshot | null>(null);

  const [qbStatus, setQbStatus] = useState<"checking" | "connected" | "failed">(
    "checking",
  );

  useEffect(() => {
    fetch(`${API_BASE}/settings`)
      .then((res) => res.json())
      .then((data) => {
        const next: SavedSnapshot = {
          downloadRoot: data.download_root ?? "",
          libraryRoot: data.library_root ?? "",
          potplayerPath:
            data.potplayer_path ?? "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayer64.exe",
          playerMode: data.player_mode === "builtin" ? "builtin" : "external",
          pollMinutes: clampPollMinutes(
            Math.round((data.rename_poll_interval_seconds ?? 300) / 60),
          ),
          defaultSource: (["dmhy", "animegarden", "nyaa"] as const).includes(
            data.default_source,
          )
            ? data.default_source
            : "dmhy",
        };
        setDownloadRoot(next.downloadRoot);
        setLibraryRoot(next.libraryRoot);
        setPotplayerPath(next.potplayerPath);
        setPlayerMode(next.playerMode);
        setPollMinutes(next.pollMinutes);
        setDefaultSource(next.defaultSource);
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
      defaultSource !== savedSnapshot.defaultSource);

  const checkQbStatus = () => {
    setQbStatus("checking");
    fetch(`${API_BASE}/qbittorrent/status`)
      .then((res) => res.json())
      .then((data) => setQbStatus(data.connected ? "connected" : "failed"))
      .catch(() => setQbStatus("failed"));
  };

  const handleBrowseDirectory = async (setter: (path: string) => void) => {
    if (!(await isTauri())) return;
    const dir = await open({ directory: true });
    if (typeof dir === "string") setter(dir);
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
          default_source: defaultSource,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
        defaultSource,
      });
    } catch (err) {
      setSaveMessage("保存失败,请检查后端连接");
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
    <div className="max-w-xl p-8">
      <h1 className="mb-4 font-display text-2xl tracking-tight">设置</h1>

      <div className="mb-6 flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
        >
          {saving ? "保存中..." : "应用"}
        </button>
        {isDirty && !saving && (
          <span className="font-mono text-[11px] text-vermillion">
            有未保存的修改
          </span>
        )}
        {saveMessage && (
          <span className="font-mono text-[11px] text-muted">{saveMessage}</span>
        )}
      </div>

      <AppearanceSection />

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
            onClick={() => handleBrowseDirectory(setDownloadRoot)}
            className="shrink-0 rounded border border-border px-3 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
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
            onClick={() => handleBrowseDirectory(setLibraryRoot)}
            className="shrink-0 rounded border border-border px-3 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
          >
            浏览...
          </button>
        </div>
      </div>

      {/* 新增：播放方式选择 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">播放方式</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          内置mpv能识别连续观看多集,逐集准确标记已看;外置播放器只在点击瞬间标记已看,后续播放行为无法感知。
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

      {/* 新增：默认下载数据源 */}
      <div className="mb-6 rounded-md border border-border bg-surface p-4">
        <div className="mb-1 text-sm">默认下载数据源</div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          打开下载页时默认选中的数据源,之后仍然可以在下载页临时切换。
        </p>
        <select
          value={defaultSource}
          onChange={(e) =>
            setDefaultSource(e.target.value as "dmhy" | "animegarden" | "nyaa")
          }
          className="rounded border border-border bg-ink px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion"
        >
          <option value="dmhy">dmhy(全量历史)</option>
          <option value="animegarden">AnimeGarden(近期资源)</option>
          <option value="nyaa">nyaa.si(英语圈综合站)</option>
        </select>
      </div>

      {/* PotPlayer 路径输入框:只有外置模式才需要 */}
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
              className="shrink-0 rounded border border-border px-3 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
            >
              浏览...
            </button>
          </div>
          {potplayerPathError && (
            <p className="mt-2 font-mono text-[11px] text-vermillion">{potplayerPathError}</p>
          )}
        </div>
      )}

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
            className="mt-3 rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
          >
            重新配置qBittorrent连接
          </button>
        )}
      </div>

      <BackupSection />
    </div>
  );
});

export default SettingsPage;

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
          className={`flex items-center gap-2 rounded border px-3 py-2 font-mono text-xs transition-colors ${
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
          className={`flex items-center gap-2 rounded border px-3 py-2 font-mono text-xs transition-colors ${
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
          className="rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
        >
          {exporting ? "导出中..." : "导出备份"}
        </button>
        <label className="cursor-pointer rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion">
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