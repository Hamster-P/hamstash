import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Loader2, Search, X } from "lucide-react";
import { API_BASE } from "../../api";
import type { Episode } from "./types";
import { btnGhost, btnPrimary, control } from "./ui";

const languages: Record<string, string> = { chs: "简体", cht: "繁体", "chs-jp": "简日双语", "cht-jp": "繁日双语", "chs-en": "简英双语", "cht-en": "繁英双语", en: "英语", ja: "日语", ko: "韩语", unknown: "语言未知" };
interface Candidate {
  id: string; name: string; format: string; language: string; release: string;
  source_duration: number | null; duration_gap: number | null; warnings: string[];
}
interface Preview {
  video_duration: number | null; first_start: number; last_end: number; cue_count: number;
  timeline_warning: string | null; target_name: string; already_exists: boolean; language: string;
}
interface Choice { rel_path: string; candidate: Candidate; preview: Preview; language: string; automatic: boolean }
interface Row {
  items: Candidate[]; video_duration: number | null; loading?: boolean; error?: string;
  status?: string; recommendation_id?: string | null; recommendation?: Choice;
}
interface PlanEntry extends Partial<Preview> { rel_path: string; candidate_id: string; error?: string }
interface SaveEntry { rel_path: string; candidate_id: string; filename?: string; already_exists?: boolean; error?: string }

