import { useEffect, useMemo, useState } from "react";

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

const API_BASE = "http://127.0.0.1:8080";
const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const SCHEDULE_CACHE_KEY = "tracking_schedule_cache_v1";
const SCHEDULE_CACHE_TTL_MS = 6 * 60 * 60 * 1000; // 6小时,过期后重新用API取数更新

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
  // null = 一周总览; 数字 = 只看这一天(点星期标题切换)
  const [focusedDay, setFocusedDay] = useState<number | null>(null);

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

  const visibleDayIndexes =
    focusedDay === null ? days.map((_, i) => i) : [focusedDay];

  return (
    <div>
      {/* 顶部说明 + 星期表头,吸顶固定,滚动内容时始终可见 */}
      <div className="sticky top-0 z-10 bg-ink px-8 pb-2 pt-8">
        <h1 className="mb-1 font-display text-2xl tracking-tight">追更</h1>
        <p className="mb-4 font-mono text-xs text-muted">
          {loading
            ? "正在获取新番连载时刻表..." // 2. 文案优化，因为不再需要缓慢的"解析"
            : `本季连载 ${schedule.length} 部 · ${
                focusedDay !== null
                  ? "点击星期标题返回一周视图"
                  : "点击星期标题只看当天"
              }`}
        </p>

        <div
          className="grid gap-3 border-b border-border pb-2"
          style={{
            gridTemplateColumns:
              focusedDay === null ? "repeat(7, 1fr)" : "1fr",
          }}
        >
          {visibleDayIndexes.map((index) => (
            <button
              key={index}
              onClick={() => setFocusedDay(focusedDay === index ? null : index)}
              className={`text-left font-display text-sm transition-colors ${
                focusedDay === index
                  ? "text-vermillion"
                  : "text-muted hover:text-paper"
              }`}
            >
              {days[index]}
            </button>
          ))}
        </div>
      </div>

      {/*
        内容区:总览 + 7个单日面板,一次性全部预先渲染好并常驻DOM,
        非活跃面板用 h-0 overflow-hidden 包裹(而不是display:none/条件挂载)。
        overflow:hidden只裁剪绘制,不影响内部布局计算和图片解码,
        所以7天内容依然是"提前加载完、缓存着",切换纯粹是显隐,没有
        重新挂载/重新解码的卡顿;同时h-0确保裁剪掉的内容不会像
        position:absolute那样撑大最近滚动祖先的可滚动区域。
      */}
      <div className="px-8 pb-8 pt-3">
        {/* 总览:7列海报卡片 */}
        <div className={focusedDay === null ? "" : "h-0 overflow-hidden"}>
          <div className="grid grid-cols-7 gap-3">
            {grouped.map((items, index) => (
              <div key={index} className="flex flex-col gap-3">
                {items.map((anime) => (
                  <AnimeCard
                    key={anime.bgm_id}
                    anime={anime}
                    onSelectAnime={onSelectAnime}
                  />
                ))}
                {!loading && items.length === 0 && (
                  <div className="pt-2 font-mono text-[11px] text-muted/50">
                    —
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* 每一天的聚焦海报墙,7份都提前渲染好,只切换显示哪一份;
            列数/卡片尺寸跟总览保持一致,只是这一天的番剧铺满7列 */}
        {grouped.map((items, index) => (
          <div
            key={index}
            className={focusedDay === index ? "" : "h-0 overflow-hidden"}
          >
            <div className="grid grid-cols-7 gap-3">
              {items.map((anime) => (
                <AnimeCard
                  key={anime.bgm_id}
                  anime={anime}
                  onSelectAnime={onSelectAnime}
                />
              ))}
            </div>
            {!loading && items.length === 0 && (
              <div className="font-mono text-xs text-muted">
                这天没有连载番剧
              </div>
            )}
          </div>
        ))}
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
  const [loaded, setLoaded] = useState(false);

  return (
    <div
      onClick={() => anime.bgm_id && onSelectAnime(anime.bgm_id)}
      className={`flex flex-col gap-1.5 rounded-md bg-surface p-1.5 transition-colors ${
        anime.bgm_id
          ? "cursor-pointer hover:bg-surface-hover"
          : "cursor-default opacity-60"
      }`}
    >
      <div className="aspect-[2/3] w-full shrink-0 overflow-hidden rounded bg-ink">
        {anime.cover_url && (
          <img
            src={anime.cover_url}
            alt=""
            loading="lazy"
            onLoad={() => setLoaded(true)}
            className={`h-full w-full object-cover transition-opacity duration-300 ${
              loaded ? "opacity-100" : "opacity-0"
            }`}
          />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="line-clamp-2 text-xs leading-snug">{anime.title}</div>
        <div className="mt-1 flex items-center gap-2">
          {anime.total_eps && (
            <div className="font-mono text-[11px] text-vermillion">
              全{anime.total_eps}话
            </div>
          )}
          {anime.score !== undefined && anime.score !== null && (
            <div className="font-mono text-[11px] text-amber-500 font-bold">
              ⭐ {anime.score.toFixed(1)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
