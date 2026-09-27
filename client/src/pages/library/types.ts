export interface LibraryAnime {
  id: number;
  folder_name: string;
  display_title: string;
  bgm_id: number | null;
  cover_url: string | null;
  summary: string;
  latest_activity_at: string | null;
  last_watched_at: string | null;
  // 未看集数角标。后端开关关闭，或该文件夹还没扫过集数时，恒为 0。
  unwatched_count: number;
}

export type SortMode = "default" | "recent_watched" | "recent_updated";

export interface RegroupMember {
  bgm_id: number;
  title: string;
  cover_url: string | null;
  platform: string | null;
  season_ordinal: string | null;
  folder_bucket: string | null;
  is_auto_root: boolean;
}

// 「归属」弹窗的数据：这个条目所在 Bangumi 家族的全部成员，以及当前生效的归属。
export interface RegroupCandidates {
  bgm_id: number;
  auto_root: { bgm_id: number; title: string };
  members: RegroupMember[];
  current_root: number;
  is_overridden: boolean;
}

// 一次归属调整的作用范围。bgmId 为 null 时，弹窗里让用户自己选是哪一部。
export interface RegroupTarget {
  label: string;
  relPaths: string[];
  bgmId: number | null;
}

export interface CoverCandidate {
  bgm_id: number;
  title: string;
  cover_url: string;
}

// 剧场版页的一行：一个独立剧场版/OVA 文件。前端按 bgm_id 收成卡片。
export interface StandaloneItem {
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

export interface Episode {
  filename: string;
  rel_path: string;
  is_watched?: boolean;
  watched_at?: string;
}

export interface AnimeDetail {
  folder_name: string;
  seasons: Record<string, Episode[]>;
  // 季度桶对应家族里的哪一部。只有「一个桶唯一对应一部」时后端才给。
  season_owners?: Record<string, { bgm_id: number; name: string }>;
  // 桶 → 该作品首播日期。作品名目录没有固定前缀，靠这个识别和排序。
  bucket_dates?: Record<string, string>;
}

// 详情头的背景图 / LOGO / 分级。status 不是 resolved 时，页面走同一套降级，不区分「还在查」和「查不到」。
export interface AnimeMeta {
  bgm_id: number;
  status: "pending" | "resolved" | "unresolved_retry" | "unresolved_permanent";
  tmdb_id?: number | null;
  backdrop_url?: string | null;
  logo_url?: string | null;
  backdrop_custom?: boolean;
  logo_custom?: boolean;
  content_rating?: string | null;
  genres?: string[];
  tags?: string[];
  studios?: string[];
  creators?: string[];
}

export type MetaScope = "library" | "movie";
export type ImageKind = "backdrop" | "logo";

export interface ImageCandidate {
  url: string;
  thumb: string;
  width?: number;
  height?: number;
  lang?: string | null;
}

export interface AppSettings {
  library_root: string;
  potplayer_path: string;
  player_mode: "builtin" | "external";
  library_unwatched_badge_enabled: boolean;
}

export const DEFAULT_SETTINGS: AppSettings = {
  library_root: "D:\\AnimeLibrary",
  potplayer_path: "C:\\Program Files\\DAUM\\PotPlayer\\PotPlayer64.exe",
  player_mode: "external",
  library_unwatched_badge_enabled: true,
};

// 详情头真图横幅高度。比海报（约 240px）更高，脸不容易刚好被裁在边上。
export const HERO_BANNER_HEIGHT = "26rem";
