import { API_BASE } from "../../api";
import type { AnimeMeta, MetaScope } from "./types";

// 手选的背景图和 LOGO 按页面分开记。
// 剧场版用 scope=movie，媒体库详情用 scope=library，互不覆盖。
export function loadAnimeMeta(bgmId: number, scope: MetaScope): Promise<AnimeMeta | null> {
  return fetch(`${API_BASE}/anime-meta/${bgmId}?scope=${scope}`)
    .then((res) => res.json())
    .catch(() => null);
}
