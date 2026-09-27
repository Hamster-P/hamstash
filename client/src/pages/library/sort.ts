import type { CoverCandidate, LibraryAnime, SortMode } from "./types";

// 常规 TV 季（Season 01、02…）是正片，不给「添加到剧场版模式」。
// Season 00、剧场版、OVA、Other、Specials 都算非常规季。
export function isRegularSeasonBucket(name: string): boolean {
  const matched = /^season\s*(\d+)$/i.exec(name.trim());
  return matched ? parseInt(matched[1], 10) >= 1 : false;
}

// 整理后的文件名里，S04E18 / 第18话 提出来当主标题。
// 认不出就返回 null，调用方继续显示完整文件名。
export function episodeLabel(filename: string): string | null {
  const seasonEp = filename.match(/S\d{1,3}E(\d{1,4})/i);
  if (seasonEp) return `第${Number(seasonEp[1])}话`;
  const spoken = filename.match(/第\s*(\d{1,4})\s*[话集]/);
  if (spoken) return `第${Number(spoken[1])}话`;
  return null;
}

// 从整理后的文件名取出标题，当作「添加到剧场版」的默认检索词。
// 去掉扩展名、[字幕组] / 【】 / () 标签，再把空白折成一格。
export function cleanTitleFromFilename(filename: string): string {
  return filename
    .replace(/\.[^.]+$/, "")
    .replace(/[[【(（][^\]】)）]*[\]】)）]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const TIER_SEASON = 0;
const TIER_WORK_TITLE = 1;
const TIER_MOVIE = 2;
const TIER_OVA = 3;
const TIER_OTHER = 4;

// 分季顺序：TV 季从新到旧，然后是作品名目录、剧场版、OVA，兜底桶最后。
// Season 00 是老库里算不出季号的桶，跟 OVA 放一起。
// 作品名目录没有固定前缀，能在 bucketDates 里查到首播日才算这一档，并按日期排。
function seasonSortKey(name: string, bucketDates?: Record<string, string>): [number, number, string] {
  const seasonMatch = name.match(/^season\s*(\d+)/i);
  if (seasonMatch) {
    const num = parseInt(seasonMatch[1], 10);
    if (num === 0) return [TIER_OVA, 0, ""];
    return [TIER_SEASON, -num, ""];
  }
  if (/剧场版|劇場版|movie/i.test(name)) return [TIER_MOVIE, 0, ""];
  if (/\bova\b/i.test(name)) return [TIER_OVA, 0, ""];
  const date = bucketDates?.[name];
  if (date) return [TIER_WORK_TITLE, 0, date];
  return [TIER_OTHER, 0, ""];
}

export function compareSeasonNames(a: string, b: string, bucketDates?: Record<string, string>): number {
  const [tierA, subA, dateA] = seasonSortKey(a, bucketDates);
  const [tierB, subB, dateB] = seasonSortKey(b, bucketDates);
  if (tierA !== tierB) return tierA - tierB;
  if (subA !== subB) return subA - subB;
  if (dateA !== dateB) return dateA < dateB ? -1 : 1;
  return a.localeCompare(b, undefined, { numeric: true });
}

// 排序只用列表接口已经带回的时间字段，不为此再请求后端。
// default 保留接口原来的顺序。
export function sortAnimes(list: LibraryAnime[], mode: SortMode): LibraryAnime[] {
  if (mode === "default") return list;
  const key: keyof LibraryAnime = mode === "recent_watched" ? "last_watched_at" : "latest_activity_at";
  return [...list].sort((a, b) => {
    const aTime = a[key] ? new Date(a[key] as string).getTime() : 0;
    const bTime = b[key] ? new Date(b[key] as string).getTime() : 0;
    return bTime - aTime;
  });
}

// 封面候选和「Bangumi 介绍」选季都按 bgm_id 从新到旧，不沿用缓存里的默认顺序。
export function sortCoverCandidates(list: CoverCandidate[]): CoverCandidate[] {
  return [...list].sort((a, b) => b.bgm_id - a.bgm_id);
}
