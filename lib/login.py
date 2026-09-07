"""登录链：state.json token 缓存 -> 探活 -> CAS+ddddocr 重登 -> OAuth -> appletBind。

已验证流程（Phase 0 实测）：
1. m.ruc.edu.cn/uc/wap/login?redirect=广场 -> 302 -> cas.ruc.edu.cn/cas/login
2. CAS 表单：username/password(明文)/authcode/rememberMe/execution/encrypted/_eventId/loginType
   验证码 GET https://cas.ruc.edu.cn/cas/captcha.jpg（4 位数字）
3. POST 登录：302=成功，200=失败（重新渲染登录页）
4. 跟 302 后 GET /uc/api/oauth/index?redirect=...rdAuth&appid=200230106162633619&state=STATE
   落地 yxkj.ruc.edu.cn/kyq-v/#/main/my?...&ticketCode=..&openid=..&account=..&sign=..
5. POST /kyq/static/public/appletBind?openid=..&sign=..&openId=..
   body {account, ticketCode, token:"", authorization:false} -> {data:{token}}
"""
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("login")

CAS_BASE = "https://cas.ruc.edu.cn"
WAP_LOGIN = ("https://m.ruc.edu.cn/uc/wap/login?redirect="
             "https%3A%2F%2Fm.ruc.edu.cn%2Fsite%2FapplicationSquare%2Findex%3Fsid%3D20")
OAUTH_URL = ("https://m.ruc.edu.cn/uc/api/oauth/index?redirect="
             "https%3A%2F%2Fyxkj.ruc.edu.cn%2Fremote%2Fstatic%2FrdAuth"
             "&appid=200230106162633619&state=STATE")

DEFAULT_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "state.json")


class AuthError(Exception):
    """认证失败（账密错误等），上层应退出（退出码 2）。"""


