import { API_BASE } from "../../api";

interface RegroupPayload {
  detail?: string;
  moved?: unknown[];
  skipped?: unknown[];
  failed?: unknown[];
  target_folder?: string;
  renamed?: { succeeded?: unknown[]; failed?: unknown[] };
}

export async function postRegroup(body: {
  bgm_id: number;
  target_root_bgm_id: number | null;
  rel_paths: string[];
  restore_auto: boolean;
}): Promise<{ ok: boolean; notice: string }> {
  try {
    const res = await fetch(`${API_BASE}/library/regroup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await res.json()) as RegroupPayload;
    if (!res.ok) {
      return { ok: false, notice: data.detail ?? `操作失败(HTTP ${res.status})` };
    }
    const parts = [`已移动 ${data.moved?.length ?? 0} 个文件到「${data.target_folder ?? ""}」`];
    if (data.skipped?.length) parts.push(`跳过 ${data.skipped.length} 个`);
    if (data.failed?.length) parts.push(`失败 ${data.failed.length} 个`);
    if (data.renamed?.succeeded?.length) {
      parts.push(`并已重排 ${data.renamed.succeeded.length} 个文件的名字`);
    }
    if (data.renamed?.failed?.length) {
      parts.push(`${data.renamed.failed.length} 个文件重排失败,可到设置页手动跑一次「修复媒体库」`);
    }
    return { ok: true, notice: parts.join("，") };
  } catch (err) {
    console.error("调整归属失败", err);
    return { ok: false, notice: "调整归属失败,请检查后端连接" };
  }
}
