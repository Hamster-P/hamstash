// pages/LibraryPage.tsx
import { useEffect, useLayoutEffect, useMemo, useState, useRef } from "react";
import { Play, FolderOpen, ArrowLeft, CheckCircle2, Trash2, Loader2, Users, Info, Settings2, Move, RefreshCcw, FolderMinus } from "lucide-react";
import { invoke } from '@tauri-apps/api/core';
import { isTauri } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { proxiedImageUrl } from "../utils/proxiedImage";
import BangumiResultsList, { type BangumiSubject } from "../components/BangumiResultsList";

interface LibraryAnime {
  id: number;
  folder_name: string;
  display_title: string;
  bgm_id: number | null;
  cover_url: string | null;
  summary: string;
  latest_activity_at: string | null;
  last_watched_at: string | null;
  unwatched_count: number; // 未看集数角标;后端开关关闭或该文件夹还没扫过集数时恒为0
}

type SortMode = "default" | "recent_watched" | "recent_updated";

// 「归属」弹窗的数据源:这个条目所在 Bangumi 家族的全部成员 + 当前生效的归属
interface RegroupCandidates {
  bgm_id: number;
  auto_root: { bgm_id: number; title: string };
  members: {
    bgm_id: number;
    title: string;
    cover_url: string | null;
    platform: string | null;
    season_ordinal: string | null;
    folder_bucket: string | null;
    is_auto_root: boolean;
  }[];
  current_root: number;
  is_overridden: boolean;
}

// "选择图片"弹窗里的一个家族封面候选
interface CoverCandidate {
  bgm_id: number;
  title: string;
  cover_url: string;
}

// 常规 TV 季桶(Season 01/02...):这些是正片,不给"添加到剧场版模式"按钮。
// Season 00 / 剧场版 / OVA / Other / Specials/Others 都不算常规季 → 显示按钮。
function isRegularSeasonBucket(name: string): boolean {
  const m = /^season\s*(\d+)$/i.exec(name.trim());
  return m ? parseInt(m[1], 10) >= 1 : false;
}

// 剧场版模式:一行独立剧场版/OVA(逐文件),前端按 bgm_id 分组成卡
interface StandaloneItem {
  id: number;
  library_folder: string;
  rel_path: string;
  filename: string;
  bgm_id: number;
  media_type: string | null;
  title: string | null;
  cover_url: string | null;
  summary: string | null;
  is_watched: boolean;
  watched_at: string | null;
  missing: boolean;
}

interface Episode {
  filename: string;
  rel_path: string;
  is_watched?: boolean;
  watched_at?: string;
}

interface AnimeDetail {
  folder_name: string;
  seasons: Record<string, Episode[]>;
  // 季度桶 -> 家族里对应的那一部(拆分/合并归属用它当主键)。
  // 只有"一个桶唯一对应一部"时后端才给,剧场版/OVA 这类多部共用的桶不给。
  season_owners?: Record<string, { bgm_id: number; name: string }>;
  // 桶 -> 该桶对应作品的首播日期。给排序用:按作品名命名的目录(Z高达/雷霆宙域这类
  // 算不出季号的旁支)桶名没有固定模式,靠这个既能认出它们、又能按时间先后排。
  bucket_dates?: Record<string, string>;
}

// 详情页头部背景图/LOGO/分级等元数据(GET /anime-meta/{bgm_id})。status非resolved时
// (pending/unresolved_retry/unresolved_permanent)前端一律走同一套降级态渲染,
// 不区分展示——对用户来说"还没查到"和"查不到"没有区别,都是"暂时没有这些"。
interface AnimeMeta {
  bgm_id: number;
  status: "pending" | "resolved" | "unresolved_retry" | "unresolved_permanent";
  tmdb_id?: number | null;
  backdrop_url?: string | null;
  logo_url?: string | null;
  content_rating?: string | null;
  genres?: string[];
  tags?: string[];
  studios?: string[];
  creators?: string[];
}

// 定义设置项的类型
interface AppSettings {
  library_root: string;
  potplayer_path: string;
  player_mode: "builtin" | "external";
  library_unwatched_badge_enabled: boolean;
}

interface LibraryPageProps {
  onSelectAnime?: (bgmId: number) => void;
  onManualMatch?: (folderName: string) => void;
  // 带滚动条的 <main>(见 App.tsx),用来在列表↔详情内部切换时保存/恢复网格滚动位置
  scrollContainerRef?: React.RefObject<HTMLElement | null>;
  // true=这是独立的"剧场版"页面(侧边栏单独入口):只显示剧场版模式,不扫盘、无系列网格
  movieOnly?: boolean;
}

// 从整理后的文件名里提取标题部分作为默认检索词:去扩展名 + 去 [字幕组]/【】/() 标签 + 折叠空白。
// 例:"名侦探柯南 独眼的残像 [SBSUB][1080P].mp4" -> "名侦探柯南 独眼的残像"
function cleanTitleFromFilename(filename: string): string {
  return filename
    .replace(/\.[^.]+$/, "")
    .replace(/[[【(（][^\]】)）]*[\]】)）]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const API_BASE = "http://127.0.0.1:8080";

// 从"补番一览"点某部关联作品会跳到顶层 DetailPage(见 App.tsx 的 selectedBgmId 分支),
// 整个 LibraryPage 被卸载,组件内 state / useRef 全部销毁。要在 DetailPage「返回」后
// 还原到补番一览,必须把详情态存到组件外——对齐 SearchPage 的 sessionStorage 做法。
const LIBRARY_DETAIL_SESSION_KEY = "library_detail_session_v1";

interface LibraryDetailSession {
  anime: LibraryAnime;            // 当时停留的那部番(补番一览的"原番")
  relatedAnime: BangumiSubject[]; // 已拉到的关联作品列表,免得返回后重新请求
  gridScrollTop: number;          // 更外层:网格的滚动位置,之后点「返回影视库」要用
  relatedScrollTop: number;       // 离开前详情页内容区的滚动位置(补番一览或普通详情态皆用这个字段)
  mode: "related" | "self";       // related=恢复到补番一览,self=恢复到这部番自己的详情页(Bangumi介绍用)
}

function loadLibraryDetailSession(): LibraryDetailSession | null {
  try {
    const raw = sessionStorage.getItem(LIBRARY_DETAIL_SESSION_KEY);
    return raw ? (JSON.parse(raw) as LibraryDetailSession) : null;
  } catch {
    return null;
  }
}

function saveLibraryDetailSession(s: LibraryDetailSession): void {
  try {
    sessionStorage.setItem(LIBRARY_DETAIL_SESSION_KEY, JSON.stringify(s));
  } catch {
    /* 隐私模式 / 配额满:存不了就算了,退化成回到网格根 */
  }
}

