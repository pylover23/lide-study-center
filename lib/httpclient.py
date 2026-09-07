"""requests.Session 封装：手动 302 跟跳、超时重试、全局节流。"""
import logging
import random
import time
from urllib.parse import urljoin

import requests

log = logging.getLogger("http")

DEFAULT_TIMEOUT = (5, 15)  # (connect, read)
THROTTLE_MS = 400
RETRY_DELAYS = (1, 3, 6)


class HttpClient:
    """跨域共享 cookie 的 HTTP 客户端（CAS -> 广场 -> yxkj 一条链）。"""

    def __init__(self, base: str = "https://yxkj.ruc.edu.cn", headers: dict | None = None):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if headers:
            self.session.headers.update(headers)
        self._last_req_time = 0.0
        self.request_count = 0
        # 本轮运行统计（写入日志摘要行）
        self.stats = {"slider_fail": 0, "submit_fail": 0}

    # ---- 基础请求 ----
    def raw(self, method: str, url: str, *, allow_redirects=False, **kw) -> requests.Response:
        """带节流/重试的单次请求（默认不自动跟跳）。"""
        for attempt, delay in enumerate([0] + list(RETRY_DELAYS)):
            if delay:
                time.sleep(delay + random.uniform(0, 0.5))
            # 全局节流
            elapsed = time.time() - self._last_req_time
            if elapsed < THROTTLE_MS / 1000:
                time.sleep(THROTTLE_MS / 1000 - elapsed)
            self._last_req_time = time.time()
            self.request_count += 1
            try:
                resp = self.session.request(
                    method, url, allow_redirects=allow_redirects,
                    timeout=DEFAULT_TIMEOUT, **kw)
                return resp
            except requests.RequestException as e:
                log.warning("请求失败(%s) 第%d次: %s", url, attempt + 1, e)
                if attempt == len(RETRY_DELAYS):
                    raise
        raise RuntimeError("unreachable")

    def follow_redirects(self, resp: requests.Response, max_hops: int = 10) -> requests.Response:
        """手动逐跳跟 302/301（Session 自动带 cookie）。"""
        hops = 0
        while resp.status_code in (301, 302, 303, 307, 308) and hops < max_hops:
            loc = resp.headers.get("Location")
            if not loc:
                break
            url = urljoin(resp.url, loc)
            resp = self.raw("GET", url)
            hops += 1
        return resp

    # ---- JSON 便捷方法 ----
    def get_json(self, url: str, **kw) -> dict:
        r = self.raw("GET", url, **kw)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, body: dict, *, token: str | None = None, **kw) -> dict:
        headers = {"Content-Type": "application/json;charset=UTF-8",
                   "Origin": self.base, "Referer": self.base + "/kyq-v/"}
        if token:
            headers["Authorization"] = "Bearer " + token
        r = self.raw("POST", url, json=body, headers=headers, **kw)
        r.raise_for_status()
        return r.json()
