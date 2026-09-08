import { invoke, isTauri } from "@tauri-apps/api/core";

// 后端服务地址。端口默认 17420(避开 qBittorrent WebUI 默认的 8080),用户可在设置页改。
// 端口写在 server 的 settings.ini 里,Rust 侧启动时读它、通过 api_port 命令暴露给前端。
//
// 这是个 let 导出:initApiBase() 在 React 渲染前把它改成真实值,ESM live binding 保证
// 所有 import 方之后读到的都是更新后的值。浏览器 dev(非 Tauri)或命令失败时保持兜底。
export let API_BASE = "http://127.0.0.1:17420";

export async function initApiBase(): Promise<void> {
  if (!isTauri()) return;
  try {
    const port = await invoke<number>("api_port");
    if (typeof port === "number" && port > 0) {
      API_BASE = `http://127.0.0.1:${port}`;
    }
  } catch {
    // 拿不到就用兜底端口,不阻断启动
  }
}
