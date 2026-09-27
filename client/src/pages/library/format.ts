export function yearOf(value: string | null | undefined): string | null {
  const matched = value?.match(/\d{4}/);
  return matched ? matched[0] : null;
}

// 排序说明用。未来时间或解析失败就不显示。
export function formatRelative(value: string | null | undefined): string | null {
  if (!value) return null;
  const time = new Date(value.replace(" ", "T")).getTime();
  if (Number.isNaN(time)) return null;
  const minutes = Math.round((Date.now() - time) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}天前`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}个月前`;
  return `${Math.round(months / 12)}年前`;
}

// 进度分母是本地正片数，不是资料站总话数。还没扫过就不显示。
export function progressLabel(
  watched: number | null | undefined,
  total: number | null | undefined,
): string | null {
  if (watched == null || total == null || total <= 0) return null;
  return `${watched}/${total}`;
}

export function mediaTypeLabel(value: string | null | undefined): string | null {
  if (value === "movie") return "剧场版";
  if (value === "ova") return "OVA";
  return null;
}