# ---------------- state.json 缓存 ----------------
def load_state(path: str = DEFAULT_STATE_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = DEFAULT_STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- CAS 登录 ----------------
def _cas_login(client, username: str, password: str, ocr=None,
               max_ocr: int = 8, captcha_save: str | None = None,
               manual: bool = False) -> None:
    """CAS 账密登录（成功后 session 持有 CAS/广场 cookie）。

    manual=True 时不用 OCR：验证码图存盘后等待人工输入（OCR 失效的终极兜底）。
    """
    page = client.follow_redirects(client.raw("GET", WAP_LOGIN))
    text = page.text
    m = re.search(r'name="execution"[^>]*value="([^"]*)"', text) or \
        re.search(r'value="([^"]*)"[^>]*name="execution"', text)
    if not m:
        raise AuthError("CAS 登录页未找到 execution 字段")
    execution = m.group(1)
    m = re.search(r'action="([^"]+)"', text)
    post_url = (CAS_BASE + m.group(1)) if m.group(1).startswith("/") else m.group(1)

    submit_attempts = 3 if manual else 1  # 人工模式允许换图重填重试
    for submit_try in range(submit_attempts):
        code = None
        for attempt in range(1 if manual else max_ocr):
            img = client.raw("GET", CAS_BASE + "/cas/captcha.jpg").content
            save_path = captcha_save or os.path.join(
                os.path.dirname(DEFAULT_STATE_FILE), "artifacts", "_cas_captcha.jpg")
            try:
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(img)
            except OSError:
                pass
            if manual:
                print("验证码图片已保存: %s" % os.path.abspath(save_path))
                code = re.sub(r"\D", "", input("请输入图中 4 位数字验证码: ").strip())
                log.info("人工输入验证码: %r", code)
            else:
                raw = ocr.classification(img)
                code = re.sub(r"\D", "", raw)
                log.info("CAS 验证码 OCR(第%d次): %r", attempt + 1, code)
            if len(code) == 4:
                break
        if not code or len(code) != 4:
            raise AuthError("CAS 验证码%s未得到 4 位数字"
                            % ("输入" if manual else (" OCR %d 次均" % max_ocr)))

        form = {"username": username, "password": password, "authcode": code,
                "rememberMe": "true", "execution": execution, "encrypted": "true",
                "_eventId": "submit", "loginType": "1"}
        r = client.raw("POST", post_url, data=form,
                       headers={"Referer": page.url, "Origin": CAS_BASE,
                                "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code == 302:
            log.info("CAS 登录成功")
            return
        # 200 = 重新渲染登录页：验证码或账密错误
        err = re.search(r'form-error[^>]*>\s*<[^>]*>([^<]{2,80})', r.text)
        msg = err.group(1).strip() if err else ""
        if "密码" in msg or "password" in msg.lower():
            raise AuthError("CAS 登录失败: %s" % (msg or "账密错误"))
        if manual:
            log.warning("人工验证码提交失败(%s)，换图重试", msg or "未知")
            continue
        raise AuthError("CAS 登录未重定向(可能是验证码识别错误): %s" % (msg or r.text[:80]))
    raise AuthError("CAS 登录人工重试 %d 次仍失败" % submit_attempts)


def _oauth_params(client) -> dict:
    """OAuth 跳转链 -> 落地 URL 提取 ticketCode/openid/account/sign。"""
    final = client.follow_redirects(client.raw("GET", OAUTH_URL,
                                               headers={"Referer": "https://m.ruc.edu.cn/"}))
    qs = parse_qs(urlparse(final.url.replace("#", "?")).query)
    p = {k: v[0] for k, v in qs.items()}
    missing = [k for k in ("ticketCode", "openid", "account", "sign") if k not in p]
    if missing:
        raise AuthError("OAuth 落地缺参数 %s, url=%s" % (missing, final.url[:120]))
    return p


def _applet_bind(client, p: dict) -> str:
    bind_url = (client.base + "/kyq/static/public/appletBind"
                + f"?openid={p['openid']}&sign={p['sign']}&openId={p['openid']}")
    j = client.post_json(bind_url, {"account": p["account"],
                                    "ticketCode": p["ticketCode"],
                                    "token": "", "authorization": False})
    token = (j.get("data") or {}).get("token")
    if not token:
        raise AuthError("appletBind 未返回 token: %s" % str(j)[:200])
    log.info("appletBind 换取 token 成功")
    return token


def _ocr_engine():
    import ddddocr
    return ddddocr.DdddOcr(show_ad=False)


# ---------------- token 探活 ----------------
def token_alive(client, token: str) -> bool:
    """queryAllRoomList 探活。实测：短时连续请求会返回限流（200+"请勿频繁操作"），
    限流不代表 token 失效，退避 3 秒重探一次以免误杀。"""
    for attempt in range(2):
        try:
            r = client.raw("GET", client.base + "/kyq/static/frontApi/room/queryAllRoomList/",
                           headers={"Authorization": "Bearer " + token,
                                    "Referer": client.base + "/kyq-v/"})
            j = r.json()
            if j.get("data"):
                return True
            if "频繁" in str(j.get("message") or ""):
                if attempt == 0:
                    time.sleep(3)
                    continue
                return True  # 限流说明会话在服务端正常处理，视为存活
            return False
        except Exception:
            return False
    return False


# ---------------- 主入口 ----------------
def get_token(client, username: str, password: str,
              state_file: str = DEFAULT_STATE_FILE,
              force_refresh: bool = False, manual: bool = False) -> str:
    """取可用 Bearer token：缓存探活 -> 失效则完整重登。manual: 人工验证码模式。"""
    state = load_state(state_file)
    if not force_refresh and not manual and state.get("token") \
            and token_alive(client, state["token"]):
        log.info("使用缓存 token（探活通过）")
        return state["token"]

    log.info("token 缺失/失效，执行 CAS 登录链%s", "（人工验证码模式）" if manual else "")
    ocr = None if manual else _ocr_engine()
    last = None
    for attempt in range(3):
        try:
            _cas_login(client, username, password, ocr, manual=manual,
                       captcha_save=os.path.join(os.path.dirname(state_file),
                                                 "artifacts", "_cas_captcha.jpg"))
            p = _oauth_params(client)
            token = _applet_bind(client, p)
            state.update({"token": token, "tokenAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "username": username})
            save_state(state, state_file)
            return token
        except AuthError:
            raise
        except Exception as e:  # 网络/解析类错误，重试
            last = e
            log.warning("登录链第 %d 次失败: %s", attempt + 1, e)
            time.sleep(1)
    raise AuthError("登录链重试 3 次仍失败: %s" % last)
