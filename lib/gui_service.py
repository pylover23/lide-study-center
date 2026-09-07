import json
import os
from copy import deepcopy
from datetime import date, timedelta

from lib import book
from lib import login as login_mod
from lib.httpclient import HttpClient
from lib.runtime_paths import ensure_runtime_dirs
from lib.slider import solve_slider

DEFAULT_ROOMS = [
    {"roomId": "1661583581389099008", "name": "研学自习位(15层)"},
    {"roomId": "1666357033522270208", "name": "研学自习位(14层)"},
]

DEFAULT_CONFIG = {
    "rooms": DEFAULT_ROOMS,
    "sliderAttempts": 5,
    "rememberCredentials": False,
}


def _default_config() -> dict:
    return deepcopy(DEFAULT_CONFIG)


def _merge_config(cfg: dict | None) -> dict:
    merged = _default_config()
    if isinstance(cfg, dict):
        merged.update(cfg)
    if not merged.get("rooms"):
        merged["rooms"] = deepcopy(DEFAULT_ROOMS)
    if not merged.get("sliderAttempts"):
        merged["sliderAttempts"] = DEFAULT_CONFIG["sliderAttempts"]
    if "rememberCredentials" not in merged:
        merged["rememberCredentials"] = DEFAULT_CONFIG["rememberCredentials"]
    return merged


def load_gui_config(path: str | None = None) -> dict:
    if path is None:
        path = ensure_runtime_dirs()["config"]
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _default_config()
    return _merge_config(loaded)


def save_gui_config(cfg: dict, path: str | None = None) -> None:
    if path is None:
        path = ensure_runtime_dirs()["config"]
    merged = _merge_config(cfg)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def _readable_status(summary: dict) -> str:
    status = summary.get("status")
    labels = {
        0: "已预约",
        3: "使用中",
        2: "已完成",
        1: "已结束",
        -1: "已取消",
        -3: "",
    }
    if status in labels:
        return labels[status]
    if status in ("", None):
        return ""
    return f"未知状态({status})"


TOKEN_EXPIRED_MESSAGE = "登录已失效，请重新登录"
TOKEN_EXPIRED_MARKERS = (
    "token访问过期",
    "token过期",
    "登录已过期",
    "登录失效",
    "登录已失效",
    "未登录",
    "请先登录",
    "401",
    "Unauthorized",
    "unauthorized",
)


class TokenExpiredError(RuntimeError):
    """GUI token was invalidated by another client or backend expiry."""


def is_token_expired_message(message) -> bool:
    text = str(message or "")
    return any(marker in text for marker in TOKEN_EXPIRED_MARKERS)


def _response_indicates_token_expired(response: dict) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("code") in (401, 20003):
        return True
    return is_token_expired_message(response.get("message"))


def _reservation_summary_for_gui(record: dict) -> dict:
    summary = book.reservation_summary(record)
    summary["statusText"] = _readable_status(summary)
    return summary


