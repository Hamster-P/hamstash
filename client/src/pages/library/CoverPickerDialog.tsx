import { useEffect, useState } from "react";
import { proxiedImageUrl } from "../../utils/proxiedImage";
import { API_BASE } from "../../api";
import { sortCoverCandidates } from "./sort";
import type { CoverCandidate } from "./types";

// 封面和「Bangumi 介绍」选季共用网格。
// cover：点一张写成该文件夹的自定义封面。intro：点一张跳到那一部的介绍页。
export default function CoverPickerDialog({
  mode,
  folderName,
  bgmId,
  introCandidates,
  onClose,
  onIntroPick,
  onCoverApplied,
}: {
  mode: "cover" | "intro";
  folderName: string | null;
  bgmId: number | null;
  introCandidates: CoverCandidate[];
  onClose: () => void;
  onIntroPick?: (bgmId: number) => void;
  onCoverApplied?: () => void;
}) {
  const [candidates, setCandidates] = useState<CoverCandidate[]>(mode === "intro" ? introCandidates : []);
  const [loading, setLoading] = useState(mode === "cover");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (mode !== "cover" || bgmId == null) return;
    let cancelled = false;
    fetch(`${API_BASE}/library/cover-candidates/${bgmId}`)
      .then((res) => res.json())
      .then((data) => {
        if (!cancelled) setCandidates(sortCoverCandidates(data?.data ?? []));
      })
      .catch(() => {
        if (!cancelled) setCandidates([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, bgmId]);

  const applyCover = async (nextBgmId: number | null) => {
    if (!folderName) return;
    setBusy(true);
    setError(null);
    try {
      const url = `${API_BASE}/library/${encodeURIComponent(folderName)}/cover`;
      const res =
        nextBgmId === null
          ? await fetch(url, { method: "DELETE" })
          : await fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ bgm_id: nextBgmId }),
            });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onCoverApplied?.();
      onClose();
    } catch (err) {
      console.error("设置封面失败", err);
      setError(err instanceof Error ? `设置封面失败:${err.message}` : "设置封面失败,请重试");
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-md border border-border bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm">{mode === "intro" ? "选择要查看的季度 / 作品" : "选择媒体库封面"}</div>
          <div className="flex items-center gap-2 font-mono text-[11px]">
            {mode === "cover" && (
              <button
                onClick={() => applyCover(null)}
                disabled={busy}
                className="rounded border border-border px-2 py-1 text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
              >
                恢复默认
              </button>
            )}
            <button
              onClick={onClose}
              className="rounded border border-border px-2 py-1 text-muted transition-colors hover:text-paper"
            >
              关闭
            </button>
          </div>
        </div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          {mode === "intro"
            ? "该系列有多部作品,点选一部查看它的 Bangumi 介绍。"
            : "从该系列家族的全部作品里挑一张作为封面;“恢复默认”按设置里的默认封面策略自动选择。"}
        </p>
        {error && (
          <div className="mb-3 rounded border border-vermillion/40 bg-ink p-2 font-mono text-[11px] text-vermillion">
            {error}
          </div>
        )}
        {loading ? (
          <div className="py-10 text-center font-mono text-xs text-muted">正在加载家族封面...</div>
        ) : candidates.length === 0 ? (
          <div className="py-10 text-center font-mono text-xs text-muted">没有可选的家族封面。</div>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-3 overflow-y-auto">
            {candidates.map((candidate) => (
              <button
                key={candidate.bgm_id}
                disabled={busy}
                onClick={() => {
                  if (mode === "intro") {
                    onClose();
                    onIntroPick?.(candidate.bgm_id);
                    return;
                  }
                  applyCover(candidate.bgm_id);
                }}
                className="group flex flex-col gap-1 text-left disabled:opacity-50"
              >
                <div className="relative aspect-[2/3] overflow-hidden rounded border border-border bg-ink transition-colors group-hover:border-vermillion">
                  <img src={proxiedImageUrl(candidate.cover_url)} alt={candidate.title} className="h-full w-full object-cover" />
                </div>
                <div className="line-clamp-2 font-mono text-[10px] leading-snug text-muted group-hover:text-vermillion">
                  {candidate.title}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
