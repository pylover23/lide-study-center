"""slider 回归测试：用已保存的抓包样本（浏览器内破解成功的同一组图片）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.slider import (
    SliderServiceUnavailable,
    build_track,
    locate_gap,
    solve_slider,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = os.path.join(ROOT, "artifacts", "_probe_bg.jpg")
PIECE = os.path.join(ROOT, "artifacts", "_probe_piece.png")



class FakeSliderClient:
    base = "https://yxkj.ruc.edu.cn"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post_json(self, url, body):
        self.calls.append((url, body))
        return next(self.responses)

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


    def test_gen_disabled_or_5003_raises_service_unavailable_once(self):
        scenarios = [
            {},
            {"status": "DISABLED", "message": "disabled"},
            {"code": 5003, "message": "请求触发过载保护"},
        ]
        for response in scenarios:
            with self.subTest(response=response):
                client = FakeSliderClient([response])
                with self.assertRaises(SliderServiceUnavailable) as raised:
                    solve_slider(client, "user", max_attempts=5, log=lambda _message: None)

                self.assertEqual(len(client.calls), 1)
                self.assertIn("/gen/SLIDER", client.calls[0][0])
                self.assertEqual(raised.exception.code, response.get("code"))
                self.assertEqual(raised.exception.response, response)
                self.assertTrue(raised.exception.message)

    def test_complete_challenge_check_failures_back_off_before_new_image(self):
        challenge = {
            "id": "captcha-id",
            "captcha": {
                "backgroundImage": "data:image/jpeg;base64,AA==",
                "templateImage": "data:image/png;base64,AA==",
            },
        }
        client = FakeSliderClient([
            challenge,
            {"code": 400, "message": "check rejected"},
            challenge,
            {"code": 400, "message": "check rejected"},
        ])

        with mock.patch("lib.slider.locate_gap", return_value=(120, 0, 1.0)), \
                mock.patch("lib.slider.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "滑块验证码破解失败"):
                solve_slider(client, "user", max_attempts=2, log=lambda _message: None)

        self.assertEqual(len(client.calls), 4)
        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args.args[0], 0.5)
        self.assertLessEqual(sleep.call_args.args[0], 1.0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
