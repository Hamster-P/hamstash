import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services import subtitle_diagnostics as diagnostic


class SubtitleDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, handler, proxy=None):
        def client_factory(actual_proxy, **kwargs):
            self.assertEqual(actual_proxy, proxy)
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(diagnostic, "make_client", side_effect=client_factory):
            return await diagnostic.probe(proxy, 1)

    async def test_search_and_actual_subtitle_download(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "api-shoulei-ssl.xunlei.com":
                return httpx.Response(200, json={"code": 0, "data": [
                    {"url": "http://127.0.0.1/private", "ext": "srt"},
                    {"url": "https://subtitle.v.geilijiasu.com/test.srt", "ext": "srt"},
                ]})
            return httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")

        checks = await self.run_probe(handler, "http://localhost:7890")
        self.assertEqual([item["level"] for item in checks], ["ok", "ok"])
        self.assertEqual(len(requests), 2)
        self.assertIn("name=", checks[0]["url"])
        self.assertTrue(checks[1]["url"].endswith("test.srt"))

    async def test_http_200_invalid_api_is_not_success(self):
        checks = await self.run_probe(lambda request: httpx.Response(200, text="<html>blocked</html>"))
        self.assertEqual([item["level"] for item in checks], ["fail", "warn"])
        self.assertIn("尚未验证", checks[1]["detail"])

    async def test_no_candidates_does_not_claim_download_works(self):
        checks = await self.run_probe(lambda request: httpx.Response(200, json={"code": 0, "data": []}))
        self.assertEqual([item["level"] for item in checks], ["ok", "warn"])
        self.assertIn("暂不能判断", diagnostic.advice(checks, None))

    async def test_download_redirect_cannot_access_private_address(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "api-shoulei-ssl.xunlei.com":
                return httpx.Response(200, json={"code": 0, "data": [
                    {"url": "https://subtitle.v.geilijiasu.com/test.srt", "ext": "srt"}]})
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        checks = await self.run_probe(handler)
        self.assertEqual(checks[1]["level"], "fail")
        self.assertEqual(len(requests), 2)

    def test_advice_compares_both_required_services(self):
        ok = [{"level": "ok"}, {"level": "ok"}]
        failed = [{"level": "ok"}, {"level": "fail"}]
        self.assertIn("无需额外配置代理", diagnostic.advice(ok, ok))
        self.assertIn("当前代理未全部通过", diagnostic.advice(ok, failed))
        self.assertIn("建议使用当前代理", diagnostic.advice(failed, ok))
        self.assertIn("暂不能确定", diagnostic.advice(failed, failed))

    async def test_settings_includes_proxy_and_direct_checks(self):
        from routers import settings
        from schemas import ProxyTestRequest

        def handler(request):
            return httpx.Response(200, json=[])

        direct = [{"name": "字幕搜索", "url": "https://example.com/", "level": "fail"}]
        proxied = [{"name": "字幕搜索", "url": "https://example.com/", "level": "ok"}]
        with patch.object(settings, "get_system_proxy", return_value=None), \
                patch.object(settings, "make_client", side_effect=lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)), \
                patch.object(settings, "_source_probes", return_value=[]), \
                patch.object(settings, "_probe", new=AsyncMock(return_value={"name": "other"})), \
                patch.object(settings.subtitle_diagnostics, "probe", new=AsyncMock(side_effect=[direct, proxied])):
            result = await settings.test_proxy(ProxyTestRequest(proxy_url="http://localhost:7890", sources=[]))
        self.assertIn("建议使用当前代理", result["subtitle_proxy_message"])
        self.assertEqual(result["checks"][-1]["name"], "字幕搜索（直连对照）")
