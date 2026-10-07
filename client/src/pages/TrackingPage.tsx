import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { proxiedImageUrl } from "../utils/proxiedImage";
import { pageSub, pageTitle, posterFrame, seg, segOff, segOn } from "./library/ui";

interface ScheduleItem {
  bgm_id: number | null;
  title: string;
  weekday: number; // 0 = 周一 ... 6 = 周日
  cover_url: string | null;
  total_eps: number | null;
  score: number | null;
}

interface TrackingPageProps {
  onSelectAnime: (bgmId: number) => void;
}

import { API_BASE } from "../api";
const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

// v3:过滤国漫的判断依据从name/name_cn启发式换成Bangumi官方meta_tags产地标签,
// 筛出来的番剧集合跟v2不完全一样,升级key让所有客户端上v2版本的旧缓存自动失效。
const SCHEDULE_CACHE_KEY = "tracking_schedule_cache_v3";
// 后端现在有AnimeOriginCache持久化缓存兜底,同一批bgm_id只有第一次会触发慢查询
// (~15秒/100+部番),后续都是数据库直接命中、接近瞬间返回,不需要再靠拉长TTL
// 来避免慢请求——1小时能让新定档/临时补漏的番剧更快反映到界面,同时避免用户
// 每次切换页面都触发一次网络请求。
const SCHEDULE_CACHE_TTL_MS = 60 * 60 * 1000;

type LayoutMode = "vertical" | "horizontal";

// 进详情会卸掉这一页。滚动位置留在 sessionStorage，回来再摆回去。
// 横版内容比纵版高。先按纵版画的话，滚动条会被掐短，横版位置就丢了。
const TRACKING_LAYOUT_CACHE_KEY = "tracking_layout_cache_v1";
const TRACKING_SCROLL_KEY = "tracking_scroll_top_v1";

function readLayoutCache(): LayoutMode {
  try {
    const value = sessionStorage.getItem(TRACKING_LAYOUT_CACHE_KEY);
    if (value === "vertical" || value === "horizontal") return value;
  } catch {
    // 读不到就先按纵向，等接口结果
  }
  return "vertical";
}

function rememberLayout(mode: LayoutMode) {
  try {
    sessionStorage.setItem(TRACKING_LAYOUT_CACHE_KEY, mode);
  } catch {
    // 存不下不影响这次显示
  }
}

function readScrollMap(): Record<LayoutMode, number> {
  try {
    const raw = sessionStorage.getItem(TRACKING_SCROLL_KEY);
    if (!raw) return { vertical: 0, horizontal: 0 };
    const parsed = JSON.parse(raw) as Partial<Record<LayoutMode, number>>;
    return {
      vertical: Number(parsed.vertical) || 0,
      horizontal: Number(parsed.horizontal) || 0,
    };
  } catch {
    return { vertical: 0, horizontal: 0 };
  }
}

function writeScroll(mode: LayoutMode, top: number) {
  const map = readScrollMap();
  map[mode] = top;
  try {
    sessionStorage.setItem(TRACKING_SCROLL_KEY, JSON.stringify(map));
  } catch {
    // 存不下就只留在这一次挂载里
  }
}

