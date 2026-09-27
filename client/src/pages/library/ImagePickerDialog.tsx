import { useEffect, useState } from "react";
import { proxiedImageUrl } from "../../utils/proxiedImage";
import { API_BASE } from "../../api";
import type { ImageCandidate, ImageKind, MetaScope } from "./types";

// 海报和 LOGO 共用这一格。scope 决定记在剧场版还是媒体库详情，两页各一份。
export default function ImagePickerDialog({
  kind,
  bgmId,
  scope,
  currentUrl,
  onClose,
  onSaved,
}: {
  kind: ImageKind;
  bgmId: number;
  scope: MetaScope;
  currentUrl: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [candidates, setCandidates] = useState<ImageCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/anime-meta/${bgmId}/image-candidates`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        const list = kind === "backdrop" ? data.backdrops : data.logos;
        setCandidates(list ?? []);
        setLoading(false);
      })
      .catch((err) => {
        console.error("获取图片候选失败", err);
        if (cancelled) return;
        setLoading(false);
        setError("获取候选图片失败,请检查网络/代理后重试");
      });
    return () => {
      cancelled = true;
    };
  }, [bgmId, kind]);

  const apply = async (url: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/anime-meta/${bgmId}/custom-image`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, url, scope }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onSaved();
      onClose();
    } catch (err) {
      console.error("保存图片选择失败", err);
      setBusy(false);
      setError("保存失败,请重试");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-4xl flex-col rounded-md border border-border bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm">{kind === "backdrop" ? "选择海报图片(背景图)" : "选择 LOGO 图片"}</div>
          <div className="flex items-center gap-2 font-mono text-[11px]">
            <button
              onClick={() => apply(null)}
              disabled={busy}
              className="rounded border border-border px-2 py-1 text-muted transition-colors hover:border-vermillion hover:text-vermillion disabled:opacity-40"
            >
              恢复默认
            </button>
            <button
              onClick={onClose}
              className="rounded border border-border px-2 py-1 text-muted transition-colors hover:text-paper"
            >
              关闭
            </button>
          </div>
        </div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          选中后会被记住,之后图片规则升级也不会覆盖;“恢复默认”回到自动挑选。
        </p>
        {error && (
          <div className="mb-3 rounded border border-vermillion/40 bg-ink p-2 font-mono text-[11px] text-vermillion">
            {error}
          </div>
        )}
        {loading ? (
          <div className="py-10 text-center font-mono text-xs text-muted">正在加载候选图片...</div>
        ) : candidates.length === 0 ? (
          <div className="py-10 text-center font-mono text-xs text-muted">没有可选的图片。</div>
        ) : (
          <div
            className={`grid gap-3 overflow-y-auto ${
              kind === "backdrop"
                ? "grid-cols-[repeat(auto-fill,minmax(220px,1fr))]"
                : "grid-cols-[repeat(auto-fill,minmax(150px,1fr))]"
            }`}
          >
            {candidates.map((candidate) => (
              <button
                key={candidate.url}
                onClick={() => apply(candidate.url)}
                disabled={busy}
                className="group flex flex-col gap-1 text-left disabled:opacity-50"
              >
                <div
                  className={`relative overflow-hidden rounded border bg-ink transition-colors group-hover:border-vermillion ${
                    kind === "backdrop" ? "aspect-video" : "flex aspect-[3/2] items-center justify-center p-2"
                  } ${currentUrl === candidate.url ? "border-vermillion" : "border-border"}`}
                >
                  <img
                    src={proxiedImageUrl(candidate.thumb)}
                    alt=""
                    loading="lazy"
                    className={kind === "backdrop" ? "h-full w-full object-cover" : "max-h-full max-w-full object-contain"}
                  />
                </div>
                <div className="font-mono text-[10px] text-muted group-hover:text-vermillion">
                  {candidate.width}×{candidate.height}
                  {candidate.lang ? ` · ${candidate.lang}` : ""}
                  {currentUrl === candidate.url ? " · 当前" : ""}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
