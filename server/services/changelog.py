"""拉取仓库 CHANGELOG.md,按版本区间裁剪出多个版本的更新说明。

背景:App 更新走 Tauri updater 的 latest.json,notes 字段只是发版当时 CI
从 CHANGELOG.md 里抽取的"这一个版本"的说明(见 .github/workflows/release.yml)。
用户跳级升级(比如落后好几个版本)时看不到中间版本的内容——这个模块用来在
运行时把"当前版本到目标版本之间"的所有版本说明拼出来,给设置页展示。
"""
import re

import httpx

from services.proxy import get_proxy_url

CHANGELOG_URL = "https://raw.githubusercontent.com/Hamster-P/hamstash/master/CHANGELOG.md"

# 跟release.yml里CI用的正则同一个思路:按"## [版本号]"切段,截止到下一个"## "或文件结尾。
_SECTION_RE = re.compile(
    r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\][^\r\n]*\r?\n(?P<body>.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
# 版本标题行里可能带日期,形如 "## [0.7.3] - 2026-08-20"
_DATE_RE = re.compile(r"-\s*(\d{4}-\d{2}-\d{2})")


def _parse_version(v: str) -> tuple[int, int, int] | None:
    """"x.y.z" -> (x, y, z)。格式不对(比如CHANGELOG里的"[待发布]")返回None,调用方跳过。"""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.groups())  # type: ignore[return-value]


def _parse_sections(content: str) -> list[dict]:
    sections = []
    for m in re.finditer(
        r"^##\s*\[([^\]]+)\][^\r\n]*\r?\n",
        content,
        re.MULTILINE,
    ):
        version_raw = m.group(1)
        parsed = _parse_version(version_raw)
        if parsed is None:
            continue  # 跳过"[待发布]"等非版本号占位标题

        heading_line = m.group(0)
        date_m = _DATE_RE.search(heading_line)
        date = date_m.group(1) if date_m else None

        body_start = m.end()
        next_m = re.search(r"^##\s", content[body_start:], re.MULTILINE)
        body_end = body_start + next_m.start() if next_m else len(content)
        body = content[body_start:body_end].strip()

        sections.append({
            "version": version_raw,
            "version_tuple": parsed,
            "date": date,
            "body": body,
        })
    return sections


async def _fetch_changelog() -> str:
    async with httpx.AsyncClient(proxy=get_proxy_url(), follow_redirects=True, timeout=10.0) as client:
        resp = await client.get(CHANGELOG_URL)
        resp.raise_for_status()
        return resp.text


async def get_changelog_range(current: str, target: str) -> list[dict]:
    """返回严格大于current、小于等于target的所有版本段落,按版本从旧到新排列。

    网络/解析异常一律吞掉返回空列表——调用方(路由层)负责在拿到空列表时
    fallback回updater自带的单版本notes,不能因为抓取GitHub失败就让"检查更新"整个报错。
    """
    current_tuple = _parse_version(current)
    target_tuple = _parse_version(target)
    if current_tuple is None or target_tuple is None:
        return []

    try:
        content = await _fetch_changelog()
    except Exception:
        return []

    sections = _parse_sections(content)
    in_range = [
        s for s in sections
        if current_tuple < s["version_tuple"] <= target_tuple
    ]
    in_range.sort(key=lambda s: s["version_tuple"])
    for s in in_range:
        del s["version_tuple"]
    return in_range
