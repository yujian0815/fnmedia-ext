"""posterbadge.landscape 竖幅海报中心裁切为 16:9 横幅的契约。"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import posterbadge
from PIL import Image


def _jpeg(w: int, h: int) -> bytes:
    im = Image.new("RGB", (w, h), (40, 90, 160))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


@unittest.skipUnless(posterbadge.available(), "Pillow 不可用")
class LandscapeCropTests(unittest.TestCase):
    def test_portrait_poster_cropped_to_16_9(self):
        out = posterbadge.landscape(_jpeg(270, 363))
        self.assertIsNotNone(out)
        with Image.open(io.BytesIO(out)) as im:
            w, h = im.size
            self.assertAlmostEqual(w / h, 16 / 9, places=2)
            # 宽度保持不变，仅裁掉上下
            self.assertEqual(w, 270)

    def test_landscape_input_kept_wide(self):
        out = posterbadge.landscape(_jpeg(640, 360))
        with Image.open(io.BytesIO(out)) as im:
            w, h = im.size
            self.assertAlmostEqual(w / h, 16 / 9, places=2)

    def test_result_cached_and_consistent(self):
        src = _jpeg(300, 400)
        a = posterbadge.landscape(src)
        b = posterbadge.landscape(src)
        self.assertEqual(a, b)

    def test_invalid_bytes_return_none(self):
        self.assertIsNone(posterbadge.landscape(b"not-an-image"))


if __name__ == "__main__":
    unittest.main()