function loadCachedSchedule(): ScheduleItem[] | null {
  try {
    const raw = localStorage.getItem(SCHEDULE_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { data: ScheduleItem[]; ts: number };
    if (Date.now() - parsed.ts > SCHEDULE_CACHE_TTL_MS) return null;
    return parsed.data;
  } catch {
    return null;
  }
}

function saveCachedSchedule(data: ScheduleItem[]) {
  try {
    localStorage.setItem(
      SCHEDULE_CACHE_KEY,
      JSON.stringify({ data, ts: Date.now() }),
    );
  } catch {
    // 存储满/被禁用时降级为纯内存,不影响本次渲染
  }
}

export default function TrackingPage({ onSelectAnime }: TrackingPageProps) {
  const initialCache = useMemo(loadCachedSchedule, []);
  const [schedule, setSchedule] = useState<ScheduleItem[]>(initialCache ?? []);
  const [loading, setLoading] = useState(initialCache === null);
  // 纵向是七列。横向把七天都按大卡片铺开。
  // 先用上次的排列，避免回来时先画纵版把横版滚动条掐短。
  const [layout, setLayout] = useState<LayoutMode>(readLayoutCache);
  const listRef = useRef<HTMLDivElement>(null);
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const scrollSaveRaf = useRef<number | null>(null);
  // 内容还没出来时不要把已记住的位置写成 0。
  const scrollRestored = useRef(false);

  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const top = readScrollMap()[layout];
    if (schedule.length === 0 && top > 0) return;
    scrollRestored.current = true;
    el.scrollTop = top;
  }, [layout, schedule.length]);

  const flushScroll = () => {
    if (scrollSaveRaf.current !== null) {
      cancelAnimationFrame(scrollSaveRaf.current);
      scrollSaveRaf.current = null;
    }
    const el = listRef.current;
    if (!el || !scrollRestored.current) return;
    writeScroll(layoutRef.current, el.scrollTop);
  };

  const handleListScroll = () => {
    if (scrollSaveRaf.current !== null) return;
    scrollSaveRaf.current = requestAnimationFrame(() => {
      scrollSaveRaf.current = null;
      flushScroll();
    });
  };

  useEffect(() => {
    fetch(`${API_BASE}/tracking/layout`)
      .then((res) => res.json())
      .then((data) => {
        if (data?.mode === "vertical" || data?.mode === "horizontal") {
          rememberLayout(data.mode);
          setLayout(data.mode);
        }
      })
      .catch(() => {});
  }, []);

  const changeLayout = (next: LayoutMode) => {
    flushScroll();
    rememberLayout(next);
    setLayout(next);
    fetch(`${API_BASE}/tracking/layout`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: next }),
    }).catch((err: unknown) => console.error("保存追更排列失败", err));
  };

  useEffect(() => {
    // 命中前台缓存(6小时内)时直接用,跳过网络请求,切回追更页不用等
    if (initialCache !== null) return;

    fetch(`${API_BASE}/bangumi/schedule`)
      .then((res) => res.json())
      .then((data) => {
        setSchedule(data);
        saveCachedSchedule(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const grouped = useMemo(
    () => days.map((_, index) => schedule.filter((a) => a.weekday === index)),
    [schedule],
  );

  return (
    <div className="absolute inset-0 flex flex-col overflow-hidden">
      {/* 标题固定。纵向时星期也固定，滚动条只在海报区。 */}
      <div className="shrink-0 bg-ink px-8 pb-2 pt-8">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className={pageTitle}>追更</h1>
            <p className={pageSub}>
              {loading ? "正在获取新番连载时刻表..." : `本季连载 ${schedule.length} 部`}
            </p>
          </div>
          <div className={`${seg} shrink-0`}>
            <button
              type="button"
              onClick={() => changeLayout("vertical")}
              className={layout === "vertical" ? segOn : segOff}
            >
              纵向
            </button>
            <button
              type="button"
              onClick={() => changeLayout("horizontal")}
              className={layout === "horizontal" ? segOn : segOff}
            >
              横向
            </button>
          </div>
        </div>

        {layout === "vertical" && (
          <div className="grid grid-cols-7 gap-3 border-b border-border pb-2">
            {days.map((day) => (
              <div key={day} className="text-sm text-muted">
                {day}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 只挂当前这种排列。封面走浏览器缓存，切换不必把每张图挂两份。 */}
      <div
        ref={listRef}
        onScroll={handleListScroll}
        onPointerDown={flushScroll}
        className="min-h-0 flex-1 overflow-y-auto px-8 pb-8 pt-3"
      >
        {layout === "vertical" ? (
          <div className="grid grid-cols-7 gap-4">
            {grouped.map((items, index) => (
              <div key={index} className="flex flex-col gap-4">
                {items.map((anime) => (
                  <AnimeCard key={anime.bgm_id ?? anime.title} anime={anime} onSelectAnime={onSelectAnime} />
                ))}
                {!loading && items.length === 0 && <div className="pt-2 text-xs text-muted/50">—</div>}
              </div>
            ))}
          </div>
        ) : (
          <div className="flex flex-col gap-8">
            {grouped.map((items, index) => (
              <section key={index}>
                <div className="mb-3 border-b border-border pb-2 text-sm text-muted">{days[index]}</div>
                {items.length > 0 ? (
                  <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
                    {items.map((anime) => (
                      <AnimeCard key={anime.bgm_id ?? anime.title} anime={anime} onSelectAnime={onSelectAnime} />
                    ))}
                  </div>
                ) : (
                  !loading && <div className="text-xs text-muted">这天没有连载番剧</div>
                )}
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AnimeCard({
  anime,
  onSelectAnime,
}: {
  anime: ScheduleItem;
  onSelectAnime: (id: number) => void;
}) {
  // 0 用原地址，1 再请求一次，2 放弃并留下底色。
  const [attempt, setAttempt] = useState(0);
  const base = anime.cover_url ? proxiedImageUrl(anime.cover_url) : undefined;
  const src = base && attempt === 1 ? `${base}&retry=1` : base;

  return (
    <div
      onClick={() => anime.bgm_id && onSelectAnime(anime.bgm_id)}
      className={anime.bgm_id ? "cursor-pointer" : "cursor-default opacity-60"}
    >
      <div className={posterFrame}>
        {/* 内部滚动区里不要加 loading=lazy，WebView2 经常不开始请求。 */}
        {src && attempt < 2 && (
          <img
            src={src}
            alt=""
            onError={() => setAttempt((current) => current + 1)}
            className="h-full w-full object-cover"
          />
        )}
      </div>
      <div className="mt-2 text-sm font-medium leading-5 break-words">
        {anime.title}
        {anime.score != null && <span className="font-normal text-score"> ★ {anime.score.toFixed(1)}</span>}
        {anime.total_eps ? <span className="font-normal text-muted"> · 全{anime.total_eps}话</span> : null}
      </div>
    </div>
  );
}
