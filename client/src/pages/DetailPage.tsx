import { useEffect, useState } from "react";
import { ArrowLeft, Download, Rss, ExternalLink, Check } from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";

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

export default function DetailPage({
  bgmId,
  onBack,
  onNavigateToDownload,
  manualMatchFolder,
  onConfirmMatch,
}: DetailPageProps) {
  const [detail, setDetail] = useState<AnimeDetail | null>(null);
  const [loading, setLoading] = useState(true);

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
                src={detail.cover_url}
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

      {/* 直接内嵌Bangumi官网页面 */}
      <div className="min-h-0 flex-1 overflow-hidden rounded-md border border-border bg-white">
        <iframe
          src={bgmUrl}
          title="Bangumi详情"
          className="h-full w-full border-none"
        />
      </div>
    </div>
  );
}
