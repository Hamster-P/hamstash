from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from routers.subtitles import SaveRequest, Selection, save_subtitles
from services import subtitles as service

SRT = b"1\n00:00:01,000 --> 00:00:03,500\nHello\n\n2\n00:00:05,000 --> 00:00:08,000\nWorld\n"
ASS = b"[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.00,0:00:08.25,Default,Hello, world\n"


class SubtitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.video = self.root / "Anime" / "Season 01" / "Anime - S01E01.mkv"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"video")
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        for cache in [service._candidates, service._contents, service._searches, service._video_cache, service._hashes]:
            cache.clear()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def candidate(self, path=None, identity="one", data=SRT, ext="srt", language="chs"):
        path = path or self.video
        service._candidates[identity] = service.Candidate(time.monotonic(), service.fingerprint(path),
            "https://subtitle.v.geilijiasu.com/test.srt", {"format": ext, "language": language})
        service._contents[identity] = data, service.analyse(data, ext)
        return identity

    def test_ass_srt_vtt_and_utf16_time_axes(self):
        self.assertEqual(service.analyse(ASS, "ass")["last_end"], 8.25)
        self.assertEqual(service.analyse(SRT, "srt")["cue_count"], 2)
        self.assertEqual(service.analyse(SRT.decode().encode("utf-16"), "srt")["first_start"], 1)
        self.assertEqual(service.analyse(b"WEBVTT\n\n00:01.000 --> 00:03.500\nHi", "vtt")["last_end"], 3.5)

    def test_invalid_download_content_rejected(self):
        for content in [b"", b"<html>error</html>", b"PK\x00\x00", b"not subtitles", b"1\n00:00:08,000 --> 00:00:01,000\nBackwards"]:
            with self.subTest(content=content), self.assertRaises(HTTPException):
                service.analyse(content, "srt")

    def test_target_must_be_video_inside_library(self):
        self.assertEqual(service.video_path(self.root, "Anime/Season 01/Anime - S01E01.mkv"), self.video)
        for target in ["../outside.mkv", str(self.root.parent / "outside.mkv"), "Anime/file.ass"]:
            with self.subTest(target=target), self.assertRaises(HTTPException):
                service.video_path(self.root, target)

    def test_download_host_and_redirect_destinations_must_be_allowed(self):
        for url in ["http://subtitle.v.geilijiasu.com/a.srt", "https://example.com/a.srt",
                    "https://subtitle.v.geilijiasu.com.evil.test/a.srt", "https://user@subtitle.v.geilijiasu.com/a.srt",
                    "https://127.0.0.1/a.srt", "file:///a.srt"]:
            with self.subTest(url=url), self.assertRaises(HTTPException):
                service.validate_url(url)

    def test_expired_or_changed_video_candidate_rejected(self):
        identity = self.candidate()
        self.video.write_bytes(b"changed file")
        with self.assertRaises(HTTPException):
            service.get_candidate(self.video, identity)
        identity = self.candidate()
        service._candidates[identity].created -= service.TTL + 1
        with self.assertRaises(HTTPException):
            service.get_candidate(self.video, identity)

    def test_candidate_cannot_be_saved_for_another_video(self):
        identity = self.candidate()
        second = self.video.with_name("Anime - S01E02.mkv")
        second.write_bytes(b"video")
        with self.assertRaises(HTTPException):
            service.save(second, identity)

    def test_existing_subtitle_preserved_and_repeated_save_idempotent(self):
        original = self.video.with_suffix(".srt")
        original.write_bytes(b"my existing subtitle")
        identity = self.candidate()
        first = service.save(self.video, identity)
        self.assertTrue(first["kept_existing"])
        self.assertEqual(original.read_bytes(), b"my existing subtitle")
        self.assertEqual(self.video.with_name(first["filename"]).read_bytes(), SRT)
        again = service.save(self.video, identity)
        self.assertEqual(again["filename"], first["filename"])
        self.assertTrue(again["already_exists"])
        self.assertEqual(len(list(self.video.parent.glob("*.srt"))), 2)

    async def test_preview_downloads_once_and_reports_timeline_warning(self):
        identity = self.candidate()
        service._contents.clear()
        with patch.object(service, "_fetch_bytes", new=AsyncMock(return_value=SRT)) as download, \
             patch.object(service, "video_duration", return_value=4):
            result = await service.prepare(self.video, identity)
            await service.prepare(self.video, identity)
            download.assert_awaited_once()
            self.assertIn("超出", result["timeline_warning"])
            self.assertEqual(result["target_name"], "Anime - S01E01.chs.srt")

    async def test_batch_save_returns_partial_failure_and_updates_folder_size(self):
        self.candidate()
        self.db.add(models.LocalMedia(folder_name="Anime"))
        self.db.commit()
        request = SaveRequest(items=[Selection(rel_path="Anime/Season 01/Anime - S01E01.mkv", candidate_id="one"),
                                     Selection(rel_path="Anime/Season 01/missing.mkv", candidate_id="missing")])
        with patch("routers.subtitles.get_library_root", return_value=self.root), patch.object(service, "video_duration", return_value=10):
            result = await save_subtitles(request, self.db)
        self.assertIn("filename", result["results"][0])
        self.assertIn("error", result["results"][1])
        media = self.db.query(models.LocalMedia).one()
        self.assertEqual(media.folder_size_bytes, len(b"video") + len(SRT))

    def test_video_duration_cache_invalidated_when_video_changes(self):
        with patch.object(service, "_duration_probe", return_value=10) as probe:
            self.assertEqual(service.video_duration(self.video), 10)
            self.assertEqual(service.video_duration(self.video), 10)
            self.video.write_bytes(b"a different video")
            self.assertEqual(service.video_duration(self.video), 10)
            self.assertEqual(probe.call_count, 2)

    async def test_search_filters_formats_deduplicates_and_marks_uncertain_matches(self):
        from contextlib import asynccontextmanager
        import httpx
        payload = {"code": 0, "data": [
            {"name": "Anime - 01.ass", "url": "https://subtitle.v.geilijiasu.com/one.ass", "ext": "ass", "duration": 8000, "languages": ["简体"]},
            {"name": "Anime - 01.ass", "url": "https://subtitle.v.geilijiasu.com/one.ass", "ext": "ass"},
            {"name": "Other Anime - 02.srt", "url": "https://subtitle.v.geilijiasu.com/two.srt", "ext": "srt"},
            {"name": "Anime - 01.zip", "url": "https://subtitle.v.geilijiasu.com/archive.zip", "ext": "zip"},
            {"name": "Anime - 01.srt", "url": "https://127.0.0.1/bad.srt", "ext": "srt"},
        ]}
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json=payload, request=httpx.Request("GET", service.SEARCH_URL))
        @asynccontextmanager
        async def fake_client(*args, **kwargs):
            yield client
        with patch.object(service, "make_client", fake_client):
            result = await service.search(self.video, "Anime S01E01", ["Anime"])
            again = await service.search(self.video, "Anime S01E01", ["Anime"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["source_duration"], 8)
        self.assertTrue(result["items"][1]["warnings"])
        self.assertTrue(again["cached"])
        self.assertEqual(client.get.await_count, 1)

    async def test_download_does_not_follow_redirect_to_private_host(self):
        from contextlib import asynccontextmanager
        import httpx
        @asynccontextmanager
        async def stream(*args, **kwargs):
            yield httpx.Response(302, headers={"location": "https://127.0.0.1/private"},
                                 request=httpx.Request("GET", "https://subtitle.v.geilijiasu.com/test.srt"))
        client = AsyncMock()
        client.stream = stream
        @asynccontextmanager
        async def fake_client(*args, **kwargs):
            yield client
        with patch.object(service, "make_client", fake_client), self.assertRaises(HTTPException):
            await service._fetch_bytes("https://subtitle.v.geilijiasu.com/test.srt")

    async def test_http_preview_and_save_require_selection_and_never_accept_remote_url(self):
        import httpx
        from fastapi import FastAPI
        from database import get_db
        from routers.subtitles import router
        app = FastAPI()
        app.include_router(router)
        async def test_db():
            yield self.db
        app.dependency_overrides[get_db] = test_db
        self.candidate()
        relative = "Anime/Season 01/Anime - S01E01.mkv"
        with patch("routers.subtitles.get_library_root", return_value=self.root), \
             patch.object(service, "video_duration", return_value=10):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/library/subtitles/prepare", json={"rel_path": relative, "candidate_id": "one"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["cue_count"], 2)
                response = await client.post("/library/subtitles/prepare", json={"rel_path": relative, "url": "https://127.0.0.1/private"})
                self.assertEqual(response.status_code, 422)
                response = await client.post("/library/subtitles/save", json={"items": []})
                self.assertEqual(response.status_code, 422)
                response = await client.post("/library/subtitles/save", json={"items": [{"rel_path": relative, "candidate_id": "one"}]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["results"][0]["filename"], "Anime - S01E01.chs.srt")

    def test_research_and_restart_deduplicate_by_disk_content(self):
        first = service.save(self.video, self.candidate())
        for cache in (service._candidates, service._contents, service._searches, service._hashes):
            cache.clear()
        second = service.save(self.video, self.candidate(identity="new-token"))
        self.assertTrue(second["already_exists"])
        self.assertEqual(first["filename"], second["filename"])
        self.assertEqual(len(list(self.video.parent.glob("*.srt"))), 1)

    def test_legacy_names_and_different_encodings_are_deduplicated(self):
        legacy = self.video.with_name(self.video.stem + ".thunder-2.srt")
        legacy.write_bytes(SRT.decode().replace("\n", "\r\n").encode("utf-16"))
        result = service.save(self.video, self.candidate())
        self.assertTrue(result["already_exists"])
        self.assertEqual(result["filename"], legacy.name)

    def test_same_language_different_content_has_stable_variant_filename(self):
        first = service.save(self.video, self.candidate())
        different = SRT.replace(b"Hello", b"Different translation")
        identity = self.candidate(identity="two", data=different)
        preview = service.target_plan(self.video, different, "srt", "chs")
        second = service.save(self.video, identity)
        self.assertEqual(first["filename"], "Anime - S01E01.chs.srt")
        self.assertEqual(second["filename"], preview["filename"])
        self.assertIn(service.content_digest(different)[:8], second["filename"])
        self.assertNotIn("thunder-", second["filename"])

    async def test_same_episode_multiple_subtitles_and_batch_plan_match_saved_names(self):
        from routers.subtitles import plan_subtitles
        first = self.candidate(identity="chs")
        second = self.candidate(identity="cht", data=SRT.replace(b"Hello", b"Traditional translation"), language="cht")
        third = self.candidate(identity="chs-alt", data=SRT.replace(b"Hello", b"Alternative translation"))
        request = SaveRequest(items=[Selection(rel_path="Anime/Season 01/Anime - S01E01.mkv", candidate_id=identity) for identity in [first, second, third]])
        with patch("routers.subtitles.get_library_root", return_value=self.root), patch.object(service, "video_duration", return_value=10):
            plan = await plan_subtitles(request, self.db)
            result = await save_subtitles(request, self.db)
        self.assertEqual([item["target_name"] for item in plan["results"]], [item["filename"] for item in result["results"]])
        self.assertEqual(len(list(self.video.parent.glob("*.srt"))), 3)
        self.assertEqual(len(result["results"]), 3)

    def test_language_detection_and_explicit_manual_override(self):
        self.assertEqual(service.language_code(["简体&日语"]), "chs-jp")
        self.assertEqual(service.language_code(["繁体"]), "cht")
        self.assertEqual(service.language_code(["其他"], "Anime.CHS_JP.ass"), "chs-jp")
        self.assertEqual(service.language_code(["其他"], "Anime.SR.ass"), "unknown")
        result = service.save(self.video, self.candidate(language="unknown"), "cht")
        self.assertEqual(result["filename"], "Anime - S01E01.cht.srt")

    def test_recommendation_requires_title_episode_language_and_duration(self):
        def item(identity, **kwargs):
            return {"id": identity, "warnings": [], "language": "chs", "duration_gap": 3, "release": "GroupA", **kwargs}
        self.assertIsNone(service.recommend([item("a")], None))
        self.assertIsNone(service.recommend([item("a", warnings=["集数待核对"])], 1440))
        self.assertIsNone(service.recommend([item("a", language="unknown")], 1440))
        self.assertIsNone(service.recommend([item("a", duration_gap=100)], 1440))
        self.assertIsNone(service.recommend([item("a"), item("b", release="GroupB")], 1440))
        self.assertEqual(service.recommend([item("a"), item("b", release="GroupB")], 1440, release="GroupA"), "a")

    def test_subject_aliases_include_english_infobox_names(self):
        from routers.subtitles import subject_aliases
        result = subject_aliases({"name_cn": "不死不幸", "infobox": [{"key": "别名", "value": [{"v": "Undead Unluck"}]}]})
        self.assertTrue(service.title_match("[Ohys-Raws] Undead Unluck - 01", result))

    def test_version_tags_do_not_confuse_broadcaster_names_with_tv(self):
        self.assertEqual(service.media_version("[Group] Anime [BDRip]"), "BD")
        self.assertEqual(service.media_version("[ViuTV][WEB-DL] Anime"), "WEB")
        self.assertIsNone(service.media_version("[ViuTV] Anime"))

    async def test_duplicate_batch_candidates_have_one_planned_output(self):
        from routers.subtitles import plan_subtitles
        self.candidate(identity="a")
        self.candidate(identity="b")
        request = SaveRequest(items=[Selection(rel_path="Anime/Season 01/Anime - S01E01.mkv", candidate_id=identity) for identity in ["a", "b"]])
        with patch("routers.subtitles.get_library_root", return_value=self.root), patch.object(service, "video_duration", return_value=10):
            plan = await plan_subtitles(request, self.db)
            result = await save_subtitles(request, self.db)
        self.assertFalse(plan["results"][0]["already_exists"])
        self.assertTrue(plan["results"][1]["already_exists"])
        self.assertEqual(plan["results"][0]["target_name"], plan["results"][1]["target_name"])
        self.assertEqual(len(list(self.video.parent.glob("*.srt"))), 1)
        self.assertTrue(result["results"][1]["already_exists"])