function clock(value: number | null | undefined): string {
  if (value == null) return "未知";
  const seconds = Math.max(0, Math.round(value)), hours = Math.floor(seconds / 3600);
  return `${hours ? `${hours}:` : ""}${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function defaultQuery(title: string, episode: Episode): string {
  const tag = episode.filename.match(/S\d+E\d+(?:\.\d+)?/i)?.[0] ?? episode.filename.match(/(?:\s-\s|第)(\d+(?:\.\d+)?)/)?.[1];
  return `${title.trim()}${tag ? ` ${tag}` : ""}`;
}
async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}/library/subtitles/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `请求失败（HTTP ${response.status}）`);
  return data as T;
}

export default function SubtitleDialog({ label, title, episodes, bgmId, onClose }: {
  label: string; title: string; episodes: Episode[]; bgmId: number | null; onClose: () => void;
}) {
  const ordered = useMemo(() => [...episodes].sort((a, b) => a.filename.localeCompare(b.filename, "zh-CN", { numeric: true })), [episodes]);
  const [searchTitle, setSearchTitle] = useState(title), [preference, setPreference] = useState("chs"), [release, setRelease] = useState("");
  const [expanded, setExpanded] = useState<string | null>(episodes.length === 1 ? episodes[0].rel_path : null);
  const [queries, setQueries] = useState<Record<string, string>>({}), [rows, setRows] = useState<Record<string, Row>>({});
  const [choices, setChoices] = useState<Record<string, Choice>>({}), [saved, setSaved] = useState<Record<string, string>>({});
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [preparing, setPreparing] = useState<string | null>(null), [saving, setSaving] = useState(false), [planning, setPlanning] = useState(false);
  const [plan, setPlan] = useState<PlanEntry[] | null>(null), [error, setError] = useState<string | null>(null), [messages, setMessages] = useState<string[]>([]);
  const request = useRef<AbortController | null>(null), generation = useRef(0);
  const selected = Object.values(choices), selectedPaths = new Set(selected.map((item) => item.rel_path));
  const busy = !!progress || !!preparing || saving || planning;
  useEffect(() => () => { ++generation.current; request.current?.abort(); }, []);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => { if (event.key === "Escape" && !saving && !planning) onClose(); };
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, [saving, planning, onClose]);
  const selections = () => selected.map((choice) => ({ rel_path: choice.rel_path, candidate_id: choice.candidate.id, language: choice.language }));
  const applyRow = (path: string, row: Row) => {
    setRows((prev) => ({ ...prev, [path]: row }));
    setChoices((prev) => {
      const next = Object.fromEntries(Object.entries(prev).filter(([, choice]) => choice.rel_path !== path || !choice.automatic));
      if (row.status === "matched" && row.recommendation && !Object.values(next).some((choice) => choice.rel_path === path)) next[row.recommendation.candidate.id] = row.recommendation;
      return next;
    });
    setPlan(null);
  };
  const fetchRow = async (episode: Episode, query: string, preferredRelease: string, signal: AbortSignal): Promise<Row> => {
    const result = await post<Row>("search", { rel_path: episode.rel_path, query, bgm_id: bgmId, preferred_language: preference, preferred_release: preferredRelease }, signal);
    const candidate = result.items.find((item) => item.id === result.recommendation_id);
    if (!candidate) return { ...result, status: result.items.length ? "review" : "missing" };
    try {
      const preview = await post<Preview>("prepare", { rel_path: episode.rel_path, candidate_id: candidate.id }, signal);
      return { ...result, status: preview.already_exists ? "exists" : preview.timeline_warning ? "review" : "matched", recommendation: { rel_path: episode.rel_path, candidate, preview, language: preview.language, automatic: true } };
    } catch (err) {
      if (signal.aborted) throw err;
      return { ...result, status: "review", error: err instanceof Error ? err.message : "推荐字幕无法预览" };
    }
  };
  const runSearch = async (targets: Episode[], preferredRelease = release) => {
    if (busy) return;
    if (searchTitle.trim().length < 3) { setError("请输入至少 3 个字符的作品名"); return; }
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    const ticket = ++generation.current;
    setProgress({ done: 0, total: targets.length }); setError(null); setPlan(null); setMessages([]);
    let done = 0;
    try {
      for (const episode of targets) {
        if (controller.signal.aborted || ticket !== generation.current) break;
        setRows((prev) => ({ ...prev, [episode.rel_path]: { ...(prev[episode.rel_path] ?? { items: [], video_duration: null }), loading: true } }));
        try {
          const query = targets.length === 1 ? queries[episode.rel_path] ?? defaultQuery(searchTitle, episode) : defaultQuery(searchTitle, episode);
          const result = await fetchRow(episode, query, preferredRelease, controller.signal);
          if (ticket === generation.current) applyRow(episode.rel_path, result);
        } catch (err) {
          if (controller.signal.aborted) break;
          if (ticket === generation.current) applyRow(episode.rel_path, { items: [], video_duration: null, status: "error", error: err instanceof Error ? err.message : "搜索失败" });
        }
        if (ticket === generation.current) setProgress({ done: ++done, total: targets.length });
      }
    } finally {
      if (ticket === generation.current) { setProgress(null); setRows((prev) => Object.fromEntries(Object.entries(prev).map(([path, row]) => [path, { ...row, loading: false }]))); }
    }
  };
  const cancelSearch = () => {
    request.current?.abort(); ++generation.current; setProgress(null);
    setRows((prev) => Object.fromEntries(Object.entries(prev).map(([path, row]) => [path, { ...row, loading: false }])));
  };
  const toggleCandidate = async (episode: Episode, candidate: Candidate, language?: string) => {
    if (busy) return;
    if (choices[candidate.id] && !language) { setChoices((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => id !== candidate.id))); setPlan(null); return; }
    const controller = new AbortController(); request.current = controller; const ticket = generation.current;
    setPreparing(candidate.id); setError(null); setPlan(null);
    try {
      const preview = await post<Preview>("prepare", { rel_path: episode.rel_path, candidate_id: candidate.id, language }, controller.signal);
      if (ticket === generation.current) {
        setChoices((prev) => ({ ...prev, [candidate.id]: { rel_path: episode.rel_path, candidate, preview, language: preview.language, automatic: false } }));
        if (!language && ordered.length > 1 && rows[episode.rel_path]?.status === "review" && !selectedPaths.has(episode.rel_path)) {
          const index = ordered.findIndex((item) => item.rel_path === episode.rel_path);
          const next = [...ordered.slice(index + 1), ...ordered.slice(0, index)].find((item) =>
            rows[item.rel_path]?.status === "review" && rows[item.rel_path].items.length > 0 && !selectedPaths.has(item.rel_path) && !saved[item.rel_path]);
          setExpanded(next?.rel_path ?? null);
        }
      }
    } catch (err) { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "字幕预览失败"); }
    finally { if (ticket === generation.current) setPreparing(null); }
  };
  const makePlan = async () => {
    if (!selected.length || busy) return;
    if (selected.length > 100) { setError("每批最多保存 100 份字幕，请减少选择后分批保存。"); return; }
    setPlanning(true); setError(null); setPlan(null);
    try { setPlan((await post<{ results: PlanEntry[] }>("plan", { items: selections() })).results); }
    catch (err) { setError(err instanceof Error ? err.message : "命名预览失败"); }
    finally { setPlanning(false); }
  };
  const save = async () => {
    if (!selected.length || busy) return;
    if (selected.length > 100) { setError("每批最多保存 100 份字幕，请减少选择后分批保存。"); return; }
    setSaving(true); setError(null); setMessages([]);
    try {
      const data = await post<{ results: SaveEntry[]; warning?: string }>("save", { items: selections() });
      const done = new Set<string>(), completed: Record<string, string> = {};
      const notices = data.results.map((result) => {
        if (result.error) return `${ordered.find((item) => item.rel_path === result.rel_path)?.filename ?? result.rel_path}：${result.error}`;
        done.add(result.candidate_id); completed[result.rel_path] = result.filename!;
        return `${result.already_exists ? "已存在，跳过" : "已保存"}：${result.filename}`;
      });
      setSaved((prev) => ({ ...prev, ...completed })); setChoices((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => !done.has(id))));
      setPlan(null); if (data.warning) notices.push(data.warning); setMessages(notices);
    } catch (err) { setError(err instanceof Error ? err.message : "字幕保存失败"); }
    finally { setSaving(false); }
  };
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/75 p-4" onClick={() => !saving && !planning && onClose()}>
    <section role="dialog" aria-modal="true" aria-labelledby="subtitle-dialog-title" className="flex max-h-[92vh] w-full max-w-6xl flex-col rounded-xl border border-border bg-surface p-5 text-paper shadow-2xl" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-start justify-between gap-3"><div><h2 id="subtitle-dialog-title" className="text-lg font-medium">搜索并下载字幕</h2><p className="mt-1 text-xs text-muted">{label} · 迅雷字幕 · 选好字幕后确认保存</p></div><button aria-label="关闭字幕搜索" disabled={saving || planning} onClick={onClose} className={btnGhost}><X size={16} /></button></div>
      <div className="mt-4 flex flex-wrap gap-2">
        <input aria-label="作品搜索名称" value={searchTitle} disabled={busy} onChange={(event) => setSearchTitle(event.target.value)} className={`${control} min-w-48 flex-1`} />
        <select aria-label="字幕语言偏好" value={preference} disabled={busy} onChange={(event) => setPreference(event.target.value)} className={control}><option value="any">语言不限</option>{Object.entries(languages).filter(([key]) => key !== "unknown").map(([key, value]) => <option key={key} value={key}>{value}优先</option>)}</select>
        <input aria-label="优先发布标记" value={release} disabled={busy} onChange={(event) => setRelease(event.target.value)} placeholder="优先发布标记（可选）" className={`${control} w-48`} />
        <button disabled={busy} className={btnPrimary} onClick={() => void runSearch(ordered)}><Search size={14} />{ordered.length > 1 ? "搜索本季并匹配" : "搜索并匹配"}</button>{progress && <button className={btnGhost} onClick={cancelSearch}>停止搜索</button>}
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted">明确匹配会预选，多个近似版本或信息不足留待确认。待确认的集首次选好后自动展开下一条，可重新展开追加字幕。可指定同一发布标记提高整季一致性。时长接近不代表完全同步。</p>
      {progress && <p role="status" className="mt-2 flex items-center gap-2 text-xs text-vermillion"><Loader2 size={14} className="animate-spin" />正在匹配 {progress.done}/{progress.total} 集，已完成结果可保留。</p>}
      {error && <div role="alert" className="mt-2 rounded border border-vermillion/40 p-2 text-xs text-vermillion">{error}</div>}
      <div className="mt-3 min-h-0 flex-1 overflow-y-auto"><table className="w-full text-left text-xs"><thead className="sticky top-0 z-10 bg-surface text-muted"><tr><th className="p-2">选择</th><th className="p-2">本地分集</th><th className="p-2">匹配情况 / 所选字幕</th><th className="p-2">操作</th></tr></thead><tbody>{ordered.map((episode) => {
        const row = rows[episode.rel_path], picked = selected.filter((choice) => choice.rel_path === episode.rel_path);
        const status = row?.loading ? "正在搜索…" : picked.length ? `已选 ${picked.length} 份` : saved[episode.rel_path] ? "已保存" : row?.status === "exists" ? "相同字幕已存在" : row?.status === "matched" ? "明确匹配" : row?.status === "review" ? "待确认" : row?.status === "missing" ? "未找到" : row?.status === "error" ? "搜索失败" : "尚未搜索";
        return <SubtitleRow key={episode.rel_path} episode={episode} row={row} picked={picked} status={status} busy={busy} expanded={expanded === episode.rel_path} query={queries[episode.rel_path] ?? defaultQuery(searchTitle, episode)} preparing={preparing}
          onExpand={() => setExpanded(expanded === episode.rel_path ? null : episode.rel_path)} onQuery={(value) => setQueries((prev) => ({ ...prev, [episode.rel_path]: value }))}
          onSearch={() => void runSearch([episode])} onToggle={(candidate, language) => void toggleCandidate(episode, candidate, language)} onRelease={(value) => { setRelease(value); void runSearch(ordered, value); }}
          onCheck={() => { setPlan(null); if (picked.length) setChoices((prev) => Object.fromEntries(Object.entries(prev).filter(([, choice]) => choice.rel_path !== episode.rel_path))); else if (row?.recommendation) setChoices((prev) => ({ ...prev, [row.recommendation!.candidate.id]: row.recommendation! })); }} />;
      })}</tbody></table>
        {plan && <div className="mt-4 rounded-lg border border-vermillion/40 p-3"><h3 className="font-medium">最终保存预览</h3><p className="mt-1 text-xs text-muted">同内容直接跳过；不同版本保留两份。保存期间若目录发生变化，会重新检查冲突。</p>{plan.map((entry, index) => <div key={index} className="mt-2 break-all border-t border-border pt-2 text-xs"><span className="text-vermillion">{entry.error ? "失败" : entry.already_exists ? "已存在 / 本批重复，跳过" : "将保存"}：</span>{entry.error ?? entry.target_name}{entry.timeline_warning && <p className="text-vermillion">{entry.timeline_warning}</p>}</div>)}</div>}
      </div>
      {messages.length > 0 && <div role="status" className="mt-3 max-h-24 overflow-y-auto rounded border border-border p-2 text-xs text-muted">{messages.map((message, index) => <p key={index} className="break-all">{message}</p>)}</div>}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-muted">已选 {selectedPaths.size} 集 / {selected.length} 份 · 字幕保存到视频同目录</p><div className="flex gap-2"><button disabled={busy || !selected.length} className={btnGhost} onClick={() => { setChoices({}); setPlan(null); }}>清空选择</button><button disabled={saving || planning} className={btnGhost} onClick={onClose}>关闭</button><button disabled={busy || !selected.length} className={btnGhost} onClick={() => void makePlan()}>{planning ? "正在生成预览…" : "查看命名（可选）"}</button><button disabled={busy || !selected.length} className={btnPrimary} onClick={() => void save()}>{saving ? "正在保存…" : `确认保存（${selected.length} 份）`}</button></div></div>
    </section>
  </div>;
}

function SubtitleRow({ episode, row, picked, status, busy, expanded, query, preparing, onExpand, onQuery, onSearch, onToggle, onRelease, onCheck }: {
  episode: Episode; row?: Row; picked: Choice[]; status: string; busy: boolean; expanded: boolean; query: string; preparing: string | null;
  onExpand: () => void; onQuery: (value: string) => void; onSearch: () => void; onToggle: (candidate: Candidate, language?: string) => void; onRelease: (value: string) => void; onCheck: () => void;
}) {
  const rowElement = useRef<HTMLTableRowElement>(null);
  useEffect(() => { if (expanded) rowElement.current?.scrollIntoView({ block: "nearest" }); }, [expanded]);
  return <><tr ref={rowElement} className={`border-t border-border ${picked.length ? "bg-vermillion/5" : ""}`}>
    <td className="p-2"><input aria-label={`选择 ${episode.filename}`} type="checkbox" checked={picked.length > 0} disabled={busy || (!picked.length && (!row?.recommendation || row.status !== "matched"))} onChange={onCheck} className="accent-vermillion" /></td>
    <td className="max-w-64 break-words p-2">{episode.filename}<p className="mt-1 text-muted">视频 {clock(row?.video_duration)}</p></td>
    <td className="max-w-96 p-2"><p className={picked.length ? "text-vermillion" : "text-muted"}>{status}</p>{picked.map((choice) => <div key={choice.candidate.id} className="mt-1 break-all"><p className="text-muted">{languages[choice.language]} · {choice.preview.target_name}</p>{choice.candidate.warnings.length > 0 && <p className="text-vermillion">{choice.candidate.warnings.join("；")}</p>}{choice.preview.timeline_warning && <p className="text-vermillion">{choice.preview.timeline_warning}</p>}</div>)}{row?.error && <p className="text-vermillion">{row.error}</p>}{!picked.length && row?.recommendation?.preview.timeline_warning && <p className="text-vermillion">{row.recommendation.preview.timeline_warning}</p>}</td>
    <td className="p-2"><button onClick={onExpand} className={btnGhost}>{expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}{expanded ? "收起" : "查看 / 改选"}</button></td>
  </tr>{expanded && <tr><td colSpan={4} className="border-t border-border bg-ink/50 p-3">
    <form className="mb-3 flex gap-2" onSubmit={(event) => { event.preventDefault(); onSearch(); }}><input aria-label={`${episode.filename} 搜索关键词`} value={query} disabled={busy} onChange={(event) => onQuery(event.target.value)} className={`${control} min-w-0 flex-1`} /><button disabled={busy} className={btnGhost}>搜索该集</button></form>
    {row && row.items.length === 0 && !row.loading && <p className="text-xs text-muted">未找到候选，可修改作品名称或集数重试。</p>}
    <div className="max-h-80 space-y-2 overflow-y-auto">{row?.items.map((candidate) => { const choice = picked.find((item) => item.candidate.id === candidate.id); return <div key={candidate.id} className={`rounded-lg border bg-surface p-3 ${choice ? "border-vermillion" : "border-border"}`}>
      <label className="flex cursor-pointer items-start gap-3"><input type="checkbox" checked={!!choice} disabled={busy} onChange={() => onToggle(candidate)} className="mt-1 accent-vermillion" /><div className="min-w-0 flex-1"><p className="break-words text-sm">{candidate.name}</p><p className="mt-1 text-xs text-muted">{languages[candidate.language] ?? "语言未知"} · {candidate.format.toUpperCase()} · 源标注 {clock(candidate.source_duration)}{candidate.duration_gap != null ? ` · 时长差约 ${Math.round(candidate.duration_gap)} 秒` : ""}</p>{candidate.warnings.length > 0 && <p className="mt-1 text-xs text-vermillion">{candidate.warnings.join("；")}</p>}{preparing === candidate.id && <p className="mt-1 flex items-center gap-2 text-xs text-muted"><Loader2 size={12} className="animate-spin" />分析时间轴…</p>}</div></label>
      {choice && <div className="mt-2 border-t border-border pt-2 text-xs"><p>字幕 {clock(choice.preview.first_start)}～{clock(choice.preview.last_end)} · {choice.preview.cue_count} 条</p>{choice.preview.timeline_warning && <p className="text-vermillion">{choice.preview.timeline_warning}</p>}<p className="mt-1 break-all">{choice.preview.already_exists ? "相同内容已存在：" : "拟保存："}{choice.preview.target_name}</p><label className="mt-2 flex items-center gap-2 text-muted">语言标记<select aria-label="字幕保存语言标记" disabled={busy} value={choice.language} onChange={(event) => onToggle(candidate, event.target.value)} className={control}>{Object.entries(languages).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label></div>}
      {candidate.release && <button disabled={busy} className={`${btnGhost} mt-2`} onClick={() => onRelease(candidate.release)}>优先「{candidate.release}」匹配本季</button>}
    </div>; })}</div>
  </td></tr>}</>;
}
