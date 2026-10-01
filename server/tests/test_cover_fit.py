"""封面只缩到卡片用得上的尺寸，小图不动。"""
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from services.cover_fit import (
    COVER_MAX_H,
    COVER_MAX_W,
    file_needs_fit,
    fit_bangumi_cover,
    is_bangumi_cover,
)


def _jpeg(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (20, 40, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class CoverFitTests(unittest.TestCase):
    def test_only_bangumi_covers(self):
        self.assertTrue(is_bangumi_cover("https://lain.bgm.tv/pic/cover/l/45/a6/339515_OTG2O.jpg"))
        self.assertFalse(is_bangumi_cover("https://image.tmdb.org/t/p/w1280/abc.jpg"))

    def test_large_cover_fits_inside_card_budget(self):
        body, content_type, changed = fit_bangumi_cover(_jpeg(3000, 4085))
        self.assertTrue(changed)
        self.assertEqual(content_type, "image/jpeg")
        image = Image.open(io.BytesIO(body))
        self.assertLessEqual(image.size[0], COVER_MAX_W)
        self.assertLessEqual(image.size[1], COVER_MAX_H)
        self.assertGreater(image.size[0], 700)

    def test_small_cover_is_unchanged(self):
        original = _jpeg(400, 560)
        body, _content_type, changed = fit_bangumi_cover(original)
        self.assertFalse(changed)
        self.assertEqual(body, original)

    def test_file_needs_fit_uses_header_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.jpg"
            small = Path(tmp) / "small.jpg"
            unknown = Path(tmp) / "unknown.bin"
            big.write_bytes(_jpeg(3000, 4085))
            small.write_bytes(_jpeg(400, 560))
            unknown.write_bytes(b"not an image")
            self.assertTrue(file_needs_fit(big))
            self.assertFalse(file_needs_fit(small))
            self.assertFalse(file_needs_fit(unknown))


if __name__ == "__main__":
    unittest.main()
