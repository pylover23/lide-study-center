"""tianai-captcha 滑块缺口定位与轨迹生成（numpy 向量化）。

定位算法（浏览器实测成功后移植）：
- 滑块 PNG alpha>128 提取 mask 及左边界像素集合 bnd
- 背景 JPEG 灰度，mask 沿 x 平移评分：
  score(x) = 4*avg(|lum(x+dx,dy)-lum(x+dx-1,dy)|, bnd) - avg(|...|, mask)
- 最高分 x 即缺口位置；拖动距离(显示) = (gap_x - mask_min_x) / 2
"""
import math
import random
import time
from datetime import datetime, timedelta

import numpy as np
from PIL import Image

from .httpclient import RequestLimitError

def _response_is_service_unavailable(response: dict) -> bool:
    text = str(response)
    return (
        response.get("code") == 5003
        or "DISABLED" in text.upper()
        or "过载保护" in text
    )

def _safe_captcha_response_summary(response: dict) -> str:
    """Describe a captcha response without logging images, IDs, or tokens."""
    top_keys = sorted(str(key) for key in response)
    captcha = response.get("captcha")
    captcha_dict = captcha if isinstance(captcha, dict) else {}
    data = response.get("data")
    message = str(response.get("message") or response.get("msg") or "")
    message = " ".join(message.split())[:160]
    parts = []
    for key in ("code", "status", "success"):
        if key in response:
            parts.append("%s=%r" % (key, response[key]))
    if message:
        parts.append("msg=%r" % message)
    parts.extend([
        "topKeys=%r" % top_keys,
        "hasId=%r" % bool(response.get("id")),
        "captchaType=%s" % type(captcha).__name__,
        "captchaKeys=%r" % sorted(str(key) for key in captcha_dict),
        "backgroundLength=%d" % len(str(captcha_dict.get("backgroundImage") or "")),
        "templateLength=%d" % len(str(captcha_dict.get("templateImage") or "")),
        "dataType=%s" % type(data).__name__,
    ])
    if isinstance(data, dict):
        parts.append("dataKeys=%r" % sorted(str(key) for key in data))
    elif isinstance(data, (str, bytes, list, tuple)):
        parts.append("dataLength=%d" % len(data))
    return ", ".join(parts)


class SliderServiceUnavailable(RuntimeError):
    """Captcha generation/check service cannot process another challenge."""

    def __init__(self, response: dict, message: str | None = None,
                 *, phase: str = "gen"):
        self._response = response
        self._code = response.get("code")
        self._phase = phase
        server_message = str(response.get("message") or response.get("msg") or "")
        base_message = message or server_message or "滑块服务不可用"
        self._message = "%s [phase=%s, %s]" % (
            base_message,
            phase,
            _safe_captcha_response_summary(response),
        )
        super().__init__(self._message)

    @property
    def code(self):
        return self._code

    @property
    def message(self):
        return self._message

    @property
    def phase(self):
        return self._phase

    @property
    def response(self):
        return self._response


def _decode_bg(jpeg_bytes: bytes) -> np.ndarray:
    """背景图 -> 灰度二维数组 (H, W)。"""
    img = Image.open(__import__("io").BytesIO(jpeg_bytes)).convert("L")
    return np.asarray(img, dtype=np.float32)


def _decode_mask(png_bytes: bytes):
    """滑块 PNG -> (mask 布尔数组 (h, w), 左边界坐标数组, mask 内部坐标数组)。"""
    img = Image.open(__import__("io").BytesIO(png_bytes))
    alpha = np.asarray(img.getchannel("A"), dtype=np.uint8)
    mask = alpha > 128  # (h, w)
    if mask.sum() < 100:
        raise ValueError("mask 像素过少(%d)，图片可能异常" % mask.sum())
    ys, xs = np.nonzero(mask)
    min_x = int(xs.min())
    # 左边界：mask 为真且左侧为假的像素
    left_edge = mask & ~np.roll(mask, 1, axis=1)
    left_edge[:, 0] = mask[:, 0]
    eys, exs = np.nonzero(left_edge)
    # 相对滑块图左上角坐标
    bnd = np.stack([eys, exs], axis=1).astype(np.int32)
    inner = np.stack([ys, xs], axis=1).astype(np.int32)
    return mask, min_x, bnd, inner


