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
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.httpclient import HttpClient, RequestLimitError
from lib import login, book
from lib.slider import SliderServiceUnavailable, solve_slider

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

    明日座位默认在前一天 07:00 开放，具体时刻由 bookingOpenTime 配置。
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

def booking_open_at(target: str, cfg: dict) -> datetime:
    """Return the local opening instant for a target booking date."""
    hhmm = str(cfg.get("bookingOpenTime", "07:00")).strip()
    try:
        target_midnight = datetime.strptime(target, "%Y-%m-%d")
        open_clock = datetime.strptime(hhmm, "%H:%M")
    except ValueError as exc:
        raise ValueError("bookingOpenTime 必须为 HH:MM，当前为 %r" % hhmm) from exc
    return (target_midnight - timedelta(days=1)).replace(
        hour=open_clock.hour,
        minute=open_clock.minute,
        second=0,
        microsecond=0,
    )


def wait_for_booking_open(target: str, cfg: dict) -> None:
    """Hard gate: no seat query or slider request may happen before opening."""
    opens_at = booking_open_at(target, cfg)
    announced = False
    while True:
        remaining = (opens_at - datetime.now()).total_seconds()
        if remaining <= 0:
            if announced:
                log.info("预约窗口已开放，开始查座并现解滑块验证码")
            return
        if not announced:
            log.info(
                "预约窗口将在 %s 开放；开放前不查座、不生成滑块验证码",
                opens_at.strftime("%Y-%m-%d %H:%M:%S"),
            )
            announced = True
        time.sleep(min(remaining, 60))


# ---------------- 响应判定 ----------------
def is_success(j: dict) -> bool:
    return j.get("status") is True and j.get("code") == 200


def _msg(j: dict) -> str:
    return str(j.get("message") or "")


def is_seat_taken(j: dict) -> bool:
    return any(k in _msg(j) for k in ("已被", "已约", "占用", "被抢", "不可预约"))


def is_window_closed(j: dict) -> bool:
    return "未开放" in _msg(j) or "重新选择日期" in _msg(j)


def is_captcha_err(j: dict) -> bool:
    message = _msg(j).lower()
    return any(k in message for k in ("验证", "captcha", "滑块"))


def is_rate_limited(j: dict) -> bool:
    return "频繁" in _msg(j)


class BookingResult(Enum):
    OK = "ok"
    EXHAUSTED = "exhausted"
    AUTH_EXPIRED = "auth_expired"
    WINDOW_CLOSED = "window_closed"
    CAPTCHA_UNAVAILABLE = "captcha_unavailable"
    TRANSIENT_ERROR = "transient_error"

CAPTCHA_COOLDOWN_SECONDS = (3, 5, 8, 12, 15)


def _inc_stat(client, key: str) -> None:
    client.stats[key] = client.stats.get(key, 0) + 1


# ---------------- 抢座编排 ----------------
def try_book_room(client, token, room, seats, target_date, cfg,
                  attempted_seats: set[int]) -> BookingResult:
    """Submit fresh slider codes until a typed terminal outcome is known."""
    slider_retries = cfg.get("sliderRetries", 3)
    for seat in seats:
        captcha_rejections = 0
        unknown_failures = 0
        while True:
            try:
                code = solve_slider(
                    client,
                    cfg["username"],
                    max_attempts=cfg.get("sliderAttempts", 5),
                    log=lambda message: log.info(message),
                )
            except RequestLimitError:
                raise
            except SliderServiceUnavailable as exc:
                _inc_stat(client, "slider_fail")
                log.warning("captcha_unavailable 房间[%s] 座位%d: %s",
                            room["name"], seat, exc)
                return BookingResult.CAPTCHA_UNAVAILABLE
            except Exception as exc:
                _inc_stat(client, "slider_fail")
                log.warning("captcha_unavailable 房间[%s] 座位%d: %s",
                            room["name"], seat, exc)
                return BookingResult.CAPTCHA_UNAVAILABLE

            try:
                response = book.submit_reservation(
                    client, token, room["roomId"], seat, target_date, code,
                )
            except RequestLimitError:
                raise
            except Exception as exc:
                _inc_stat(client, "submit_fail")
                log.error("submit_transport_unknown 房间[%s] 座位%d: %s",
                          room["name"], seat, exc)
                return BookingResult.TRANSIENT_ERROR

            artifact = "save_%s_%s_seat%d_%d.json" % (
                target_date, room["roomId"], seat, time.time_ns(),
            )
            save_artifact(artifact, json.dumps(response, ensure_ascii=False, indent=1))
            if is_success(response):
                log.info("预约成功: %s %d号位 (%s)", room["name"], seat, target_date)
                return BookingResult.OK

            _inc_stat(client, "submit_fail")
            if login.is_token_expired(response):
                log.warning("auth_expired 房间[%s] 座位%d: %s",
                            room["name"], seat, _msg(response))
                return BookingResult.AUTH_EXPIRED
            if is_window_closed(response):
                log.info("预约窗口未开放: %s", _msg(response))
                return BookingResult.WINDOW_CLOSED
            if is_rate_limited(response):
                _inc_stat(client, "rate_limit")
                wait_s = random.uniform(3, 5)
                log.warning("rate_limited 退避 %.1f 秒后为原座重解: %s",
                            wait_s, _msg(response))
                time.sleep(wait_s)
                continue
            if is_captcha_err(response):
                captcha_rejections += 1
                _inc_stat(client, "captcha_reject")
                log.warning("captcha_reject 座位%d (%d/%d): %s",
                            seat, captcha_rejections, slider_retries, _msg(response))
                if captcha_rejections > slider_retries:
                    return BookingResult.CAPTCHA_UNAVAILABLE
                time.sleep(random.uniform(1, 2))
                continue
            if is_seat_taken(response):
                attempted_seats.add(seat)
                log.info("seat_taken 座位%d: %s", seat, _msg(response))
                break

            unknown_failures += 1
            log.warning("unknown 座位%d (%d/3): %s",
                        seat, unknown_failures, _msg(response))
            if unknown_failures >= 3:
                return BookingResult.TRANSIENT_ERROR
            time.sleep(random.uniform(3, 5))
    return BookingResult.EXHAUSTED


