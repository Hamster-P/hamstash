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
    return <div className="text-center py-10 text-muted font-mono text-xs">{emptyText}</div>;
  }

  return (
    <div className="space-y-2">
      {results.map((item) => (
        <div
          key={item.id}
          onClick={() => onSelect(item.id)}
          className="flex cursor-pointer items-center gap-4 rounded-md border border-border p-3 transition-colors hover:border-vermillion"
        >
          <div className="h-12 w-16 shrink-0 rounded bg-muted overflow-hidden">
            {item.images?.common && (
              <img
                src={proxiedImageUrl(item.images.common)}
                className="h-full w-full object-cover"
              />
            )}
          </div>
          <div className="flex-1 min-w-0">
            <div className="truncate text-sm">{item.name_cn || item.name}</div>
            <div className="truncate font-mono text-[11px] text-muted">{item.name}</div>
          </div>
          <div className="flex shrink-0 items-center gap-3 text-right">
            {item.rating?.score !== undefined && item.rating.score !== null && (
              <div className="font-mono text-xs font-bold text-amber-500">
                ⭐ {item.rating.score.toFixed(1)}
              </div>
            )}
            <div className="font-mono text-xs text-muted w-24">{item.date || "—"}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
