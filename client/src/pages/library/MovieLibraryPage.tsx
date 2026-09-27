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
import { btnGhost, btnPrimary, pageTitle, posterFrame, posterTitle, seg, segOff, segOn } from "./ui";

type WatchFilter = "all" | "unwatched" | "watched";

// 槽位先空着。LOGO 出来再填进去，失败或确认没有图才改成文字标题。
function HeroLogo({
  logoUrl,
  pending,
  title,
  failedUrl,
  onFail,
}: {
  logoUrl: string | null;
  pending: boolean;
  title: string;
  failedUrl: string | null;
  onFail: (url: string) => void;
}) {
  const failed = !!logoUrl && failedUrl === logoUrl;
  const showText = !pending && (!logoUrl || failed);
  return (
    // 绝对定位，图片的原始像素不能把这一格撑高。
    // 否则宽 LOGO 会按原图像素画出来，把整页布局撑爆。
    <div className="relative h-[30%] min-h-0 shrink-0 overflow-hidden">
      {logoUrl && !failed && (
        <img
          src={proxiedImageUrl(logoUrl)}
          alt=""
          onError={() => onFail(logoUrl)}
          className="absolute inset-0 h-full w-full object-contain object-left [filter:drop-shadow(0_1px_1px_rgba(0,0,0,0.55))]"
        />
      )}
      {showText && (
        <h2 className="line-clamp-2 font-display text-2xl leading-tight tracking-tight drop-shadow">{title}</h2>
      )}
    </div>
  );
}

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
  // LOGO 图加载失败才改显示文字。换地址后重新试。
  const [logoFailedUrl, setLogoFailedUrl] = useState<string | null>(null);
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
        <div className="relative mb-4 flex items-start justify-between gap-4">
          <h1 className={pageTitle}>剧场版</h1>
          <div className="flex shrink-0 items-center gap-2">
            <div className={seg}>
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
                  className={watchFilter === opt.value ? segOn : segOff}
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
              className={manage ? btnPrimary : btnGhost}
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
          <div className="relative">
          <div className="flex gap-6">
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
            {/* 高度按左侧海报切：LOGO 30%，分级 10%，简介 60%。 */}
            <div className="flex h-56 min-w-0 flex-1 flex-col">
              <HeroLogo
                logoUrl={animeMeta && animeMeta.status !== "pending" ? animeMeta.logo_url || null : null}
                pending={!animeMeta || animeMeta.status === "pending"}
                title={activeHead.title || activeHead.filename}
                failedUrl={logoFailedUrl}
                onFail={setLogoFailedUrl}
              />
              <div className="flex h-[10%] shrink-0 items-center overflow-hidden font-mono text-[11px] text-paper/90 drop-shadow">
                {animeMeta?.status === "resolved" && (
                  <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
                    {animeMeta.content_rating && (
                      <span className="rounded border border-border/80 bg-ink/40 px-1.5 py-0.5">{animeMeta.content_rating}</span>
                    )}
                    {(animeMeta.genres?.length ?? 0) > 0 && <span className="truncate">{animeMeta.genres!.join(" / ")}</span>}
                    {(animeMeta.studios?.length ?? 0) > 0 && <span className="truncate">{animeMeta.studios!.join(" / ")}</span>}
                  </div>
                )}
              </div>
              <p className="h-[60%] overflow-y-auto pr-1 text-sm text-paper/90 drop-shadow">
                {activeHead.summary || "暂无简介"}
              </p>
            </div>
          </div>
          {manage && (
                <div className="relative mt-3 flex shrink-0 items-center gap-2 font-mono text-xs">
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
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-8 pb-8 pt-4">
        {expandedBgm !== null && expandedItems.length > 0 && (
          <div className="mb-4 flex flex-col rounded-md border border-border bg-surface px-3 py-1">
            {expandedItems.map((item) => (
              <div key={item.id} className="flex items-center justify-between gap-3 rounded-md px-2 py-2 hover:bg-paper/5">
                <div className="flex min-w-0 items-center gap-2">
                  {item.is_watched && <CheckCircle2 size={14} className="shrink-0 text-vermillion" />}
                  <span
                    className={`min-w-0 truncate font-mono text-xs ${item.is_watched ? "text-muted line-through" : "text-paper"} ${item.missing ? "opacity-50" : ""}`}
                    title={item.rel_path}
                  >
                    {item.filename}
                    {item.missing ? "（文件缺失）" : ""}
                  </span>
                </div>
                {manage ? (
                  pendingDelete === item.rel_path ? (
                    <div className="flex shrink-0 items-center gap-1.5">
                      <button disabled={busy} onClick={() => deleteFile(item)} className={btnPrimary}>
                        确定删
                      </button>
                      <button onClick={() => setPendingDelete(null)} className={btnGhost}>
                        取消
                      </button>
                    </div>
                  ) : (
                    <div className="flex shrink-0 items-center gap-2">
                      <button onClick={() => setPendingDelete(item.rel_path)} className={btnPrimary}>
                        <Trash2 size={14} /> 删除文件
                      </button>
                      <button disabled={busy} onClick={() => removeItems([item])} className={btnGhost}>
                        <FolderMinus size={14} /> 仅移出
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
                        className={btnGhost}
                      >
                        <Move size={14} /> 调整归属…
                      </button>
                    </div>
                  )
                ) : (
                  <button
                    disabled={item.missing}
                    onClick={() => playItem(item)}
                    className={`${item.is_watched ? btnGhost : btnPrimary} min-w-24`}
                  >
                    <Play size={14} fill="currentColor" />
                    {item.is_watched ? "再次播放" : "播放"}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        {standaloneLoading ? (
          <div className="text-xs text-muted">正在加载...</div>
        ) : groups.length === 0 ? (
          <div className="py-16 text-center text-xs text-muted">
            还没有独立展示的剧场版/OVA。下载剧场版会自动加入；也可在某部番的详情页里，把某一集设为独立剧场版/OVA。
          </div>
        ) : filteredGroups.length === 0 ? (
          <div className="py-16 text-center text-xs text-muted">当前筛选下没有内容。</div>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
            {filteredGroups.map((group) => {
              const head = group.items[0];
              const allMissing = group.items.every((item) => item.missing);
              const isActive = group.bgm_id === activeBgm;
              return (
                <div
                  key={group.bgm_id}
                  onMouseEnter={() => hoverCard(group.bgm_id)}
                  onClick={() => clickCard(group)}
                  className="group cursor-pointer"
                >
                  <div className={`${posterFrame} ring-2 ${isActive ? "ring-vermillion" : "ring-transparent"}`}>
                    {head.cover_url ? (
                      <img
                        src={proxiedImageUrl(head.cover_url)}
                        alt={head.title ?? head.filename}
                        loading="lazy"
                        decoding="async"
                        className={`h-full w-full object-cover ${allMissing ? "opacity-50" : ""}`}
                      />
                    ) : (
                      <div className="flex h-full w-full flex-col items-center justify-center gap-1 text-muted">
                        <Loader2 size={22} strokeWidth={1.5} className="animate-spin" />
                        <span className="text-xs">等待更新</span>
                      </div>
                    )}
                    {!manage && (
                      <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-scrim/30 opacity-0 transition-opacity group-hover:opacity-100">
                        <Play size={32} fill="currentColor" className="text-on-scrim" />
                      </div>
                    )}
                  </div>
                  <div className={`${posterTitle} ${isActive ? "text-vermillion" : "group-hover:text-vermillion"}`}>
                    {head.title || head.filename}
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
