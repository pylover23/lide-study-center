"""立德研学中心自动抢座脚本入口。

子命令：
  run    完整抢座流程（默认）。--at HH:MM 则进入每日调度循环；--day 指定目标日期
  test    只读探活：token + 查座 + （--slider）滑块验证码链路；--day 指定查询日期
  records 查询预约记录与违约记录
  login  强制重新走 CAS 登录链刷新 token

退出码：0 成功 / 1 满座(含请求硬上限) / 2 认证失败 / 3 看门狗 / 4 未知异常
"""
import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.httpclient import HttpClient
from lib import login, book
from lib.slider import solve_slider

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "config.json")
STATE_FILE = login.DEFAULT_STATE_FILE
LOG_DIR = os.path.join(ROOT, "logs")
ART_DIR = os.path.join(ROOT, "artifacts")

EXIT_OK, EXIT_FULL, EXIT_AUTH, EXIT_WATCHDOG, EXIT_UNKNOWN = 0, 1, 2, 3, 4

log = logging.getLogger("main")


# ---------------- 基础 ----------------
def load_cfg() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(sh)


def append_jsonl(record: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "run-%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_artifact(name: str, text: str):
    try:
        os.makedirs(ART_DIR, exist_ok=True)
        with open(os.path.join(ART_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def start_watchdog(seconds: int):
    t = threading.Timer(seconds, lambda: os._exit(EXIT_WATCHDOG))
    t.daemon = True
    t.start()
    return t


def tomorrow_str() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def resolve_day(day: str) -> str:
    """--day 参数 -> YYYY-MM-DD。支持 tomorrow(默认)/today/具体日期。

    系统可预约今天和明天两天内的座位（今天的窗口 07:10 后开放）。
    """
    day = (day or "tomorrow").strip().lower()
    if day == "tomorrow":
        return tomorrow_str()
    if day == "today":
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise SystemExit("--day 取值无效: %r（支持 tomorrow/today/YYYY-MM-DD）" % day)


# ---------------- 响应判定（宽松匹配，实测后收敛） ----------------
def is_success(j: dict) -> bool:
    return j.get("status") is True and j.get("code") == 200


def _msg(j: dict) -> str:
    return str(j.get("message") or "")


def is_seat_taken(j: dict) -> bool:
    m = _msg(j)
    return any(k in m for k in ("已被", "已约", "占用", "被抢", "不可预约"))


def is_window_closed(j: dict) -> bool:
    # 实测样本："房间在1788267600000未开放，请重新选择日期"——预约窗口未到/已过。
    return "未开放" in _msg(j) or "重新选择日期" in _msg(j)


def is_captcha_err(j: dict) -> bool:
    m = _msg(j)
    return any(k in m for k in ("验证", "captcha", "滑块", "过期"))


def is_rate_limited(j: dict) -> bool:
    # 实测样本（短时连续提交）："请勿频繁操作 请稍后再试"——退避后重试。
    return "频繁" in _msg(j)


# ---------------- 抢座编排 ----------------
def try_book_room(client, token, room, seats, target_date, cfg) -> str:
    """对单房间按候选座位逐个尝试。返回 'ok' | 'taken_all' | 'error:<msg>'。

    实测约束：滑块 code 一次性（提交即消费，无论成败），故每座必新解；
    连续提交会触发"请勿频繁操作"限流，退避 3-5s 后原 code 已废须重解。
    """
    slider_retries = cfg.get("sliderRetries", 3)
    for seat in seats:
        cap_fails = 0
        while cap_fails <= slider_retries:
            try:
                code = solve_slider(client, cfg["username"],
                                    max_attempts=cfg.get("sliderAttempts", 5),
                                    log=lambda s: log.info(s))
            except Exception as e:
                cap_fails += 1
                client.stats["slider_fail"] += 1
                log.warning("房间[%s] 座位%d 滑块破解失败(%d): %s",
                            room["name"], seat, cap_fails, e)
                if cap_fails > slider_retries:
                    return "error:滑块破解超限"
                continue
            j = book.submit_reservation(client, token, room["roomId"], seat,
                                        target_date, code)
            save_artifact("save_%s_seat%d.json" % (target_date, seat),
                          json.dumps(j, ensure_ascii=False, indent=1))
            if is_success(j):
                log.info("预约成功: %s %d号位 (%s)", room["name"], seat, target_date)
                return "ok"
            client.stats["submit_fail"] += 1
            if is_window_closed(j):
                log.info("预约窗口未开放: %s", _msg(j))
                return "error:window_closed"
            if is_rate_limited(j):
                wait_s = random.uniform(3, 5)
                log.warning("触发限流，退避 %.1f 秒后重解再试: %s", wait_s, _msg(j))
                time.sleep(wait_s)
                cap_fails += 1
                continue
            if is_captcha_err(j):
                cap_fails += 1
                log.warning("验证码被拒(%d): %s", cap_fails, _msg(j))
                continue
            if is_seat_taken(j):
                log.info("座位 %d 已被抢: %s", seat, _msg(j))
                break
            log.warning("预约失败(未知响应，换下一座): %s", _msg(j))
            break
    return "taken_all"


def do_grab(cfg, target: str) -> int:
    """一次完整抢座流程（target: YYYY-MM-DD）。返回退出码。"""
    t0 = time.time()
    state = login.load_state(STATE_FILE)
    if state.get("lastSuccessDate") == target:
        log.info("幂等检查：%s 已成功预约，跳过", target)
        return EXIT_OK

    client = HttpClient()
    start_watchdog(cfg.get("watchdogSec", 720))

    try:
        token = login.get_token(client, cfg["username"], cfg["password"], STATE_FILE)
    except login.AuthError as e:
        log.error("认证失败: %s", e)
        _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
        return EXIT_AUTH

    rooms = cfg.get("rooms", [])
    poll_until = time.time() + cfg.get("pollMaxMin", 20) * 60
    tried_rooms = 0
    while True:
        if client.request_count >= cfg.get("maxRequests", 120):
            log.error("请求硬上限 %d 已用尽，中止本轮", client.request_count)
            _summary(cfg, target, "req_limit", EXIT_FULL, t0, client)
            return EXIT_FULL

        any_seat = False
        for room in rooms:
            try:
                seats = book.available_seats(client, token, room["roomId"], target)
            except Exception as e:
                msg = str(e)
                log.warning("查座失败 [%s]: %s", room["name"], msg)
                if "401" in msg:  # token 失效，重登一次
                    try:
                        token = login.get_token(client, cfg["username"], cfg["password"],
                                                STATE_FILE, force_refresh=True)
                    except login.AuthError as ae:
                        log.error("重登失败: %s", ae)
                        _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
                        return EXIT_AUTH
                continue
            log.info("[%s] 可约 %d 个: %s", room["name"], len(seats),
                     seats[:20] if len(seats) > 20 else seats)
            if not seats:
                continue
            any_seat = True
            tried_rooms += 1
            order = book.order_candidates(seats, cfg)[:cfg.get("maxSeatsTry", 8)]
            res = try_book_room(client, token, room, order, target, cfg)
            if res == "ok":
                state["lastSuccessDate"] = target
                state["lastSeat"] = {"room": room["name"], "date": target}
                login.save_state(state, STATE_FILE)
                _summary(cfg, target, "success", EXIT_OK, t0, client)
                return EXIT_OK
            if res == "error:window_closed":
                log.error("预约窗口尚未开放(%s)，本轮中止（请确认 --at 时刻在开放窗口内）", target)
                _summary(cfg, target, "window_closed", EXIT_FULL, t0, client)
                return EXIT_FULL
            log.info("[%s] 本轮未成功(%s)，继续下一房间", room["name"], res)

        if any_seat or tried_rooms > 0:
            # 有空位但没抢到：立即再扫一轮（不等待）
            continue
        # 全满：轮询蹲守首选房间
        if time.time() > poll_until:
            log.info("蹲守超时（%d 分钟），判定满座退出", cfg.get("pollMaxMin", 20))
            _summary(cfg, target, "full", EXIT_FULL, t0, client)
            return EXIT_FULL
        log.info("全满，%d 秒后轮询...", cfg.get("pollIntervalSec", 45))
        time.sleep(cfg.get("pollIntervalSec", 45))


def _summary(cfg, target, result, exit_code, t0, client):
    append_jsonl({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "cmd": "run", "targetDate": target, "result": result,
                  "exitCode": exit_code, "durationSec": round(time.time() - t0, 1),
                  "requests": client.request_count,
                  "retries": dict(client.stats)})


# ---------------- 子命令 ----------------
def cmd_run(args) -> int:
    cfg = load_cfg()
    day = getattr(args, "day", None)
    if args.at:
        from lib.scheduler import run_daily_at

        def job():
            # 触发时刻动态解析：--at 07:10 触发时 --day today 即当天（窗口已开）
            code = do_grab(cfg, resolve_day(day))
            log.info("本轮退出码: %d", code)

        preheat = None
        if cfg.get("preWarmSec", 60) > 0:
            def preheat():
                # 触发前 60 秒预热：token 探活，失效则提前重登（避免挤在触发时刻）
                try:
                    client = HttpClient()
                    login.get_token(client, cfg["username"], cfg["password"], STATE_FILE)
                    log.info("触发前预热: token 探活/刷新完成")
                except Exception as e:
                    log.warning("触发前预热失败（正式流程会再重试）: %s", e)

        log.info("进入每日调度模式，触发时刻 %s（Ctrl+C 退出）", args.at)
        try:
            run_daily_at(args.at, job, on_tick=preheat)
        except KeyboardInterrupt:
            log.info("手动停止调度")
        return 0
    return do_grab(cfg, resolve_day(day))


def cmd_test(args) -> int:
    cfg = load_cfg()
    client = HttpClient()
    try:
        token = login.get_token(client, cfg["username"], cfg["password"], STATE_FILE)
    except login.AuthError as e:
        log.error("认证失败: %s", e)
        return EXIT_AUTH
    log.info("token 有效: %s...", token[:12])
    target = resolve_day(getattr(args, "day", None))
    for room in cfg["rooms"]:
        seats = book.query_seats(client, token, room["roomId"], target)
        free = [s["no"] for s in seats if s.get("status") == 0]
        log.info("[%s] %s: 总 %d 座，可约 %d 个 %s", room["name"], target,
                 len(seats), len(free), free[:20])
    if args.slider:
        code = solve_slider(client, cfg["username"],
                            max_attempts=cfg.get("sliderAttempts", 5),
                            log=lambda s: log.info(s))
        log.info("滑块链路验证通过: %s（未提交预约）", code)
    if args.save_probe:
        # 安全采集错误码样本：故意用无效滑块 token 提交，不产生真实预约。
        # 样本用于收敛 is_seat_taken / is_captcha_err 判定。
        room = cfg["rooms"][0]
        j = book.submit_reservation(client, token, room["roomId"], 1,
                                    target, "SLIDER_invalid_probe")
        log.info("saveReservation 无效码样本: %s",
                 json.dumps(j, ensure_ascii=False))
        append_jsonl({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "cmd": "save_probe", "response": j})
    return EXIT_OK


def cmd_records(args) -> int:
    cfg = load_cfg()
    client = HttpClient()
    try:
        token = login.get_token(client, cfg["username"], cfg["password"], STATE_FILE)
    except login.AuthError as e:
        log.error("认证失败: %s", e)
        return EXIT_AUTH
    records = book.all_reservation_records(client, token)
    appointments = records["appointments"]
    breaches = records["breaches"]
    print(json.dumps({
        "appointmentCount": len(appointments),
        "breachCount": len(breaches),
        "appointments": [book.reservation_summary(record) for record in appointments],
        "breaches": [book.reservation_summary(record) for record in breaches],
    }, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_login(args) -> int:
    cfg = load_cfg()
    client = HttpClient()
    try:
        token = login.get_token(client, cfg["username"], cfg["password"],
                                STATE_FILE, force_refresh=True,
                                manual=getattr(args, "manual", False))
    except login.AuthError as e:
        log.error("登录失败: %s", e)
        return EXIT_AUTH
    log.info("登录成功，token=%s...（已写入 state.json）", token[:12])
    return EXIT_OK


def cmd_status(args) -> int:
    state = login.load_state(STATE_FILE)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, "run-%s.jsonl" % today)
    if os.path.exists(path):
        print("--- 最近运行 ---")
        for line in open(path, encoding="utf-8").readlines()[-5:]:
            print(line.rstrip())
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="立德研学中心自动抢座")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="执行抢座（默认子命令）")
    p_run.add_argument("--at", help="每日调度时刻，如 07:10；缺省则立即执行一次")
    p_run.add_argument("--day", default="tomorrow",
                       help="目标日期: tomorrow(默认)/today/YYYY-MM-DD")

    p_test = sub.add_parser("test", help="只读探活：token+查座")
    p_test.add_argument("--day", default="tomorrow",
                        help="查询日期: tomorrow(默认)/today/YYYY-MM-DD")
    p_test.add_argument("--slider", action="store_true",
                        help="额外验证滑块验证码链路（不提交预约）")
    p_test.add_argument("--save-probe", action="store_true",
                        help="用无效滑块码探测 saveReservation 错误响应（采集样本，不产生预约）")

    p_login = sub.add_parser("login", help="强制重登刷新 token")
    p_login.add_argument("--manual", action="store_true",
                         help="人工输入验证码（OCR 失效时的兜底）")
    sub.add_parser("status", help="查看缓存状态与最近运行")
    sub.add_parser("records", help="查询全部预约历史")

    args = parser.parse_args()
    if not args.cmd:
        args = p_run.parse_args([])
    setup_logging()
    handlers = {"run": cmd_run, "test": cmd_test, "records": cmd_records,
                "login": cmd_login, "status": cmd_status}
    try:
        return handlers[args.cmd](args)
    except Exception:
        log.exception("未知异常")
        append_jsonl({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "cmd": args.cmd, "result": "exception", "exitCode": EXIT_UNKNOWN})
        return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