class GuiService:
    def __init__(self, config_path: str | None = None, state_path: str | None = None):
        paths = ensure_runtime_dirs()
        self.config_path = config_path or paths["config"]
        self.state_path = state_path or paths["state"]
        self.cfg = load_gui_config(self.config_path)
        self.client = HttpClient()
        self.token = None

    def _require_token(self) -> str:
        if not self.token:
            raise RuntimeError("请先登录")
        return self.token

    def _expire_token(self) -> None:
        self.token = None
        raise TokenExpiredError(TOKEN_EXPIRED_MESSAGE)

    def _authenticated(self, func):
        try:
            return func()
        except book.ReservationQueryError as exc:
            if is_token_expired_message(str(exc)):
                self._expire_token()
            raise

    def saved_username(self) -> str:
        return self.cfg.get("username", "")

    def has_saved_password(self) -> bool:
        return bool(self.cfg.get("password"))

    def login(self, username: str, password: str, remember: bool = False) -> str:
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            raise ValueError("请输入学工号和密码")
        token = login_mod.get_token(
            self.client, username, password, self.state_path, force_refresh=True,
        )
        self.token = token
        self.cfg["username"] = username
        self.cfg["rememberCredentials"] = bool(remember)
        self.cfg.setdefault("rooms", deepcopy(DEFAULT_ROOMS))
        self.cfg.setdefault("sliderAttempts", DEFAULT_CONFIG["sliderAttempts"])
        to_save = {
            "username": username,
            "rememberCredentials": bool(remember),
            "rooms": self.cfg.get("rooms") or deepcopy(DEFAULT_ROOMS),
            "sliderAttempts": self.cfg.get("sliderAttempts", DEFAULT_CONFIG["sliderAttempts"]),
        }
        if remember:
            to_save["password"] = password
            self.cfg["password"] = password
        else:
            self.cfg.pop("password", None)
        save_gui_config(to_save, self.config_path)
        self.cfg = load_gui_config(self.config_path)
        return token

    def login_with_saved_credentials(self, force_refresh: bool = False) -> str:
        username = self.cfg.get("username", "")
        password = self.cfg.get("password", "")
        if not username or not password:
            raise ValueError("本机未保存密码，请手动登录")
        if force_refresh:
            return self.login(username, password, remember=True)
        token = login_mod.get_token(
            self.client, username, password, self.state_path, force_refresh=False,
        )
        self.token = token
        self.cfg["rememberCredentials"] = True
        save_gui_config(self.cfg, self.config_path)
        self.cfg = load_gui_config(self.config_path)
        return token

    def records(self) -> dict:
        def load_records():
            token = self._require_token()
            records = book.all_reservation_records(self.client, token)
            appointments = records.get("appointments") or []
            breaches = records.get("breaches") or []
            return {
                "appointmentCount": len(appointments),
                "breachCount": len(breaches),
                "appointments": [_reservation_summary_for_gui(x) for x in appointments],
                "breaches": [_reservation_summary_for_gui(x) for x in breaches],
            }

        return self._authenticated(load_records)

    def rooms(self) -> list:
        def load_rooms():
            token = self._require_token()
            venues = book.query_all_rooms(self.client, token)
            rooms = []
            for venue in venues:
                venue_name = str(venue.get("name") or "").strip()
                for room in venue.get("roomList") or []:
                    rooms.append({
                        "roomId": str(room.get("id") or room.get("roomId") or ""),
                        "name": str(room.get("name") or "").strip(),
                        "venueName": venue_name,
                        "seatsNum": room.get("seatsNum"),
                        "available": bool(room.get("ifAppointment", room.get("available", True))),
                        "openDays": room.get("openDays"),
                        "recordToday": room.get("recordToday"),
                        "raw": room,
                    })
            return rooms

        return self._authenticated(load_rooms)

    def system_settings(self) -> dict:
        return self._authenticated(
            lambda: book.query_system_settings(self.client, self._require_token())
        )

    def available_date_options(self, room: dict, settings: dict, today=None) -> list:
        if today is None:
            today = date.today()
        open_days = room.get("openDays") or settings.get("advanceAppointmentDays") or 2
        try:
            open_days = int(open_days)
        except (TypeError, ValueError):
            open_days = 2
        if open_days < 1:
            open_days = 1
        non_allday = settings.get("appointmentMethod") != "ALLDAY"
        options = []
        for offset in range(open_days):
            current = today + timedelta(days=offset)
            if offset == 0:
                label = "今天 07:00-23:00"
            elif offset == 1:
                label = "明天 07:00-23:00"
            else:
                label = current.strftime("%Y-%m-%d") + " 07:00-23:00"
            item = {"date": current.strftime("%Y-%m-%d"), "label": label}
            if non_allday:
                item["note"] = "当前系统不是全天预约模式，提交前请以返回错误为准"
            options.append(item)
        return options

    def seats(self, room_id: str, date_str: str) -> list:
        def load_seats():
            seats = book.query_seats(self.client, self._require_token(), room_id, date_str)
            normalized = []
            for seat in seats:
                no = seat.get("no")
                status = seat.get("status")
                normalized.append({"no": no, "status": status, "available": status == 0})
            return sorted(normalized, key=lambda item: int(item["no"]))

        return self._authenticated(load_seats)

    def reserve(self, room_id: str, seat_no: int, date_str: str, progress=None) -> dict:
        def submit():
            token = self._require_token()
            emit = progress or (lambda _: None)
            emit("正在破解滑块验证码...")
            slider_code = solve_slider(
                self.client,
                self.cfg["username"],
                max_attempts=self.cfg.get("sliderAttempts", 5),
                log=emit,
            )
            emit("正在提交预约...")
            response = book.submit_reservation(
                self.client, token, room_id, seat_no, date_str, slider_code,
            )
            if _response_indicates_token_expired(response):
                self._expire_token()
            if response.get("status") is True and response.get("code") == 200:
                return {"ok": True, "message": "预约成功", "response": response}
            return {
                "ok": False,
                "message": response.get("message") or "预约失败",
                "response": response,
            }

        return self._authenticated(submit)
