import { Fragment, useEffect, useMemo, useState } from "react";

interface RssSubscription {
  id: number;
  anime_title: string;
  bgm_id: number | null;
  keyword: string;
  source: string;
  fansub_name: string | null;
  quality: string | null;
  subtitle: string | null;
  format: string | null;
  release_type: string | null;
  auto_rename: boolean;
  enabled: boolean;
  rss_url: string | null;
  created_at: string;
  last_polled_at: string | null;
  season_complete?: boolean;
}

// 搜索结果的最小形状,编辑弹窗"预览匹配"用,跟DownloadPage.tsx的ResourceItem同源
// (/resources/search 的返回字段),这里只取展示需要的几个。
interface PreviewItem {
  title: string;
  fansub_name: string;
  created_at: string | null;
}

const QUALITY_OPTIONS = ["不限", "1080p", "720p", "2160p"];
const SUBTITLE_OPTIONS = ["不限", "简体", "繁体", "简繁", "简日", "繁日", "RAW", "日文/无字"];
const FORMAT_OPTIONS = ["不限", "MKV", "MP4"];
const TYPE_OPTIONS = ["不限", "单集(追更)", "合集/全集(完结)"];

// 跟DownloadPage.tsx同款的控件样式
const controlClass =
  "rounded border border-border bg-surface px-2 py-1.5 font-mono text-xs text-paper outline-none focus:border-vermillion focus:ring-1 focus:ring-vermillion";

interface RssMatchedItem {
  id: number;
  guid: string;
  title: string;
  magnet: string | null;
  download_status: string;
  error: string | null;
  matched_at: string;
}

