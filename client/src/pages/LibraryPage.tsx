// pages/LibraryPage.tsx
import { useEffect, useLayoutEffect, useMemo, useState, useRef } from "react";
import { Play, FolderOpen, ArrowLeft, CheckCircle2, Trash2 } from "lucide-react";
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
}

type SortMode = "default" | "recent_watched" | "recent_updated";

interface Episode {
  filename: string;
  rel_path: string;
  is_watched?: boolean;
  watched_at?: string;
}

interface AnimeDetail {
  folder_name: string;
  seasons: Record<string, Episode[]>;
}

// 定义设置项的类型
interface AppSettings {
  library_root: string;
  potplayer_path: string;
  player_mode: "builtin" | "external";
}

interface LibraryPageProps {
  onSelectAnime?: (bgmId: number) => void;
  onManualMatch?: (folderName: string) => void;
}

const API_BASE = "http://127.0.0.1:8080";

// 分季排序:TV季数从新到旧排最前,然后剧场版,再OVA,Other/无法识别的兜底桶排最后
// (对应后端 rename_engine.py / routers/library.py 实际会产出的分类桶名)
function seasonSortKey(name: string): [number, number] {
  const seasonMatch = name.match(/^season\s*(\d+)/i);
  if (seasonMatch) {
    const num = parseInt(seasonMatch[1], 10);
    if (num === 0) return [2, 0]; // "Season 00" 是OVA专用桶,不算正常TV季
    return [0, -num]; // 数字取负,配合升序排序实现"季数越大越靠前"
  }
  if (/剧场版|劇場版|movie/i.test(name)) return [1, 0];
  if (/\bova\b/i.test(name)) return [2, 0];
  return [3, 0];
}

