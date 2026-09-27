import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  FolderMinus,
  ImageIcon,
  Loader2,
  Move,
  Play,
  RefreshCcw,
  Settings2,
  Trash2,
  Type,
} from "lucide-react";
import { proxiedImageUrl } from "../../utils/proxiedImage";
import { API_BASE } from "../../api";
import EntryPickerDialog, { type EntryPickerRequest } from "./EntryPickerDialog";
import ImagePickerDialog from "./ImagePickerDialog";
import { loadAnimeMeta } from "./meta";
import { beginPlay, endPlay, openPlayer, postWatch } from "./playback";
import { videoPath } from "./paths";
import RegroupDialog from "./RegroupDialog";
import { useLibrarySettings } from "./settings";
import { HERO_BANNER_HEIGHT, type ImageKind, type LibraryAnime, type RegroupTarget, type StandaloneItem } from "./types";

type WatchFilter = "all" | "unwatched" | "watched";

export default function MovieLibraryPage() {
  const settings = useLibrarySettings();
  const [animes, setAnimes] = useState<LibraryAnime[]>([]);
  const [standalones, setStandalones] = useState<StandaloneItem[]>([]);
  const [standaloneLoading, setStandaloneLoading] = useState(false);
  const [manage, setManage] = useState(false);
  const [activeBgm, setActiveBgm] = useState<number | null>(null);
  const [expandedBgm, setExpandedBgm] = useState<number | null>(null);
  const [watchFilter, setWatchFilter] = useState<WatchFilter>("all");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [regroupTarget, setRegroupTarget] = useState<RegroupTarget | null>(null);
  const [regroupNotice, setRegroupNotice] = useState<string | null>(null);
  const [entryRequest, setEntryRequest] = useState<EntryPickerRequest | null>(null);
  const [imageKind, setImageKind] = useState<ImageKind | null>(null);
  const [animeMeta, setAnimeMeta] = useState<Awaited<ReturnType<typeof loadAnimeMeta>>>(null);
  const playLock = useRef(false);
  const metaTicket = useRef(0);

  const loadStandalones = (silent = false) => {
    if (!silent) setStandaloneLoading(true);
    fetch(`${API_BASE}/library/standalone`)
      .then((res) => res.json())
      .then((data) => setStandalones(Array.isArray(data) ? data : []))
      .catch(() => setStandalones([]))
      .finally(() => {
        if (!silent) setStandaloneLoading(false);
      });
  };

  useEffect(() => {
    loadStandalones();
    // 「合并到…」的下拉需要系列列表。只读一次，不扫盘，也不跟着封面补全轮询。
    fetch(`${API_BASE}/library/animes`)
      .then((res) => res.json())
      .then((data) => {
        if (Array.isArray(data)) setAnimes(data);
      })
      .catch(() => {});
  }, []);

  const groups = useMemo(() => {
    const grouped = new Map<number, StandaloneItem[]>();
    for (const item of standalones) {
      const list = grouped.get(item.bgm_id) ?? [];
      list.push(item);
      grouped.set(item.bgm_id, list);
    }
    return Array.from(grouped.entries()).map(([bgm_id, items]) => ({ bgm_id, items }));
  }, [standalones]);

  // 整组每一集都看过，才算「已看」。
  const filteredGroups = useMemo(() => {
    if (watchFilter === "all") return groups;
    return groups.filter((group) => {
      const allWatched = group.items.every((item) => item.is_watched);
      return watchFilter === "watched" ? allWatched : !allWatched;
    });
  }, [groups, watchFilter]);

  const activeItems = useMemo(
    () => (activeBgm === null ? [] : standalones.filter((item) => item.bgm_id === activeBgm)),
    [activeBgm, standalones],
  );
  const activeHead = activeItems[0];
  const expandedItems = useMemo(
    () => (expandedBgm === null ? [] : standalones.filter((item) => item.bgm_id === expandedBgm)),
    [expandedBgm, standalones],
  );

  useEffect(() => {
    const stillVisible = filteredGroups.some((group) => group.bgm_id === activeBgm);
    if (!stillVisible) {
      const next = filteredGroups[0]?.bgm_id ?? null;
      if (next !== activeBgm) setActiveBgm(next);
    }
    if (expandedBgm !== null && !filteredGroups.some((group) => group.bgm_id === expandedBgm)) {
      setExpandedBgm(null);
    }
  }, [filteredGroups, activeBgm, expandedBgm]);

  // hover 划过多张卡时，200ms 内只请求最后一张的背景和 LOGO。
  // 先作废上一张的回包，避免慢请求把已经切走的图写回来。
  useEffect(() => {
    const ticket = ++metaTicket.current;
    setAnimeMeta(null);
    const bgmId = activeHead?.bgm_id;
    if (!bgmId) return;
    const timer = setTimeout(() => {
      loadAnimeMeta(bgmId, "movie").then((data) => {
        if (metaTicket.current === ticket) setAnimeMeta(data);
      });
    }, 200);
    return () => clearTimeout(timer);
  }, [activeHead?.bgm_id]);

  const playItem = async (item: StandaloneItem) => {
    if (!beginPlay(playLock)) return;
    try {
      postWatch(item.library_folder, item.filename, item.rel_path);
      await openPlayer(settings, [videoPath(settings.library_root, item.rel_path)]);
      setStandalones((prev) => prev.map((row) => (row.rel_path === item.rel_path ? { ...row, is_watched: true } : row)));
    } finally {
      endPlay(playLock);
    }
  };

  const deleteFile = async (item: StandaloneItem) => {
    setBusy(true);
    setActionError(null);
    try {
      const res = await fetch(`${API_BASE}/library/episode/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel_path: item.rel_path }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStandalones((prev) => prev.filter((row) => row.rel_path !== item.rel_path));
      setPendingDelete(null);
    } catch (err) {
      console.error("删除剧场版文件失败", err);
      setActionError("删除文件失败");
    } finally {
      setBusy(false);
    }
  };

  // 一张卡可能有多集。逐个删登记，忙状态盖住整段，失败的文件留在列表里并提示。
  const removeItems = async (items: StandaloneItem[]) => {
    if (items.length === 0 || busy) return;
    setBusy(true);
    setActionError(null);
    const failed: string[] = [];
    const removed = new Set<number>();
    for (const item of items) {
      try {
        const res = await fetch(`${API_BASE}/library/standalone/${item.id}`, { method: "DELETE" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        removed.add(item.id);
      } catch (err) {
        console.error("移出剧场版列表失败", err);
        failed.push(item.filename);
      }
    }
    if (removed.size > 0) {
      setStandalones((prev) => prev.filter((row) => !removed.has(row.id)));
    }
    setBusy(false);
    if (failed.length > 0) setActionError(`移出失败：${failed.join("、")}`);
  };

  const hoverCard = (bgmId: number) => {
    setActiveBgm(bgmId);
    setExpandedBgm((current) => (current !== null && current !== bgmId ? null : current));
  };

  const clickCard = (group: { bgm_id: number; items: StandaloneItem[] }) => {
    if (manage) {
      setExpandedBgm(group.bgm_id);
      return;
    }
    if (group.items.length === 1) playItem(group.items[0]);
    else setExpandedBgm(group.bgm_id);
  };

  const currentImageUrl =
    imageKind === "backdrop" ? animeMeta?.backdrop_url ?? null : imageKind === "logo" ? animeMeta?.logo_url ?? null : null;

  return (
    <div className="flex h-full flex-col text-paper">
      {activeHead && (
        <div className="fixed inset-0 -z-10">
          {activeHead.cover_url && (
            <img
              src={proxiedImageUrl(activeHead.cover_url)}
              alt=""
              className="h-full w-full scale-125 object-cover object-top blur-2xl"
            />
          )}
          {animeMeta?.status === "resolved" && animeMeta.backdrop_url && (
            <>
              {/* 模糊底铺满窗口。真图只占顶部一条，向左、向下渐隐，避免宽屏被裁成一条。 */}
              <img
                src={proxiedImageUrl(animeMeta.backdrop_url)}
                alt=""
                className="absolute inset-0 h-full w-full scale-125 object-cover blur-2xl"
              />
              <div className="absolute inset-x-0 top-0 flex justify-end overflow-hidden" style={{ height: HERO_BANNER_HEIGHT }}>
                <img
                  src={proxiedImageUrl(animeMeta.backdrop_url)}
                  alt=""
                  className="h-full w-auto max-w-full object-cover object-top opacity-90"
                  style={{
                    maskImage:
                      "linear-gradient(to right, transparent, black 35%), linear-gradient(to bottom, black 55%, transparent)",
                    maskComposite: "intersect",
                    WebkitMaskImage:
                      "linear-gradient(to right, transparent, black 35%), linear-gradient(to bottom, black 55%, transparent)",
                    WebkitMaskComposite: "source-in",
                  }}
                />
              </div>
            </>
          )}
          <div className="absolute inset-0 bg-gradient-to-b from-ink/40 via-ink/15 to-ink/75" />
        </div>
      )}

      {/* 标题、hero、明细行冻在顶部。暗化只靠上面那层 fixed 渐变，这里不再铺第二层底。 */}
      <div className="relative shrink-0 overflow-hidden px-8 pb-6 pt-8">
        <div className="relative mb-6 flex items-center justify-between">
          <h1 className="font-display text-2xl tracking-tight">剧场版</h1>
          <div className="flex items-center gap-2">
            <div className="flex overflow-hidden rounded-md border border-border font-mono text-xs">
              {(
                [
                  { value: "all", label: "全部" },
                  { value: "unwatched", label: "未看" },
                  { value: "watched", label: "已看" },
                ] as { value: WatchFilter; label: string }[]
              ).map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setWatchFilter(opt.value)}
                  className={`px-3 py-1.5 transition-colors ${
                    watchFilter === opt.value
                      ? "bg-vermillion text-ink"
                      : "bg-surface text-muted hover:bg-surface-hover hover:text-paper"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <button
              onClick={() => {
                setManage((value) => !value);
                setPendingDelete(null);
              }}
              className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                manage
                  ? "border-vermillion bg-vermillion text-ink"
                  : "border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion"
              }`}
            >
              <Settings2 size={14} /> {manage ? "结束管理" : "管理"}
            </button>
          </div>
        </div>

        {actionError && (
          <div className="relative mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
            {actionError}
          </div>
        )}
        {regroupNotice && (
          <div className="relative mb-4 flex items-start justify-between gap-3 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-[11px] text-vermillion">
            <span>{regroupNotice}</span>
            <button onClick={() => setRegroupNotice(null)} className="shrink-0 text-muted transition-colors hover:text-paper">
              知道了
            </button>
          </div>
        )}

        {activeHead && (
          <div className="relative flex gap-6">
            <div className="h-56 w-40 shrink-0 overflow-hidden rounded-md border border-border bg-surface shadow-2xl">
              {activeHead.cover_url ? (
                <img src={proxiedImageUrl(activeHead.cover_url)} alt={activeHead.title ?? ""} className="h-full w-full object-cover" />
              ) : (
                <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted">
                  <Loader2 size={24} strokeWidth={1.5} className="animate-spin" />
                  <span className="font-mono text-[10px]">等待更新</span>
                </div>
              )}
            </div>
            {/* 右列跟海报等高。简介在内部滚动，下边线不会超出海报。 */}
            <div className="flex h-56 min-w-0 flex-1 flex-col overflow-hidden">
              <div className="mb-2 flex h-16 shrink-0 items-end">
                {animeMeta?.status === "resolved" && animeMeta.logo_url ? (
                  <img
                    src={proxiedImageUrl(animeMeta.logo_url)}
                    alt={activeHead.title || activeHead.filename}
                    className="max-h-16 max-w-full object-contain object-left [filter:drop-shadow(0_1px_1px_rgba(0,0,0,0.55))_drop-shadow(0_0_7px_rgba(0,0,0,0.6))]"
                  />
                ) : (
                  <h1 className="line-clamp-2 font-display text-2xl leading-tight tracking-tight drop-shadow">
                    {activeHead.title || activeHead.filename}
                  </h1>
                )}
              </div>
              {animeMeta?.status === "resolved" &&
                (animeMeta.content_rating || (animeMeta.genres?.length ?? 0) > 0 || (animeMeta.studios?.length ?? 0) > 0) && (
                  <div className="mb-2 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-paper/90 drop-shadow">
                    {animeMeta.content_rating && (
                      <span className="rounded border border-border/80 bg-ink/40 px-1.5 py-0.5">{animeMeta.content_rating}</span>
                    )}
                    {(animeMeta.genres?.length ?? 0) > 0 && <span>{animeMeta.genres!.join(" / ")}</span>}
                    {(animeMeta.studios?.length ?? 0) > 0 && <span>{animeMeta.studios!.join(" / ")}</span>}
                  </div>
                )}
              <p className="min-h-0 max-w-2xl flex-1 overflow-y-auto font-mono text-xs leading-relaxed text-paper/90 drop-shadow">
                {activeHead.summary || "暂无简介"}
              </p>
              {manage && (
                <div className="mt-3 flex shrink-0 items-center gap-2 font-mono text-xs">
                  <button
                    onClick={() =>
                      setEntryRequest({
                        mode: "regroup",
                        ids: activeItems.map((item) => item.id),
                        keyword: activeItems[0]?.title ?? "",
                      })
                    }
                    className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                  >
                    <RefreshCcw size={14} /> 重选条目
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => removeItems(activeItems)}
                    className="flex items-center gap-1.5 rounded-md border border-vermillion bg-ink/60 px-3 py-1.5 text-vermillion backdrop-blur transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                  >
                    <FolderMinus size={14} /> 移出列表
                  </button>
                  {/* 剧场版单独记一份海报 / LOGO（scope=movie），不改媒体库详情那一份。 */}
                  {activeHead.bgm_id && animeMeta?.status === "resolved" && (
                    <>
                      <button
                        onClick={() => setImageKind("backdrop")}
                        title="调整海报图片"
                        className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                      >
                        <ImageIcon size={14} /> 调整海报图片
                      </button>
                      <button
                        onClick={() => setImageKind("logo")}
                        title="调整logo图片"
                        className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                      >
                        <Type size={14} /> 调整logo图片
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {expandedBgm !== null && expandedItems.length > 0 && (
          <div className="relative mt-4 flex flex-col gap-1.5 rounded-md border border-border bg-surface p-3">
            {expandedItems.map((item) => (
              <div key={item.id} className="flex items-center justify-between gap-3 rounded px-2 py-1.5 hover:bg-paper/5">
                <div className="flex min-w-0 items-center gap-2">
                  {item.is_watched && <CheckCircle2 size={14} className="shrink-0 text-green-500/80" />}
                  <span
                    className={`min-w-0 truncate font-mono text-xs ${item.is_watched ? "text-muted line-through" : "text-paper"} ${item.missing ? "opacity-50" : ""}`}
                    title={item.rel_path}
                  >
                    {item.filename}
                    {item.missing ? "(文件缺失)" : ""}
                  </span>
                </div>
                {manage ? (
                  pendingDelete === item.rel_path ? (
                    <div className="flex shrink-0 items-center gap-1.5 font-mono text-xs">
                      <button
                        disabled={busy}
                        onClick={() => deleteFile(item)}
                        className="rounded border border-vermillion bg-vermillion px-2 py-1 text-ink hover:bg-vermillion/90 disabled:opacity-40"
                      >
                        确定删
                      </button>
                      <button
                        onClick={() => setPendingDelete(null)}
                        className="rounded border border-border bg-surface px-2 py-1 text-muted hover:text-paper"
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <div className="flex shrink-0 items-center gap-2 font-mono text-xs">
                      <button
                        onClick={() => setPendingDelete(item.rel_path)}
                        className="flex items-center gap-1 rounded-md border border-vermillion bg-vermillion px-3 py-1 text-ink hover:bg-vermillion/90"
                      >
                        <Trash2 size={13} /> 删除文件
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => removeItems([item])}
                        className="flex items-center gap-1 rounded-md border border-border px-3 py-1 text-muted hover:border-vermillion hover:text-vermillion disabled:opacity-40"
                      >
                        <FolderMinus size={13} /> 仅移出
                      </button>
                      <button
                        disabled={busy || item.missing}
                        onClick={() =>
                          setRegroupTarget({
                            label: item.filename,
                            relPaths: [item.rel_path],
                            bgmId: item.bgm_id,
                          })
                        }
                        className="flex items-center gap-1 rounded-md border border-border px-3 py-1 text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
                      >
                        <Move size={13} /> 调整归属…
                      </button>
                    </div>
                  )
                ) : (
                  <button
                    disabled={item.missing}
                    onClick={() => playItem(item)}
                    className={`flex w-24 shrink-0 items-center justify-center gap-1.5 rounded-md border px-3 py-1 font-mono text-xs transition-colors disabled:opacity-40 ${
                      item.is_watched
                        ? "border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion"
                        : "border-vermillion bg-vermillion text-ink hover:bg-vermillion/90"
                    }`}
                  >
                    <Play size={13} fill="currentColor" />
                    {item.is_watched ? "再看" : "播放"}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 只有卡片区滚动。窗口矮时，卡片不会滚进透明的 hero 后面。 */}
      <div className="min-h-0 flex-1 overflow-y-auto px-8 pb-8 pt-3">
        {standaloneLoading ? (
          <div className="font-mono text-xs text-muted">正在加载...</div>
        ) : groups.length === 0 ? (
          <div className="py-16 text-center font-mono text-xs text-muted">
            还没有独立展示的剧场版/OVA。下载剧场版会自动加入;也可在某部番的详情页里,把某一集设为独立剧场版/OVA。
          </div>
        ) : filteredGroups.length === 0 ? (
          <div className="py-16 text-center font-mono text-xs text-muted">当前筛选下没有内容。</div>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
            {filteredGroups.map((group) => {
              const head = group.items[0];
              const allWatched = group.items.every((item) => item.is_watched);
              const isActive = group.bgm_id === activeBgm;
              return (
                <div
                  key={group.bgm_id}
                  onMouseEnter={() => hoverCard(group.bgm_id)}
                  onClick={() => clickCard(group)}
                  className="group cursor-pointer"
                >
                  <div
                    className={`relative aspect-[2/3] overflow-hidden rounded-md bg-surface shadow-md ring-2 transition-all ${
                      isActive ? "ring-vermillion" : "ring-transparent"
                    }`}
                  >
                    {head.cover_url ? (
                      <img
                        src={proxiedImageUrl(head.cover_url)}
                        alt={head.title ?? head.filename}
                        className="h-full w-full object-cover transition-transform group-hover:scale-105"
                      />
                    ) : (
                      <div className="flex h-full w-full flex-col items-center justify-center gap-1 border border-border bg-surface text-muted">
                        <Loader2 size={22} strokeWidth={1.5} className="animate-spin" />
                        <span className="font-mono text-[10px]">等待更新</span>
                      </div>
                    )}
                    <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-ink/25 opacity-0 transition-opacity group-hover:opacity-100">
                      <Play size={40} fill="currentColor" className="text-vermillion drop-shadow-lg" />
                    </div>
                  </div>
                  <div
                    className={`mt-2 line-clamp-2 text-sm font-medium leading-snug transition-colors ${
                      isActive ? "text-vermillion" : "group-hover:text-vermillion"
                    }`}
                  >
                    {head.title || head.filename}
                    {allWatched && (
                      <span className="ml-1 inline-flex items-center align-middle text-green-500/80" title="已播放">
                        <CheckCircle2 size={13} />
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {entryRequest && (
        <EntryPickerDialog
          request={entryRequest}
          onClose={() => setEntryRequest(null)}
          onSaved={() => loadStandalones(true)}
        />
      )}
      {regroupTarget && (
        <RegroupDialog
          target={regroupTarget}
          fallbackBgmId={regroupTarget.bgmId}
          animes={animes}
          onClose={() => setRegroupTarget(null)}
          onCompleted={(notice) => {
            setRegroupNotice(notice);
            setRegroupTarget(null);
            loadStandalones(true);
          }}
        />
      )}
      {imageKind && activeHead?.bgm_id && (
        <ImagePickerDialog
          kind={imageKind}
          bgmId={activeHead.bgm_id}
          scope="movie"
          currentUrl={currentImageUrl}
          onClose={() => setImageKind(null)}
          onSaved={() => {
            const ticket = ++metaTicket.current;
            loadAnimeMeta(activeHead.bgm_id, "movie").then((data) => {
              if (metaTicket.current === ticket) setAnimeMeta(data);
            });
          }}
        />
      )}
    </div>
  );
}
