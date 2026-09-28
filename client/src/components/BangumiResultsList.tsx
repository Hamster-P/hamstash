// components/BangumiResultsList.tsx
// 从SearchPage.tsx抽出来的Bangumi条目一览渲染:缩略图+中日文标题+评分+日期,
// 点击一项调用onSelect跳转详情页。搜索页(分页搜索结果)和影视库详情页的
// "补番"入口(某系列的全部关联作品)共用同一份卡片样式,不用各写一份。
import { proxiedImageUrl } from "../utils/proxiedImage";

export interface BangumiSubject {
  id: number;
  name: string;
  name_cn: string;
  date?: string;
  eps?: number;
  images?: { large?: string; common?: string };
  rating?: { score?: number };
}

interface BangumiResultsListProps {
  results: BangumiSubject[];
  onSelect: (bgmId: number) => void;
  emptyText: string;
}

export default function BangumiResultsList({ results, onSelect, emptyText }: BangumiResultsListProps) {
  if (results.length === 0) {
    return <div className="py-10 text-center text-xs text-muted">{emptyText}</div>;
  }

  return (
    <div>
      {results.map((item) => {
        const original = item.name && item.name !== item.name_cn ? item.name : null;
        const eps = item.eps ? `全${item.eps}话` : null;
        const sub = [original, eps].filter(Boolean).join(" · ");
        return (
          <div
            key={item.id}
            onClick={() => onSelect(item.id)}
            className="flex cursor-pointer items-center gap-3 border-b border-border py-2.5 transition-colors hover:bg-surface"
          >
            <div className="h-16 w-11 shrink-0 overflow-hidden rounded bg-surface">
              {item.images?.common && (
                <img
                  src={proxiedImageUrl(item.images.common)}
                  alt=""
                  decoding="async"
                  className="h-full w-full object-cover"
                />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm">{item.name_cn || item.name}</div>
              <div className="h-4 truncate text-xs text-muted">{sub}</div>
            </div>
            <div className="flex shrink-0 items-center gap-3 text-right">
              {item.rating?.score !== undefined && item.rating.score !== null && (
                <div className="text-xs text-score">★ {item.rating.score.toFixed(1)}</div>
              )}
              <div className="w-24 text-xs text-muted">{item.date || "—"}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
