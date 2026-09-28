"""Bangumi 封面按卡片尺寸缩小。

海报在界面上大约 350×550 到 550×750。
原图经常两三千像素宽，浏览器一次缩到位时细线会断成虚线。
这里先缩到略大于卡片的尺寸，浏览器只再缩一点点。
"""
import io
import struct
from urllib.parse import urlparse

from PIL import Image

# 比最大的卡片再留一圈，高分屏不至于发糊。
COVER_MAX_W = 800
COVER_MAX_H = 1120


def is_bangumi_cover(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == "lain.bgm.tv" and "/pic/cover/" in parsed.path


def _dimensions(content: bytes) -> tuple[int, int] | None:
    """只读文件头。已经够小的封面不必整张解码。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return struct.unpack(">II", content[16:24])
    if not content.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(content):
        if content[index] != 0xFF:
            index += 1
            continue
        marker = content[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", content[index + 5 : index + 9])
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD9:
            index += 2
            continue
        if index + 4 > len(content):
            return None
        segment = struct.unpack(">H", content[index + 2 : index + 4])[0]
        if segment < 2:
            return None
        index += 2 + segment
    return None


def fit_bangumi_cover(content: bytes) -> tuple[bytes, str, bool]:
    """超过上限才缩小。返回 (字节, content-type, 是否改过)。

    打不开或本来就不大时，原样返回。
    """
    size = _dimensions(content)
    if size is not None and size[0] <= COVER_MAX_W and size[1] <= COVER_MAX_H:
        return content, "image/jpeg", False
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Exception:
        return content, "image/jpeg", False

    width, height = image.size
    if width <= COVER_MAX_W and height <= COVER_MAX_H:
        return content, "image/jpeg", False

    scale = min(COVER_MAX_W / width, COVER_MAX_H / height)
    resized = image.convert("RGB").resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        Image.Resampling.LANCZOS,
    )
    # 4:4:4，避免色度抽样把细线再次拆开。
    buffer = io.BytesIO()
    resized.save(buffer, format="JPEG", quality=90, subsampling=0, optimize=True)
    return buffer.getvalue(), "image/jpeg", True
