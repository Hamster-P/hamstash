"""字幕服务的实际请求诊断；直连测试不读取环境代理。"""
import asyncio
import time
from urllib.parse import urlencode

from fastapi import HTTPException

from services import subtitles
from services.proxy import make_client


def _supported(item) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("ext"), str) or not isinstance(item.get("url"), str):
        return False
    try:
        subtitles.validate_url(item["url"])
        return item["ext"].lower().lstrip(".") in subtitles.FORMATS
    except (HTTPException, ValueError):
        return False


async def probe(proxy: str | None, timeout: float) -> list[dict]:
    search_url = subtitles.SEARCH_URL + "?" + urlencode({"name": "葬送的芙莉莲 S01E01"})
    download_url = "https://" + sorted(subtitles.DOWNLOAD_HOSTS)[0] + "/"
    results = []

    def result(name, url, level, started, detail, status=None):
        return {"name": name, "url": url, "note": "字幕服务 · " + ("当前代理" if proxy else "直连"),
                "level": level, "status": status, "elapsed_ms": int((time.monotonic() - started) * 1000),
                "detail": detail}

    async with make_client(proxy, timeout=timeout, trust_env=False, follow_redirects=False,
                           headers={"User-Agent": "HamStash/Subtitle"}) as client:
        started = time.monotonic()
        try:
            response = await client.get(search_url)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), list):
                raise ValueError("字幕 API 返回格式或业务状态异常")
            results.append(result("迅雷字幕搜索 API", search_url, "ok", started, "搜索 API 正常", response.status_code))
            candidate = next((item for item in data["data"] if _supported(item)), None)
        except Exception as exc:
            results.append(result("迅雷字幕搜索 API", search_url, "fail", started, f"{type(exc).__name__}: {exc}"))
            candidate = None
        if candidate is None:
            results.append(result("迅雷字幕文件下载", download_url, "warn", time.monotonic(), "未取得有效字幕地址，下载链路尚未验证"))
            return results

        download_url = candidate["url"]
        started = time.monotonic()
        try:
            for _ in range(5):
                async with client.stream("GET", download_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise ValueError("字幕下载重定向缺少地址")
                        download_url = str(response.url.join(location))
                        subtitles.validate_url(download_url)
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > subtitles.MAX_BYTES:
                            raise ValueError("字幕文件超过大小限制")
                    await asyncio.to_thread(subtitles.analyse, bytes(content), candidate["ext"].lower().lstrip("."))
                    results.append(result("迅雷字幕文件下载", download_url, "ok", started, "已下载并验证字幕内容（不保存）", response.status_code))
                    break
            else:
                raise ValueError("字幕下载重定向过多")
        except Exception as exc:
            results.append(result("迅雷字幕文件下载", download_url, "fail", started, f"{type(exc).__name__}: {exc}"))
    return results


def advice(direct: list[dict], proxied: list[dict] | None) -> str:
    direct_ok = all(item["level"] == "ok" for item in direct)
    proxy_ok = proxied is not None and all(item["level"] == "ok" for item in proxied)
    if direct_ok:
        return "字幕搜索和下载均可直连，无需额外配置代理。" + ("当前代理未全部通过，可能影响字幕功能。" if proxied is not None and not proxy_ok else "")
    if proxy_ok:
        return "字幕直连未全部通过，当前代理下搜索和下载均正常，建议使用当前代理。"
    if proxied is None:
        return "字幕直连未全部通过；尚无可测试的代理，暂不能判断代理能否解决，请检查结果后配置代理重试。"
    return "字幕直连和当前代理均未全部通过，暂不能确定是否需要代理，请检查网络、代理或字幕服务状态。"
