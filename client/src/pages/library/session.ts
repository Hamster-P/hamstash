import type { BangumiSubject } from "../../components/BangumiResultsList";
import type { LibraryAnime } from "./types";

// 从补番或 Bangumi 介绍跳到顶层详情页时，媒体库页会被卸掉。
// 组件内的 state 保不住，回来靠 sessionStorage 还原。对齐搜索页的做法。
const LIBRARY_DETAIL_SESSION_KEY = "library_detail_session_v1";

export interface LibraryDetailSession {
  anime: LibraryAnime;
  relatedAnime: BangumiSubject[];
  gridScrollTop: number;
  relatedScrollTop: number;
  mode: "related" | "self";
}

export function loadLibraryDetailSession(): LibraryDetailSession | null {
  try {
    const raw = sessionStorage.getItem(LIBRARY_DETAIL_SESSION_KEY);
    return raw ? (JSON.parse(raw) as LibraryDetailSession) : null;
  } catch {
    return null;
  }
}

export function saveLibraryDetailSession(session: LibraryDetailSession): void {
  try {
    sessionStorage.setItem(LIBRARY_DETAIL_SESSION_KEY, JSON.stringify(session));
  } catch {
    // 隐私模式或配额满时存不下，退回网格即可。
  }
}

export function clearLibraryDetailSession(): void {
  try {
    sessionStorage.removeItem(LIBRARY_DETAIL_SESSION_KEY);
  } catch {
    // 清不掉也不影响下一次正常进入。
  }
}

// 开发模式会把组件装上、卸掉、再装上。
// 第一次读的时候存储已经被清掉，第二次还得拿得到同一次的内容。
// 这次渲染结束后丢掉，避免下次正常打开影视库又跳回详情。
let handoff: LibraryDetailSession | null = null;
let handoffTimer: ReturnType<typeof setTimeout> | null = null;

export function takeLibraryDetailSession(): LibraryDetailSession | null {
  const loaded = loadLibraryDetailSession();
  if (loaded) {
    clearLibraryDetailSession();
    handoff = loaded;
    if (handoffTimer) clearTimeout(handoffTimer);
    handoffTimer = setTimeout(() => {
      handoff = null;
      handoffTimer = null;
    }, 0);
    return loaded;
  }
  return handoff;
}
