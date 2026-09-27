import { useEffect, useState } from "react";
import { API_BASE } from "../../api";
import { DEFAULT_SETTINGS, type AppSettings } from "./types";

export function useLibrarySettings(): AppSettings {
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);

  useEffect(() => {
    fetch(`${API_BASE}/settings`)
      .then((res) => {
        if (!res.ok) throw new Error("设置接口未就绪");
        return res.json();
      })
      .then((data) => {
        setSettings((prev) => ({
          library_root: data.library_root || prev.library_root,
          potplayer_path: data.potplayer_path || prev.potplayer_path,
          player_mode: data.player_mode === "builtin" ? "builtin" : "external",
          library_unwatched_badge_enabled: data.library_unwatched_badge_enabled !== false,
        }));
      })
      .catch((err: unknown) => {
        console.warn("无法从后端获取配置，尝试使用 localStorage 兜底", err);
        const localRoot = localStorage.getItem("library_root");
        const localPlayer = localStorage.getItem("potplayer_path");
        if (localRoot || localPlayer) {
          setSettings((prev) => ({
            ...prev,
            library_root: localRoot || prev.library_root,
            potplayer_path: localPlayer || prev.potplayer_path,
          }));
        }
      });
  }, []);

  return settings;
}
