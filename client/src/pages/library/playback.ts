import { invoke, isTauri } from "@tauri-apps/api/core";
import { API_BASE } from "../../api";
import type { AppSettings } from "./types";

// 点播放和播放器连播都会进来。1 秒内的重复点击丢掉，避免连续拉起两个播放器。
export function beginPlay(lock: { current: boolean }): boolean {
  if (lock.current) return false;
  lock.current = true;
  return true;
}

export function endPlay(lock: { current: boolean }): void {
  setTimeout(() => {
    lock.current = false;
  }, 1000);
}

export function postWatch(folderName: string, filename: string, relPath?: string): void {
  fetch(`${API_BASE}/library/watch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder_name: folderName, filename, rel_path: relPath ?? null }),
  }).catch((err: unknown) => console.error("标记进度失败", err));
}

// 内置播放器吃整份路径做连播；外置播放器只开第一条。
export async function openPlayer(settings: AppSettings, paths: string[]): Promise<void> {
  if (paths.length === 0) return;
  if (!(await isTauri())) {
    if (settings.player_mode === "builtin") {
      console.log("[开发调试] 模拟内置mpv播放列表:", paths);
    } else {
      console.log(`[开发调试] 模拟拉起播放器:\n播放器: ${settings.potplayer_path}\n视频: "${paths[0]}"`);
    }
    return;
  }
  if (settings.player_mode === "builtin") {
    await invoke("open_builtin_player", { videoPaths: paths }).catch((err: unknown) =>
      console.error("拉起内置播放器失败:", err),
    );
    return;
  }
  await invoke("open_external_player", {
    videoPath: paths[0],
    playerPath: settings.potplayer_path,
  }).catch((err: unknown) => console.error("唤起播放器失败:", err));
}
