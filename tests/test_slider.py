"""slider 回归测试：用已保存的抓包样本（浏览器内破解成功的同一组图片）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.slider import locate_gap, build_track

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = os.path.join(ROOT, "artifacts", "_probe_bg.jpg")
PIECE = os.path.join(ROOT, "artifacts", "_probe_piece.png")


class TestSlider(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(BG) and os.path.exists(PIECE),
                         "缺少回归样本")
    def test_locate_gap_sample(self):
        with open(BG, "rb") as f:
            bg = f.read()
        with open(PIECE, "rb") as f:
            piece = f.read()
        gap_x, mask_min_x, conf = locate_gap(bg, piece)
        # 背景 600x360：缺口应在合理区间且置信度充足
        self.assertTrue(60 <= gap_x <= 545, "gap_x=%d" % gap_x)
        self.assertGreater(conf, 0.5, "conf=%.3f" % conf)
        drag = (gap_x - mask_min_x) / 2.0
        self.assertTrue(10 <= drag <= 245, "drag=%.1f" % drag)

    def test_build_track_shape(self):
        track = build_track(120.5)
        self.assertGreaterEqual(len(track), 30)
        self.assertEqual(track[0]["type"], "start")
        self.assertEqual(track[-1]["type"], "up")
        self.assertEqual(track[0]["t"], 0)
        ts = [p["t"] for p in track]
        self.assertEqual(ts, sorted(ts))  # 单调不减
        self.assertTrue(all(set(p) == {"x", "y", "type", "t"} for p in track))
        self.assertLessEqual(track[-1]["t"], 1600)
        self.assertGreaterEqual(track[-1]["t"], 800)
        # 终点接近目标距离
        self.assertAlmostEqual(track[-1]["x"], 120.5, delta=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
