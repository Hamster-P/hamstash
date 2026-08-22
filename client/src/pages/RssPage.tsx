import { Fragment, useEffect, useState } from "react";

interface RssSubscription {
  id: number;
  anime_title: string;
  bgm_id: number | null;
  keyword: string;
  fansub_name: string | null;
  quality: string | null;
  auto_rename: boolean;
  enabled: boolean;
  rss_url: string | null;
  created_at: string;
  last_polled_at: string | null;
}

interface RssMatchedItem {
  id: number;
  guid: string;
  title: string;
  magnet: string | null;
  download_status: string;
  error: string | null;
  matched_at: string;
}

const API_BASE = "http://127.0.0.1:8080";

function formatDate(iso: string) {
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

const STATUS_LABEL: Record<string, string> = {
  added: "已下载",
  failed: "失败",
  skipped: "已跳过",
};

export default function RssPage() {
  const [subs, setSubs] = useState<RssSubscription[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [matchedItems, setMatchedItems] = useState<RssMatchedItem[]>([]);
  const [matchedLoading, setMatchedLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // 上次 RSS 轮询的前置障碍消息(代理/qB 不可达),空=正常。只在进入页面时读一次(见下方
  // 挂载 effect),不做定时刷新;"更新所有RSS源"按钮会重查并刷新它。
  const [statusMessage, setStatusMessage] = useState("");
  // 升级时做过的一次性数据迁移提示。跟 statusMessage 分开:那个是每轮轮询重算的
  // 瞬时状态(代理/qB 不可达),这个要一直留到用户点掉。
  const [notice, setNotice] = useState("");
  const [refreshingAll, setRefreshingAll] = useState(false);

  const loadSubs = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await fetch(`${API_BASE}/rss-engine/subscriptions`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setSubs(data);
    } catch (err) {
      console.error("加载RSS订阅一览失败", err);
      setLoadError("加载失败,请检查后端是否已启动");
    } finally {
      setLoading(false);
    }
  };

  // 读取上次 RSS 轮询的障碍消息(只读后端全局变量,不发网络探测)。
  const loadStatus = async () => {
    try {
      const res = await fetch(`${API_BASE}/rss-engine/status`);
      if (!res.ok) return;
      const data = (await res.json()) as { message: string; notice?: string };
      setStatusMessage(data.message ?? "");
      setNotice(data.notice ?? "");
    } catch {
      // 后端没起/请求失败:保持上次已知消息,不误报
    }
  };

  // 一次性升级提示(比如字幕组识别改动导致订阅被重置),点掉之后后端不再返回。
  const dismissNotice = async () => {
    setNotice("");
    try {
      await fetch(`${API_BASE}/rss-engine/status/dismiss-notice`, { method: "POST" });
    } catch {
      // 清不掉也没关系:下次进页面会再提示一遍,不会丢信息
    }
  };

  useEffect(() => {
    loadSubs();
    // 只在挂载(进入/重新进入本页)时刷新一次状态消息,不做定时轮询。
    loadStatus();
  }, []);

  // "更新所有RSS源":后端同步重查代理/qB(立即更新红字),无障碍则后台重跑一轮抓取。
  const handleRefreshAll = async () => {
    setRefreshingAll(true);
    try {
      const res = await fetch(`${API_BASE}/rss-engine/refresh-all`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { message: string };
      setStatusMessage(data.message ?? "");
    } catch (err) {
      console.error("更新所有RSS源失败", err);
    } finally {
      setRefreshingAll(false);
    }
  };

  // 选中某条订阅时加载它的命中记录——每次切换选中项都重新拉一次最新列表。
  const loadMatchedItems = async (id: number) => {
    setMatchedLoading(true);
    try {
      const res = await fetch(`${API_BASE}/rss-engine/subscriptions/${id}/matched-items`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setMatchedItems(await res.json());
    } catch (err) {
      console.error("加载命中记录失败", err);
      setMatchedItems([]);
    } finally {
      setMatchedLoading(false);
    }
  };

  useEffect(() => {
    if (selectedId !== null) loadMatchedItems(selectedId);
    else setMatchedItems([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const handleToggle = async (sub: RssSubscription) => {
    setBusyId(sub.id);
    try {
      const res = await fetch(
        `${API_BASE}/rss-engine/subscriptions/${sub.id}/toggle`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !sub.enabled }),
        },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated: RssSubscription = await res.json();
      setSubs((prev) => prev.map((s) => (s.id === sub.id ? updated : s)));
    } catch (err) {
      console.error("切换RSS开关失败", err);
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async (id: number) => {
    setBusyId(id);
    setDeleteError(null);
    try {
      const res = await fetch(`${API_BASE}/rss-engine/subscriptions/${id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.detail ?? `HTTP ${res.status}`);
      }
      setSubs((prev) => prev.filter((s) => s.id !== id));
      if (selectedId === id) setSelectedId(null);
    } catch (err) {
      console.error("删除RSS订阅失败", err);
      setDeleteError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setBusyId(null);
      setPendingDeleteId(null);
    }
  };

  // "立即更新":不等自动轮询周期,马上对这条订阅跑一轮抓取+匹配+下载,
  // 跑完之后顺手把命中记录列表刷新一遍,能立刻看到新结果。
  const handleRefreshNow = async (id: number) => {
    setRefreshing(true);
    try {
      const res = await fetch(`${API_BASE}/rss-engine/subscriptions/${id}/refresh-now`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadMatchedItems(id);
    } catch (err) {
      console.error("立即更新失败", err);
    } finally {
      setRefreshing(false);
    }
  };

  const selected = subs.find((s) => s.id === selectedId) ?? null;

  return (
    <div>
      {/* 冻结顶部:标题 + "更新所有RSS源",滚动列表时始终可见 */}
      <div className="sticky top-0 z-10 bg-ink px-8 pb-4 pt-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-2xl tracking-tight">RSS订阅一览</h1>
          <p className="mt-1 font-mono text-[11px] text-muted">
            后台每隔一段时间自动轮询抓取匹配的新种子;开关只控制是否参与轮询,删除前会先要求二次确认
          </p>
        </div>
        <button
          onClick={handleRefreshAll}
          disabled={refreshingAll}
          className="shrink-0 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
        >
          {refreshingAll ? "更新中..." : "更新所有RSS源"}
        </button>
      </div>
      </div>
      {/* /冻结顶部 */}

      <div className="px-8 pb-8">
      {statusMessage && (
        <div className="mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
          {statusMessage}
        </div>
      )}

      {/* 一次性升级提示:用 gold 而不是 vermillion,跟上面"出错了"的红字区分开——
          这是"我们替你改了配置"的知会,不是故障。 */}
      {notice && (
        <div className="mb-4 flex items-start justify-between gap-3 rounded-md border border-gold/40 bg-surface p-3 font-mono text-xs text-gold">
          <span>{notice}</span>
          <button
            onClick={dismissNotice}
            className="shrink-0 text-muted transition-colors hover:text-paper"
          >
            知道了
          </button>
        </div>
      )}

      {loadError && (
        <div className="mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
          {loadError}
        </div>
      )}

      {deleteError && (
        <div className="mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
          删除失败: {deleteError}
        </div>
      )}

      {!loading && !loadError && subs.length === 0 && (
        <div className="rounded-md border border-border bg-surface p-6 text-center font-mono text-xs text-muted">
          还没有任何RSS订阅。去"下载"页搜索番剧,选中种子后打开"RSS订阅"开关并提交即可创建。
        </div>
      )}

      {subs.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          {/* 内层套一层横向滚动,窄窗口下表格不会撑破页面布局 */}
          <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-surface font-mono text-[11px] uppercase tracking-wide text-muted">
              <tr>
                <th className="px-3 py-2">番剧</th>
                <th className="px-3 py-2">关键词</th>
                <th className="px-3 py-2">字幕组</th>
                <th className="px-3 py-2">画质</th>
                <th className="px-3 py-2">状态</th>
                <th className="px-3 py-2">开关</th>
                <th className="px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {subs.map((sub) => (
                <Fragment key={sub.id}>
                  <tr
                    onClick={() =>
                      setSelectedId(selectedId === sub.id ? null : sub.id)
                    }
                    className={`cursor-pointer transition-colors hover:bg-surface-hover ${
                      selectedId === sub.id ? "bg-surface-hover" : ""
                    }`}
                  >
                    <td className="px-3 py-2">{sub.anime_title}</td>
                    <td className="max-w-[160px] truncate px-3 py-2 font-mono text-xs text-muted">
                      {sub.keyword}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-vermillion">
                      {sub.fansub_name ?? "不限"}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {sub.quality ?? "不限"}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px]">
                      {sub.enabled ? (
                        <span className="text-gold">运行中</span>
                      ) : (
                        <span className="text-muted">已暂停</span>
                      )}
                    </td>
                    <td
                      className="px-3 py-2"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        type="button"
                        disabled={busyId === sub.id}
                        onClick={() => handleToggle(sub)}
                        aria-pressed={sub.enabled}
                        className={`relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 disabled:opacity-40 ${
                          sub.enabled ? "bg-toggle-on" : "bg-border"
                        }`}
                      >
                        <span
                          className="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200"
                          style={{
                            transform: sub.enabled
                              ? "translateX(20px)"
                              : "translateX(0px)",
                          }}
                        />
                      </button>
                    </td>
                    <td
                      className="px-3 py-2"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {pendingDeleteId === sub.id ? (
                        <div className="flex items-center gap-2 font-mono text-[11px]">
                          <button
                            disabled={busyId === sub.id}
                            onClick={() => handleDelete(sub.id)}
                            className="rounded border border-vermillion px-2 py-0.5 text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                          >
                            删除
                          </button>
                          <button
                            onClick={() => setPendingDeleteId(null)}
                            className="rounded border border-border px-2 py-0.5 text-muted transition-colors hover:text-paper"
                          >
                            取消
                          </button>
                        </div>
                      ) : (
                        <div className="flex items-center min-w-[88px] font-mono text-[11px]">
                          <button
                            onClick={() => setPendingDeleteId(sub.id)}
                            className="text-muted transition-colors hover:text-vermillion"
                          >
                            删除
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                  {/* 详情/命中记录:内嵌成点击行紧跟着的下一行,而不是整张表格下面的
                      独立区块——用户希望点哪行详情就跟在哪行下面展开,不用来回找。 */}
                  {selected && selected.id === sub.id && (
                    <tr key={`${sub.id}-detail`}>
                      <td colSpan={7} className="bg-surface p-4">
                        <div className="mb-2 flex items-center justify-between">
                          <div className="font-mono text-[11px] uppercase tracking-wide text-muted">
                            订阅详情 · #{selected.id}
                          </div>
                          <button
                            onClick={() => handleRefreshNow(selected.id)}
                            disabled={refreshing}
                            className="rounded border border-vermillion px-2 py-1 font-mono text-[11px] text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                          >
                            {refreshing ? "更新中..." : "立即更新"}
                          </button>
                        </div>
                        <div className="flex flex-col gap-1.5 font-mono text-[11px] text-muted">
                          <div>自动改名: {selected.auto_rename ? "开启" : "关闭"}</div>
                          <div>创建时间: {formatDate(selected.created_at)}</div>
                          <div>
                            上次更新时间:{" "}
                            {selected.last_polled_at
                              ? formatDate(selected.last_polled_at)
                              : "还没轮询过"}
                          </div>
                        </div>

                        <div className="mt-4 border-t border-border pt-3">
                          <div className="mb-2 font-mono text-[11px] uppercase tracking-wide text-muted">
                            命中记录
                          </div>
                          {matchedLoading && (
                            <div className="font-mono text-[11px] text-muted">加载中...</div>
                          )}
                          {!matchedLoading && matchedItems.length === 0 && (
                            <div className="font-mono text-[11px] text-muted">
                              还没有命中过任何文章,等下一轮自动轮询,或点击"立即更新"。
                            </div>
                          )}
                          {!matchedLoading && matchedItems.length > 0 && (
                            <div className="flex flex-col gap-2">
                              {matchedItems.map((item) => (
                                <div
                                  key={item.id}
                                  className="flex flex-col gap-0.5 rounded border border-border px-3 py-2 font-mono text-[11px]"
                                >
                                  <div className="flex items-center justify-between gap-2">
                                    <span className="truncate text-paper">{item.title}</span>
                                    <span
                                      className={
                                        item.download_status === "added"
                                          ? "shrink-0 text-gold"
                                          : item.download_status === "failed"
                                            ? "shrink-0 text-vermillion"
                                            : "shrink-0 text-muted"
                                      }
                                    >
                                      {STATUS_LABEL[item.download_status] ?? item.download_status}
                                    </span>
                                  </div>
                                  <div className="text-muted">{formatDate(item.matched_at)}</div>
                                  {item.error && <div className="text-vermillion">{item.error}</div>}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}
      </div>
    </div>
  );
}