def locate_gap(bg_jpeg_bytes: bytes, slider_png_bytes: bytes):
    """返回 (gap_x, mask_min_x, confidence)；gap_x 为背景自然坐标下缺口左边缘。"""
    lum = _decode_bg(bg_jpeg_bytes)
    _, min_x, bnd, inner = _decode_mask(slider_png_bytes)
    gx = bnd[:, 1]
    gy = bnd[:, 0]
    ix = inner[:, 1]
    iy = inner[:, 0]
    height, width = lum.shape
    grad = np.abs(np.diff(lum, axis=1))
    grad_height, grad_width = grad.shape
    if (gy.min() < 0 or iy.min() < 0
            or gy.max() >= grad_height or iy.max() >= grad_height):
        raise ValueError(
            "滑块模板高度超出背景: background=%dx%d" % (width, height),
        )

    # grad 的宽度比原图少 1；预先计算完整合法范围，避免先索引后检查。
    boundary_min = int(gx.min()) - 1
    boundary_max = int(gx.max()) - 1
    inner_min = int(ix.min()) - 1
    inner_max = int(ix.max()) - 1
    start = max(1, -boundary_min, -inner_min)
    stop = min(width, grad_width - boundary_max, grad_width - inner_max)
    if start >= stop:
        raise ValueError(
            "滑块模板宽度超出背景: background=%d template=%d"
            % (width, max(boundary_max, inner_max) + 1),
        )

    scores = np.full(width, -1e9, dtype=np.float32)
    for x in range(start, stop):
        bvals = grad[gy, boundary_min + x]
        ivals = grad[iy, inner_min + x]
        scores[x] = 4.0 * bvals.mean() - ivals.mean()

    best = int(np.argmax(scores))
    order = np.argsort(scores)[::-1]
    top1, top2 = scores[order[0]], scores[order[1]]
    confidence = float(top1 - top2)
    if confidence < 0.5:
        raise ValueError("定位退化：top1-top2 分差过小 (%.3f)" % confidence)
    return best, min_x, confidence


