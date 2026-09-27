import { useEffect, useState } from "react";
import { proxiedImageUrl } from "../../utils/proxiedImage";
import { API_BASE } from "../../api";
import { postRegroup } from "./regroup";
import type { LibraryAnime, RegroupCandidates, RegroupTarget } from "./types";
import { btnGhost, btnPrimary, control } from "./ui";

// 三个入口共用：整个文件夹、某个季度桶、剧场版里的单个文件。
// 差别只在作用哪些文件，以及能不能事先确定这批文件是哪一部。
export default function RegroupDialog({
  target,
  fallbackBgmId,
  animes,
  onClose,
  onCompleted,
}: {
  target: RegroupTarget;
  fallbackBgmId: number | null;
  animes: LibraryAnime[];
  onClose: () => void;
  onCompleted: (notice: string) => void;
}) {
  const [candidates, setCandidates] = useState<RegroupCandidates | null>(null);
  const [loading, setLoading] = useState(false);
  const [pickedBgmId, setPickedBgmId] = useState<number | null>(target.bgmId);
  const [busy, setBusy] = useState(false);
  // 失败留在弹窗里。成功由调用方接到页面上，避免关弹窗时把提示一起卸掉。
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const probe = target.bgmId ?? fallbackBgmId;
    if (!probe) return;
    let cancelled = false;
    setLoading(true);
    fetch(`${API_BASE}/library/regroup/candidates/${probe}`)
      .then((res) => res.json())
      .then((data: RegroupCandidates) => {
        if (cancelled) return;
        setCandidates(data);
        setPickedBgmId((prev) => prev ?? (data.members.length === 1 ? data.members[0].bgm_id : null));
      })
      .catch(() => {
        if (!cancelled) setCandidates(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [target.bgmId, fallbackBgmId]);

  const run = async (targetRootBgmId: number | null, restoreAuto = false) => {
    if (pickedBgmId === null || target.relPaths.length === 0 || busy) return;
    setBusy(true);
    setError(null);
    const result = await postRegroup({
      bgm_id: pickedBgmId,
      target_root_bgm_id: targetRootBgmId,
      rel_paths: target.relPaths,
      restore_auto: restoreAuto,
    });
    setBusy(false);
    if (!result.ok) {
      setError(result.notice);
      return;
    }
    onCompleted(result.notice);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-md border border-border bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <div className="min-w-0 text-sm">
            调整归属：<span className="text-muted">{target.label}</span>
          </div>
          <button
            onClick={onClose}
            className={`${btnGhost} shrink-0`}
          >
            关闭
          </button>
        </div>
        <p className="mb-3 font-mono text-[11px] text-muted">
          共 {target.relPaths.length} 个文件。搬到新文件夹后会自动按新归属重排名字和季度目录，
          不需要再手动跑「修复媒体库」。
        </p>

        {loading ? (
          <div className="py-10 text-center font-mono text-xs text-muted">正在加载家族信息...</div>
        ) : (
          <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
            <div>
              <div className="mb-2 font-mono text-[11px] text-muted">
                1. 这批文件属于哪一部
                {target.bgmId !== null && <span className="ml-2 text-vermillion">已自动识别，可改</span>}
              </div>
              {!candidates || candidates.members.length === 0 ? (
                <div className="rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
                  拿不到家族成员列表（这部可能还没匹配 Bangumi 条目）。
                </div>
              ) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(110px,1fr))] gap-3">
                  {candidates.members.map((member) => (
                    <button
                      key={member.bgm_id}
                      onClick={() => setPickedBgmId(member.bgm_id)}
                      className="group flex flex-col gap-1 text-left"
                    >
                      <div
                        className={`relative aspect-[2/3] overflow-hidden rounded border bg-ink transition-colors ${
                          pickedBgmId === member.bgm_id
                            ? "border-vermillion ring-1 ring-vermillion"
                            : "border-border group-hover:border-vermillion"
                        }`}
                      >
                        {member.cover_url ? (
                          <img src={proxiedImageUrl(member.cover_url)} alt={member.title} className="h-full w-full object-cover" />
                        ) : (
                          <div className="flex h-full w-full items-center justify-center font-mono text-[10px] text-muted">
                            无封面
                          </div>
                        )}
                      </div>
                      <div
                        className={`line-clamp-2 font-mono text-[10px] leading-snug ${
                          pickedBgmId === member.bgm_id ? "text-vermillion" : "text-muted"
                        }`}
                      >
                        {member.title}
                        {member.is_auto_root && <span className="text-vermillion">（系列根）</span>}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div>
              <div className="mb-2 font-mono text-[11px] text-muted">2. 归到哪里</div>
              {pickedBgmId === null ? (
                <div className="rounded border border-border bg-ink p-3 font-mono text-[11px] text-muted">
                  请先在上面选中这批文件属于哪一部。
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-2 font-mono text-[11px]">
                  <button
                    disabled={busy}
                    onClick={() => run(null)}
                    className={btnGhost}
                  >
                    独立成一部
                  </button>
                  <select
                    value=""
                    disabled={busy}
                    onChange={(e) => {
                      const next = Number(e.target.value);
                      if (next) run(next);
                    }}
                    className={control}
                  >
                    <option value="">合并到…</option>
                    {candidates && (
                      <option value={candidates.auto_root.bgm_id}>{candidates.auto_root.title}（原系列）</option>
                    )}
                    {animes
                      .filter(
                        (anime) =>
                          anime.bgm_id &&
                          anime.bgm_id !== candidates?.auto_root.bgm_id &&
                          anime.bgm_id !== pickedBgmId,
                      )
                      .map((anime) => (
                        <option key={anime.folder_name} value={anime.bgm_id!}>
                          {anime.display_title || anime.folder_name}
                        </option>
                      ))}
                  </select>
                  {candidates?.is_overridden && (
                    <button
                      disabled={busy}
                      onClick={() => run(null, true)}
                      className={btnPrimary}
                    >
                      恢复自动归属
                    </button>
                  )}
                  {busy && <span className="text-muted">处理中...</span>}
                </div>
              )}
              {candidates?.is_overridden && (
                <p className="mt-2 font-mono text-[11px] text-muted">
                  这一部当前是手动指定的归属。「恢复自动归属」会清掉手动设置，
                  并把文件搬回 Bangumi 判定的系列「{candidates.auto_root.title}」。
                </p>
              )}
            </div>
          </div>
        )}

        {error && (
          <div className="mt-3 rounded border border-vermillion/40 bg-ink p-3 font-mono text-[11px] text-vermillion">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