function clearLibraryDetailSession(): void {
  try {
    sessionStorage.removeItem(LIBRARY_DETAIL_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

// 分季排序:TV季数从新到旧排最前,然后是"算不出季号、按作品名建的目录"(Z高达/雷霆宙域
// 这类旁支,见后端 rename_engine.work_title_bucket),再剧场版、OVA,Other/无法识别的
// 兜底桶排最后。(对应后端 rename_engine.py / routers/library.py 实际会产出的分类桶名)
//
// 作品名目录的桶名是按Bangumi作品名动态生成的,认不出固定模式,靠后端一起返回的
// bucket_dates(家族成员首播日期)来识别并按时间先后排——旁支之间按时间读起来最自然。
const TIER_SEASON = 0;
const TIER_WORK_TITLE = 1;
const TIER_MOVIE = 2;
const TIER_OVA = 3;
const TIER_OTHER = 4;

function seasonSortKey(name: string, bucketDates?: Record<string, string>): [number, number, string] {
  const seasonMatch = name.match(/^season\s*(\d+)/i);
  if (seasonMatch) {
    const num = parseInt(seasonMatch[1], 10);
    // "Season 00" 是老库里"算不出季号"的兜底桶(新内容改走作品名目录),跟OVA放一起
    if (num === 0) return [TIER_OVA, 0, ""];
    return [TIER_SEASON, -num, ""]; // 数字取负,配合升序排序实现"季数越大越靠前"
  }
  if (/剧场版|劇場版|movie/i.test(name)) return [TIER_MOVIE, 0, ""];
  if (/\bova\b/i.test(name)) return [TIER_OVA, 0, ""];
  // 剩下的:能在家族成员里查到首播日期的,就是作品名目录;查不到才是真的兜底桶。
  const date = bucketDates?.[name];
  if (date) return [TIER_WORK_TITLE, 0, date];
  return [TIER_OTHER, 0, ""];
}

function compareSeasonNames(a: string, b: string, bucketDates?: Record<string, string>): number {
  const [tierA, subA, dateA] = seasonSortKey(a, bucketDates);
  const [tierB, subB, dateB] = seasonSortKey(b, bucketDates);
  if (tierA !== tierB) return tierA - tierB;
  if (subA !== subB) return subA - subB;
  if (dateA !== dateB) return dateA < dateB ? -1 : 1;
  return a.localeCompare(b, undefined, { numeric: true });
}

// 三种排序模式全部依据/library/animes接口本来就返回的字段(latest_activity_at/
// last_watched_at),纯前端本地排序即可——不需要为了换排序方式重新请求后端
// (后端list_library_animes现在是纯读接口,不再自己排序,见routers/library.py)。
// "default"保留接口返回的原始顺序,不参与这里的比较。
function sortAnimes(list: LibraryAnime[], mode: SortMode): LibraryAnime[] {
  if (mode === "default") return list;
  const key: keyof LibraryAnime = mode === "recent_watched" ? "last_watched_at" : "latest_activity_at";
  return [...list].sort((a, b) => {
    const aTime = a[key] ? new Date(a[key] as string).getTime() : 0;
    const bTime = b[key] ? new Date(b[key] as string).getTime() : 0;
    return bTime - aTime;
  });
}

// 从mpv上报的完整路径反推出 folder_name/filename,不依赖"当前正在看哪部番"这种UI状态
// ——mpv连播是后台进行的,用户可能已经切走详情页,甚至切到别的番。
function parseLibraryPath(
  fullPath: string,
  libraryRoot: string,
): { folderName: string; filename: string; relPath: string } | null {
  const normalizedRoot = libraryRoot.replace(/[/\\]$/, "").replace(/\//g, "\\").toLowerCase();
  const normalizedFull = fullPath.replace(/\//g, "\\");
  if (!normalizedFull.toLowerCase().startsWith(normalizedRoot)) return null;
  const rel = normalizedFull.slice(normalizedRoot.length).replace(/^\\/, "");
  const parts = rel.split("\\");
  if (parts.length < 2) return null;
  return {
    folderName: parts[0],
    filename: parts[parts.length - 1],
    relPath: parts.join("/"), // 相对 library_root 的正斜杠路径,给后端角标增量判断桶用
  };
}

export default function LibraryPage({ onSelectAnime, onManualMatch, scrollContainerRef, movieOnly = false }: LibraryPageProps) {
  const [animes, setAnimes] = useState<LibraryAnime[]>([]);
  const [selectedAnime, setSelectedAnime] = useState<LibraryAnime | null>(null);
  const [detail, setDetail] = useState<AnimeDetail | null>(null);
  // 详情页头部背景图/LOGO等,跟detail(季度结构)并列、互不影响,请求失败/还没解析出
  // 结果时保持null——渲染层按"没有animeMeta或status非resolved"统一走降级态。
  const [animeMeta, setAnimeMeta] = useState<AnimeMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [sort, setSort] = useState<SortMode>("default");
  // 管理模式总开关:打开后所有卡片(不止未匹配的)都显示重新匹配按钮,
  // 同时额外露出删除入口(整部番删除,见handleDeleteAnime)
  const [matchMode, setMatchMode] = useState(false);
  // 列表页删除整部番:二次确认态存的是正在确认哪个folder_name,取消/成功后清空
  const [pendingDeleteFolder, setPendingDeleteFolder] = useState<string | null>(null);
  const [deletingFolder, setDeletingFolder] = useState<string | null>(null);
  const [deleteAnimeError, setDeleteAnimeError] = useState<string | null>(null);
  // 详情页管理模式:打开后每一集的播放按钮变成删除按钮
  const [episodeManageMode, setEpisodeManageMode] = useState(false);
  const [pendingDeleteEpisode, setPendingDeleteEpisode] = useState<string | null>(null);
  const [deletingEpisode, setDeletingEpisode] = useState<string | null>(null);
  const [episodeDeleteError, setEpisodeDeleteError] = useState<string | null>(null);
  // "补番"入口:拉这部番所在Bangumi系列的全部关联作品(续集/前传/剧场版/OVA等),
  // 跟episodeManageMode互斥,复用SearchPage.tsx抽出来的BangumiResultsList渲染
  const [showRelatedAnime, setShowRelatedAnime] = useState(false);
  const [relatedAnime, setRelatedAnime] = useState<BangumiSubject[]>([]);
  const [relatedLoading, setRelatedLoading] = useState(false);
  const [relatedError, setRelatedError] = useState<string | null>(null);

  // "选择图片"弹窗:从家族全部作品里挑一张作为该番封面。coverPickerFolder非空=弹窗打开。
  const [coverPickerFolder, setCoverPickerFolder] = useState<string | null>(null);
  const [coverCandidates, setCoverCandidates] = useState<CoverCandidate[]>([]);
  const [coverLoading, setCoverLoading] = useState(false);
  const [coverBusy, setCoverBusy] = useState(false);

  // 剧场版模式独立成 movieOnly 页面(见 movieOnly prop),不再用 viewMode toggle。
  const [standalones, setStandalones] = useState<StandaloneItem[]>([]);
  const [standaloneLoading, setStandaloneLoading] = useState(false);
  const [movieManage, setMovieManage] = useState(false); // 剧场版模式管理态
  const [activeBgm, setActiveBgm] = useState<number | null>(null); // hover 聚焦条目(只驱动上方 hero)
  const [expandedBgm, setExpandedBgm] = useState<number | null>(null); // 点击展开的条目(驱动多条明细行);hover 到别的卡自动收起
  const [movieWatchFilter, setMovieWatchFilter] = useState<"all" | "unwatched" | "watched">("all");
  const [pendingMovieDelete, setPendingMovieDelete] = useState<string | null>(null); // 删文件二次确认(rel_path)
  const [movieBusy, setMovieBusy] = useState(false);
  // 选条目弹窗(手动追加 / 重选条目共用):复用 BangumiResultsList
  const [pickerMode, setPickerMode] = useState<"add" | "regroup" | null>(null);
  const [pickerAddCtx, setPickerAddCtx] = useState<{ library_folder: string; rel_path: string; filename: string } | null>(null);
  const [pickerRegroupIds, setPickerRegroupIds] = useState<number[]>([]);
  const [pickerKeyword, setPickerKeyword] = useState("");
  const [pickerResults, setPickerResults] = useState<BangumiSubject[]>([]);
  const [pickerLoading, setPickerLoading] = useState(false);

  const isPlayingRef = useRef(false); // 新增：播放锁
  // 进详情视图那一刻网格的滚动位置,返回列表时恢复。列表/详情共用 <main> 这一个滚动条,
  // 内部切换不卸载组件,所以用 ref 存即可,不需要 sessionStorage。
  const gridScrollTop = useRef(0);
  // 从 sessionStorage 还原"补番一览"时置真:让下面那个把 scrollTop 归零的 layout-effect
  // 改用保存的补番一览滚动位置,只生效一次。
  const restoringRelatedRef = useRef(false);
  const restoreScrollTop = useRef(0);
  // 这次进详情页期间标记过"已看"——返回网格时静默重拉一次列表,让角标反映后端增量结果
  const watchedInDetailRef = useRef(false);
  // 吸顶头部(封面+简介+分季快捷按钮)的实际高度,用来给每个分季区块留出滚动余量,
  // 避免点快捷按钮跳转后,区块顶部被吸顶头部盖住
  const headerRef = useRef<HTMLDivElement>(null);
  const [headerHeight, setHeaderHeight] = useState(0);
  const seasonRefs = useRef<Record<string, HTMLDivElement | null>>({});
  // 新增：全局设置状态，赋予默认值防崩溃
  const [settings, setSettings] = useState<AppSettings>({
    library_root: "D:\\AnimeLibrary",
    potplayer_path: "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayer64.exe",
    player_mode: "external",
    library_unwatched_badge_enabled: true,
  });

  // 组件挂载时:先拉取设置,再"先秒开列表、后台扫盘",同时恢复上次记住的排序方式。
  // 进页面不再阻塞等扫盘(scandir/stat 在网络共享媒体库下会卡)——先 fetchAnimes 从 DB
  // 直接把列表渲染出来,扫盘丢后台跑,跑完再静默(silent,不闪"正在加载")刷新一次。
  useEffect(() => {
    fetchSettings(); // 播放路径要用,两种页面都要
    // 剧场版页(movieOnly):不扫盘、不拉系列列表(那些由上面的 movieOnly effect 拉 standalone)。
    if (movieOnly) return;
    fetchAnimes();
    fetch(`${API_BASE}/library/scan`)
      .then(() => {
        fetchAnimes(true);
        // /library/scan 的"未看集数"补课是后端 background task,上面这次 fetchAnimes
        // 多半比它先返回、拿到的还是旧值——3 秒后再静默补一次,兜住这个窗口。
        setTimeout(() => fetchAnimes(true), 3000);
      })
      .catch(() => {});

    // 从 DetailPage 返回:还原"补番一览"(原番 + 关联作品列表 + 滚动位置)。
    // 一次性——用完即清,之后正常进出详情不受影响。
    const snap = loadLibraryDetailSession();
    if (snap) {
      clearLibraryDetailSession();
      restoringRelatedRef.current = true;
      restoreScrollTop.current = snap.relatedScrollTop;
      gridScrollTop.current = snap.gridScrollTop; // 之后点「返回影视库」回到网格原位
      setSelectedAnime(snap.anime);
      if (snap.anime.bgm_id) fetchAnimeMeta(snap.anime.bgm_id);
      if (snap.mode === "self") {
        setShowRelatedAnime(false);
        setRelatedAnime([]);
      } else {
        setShowRelatedAnime(true);
        setRelatedAnime(snap.relatedAnime);
      }
      setDetailLoading(true);
      fetch(`${API_BASE}/library/detail/${encodeURIComponent(snap.anime.folder_name)}`)
        .then((res) => res.json())
        .then((data) => {
          setDetail(data);
          setDetailLoading(false);
        })
        .catch(() => setDetailLoading(false)); // 番被删/改名:补番一览仍可看,退出补番时分集为空
    }
    fetch(`${API_BASE}/library/sort-mode`)
      .then((res) => res.json())
      .then((data) => {
        if (data?.mode) setSort(data.mode as SortMode);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 从后端或本地获取设置
  const fetchSettings = () => {
    // 假设你后端有一个提供 config 的接口，请根据实际情况修改 URL
    fetch(`${API_BASE}/settings`)
      .then((res) => {
        if (!res.ok) throw new Error("设置接口未就绪");
        return res.json();
      })
      .then((data) => {
        setSettings({
          library_root: data.library_root || settings.library_root,
          potplayer_path: data.potplayer_path || settings.potplayer_path,
          player_mode: data.player_mode === "builtin" ? "builtin" : "external",
          library_unwatched_badge_enabled: data.library_unwatched_badge_enabled !== false,
        });
      })
      .catch((err: any) => {
        console.warn("无法从后端获取配置，尝试使用 localStorage 兜底", err);
        // 如果后端接口没写好，可以先作为过渡从 localStorage 拿
        const localRoot = localStorage.getItem("library_root");
        const localPlayer = localStorage.getItem("potplayer_path");
        if (localRoot || localPlayer) {
          setSettings(prev => ({
            ...prev,
            library_root: localRoot || prev.library_root,
            potplayer_path: localPlayer || prev.potplayer_path
          }));
        }
      });
  };

  // 纯拉列表,不触发扫盘——后端list_library_animes现在是纯读接口(见
  // routers/library.py),真正的扫盘只由scanAndFetchAnimes显式触发。
  // silent=true:后台静默刷新(比如挂载时扫盘完成后的二次拉取),不翻 loading,
  // 避免已经渲染出来的网格闪一下"正在加载"。
  const fetchAnimes = (silent = false) => {
    if (!silent) setLoading(true);
    fetch(`${API_BASE}/library/animes`)
      .then((res) => res.json())
      .then((data) => {
        setAnimes(data);
        if (!silent) setLoading(false);
      })
      .catch(() => {
        if (!silent) setLoading(false);
      });
  };

  // 打开"选择图片"弹窗:拉该番所在家族的封面候选(后端缓存优先,缺失才远程补)。
  const openCoverPicker = (folderName: string, bgmId: number) => {
    setCoverPickerFolder(folderName);
    setCoverCandidates([]);
    setCoverLoading(true);
    fetch(`${API_BASE}/library/cover-candidates/${bgmId}`)
      .then((res) => res.json())
      .then((data) => setCoverCandidates(data?.data ?? []))
      .catch(() => setCoverCandidates([]))
      .finally(() => setCoverLoading(false));
  };

  const closeCoverPicker = () => {
    setCoverPickerFolder(null);
    setCoverCandidates([]);
  };

  // 选中某张家族封面 → 标记为该番自定义封面;或"恢复默认"(bgmId=null)清掉自定义。
  const applyCover = async (bgmId: number | null) => {
    if (!coverPickerFolder) return;
    setCoverBusy(true);
    try {
      const url = `${API_BASE}/library/${encodeURIComponent(coverPickerFolder)}/cover`;
      const res =
        bgmId === null
          ? await fetch(url, { method: "DELETE" })
          : await fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ bgm_id: bgmId }),
            });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      closeCoverPicker();
      fetchAnimes(true); // 静默刷新,封面立即更新
    } catch (err) {
      console.error("设置封面失败", err);
    } finally {
      setCoverBusy(false);
    }
  };

  // 有"等待更新"封面的卡片(已匹配但cover_url还没补上)时,后台每隔几秒静默刷新一次
  // 列表,直到全部补全或达到重试上限——封面可能本来就取不到图(Bangumi没图/联网失败),
  // 不能无限轮询,达到上限就停手,保留"等待更新"占位。补全或没有待补时把计数归零,
  // 让之后新匹配的番能重新开始这套等待刷新。
  const coverRetryRef = useRef(0);
  useEffect(() => {
    const pending = animes.some((a) => a.bgm_id && !a.cover_url);
    if (!pending) {
      coverRetryRef.current = 0;
      return;
    }
    if (coverRetryRef.current >= 6) return; // 约6次(~24秒)后停手
    const timer = setTimeout(() => {
      coverRetryRef.current += 1;
      fetchAnimes(true);
    }, 4000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animes]);

  // ---- 剧场版模式 ----
  const fetchStandalones = (silent = false) => {
    if (!silent) setStandaloneLoading(true);
    fetch(`${API_BASE}/library/standalone`)
      .then((res) => res.json())
      .then((data) => setStandalones(Array.isArray(data) ? data : []))
      .catch(() => setStandalones([]))
      .finally(() => { if (!silent) setStandaloneLoading(false); });
  };

  // 剧场版页(movieOnly)挂载时拉一次列表。系列列表这里本来不需要(不扫盘、无网格),
  // 但管理态下的"移到其他系列"要拿它当候选,所以静默拉一份——silent=true不会触发
  // 扫盘,只读已有记录。
  useEffect(() => {
    if (movieOnly) {
      fetchStandalones();
      fetchAnimes(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [movieOnly]);

  // 进某部番详情时,静默拉一次 standalone 列表——好让分集里"添加到剧场版模式"按钮
  // 知道哪些集已经加过(显示"已添加到剧场版")。
  useEffect(() => {
    if (selectedAnime) fetchStandalones(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAnime]);

  // 已加入剧场版模式的文件 rel_path 集合(给分集按钮判断状态用)
  const addedRelPaths = useMemo(() => new Set(standalones.map((s) => s.rel_path)), [standalones]);

  // 按 bgm_id 分组成卡:一部剧场版/OVA 一张卡(可含多集)
  const movieGroups = useMemo(() => {
    const m = new Map<number, StandaloneItem[]>();
    for (const s of standalones) {
      const arr = m.get(s.bgm_id) ?? [];
      arr.push(s);
      m.set(s.bgm_id, arr);
    }
    return Array.from(m.entries()).map(([bgm_id, items]) => ({ bgm_id, items }));
  }, [standalones]);

  // 观看态筛选:整组各集都看过才算"已看"
  const filteredGroups = useMemo(() => {
    if (movieWatchFilter === "all") return movieGroups;
    return movieGroups.filter((g) => {
      const allWatched = g.items.every((i) => i.is_watched);
      return movieWatchFilter === "watched" ? allWatched : !allWatched;
    });
  }, [movieGroups, movieWatchFilter]);

  // 当前聚焦条目下的各集(hero/明细行据此显示)
  const activeItems = useMemo(
    () => (activeBgm === null ? [] : standalones.filter((s) => s.bgm_id === activeBgm)),
    [activeBgm, standalones],
  );
  const activeHead = activeItems[0];
  // 点击展开的那部的各集(多条明细行)
  const expandedItems = useMemo(
    () => (expandedBgm === null ? [] : standalones.filter((s) => s.bgm_id === expandedBgm)),
    [expandedBgm, standalones],
  );

  // 默认/纠偏聚焦:activeBgm 为空或已不在当前筛选结果里 → 落到第一张;carouselStart 越界归零;
  // 展开的条目若已不在筛选结果里 → 收起明细行。
  useEffect(() => {
    if (!movieOnly) return;
    if (!filteredGroups.some((g) => g.bgm_id === activeBgm)) {
      setActiveBgm(filteredGroups[0]?.bgm_id ?? null);
    }
    if (expandedBgm !== null && !filteredGroups.some((g) => g.bgm_id === expandedBgm)) {
      setExpandedBgm(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredGroups, movieOnly]);

  // 剧场版hero的背景/LOGO跟着activeBgm(鼠标hover切换)走,复用跟详情页头部同一套
  // animeMeta/fetchAnimeMeta——两者是同一组件实例里互斥的两个视图分支(movieOnly页
  // 不会进selectedAnime详情态),不会互相冲突。hover划过多张卡会连续触发activeBgm变化,
  // 加个200ms防抖,划过路上的卡不用每张都发请求。
  useEffect(() => {
    if (!movieOnly) return;
    setAnimeMeta(null);
    const bgmId = activeHead?.bgm_id;
    if (!bgmId) return;
    const timer = setTimeout(() => fetchAnimeMeta(bgmId), 200);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [movieOnly, activeHead?.bgm_id]);

  // 播放一个独立剧场版/OVA 文件(单文件,不做整季连播)
  const handlePlayStandalone = async (item: StandaloneItem) => {
    if (isPlayingRef.current) return;
    isPlayingRef.current = true;
    try {
      markWatched(item.library_folder, item.filename, item.rel_path);
      const cleanRoot = settings.library_root.replace(/[/\\]$/, "");
      const localVideoPath = `${cleanRoot}\\${item.rel_path.replace(/\//g, "\\")}`;
      if (await isTauri()) {
        if (settings.player_mode === "builtin") {
          await invoke("open_builtin_player", { videoPaths: [localVideoPath] }).catch((err: any) =>
            console.error("拉起内置播放器失败:", err));
        } else {
          await invoke("open_external_player", {
            videoPath: localVideoPath, playerPath: settings.potplayer_path,
          }).catch((err: any) => console.error("唤起播放器失败:", err));
        }
      } else {
        console.log("[开发调试] 模拟播放剧场版:", localVideoPath);
      }
      // 乐观更新本地已看态
      setStandalones((prev) =>
        prev.map((s) => (s.rel_path === item.rel_path ? { ...s, is_watched: true } : s)),
      );
    } finally {
      setTimeout(() => { isPlayingRef.current = false; }, 1000);
    }
  };

  // 删文件(复用删集接口,后端级联清表行);删完刷新列表
  const handleDeleteStandaloneFile = async (item: StandaloneItem) => {
    setMovieBusy(true);
    try {
      const res = await fetch(`${API_BASE}/library/episode/delete`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel_path: item.rel_path }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStandalones((prev) => prev.filter((s) => s.rel_path !== item.rel_path));
    } catch (err) {
      console.error("删除剧场版文件失败", err);
    } finally {
      setMovieBusy(false);
      setPendingMovieDelete(null);
    }
  };

  // 仅移出列表(保留文件)
  const handleRemoveStandalone = async (item: StandaloneItem) => {
    setMovieBusy(true);
    try {
      const res = await fetch(`${API_BASE}/library/standalone/${item.id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStandalones((prev) => prev.filter((s) => s.id !== item.id));
    } catch (err) {
      console.error("移出剧场版列表失败", err);
    } finally {
      setMovieBusy(false);
    }
  };

  // hover 卡:只更新上方 hero;若之前展开的是别的卡,自动收起明细行
  const handleMovieCardHover = (bgm: number) => {
    setActiveBgm(bgm);
    setExpandedBgm((cur) => (cur !== null && cur !== bgm ? null : cur));
  };

  // 点卡:管理态点=展开明细(删/移出);非管理态,单文件直接播、多文件点击才展开明细行
  const handleMovieCardClick = (g: { bgm_id: number; items: StandaloneItem[] }) => {
    if (movieManage) { setExpandedBgm(g.bgm_id); return; }
    if (g.items.length === 1) handlePlayStandalone(g.items[0]);
    else setExpandedBgm(g.bgm_id);
  };

  // ---- 选条目弹窗(手动追加 / 重选条目共用) ----
  const openPickerForAdd = (ep: Episode) => {
    if (!selectedAnime) return;
    setPickerMode("add");
    setPickerAddCtx({ library_folder: selectedAnime.folder_name, rel_path: ep.rel_path, filename: ep.filename });
    // 默认检索词用这一行文件名的标题部分(去字幕组/画质标签),而非根目录名,降低选错概率
    setPickerKeyword(cleanTitleFromFilename(ep.filename));
    setPickerResults([]);
  };
  const openPickerForRegroup = (items: StandaloneItem[]) => {
    setPickerMode("regroup");
    setPickerRegroupIds(items.map((i) => i.id));
    setPickerKeyword(items[0]?.title ?? "");
    setPickerResults([]);
  };
  const closePicker = () => {
    setPickerMode(null);
    setPickerAddCtx(null);
    setPickerRegroupIds([]);
    setPickerResults([]);
    setPickerKeyword("");
  };
  const runPickerSearch = () => {
    const kw = pickerKeyword.trim();
    if (!kw) return;
    setPickerLoading(true);
    fetch(`${API_BASE}/bangumi/search?keyword=${encodeURIComponent(kw)}`)
      .then((res) => res.json())
      .then((data) => setPickerResults(data?.data ?? []))
      .catch(() => setPickerResults([]))
      .finally(() => setPickerLoading(false));
  };
  const pickerSelect = async (bgmId: number) => {
    setMovieBusy(true);
    try {
      if (pickerMode === "add" && pickerAddCtx) {
        const res = await fetch(`${API_BASE}/library/standalone`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...pickerAddCtx, bgm_id: bgmId, media_type: null }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      } else if (pickerMode === "regroup") {
        const res = await fetch(`${API_BASE}/library/standalone/regroup`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: pickerRegroupIds, bgm_id: bgmId }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      }
      closePicker();
      // 刷新 standalone 列表:剧场版模式网格更新,同时分集里的"添加"按钮翻成"已添加"。
      fetchStandalones(true);
    } catch (err) {
      console.error("设置独立剧场版/OVA 失败", err);
    } finally {
      setMovieBusy(false);
    }
  };

  // 唯一会触发完整扫盘的入口:先GET /library/scan(遍历硬盘、刷新mtime),
  // 再拉一遍列表——挂载时和"刷新 & 扫盘"按钮用这个,其余场景(比如切换排序)
  // 不需要重新问硬盘要一遍数据。
  const scanAndFetchAnimes = () => {
    setLoading(true);
    fetch(`${API_BASE}/library/scan`)
      .then(() => fetchAnimes())
      .catch(() => setLoading(false));
  };

  // 切换排序方式纯本地重排已经拿到手的数据(见sortAnimes/displayedAnimes),
  // 不重新请求后端,不触发扫盘——这是本次要修的"切排序按钮卡顿"的核心改动。
  // 同时把选择持久化到后端(DB+INI),下次打开页面能恢复上次的选择。
  const handleSortChange = (sortValue: SortMode) => {
    setSort(sortValue);
    fetch(`${API_BASE}/library/sort-mode`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: sortValue }),
    }).catch((err: any) => console.error("保存排序方式失败", err));
  };

  // 详情页头部背景图/LOGO等,跟季度结构(detail)分开单独请求,互不阻塞。
  // 查无记录时后端会自己丢一个后台解析任务、先回status=pending,这里不重试轮询,
  // 用户下次重新进这部番的详情页(或过一阵子)自然就有数据了。
  const fetchAnimeMeta = (bgmId: number) => {
    fetch(`${API_BASE}/anime-meta/${bgmId}`)
      .then((res) => res.json())
      .then((data) => setAnimeMeta(data))
      .catch(() => setAnimeMeta(null));
  };

  // 点击某部动漫后，获取具体季、集数据
  const handleSelectAnime = (anime: LibraryAnime) => {
    // 先同步记下当前网格滚动量,返回列表时恢复
    gridScrollTop.current = scrollContainerRef?.current?.scrollTop ?? 0;
    watchedInDetailRef.current = false;
    setSelectedAnime(anime);
    setDetailLoading(true);
    // 管理模式/删除确认态/补番列表都是详情页局部状态,不能带着上一部番的状态进新一部的详情页
    setEpisodeManageMode(false);
    setPendingDeleteEpisode(null);
    setEpisodeDeleteError(null);
    setShowRelatedAnime(false);
    setRelatedAnime([]);
    setRelatedError(null);
    setAnimeMeta(null);
    if (anime.bgm_id) fetchAnimeMeta(anime.bgm_id);
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
    setEpisodeManageMode(false);
    setPendingDeleteEpisode(null);
    setEpisodeDeleteError(null);
    setShowRelatedAnime(false);
    setRelatedAnime([]);
    setRelatedError(null);

    // 返回列表时静默重拉一次的两种情况:
    // 1) 这次在详情页标记过已看 → 让网格角标落到后端刚增量好的准确值(乐观 -1 已先顶上)
    // 2) 存在"已匹配bgm但封面还没补上"的番(后台补全中)→ 把封面补上
    if (
      watchedInDetailRef.current ||
      animes.some((a) => a.bgm_id != null && !a.cover_url)
    ) {
      fetchAnimes(true);
    }
    watchedInDetailRef.current = false;
  };

  // 删除整部番:磁盘文件夹+LocalMedia记录一起删,播放记录保留。
  // 成功后重新拉一遍列表(本来就要重新过一遍scan_and_update_library),不做本地摘除。
  const handleDeleteAnime = async (folderName: string) => {
    setDeletingFolder(folderName);
    setDeleteAnimeError(null);
    try {
      const res = await fetch(`${API_BASE}/library/animes/${encodeURIComponent(folderName)}`, {
        method: "DELETE",
      });
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

  // 删除单集:成功后直接在本地detail状态里摘掉这一集,不用重新扫一遍硬盘目录结构;
  // 如果某个季度删完变空,连同这个季度键一起摘掉,不渲染空的季度区块。
  const handleDeleteEpisode = async (ep: Episode) => {
    setDeletingEpisode(ep.rel_path);
    setEpisodeDeleteError(null);
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
      setPendingDeleteEpisode(null);
      // 后端删文件时已级联删掉剧场版表里该 rel_path 的行(见 /library/episode/delete),
      // 这里同步本地 standalones,让"已添加到剧场版"状态即时更新。
      setStandalones((prev) => prev.filter((s) => s.rel_path !== ep.rel_path));
      setDetail((prev) => {
        if (!prev) return prev;
        const newSeasons: Record<string, Episode[]> = {};
        for (const [season, episodes] of Object.entries(prev.seasons)) {
          const remaining = episodes.filter((e) => e.rel_path !== ep.rel_path);
          if (remaining.length > 0) newSeasons[season] = remaining;
        }
        return { ...prev, seasons: newSeasons };
      });
    } catch (err) {
      setEpisodeDeleteError(err instanceof Error ? err.message : "删除失败");
    } finally {
      setDeletingEpisode(null);
    }
  };

  // 拉这部番所在Bangumi系列的全部关联作品(后端用resolve_root_subject_id找根节点
  // +resolve_family_season_map枚举整个家族,见server/routers/search.py::get_related_anime)
  const fetchRelatedAnime = (bgmId: number) => {
    setRelatedLoading(true);
    setRelatedError(null);
    fetch(`${API_BASE}/bangumi/related/${bgmId}`)
      .then((res) => res.json())
      .then((data) => {
        setRelatedAnime(data?.data ?? []);
        setRelatedLoading(false);
      })
      .catch((err: any) => {
        setRelatedError(err instanceof Error ? err.message : "获取相关作品失败");
        setRelatedLoading(false);
      });
  };

  // "Bangumi介绍"按钮:跳到顶层DetailPage看这部番自己的Bangumi简介/内嵌详情页
  // (跟"补番一览点关联作品"跳的是同一个onSelectAnime,同一套session机制,
  // 只是mode="self"——回来时恢复到这部番自己的详情页,而不是补番一览)。
  const handleOpenBangumiIntro = () => {
    if (!selectedAnime?.bgm_id) return;
    saveLibraryDetailSession({
      anime: selectedAnime,
      relatedAnime,
      gridScrollTop: gridScrollTop.current,
      relatedScrollTop: scrollContainerRef?.current?.scrollTop ?? 0,
      mode: "self",
    });
    onSelectAnime?.(selectedAnime.bgm_id);
  };

  // "补番"按钮:跟管理模式互斥,第一次打开且还没拉过数据时才发请求
  const handleToggleRelatedAnime = () => {
    if (!showRelatedAnime) {
      setEpisodeManageMode(false);
      setPendingDeleteEpisode(null);
      if (relatedAnime.length === 0 && selectedAnime?.bgm_id) {
        fetchRelatedAnime(selectedAnime.bgm_id);
      }
    }
    setShowRelatedAnime((v) => !v);
  };

  // 列表↔详情内部切换时管理 <main> 的滚动位置:进详情从顶部开始,返回列表恢复原位置。
  // 返回时 animes/displayedAnimes 仍在 state 里(handleBack 不清空、不阻塞重载),
  // 网格高度已就绪,scrollTop 恢复能生效。
  useLayoutEffect(() => {
    const el = scrollContainerRef?.current;
    if (!el) return;
    if (selectedAnime) {
      if (restoringRelatedRef.current) {
        // 从 DetailPage 返回补番一览:恢复一览的滚动位置,只此一次
        el.scrollTop = restoreScrollTop.current;
        restoringRelatedRef.current = false;
      } else {
        el.scrollTop = 0;
      }
    } else {
      el.scrollTop = gridScrollTop.current;
    }
  }, [selectedAnime]);

  // 头部内容(简介行数、快捷按钮是否显示)会变,量出来的高度也要跟着更新
  useLayoutEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const update = () => setHeaderHeight(el.offsetHeight);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [selectedAnime, detail]);

  const scrollToSeason = (seasonName: string) => {
    seasonRefs.current[seasonName]?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  };

  // 乐观刷新本地状态(详情页勾选 + 网格角标),不等真的播完——点开/切到这一集就算。
  // folderName来自调用方各自的上下文(选中的番,或者从mpv/PotPlayer上报路径反推出来的),
  // 不用同一个"当前选中番"假设,因为连播时用户可能已经切走界面。
  // 纯本地状态更新,不发请求——写库这件事现在由 Rust 后台层(lib.rs::report_episode_started)
  // 在检测到切集的那一刻直接完成,不依赖这个页面是否还挂载着,自然也不该重复发一遍。
  const applyWatchedLocally = (folderName: string, filename: string) => {
    // 返回网格时静默重拉一次,让角标落到后端刚增量好的准确值(见 handleBack)
    watchedInDetailRef.current = true;

    const nowStr = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
    // 这一集之前在详情里是不是"没看过"、且落在正片桶(非 Other/Specials/Others)——
    // 用来决定要不要乐观把网格角标 -1。从当前 detail 快照直接判,不依赖 setState 的时序。
    const wasUnwatchedRealEp =
      !!detail &&
      detail.folder_name === folderName &&
      Object.entries(detail.seasons).some(
        ([season, eps]) =>
          !/^other$|^specials\/others$/i.test(season) &&
          eps.some((e) => e.filename === filename && !e.is_watched),
      );
    setDetail((prev) => {
      if (!prev || prev.folder_name !== folderName) return prev;
      const newSeasons = { ...prev.seasons };
      for (const season in newSeasons) {
        newSeasons[season] = newSeasons[season].map((item) =>
          item.filename === filename
            ? { ...item, is_watched: true, watched_at: nowStr }
            : item
        );
      }
      return { ...prev, seasons: newSeasons };
    });

    // 同步乐观更新列表数据里这部番的last_watched_at,让"最近观看"排序在返回列表时
    // 立即重排——不重新请求后端(避免加载闪烁/与上面watch写入的竞态)。这里必须用
    // 可被new Date()解析的ISO时间戳(sortAnimes用new Date(last_watched_at).getTime()),
    // 不能用上面那个本地化显示串nowStr。
    const nowIso = new Date().toISOString();
    setAnimes((prev) =>
      prev.map((a) =>
        a.folder_name === folderName
          ? {
              ...a,
              last_watched_at: nowIso,
              // 乐观 -1,即时反馈;后端 backfill 稍后会校准成准确值
              unwatched_count: wasUnwatchedRealEp
                ? Math.max(a.unwatched_count - 1, 0)
                : a.unwatched_count,
            }
          : a,
      ),
    );
  };

  // 用户手动点开某一集播放:这一下是前端唯一权威来源(播放器这时候还没启动,
  // Rust 后台无从得知),既要发请求写库、也要立即刷新本地画面。
  // 自动连播切到的后续集数走的是下面的 onEpisodeStarted 监听器,不经过这里
  // ——那部分写库已经由 Rust 后台直接做了,监听器只调 applyWatchedLocally 刷画面。
  const markWatched = (folderName: string, filename: string, relPath?: string) => {
    fetch(`${API_BASE}/library/watch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_name: folderName, filename, rel_path: relPath ?? null }),
    }).catch((err: any) => console.error("标记进度失败", err));
    applyWatchedLocally(folderName, filename);
  };

  // ---- 归属弹窗 ----
  // 一个弹窗三个入口(整个文件夹 / 某个季度桶 / 剧场版模式里的单个文件),
  // 区别只在"作用于哪些文件"和"这批文件是哪一部能不能自动确定"。
  // 关键:按钮永远都有,自动确定不了就在弹窗里让用户自己选——绑死在"能自动识别"
  // 上正是之前拆出去就合并不回来的原因(拆出去之后 season_owners 必然为空)。
  interface RegroupTarget {
    label: string;        // 弹窗标题里显示的作用对象
    relPaths: string[];   // 要搬的文件
    bgmId: number | null; // 已能确定的归属主体;null=要用户在弹窗里选
  }
  const [regroupTarget, setRegroupTarget] = useState<RegroupTarget | null>(null);
  const [regroupCandidates, setRegroupCandidates] = useState<RegroupCandidates | null>(null);
  const [regroupLoading, setRegroupLoading] = useState(false);
  // 用户在弹窗里选中的"这批文件是哪一部"
  const [pickedBgmId, setPickedBgmId] = useState<number | null>(null);

  const openRegroupDialog = (target: RegroupTarget) => {
    setRegroupTarget(target);
    setPickedBgmId(target.bgmId);
    setRegroupCandidates(null);
    setRegroupNotice(null);
    // 候选按"哪个条目"拉:已确定就用它,没确定就用当前文件夹的 bgm_id 当入口——
    // 后端走的是 Bangumi 客观家族(不受用户覆盖影响),两者都能列出完整家族。
    const probe = target.bgmId ?? selectedAnime?.bgm_id ?? null;
    if (!probe) return;
    setRegroupLoading(true);
    fetch(`${API_BASE}/library/regroup/candidates/${probe}`)
      .then((r) => r.json())
      .then((d: RegroupCandidates) => {
        setRegroupCandidates(d);
        // 没预选时,如果家族里只有一个成员,直接替用户选上
        setPickedBgmId((prev) => prev ?? (d.members.length === 1 ? d.members[0].bgm_id : null));
      })
      .catch(() => setRegroupCandidates(null))
      .finally(() => setRegroupLoading(false));
  };

  const closeRegroupDialog = () => {
    setRegroupTarget(null);
    setRegroupCandidates(null);
    setPickedBgmId(null);
  };

  // ---- 归属调整:拆成单独一部 / 合并到另一部 ----
  // 后端只换顶层文件夹、不重排季度和文件名(那是"修复媒体库"的职责),
  // 所以成功后提示用户去跑一次修复,避免文件名里还留着旧归属的标题/季号。
  const [regrouping, setRegrouping] = useState<string | null>(null);
  const [regroupNotice, setRegroupNotice] = useState<string | null>(null);

  const runRegroup = async (
    key: string,
    bgmId: number,
    targetRootBgmId: number | null,
    relPaths: string[],
    restoreAuto = false,
  ) => {
    if (relPaths.length === 0) return;
    setRegrouping(key);
    setRegroupNotice(null);
    try {
      const res = await fetch(`${API_BASE}/library/regroup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bgm_id: bgmId,
          target_root_bgm_id: targetRootBgmId,
          rel_paths: relPaths,
          restore_auto: restoreAuto,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setRegroupNotice(data.detail ?? `操作失败(HTTP ${res.status})`);
        return;
      }
      const parts = [`已移动 ${data.moved.length} 个文件到「${data.target_folder}」`];
      if (data.skipped?.length) parts.push(`跳过 ${data.skipped.length} 个`);
      if (data.failed?.length) parts.push(`失败 ${data.failed.length} 个`);
      // 后端搬完会顺手按新归属重排名字/季度目录,不再需要用户手动跑「修复媒体库」
      const renamed = data.renamed;
      if (renamed?.succeeded?.length) parts.push(`并已重排 ${renamed.succeeded.length} 个文件的名字`);
      if (renamed?.failed?.length) {
        parts.push(`${renamed.failed.length} 个文件重排失败,可到设置页手动跑一次「修复媒体库」`);
      }
      setRegroupNotice(parts.join("，"));
      closeRegroupDialog();
      // 目录结构变了,两个列表都要刷新。剧场版页(movieOnly)没有详情态,
      // 只刷登记表即可,不要把它踢回一个它本来就不在的系列网格。
      fetchStandalones(true);
      if (!movieOnly) {
        // 详情页停留的文件夹可能已经被搬空删掉了,退回列表重新扫描
        setSelectedAnime(null);
        setDetail(null);
        fetchAnimes();
      }
    } catch (err) {
      console.error("调整归属失败", err);
      setRegroupNotice("调整归属失败,请检查后端连接");
    } finally {
      setRegrouping(null);
    }
  };

  // 找到这一集所在的季,以及从这一集开始(含)往后的剩余集数(升序,即"接下来该看的顺序")
  const findRemainingEpisodesInSeason = (ep: Episode): Episode[] => {
    if (!detail) return [ep];
    for (const episodes of Object.values(detail.seasons)) {
      const idx = episodes.findIndex((e) => e.filename === ep.filename);
      if (idx !== -1) return episodes.slice(idx);
    }
    return [ep];
  };

  // 播放器自动连播切到下一集时,乐观标记那一集已看(跟点击那一集的语义一样,不等播完)。
  // 第一集是点击时就已经标记过的,这里只负责补上自动连播切到的后续集数。
  // 内置mpv靠IPC上报start-file,外置PotPlayer靠轮询窗口标题(见lib.rs),
  // 两边上报的负载形状一致,共用同一套处理。
  useEffect(() => {
    const unlistens: Array<() => void> = [];
    let cancelled = false;

    const onEpisodeStarted = (event: { payload: { path: string } }) => {
      const parsed = parseLibraryPath(event.payload.path, settings.library_root);
      if (!parsed) return;
      // 写库已经由 Rust 后台(lib.rs::report_episode_started)在检测到切集的那一刻
      // 直接完成了,不依赖这个页面是否还挂载着;这里只在页面还在场时顺手刷新画面,
      // 不再重复发请求。
      applyWatchedLocally(parsed.folderName, parsed.filename);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.library_root]);

  // 播放处理（基于动态拉取的设置拼接路径）
  const handlePlay = async (ep: Episode) => {
    if (!selectedAnime) return;

    // 防止双击：如果正在处理上一次播放请求，直接忽略这次点击
    if (isPlayingRef.current) return;
    isPlayingRef.current = true;

    try {
      const cleanRoot = settings.library_root.replace(/[/\\]$/, "");

      // 乐观标记:点开/切到这一集就算已看,内置mpv和外置播放器统一行为,
      // 不做"播到多少才算看完"这种判断。
      markWatched(selectedAnime.folder_name, ep.filename, ep.rel_path);

      if (settings.player_mode === "builtin") {
        // 内置mpv:把从这一集开始的剩余集数整季喂给它连播;自动连播切到的后续集数
        // 靠mpv上报的start-file事件补标记(见上面的useEffect),这里只标记点开的这一集。
        const remaining = findRemainingEpisodesInSeason(ep);
        const videoPaths = remaining.map(
          (e) => `${cleanRoot}\\${e.rel_path.replace(/\//g, "\\")}`,
        );
        if (await isTauri()) {
          await invoke("open_builtin_player", { videoPaths }).catch((err: any) =>
            console.error("拉起内置播放器失败:", err),
          );
        } else {
          console.log("[开发调试] 模拟内置mpv播放列表:", videoPaths);
        }
      } else {
        const localVideoPath = `${cleanRoot}\\${ep.rel_path.replace(/\//g, "\\")}`;
        if (await isTauri()) {
          await invoke("open_external_player", {
            videoPath: localVideoPath,
            playerPath: settings.potplayer_path,
          }).catch((err: any) => console.error("唤起播放器失败:", err));
        } else {
          console.log(`[开发调试] 模拟拉起播放器:\n播放器: ${settings.potplayer_path}\n视频: "${localVideoPath}"`);
        }
      }
    } finally {
      // 解锁：延迟一小段时间再解锁,避免快速双击在极短时间内仍然连续触发
      setTimeout(() => {
        isPlayingRef.current = false;
      }, 1000); // 1秒内的重复点击都会被忽略
    }
  };

  const sortedSeasons = detail
    ? Object.entries(detail.seasons).sort(([a], [b]) =>
        compareSeasonNames(a, b, detail.bucket_dates))
    : [];

  // 详情页头部真图横幅:比海报渲染高度(240px)更高一些,截取位置能往下多露一截,
  // 不至于人物脸部刚好卡在裁切线上。标题/简介叠在图片底部,用同一个高度对齐。
  const HERO_BANNER_HEIGHT = "26rem";
  const hasHeroBanner = animeMeta?.status === "resolved" && !!animeMeta.backdrop_url;

  // 列表页展示顺序:纯本地按当前sort模式重排,animes本身始终保留接口返回的原始顺序
  const displayedAnimes = useMemo(() => sortAnimes(animes, sort), [animes, sort]);

  return (
    <div className={`text-paper${movieOnly ? " flex h-full flex-col" : ""}`}>
      {selectedAnime ? (
        /* 详情视图:头部(返回+封面+简介+分季快捷跳转)吸顶固定,滚动集数列表时始终可见,
           跟Excel冻结表头一个道理 */
        <div>
          <div
            ref={headerRef}
            className="sticky top-0 z-10 overflow-hidden bg-ink"
          >
            {/* 底层:模糊拉伸的氛围图,铺满整个头部(不管头部因为下面的真图撑到多高)。
                这层反正是糊的,裁多少无所谓——真正要保证不裁切的是第2层的真图。
                有resolved背景图就用它模糊化(跟上层真图同一张,气氛统一);
                没有就退回Bangumi封面模糊放大,这个降级态本身就是没有TMDB数据时的
                正经默认态,不是"报错"或空白观感——追番用户访问新番详情页大概率
                长期停在这个状态。 */}
            <div className="absolute inset-0">
              {animeMeta?.status === "resolved" && animeMeta.backdrop_url ? (
                <img
                  src={proxiedImageUrl(animeMeta.backdrop_url)}
                  alt=""
                  className="h-full w-full scale-125 object-cover blur-2xl"
                />
              ) : selectedAnime.cover_url ? (
                <img
                  src={proxiedImageUrl(selectedAnime.cover_url)}
                  alt=""
                  className="h-full w-full scale-125 object-cover object-top blur-2xl"
                />
              ) : null}
              <div className="absolute inset-0 bg-gradient-to-b from-ink/10 via-ink/70 to-ink" />
            </div>

            {/* 真图:全宽横幅,固定高度HERO_BANNER_HEIGHT(比海报渲染高度240px更高一些,
                之前卡在海报高度上截得太靠上,人物脸部常常被切掉,加高之后object-top能往下
                多露出一截)。标题/分级/简介不再挪到图片下面单独一段,而是叠回图片底部——
                跟下面内容层用同一个minHeight对齐,再配一层由下往上的暗化渐变保证可读。 */}
            {animeMeta?.status === "resolved" && animeMeta.backdrop_url && (
              <div className="absolute inset-x-0 top-0 overflow-hidden" style={{ height: HERO_BANNER_HEIGHT }}>
                <img
                  src={proxiedImageUrl(animeMeta.backdrop_url)}
                  alt=""
                  className="h-full w-full object-cover object-top opacity-80"
                />
                {/* 暗化渐变加深(via-ink/30→via-ink/70,顶部也从全透明改成留一点底色):
                    之前文字直接叠在鲜艳的原图上对比度不够,标题还好(带drop-shadow),
                    分级/类型/简介这些次要文字看着发灰、糊在图里。 */}
                <div className="absolute inset-0 bg-gradient-to-t from-ink via-ink/70 to-ink/10" />
              </div>
            )}

            <div
              className="relative flex flex-col px-8 pb-4 pt-8"
              style={hasHeroBanner ? { minHeight: HERO_BANNER_HEIGHT } : undefined}
            >
              <div className="mb-6 flex flex-wrap items-center gap-2">
                <button
                  onClick={handleBack}
                  className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                >
                  <ArrowLeft size={14} /> 返回影视库
                </button>
                {/* 补番:只有已经匹配了bgm_id的番剧才有起点可以查关联作品 */}
                {selectedAnime.bgm_id && (
                  <button
                    onClick={handleToggleRelatedAnime}
                    title={showRelatedAnime ? "退出补番" : "补番"}
                    className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs backdrop-blur transition-colors ${
                      showRelatedAnime
                        ? "border-vermillion bg-vermillion text-ink"
                        : "border-border bg-ink/60 text-muted hover:border-vermillion hover:text-vermillion"
                    }`}
                  >
                    <Users size={14} /> {showRelatedAnime ? "退出补番" : "补番"}
                  </button>
                )}
                {selectedAnime.bgm_id && (
                  <button
                    onClick={handleOpenBangumiIntro}
                    title="Bangumi介绍"
                    className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                  >
                    <Info size={14} /> Bangumi介绍
                  </button>
                )}
                {!showRelatedAnime && (
                  <button
                    onClick={() => {
                      setEpisodeManageMode((v) => !v);
                      setPendingDeleteEpisode(null);
                    }}
                    title={episodeManageMode ? "结束管理" : "管理"}
                    className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs backdrop-blur transition-colors ${
                      episodeManageMode
                        ? "border-vermillion bg-vermillion text-ink"
                        : "border-border bg-ink/60 text-muted hover:border-vermillion hover:text-vermillion"
                    }`}
                  >
                    <Settings2 size={14} /> {episodeManageMode ? "结束管理" : "管理"}
                  </button>
                )}
                {/* 文件夹级归属入口:作用对象是这个文件夹的全部文件,归属主体就是
                    文件夹自己的 bgm_id,不存在"判断不出是哪一部"的问题。
                    已经拆出去的那一部要合并回原系列,走的就是这个入口。 */}
                {episodeManageMode && selectedAnime.bgm_id && detail && (
                  <button
                    disabled={regrouping !== null}
                    onClick={() =>
                      openRegroupDialog({
                        label: `整个「${selectedAnime.display_title || selectedAnime.folder_name}」`,
                        relPaths: Object.values(detail.seasons).flat().map((e) => e.rel_path),
                        bgmId: selectedAnime.bgm_id,
                      })
                    }
                    title="调整归属…"
                    className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 font-mono text-xs text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
                  >
                    <Move size={14} /> 调整归属…
                  </button>
                )}
                {episodeDeleteError && (
                  <span className="font-mono text-[11px] text-vermillion">
                    删除失败: {episodeDeleteError}
                  </span>
                )}
              </div>

              {regroupNotice && (
                <div className="mb-4 flex items-start justify-between gap-3 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-[11px] text-vermillion">
                  <span>{regroupNotice}</span>
                  <button
                    onClick={() => setRegroupNotice(null)}
                    className="shrink-0 text-muted transition-colors hover:text-paper"
                  >
                    知道了
                  </button>
                </div>
              )}

              {/* 海报+标题/简介+分季跳转打包成一组,用flex-1 justify-center在按钮行
                  之下的剩余空间里居中——不是把整个头部都居中(按钮行还是钉在最上面),
                  只是让这一组在按钮行下方"匀出来的高度"里上下留白相等,不再是之前
                  mt-auto那种"全部空间都堆在上面、这组贴死在底边"的不对称观感。
                  没有真图时minHeight不生效,没有多余空间可分,这个包裹div就是普通block,
                  行为跟以前一样。 */}
              <div className={hasHeroBanner ? "flex flex-1 flex-col justify-center" : undefined}>
              <div className="flex flex-col gap-6 md:flex-row md:items-end">
                <div className="w-40 aspect-[2/3] shrink-0 rounded-md border border-border bg-surface shadow-2xl overflow-hidden">
                  {selectedAnime.cover_url ? (
                    <img
                      src={proxiedImageUrl(selectedAnime.cover_url)}
                      alt={selectedAnime.folder_name}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center text-muted">
                      <FolderOpen size={40} />
                    </div>
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  {/* LOGO:有resolved的TMDB LOGO图就用它,否则退化成文字标题——
                      这不是"缺失"的观感,大多数动画本来就没有专属ClearLogo。 */}
                  {animeMeta?.status === "resolved" && animeMeta.logo_url ? (
                    <img
                      src={proxiedImageUrl(animeMeta.logo_url)}
                      alt={selectedAnime.display_title || selectedAnime.folder_name}
                      className="mb-2 max-h-16 max-w-full object-contain object-left [filter:drop-shadow(0_1px_1px_rgba(0,0,0,0.55))_drop-shadow(0_0_7px_rgba(0,0,0,0.6))]"
                    />
                  ) : (
                    <h1 className="mb-2 font-display text-2xl tracking-tight drop-shadow">
                      {selectedAnime.display_title || selectedAnime.folder_name}
                    </h1>
                  )}
                  {/* 元数据行:分级/类型/工作室,只在resolved且真有内容时显示,
                      不留空占位——跟其他字段缺失时的克制风格一致。 */}
                  {/* 叠在图上的次要文字之前用text-muted(中灰),对比度不够、糊在图里——
                      改成text-paper/90(接近白)+drop-shadow,跟标题一个观感强度。 */}
                  {animeMeta?.status === "resolved" &&
                    (animeMeta.content_rating || (animeMeta.genres?.length ?? 0) > 0) && (
                      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-paper/90 drop-shadow">
                        {animeMeta.content_rating && (
                          <span className="rounded border border-border/80 bg-ink/40 px-1.5 py-0.5">
                            {animeMeta.content_rating}
                          </span>
                        )}
                        {(animeMeta.genres?.length ?? 0) > 0 && <span>{animeMeta.genres!.join(" / ")}</span>}
                      </div>
                    )}
                  <p className="max-h-32 overflow-y-auto pr-1 text-sm text-paper/90 drop-shadow">
                    {selectedAnime.summary}
                  </p>
                </div>
              </div>

              {/* 分季/剧场版快捷跳转:按实际扫到的分季动态生成,只有一季时不用显示;
                  补番视图下跟集数列表无关,不显示 */}
              {!showRelatedAnime && sortedSeasons.length > 1 && (
                <div className="flex flex-wrap gap-2 pt-4">
                  {sortedSeasons.map(([seasonName]) => (
                    <button
                      key={seasonName}
                      onClick={() => scrollToSeason(seasonName)}
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
            {showRelatedAnime ? (
              relatedLoading ? (
                <div className="font-mono text-xs text-muted">正在查询相关作品...</div>
              ) : relatedError ? (
                <div className="font-mono text-xs text-vermillion">获取失败: {relatedError}</div>
              ) : (
                <BangumiResultsList
                  results={relatedAnime}
                  onSelect={(bgmId) => {
                    // 跳 DetailPage 前存快照,让 DetailPage「返回」能回到这个补番一览
                    if (selectedAnime) {
                      saveLibraryDetailSession({
                        anime: selectedAnime,
                        relatedAnime,
                        gridScrollTop: gridScrollTop.current, // 进详情时已记(handleSelectAnime)
                        relatedScrollTop: scrollContainerRef?.current?.scrollTop ?? 0,
                        mode: "related",
                      });
                    }
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
                    className="border border-border rounded-lg bg-surface p-4"
                  >
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border pb-1.5">
                      <h3 className="font-display text-lg text-vermillion">{seasonName}</h3>
                      {/* 归属入口:管理态下**每个桶都有**。能自动确定是哪一部就带上
                          名字直接预选,确定不了(剧场版/OVA 这类几十部共用的桶)也照样
                          有按钮,进弹窗让用户自己选——按钮的有无不该取决于系统能不能
                          自动识别,那正是之前拆出去就合并不回来的根因。 */}
                      {episodeManageMode && (
                        <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
                          {detail?.season_owners?.[seasonName] && (
                            <span className="text-muted/70">
                              {detail.season_owners[seasonName].name}
                            </span>
                          )}
                          <button
                            disabled={regrouping !== null}
                            onClick={() =>
                              openRegroupDialog({
                                label: seasonName,
                                relPaths: episodes.map((e) => e.rel_path),
                                bgmId: detail?.season_owners?.[seasonName]?.bgm_id ?? null,
                              })
                            }
                            className="flex items-center gap-1 rounded border border-border px-2 py-1 text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
                          >
                            <Move size={12} /> 调整归属…
                          </button>
                        </div>
                      )}
                    </div>
                    <div className="flex flex-col gap-2">
                      {[...episodes].reverse().map((ep, idx) => (
                        <div
                          key={idx}
                          className="flex items-center justify-between gap-3 p-3 rounded hover:bg-paper/5 transition-colors group border border-transparent hover:border-border/50"
                        >
                          {/* 左侧：文件名与播放时间信息,宽度不够时文件名省略号截断,不挤开右侧播放按钮 */}
                          <div className="flex min-w-0 flex-1 flex-col">
                            <div className="flex min-w-0 items-center gap-2">
                              <span
                                className={`min-w-0 flex-1 truncate font-mono text-sm ${
                                  ep.is_watched ? "text-muted line-through" : "text-paper"
                                }`}
                              >
                                {ep.filename}
                              </span>
                              {ep.is_watched && (
                                <CheckCircle2 size={14} className="shrink-0 text-green-500/80" />
                              )}
                            </div>

                            {/* 渲染最后播放时间 */}
                            {ep.is_watched && ep.watched_at && (
                              <span className="text-[10px] text-green-500/70 mt-1 font-mono">
                                上次观看: {ep.watched_at}
                              </span>
                            )}
                          </div>

                          {/* 右侧：播放按钮(固定宽度,不随文件名长短被挤压/挤出屏幕),
                              管理模式下换成删除入口,点删除先原地变成确定/取消二次确认 */}
                          {episodeManageMode ? (
                            pendingDeleteEpisode === ep.rel_path ? (
                              <div className="flex w-28 shrink-0 items-center justify-center gap-1.5 font-mono text-xs">
                                <button
                                  disabled={deletingEpisode === ep.rel_path}
                                  onClick={() => handleDeleteEpisode(ep)}
                                  className="rounded border border-vermillion bg-vermillion px-2 py-1 text-ink transition-colors hover:bg-vermillion/90 disabled:opacity-40"
                                >
                                  确定
                                </button>
                                <button
                                  onClick={() => setPendingDeleteEpisode(null)}
                                  className="rounded border border-border bg-surface px-2 py-1 text-muted transition-colors hover:text-paper"
                                >
                                  取消
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={() => setPendingDeleteEpisode(ep.rel_path)}
                                className="flex w-28 shrink-0 items-center justify-center gap-1.5 rounded-md border border-vermillion bg-vermillion px-3 py-1.5 font-mono text-xs text-ink transition-colors hover:bg-vermillion/90"
                              >
                                <Trash2 size={14} />
                                删除
                              </button>
                            )
                          ) : (
                            <div className="flex shrink-0 items-center gap-2 font-mono text-xs">
                              {/* 非常规季桶(剧场版/OVA/Season 00/Other/Specials)默认就显示"添加到剧场版模式";
                                  已加过的显示"已添加到剧场版"(不可再点)。常规 Season 01+ 不显示。 */}
                              {!isRegularSeasonBucket(seasonName) && (
                                addedRelPaths.has(ep.rel_path) ? (
                                  <span className="rounded-md border border-border px-3 py-1.5 text-muted/70">
                                    已添加到剧场版
                                  </span>
                                ) : (
                                  <button
                                    onClick={() => openPickerForAdd(ep)}
                                    title="把这一集作为独立剧场版/OVA 加入剧场版模式"
                                    className="rounded-md border border-border px-3 py-1.5 text-muted transition-colors hover:border-vermillion hover:text-vermillion"
                                  >
                                    添加到剧场版模式
                                  </button>
                                )
                              )}
                              <button
                                onClick={() => handlePlay(ep)}
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
              <div className="font-mono text-xs text-muted py-8">
                该文件夹下未找到符合格式的视频文件。
              </div>
            )}
          </div>
        </div>
      ) : (
        /* 列表视图 */
        <div className={movieOnly ? "flex min-h-0 flex-1 flex-col" : ""}>
          {/* 铺满整屏的背景,只开这一份,不再跟冻结顶部各搞一套:
              position:fixed,尺寸永远是整个可视窗口(不是内容高度),盖在body底色
              和卡片网格后面(负z-index,侧栏Sidebar自己有实色背景不会被透过去),
              滚动卡片列表时背景常驻不跟着滚走。
              真图(有TMDB数据时)object-cover铺满整个窗口高度——之前是按16:9比例
              限高,清晰图只占一小截、剩下大片全是模糊糊,两段观感断裂;现在宽屏/
              高窗口下会裁掉一些边缘细节,但换来"从头到尾一张连续的图"的整体感,
              用户反馈更看重这个。模糊放大图仍然垫底,只有没有TMDB数据时才会真的
              露出来当兜底,不是主力观感。 */}
          {movieOnly && activeHead && (
            <div className="fixed inset-0 -z-10">
              {activeHead.cover_url && (
                <img
                  src={proxiedImageUrl(activeHead.cover_url)}
                  alt=""
                  className="h-full w-full scale-125 object-cover object-top blur-2xl"
                />
              )}
              {animeMeta?.status === "resolved" && animeMeta.backdrop_url && (
                <img
                  src={proxiedImageUrl(animeMeta.backdrop_url)}
                  alt=""
                  className="absolute inset-0 h-full w-full object-cover object-top opacity-90"
                />
              )}
              <div className="absolute inset-0 bg-gradient-to-b from-ink/40 via-ink/15 to-ink/75" />
            </div>
          )}
          {/* 冻结顶部:标题+控件(剧场版再含 hero+明细行),照追更页做法始终贴顶不滚。
              之前给冻结顶部另外叠了一层bg-ink/45,跟下面fixed背景自己的渐变暗化
              (from-ink/25)撞在一起变成两层暗化叠加,冻结顶部范围因此比卡片区更暗、
              更"糊"——同一张图深浅不一致,看着像断层。现在冻结顶部完全不再单独加
              任何背景/遮罩,暗化只交给下面那一份fixed渐变来管,全程只有一个暗化
              来源,深浅必然连续、不会再有接缝。 */}
          <div className={`overflow-hidden px-8 pt-8 ${movieOnly ? "relative shrink-0 pb-6" : "sticky top-0 z-20 bg-ink pb-4"}`}>
          <div className="relative flex justify-between items-center mb-6">
            <div>
              <h1 className="font-display text-2xl tracking-tight">{movieOnly ? "剧场版" : "影视库"}</h1>
              {!movieOnly && (
                <p className="font-mono text-xs text-muted mt-1">
                  关联目录：{settings.library_root} | 发现 {animes.length} 部动画
                </p>
              )}
            </div>
            <div className="flex items-center gap-2">
              {/* 影视库页:排序 / 刷新扫盘 / 管理。剧场版页:表驱动、不扫盘,只有管理。 */}
              {!movieOnly ? (
                <>
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
                        onClick={() => handleSortChange(opt.value)}
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
                  {/* "刷新 & 扫盘"平时用不上(挂载/切回本页会自动静默扫一遍),只有
                      "停留在本页时手动往库文件夹里加文件"这种场景才需要手动触发,
                      跟着管理模式一起显示/隐藏,不常驻主工具栏。 */}
                  {matchMode && (
                    <button
                      onClick={() => scanAndFetchAnimes()}
                      className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
                    >
                      <RefreshCcw size={14} /> 刷新 & 扫盘
                    </button>
                  )}
                  <button
                    onClick={() => {
                      setMatchMode((v) => !v);
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
                </>
              ) : (
                <>
                  {/* 观看态筛选:全部 / 未看 / 已看 */}
                  <div className="flex overflow-hidden rounded-md border border-border font-mono text-xs">
                    {(
                      [
                        { value: "all", label: "全部" },
                        { value: "unwatched", label: "未看" },
                        { value: "watched", label: "已看" },
                      ] as { value: "all" | "unwatched" | "watched"; label: string }[]
                    ).map((opt) => (
                      <button
                        key={opt.value}
                        onClick={() => setMovieWatchFilter(opt.value)}
                        className={`px-3 py-1.5 transition-colors ${
                          movieWatchFilter === opt.value
                            ? "bg-vermillion text-ink"
                            : "bg-surface text-muted hover:bg-surface-hover hover:text-paper"
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  {/* 之前非激活态没有背景色,直接透在背景图上,灰字+小图标基本看不清——
                      补上bg-surface,跟左边"全部/未看/已看"筛选条的非激活态背景统一。 */}
                  <button
                    onClick={() => { setMovieManage((v) => !v); setPendingMovieDelete(null); }}
                    className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                      movieManage
                        ? "border-vermillion bg-vermillion text-ink"
                        : "border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion hover:text-paper"
                    }`}
                  >
                    <Settings2 size={14} /> {movieManage ? "结束管理" : "管理"}
                  </button>
                </>
              )}
            </div>
          </div>

          {deleteAnimeError && (
            <div className="relative mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
              删除失败: {deleteAnimeError}
            </div>
          )}

          {/* 剧场版 hero + 明细行:放进冻结顶部,滚动卡片网格时保持不动。
              背景已经挪到冻结顶部整层(见上面),这里不再自己围边框/背景,
              直接把海报+文字铺在共享的通栏背景上——跟详情页头部同一个观感。
              LOGO/分级/类型/工作室这几块也搬过来跟详情页头部对齐,之前这里
              没有,看起来两个页面不是一套东西。 */}
          {movieOnly && activeHead && (
            <div className="relative flex gap-6">
              <div className="h-56 w-40 shrink-0 overflow-hidden rounded-md border border-border bg-surface shadow-2xl">
                {activeHead.cover_url ? (
                  <img src={proxiedImageUrl(activeHead.cover_url)} alt={activeHead.title ?? ""} className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted">
                    <Loader2 size={24} strokeWidth={1.5} className="animate-spin" />
                    <span className="text-[10px] font-mono">等待更新</span>
                  </div>
                )}
              </div>
              <div className="flex min-w-0 flex-1 flex-col">
                {animeMeta?.status === "resolved" && animeMeta.logo_url ? (
                  <img
                    src={proxiedImageUrl(animeMeta.logo_url)}
                    alt={activeHead.title || activeHead.filename}
                    className="mb-2 max-h-16 max-w-full object-contain object-left [filter:drop-shadow(0_1px_1px_rgba(0,0,0,0.55))_drop-shadow(0_0_7px_rgba(0,0,0,0.6))]"
                  />
                ) : (
                  <h1 className="mb-2 font-display text-2xl tracking-tight drop-shadow">{activeHead.title || activeHead.filename}</h1>
                )}
                {animeMeta?.status === "resolved" &&
                  (animeMeta.content_rating || (animeMeta.genres?.length ?? 0) > 0 || (animeMeta.studios?.length ?? 0) > 0) && (
                    <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-paper/90 drop-shadow">
                      {animeMeta.content_rating && (
                        <span className="rounded border border-border/80 bg-ink/40 px-1.5 py-0.5">
                          {animeMeta.content_rating}
                        </span>
                      )}
                      {(animeMeta.genres?.length ?? 0) > 0 && <span>{animeMeta.genres!.join(" / ")}</span>}
                      {(animeMeta.studios?.length ?? 0) > 0 && <span>{animeMeta.studios!.join(" / ")}</span>}
                    </div>
                  )}
                {/* 之前用flex-1+overflow-y-auto指望flex拉伸出高度上限,但这一列的父行
                    没有强制等高(内容比海报高就把整行撑高),导致简介一直不触发滚动、
                    往下无限撑。改成跟详情页头部一样直接给max-h硬顶。 */}
                <p className="max-h-32 max-w-2xl overflow-y-auto font-mono text-xs leading-relaxed text-paper/90 drop-shadow">
                  {activeHead.summary || "暂无简介"}
                </p>
                {movieManage && (
                  <div className="mt-3 flex items-center gap-2 font-mono text-xs">
                    <button
                      onClick={() => openPickerForRegroup(activeItems)}
                      className="flex items-center gap-1.5 rounded-md border border-border bg-ink/60 px-3 py-1.5 text-muted backdrop-blur transition-colors hover:border-vermillion hover:text-vermillion"
                    >
                      <RefreshCcw size={14} /> 重选条目
                    </button>
                    <button
                      disabled={movieBusy}
                      onClick={() => activeItems.forEach((it) => handleRemoveStandalone(it))}
                      className="flex items-center gap-1.5 rounded-md border border-vermillion bg-ink/60 px-3 py-1.5 text-vermillion backdrop-blur transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                    >
                      <FolderMinus size={14} /> 移出列表
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
          {movieOnly && expandedBgm !== null && expandedItems.length > 0 && (
            <div className="relative mt-4 flex flex-col gap-1.5 rounded-md border border-border bg-surface p-3">
              {expandedItems.map((item) => (
                <div key={item.id} className="flex items-center justify-between gap-3 rounded px-2 py-1.5 hover:bg-paper/5">
                  <div className="flex min-w-0 items-center gap-2">
                    {item.is_watched && <CheckCircle2 size={14} className="shrink-0 text-green-500/80" />}
                    <span className={`min-w-0 truncate font-mono text-xs ${item.is_watched ? "text-muted line-through" : "text-paper"} ${item.missing ? "opacity-50" : ""}`} title={item.rel_path}>
                      {item.filename}{item.missing ? "(文件缺失)" : ""}
                    </span>
                  </div>
                  {movieManage ? (
                    pendingMovieDelete === item.rel_path ? (
                      <div className="flex shrink-0 items-center gap-1.5 font-mono text-xs">
                        <button disabled={movieBusy} onClick={() => handleDeleteStandaloneFile(item)} className="rounded border border-vermillion bg-vermillion px-2 py-1 text-ink hover:bg-vermillion/90 disabled:opacity-40">确定删</button>
                        <button onClick={() => setPendingMovieDelete(null)} className="rounded border border-border bg-surface px-2 py-1 text-muted hover:text-paper">取消</button>
                      </div>
                    ) : (
                      <div className="flex shrink-0 items-center gap-2 font-mono text-xs">
                        <button onClick={() => setPendingMovieDelete(item.rel_path)} className="flex items-center gap-1 rounded-md border border-vermillion bg-vermillion px-3 py-1 text-ink hover:bg-vermillion/90">
                          <Trash2 size={13} /> 删除文件
                        </button>
                        <button disabled={movieBusy} onClick={() => handleRemoveStandalone(item)} className="flex items-center gap-1 rounded-md border border-border px-3 py-1 text-muted hover:border-vermillion hover:text-vermillion disabled:opacity-40">
                          <FolderMinus size={13} /> 仅移出
                        </button>
                        {/* 归属调整:跟上面的"重新分组"(只改封面来源、不动文件)不同,
                            这个会把磁盘上的文件真的搬到另一个系列文件夹下。 */}
                        <button
                          disabled={regrouping !== null || item.missing}
                          onClick={() =>
                            openRegroupDialog({
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
                      onClick={() => handlePlayStandalone(item)}
                      className={`flex w-24 shrink-0 items-center justify-center gap-1.5 rounded-md border px-3 py-1 font-mono text-xs transition-colors disabled:opacity-40 ${item.is_watched ? "border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion" : "border-vermillion bg-vermillion text-ink hover:bg-vermillion/90"}`}
                    >
                      <Play size={13} fill="currentColor" />{item.is_watched ? "再看" : "播放"}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          </div>
          {/* /冻结顶部 */}

          {/* pt-3:给卡片右上角骑边框的"未看集数"角标留出空间——它用负偏移探出卡片
              顶部一点,第一排卡片正好贴着这个容器的上边缘,不留白会被上面滚动区域裁掉。
              剧场版页:顶部 hero 固定不滚,只有卡片网格这一块内部滚动(min-h-0 flex-1
              overflow-y-auto),否则窗口一矮,卡片会滚到透明的 hero 区域后面露出来。 */}
          <div className={`px-8 pb-8 pt-3${movieOnly ? " min-h-0 flex-1 overflow-y-auto" : ""}`}>
          {!movieOnly && (loading ? (
            <div className="font-mono text-xs text-muted">正在加载...</div>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
              {displayedAnimes.map((anime) => (
                <div
                  key={anime.id}
                  onClick={() => handleSelectAnime(anime)}
                  className="group relative cursor-pointer"
                >
                  <div className="relative aspect-[2/3] overflow-hidden rounded-md bg-surface shadow-md">
                    {anime.cover_url ? (
                      <img
                        src={proxiedImageUrl(anime.cover_url)}
                        alt={anime.folder_name}
                        className="h-full w-full object-cover transition-transform group-hover:scale-105"
                      />
                    ) : anime.bgm_id ? (
                      // 已匹配但封面还没拉到(后台正在按策略解析/补缓存):显示"等待更新"而不是
                      // "No Cover"——这是暂态,下次列表请求补全后就有图了。
                      <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted bg-surface border border-border">
                        <Loader2 size={28} strokeWidth={1.5} className="animate-spin" />
                        <span className="text-[10px] font-mono">等待更新</span>
                      </div>
                    ) : (
                      <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted bg-surface border border-border">
                        <FolderOpen size={32} strokeWidth={1.5} />
                        <span className="text-[10px] font-mono">No Cover</span>
                      </div>
                    )}
                    {(matchMode || !anime.bgm_id) && (
                      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink/60">
                        {/* 手动选封面:仅管理模式 + 已匹配bgm_id(有家族可取图)时显示,放在重新匹配上方 */}
                        {matchMode && anime.bgm_id && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              openCoverPicker(anime.folder_name, anime.bgm_id!);
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
                        {/* 删除入口只在管理模式下露出,不跟着"未匹配卡片始终显示覆盖层"这条 */}
                        {matchMode && (
                          pendingDeleteFolder === anime.folder_name ? (
                            <div
                              className="flex items-center gap-1.5 font-mono text-[10px]"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <button
                                disabled={deletingFolder === anime.folder_name}
                                onClick={() => handleDeleteAnime(anime.folder_name)}
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
                          )
                        )}
                      </div>
                    )}
                  </div>
                  {/* 未看集数角标:开关打开且确实有未看集数才显示。挂在最外层卡片(而不是
                      上面overflow-hidden的封面容器)上,才能用负偏移让圆圈骑在卡片右上角
                      边框上,不被圆角裁掉一部分。 */}
                  {settings.library_unwatched_badge_enabled && anime.unwatched_count > 0 && (
                    <div className="absolute -top-2 -right-2 flex h-7 min-w-7 items-center justify-center rounded-full bg-vermillion/80 px-1.5 font-mono text-xs font-bold text-ink shadow-lg ring-2 ring-surface backdrop-blur-sm">
                      {anime.unwatched_count > 99 ? "99+" : anime.unwatched_count}
                    </div>
                  )}
                  <div className="mt-2 line-clamp-2 text-sm font-medium leading-snug group-hover:text-vermillion transition-colors">
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
          ))}

          {/* 剧场版页:顶部 hero + 明细行 + 底部无限轮播 */}
          {movieOnly && (
            standaloneLoading ? (
              <div className="font-mono text-xs text-muted">正在加载...</div>
            ) : movieGroups.length === 0 ? (
              <div className="py-16 text-center font-mono text-xs text-muted">
                还没有独立展示的剧场版/OVA。下载剧场版会自动加入;也可在某部番的详情页里,把某一集设为独立剧场版/OVA。
              </div>
            ) : filteredGroups.length === 0 ? (
              <div className="py-16 text-center font-mono text-xs text-muted">
                当前筛选下没有内容。
              </div>
            ) : (
              <div>
                {/* 底部卡片:纵向网格(和番剧模式一致,向下滚动),点击直接播放/展开。hero/明细行已移到冻结顶部 */}
                <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
                  {filteredGroups.map((g) => {
                    const head = g.items[0];
                    const allWatched = g.items.every((it) => it.is_watched);
                    const isActive = g.bgm_id === activeBgm;
                    return (
                      <div
                        key={g.bgm_id}
                        onMouseEnter={() => handleMovieCardHover(g.bgm_id)}
                        onClick={() => handleMovieCardClick(g)}
                        className="group cursor-pointer"
                      >
                        <div className={`relative aspect-[2/3] overflow-hidden rounded-md bg-surface shadow-md ring-2 transition-all ${isActive ? "ring-vermillion" : "ring-transparent"}`}>
                          {head.cover_url ? (
                            <img src={proxiedImageUrl(head.cover_url)} alt={head.title ?? head.filename}
                              className="h-full w-full object-cover transition-transform group-hover:scale-105" />
                          ) : (
                            <div className="flex h-full w-full flex-col items-center justify-center gap-1 text-muted bg-surface border border-border">
                              <Loader2 size={22} strokeWidth={1.5} className="animate-spin" />
                              <span className="text-[10px] font-mono">等待更新</span>
                            </div>
                          )}
                          {/* hover 时浮现播放三角:用主色 vermillion(与各处播放按钮一致),
                              不用 text-paper——那个在浅色模式会翻成深色,三角发黑难看 */}
                          <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-ink/25 opacity-0 transition-opacity group-hover:opacity-100">
                            <Play size={40} fill="currentColor" className="text-vermillion drop-shadow-lg" />
                          </div>
                        </div>
                        <div className={`mt-2 line-clamp-2 text-sm font-medium leading-snug transition-colors ${isActive ? "text-vermillion" : "group-hover:text-vermillion"}`}>
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
              </div>
            )
          )}
          </div>
          {/* /内容区 */}
        </div>
      )}

      {/* 选条目弹窗(手动追加 / 重选条目共用):复用 BangumiResultsList */}
      {pickerMode !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6" onClick={closePicker}>
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-md border border-border bg-surface p-5" onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 flex items-center justify-between">
              <div className="text-sm">{pickerMode === "add" ? "设为独立剧场版/OVA — 选择条目" : "重选条目"}</div>
              <button onClick={closePicker} className="rounded border border-border px-2 py-1 font-mono text-[11px] text-muted hover:text-paper">关闭</button>
            </div>
            <div className="mb-3 flex items-center gap-2">
              <input
                value={pickerKeyword}
                onChange={(e) => setPickerKeyword(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && runPickerSearch()}
                placeholder="搜索剧场版/OVA 名称"
                className="flex-1 rounded border border-border bg-ink px-3 py-1.5 text-sm text-paper outline-none placeholder:text-muted/60 focus:border-vermillion"
              />
              <button onClick={runPickerSearch} disabled={pickerLoading} className="rounded-md border border-vermillion px-4 py-1.5 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40">
                {pickerLoading ? "检索中..." : "检索"}
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {pickerResults.length === 0 ? (
                <div className="py-10 text-center font-mono text-xs text-muted">输入名称检索,选中一部作为该{pickerMode === "add" ? "文件" : "卡片"}的条目。</div>
              ) : (
                <BangumiResultsList results={pickerResults} onSelect={(bgmId) => pickerSelect(bgmId)} emptyText="" />
              )}
            </div>
          </div>
        </div>
      )}

      {/* 「归属」弹窗:先确认这批文件是哪一部,再决定归到哪里 */}
      {regroupTarget !== null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6"
          onClick={closeRegroupDialog}
        >
          <div
            className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-md border border-border bg-surface p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <div className="min-w-0 text-sm">
                调整归属：<span className="text-muted">{regroupTarget.label}</span>
              </div>
              <button
                onClick={closeRegroupDialog}
                className="shrink-0 rounded border border-border px-2 py-1 font-mono text-[11px] text-muted transition-colors hover:text-paper"
              >
                关闭
              </button>
            </div>
            <p className="mb-3 font-mono text-[11px] text-muted">
              共 {regroupTarget.relPaths.length} 个文件。搬到新文件夹后会自动按新归属重排名字和季度目录，
              不需要再手动跑「修复媒体库」。
            </p>

            {regroupLoading ? (
              <div className="py-10 text-center font-mono text-xs text-muted">正在加载家族信息...</div>
            ) : (
              <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
                {/* 第一段:这批文件是哪一部 */}
                <div>
                  <div className="mb-2 font-mono text-[11px] text-muted">
                    1. 这批文件属于哪一部
                    {regroupTarget.bgmId !== null && (
                      <span className="ml-2 text-vermillion">已自动识别，可改</span>
                    )}
                  </div>
                  {!regroupCandidates || regroupCandidates.members.length === 0 ? (
                    <div className="rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
                      拿不到家族成员列表（这部可能还没匹配 Bangumi 条目）。
                    </div>
                  ) : (
                    <div className="grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-3">
                      {regroupCandidates.members.map((m) => (
                        <button
                          key={m.bgm_id}
                          onClick={() => setPickedBgmId(m.bgm_id)}
                          className="group flex flex-col gap-1 text-left"
                        >
                          <div
                            className={`relative aspect-[2/3] overflow-hidden rounded border bg-ink transition-colors ${
                              pickedBgmId === m.bgm_id
                                ? "border-vermillion ring-1 ring-vermillion"
                                : "border-border group-hover:border-vermillion"
                            }`}
                          >
                            {m.cover_url ? (
                              <img
                                src={proxiedImageUrl(m.cover_url)}
                                alt={m.title}
                                className="h-full w-full object-cover"
                              />
                            ) : (
                              <div className="flex h-full w-full items-center justify-center font-mono text-[10px] text-muted">
                                无封面
                              </div>
                            )}
                          </div>
                          <div
                            className={`line-clamp-2 font-mono text-[10px] leading-snug ${
                              pickedBgmId === m.bgm_id ? "text-vermillion" : "text-muted"
                            }`}
                          >
                            {m.title}
                            {m.is_auto_root && <span className="text-vermillion">（系列根）</span>}
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                {/* 第二段:归到哪里 */}
                <div>
                  <div className="mb-2 font-mono text-[11px] text-muted">2. 归到哪里</div>
                  {pickedBgmId === null ? (
                    <div className="rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
                      请先在上面选中这批文件属于哪一部。
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
                      <button
                        disabled={regrouping !== null}
                        onClick={() =>
                          runRegroup("dialog", pickedBgmId, null, regroupTarget.relPaths)
                        }
                        className="rounded border border-border px-3 py-1.5 text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
                      >
                        独立成一部
                      </button>
                      <select
                        defaultValue=""
                        disabled={regrouping !== null}
                        onChange={(e) => {
                          const target = Number(e.target.value);
                          if (target) runRegroup("dialog", pickedBgmId, target, regroupTarget.relPaths);
                        }}
                        className="rounded border border-border bg-surface px-2 py-1.5 text-paper outline-none focus:border-vermillion disabled:opacity-40"
                      >
                        <option value="">合并到…</option>
                        {/* 家族根排最前(合并回原系列是最常用的动作),再列媒体库其余系列 */}
                        {regroupCandidates && (
                          <option value={regroupCandidates.auto_root.bgm_id}>
                            {regroupCandidates.auto_root.title}（原系列）
                          </option>
                        )}
                        {animes
                          .filter(
                            (a) =>
                              a.bgm_id &&
                              a.bgm_id !== regroupCandidates?.auto_root.bgm_id &&
                              a.bgm_id !== pickedBgmId,
                          )
                          .map((a) => (
                            <option key={a.folder_name} value={a.bgm_id!}>
                              {a.display_title || a.folder_name}
                            </option>
                          ))}
                      </select>
                      {regroupCandidates?.is_overridden && (
                        <button
                          disabled={regrouping !== null}
                          onClick={() =>
                            runRegroup("dialog", pickedBgmId, null, regroupTarget.relPaths, true)
                          }
                          className="rounded border border-vermillion px-3 py-1.5 text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
                        >
                          恢复自动归属
                        </button>
                      )}
                      {regrouping !== null && <span className="text-muted">处理中...</span>}
                    </div>
                  )}
                  {regroupCandidates?.is_overridden && (
                    <p className="mt-2 font-mono text-[11px] text-muted">
                      这一部当前是手动指定的归属。「恢复自动归属」会清掉手动设置，
                      并把文件搬回 Bangumi 判定的系列「{regroupCandidates.auto_root.title}」。
                    </p>
                  )}
                </div>
              </div>
            )}

            {regroupNotice && (
              <div className="mt-3 rounded border border-vermillion/40 bg-ink p-3 font-mono text-[11px] text-vermillion">
                {regroupNotice}
              </div>
            )}
          </div>
        </div>
      )}

      {/* "选择图片"弹窗:家族全部作品封面网格,点一张即设为该番封面 */}
      {coverPickerFolder !== null && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6"
          onClick={closeCoverPicker}
        >
          <div
            className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-md border border-border bg-surface p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <div className="text-sm">选择媒体库封面</div>
              <div className="flex items-center gap-2 font-mono text-[11px]">
                <button
                  onClick={() => applyCover(null)}
                  disabled={coverBusy}
                  className="rounded border border-border px-2 py-1 text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
                >
                  恢复默认
                </button>
                <button
                  onClick={closeCoverPicker}
                  className="rounded border border-border px-2 py-1 text-muted transition-colors hover:text-paper"
                >
                  关闭
                </button>
              </div>
            </div>
            <p className="mb-3 font-mono text-[11px] text-muted">
              从该系列家族的全部作品里挑一张作为封面;“恢复默认”按设置里的默认封面策略自动选择。
            </p>
            {coverLoading ? (
              <div className="py-10 text-center font-mono text-xs text-muted">正在加载家族封面...</div>
            ) : coverCandidates.length === 0 ? (
              <div className="py-10 text-center font-mono text-xs text-muted">没有可选的家族封面。</div>
            ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-3 overflow-y-auto">
                {coverCandidates.map((c) => (
                  <button
                    key={c.bgm_id}
                    onClick={() => applyCover(c.bgm_id)}
                    disabled={coverBusy}
                    className="group flex flex-col gap-1 text-left disabled:opacity-50"
                  >
                    <div className="relative aspect-[2/3] overflow-hidden rounded border border-border bg-ink transition-colors group-hover:border-vermillion">
                      <img
                        src={proxiedImageUrl(c.cover_url)}
                        alt={c.title}
                        className="h-full w-full object-cover"
                      />
                    </div>
                    <div className="line-clamp-2 font-mono text-[10px] leading-snug text-muted group-hover:text-vermillion">
                      {c.title}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}