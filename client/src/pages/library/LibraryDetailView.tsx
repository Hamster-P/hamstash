import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  FolderOpen,
  ImageIcon,
  Info,
  Move,
  Play,
  Settings2,
  Trash2,
  Type,
  Users,
} from "lucide-react";
import BangumiResultsList, { type BangumiSubject } from "../../components/BangumiResultsList";
import { proxiedImageUrl } from "../../utils/proxiedImage";
import { API_BASE } from "../../api";
import CoverPickerDialog from "./CoverPickerDialog";
import EntryPickerDialog, { type EntryPickerRequest } from "./EntryPickerDialog";
import ImagePickerDialog from "./ImagePickerDialog";
import { saveLibraryDetailSession } from "./session";
import { cleanTitleFromFilename, compareSeasonNames, isRegularSeasonBucket, sortCoverCandidates } from "./sort";
import { HERO_BANNER_HEIGHT, type AnimeDetail, type AnimeMeta, type CoverCandidate, type Episode, type ImageKind, type LibraryAnime, type RegroupTarget, type StandaloneItem } from "./types";

export default function LibraryDetailView({
  anime,
  detail,
  detailLoading,
  animeMeta,
  scrollContainerRef,
  gridScrollTopRef,
  initialShowRelated,
  initialRelated,
  onBack,
  onSelectAnime,
  onPlay,
  onEpisodeRemoved,
  onReloadMeta,
  onOpenRegroup,
}: {
  anime: LibraryAnime;
  detail: AnimeDetail | null;
  detailLoading: boolean;
  animeMeta: AnimeMeta | null;
  scrollContainerRef?: RefObject<HTMLElement | null>;
  gridScrollTopRef: RefObject<number>;
  initialShowRelated: boolean;
  initialRelated: BangumiSubject[];
  onBack: () => void;
  onSelectAnime?: (bgmId: number) => void;
  onPlay: (ep: Episode) => void;
  onEpisodeRemoved: (relPath: string) => void;
  onReloadMeta: () => void;
  onOpenRegroup: (target: RegroupTarget) => void;
}) {
  const [showRelated, setShowRelated] = useState(initialShowRelated);
  const [related, setRelated] = useState<BangumiSubject[]>(initialRelated);
  const [relatedLoading, setRelatedLoading] = useState(false);
  const [relatedError, setRelatedError] = useState<string | null>(null);
  const [manageMode, setManageMode] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [standalones, setStandalones] = useState<StandaloneItem[]>([]);
  const [entryRequest, setEntryRequest] = useState<EntryPickerRequest | null>(null);
  const [imageKind, setImageKind] = useState<ImageKind | null>(null);
  const [introOpen, setIntroOpen] = useState(false);
  const [introCandidates, setIntroCandidates] = useState<CoverCandidate[]>([]);
  const [introChecking, setIntroChecking] = useState(false);
  const headerRef = useRef<HTMLDivElement>(null);
  const [headerHeight, setHeaderHeight] = useState(0);
  const seasonRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // 只为了分集按钮知道哪些文件已经进了剧场版列表，不渲染剧场版页。
  const loadStandalones = () => {
    fetch(`${API_BASE}/library/standalone`)
      .then((res) => res.json())
      .then((data) => setStandalones(Array.isArray(data) ? data : []))
      .catch(() => setStandalones([]));
  };

  useEffect(() => {
    loadStandalones();
  }, [anime.folder_name]);

  const addedRelPaths = useMemo(() => new Set(standalones.map((item) => item.rel_path)), [standalones]);

  const sortedSeasons = detail
    ? Object.entries(detail.seasons).sort(([a], [b]) => compareSeasonNames(a, b, detail.bucket_dates))
    : [];
  const hasHeroBanner = animeMeta?.status === "resolved" && !!animeMeta.backdrop_url;

  // 头部高度会随简介和快捷按钮变。量出来留给分季块，跳转时才不会被吸顶头盖住。
  useLayoutEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const update = () => setHeaderHeight(el.offsetHeight);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [detail, showRelated]);

  const fetchRelated = (bgmId: number) => {
    setRelatedLoading(true);
    setRelatedError(null);
    fetch(`${API_BASE}/bangumi/related/${bgmId}`)
      .then((res) => res.json())
      .then((data) => {
        setRelated(data?.data ?? []);
        setRelatedLoading(false);
      })
      .catch((err: unknown) => {
        setRelatedError(err instanceof Error ? err.message : "获取相关作品失败");
        setRelatedLoading(false);
      });
  };

  const toggleRelated = () => {
    if (!showRelated) {
      setManageMode(false);
      setPendingDelete(null);
      if (related.length === 0 && anime.bgm_id) fetchRelated(anime.bgm_id);
    }
    setShowRelated((value) => !value);
  };

  const rememberSession = (mode: "related" | "self") => {
    saveLibraryDetailSession({
      anime,
      relatedAnime: related,
      gridScrollTop: gridScrollTopRef.current,
      relatedScrollTop: scrollContainerRef?.current?.scrollTop ?? 0,
      mode,
    });
  };

  const jumpToIntro = (bgmId: number) => {
    rememberSession("self");
    onSelectAnime?.(bgmId);
  };

  // 只有一季（或拉取失败）直接跳。多季才弹出选择。
  const openIntro = async () => {
    if (!anime.bgm_id || introChecking) return;
    setIntroChecking(true);
    try {
      const res = await fetch(`${API_BASE}/library/cover-candidates/${anime.bgm_id}`);
      const list: CoverCandidate[] = (await res.json())?.data ?? [];
      if (list.length <= 1) {
        jumpToIntro(list[0]?.bgm_id ?? anime.bgm_id);
        return;
      }
      setIntroCandidates(sortCoverCandidates(list));
      setIntroOpen(true);
    } catch {
      jumpToIntro(anime.bgm_id);
    } finally {
      setIntroChecking(false);
    }
  };

  const deleteEpisode = async (ep: Episode) => {
    setDeleting(ep.rel_path);
    setDeleteError(null);
    try {
      const res = await fetch(`${API_BASE}/library/episode/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel_path: ep.rel_path }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `HTTP ${res.status}`);
      }
      setPendingDelete(null);
      setStandalones((prev) => prev.filter((item) => item.rel_path !== ep.rel_path));
      onEpisodeRemoved(ep.rel_path);
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setDeleting(null);
    }
  };

  const currentImageUrl =
    imageKind === "backdrop" ? animeMeta?.backdrop_url ?? null : imageKind === "logo" ? animeMeta?.logo_url ?? null : null;

  return (
    <div>
      <div ref={headerRef} className="sticky top-0 z-10 overflow-hidden bg-ink">
        {/* 模糊底铺满头部。没有 TMDB 背景时用 Bangumi 封面，这是正常降级，不是报错。 */}
        <div className="absolute inset-0">
          {animeMeta?.status === "resolved" && animeMeta.backdrop_url ? (
            <img src={proxiedImageUrl(animeMeta.backdrop_url)} alt="" className="h-full w-full scale-125 object-cover blur-2xl" />
          ) : anime.cover_url ? (
            <img src={proxiedImageUrl(anime.cover_url)} alt="" className="h-full w-full scale-125 object-cover object-top blur-2xl" />
          ) : null}
          <div className="absolute inset-0 bg-gradient-to-b from-ink/10 via-ink/70 to-ink" />
        </div>

        {/* 真图保持比例靠右。窗口窄时 max-w-full 退回裁切，左缘渐隐进模糊底。 */}
        {animeMeta?.status === "resolved" && animeMeta.backdrop_url && (
          <div className="absolute inset-x-0 top-0 flex justify-end overflow-hidden" style={{ height: HERO_BANNER_HEIGHT }}>
            <img
              src={proxiedImageUrl(animeMeta.backdrop_url)}
              alt=""
              className="h-full w-auto max-w-full object-cover object-top opacity-80"
              style={{
                maskImage: "linear-gradient(to right, transparent, black 35%)",
                WebkitMaskImage: "linear-gradient(to right, transparent, black 35%)",
              }}
            />
            <div className="absolute inset-0 bg-gradient-to-t from-ink via-ink/70 to-ink/10" />
          </div>
        )}

        <div
          className="relative flex flex-col px-8 pb-4 pt-8"
          style={hasHeroBanner ? { minHeight: HERO_BANNER_HEIGHT } : undefined}
        >
          <div className="mb-6 flex flex-wrap items-center gap-2">
            <button
              onClick={onBack}
              className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
            >
              <ArrowLeft size={14} /> 返回影视库
            </button>
            {anime.bgm_id && (
              <button
                onClick={toggleRelated}
                title={showRelated ? "退出补番" : "补番"}
                className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs backdrop-blur transition-colors ${
                  showRelated
                    ? "border-vermillion bg-vermillion text-ink"
                    : "border-border bg-ink/60 text-muted hover:border-vermillion hover:text-vermillion"
                }`}
              >
                <Users size={14} /> {showRelated ? "退出补番" : "补番"}
              </button>
            )}
            {anime.bgm_id && (
              <button
                onClick={openIntro}
                title="Bangumi介绍"
                className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
              >
                <Info size={14} /> Bangumi介绍
              </button>
            )}
            {!showRelated && (
              <button
                onClick={() => {
                  setManageMode((value) => !value);
                  setPendingDelete(null);
                }}
                title={manageMode ? "结束管理" : "管理"}
                className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs backdrop-blur transition-colors ${
                  manageMode
                    ? "border-vermillion bg-vermillion text-ink"
                    : "border-border bg-ink/60 text-muted hover:border-vermillion hover:text-vermillion"
                }`}
              >
                <Settings2 size={14} /> {manageMode ? "结束管理" : "管理"}
              </button>
            )}
            {manageMode && anime.bgm_id && detail && (
              <button
                onClick={() =>
                  onOpenRegroup({
                    label: `整个「${anime.display_title || anime.folder_name}」`,
                    relPaths: Object.values(detail.seasons).flat().map((ep) => ep.rel_path),
                    bgmId: anime.bgm_id,
                  })
                }
                title="调整归属…"
                className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
              >
                <Move size={14} /> 调整归属…
              </button>
            )}
            {manageMode && anime.bgm_id && animeMeta?.status === "resolved" && (
              <>
                <button
                  onClick={() => setImageKind("backdrop")}
                  title="调整海报图片"
                  className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                >
                  <ImageIcon size={14} /> 调整海报图片
                </button>
                <button
                  onClick={() => setImageKind("logo")}
                  title="调整logo图片"
                  className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                >
                  <Type size={14} /> 调整logo图片
                </button>
              </>
            )}
            {deleteError && <span className="font-mono text-[11px] text-vermillion">删除失败: {deleteError}</span>}
          </div>

          {/* 有真图时，海报和简介在按钮行下方的剩余高度里居中。没有真图就按普通文档流排。 */}
          <div className={hasHeroBanner ? "flex flex-1 flex-col justify-center" : undefined}>
            <div className="flex flex-col gap-6 md:flex-row md:items-end">
              <div className="aspect-[2/3] w-40 shrink-0 overflow-hidden rounded-md border border-border bg-surface shadow-2xl">
                {anime.cover_url ? (
                  <img src={proxiedImageUrl(anime.cover_url)} alt={anime.folder_name} className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-muted">
                    <FolderOpen size={40} />
                  </div>
                )}
              </div>
              <div className="min-w-0 flex-1">
                {/* 固定高度槽位：文字标题换成 LOGO 时，下面的简介不会被顶一下。 */}
                <div className="mb-2 flex h-16 items-end">
                  {animeMeta?.status === "resolved" && animeMeta.logo_url ? (
                    <img
                      src={proxiedImageUrl(animeMeta.logo_url)}
                      alt={anime.display_title || anime.folder_name}
                      className="max-h-16 max-w-full object-contain object-left [filter:drop-shadow(0_1px_1px_rgba(0,0,0,0.55))_drop-shadow(0_0_7px_rgba(0,0,0,0.6))]"
                    />
                  ) : (
                    <h1 className="line-clamp-2 font-display text-2xl leading-tight tracking-tight drop-shadow">
                      {anime.display_title || anime.folder_name}
                    </h1>
                  )}
                </div>
                {animeMeta?.status === "resolved" &&
                  (animeMeta.content_rating || (animeMeta.genres?.length ?? 0) > 0) && (
                    <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-paper/90 drop-shadow">
                      {animeMeta.content_rating && (
                        <span className="rounded border border-border/80 bg-ink/40 px-1.5 py-0.5">{animeMeta.content_rating}</span>
                      )}
                      {(animeMeta.genres?.length ?? 0) > 0 && <span>{animeMeta.genres!.join(" / ")}</span>}
                    </div>
                  )}
                <p className="max-h-32 overflow-y-auto pr-1 text-sm text-paper/90 drop-shadow">{anime.summary}</p>
              </div>
            </div>

            {!showRelated && sortedSeasons.length > 1 && (
              <div className="flex flex-wrap gap-2 pt-4">
                {sortedSeasons.map(([seasonName]) => (
                  <button
                    key={seasonName}
                    onClick={() => seasonRefs.current[seasonName]?.scrollIntoView({ behavior: "smooth", block: "start" })}
                    className="rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                  >
                    {seasonName}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="px-8 pb-8">
        {showRelated ? (
          relatedLoading ? (
            <div className="font-mono text-xs text-muted">正在查询相关作品...</div>
          ) : relatedError ? (
            <div className="font-mono text-xs text-vermillion">获取失败: {relatedError}</div>
          ) : (
            <BangumiResultsList
              results={related}
              onSelect={(bgmId) => {
                rememberSession("related");
                onSelectAnime?.(bgmId);
              }}
              emptyText="没有找到相关的关联作品"
            />
          )
        ) : detailLoading ? (
          <div className="font-mono text-xs text-muted">正在读取硬盘文件结构...</div>
        ) : sortedSeasons.length > 0 ? (
          <div className="space-y-6">
            {sortedSeasons.map(([seasonName, episodes]) => (
              <div
                key={seasonName}
                ref={(el) => {
                  seasonRefs.current[seasonName] = el;
                }}
                style={{ scrollMarginTop: headerHeight + 16 }}
                className="rounded-lg border border-border bg-surface p-4"
              >
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border pb-1.5">
                  <h3 className="font-display text-lg text-vermillion">{seasonName}</h3>
                  {/* 每个桶都有归属入口。认不出是哪一部也给按钮，进弹窗再选。 */}
                  {manageMode && (
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
                      {detail?.season_owners?.[seasonName] && (
                        <span className="text-muted/70">{detail.season_owners[seasonName].name}</span>
                      )}
                      <button
                        onClick={() =>
                          onOpenRegroup({
                            label: seasonName,
                            relPaths: episodes.map((ep) => ep.rel_path),
                            bgmId: detail?.season_owners?.[seasonName]?.bgm_id ?? null,
                          })
                        }
                        className="flex items-center gap-1 rounded border border-border px-2 py-1 text-muted transition-colors hover:border-vermillion hover:text-vermillion"
                      >
                        <Move size={12} /> 调整归属…
                      </button>
                    </div>
                  )}
                </div>
                <div className="flex flex-col gap-2">
                  {[...episodes].reverse().map((ep) => (
                    <div
                      key={ep.rel_path}
                      className="group flex items-center justify-between gap-3 rounded border border-transparent p-3 transition-colors hover:border-border/50 hover:bg-paper/5"
                    >
                      <div className="flex min-w-0 flex-1 flex-col">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className={`min-w-0 flex-1 truncate font-mono text-sm ${ep.is_watched ? "text-muted line-through" : "text-paper"}`}>
                            {ep.filename}
                          </span>
                          {ep.is_watched && <CheckCircle2 size={14} className="shrink-0 text-green-500/80" />}
                        </div>
                        {ep.is_watched && ep.watched_at && (
                          <span className="mt-1 font-mono text-[10px] text-green-500/70">上次观看: {ep.watched_at}</span>
                        )}
                      </div>

                      {manageMode ? (
                        pendingDelete === ep.rel_path ? (
                          <div className="flex w-28 shrink-0 items-center justify-center gap-1.5 font-mono text-xs">
                            <button
                              disabled={deleting === ep.rel_path}
                              onClick={() => deleteEpisode(ep)}
                              className="rounded border border-vermillion bg-vermillion px-2 py-1 text-ink transition-colors hover:bg-vermillion/90 disabled:opacity-40"
                            >
                              确定
                            </button>
                            <button
                              onClick={() => setPendingDelete(null)}
                              className="rounded border border-border bg-surface px-2 py-1 text-muted transition-colors hover:text-paper"
                            >
                              取消
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setPendingDelete(ep.rel_path)}
                            className="flex w-28 shrink-0 items-center justify-center gap-1.5 rounded-md border border-vermillion bg-vermillion px-3 py-1.5 font-mono text-xs text-ink transition-colors hover:bg-vermillion/90"
                          >
                            <Trash2 size={14} />
                            删除
                          </button>
                        )
                      ) : (
                        <div className="flex shrink-0 items-center gap-2 font-mono text-xs">
                          {!isRegularSeasonBucket(seasonName) &&
                            (addedRelPaths.has(ep.rel_path) ? (
                              <span className="rounded-md border border-border px-3 py-1.5 text-muted/70">已添加到剧场版</span>
                            ) : (
                              <button
                                onClick={() =>
                                  setEntryRequest({
                                    mode: "add",
                                    libraryFolder: anime.folder_name,
                                    relPath: ep.rel_path,
                                    filename: ep.filename,
                                    keyword: cleanTitleFromFilename(ep.filename),
                                  })
                                }
                                title="把这一集作为独立剧场版/OVA 加入剧场版模式"
                                className="rounded-md border border-border px-3 py-1.5 text-muted transition-colors hover:border-vermillion hover:text-vermillion"
                              >
                                添加到剧场版模式
                              </button>
                            ))}
                          <button
                            onClick={() => onPlay(ep)}
                            className={`flex w-28 items-center justify-center gap-1.5 rounded-md border px-3 py-1.5 transition-colors ${
                              ep.is_watched
                                ? "border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion"
                                : "border-vermillion bg-vermillion text-ink hover:bg-vermillion/90"
                            }`}
                          >
                            <Play size={14} fill="currentColor" />
                            {ep.is_watched ? "再次播放" : "播放"}
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="py-8 font-mono text-xs text-muted">该文件夹下未找到符合格式的视频文件。</div>
        )}
      </div>

      {entryRequest && (
        <EntryPickerDialog request={entryRequest} onClose={() => setEntryRequest(null)} onSaved={loadStandalones} />
      )}
      {imageKind && anime.bgm_id && (
        <ImagePickerDialog
          kind={imageKind}
          bgmId={anime.bgm_id}
          scope="library"
          currentUrl={currentImageUrl}
          onClose={() => setImageKind(null)}
          onSaved={onReloadMeta}
        />
      )}
      {introOpen && (
        <CoverPickerDialog
          mode="intro"
          folderName={null}
          bgmId={null}
          introCandidates={introCandidates}
          onClose={() => setIntroOpen(false)}
          onIntroPick={jumpToIntro}
        />
      )}
    </div>
  );
}
