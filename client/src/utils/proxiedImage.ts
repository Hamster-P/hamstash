const API_BASE = "http://127.0.0.1:8080";

// Bangumi封面图走后端/media/image-proxy转发,而不是<img>直连图床——
// WebView的<img>请求是前端自己发的,不经过后端,吃不到设置里配的网络代理,
// 国内网络下容易加载不出来(参考DetailPage里改成独立窗口打开bgm.tv的思路,
// 这里是给"图片"这类前端直连资源补上同样的代理能力)。
export function proxiedImageUrl(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  return `${API_BASE}/media/image-proxy?url=${encodeURIComponent(url)}`;
}