def _now_js_iso(offset_ms: int = 0) -> str:
    """模拟 JS new Date().toJSON()：UTC ISO 8601 带毫秒，如 2026-08-31T01:23:45.678Z。"""
    dt = datetime.utcnow() + timedelta(milliseconds=offset_ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def build_track(drag_distance: float):
    """生成人类化拖动轨迹（显示坐标，与浏览器抓包一致）。

    返回 trackList: [{x, y, type, t}]，t 毫秒单调递增，总时长 800-1600ms。
    """
    n = random.randint(30, 50)
    total_ms = random.randint(800, 1600)
    overshoot = random.uniform(2, 4)
    # ease-out: 1-(1-t)^2.2，末端过冲后回拉
    ts = [int(round(total_ms * (i / n))) for i in range(1, n)] + [total_ms]
    track = [{"x": 0, "y": 0, "type": "start", "t": 0}]
    last_x = 0.0
    for i, t in enumerate(ts):
        p = t / total_ms
        if p < 0.9:
            eased = 1 - (1 - p / 0.9) ** 2.2
            x = (drag_distance + overshoot) * eased
        else:
            # 末段回拉
            q = (p - 0.9) / 0.1
            x = drag_distance + overshoot * (1 - q)
        y = math.sin(p * math.pi * 1.5) * 2 * random.uniform(0.5, 1.0)
        track.append({
            "x": round(x - 0, 1), "y": round(y, 1),
            "type": "move", "t": t,
        })
        last_x = x
    track.append({"x": round(drag_distance, 1), "y": track[-1]["y"], "type": "up", "t": total_ms})
    return track


def solve_slider(client, username: str, max_attempts: int = 5, log=print):
    """完整破解流程：gen -> 定位 -> 轨迹 -> check。返回滑块 token。

    client: lib.httpclient.HttpClient（已带 Referer/Origin/UA 头与 cookie）
    """
    from .encrypt import aes_ctr_encrypt, encrypt_ki, aes_ctr_decrypt
    import base64
    import json as _json

    base = client.base
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            key = __import__("os").urandom(16)
            iv = __import__("os").urandom(16)
            custom_plain = _json.dumps(
                {"session": {"username": username,
                             "current_window_url": base + "/kyq-v/"}},
                separators=(",", ":"))
            gen_body = {"custom": aes_ctr_encrypt(key, iv, custom_plain),
                        "ki": encrypt_ki(key, iv)}
            t0 = time.time()
            j = client.post_json(base + "/kyq/static/cap/cg/gen/SLIDER", gen_body)
            cap = j.get("captcha") or {}
            captcha_id = j.get("id")  # 顶层 id，非 captcha.id
            bg_b64, tp_b64 = cap.get("backgroundImage", ""), cap.get("templateImage", "")
            if _response_is_service_unavailable(j):
                raise SliderServiceUnavailable(j, phase="gen")
            if not bg_b64 or not tp_b64 or not captcha_id:
                raise SliderServiceUnavailable(
                    j,
                    "gen 未返回完整图片/id",
                    phase="gen",
                )
            bg = base64.b64decode(bg_b64.split(",")[-1])
            tp = base64.b64decode(tp_b64.split(",")[-1])

            gap_x, mask_min_x, conf = locate_gap(bg, tp)
            drag_display = (gap_x - mask_min_x) / 2.0  # 自然尺寸 -> 显示尺寸 (÷2)
            log("  [slider] 尝试%d: 缺口x=%d 置信度=%.2f 拖动=%.1fpx (%.0fms)"
                % (attempt, gap_x, conf, drag_display, (time.time() - t0) * 1000))
            if not (10 <= drag_display <= 245):
                raise ValueError("拖动距离异常: %.1f" % drag_display)

            track = build_track(drag_display)
            # 站点未启用 timeToTimestamp：Date 经 JSON.stringify 序列化为 ISO 8601 字符串
            start_iso = _now_js_iso()
            end_iso = _now_js_iso(track[-1]["t"] + random.randint(100, 400))
            check_plain_data = {
                "bgImageWidth": 300, "bgImageHeight": 180,
                "sliderImageWidth": 55, "sliderImageHeight": 180,
                "startSlidingTime": start_iso, "endSlidingTime": end_iso,
                "trackList": track,
            }
            check_body = {
                "id": captcha_id,
                "data": aes_ctr_encrypt(key, iv, _json.dumps(check_plain_data, separators=(",", ":"))),
                "custom": aes_ctr_encrypt(key, iv, custom_plain),
                "ki": encrypt_ki(key, iv),
            }
            j2 = client.post_json(base + "/kyq/static/cap/cg/check", check_body)
            if _response_is_service_unavailable(j2):
                raise SliderServiceUnavailable(j2, phase="check")
            if j2.get("code") == 200:
                token = (j2.get("data") or {}).get("token")
                if token:
                    log("  [slider] check 成功")
                    return token
            raise ValueError("check 失败: %s" % str(j2)[:200])
        except RequestLimitError:
            raise
        except SliderServiceUnavailable:
            raise
        except Exception as e:  # 换图重试
            last_err = e
            log("  [slider] 尝试%d失败: %s" % (attempt, e))
            if attempt < max_attempts:
                time.sleep(random.uniform(0.5, 1.0))
    raise RuntimeError("滑块验证码破解失败(%d次): %s" % (max_attempts, last_err))