import { API_BASE } from "../api";

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
  const [editing, setEditing] = useState<RssSubscription | null>(null);

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
      await loadSubs();
    } catch (err) {
      console.error("立即更新失败", err);
    } finally {
      setRefreshing(false);
    }
  };

  const selected = subs.find((s) => s.id === selectedId) ?? null;

  return (
    <div className="absolute inset-0 flex flex-col overflow-hidden">
      {/* 标题固定。滚动条从订阅列表顶部开始。 */}
      <div className="shrink-0 bg-ink px-8 pb-4 pt-8">
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

      <div className="min-h-0 flex-1 overflow-y-auto px-8 pb-8">
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
                    <td className="px-3 py-2">
                      <div>{sub.anime_title}</div>
                      {sub.season_complete && (
                        <div className="mt-1 font-mono text-[11px] text-vermillion">
                          本季已全部下载，推荐删除rss订阅
                        </div>
                      )}
                    </td>
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
                        <div className="flex items-center gap-3 min-w-[88px] font-mono text-[11px]">
                          <button
                            onClick={() => setEditing(sub)}
                            className="text-muted transition-colors hover:text-paper"
                          >
                            编辑
                          </button>
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

      {editing && (
        <EditSubscriptionDialog
          sub={editing}
          onClose={() => setEditing(null)}
          onSaved={(updated) => {
            setSubs((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
            setEditing(null);
            if (selectedId === updated.id) loadMatchedItems(updated.id);
          }}
        />
      )}
    </div>
  );
}

// 编辑弹窗:只改匹配条件(关键词/字幕组/画质/字幕语言/格式/发布类型),数据源和
// 归属番保持只读展示——源决定抓取方式,改源等于换了完全不同的规则,要改请删了重建。
// "预览匹配"直接调现成的 /resources/search(搜索页同一个接口,服务端权威过滤
// matches_criteria),不另写一套匹配逻辑,保证"预览看到的"和"保存后实际会下的"一致。
function EditSubscriptionDialog({
  sub,
  onClose,
  onSaved,
}: {
  sub: RssSubscription;
  onClose: () => void;
  onSaved: (updated: RssSubscription) => void;
}) {
  const [keyword, setKeyword] = useState(sub.keyword);
  const [fansub, setFansub] = useState(sub.fansub_name ?? "");
  const [quality, setQuality] = useState(sub.quality ?? "不限");
  const [subtitle, setSubtitle] = useState(sub.subtitle ?? "不限");
  const [format, setFormat] = useState(sub.format ?? "不限");
  const [releaseType, setReleaseType] = useState(sub.release_type ?? "不限");

  // 字幕组下拉选项:后台先用"不限字幕组"搜一次(只带关键词,不带字幕组条件),
  // 从结果里的fansub_name去重收集出"这个关键词下实际有哪些字幕组在发布",
  // 跟DownloadPage.tsx的fansubOptionNames是同一个思路。
  const [fansubOptions, setFansubOptions] = useState<string[]>([]);
  const [fansubLoading, setFansubLoading] = useState(false);
  const [fansubError, setFansubError] = useState<string | null>(null);

  const loadFansubOptions = async (kw: string) => {
    if (!kw.trim()) return;
    setFansubLoading(true);
    setFansubError(null);
    try {
      const params = new URLSearchParams({ keyword: kw, source: sub.source, page: "1" });
      if (sub.bgm_id) params.set("bgm_id", String(sub.bgm_id));
      const res = await fetch(`${API_BASE}/resources/search?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const items: PreviewItem[] = data.results ?? [];
      const names = Array.from(new Set(items.map((i) => i.fansub_name))).sort();
      setFansubOptions(names);
    } catch (err) {
      console.error("获取字幕组列表失败", err);
      setFansubError("获取字幕组列表失败,请检查网络/代理");
    } finally {
      setFansubLoading(false);
    }
  };

  // 打开弹窗即按当前关键词查一次,不用用户先手动点一下才有得选
  useEffect(() => {
    loadFansubOptions(sub.keyword);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sub.id]);

  // 下拉里始终包含当前已选中的字幕组(哪怕这次没搜到——避免原有选择凭空消失/
  // 被强制重置成"不限"),以及"不限"本身。
  const fansubSelectOptions = useMemo(() => {
    const names = new Set(fansubOptions);
    if (fansub) names.add(fansub);
    return Array.from(names).sort();
  }, [fansubOptions, fansub]);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // 预览结果:编辑任一字段后标记为"未刷新"(stale),避免用户以为是最新结果。
  const [previewItems, setPreviewItems] = useState<PreviewItem[] | null>(null);
  const [previewHasMore, setPreviewHasMore] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewStale, setPreviewStale] = useState(true);

  const markStale = () => setPreviewStale(true);

  const handlePreview = async () => {
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const params = new URLSearchParams({ keyword, source: sub.source, page: "1" });
      if (sub.bgm_id) params.set("bgm_id", String(sub.bgm_id));
      if (fansub.trim()) params.set("fansub_name", fansub.trim());
      if (quality !== "不限") params.set("quality", quality);
      if (subtitle !== "不限") params.set("subtitle", subtitle);
      if (format !== "不限") params.set("format", format);
      if (releaseType !== "不限") params.set("release_type", releaseType);

      const res = await fetch(`${API_BASE}/resources/search?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const items: PreviewItem[] = data.results ?? [];
      // 按发布时间从新到旧排:没有时间的(个别源/个别条目可能取不到)排到最后,
      // 不跟有时间的混在一起干扰判断。
      const sorted = [...items].sort((a, b) => {
        if (!a.created_at && !b.created_at) return 0;
        if (!a.created_at) return 1;
        if (!b.created_at) return -1;
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      });
      setPreviewItems(sorted);
      setPreviewHasMore(Boolean(data.has_more));
      setPreviewStale(false);
    } catch (err) {
      console.error("预览匹配失败", err);
      setPreviewError("预览失败,请检查网络/代理后重试");
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleSave = async () => {
    if (!keyword.trim()) {
      setSaveError("关键词不能为空");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const res = await fetch(`${API_BASE}/rss-engine/subscriptions/${sub.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyword: keyword.trim(),
          fansub_name: fansub.trim() || null,
          quality: quality !== "不限" ? quality : null,
          subtitle: subtitle !== "不限" ? subtitle : null,
          format: format !== "不限" ? format : null,
          release_type: releaseType !== "不限" ? releaseType : null,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.detail ?? `HTTP ${res.status}`);
      }
      onSaved(await res.json());
    } catch (err) {
      console.error("保存订阅失败", err);
      setSaveError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6" onClick={onClose}>
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-md border border-border bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-center justify-between">
          <div className="text-sm">编辑订阅</div>
          <button onClick={onClose} className="rounded border border-border px-2 py-1 font-mono text-[11px] text-muted transition-colors hover:text-paper">
            关闭
          </button>
        </div>
        <div className="mb-4 font-mono text-[11px] text-muted">
          {sub.anime_title} · 数据源: {sub.source}(不可修改)
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mb-3">
            <div className="mb-1 font-mono text-[11px] text-muted">关键词</div>
            <input
              value={keyword}
              onChange={(e) => {
                setKeyword(e.target.value);
                markStale();
              }}
              className={`${controlClass} w-full`}
            />
          </div>

          <div className="mb-3">
            <div className="mb-1 flex items-center justify-between">
              <span className="font-mono text-[11px] text-muted">字幕组</span>
              <button
                onClick={() => loadFansubOptions(keyword)}
                disabled={fansubLoading || !keyword.trim()}
                className="font-mono text-[11px] text-muted transition-colors hover:text-vermillion disabled:opacity-40"
              >
                {fansubLoading ? "搜索中..." : "按当前关键词刷新"}
              </button>
            </div>
            <select
              value={fansub}
              onChange={(e) => {
                setFansub(e.target.value);
                markStale();
              }}
              className={`${controlClass} w-full`}
            >
              <option value="">不限</option>
              {fansubSelectOptions.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
            {fansubError && <div className="mt-1 font-mono text-[11px] text-vermillion">{fansubError}</div>}
            {!fansubError && !fansubLoading && fansubOptions.length === 0 && (
              <div className="mt-1 font-mono text-[11px] text-muted">
                没搜到任何字幕组,可能是关键词太窄或该源暂无结果,可改关键词后点"按当前关键词刷新"。
              </div>
            )}
          </div>

          <div className="mb-4 flex flex-wrap gap-2">
            <select
              value={quality}
              onChange={(e) => {
                setQuality(e.target.value);
                markStale();
              }}
              className={controlClass}
            >
              {QUALITY_OPTIONS.map((q) => (
                <option key={q} value={q}>{q === "不限" ? "画质: 不限" : q}</option>
              ))}
            </select>
            <select
              value={subtitle}
              onChange={(e) => {
                setSubtitle(e.target.value);
                markStale();
              }}
              className={controlClass}
            >
              {SUBTITLE_OPTIONS.map((s) => (
                <option key={s} value={s}>{s === "不限" ? "语言: 不限" : s}</option>
              ))}
            </select>
            <select
              value={format}
              onChange={(e) => {
                setFormat(e.target.value);
                markStale();
              }}
              className={controlClass}
            >
              {FORMAT_OPTIONS.map((f) => (
                <option key={f} value={f}>{f === "不限" ? "格式: 不限" : f}</option>
              ))}
            </select>
            <select
              value={releaseType}
              onChange={(e) => {
                setReleaseType(e.target.value);
                markStale();
              }}
              className={controlClass}
            >
              {TYPE_OPTIONS.map((t) => (
                <option key={t} value={t}>{t === "不限" ? "类型: 不限" : t}</option>
              ))}
            </select>
          </div>

          <div className="rounded border border-border bg-ink p-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="font-mono text-[11px] uppercase tracking-wide text-muted">
                预览匹配{previewStale && previewItems !== null ? "(未刷新)" : ""}
              </div>
              <button
                onClick={handlePreview}
                disabled={previewLoading || !keyword.trim()}
                className="rounded border border-vermillion px-2 py-1 font-mono text-[11px] text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
              >
                {previewLoading ? "匹配中..." : "预览匹配"}
              </button>
            </div>
            {previewError && <div className="font-mono text-[11px] text-vermillion">{previewError}</div>}
            {previewItems === null && !previewError && (
              <div className="font-mono text-[11px] text-muted">改好条件后点"预览匹配",看看当前能命中哪些种子。</div>
            )}
            {previewItems !== null && (
              <div className={`flex flex-col gap-1 ${previewStale ? "opacity-50" : ""}`}>
                <div className="font-mono text-[11px] text-muted">
                  命中 {previewItems.length} 条{previewHasMore ? "(含更多未展示)" : ""}
                </div>
                {previewItems.length === 0 && (
                  <div className="font-mono text-[11px] text-muted">没有匹配到任何种子,检查一下关键词/字幕组是不是改过头了。</div>
                )}
                {previewItems.slice(0, 20).map((item, i) => (
                  <div key={i} className="flex items-baseline gap-2 font-mono text-[11px]">
                    <span className="shrink-0 text-muted">
                      {item.created_at ? formatDate(item.created_at) : "时间未知"}
                    </span>
                    {/* 不再截断——文件名里的集数/画质/字幕组这些关键信息不能被省略号吞掉,
                        改为完整换行显示;title属性兼顾想整行复制/鼠标悬停确认的场景。 */}
                    <span className="break-all text-paper" title={`[${item.fansub_name}] ${item.title}`}>
                      [{item.fansub_name}] {item.title}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="mt-4 flex items-center justify-end gap-2">
          {saveError && <span className="mr-auto font-mono text-[11px] text-vermillion">{saveError}</span>}
          <button
            onClick={onClose}
            disabled={saving}
            className="rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:text-paper disabled:opacity-40"
          >
            取消
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="rounded border border-vermillion bg-vermillion px-3 py-1.5 font-mono text-xs text-ink transition-colors hover:bg-vermillion/90 disabled:opacity-40"
          >
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
