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
import { cleanTitleFromFilename, compareSeasonNames, episodeLabel, isRegularSeasonBucket, sortCoverCandidates } from "./sort";
import { HERO_BANNER_HEIGHT, type AnimeDetail, type AnimeMeta, type CoverCandidate, type Episode, type ImageKind, type LibraryAnime, type RegroupTarget, type StandaloneItem } from "./types";
import { btnGhost, btnPrimary } from "./ui";

export default function LibraryDetailView({
  anime,
  detail,
  detailLoading,
  animeMeta,
  gridScrollTopRef,
  initialBodyScroll,
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
  gridScrollTopRef: RefObject<number>;
  initialBodyScroll: number;
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
  const bodyRef = useRef<HTMLDivElement>(null);
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

  // 分集区自己滚动。回来时把补番列表滚回离开前的位置，只做一次。
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    el.scrollTop = initialBodyScroll;
    // 只在进入这一页时定位，后面简介变高不要把用户再拽回去。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      relatedScrollTop: bodyRef.current?.scrollTop ?? 0,
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
    <div className="flex min-h-0 flex-1 flex-col">
      {/* relative 把头图的绝对定位层关在标题栏里，不然会盖住下面的分集。 */}
      <div className="relative shrink-0 overflow-hidden bg-ink">
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

      <div ref={bodyRef} className="min-h-0 flex-1 overflow-y-auto px-8 pb-8">
        {showRelated ? (
          relatedLoading ? (
            <div className="text-xs text-muted">正在查询相关作品...</div>
          ) : relatedError ? (
            <div className="text-xs text-vermillion">获取失败: {relatedError}</div>
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
          <div className="text-xs text-muted">正在读取硬盘文件结构...</div>
        ) : sortedSeasons.length > 0 ? (
          <div className="space-y-6">
            {sortedSeasons.map(([seasonName, episodes]) => (
              <div
                key={seasonName}
                ref={(el) => {
                  seasonRefs.current[seasonName] = el;
                }}
                style={{ scrollMarginTop: 16 }}
                className="rounded-lg border border-border bg-surface p-4"
              >
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border pb-1.5">
                  <h3 className="font-display text-lg text-vermillion">
                    {seasonName}
                    <span className="ml-2 text-xs font-normal text-muted">
                      已看 {episodes.filter((ep) => ep.is_watched).length}/{episodes.length}
                    </span>
                  </h3>
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
                        className={btnGhost}
                      >
                        <Move size={12} /> 调整归属…
                      </button>
                    </div>
                  )}
                </div>
                <div className="flex flex-col">
                  {[...episodes].reverse().map((ep) => {
                    const label = episodeLabel(ep.filename);
                    return (
                      <div
                        key={ep.rel_path}
                        className="flex items-center justify-between gap-3 rounded-md px-2 py-2 transition-colors hover:bg-paper/5"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-2">
                            <span className={`min-w-0 truncate text-sm ${ep.is_watched ? "text-muted line-through" : "text-paper"}`}>
                              {label ?? ep.filename}
                            </span>
                            {ep.is_watched && <CheckCircle2 size={14} className="shrink-0 text-vermillion" />}
                          </div>
                          {label && <div className="truncate font-mono text-[11px] text-muted">{ep.filename}</div>}
                          {ep.is_watched && ep.watched_at && (
                            <div className="font-mono text-[11px] text-muted">上次观看 {ep.watched_at}</div>
                          )}
                        </div>

                        {manageMode ? (
                          pendingDelete === ep.rel_path ? (
                            <div className="flex shrink-0 items-center gap-1.5">
                              <button disabled={deleting === ep.rel_path} onClick={() => deleteEpisode(ep)} className={btnPrimary}>
                                确定
                              </button>
                              <button onClick={() => setPendingDelete(null)} className={btnGhost}>
                                取消
                              </button>
                            </div>
                          ) : (
                            <button onClick={() => setPendingDelete(ep.rel_path)} className={`${btnPrimary} min-w-24`}>
                              <Trash2 size={14} />
                              删除
                            </button>
                          )
                        ) : (
                          <div className="flex shrink-0 items-center gap-2">
                            {!isRegularSeasonBucket(seasonName) &&
                              (addedRelPaths.has(ep.rel_path) ? (
                                <span className={`${btnGhost} pointer-events-none opacity-70`}>已添加到剧场版</span>
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
                                  className={btnGhost}
                                >
                                  添加到剧场版模式
                                </button>
                              ))}
                            <button
                              onClick={() => onPlay(ep)}
                              className={`${ep.is_watched ? btnGhost : btnPrimary} min-w-24`}
                            >
                              <Play size={14} fill="currentColor" />
                              {ep.is_watched ? "再次播放" : "播放"}
                            </button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="py-8 text-xs text-muted">该文件夹下未找到符合格式的视频文件。</div>
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
