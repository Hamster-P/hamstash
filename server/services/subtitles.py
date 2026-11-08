"""手动迅雷字幕搜索、时间轴预览及同目录保存。网络请求不接受任意 URL。"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

import rename_engine
from paths import get_install_dir
from services.proxy import get_proxy_url, make_client

SEARCH_URL = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
DOWNLOAD_HOSTS = {"subtitle.v.geilijiasu.com"}
FORMATS = {"ass", "ssa", "srt", "vtt"}
MAX_BYTES = 4 * 1024 * 1024
TTL = 60 * 60
LANGUAGES = {"chs": "简体", "cht": "繁体", "chs-jp": "简日双语", "cht-jp": "繁日双语",
             "chs-en": "简英双语", "cht-en": "繁英双语", "en": "英语", "ja": "日语", "ko": "韩语", "unknown": "语言未知"}
_network = asyncio.Semaphore(2)
_candidates: OrderedDict = OrderedDict()
_contents: OrderedDict = OrderedDict()
_searches: OrderedDict = OrderedDict()
_video_cache: OrderedDict = OrderedDict()
_write_lock = threading.Lock()
_hashes: OrderedDict = OrderedDict()


def video_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative.replace("\\", "/")).resolve()
    if not path.is_relative_to(root) or path == root:
        raise HTTPException(403, "视频路径超出媒体库")
    if path.suffix.lower().lstrip(".") not in rename_engine.VIDEO_EXTS:
        raise HTTPException(400, "请选择视频文件")
    if not path.is_file():
        raise HTTPException(404, "视频文件不存在或媒体库不可达")
    return path


def fingerprint(path: Path) -> tuple:
    stat = path.stat()
    return str(path), stat.st_size, stat.st_mtime_ns


def _trim(cache, limit):
    while len(cache) > limit:
        cache.popitem(last=False)


def _prune():
    now = time.monotonic()
    for key, candidate in list(_candidates.items()):
        if now - candidate.created > TTL:
            _candidates.pop(key, None)
            _contents.pop(key, None)
    _trim(_candidates, 8000)
    for key in list(_contents):
        if key not in _candidates:
            _contents.pop(key, None)


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise HTTPException(502, "字幕源返回了不支持的下载地址")
    return url


async def _fetch_bytes(url: str) -> bytes:
    async with _network:
        async with make_client(get_proxy_url(), timeout=25, trust_env=False, follow_redirects=False) as client:
            for _ in range(4):
                validate_url(url)
                async with client.stream("GET", url, headers={"User-Agent": "HamStash/Subtitle"}) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get("location", "")))
                        continue
                    response.raise_for_status()
                    data = bytearray()
                    async for block in response.aiter_bytes():
                        data.extend(block)
                        if len(data) > MAX_BYTES:
                            raise HTTPException(502, "字幕文件超过 4 MB，暂不支持")
                    return bytes(data)
    raise HTTPException(502, "字幕下载重定向过多")


def _seconds(value: str) -> float:
    values = value.replace(",", ".").split(":")
    if len(values) == 2:
        values.insert(0, "0")
    return int(values[0]) * 3600 + int(values[1]) * 60 + float(values[2])


def analyse(data: bytes, ext: str) -> dict:
    text = None
    encoding = None
    for codec in ("utf-8-sig", "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8", "gb18030", "big5"):
        try:
            text = data.decode(codec)
            encoding = codec
            break
        except UnicodeError:
            pass
    if text is None or "\x00" in text or re.search(r"<(?:!doctype|html)\b", text[:1000], re.I):
        raise HTTPException(502, "下载内容不是可识别的字幕文件")
    cues = []
    if ext in {"ass", "ssa"}:
        fields = []
        in_events = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                in_events = line.casefold() == "[events]"
            elif in_events and line.lower().startswith("format:"):
                fields = [field.strip().lower() for field in line.split(":", 1)[1].split(",")]
            elif in_events and line.lower().startswith("dialogue:") and "start" in fields and "end" in fields:
                values = line.split(":", 1)[1].split(",", len(fields) - 1)
                try:
                    cues.append((_seconds(values[fields.index("start")].strip()), _seconds(values[fields.index("end")].strip())))
                except (ValueError, IndexError):
                    continue
    else:
        pattern = r"((?:\d+:)?\d{2}:\d{2}[,.]\d+)\s*-->\s*((?:\d+:)?\d{2}:\d{2}[,.]\d+)"
        cues = [(_seconds(start), _seconds(end)) for start, end in re.findall(pattern, text)]
    cues = [(start, end) for start, end in cues if math.isfinite(start) and math.isfinite(end) and 0 <= start < end < 7 * 86400]
    if not cues:
        raise HTTPException(502, "字幕内容缺少有效时间轴，未保存")
    return {"encoding": encoding, "cue_count": len(cues), "first_start": min(start for start, _ in cues),
            "last_end": max(end for _, end in cues), "bytes": len(data)}


def _duration_probe(path: Path) -> float | None:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    kwargs = dict(stdin=subprocess.DEVNULL, capture_output=True, timeout=15, encoding="utf-8", errors="replace", creationflags=creationflags)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], **kwargs)
            duration = float(json.loads(result.stdout)["format"]["duration"])
            if math.isfinite(duration) and duration > 0:
                return duration
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
    install = get_install_dir()
    candidates = [install / "mpv.exe", install / "client/src-tauri/binaries/mpv-x86_64-pc-windows-msvc.exe"]
    executable = next((str(item) for item in candidates if item.is_file()), None) or shutil.which("mpv")
    if executable:
        # file-loaded 后读取时长即退出，无窗口、无声，不接入播放/观看记录。
        script = 'mp.register_event("file-loaded", function() local d=mp.get_property_number("duration"); if d then print("HAMSTASH_DURATION="..d) end; mp.commandv("quit") end)'
        try:
            with tempfile.TemporaryDirectory(prefix="hamstash-probe-") as temp:
                lua = Path(temp) / "duration.lua"
                lua.write_text(script, encoding="utf-8")
                result = subprocess.run([executable, "--no-config", "--load-scripts=no", "--ytdl=no", "--vo=null", "--ao=null", "--pause", "--sub-auto=no", "--audio-file-auto=no", f"--script={lua}", "--", str(path)], **kwargs)
                match = re.search(r"HAMSTASH_DURATION=([\d.]+)", result.stdout)
                duration = float(match.group(1)) if match else None
                if duration and math.isfinite(duration) and duration > 0:
                    return duration
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return None


def video_duration(path: Path) -> float | None:
    signature = fingerprint(path)
    if signature not in _video_cache:
        _video_cache[signature] = _duration_probe(path)
        _trim(_video_cache, 256)
    return _video_cache[signature]


def _normalise(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", text).casefold())


def title_match(name: str, aliases: list[str]) -> bool:
    name = _normalise(name)
    return any(len(clean := _normalise(alias)) >= 3 and clean in name for alias in aliases)


def language_code(languages: list[str], name="") -> str:
    text = " ".join(languages).casefold()
    simplified = "简" in text or bool(re.search(r"(?:chs|zh[-_]hans)", text))
    traditional = "繁" in text or bool(re.search(r"(?:cht|zh[-_]hant)", text))
    japanese = "日" in text or bool(re.search(r"\b(?:ja|jp|japanese)\b", text))
    english = "英" in text or bool(re.search(r"\b(?:en|eng|english)\b", text))
    if not (simplified or traditional):
        simplified = bool(re.search(r"(?:^|[.\[\s_-])(?:chs|zh[-_]hans|jpsc)(?=$|[.\]\s_-])", name, re.I))
        traditional = bool(re.search(r"(?:^|[.\[\s_-])(?:cht|zh[-_]hant|jptc)(?=$|[.\]\s_-])", name, re.I))
        japanese = japanese or bool(re.search(r"(?:chs|cht)[_-]jp|jp(?:sc|tc)", name, re.I))
    if simplified and not traditional:
        return "chs-jp" if japanese else "chs-en" if english else "chs"
    if traditional and not simplified:
        return "cht-jp" if japanese else "cht-en" if english else "cht"
    if not simplified and not traditional:
        if japanese:
            return "ja"
        if english:
            return "en"
        if "韩" in text or "korean" in text:
            return "ko"
    return "unknown"


def release_tag(name: str) -> str:
    match = re.search(r"\[([^\]]{2,60})\]", name)
    return match.group(1) if match else ""


def media_version(name: str) -> str | None:
    versions = set()
    for version, pattern in {"BD": r"(?<![a-z])(?:bd(?:rip|remux|box)?|blu[- ]?ray)(?![a-z])",
                             "WEB": r"(?<![a-z])web(?:[- .]?(?:dl|rip))?(?![a-z])",
                             "TV": r"(?<![a-z])(?:tv(?:rip)?|hdtv)(?![a-z])"}.items():
        if re.search(pattern, name, re.I):
            versions.add(version)
    return next(iter(versions)) if len(versions) == 1 else None


def language_rank(language: str, preference: str) -> int:
    if preference == "any":
        return 1 if language == "unknown" else 0
    if language == preference:
        return 0
    if preference in {"chs", "cht"} and language.startswith(preference + "-"):
        return 1
    return 2


def recommend(items: list[dict], duration: float | None, preference="chs", release="") -> str | None:
    if not duration:
        return None  # 不能只按文件名、或单靠时间长度就判定匹配。
    eligible = [item for item in items if not item["warnings"] and item.get("language", "unknown") != "unknown"
                and language_rank(item["language"], preference) < 2 and item.get("duration_gap") is not None
                and item["duration_gap"] <= min(40, duration * .03)
                and (not release or release.casefold() in item.get("release", "").casefold())]
    eligible.sort(key=lambda item: (language_rank(item["language"], preference), item["duration_gap"]))
    if not eligible:
        return None
    if len(eligible) > 1:
        first, second = eligible[:2]
        if (language_rank(first["language"], preference) == language_rank(second["language"], preference)
                and abs(first["duration_gap"] - second["duration_gap"]) < 2 and not release):
            return None  # 多个近似版本不能擅自选一个；可先指定发布标记。
    return eligible[0]["id"]


def content_digest(data: bytes) -> str:
    # 相同字幕即使 BOM、编码或换行方式不同，也只保留一份。
    for codec in ("utf-8-sig", "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8", "gb18030", "big5"):
        try:
            text = unicodedata.normalize("NFC", data.decode(codec)).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        except UnicodeError:
            pass
    return hashlib.sha256(data).hexdigest()


def existing_subtitles(path: Path) -> list[Path]:
    prefix = (path.stem + ".").casefold()
    return [entry for entry in path.parent.iterdir() if entry.name.casefold().startswith(prefix)
            and entry.suffix.lower().lstrip(".") in FORMATS and not entry.is_symlink() and entry.is_file()]


def _file_digest(path: Path) -> str | None:
    signature = fingerprint(path)
    if signature[1] > MAX_BYTES:
        return None
    if signature not in _hashes:
        _hashes[signature] = content_digest(path.read_bytes())
        _trim(_hashes, 1024)
    return _hashes[signature]


def target_plan(path: Path, data: bytes, ext: str, language: str, reserved=None) -> dict:
    if language not in LANGUAGES:
        raise HTTPException(400, "不支持的字幕语言标记")
    reserved = reserved if reserved is not None else {}
    digest = content_digest(data)
    files = existing_subtitles(path)
    for existing in files:
        if _file_digest(existing) == digest:
            return {"filename": existing.name, "already_exists": True, "kept_existing": True}
    # 同一批次的重复内容也合并，不能依赖临时候选 ID。
    for full_name, previous_digest in reserved.items():
        previous = Path(full_name)
        if previous.parent == path.parent and previous.name.casefold().startswith((path.stem + ".").casefold()) and previous_digest == digest:
            return {"filename": previous.name, "already_exists": True, "kept_existing": True}
    for length in (0, 8, 12, 16, 64):
        variant = f".{digest[:length]}" if length else ""
        name = f"{path.stem}.{language}{variant}.{ext}"
        if len(name.encode("utf-16-le")) // 2 > 255:
            raise HTTPException(400, "视频文件名过长，无法追加字幕语言标记，请先缩短视频文件名")
        target = path.with_name(name)
        if not target.exists() and not target.is_symlink() and str(target) not in reserved:
            reserved[str(target)] = digest
            return {"filename": name, "already_exists": False, "kept_existing": bool(files) or bool(length)}
    raise HTTPException(409, "字幕命名冲突，请检查目录")


def episode_number(name: str, offset=0, total=None) -> float | None:
    if rename_engine.is_collection_title(name):
        return None
    parsed = rename_engine.parse_file_episode(name)
    if parsed is None:
        match = re.search(r"第\s*(\d+(?:\.\d+)?)\s*[话話集]", name)
        parsed = match.group(1) if match else None
    return rename_engine._normalize_absolute_episode(float(parsed), offset, total) if parsed else None


@dataclass
class Candidate:
    created: float
    signature: tuple
    url: str
    public: dict


async def search(path: Path, query: str, aliases: list[str], offset=0, total=None, video_hint="") -> dict:
    _prune()
    signature = await asyncio.to_thread(fingerprint, path)
    key = signature, query, tuple(aliases), offset, total, video_hint
    cached = _searches.get(key)
    if cached and time.monotonic() - cached[0] < 300 and all(item["id"] in _candidates for item in cached[1]):
        return {"items": cached[1], "cached": True}
    try:
        async with _network:
            async with make_client(get_proxy_url(), timeout=25, trust_env=False, follow_redirects=False) as client:
                response = await client.get(SEARCH_URL, params={"name": query}, headers={"User-Agent": "HamStash/Subtitle"})
                response.raise_for_status()
                if len(response.content) > MAX_BYTES:
                    raise HTTPException(502, "字幕源响应过大")
                payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "迅雷字幕搜索失败，请稍后重试或检查代理设置")
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), list):
        raise HTTPException(502, "迅雷字幕源返回异常数据")
    expected = episode_number(path.stem)  # 本地已经整理成季内编号，不再减偏移。
    items, seen = [], set()
    for raw in payload["data"][:200]:
        if not isinstance(raw, dict):
            continue
        ext = str(raw.get("ext", "")).lower().lstrip(".")
        if ext not in FORMATS or not isinstance(raw.get("name"), str) or not isinstance(raw.get("url"), str):
            continue
        try:
            url = validate_url(raw["url"])
        except (HTTPException, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        name = raw["name"]
        episode = episode_number(name, offset, total)
        warnings = []
        if not title_match(name + " " + str(raw.get("simple_name", "")), aliases):
            warnings.append("作品名待核对")
        if episode is None or expected is None:
            warnings.append("集数待核对")
        elif episode != expected:
            warnings.append(f"集数可能不符（候选 {episode:g} / 当前 {expected:g}）")
        local_season = rename_engine.parse_file_season(path.stem)
        candidate_season = rename_engine.parse_file_season(name)
        if local_season and candidate_season and local_season != candidate_season:
            warnings.append(f"季度编号待核对（候选 S{candidate_season} / 当前 S{local_season}）")
        local_version, candidate_version = media_version(video_hint or path.stem), media_version(name)
        if local_version and candidate_version and local_version != candidate_version:
            warnings.append(f"版本待核对（候选 {candidate_version} / 当前 {local_version}）")
        languages = raw.get("languages")
        languages = [value for value in languages if isinstance(value, str) and value] if isinstance(languages, list) else []
        duration = raw.get("duration")
        duration = duration / 1000 if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 < duration < 7 * 86400 * 1000 else None
        identity = secrets.token_urlsafe(18)
        public = {"id": identity, "name": name, "format": ext, "languages": languages,
                  "source_duration": duration, "episode": episode, "warnings": warnings,
                  "language": language_code(languages, name), "release": release_tag(name)}
        _candidates[identity] = Candidate(time.monotonic(), signature, url, public)
        items.append(public)
    items.sort(key=lambda item: len(item["warnings"]))
    _searches[key] = time.monotonic(), items
    _trim(_searches, 256)
    _prune()
    return {"items": items, "cached": False}


def get_candidate(path: Path, identity: str) -> Candidate:
    _prune()
    item = _candidates.get(identity)
    if item is None:
        raise HTTPException(409, "候选已过期或服务已重启，请重新搜索")
    if item.signature != fingerprint(path):
        raise HTTPException(409, "视频文件已变化或候选不属于该集，请重新搜索")
    return item


async def prepare(path: Path, identity: str, language: str | None = None) -> dict:
    item = get_candidate(path, identity)
    if identity not in _contents:
        try:
            data = await _fetch_bytes(item.url)
        except httpx.HTTPError:
            raise HTTPException(502, "字幕下载失败，请稍后重试")
        metadata = await asyncio.to_thread(analyse, data, item.public["format"])
        _contents[identity] = data, metadata
        while len(_contents) > 256 or sum(len(value[0]) for value in _contents.values()) > 64 * 1024 * 1024:
            _contents.popitem(last=False)
    data, metadata = _contents[identity]
    _contents.move_to_end(identity)
    duration = await asyncio.to_thread(video_duration, path)
    end = metadata["last_end"]
    warning = None
    if duration and end > duration + 3:
        warning = "字幕超出视频结尾，可能版本不符"
    elif duration and end < duration * .7:
        warning = "字幕覆盖时间明显偏短，请核对正片/短篇或版本"
    language = language or item.public.get("language", "unknown")
    plan = await asyncio.to_thread(target_plan, path, data, item.public["format"], language)
    return {**metadata, "video_duration": duration, "end_gap": duration - end if duration else None,
            "timeline_warning": warning, "target_name": plan["filename"], "already_exists": plan["already_exists"],
            "language": language, "existing": plan["kept_existing"]}


def save(path: Path, identity: str, language: str | None = None) -> dict:
    with _write_lock:
        return _save_locked(path, identity, language)


def _save_locked(path: Path, identity: str, language: str | None = None) -> dict:
    item = get_candidate(path, identity)
    prepared = _contents.get(identity)
    if prepared is None:
        raise HTTPException(409, "字幕预览已过期，请重新选择候选")
    data, _ = prepared
    ext = item.public["format"]
    language = language or item.public.get("language", "unknown")
    for _ in range(5):
        plan = target_plan(path, data, ext, language)
        if plan["already_exists"]:
            return plan
        target = path.with_name(plan["filename"])
        try:
            # 独占创建，永不覆盖用户已有字幕（也不跟随同名符号链接）。
            with target.open("xb") as output:
                try:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                except OSError:
                    output.close()
                    target.unlink(missing_ok=True)
                    raise
            return plan
        except FileExistsError:
            continue
    raise HTTPException(409, "同名字幕过多，请先清理后重试")