function compareSeasonNames(a: string, b: string): number {
  const [tierA, subA] = seasonSortKey(a);
  const [tierB, subB] = seasonSortKey(b);
  if (tierA !== tierB) return tierA - tierB;
  if (subA !== subB) return subA - subB;
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
): { folderName: string; filename: string } | null {
  const normalizedRoot = libraryRoot.replace(/[/\\]$/, "").replace(/\//g, "\\").toLowerCase();
  const normalizedFull = fullPath.replace(/\//g, "\\");
  if (!normalizedFull.toLowerCase().startsWith(normalizedRoot)) return null;
  const rel = normalizedFull.slice(normalizedRoot.length).replace(/^\\/, "");
  const parts = rel.split("\\");
  if (parts.length < 2) return null;
  return { folderName: parts[0], filename: parts[parts.length - 1] };
}

export default function LibraryPage({ onSelectAnime, onManualMatch }: LibraryPageProps) {
  const [animes, setAnimes] = useState<LibraryAnime[]>([]);
  const [selectedAnime, setSelectedAnime] = useState<LibraryAnime | null>(null);
  const [detail, setDetail] = useState<AnimeDetail | null>(null);
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
  const isPlayingRef = useRef(false); // 新增：播放锁
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
  });

  // 组件挂载时:先拉取设置,再"先秒开列表、后台扫盘",同时恢复上次记住的排序方式。
  // 进页面不再阻塞等扫盘(scandir/stat 在网络共享媒体库下会卡)——先 fetchAnimes 从 DB
  // 直接把列表渲染出来,扫盘丢后台跑,跑完再静默(silent,不闪"正在加载")刷新一次。
  useEffect(() => {
    fetchSettings();
    fetchAnimes();
    fetch(`${API_BASE}/library/scan`)
      .then(() => fetchAnimes(true))
      .catch(() => {});
    fetch(`${API_BASE}/library/sort-mode`)
      .then((res) => res.json())
      .then((data) => {
        if (data?.mode) setSort(data.mode as SortMode);
      })
      .catch(() => {});
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

  // 点击某部动漫后，获取具体季、集数据
  const handleSelectAnime = (anime: LibraryAnime) => {
    setSelectedAnime(anime);
    setDetailLoading(true);
    // 管理模式/删除确认态/补番列表都是详情页局部状态,不能带着上一部番的状态进新一部的详情页
    setEpisodeManageMode(false);
    setPendingDeleteEpisode(null);
    setEpisodeDeleteError(null);
    setShowRelatedAnime(false);
    setRelatedAnime([]);
    setRelatedError(null);
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
    setEpisodeManageMode(false);
    setPendingDeleteEpisode(null);
    setEpisodeDeleteError(null);
    setShowRelatedAnime(false);
    setRelatedAnime([]);
    setRelatedError(null);

    // 返回列表时,若存在"已匹配bgm但封面还没补上"的番(通常是新加入/移动后
    // Bangumi详情刚由后台补全,见list_library_animes的懒加载补全),静默重拉一次
    // 把封面补上;没有待补的就不发请求。未匹配(无bgm_id)的番不算待补。
    if (animes.some((a) => a.bgm_id != null && !a.cover_url)) {
      fetchAnimes(true);
    }
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

  // 标记已看:乐观标记,不等真的播完——点开/切到这一集就算。
  // folderName来自调用方各自的上下文(选中的番,或者从mpv上报路径反推出来的),
  // 不用同一个"当前选中番"假设,因为mpv连播时用户可能已经切走界面。
  const markWatched = (folderName: string, filename: string) => {
    fetch(`${API_BASE}/library/watch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_name: folderName, filename }),
    }).catch((err: any) => console.error("标记进度失败", err));

    const nowStr = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
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
        a.folder_name === folderName ? { ...a, last_watched_at: nowIso } : a,
      ),
    );
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
      markWatched(parsed.folderName, parsed.filename);
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
      markWatched(selectedAnime.folder_name, ep.filename);

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
    ? Object.entries(detail.seasons).sort(([a], [b]) => compareSeasonNames(a, b))
    : [];

  // 列表页展示顺序:纯本地按当前sort模式重排,animes本身始终保留接口返回的原始顺序
  const displayedAnimes = useMemo(() => sortAnimes(animes, sort), [animes, sort]);

  return (
    <div className="text-paper">
      {selectedAnime ? (
        /* 详情视图:头部(返回+封面+简介+分季快捷跳转)吸顶固定,滚动集数列表时始终可见,
           跟Excel冻结表头一个道理 */
        <div>
          <div
            ref={headerRef}
            className="sticky top-0 z-10 bg-ink px-8 pb-4 pt-8"
          >
            <div className="mb-6 flex items-center gap-2">
              <button
                onClick={handleBack}
                className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
              >
                <ArrowLeft size={14} /> 返回影视库
              </button>
              {/* 补番:只有已经匹配了bgm_id的番剧才有起点可以查关联作品 */}
              {selectedAnime.bgm_id && (
                <button
                  onClick={handleToggleRelatedAnime}
                  className={`rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                    showRelatedAnime
                      ? "border-vermillion bg-vermillion text-ink"
                      : "border-border text-muted hover:border-vermillion hover:text-vermillion"
                  }`}
                >
                  {showRelatedAnime ? "退出补番" : "补番"}
                </button>
              )}
              {!showRelatedAnime && (
                <button
                  onClick={() => {
                    setEpisodeManageMode((v) => !v);
                    setPendingDeleteEpisode(null);
                  }}
                  className={`rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                    episodeManageMode
                      ? "border-vermillion bg-vermillion text-ink"
                      : "border-border text-muted hover:border-vermillion hover:text-vermillion"
                  }`}
                >
                  {episodeManageMode ? "结束管理" : "管理"}
                </button>
              )}
              {episodeDeleteError && (
                <span className="font-mono text-[11px] text-vermillion">
                  删除失败: {episodeDeleteError}
                </span>
              )}
            </div>

            <div className="flex flex-col md:flex-row gap-6">
              <div className="w-40 aspect-[2/3] shrink-0 rounded-md bg-surface overflow-hidden shadow-lg">
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
              <div className="min-w-0">
                <h1 className="text-2xl font-bold mb-2">{selectedAnime.display_title || selectedAnime.folder_name}</h1>
                <p className="text-sm text-muted line-clamp-4 max-w-2xl">
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
                    className="rounded-md border border-border bg-surface px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
                  >
                    {seasonName}
                  </button>
                ))}
              </div>
            )}
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
                  onSelect={(bgmId) => onSelectAnime?.(bgmId)}
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
                    <h3 className="font-display text-lg mb-3 border-b border-border pb-1.5 text-vermillion">
                      {seasonName}
                    </h3>
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
                            <button
                              onClick={() => handlePlay(ep)}
                              className={`flex w-28 shrink-0 items-center justify-center gap-1.5 rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                                ep.is_watched
                                  ? "border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion"
                                  : "border-vermillion bg-vermillion text-ink hover:bg-vermillion/90"
                              }`}
                            >
                              <Play size={14} fill="currentColor" />
                              {ep.is_watched ? "再次播放" : "播放"}
                            </button>
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
        <div className="p-8">
          <div className="flex justify-between items-center mb-6">
            <div>
              <h1 className="font-display text-2xl tracking-tight">影视库</h1>
              <p className="font-mono text-xs text-muted mt-1">
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
              <button
                onClick={() => scanAndFetchAnimes()}
                className="rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
              >
                刷新 & 扫盘
              </button>
              <button
                onClick={() => {
                  setMatchMode((v) => !v);
                  setPendingDeleteFolder(null);
                }}
                className={`rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                  matchMode
                    ? "border-vermillion bg-vermillion text-ink"
                    : "border-border text-muted hover:border-vermillion hover:text-vermillion"
                }`}
              >
                {matchMode ? "结束管理" : "管理"}
              </button>
            </div>
          </div>

          {deleteAnimeError && (
            <div className="mb-4 rounded-md border border-vermillion/40 bg-surface p-3 font-mono text-xs text-vermillion">
              删除失败: {deleteAnimeError}
            </div>
          )}

          {loading ? (
            <div className="font-mono text-xs text-muted">正在加载...</div>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
              {displayedAnimes.map((anime) => (
                <div
                  key={anime.id}
                  onClick={() => handleSelectAnime(anime)}
                  className="group cursor-pointer"
                >
                  <div className="relative aspect-[2/3] overflow-hidden rounded-md bg-surface shadow-md">
                    {anime.cover_url ? (
                      <img
                        src={proxiedImageUrl(anime.cover_url)}
                        alt={anime.folder_name}
                        className="h-full w-full object-cover transition-transform group-hover:scale-105"
                      />
                    ) : (
                      <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted bg-surface border border-border">
                        <FolderOpen size={32} strokeWidth={1.5} />
                        <span className="text-[10px] font-mono">No Cover</span>
                      </div>
                    )}
                    {(matchMode || !anime.bgm_id) && (
                      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink/60">
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
          )}
        </div>
      )}
    </div>
  );
}