import { useEffect, useMemo, useRef, useState } from "react";

interface DownloadTask {
  hash: string;
  name: string;
  progress: number; // 0-100
  status_label: "下载中" | "已完成" | "已整理" | "出错";
  size: number;
  dlspeed: number;
  added_on: string | null;
}

const API_BASE = "http://127.0.0.1:8080";
const POLL_INTERVAL_MS = 5000;
const STATUS_OPTIONS = ["全部", "下载中", "已完成", "已整理", "出错"] as const;

function formatSize(bytes: number) {
  if (!bytes) return "—";
  const gb = bytes / 1024 / 1024 / 1024;
  return gb >= 1
    ? `${gb.toFixed(2)} GB`
    : `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

function formatSpeed(bytesPerSec: number) {
  if (!bytesPerSec) return "";
  return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`;
}

function formatDate(iso: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

export default function DownloadManagerPage() {
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<(typeof STATUS_OPTIONS)[number]>("全部");
  const [keyword, setKeyword] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pendingDeleteHash, setPendingDeleteHash] = useState<string | null>(null);
  const [pendingBatchDelete, setPendingBatchDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  const loadTasks = async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch(`${API_BASE}/downloads/tasks`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: DownloadTask[] = await res.json();
      setTasks(data);
      // 已经删除/不存在的种子,从选中集合里一并清掉
      setSelected((prev) => {
        const validHashes = new Set(data.map((t) => t.hash));
        const next = new Set<string>();
        prev.forEach((h) => {
          if (validHashes.has(h)) next.add(h);
        });
        return next;
      });
    } catch (err) {
      console.error("加载下载任务失败", err);
      setLoadError("加载失败,请检查后端/qBittorrent是否已启动");
    } finally {
      if (showLoading) setLoading(false);
    }
  };

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    loadTasks();
    pollTimer.current = setInterval(() => loadTasks(false), POLL_INTERVAL_MS);
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      if (statusFilter !== "全部" && task.status_label !== statusFilter) return false;
      if (keyword.trim() && !task.name.toLowerCase().includes(keyword.trim().toLowerCase())) {
        return false;
      }
      return true;
    });
  }, [tasks, statusFilter, keyword]);

  const allChecked =
    filteredTasks.length > 0 && filteredTasks.every((t) => selected.has(t.hash));

  const toggleAll = () => {
    if (allChecked) {
      setSelected(new Set());
    } else {
      setSelected(new Set(filteredTasks.map((t) => t.hash)));
    }
  };

  const toggleOne = (hash: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(hash)) next.delete(hash);
      else next.add(hash);
      return next;
    });
  };

  const handleDeleteOne = async (hash: string) => {
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/downloads/tasks/${hash}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadTasks(false);
    } catch (err) {
      console.error("删除下载任务失败", err);
    } finally {
      setBusy(false);
      setPendingDeleteHash(null);
    }
  };

  const handleBatchDelete = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/downloads/tasks/batch-delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hashes: Array.from(selected) }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSelected(new Set());
      await loadTasks(false);
    } catch (err) {
      console.error("批量删除下载任务失败", err);
    } finally {
      setBusy(false);
      setPendingBatchDelete(false);
    }
  };

  return (
    <div className="p-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl tracking-tight">下载详情</h1>
          <p className="mt-1 font-mono text-[11px] text-muted">
            实时反映qBittorrent里的下载任务;已整理进媒体库的种子删除时只删种子、不动文件
          </p>
        </div>
        <button
          onClick={() => loadTasks()}
          disabled={loading}
          className="rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
        >
          {loading ? "刷新中..." : "刷新"}
        </button>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as (typeof STATUS_OPTIONS)[number])}
          className="rounded border border-border bg-surface px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion focus:ring-1 focus:ring-vermillion"
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s === "全部" ? "状态: 全部" : s}
            </option>
          ))}
        </select>
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="按种子名搜索"
          className="rounded border border-border bg-surface px-3 py-1.5 font-mono text-xs text-paper outline-none placeholder:text-muted/60 focus:border-vermillion focus:ring-1 focus:ring-vermillion"
        />

        {selected.size > 0 && (
          <div className="ml-auto flex items-center gap-2 font-mono text-[11px]">
            <span className="text-muted">已选{selected.size}项</span>
            {pendingBatchDelete ? (
              <>
                <button
                  disabled={busy}
                  onClick={handleBatchDelete}
                  className="rounded border border-vermillion px-2 py-0.5 text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                >
                  确认删除{selected.size}项
                </button>
                <button
                  onClick={() => setPendingBatchDelete(false)}
                  className="rounded border border-border px-2 py-0.5 text-muted transition-colors hover:text-paper"
                >
                  取消
                </button>
              </>
            ) : (
              <button
                onClick={() => setPendingBatchDelete(true)}
                className="rounded border border-border px-2 py-0.5 text-muted transition-colors hover:border-vermillion hover:text-vermillion"
              >
                批量删除
              </button>
            )}
          </div>
        )}
      </div>

      {loadError && (
        <div className="mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
          {loadError}
        </div>
      )}

      {!loading && !loadError && filteredTasks.length === 0 && (
        <div className="rounded-md border border-border bg-surface p-6 text-center font-mono text-xs text-muted">
          {tasks.length === 0 ? "还没有任何下载任务。" : "没有符合筛选条件的任务。"}
        </div>
      )}

      {filteredTasks.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          {/* 内层套一层横向滚动,窄窗口下表格不会撑破页面布局 */}
          <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-surface font-mono text-[11px] uppercase tracking-wide text-muted">
              <tr>
                <th className="w-10 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={allChecked}
                    onChange={toggleAll}
                    className="h-4 w-4 accent-vermillion"
                  />
                </th>
                <th className="w-10 px-3 py-2">#</th>
                <th className="px-3 py-2">种子名</th>
                <th className="px-3 py-2">进度</th>
                <th className="px-3 py-2">状态</th>
                <th className="px-3 py-2">大小</th>
                <th className="px-3 py-2">添加时间</th>
                <th className="px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filteredTasks.map((task, index) => (
                <tr key={task.hash} className="transition-colors hover:bg-surface-hover">
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(task.hash)}
                      onChange={() => toggleOne(task.hash)}
                      className="h-4 w-4 accent-vermillion"
                    />
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted">{index + 1}</td>
                  <td className="max-w-[360px] truncate px-3 py-2" title={task.name}>
                    {task.name}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-border">
                        <div
                          className="h-full bg-vermillion"
                          style={{ width: `${task.progress}%` }}
                        />
                      </div>
                      <span className="font-mono text-[11px] text-muted">
                        {task.progress}%{task.dlspeed > 0 && ` · ${formatSpeed(task.dlspeed)}`}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px]">
                    {task.status_label === "出错" ? (
                      <span className="text-vermillion">出错</span>
                    ) : task.status_label === "已整理" ? (
                      <span className="text-gold">已整理</span>
                    ) : task.status_label === "已完成" ? (
                      <span className="text-paper">已完成</span>
                    ) : (
                      <span className="text-muted">下载中</span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted">
                    {formatSize(task.size)}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-muted">
                    {formatDate(task.added_on)}
                  </td>
                  <td className="px-3 py-2">
                    {pendingDeleteHash === task.hash ? (
                      <div className="flex items-center gap-2 font-mono text-[11px]">
                        <button
                          disabled={busy}
                          onClick={() => handleDeleteOne(task.hash)}
                          className="rounded border border-vermillion px-2 py-0.5 text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                        >
                          删除
                        </button>
                        <button
                          onClick={() => setPendingDeleteHash(null)}
                          className="rounded border border-border px-2 py-0.5 text-muted transition-colors hover:text-paper"
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      // 跟"删除+取消"两按钮状态占同样的最小宽度,点删除时这一列/整个表格
                      // 不会跟着变宽、把左边内容一起挤动
                      <div className="flex min-w-[88px] items-center font-mono text-[11px]">
                        <button
                          onClick={() => setPendingDeleteHash(task.hash)}
                          className="text-muted transition-colors hover:text-vermillion"
                        >
                          删除
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}
    </div>
  );
}
