import { useState } from "react";
import BangumiResultsList, { type BangumiSubject } from "../../components/BangumiResultsList";
import { API_BASE } from "../../api";

export type EntryPickerRequest =
  | { mode: "add"; libraryFolder: string; relPath: string; filename: string; keyword: string }
  | { mode: "regroup"; ids: number[]; keyword: string };

// 媒体库分集「添加到剧场版」和剧场版卡「重选条目」共用。
// 追加写 standalone 行；重选只改这张卡对应的 bgm_id。
export default function EntryPickerDialog({
  request,
  onClose,
  onSaved,
}: {
  request: EntryPickerRequest;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [keyword, setKeyword] = useState(request.keyword);
  const [results, setResults] = useState<BangumiSubject[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = () => {
    const text = keyword.trim();
    if (!text || loading) return;
    setLoading(true);
    setError(null);
    fetch(`${API_BASE}/bangumi/search?keyword=${encodeURIComponent(text)}`)
      .then((res) => res.json())
      .then((data) => setResults(data?.data ?? []))
      .catch(() => setResults([]))
      .finally(() => setLoading(false));
  };

  const select = async (bgmId: number) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res =
        request.mode === "add"
          ? await fetch(`${API_BASE}/library/standalone`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                library_folder: request.libraryFolder,
                rel_path: request.relPath,
                filename: request.filename,
                bgm_id: bgmId,
                media_type: null,
              }),
            })
          : await fetch(`${API_BASE}/library/standalone/regroup`, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ ids: request.ids, bgm_id: bgmId }),
            });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onSaved();
      onClose();
    } catch (err) {
      console.error("设置独立剧场版/OVA 失败", err);
      setBusy(false);
      setError("设置失败,请重试");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-6" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-md border border-border bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm">{request.mode === "add" ? "设为独立剧场版/OVA — 选择条目" : "重选条目"}</div>
          <button onClick={onClose} className="rounded border border-border px-2 py-1 font-mono text-[11px] text-muted hover:text-paper">
            关闭
          </button>
        </div>
        <div className="mb-3 flex items-center gap-2">
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
            placeholder="搜索剧场版/OVA 名称"
            className="flex-1 rounded border border-border bg-ink px-3 py-1.5 text-sm text-paper outline-none placeholder:text-muted/60 focus:border-vermillion"
          />
          <button
            onClick={search}
            disabled={loading}
            className="rounded-md border border-vermillion px-4 py-1.5 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink disabled:opacity-40"
          >
            {loading ? "检索中..." : "检索"}
          </button>
        </div>
        {error && <div className="mb-3 font-mono text-[11px] text-vermillion">{error}</div>}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {results.length === 0 ? (
            <div className="py-10 text-center font-mono text-xs text-muted">
              输入名称检索,选中一部作为该{request.mode === "add" ? "文件" : "卡片"}的条目。
            </div>
          ) : (
            <BangumiResultsList results={results} onSelect={(bgmId) => select(bgmId)} emptyText="" />
          )}
        </div>
      </div>
    </div>
  );
}