def do_grab(cfg, target: str) -> int:
    """Run one bounded booking state machine for ``target``."""
    t0 = time.time()
    state = login.load_state(STATE_FILE)
    if state.get("lastSuccessDate") == target:
        log.info("幂等检查：%s 已成功预约，跳过", target)
        return EXIT_OK

    wait_for_booking_open(target, cfg)

    client = HttpClient(max_requests=cfg.get("maxRequests", 120))
    watchdog = start_watchdog(cfg.get("watchdogSec", 720))
    try:
        try:
            token = login.get_token(client, cfg["username"], cfg["password"], STATE_FILE)
        except login.AuthError as exc:
            log.error("认证失败: %s", exc)
            _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
            return EXIT_AUTH

        rooms = cfg.get("rooms", [])
        attempted_by_room = {str(room["roomId"]): set() for room in rooms}
        poll_until = time.time() + cfg.get("pollMaxMin", 20) * 60
        auth_refreshed = False
        query_failure_rounds = 0
        captcha_cooldown_streak = 0

        while True:
            successful_queries = 0
            failed_queries = 0
            any_seat = False
            restart_scan = False

            for room in rooms:
                room_id = str(room["roomId"])
                try:
                    seats = book.available_seats(client, token, room_id, target)
                except RequestLimitError:
                    raise
                except Exception as exc:
                    if login.is_token_expired(exc):
                        if auth_refreshed:
                            log.error("auth_fail 第二次认证失效: %s", exc)
                            _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
                            return EXIT_AUTH
                        try:
                            token = login.get_token(
                                client,
                                cfg["username"],
                                cfg["password"],
                                STATE_FILE,
                                force_refresh=True,
                            )
                        except login.AuthError as auth_exc:
                            log.error("auth_fail 重登失败: %s", auth_exc)
                            _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
                            return EXIT_AUTH
                        auth_refreshed = True
                        _inc_stat(client, "token_refresh")
                        restart_scan = True
                        break
                    failed_queries += 1
                    _inc_stat(client, "query_fail")
                    log.warning("query_fail [%s]: %s", room["name"], exc)
                    continue

                successful_queries += 1
                log.info("[%s] 可约 %d 个: %s", room["name"], len(seats),
                         seats[:20] if len(seats) > 20 else seats)
                if not seats:
                    continue
                any_seat = True
                attempted = attempted_by_room[room_id]
                remaining = [seat for seat in book.order_candidates(seats, cfg)
                             if seat not in attempted]
                if not remaining:
                    wait_s = random.uniform(3, 5)
                    log.info("[%s] 当前可用座均已确认被占，%.1f 秒后开启新轮次",
                             room["name"], wait_s)
                    time.sleep(wait_s)
                    attempted.clear()
                    restart_scan = True
                    break

                candidates = remaining[:cfg.get("maxSeatsTry", 8)]
                result = try_book_room(
                    client, token, room, candidates, target, cfg, attempted,
                )
                if result is not BookingResult.CAPTCHA_UNAVAILABLE:
                    captcha_cooldown_streak = 0
                if result is BookingResult.OK:
                    latest_state = login.load_state(STATE_FILE)
                    latest_state["lastSuccessDate"] = target
                    latest_state["lastSeat"] = {
                        "room": room["name"],
                        "date": target,
                    }
                    login.save_state(latest_state, STATE_FILE)
                    _summary(cfg, target, "success", EXIT_OK, t0, client)
                    return EXIT_OK
                if result is BookingResult.AUTH_EXPIRED:
                    if auth_refreshed:
                        log.error("auth_fail 第二次认证失效")
                        _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
                        return EXIT_AUTH
                    try:
                        token = login.get_token(
                            client,
                            cfg["username"],
                            cfg["password"],
                            STATE_FILE,
                            force_refresh=True,
                        )
                    except login.AuthError as exc:
                        log.error("auth_fail 重登失败: %s", exc)
                        _summary(cfg, target, "auth_fail", EXIT_AUTH, t0, client)
                        return EXIT_AUTH
                    auth_refreshed = True
                    _inc_stat(client, "token_refresh")
                    restart_scan = True
                    break
                if result is BookingResult.WINDOW_CLOSED:
                    log.error("预约窗口尚未开放(%s)，本轮中止", target)
                    _summary(cfg, target, "window_closed", EXIT_FULL, t0, client)
                    return EXIT_FULL
                if result is BookingResult.CAPTCHA_UNAVAILABLE:
                    cooldown = CAPTCHA_COOLDOWN_SECONDS[
                        min(captcha_cooldown_streak,
                            len(CAPTCHA_COOLDOWN_SECONDS) - 1)
                    ]
                    captcha_cooldown_streak += 1
                    wait_s = cooldown + random.uniform(0, 1)
                    log.warning(
                        "captcha_unavailable 连续第%d次，全局冷却 %.1f 秒后重查原座",
                        captcha_cooldown_streak,
                        wait_s,
                    )
                    time.sleep(wait_s)
                    restart_scan = True
                    break
                if result is BookingResult.TRANSIENT_ERROR:
                    _summary(cfg, target, "submit_unknown", EXIT_UNKNOWN, t0, client)
                    return EXIT_UNKNOWN

            if restart_scan:
                continue
            if failed_queries:
                query_failure_rounds += 1
                delay = (1, 2, 4)[min(query_failure_rounds - 1, 2)]
                log.warning("查询结果不完整，第%d轮；%d 秒后重查",
                            query_failure_rounds, delay)
                time.sleep(delay)
                if query_failure_rounds >= 3:
                    _summary(cfg, target, "query_fail", EXIT_UNKNOWN, t0, client)
                    return EXIT_UNKNOWN
                continue

            query_failure_rounds = 0
            if any_seat:
                continue
            if successful_queries != len(rooms):
                _summary(cfg, target, "query_fail", EXIT_UNKNOWN, t0, client)
                return EXIT_UNKNOWN
            if time.time() > poll_until:
                log.info("蹲守超时（%d 分钟），判定满座退出", cfg.get("pollMaxMin", 20))
                _summary(cfg, target, "full", EXIT_FULL, t0, client)
                return EXIT_FULL
            log.info("全满，%d 秒后轮询...", cfg.get("pollIntervalSec", 45))
            time.sleep(cfg.get("pollIntervalSec", 45))
    except RequestLimitError as exc:
        log.error("req_limit: %s", exc)
        _summary(cfg, target, "req_limit", EXIT_FULL, t0, client)
        return EXIT_FULL
    finally:
        watchdog.cancel()


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
            run_daily_at(
                args.at,
                job,
                on_tick=preheat,
                pre_warm_sec=cfg.get("preWarmSec", 60),
            )
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
    log.info("token 有效")
    target = resolve_day(getattr(args, "day", None))
    for room in cfg["rooms"]:
        seats = book.query_seats(client, token, room["roomId"], target)
        free = [s["no"] for s in seats if s.get("status") == 0]
        log.info("[%s] %s: 总 %d 座，可约 %d 个 %s", room["name"], target,
                 len(seats), len(free), free[:20])
    if args.slider:
        solve_slider(client, cfg["username"],
                     max_attempts=cfg.get("sliderAttempts", 5),
                     log=lambda s: log.info(s))
        log.info("滑块链路验证通过（未提交预约）")
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
