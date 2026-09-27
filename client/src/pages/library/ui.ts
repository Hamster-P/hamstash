// 媒体页共用的字号和按钮。
// 页标题 24px。按钮 12px、左右 12px、上下 6px，图标 14。
// 海报标题固定两行，副信息固定一行。
// 设置、下载、RSS 保持原来的密度，不引用这里。

export const pageTitle = "font-display text-2xl tracking-tight text-paper";
export const pageSub = "mt-1 text-xs leading-5 text-muted";

export const btn =
  "inline-flex items-center justify-center gap-1.5 rounded-md border px-3 py-1.5 font-body text-xs transition-colors disabled:cursor-default disabled:opacity-40";
export const btnPrimary = `${btn} border-vermillion bg-vermillion text-ink hover:bg-vermillion/90`;
export const btnGhost = `${btn} border-border bg-surface text-muted hover:border-vermillion hover:text-vermillion`;

export const seg = "flex overflow-hidden rounded-md border border-border font-body text-xs";
export const segOn = "bg-vermillion px-3 py-1.5 text-ink";
export const segOff =
  "bg-surface px-3 py-1.5 text-muted transition-colors hover:bg-surface-hover hover:text-paper";

export const posterFrame = "relative aspect-[2/3] overflow-hidden rounded-md bg-surface shadow-sm";
export const posterTitle = "mt-2 line-clamp-2 min-h-10 text-sm font-medium leading-5";
export const posterMeta = "mt-0.5 h-4 truncate text-xs text-muted";
export const countBadge =
  "absolute -right-2 -top-2 z-10 flex h-6 min-w-6 items-center justify-center rounded-full bg-vermillion px-1.5 text-xs font-medium text-ink";

export const control =
  "rounded-md border border-border bg-surface px-3 py-1.5 text-xs text-paper outline-none focus:border-vermillion";
