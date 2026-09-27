import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { FolderOpen, Loader2, RefreshCcw, Settings2 } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import type { BangumiSubject } from "../components/BangumiResultsList";
import { proxiedImageUrl } from "../utils/proxiedImage";
import { API_BASE } from "../api";
import CoverPickerDialog from "./library/CoverPickerDialog";
import LibraryDetailView from "./library/LibraryDetailView";
import { loadAnimeMeta } from "./library/meta";
import { parseLibraryPath, videoPath } from "./library/paths";
import { beginPlay, endPlay, openPlayer, postWatch } from "./library/playback";
import RegroupDialog from "./library/RegroupDialog";
import { takeLibraryDetailSession } from "./library/session";
import { useLibrarySettings } from "./library/settings";
import { sortAnimes } from "./library/sort";
import type { AnimeDetail, AnimeMeta, Episode, LibraryAnime, RegroupTarget, SortMode } from "./library/types";

interface LibraryPageProps {
  onSelectAnime?: (bgmId: number) => void;
  onManualMatch?: (folderName: string) => void;
  // 带滚动条的 <main>。列表和详情共用它，切换时保存/恢复网格滚动位置。
  scrollContainerRef?: RefObject<HTMLElement | null>;
}

export default function LibraryPage({ onSelectAnime, onManualMatch, scrollContainerRef }: LibraryPageProps) {
  const settings = useLibrarySettings();
  // 从介绍页回来时，第一次渲染就进详情，避免先闪一下网格再跳进去。
  const [restored] = useState(() => takeLibraryDetailSession());
  const [animes, setAnimes] = useState<LibraryAnime[]>([]);
  const [selectedAnime, setSelectedAnime] = useState<LibraryAnime | null>(restored?.anime ?? null);
  const [detail, setDetail] = useState<AnimeDetail | null>(null);
  const [animeMeta, setAnimeMeta] = useState<AnimeMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(!!restored);
  const [sort, setSort] = useState<SortMode>("default");
  const [matchMode, setMatchMode] = useState(false);
  const [pendingDeleteFolder, setPendingDeleteFolder] = useState<string | null>(null);
  const [deletingFolder, setDeletingFolder] = useState<string | null>(null);
  const [deleteAnimeError, setDeleteAnimeError] = useState<string | null>(null);
  const [coverPicker, setCoverPicker] = useState<{ folderName: string; bgmId: number } | null>(null);
  const [regroupTarget, setRegroupTarget] = useState<RegroupTarget | null>(null);
  const [regroupNotice, setRegroupNotice] = useState<string | null>(null);
  const [relatedSeed, setRelatedSeed] = useState<{ show: boolean; items: BangumiSubject[] }>({
    show: !!restored && restored.mode !== "self",
    items: restored && restored.mode !== "self" ? restored.relatedAnime : [],
  });

  const playLock = useRef(false);
  const gridScrollTop = useRef(restored?.gridScrollTop ?? 0);
  const restoringRelatedRef = useRef(!!restored);
  const restoreScrollTop = useRef(restored?.relatedScrollTop ?? 0);
  const watchedInDetailRef = useRef(false);
  const detailRef = useRef<AnimeDetail | null>(null);
  const metaTicket = useRef(0);
  const coverRetryRef = useRef(0);
  detailRef.current = detail;

  const displayedAnimes = useMemo(() => sortAnimes(animes, sort), [animes, sort]);

  const fetchAnimes = (silent = false) => {
    if (!silent) setLoading(true);
    fetch(`${API_BASE}/library/animes`)
      .then((res) => res.json())
      .then((data) => {
        setAnimes(Array.isArray(data) ? data : []);
        if (!silent) setLoading(false);
      })
      .catch(() => {
        if (!silent) setLoading(false);
      });
  };

  const reloadMeta = (bgmId: number) => {
    const ticket = ++metaTicket.current;
    setAnimeMeta(null);
    loadAnimeMeta(bgmId, "library").then((data) => {
      if (metaTicket.current === ticket) setAnimeMeta(data);
    });
  };

  useEffect(() => {
    fetchAnimes();
    fetch(`${API_BASE}/library/scan`)
      .then(() => {
        fetchAnimes(true);
        // 扫盘里的未看集数补课是后台任务，这一次列表多半还是旧数字。3 秒后再补拉一次。
        setTimeout(() => fetchAnimes(true), 3000);
      })
      .catch(() => {});

    if (restored) {
      if (restored.anime.bgm_id) reloadMeta(restored.anime.bgm_id);
      fetch(`${API_BASE}/library/detail/${encodeURIComponent(restored.anime.folder_name)}`)
        .then((res) => res.json())
        .then((data) => {
          setDetail(data);
          setDetailLoading(false);
        })
        .catch(() => setDetailLoading(false));
    }

    fetch(`${API_BASE}/library/sort-mode`)
      .then((res) => res.json())
      .then((data) => {
        if (data?.mode) setSort(data.mode as SortMode);
      })
      .catch(() => {});
    // 挂载时拉一次即可。fetchAnimes / reloadMeta 用的是当次渲染里的 setState。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 已匹配但封面还没到的卡片，每 4 秒静默刷新，大约 6 次后停。封面可能本来就没有。
  useEffect(() => {
    const pending = animes.some((anime) => anime.bgm_id && !anime.cover_url);
    if (!pending) {
      coverRetryRef.current = 0;
      return;
    }
    if (coverRetryRef.current >= 6) return;
    const timer = setTimeout(() => {
      coverRetryRef.current += 1;
      fetchAnimes(true);
    }, 4000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animes]);

  useLayoutEffect(() => {
    const el = scrollContainerRef?.current;
    if (!el) return;
    if (selectedAnime) {
      if (restoringRelatedRef.current) {
        el.scrollTop = restoreScrollTop.current;
        restoringRelatedRef.current = false;
      } else {
        el.scrollTop = 0;
      }
    } else {
      el.scrollTop = gridScrollTop.current;
    }
  }, [selectedAnime, scrollContainerRef]);

  const applyWatchedLocally = (folderName: string, filename: string) => {
    watchedInDetailRef.current = true;
    const nowStr = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
    const current = detailRef.current;
    // 用 ref 读当前详情。播放器连播的监听器不会跟着详情重新订阅，闭包里的 detail 是旧的。
    // 标记后立刻写回 ref，同一次播放里紧接着的「开始播放」事件不会把角标再减一次。
    const wasUnwatchedRealEp =
      !!current &&
      current.folder_name === folderName &&
      Object.entries(current.seasons).some(
        ([season, eps]) =>
          !/^other$|^specials\/others$/i.test(season) && eps.some((ep) => ep.filename === filename && !ep.is_watched),
      );
    const markWatchedSeasons = (prev: AnimeDetail): AnimeDetail => {
      const newSeasons = { ...prev.seasons };
      for (const season in newSeasons) {
        newSeasons[season] = newSeasons[season].map((item) =>
          item.filename === filename ? { ...item, is_watched: true, watched_at: nowStr } : item,
        );
      }
      return { ...prev, seasons: newSeasons };
    };
    if (current && current.folder_name === folderName) {
      const next = markWatchedSeasons(current);
      detailRef.current = next;
      setDetail(next);
    } else {
      setDetail((prev) => {
        if (!prev || prev.folder_name !== folderName) return prev;
        const next = markWatchedSeasons(prev);
        detailRef.current = next;
        return next;
      });
    }
    const nowIso = new Date().toISOString();
    setAnimes((prev) =>
      prev.map((anime) =>
        anime.folder_name === folderName
          ? {
              ...anime,
              last_watched_at: nowIso,
              unwatched_count: wasUnwatchedRealEp ? Math.max(anime.unwatched_count - 1, 0) : anime.unwatched_count,
            }
          : anime,
      ),
    );
  };

  const applyRef = useRef(applyWatchedLocally);
  applyRef.current = applyWatchedLocally;

  useEffect(() => {
    const unlistens: Array<() => void> = [];
    let cancelled = false;
    const onEpisodeStarted = (event: { payload: { path: string } }) => {
      const parsed = parseLibraryPath(event.payload.path, settings.library_root);
      if (!parsed) return;
      applyRef.current(parsed.folderName, parsed.filename);
    };
    for (const eventName of ["mpv-episode-started", "external-episode-started"]) {
      listen<{ path: string }>(eventName, onEpisodeStarted).then((fn) => {
        if (cancelled) fn();
        else unlistens.push(fn);
      });
    }
    return () => {
      cancelled = true;
      unlistens.forEach((fn) => fn());
    };
  }, [settings.library_root]);

  const openDetail = (anime: LibraryAnime) => {
    gridScrollTop.current = scrollContainerRef?.current?.scrollTop ?? 0;
    watchedInDetailRef.current = false;
    setRelatedSeed({ show: false, items: [] });
    setSelectedAnime(anime);
    setDetailLoading(true);
    setDetail(null);
    if (anime.bgm_id) reloadMeta(anime.bgm_id);
    else setAnimeMeta(null);
    fetch(`${API_BASE}/library/detail/${encodeURIComponent(anime.folder_name)}`)
      .then((res) => res.json())
      .then((data) => {
        setDetail(data);
        setDetailLoading(false);
      })
      .catch(() => setDetailLoading(false));
  };

  const handleBack = () => {
    setSelectedAnime(null);
    setDetail(null);
    setAnimeMeta(null);
    if (watchedInDetailRef.current || animes.some((anime) => anime.bgm_id != null && !anime.cover_url)) {
      fetchAnimes(true);
    }
    watchedInDetailRef.current = false;
  };

  const deleteAnime = async (folderName: string) => {
    setDeletingFolder(folderName);
    setDeleteAnimeError(null);
    try {
      const res = await fetch(`${API_BASE}/library/animes/${encodeURIComponent(folderName)}`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `HTTP ${res.status}`);
      }
      setPendingDeleteFolder(null);
      fetchAnimes();
    } catch (err) {
      setDeleteAnimeError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setDeletingFolder(null);
    }
  };

  const playEpisode = async (ep: Episode) => {
    if (!selectedAnime || !beginPlay(playLock)) return;
    try {
      postWatch(selectedAnime.folder_name, ep.filename, ep.rel_path);
      applyWatchedLocally(selectedAnime.folder_name, ep.filename);
      if (settings.player_mode === "builtin") {
        const remaining = remainingEpisodes(detail, ep);
        await openPlayer(
          settings,
          remaining.map((item) => videoPath(settings.library_root, item.rel_path)),
        );
      } else {
        await openPlayer(settings, [videoPath(settings.library_root, ep.rel_path)]);
      }
    } finally {
      endPlay(playLock);
    }
  };

  const changeSort = (next: SortMode) => {
    setSort(next);
    fetch(`${API_BASE}/library/sort-mode`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: next }),
    }).catch((err: unknown) => console.error("保存排序方式失败", err));
  };

  return (
    <div className="text-paper">
      {selectedAnime ? (
        <LibraryDetailView
          anime={selectedAnime}
          detail={detail}
          detailLoading={detailLoading}
          animeMeta={animeMeta}
          scrollContainerRef={scrollContainerRef}
          gridScrollTopRef={gridScrollTop}
          initialShowRelated={relatedSeed.show}
          initialRelated={relatedSeed.items}
          onBack={handleBack}
          onSelectAnime={onSelectAnime}
          onPlay={playEpisode}
          onEpisodeRemoved={(relPath) => {
            setDetail((prev) => {
              if (!prev) return prev;
              const seasons: Record<string, Episode[]> = {};
              for (const [season, episodes] of Object.entries(prev.seasons)) {
                const remaining = episodes.filter((ep) => ep.rel_path !== relPath);
                if (remaining.length > 0) seasons[season] = remaining;
              }
              return { ...prev, seasons };
            });
          }}
          onReloadMeta={() => {
            if (selectedAnime.bgm_id) reloadMeta(selectedAnime.bgm_id);
          }}
          onOpenRegroup={(target) => {
            setRegroupNotice(null);
            setRegroupTarget(target);
          }}
        />
      ) : (
        <div>
          <div className="sticky top-0 z-20 overflow-hidden bg-ink px-8 pb-4 pt-8">
            <div className="mb-6 flex items-center justify-between">
              <div>
                <h1 className="font-display text-2xl tracking-tight">影视库</h1>
                <p className="mt-1 font-mono text-xs text-muted">
                  关联目录：{settings.library_root} | 发现 {animes.length} 部动画
                </p>
              </div>
              <div className="flex items-center gap-2">
                <div className="flex overflow-hidden rounded-md border border-border font-mono text-xs">
                  {(
                    [
                      { value: "default", label: "默认" },
                      { value: "recent_watched", label: "最近观看" },
                      { value: "recent_updated", label: "最新更新" },
                    ] as { value: SortMode; label: string }[]
                  ).map((opt) => (
                    <button
                      key={opt.value}
                      onClick={() => changeSort(opt.value)}
                      className={`px-3 py-1.5 transition-colors ${
                        sort === opt.value
                          ? "bg-vermillion text-ink"
                          : "bg-surface text-muted hover:bg-surface-hover hover:text-paper"
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                {matchMode && (
                  <button
                    onClick={() => {
                      setLoading(true);
                      fetch(`${API_BASE}/library/scan`)
                        .then(() => fetchAnimes())
                        .catch(() => setLoading(false));
                    }}
                    className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
                  >
                    <RefreshCcw size={14} /> 刷新 & 扫盘
                  </button>
                )}
                <button
                  onClick={() => {
                    setMatchMode((value) => !value);
                    setPendingDeleteFolder(null);
                  }}
                  className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                    matchMode
                      ? "border-vermillion bg-vermillion text-ink"
                      : "border-border text-muted hover:border-vermillion hover:text-vermillion"
                  }`}
                >
                  <Settings2 size={14} /> {matchMode ? "结束管理" : "管理"}
                </button>
              </div>
            </div>
            {deleteAnimeError && (
              <div className="mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
                删除失败: {deleteAnimeError}
              </div>
            )}
            {regroupNotice && (
              <div className="mb-4 flex items-start justify-between gap-3 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-[11px] text-vermillion">
                <span>{regroupNotice}</span>
                <button onClick={() => setRegroupNotice(null)} className="shrink-0 text-muted transition-colors hover:text-paper">
                  知道了
                </button>
              </div>
            )}
          </div>

          {/* 角标用负偏移骑在卡片右上角，第一排需要这点上边距才不会被裁掉。 */}
          <div className="px-8 pb-8 pt-3">
            {loading ? (
              <div className="font-mono text-xs text-muted">正在加载...</div>
            ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
                {displayedAnimes.map((anime) => (
                  <div key={anime.id} onClick={() => openDetail(anime)} className="group relative cursor-pointer">
                    <div className="relative aspect-[2/3] overflow-hidden rounded-md bg-surface shadow-md">
                      {anime.cover_url ? (
                        <img
                          src={proxiedImageUrl(anime.cover_url)}
                          alt={anime.folder_name}
                          className="h-full w-full object-cover transition-transform group-hover:scale-105"
                        />
                      ) : anime.bgm_id ? (
                        <div className="flex h-full w-full flex-col items-center justify-center gap-2 border border-border bg-surface text-muted">
                          <Loader2 size={28} strokeWidth={1.5} className="animate-spin" />
                          <span className="font-mono text-[10px]">等待更新</span>
                        </div>
                      ) : (
                        <div className="flex h-full w-full flex-col items-center justify-center gap-2 border border-border bg-surface text-muted">
                          <FolderOpen size={32} strokeWidth={1.5} />
                          <span className="font-mono text-[10px]">No Cover</span>
                        </div>
                      )}
                      {(matchMode || !anime.bgm_id) && (
                        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink/60">
                          {matchMode && anime.bgm_id && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setCoverPicker({ folderName: anime.folder_name, bgmId: anime.bgm_id! });
                              }}
                              className="rounded border border-border bg-surface/90 px-2 py-1 font-mono text-[10px] text-paper transition-colors hover:border-vermillion hover:text-vermillion"
                            >
                              选择图片
                            </button>
                          )}
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              onManualMatch?.(anime.folder_name);
                            }}
                            className="rounded border border-vermillion bg-vermillion/80 px-2 py-1 font-mono text-[10px] text-ink transition-colors hover:bg-vermillion"
                          >
                            {anime.bgm_id ? "重新匹配" : "指定动漫"}
                          </button>
                          {matchMode &&
                            (pendingDeleteFolder === anime.folder_name ? (
                              <div className="flex items-center gap-1.5 font-mono text-[10px]" onClick={(e) => e.stopPropagation()}>
                                <button
                                  disabled={deletingFolder === anime.folder_name}
                                  onClick={() => deleteAnime(anime.folder_name)}
                                  className="rounded border border-vermillion bg-vermillion px-2 py-1 text-ink transition-colors hover:bg-vermillion/90 disabled:opacity-40"
                                >
                                  确定删除
                                </button>
                                <button
                                  onClick={() => setPendingDeleteFolder(null)}
                                  className="rounded border border-border bg-surface px-2 py-1 text-muted transition-colors hover:text-paper"
                                >
                                  取消
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setPendingDeleteFolder(anime.folder_name);
                                }}
                                className="rounded border border-vermillion bg-vermillion px-2 py-1 font-mono text-[10px] text-ink transition-colors hover:bg-vermillion/90"
                              >
                                删除
                              </button>
                            ))}
                        </div>
                      )}
                    </div>
                    {settings.library_unwatched_badge_enabled && anime.unwatched_count > 0 && (
                      <div className="absolute -right-2 -top-2 flex h-7 min-w-7 items-center justify-center rounded-full bg-vermillion/80 px-1.5 font-mono text-xs font-bold text-ink shadow-lg ring-2 ring-surface backdrop-blur-sm">
                        {anime.unwatched_count > 99 ? "99+" : anime.unwatched_count}
                      </div>
                    )}
                    <div className="mt-2 line-clamp-2 text-sm font-medium leading-snug transition-colors group-hover:text-vermillion">
                      {anime.display_title || anime.folder_name}
                    </div>
                  </div>
                ))}
                {animes.length === 0 && (
                  <div className="col-span-full py-16 text-center font-mono text-xs text-muted">
                    {settings.library_root} 下没有发现任何子目录，请先往该目录下载动画。
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {coverPicker && (
        <CoverPickerDialog
          mode="cover"
          folderName={coverPicker.folderName}
          bgmId={coverPicker.bgmId}
          introCandidates={[]}
          onClose={() => setCoverPicker(null)}
          onCoverApplied={() => fetchAnimes(true)}
        />
      )}
      {regroupTarget && (
        <RegroupDialog
          target={regroupTarget}
          fallbackBgmId={selectedAnime?.bgm_id ?? null}
          animes={animes}
          onClose={() => setRegroupTarget(null)}
          onCompleted={(notice) => {
            setRegroupNotice(notice);
            setRegroupTarget(null);
            // 文件可能已经不在当前文件夹里，退回网格再拉列表。
            setSelectedAnime(null);
            setDetail(null);
            setAnimeMeta(null);
            fetchAnimes();
          }}
        />
      )}
    </div>
  );
}

function remainingEpisodes(detail: AnimeDetail | null, ep: Episode): Episode[] {
  if (!detail) return [ep];
  for (const episodes of Object.values(detail.seasons)) {
    const index = episodes.findIndex((item) => item.filename === ep.filename);
    if (index !== -1) return episodes.slice(index);
  }
  return [ep];
}
