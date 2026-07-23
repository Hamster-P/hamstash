import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Download, Rss, ExternalLink, Check } from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { proxiedImageUrl } from "../utils/proxiedImage";

interface AnimeDetail {
  bgm_id: number;
  title: string;
  title_original: string | null;
  cover_url: string | null;
  air_date: string | null;
  total_eps: number | null;
}

interface DetailPageProps {
  bgmId: number;
  onBack: () => void;
  onNavigateToDownload: (
    keyword: string,
    bgmId: number | null,
    subscribe: boolean,
  ) => void;
  manualMatchFolder?: string | null;
  onConfirmMatch?: (bgmId: number) => void;
}

const API_BASE = "http://127.0.0.1:8080";

// 内嵌webview多久没报告"页面加载完成"就认为加载不出来。给得比较宽松,
// 因为走代理连bgm.tv本来就慢,不能刚过几秒就误报。
const EMBED_LOAD_TIMEOUT_MS = 12000;

export default function DetailPage({
  bgmId,
  onBack,
  onNavigateToDownload,
  manualMatchFolder,
  onConfirmMatch,
}: DetailPageProps) {
  const [detail, setDetail] = useState<AnimeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [embedError, setEmbedError] = useState<string | null>(null);
  const embedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLoading(true);
    fetch(`${API_BASE}/bangumi/detail/${bgmId}`)
      .then((res) => res.json())
      .then((data) => {
        setDetail(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [bgmId]);

  // 内嵌Bangumi详情页:挂载/切换到另一部番时,量出占位区域在屏幕上的实际位置,
  // 让Rust侧的子webview导航过去、对齐到这块区域(子webview已存在且代理没变时会直接
  // 复用+导航,不重新创建)。这里的cleanup只收拾本轮的超时计时器和事件监听,
  // 销毁webview是下面那个只在真正离开详情页时触发的effect的事。
  useEffect(() => {
    const el = embedRef.current;
    if (!el) return;
    let cancelled = false;
    let timeoutId: number | undefined;

    // 先挂监听再发创建命令:两边都是异步IPC,反过来的话页面加载得快时
    // "加载完成"事件可能赶在监听注册好之前发出,白白误报一次超时。
    const unlisten = listen("bgm-embed-loaded", () => {
      window.clearTimeout(timeoutId);
    });

    const rect = el.getBoundingClientRect();
    setEmbedError(null);
    invoke("embed_bgm_webview", {
      bgmId,
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    })
      .then(() => {
        if (cancelled) return;
        // 上面量的rect是挂载那一刻的:那时detail还没回来,简要信息条(依赖detail才渲染)
        // 还不存在,占位区比最终布局更高更大。等命令返回时布局多半已经变过了,
        // 这里再对齐一次,免得webview停在过期坐标上、盖住上面的信息条。
        const current = el.getBoundingClientRect();
        invoke("resize_bgm_webview", {
          x: current.x,
          y: current.y,
          width: current.width,
          height: current.height,
        }).catch(() => {});
      })
      .catch((err) => {
        if (cancelled) return;
        setEmbedError(typeof err === "string" ? err : "内嵌Bangumi详情页失败");
      });

    // 内嵌webview是原生图层,加载不出来时就是一块没有任何反馈的白框(命令本身是成功的,
    // 失败发生在它自己去连bgm.tv的时候)。这里靠Rust侧在页面加载完成时发的事件来确认,
    // 超时还没等到就给一句可操作的提示,而不是让用户对着白框猜。
    timeoutId = window.setTimeout(() => {
      if (!cancelled) {
        setEmbedError("Bangumi详情页加载超时,国内网络可能需要在设置里配置代理地址");
      }
    }, EMBED_LOAD_TIMEOUT_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
      unlisten.then((off) => off()).catch(() => {});
    };
  }, [bgmId]);

  // 占位区域尺寸变化(比如窗口resize)时同步内嵌webview的位置大小;
  // 只在真正离开详情页(组件卸载)时才销毁内嵌webview,不能跟着bgmId变化触发,
  // 否则每次切番剧都会把webview关掉重建,白白丢掉"直接导航复用"的好处。
  useEffect(() => {
    const el = embedRef.current;
    if (!el) return;

    const syncBounds = () => {
      const rect = el.getBoundingClientRect();
      invoke("resize_bgm_webview", {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      }).catch(() => {});
    };

    const resizeObserver = new ResizeObserver(syncBounds);
    resizeObserver.observe(el);
    window.addEventListener("resize", syncBounds);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener("resize", syncBounds);
      invoke("close_bgm_webview").catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const bgmUrl = `https://bgm.tv/subject/${bgmId}`;
  const searchKeyword = detail?.title ?? "";

  return (
    <div className="flex h-full flex-col p-8">
      {/* 顶部操作区 */}
      <div className="mb-4 flex shrink-0 items-center gap-2 rounded-md border border-border bg-surface px-4 py-3">
        {manualMatchFolder ? (
          <button
            onClick={() => onConfirmMatch?.(bgmId)}
            disabled={!detail}
            className="flex items-center gap-1.5 rounded border border-vermillion px-3 py-1.5 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
          >
            <Check size={14} />
            确认匹配
          </button>
        ) : (
          <>
            <button
              onClick={() => onNavigateToDownload(searchKeyword, bgmId, false)}
              disabled={!detail}
              className="flex items-center gap-1.5 rounded border border-border px-3 py-1.5 font-mono text-xs text-paper transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
            >
              <Download size={14} />
              下载
            </button>
            <button
              onClick={() => onNavigateToDownload(searchKeyword, bgmId, true)}
              disabled={!detail}
              className="flex items-center gap-1.5 rounded border border-border px-3 py-1.5 font-mono text-xs text-paper transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
            >
              <Rss size={14} />
              RSS订阅
            </button>
          </>
        )}
        <button
          onClick={() => openUrl(bgmUrl)}
          className="ml-auto flex items-center gap-1.5 rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:text-paper"
        >
          <ExternalLink size={14} />
          在浏览器打开
        </button>
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
        >
          <ArrowLeft size={14} />
          返回
        </button>
      </div>

      {/* 简要信息条 */}
      {!loading && detail && (
        <div className="mb-3 flex shrink-0 items-center gap-3">
          <div className="h-16 w-11 shrink-0 overflow-hidden rounded bg-surface">
            {detail.cover_url && (
              <img
                src={proxiedImageUrl(detail.cover_url)}
                alt=""
                className="h-full w-full object-cover"
              />
            )}
          </div>
          <div className="min-w-0">
            <div className="truncate font-display text-lg tracking-tight">
              {detail.title}
            </div>
            <div className="font-mono text-[11px] text-muted">
              {detail.air_date || "—"} ·{" "}
              {detail.total_eps ? `全${detail.total_eps}话` : "集数未知"}
            </div>
          </div>
        </div>
      )}

      {/* 内嵌Bangumi详情页:这个div本身不渲染iframe,只用来占位/量尺寸——
          真正的内容是Rust侧创建的原生子webview,精确覆盖在这块区域上面
          (可以单独设代理,不会像iframe那样绕开后端代理配置)。 */}
      <div
        ref={embedRef}
        className="relative min-h-0 flex-1 rounded-md border border-border bg-surface"
      >
        {embedError && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-4 text-center">
            <p className="font-mono text-xs text-vermillion">{embedError}</p>
            <button
              onClick={() => openUrl(bgmUrl)}
              className="flex items-center gap-1.5 rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
            >
              <ExternalLink size={14} />
              在浏览器打开
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
