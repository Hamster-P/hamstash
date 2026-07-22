import { useState } from "react";
import { CheckCircle2, XCircle, Loader2 } from "lucide-react";

const API_BASE = "http://127.0.0.1:8080";

type Branch = "has_webui" | "installed_no_webui" | "not_installed" | null;

interface CurrentPreferences {
  rss_processing_enabled?: boolean;
  rss_auto_downloading_enabled?: boolean;
  web_ui_host_header_validation_enabled?: boolean;
  web_ui_max_auth_fail_count?: number;
}

interface QbittorrentSetupPageProps {
  onComplete: () => void;
}

const inputClass =
  "w-full rounded border border-border bg-ink px-3 py-2 text-sm text-paper outline-none placeholder:text-muted/60 focus:border-vermillion focus:ring-1 focus:ring-vermillion";

export default function QbittorrentSetupPage({ onComplete }: QbittorrentSetupPageProps) {
  const [branch, setBranch] = useState<Branch>(null);

  const [host, setHost] = useState("127.0.0.1");
  const [port, setPort] = useState("8080");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const [testState, setTestState] = useState<"idle" | "testing" | "success" | "failed">("idle");
  const [testError, setTestError] = useState<string | null>(null);
  const [currentPrefs, setCurrentPrefs] = useState<CurrentPreferences | null>(null);
  const [applyRecommended, setApplyRecommended] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const handleTest = async () => {
    setTestState("testing");
    setTestError(null);
    setCurrentPrefs(null);
    try {
      const res = await fetch(`${API_BASE}/setup/qbittorrent/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host, port: Number(port), username, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setCurrentPrefs(data.current_preferences ?? null);
      setTestState("success");
    } catch (err: any) {
      setTestError(err.message ?? "连接失败");
      setTestState("failed");
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const res = await fetch(`${API_BASE}/setup/qbittorrent/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host,
          port: Number(port),
          username,
          password,
          apply_recommended: applyRecommended,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      onComplete();
    } catch (err: any) {
      setSaveError(err.message ?? "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const renderCredentialsForm = () => (
    <div className="mt-4 flex flex-col gap-3">
      <div className="grid grid-cols-[2fr,1fr] gap-3">
        <div>
          <div className="mb-1 font-mono text-[11px] text-muted">地址</div>
          <input value={host} onChange={(e) => setHost(e.target.value)} className={inputClass} />
        </div>
        <div>
          <div className="mb-1 font-mono text-[11px] text-muted">端口</div>
          <input value={port} onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, ""))} className={inputClass} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="mb-1 font-mono text-[11px] text-muted">用户名</div>
          <input value={username} onChange={(e) => setUsername(e.target.value)} className={inputClass} />
        </div>
        <div>
          <div className="mb-1 font-mono text-[11px] text-muted">密码</div>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
          />
        </div>
      </div>

      <button
        onClick={handleTest}
        disabled={testState === "testing"}
        className="mt-1 flex items-center gap-2 self-start rounded border border-border px-4 py-2 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
      >
        {testState === "testing" && <Loader2 size={14} className="animate-spin" />}
        {testState === "testing" ? "测试中..." : "测试连接"}
      </button>

      {testState === "failed" && (
        <div className="flex items-center gap-2 font-mono text-xs text-vermillion">
          <XCircle size={14} /> {testError}
        </div>
      )}

      {testState === "success" && (
        <div className="rounded border border-border bg-ink p-3">
          <div className="mb-2 flex items-center gap-2 font-mono text-xs text-gold">
            <CheckCircle2 size={14} /> 连接成功
          </div>
          {currentPrefs && (
            <div className="mb-2 font-mono text-[11px] text-muted">
              当前RSS处理:{currentPrefs.rss_processing_enabled ? "已开启" : "未开启"} / 自动下载:
              {currentPrefs.rss_auto_downloading_enabled ? "已开启" : "未开启"}
            </div>
          )}
          <label className="flex items-center gap-2 font-mono text-xs text-paper">
            <input
              type="checkbox"
              checked={applyRecommended}
              onChange={(e) => setApplyRecommended(e.target.checked)}
              className="accent-vermillion"
            />
            应用推荐设置(开启RSS自动下载、优化连接稳定性)
          </label>

          <button
            onClick={handleSave}
            disabled={saving}
            className="mt-3 rounded border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
          >
            {saving ? "保存中..." : "保存并继续"}
          </button>
          {saveError && (
            <div className="mt-2 font-mono text-[11px] text-vermillion">{saveError}</div>
          )}
        </div>
      )}
    </div>
  );

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-ink p-8">
      <div className="w-full max-w-xl rounded-md border border-border bg-surface p-6">
        <h1 className="mb-2 font-display text-2xl tracking-tight">连接qBittorrent</h1>
        <p className="mb-6 font-mono text-xs text-muted">
          HamStash靠qBittorrent的WebUI搜索/下载/自动追更，需要先完成这一步连接配置。
        </p>

        {branch === null && (
          <div className="flex flex-col gap-3">
            <button
              onClick={() => setBranch("has_webui")}
              className="rounded border border-border p-3 text-left transition-colors hover:border-vermillion"
            >
              <div className="text-sm">WebUI已经开着了</div>
              <div className="mt-1 font-mono text-[11px] text-muted">
                我已经在qBittorrent里开启过Web用户界面
              </div>
            </button>
            <button
              onClick={() => setBranch("installed_no_webui")}
              className="rounded border border-border p-3 text-left transition-colors hover:border-vermillion"
            >
              <div className="text-sm">装了qBittorrent，但还没开WebUI</div>
              <div className="mt-1 font-mono text-[11px] text-muted">
                需要先去qBittorrent里开一下
              </div>
            </button>
            <button
              onClick={() => setBranch("not_installed")}
              className="rounded border border-border p-3 text-left transition-colors hover:border-vermillion"
            >
              <div className="text-sm">还没装qBittorrent</div>
              <div className="mt-1 font-mono text-[11px] text-muted">需要先下载安装</div>
            </button>
          </div>
        )}

        {branch === "not_installed" && (
          <div>
            <div className="rounded border border-border bg-ink p-4">
              <p className="mb-3 font-mono text-xs text-muted">
                前往官网下载安装qBittorrent，安装完成后回到这里继续。
              </p>
              <a
                href="https://www.qbittorrent.org/download"
                target="_blank"
                rel="noreferrer"
                className="font-mono text-xs text-vermillion underline"
              >
                https://www.qbittorrent.org/download
              </a>
            </div>
            <button
              onClick={() => setBranch("installed_no_webui")}
              className="mt-4 rounded border border-vermillion px-4 py-2 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink"
            >
              安装完成，继续
            </button>
          </div>
        )}

        {branch === "installed_no_webui" && (
          <div>
            <div className="rounded border border-border bg-ink p-4 font-mono text-xs text-muted leading-relaxed">
              打开qBittorrent → 工具(Tools) → 选项(Options) → Web UI → 勾选"启用Web用户界面" →
              设置一个端口和账号密码 → 保存。然后把同样的信息填在下面：
            </div>
            {renderCredentialsForm()}
          </div>
        )}

        {branch === "has_webui" && <div>{renderCredentialsForm()}</div>}

        {branch !== null && (
          <button
            onClick={() => {
              setBranch(null);
              setTestState("idle");
              setTestError(null);
              setCurrentPrefs(null);
            }}
            className="mt-6 font-mono text-[11px] text-muted underline hover:text-paper"
          >
            返回重新选择
          </button>
        )}
      </div>
    </div>
  );
}
