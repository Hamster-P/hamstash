import { Fragment, useEffect, useMemo, useRef, useState } from "react";

interface DownloadTask {
  hash: string;
  name: string;
  progress: number; // 0-100
  status_label: "下载中" | "已完成" | "已整理" | "出错";
  size: number;
  dlspeed: number;
  added_on: string | null;
}

// 点开某行时展开的该种子内文件明细(区分同名分集种子到底是哪一集)
interface TorrentFile {
  name: string;
  size: number;
  progress: number; // 0-100
}

const API_BASE = "http://127.0.0.1:8080";
const POLL_INTERVAL_MS = 5000;
const PAGE_SIZE = 20;
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

  // 展开某行看该种子的文件明细:仿RssPage,点行切换,展开时按hash拉一次文件列表
  const [expandedHash, setExpandedHash] = useState<string | null>(null);
  const [files, setFiles] = useState<TorrentFile[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);

  // 前端分页游标:默认只渲染前PAGE_SIZE条,点"加载更多"再+PAGE_SIZE。
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

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

  // 展开某行时拉取该种子的文件明细;收起(expandedHash为null)时清空。
  useEffect(() => {
    if (expandedHash === null) {
      setFiles([]);
      return;
    }
    let cancelled = false;
    setFilesLoading(true);
    fetch(`${API_BASE}/downloads/tasks/${expandedHash}/files`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: TorrentFile[]) => {
        if (!cancelled) setFiles(data);
      })
      .catch((err) => {
        console.error("加载种子文件明细失败", err);
        if (!cancelled) setFiles([]);
      })
      .finally(() => {
        if (!cancelled) setFilesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [expandedHash]);

  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      if (statusFilter !== "全部" && task.status_label !== statusFilter) return false;
      if (keyword.trim() && !task.name.toLowerCase().includes(keyword.trim().toLowerCase())) {
        return false;
      }
      return true;
    });
  }, [tasks, statusFilter, keyword]);

  // 筛选条件变化时回到第一页(重置游标)。注意依赖里只有筛选项,不含tasks——
  // 5秒轮询更新的是tasks,不能让轮询把游标塌回PAGE_SIZE、收回用户已"加载更多"的行。
  useEffect(() => {
    setVisibleCount(PAGE_SIZE);
  }, [statusFilter, keyword]);

  const visibleTasks = filteredTasks.slice(0, visibleCount);

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

  const handleRetryOne = async (hash: string) => {
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/downloads/tasks/${hash}/retry`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadTasks(false);
    } catch (err) {
      console.error("重试下载任务失败", err);
    } finally {
      setBusy(false);
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
              {visibleTasks.map((task, index) => (
                <Fragment key={task.hash}>
                <tr
                  onClick={() =>
                    setExpandedHash(expandedHash === task.hash ? null : task.hash)
                  }
                  className={`cursor-pointer transition-colors hover:bg-surface-hover ${
                    expandedHash === task.hash ? "bg-surface-hover" : ""
                  }`}
                >
                  {/* 复选框列:阻止冒泡,勾选时不触发整行展开 */}
                  <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
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
                  {/* 操作列:阻止冒泡,删除/重试/取消不触发整行展开 */}
                  <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
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
                      <div className="flex min-w-[88px] items-center gap-3 font-mono text-[11px]">
                        {task.status_label === "出错" && (
                          <button
                            disabled={busy}
                            onClick={() => handleRetryOne(task.hash)}
                            className="text-muted transition-colors hover:text-vermillion disabled:opacity-40"
                          >
                            重试
                          </button>
                        )}
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
                {/* 展开的文件明细:点击行紧跟在下方展开,同名分集种子靠这里的实际
                    文件名(带集数)区分是哪一集 */}
                {expandedHash === task.hash && (
                  <tr>
                    <td colSpan={8} className="bg-surface p-4">
                      <div className="mb-2 font-mono text-[11px] uppercase tracking-wide text-muted">
                        文件明细
                      </div>
                      {filesLoading && (
                        <div className="font-mono text-[11px] text-muted">加载中...</div>
                      )}
                      {!filesLoading && files.length === 0 && (
                        <div className="font-mono text-[11px] text-muted">
                          没有拿到文件明细(种子元数据可能还没就绪)。
                        </div>
                      )}
                      {!filesLoading && files.length > 0 && (
                        <div className="flex flex-col gap-2">
                          {files.map((f, i) => (
                            <div
                              key={i}
                              className="flex items-center justify-between gap-3 rounded border border-border px-3 py-2 font-mono text-[11px]"
                            >
                              <span className="min-w-0 flex-1 truncate text-paper" title={f.name}>
                                {f.name}
                              </span>
                              <span className="shrink-0 text-muted">{formatSize(f.size)}</span>
                              <span className="w-12 shrink-0 text-right text-muted">
                                {f.progress}%
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
          {visibleCount < filteredTasks.length && (
            <div className="border-t border-border p-2 text-center">
              <button
                type="button"
                onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                className="font-mono text-xs text-muted transition-colors hover:text-vermillion"
              >
                加载更多(已显示 {visibleTasks.length} / {filteredTasks.length})
              </button>
            </div>
          )}
          </div>
        </div>
      )}
    </div>
  );
}
