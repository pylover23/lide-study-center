"""座位扫描与预约提交。

已实测字段（2026-09-02）：
- POST /kyq/static/frontApi/room/getRoomList
  body {"endDate": "YYYY-MM-DD", "roomId": "<id>", "statDate": "YYYY-MM-DD", "authorization": true}
  -> data: [{"no": 1, "status": 0|-1|...}]，status=0 可约
- POST /kyq/static/frontApi/reservation/saveReservation
  body {"code": <滑块token>, "endDate": "YYYY-MM-DD", "onDate": "YYYY-MM-DD",
        "roomId": "<id>", "mobilePhone": "", "seatNumber": <int>, "authorization": true}
"""
import logging
import time


log = logging.getLogger("book")

class ReservationQueryError(RuntimeError):
    """预约记录接口返回业务错误。"""


def _reservation_records(client, token: str, record_type: int,
                         *, first_index: int, next_index) -> list:
    """分页获取指定类型预约记录。"""
    endpoint = client.base + f"/kyq/static/frontApi/reservation/userReservation/record/{record_type}"
    headers = {"Authorization": "Bearer " + token,
               "Referer": client.base + "/kyq-v/"}
    records = []
    index = first_index
    while True:
        for attempt in range(2):
            j = client.get_json(endpoint + f"?index={index}&pageSize=20", headers=headers)
            if j.get("status") is True and j.get("code") == 200:
                break
            message = j.get("message") or "预约记录查询失败"
            if "频繁" not in message or attempt:
                raise ReservationQueryError(message)
            time.sleep(3)
        data = j.get("data") or {}
        records.extend(data.get("result") or [])
        if not data.get("next"):
            return records
        index = next_index(index)


def user_reservations(client, token: str) -> list:
    """分页获取预约记录。前端该 tab 使用 type=0，index 为页码：1, 2, ...。"""
    return _reservation_records(client, token, 0, first_index=1,
                                next_index=lambda index: index + 1)


def breach_reservations(client, token: str) -> list:
    """分页获取违约记录。前端该 tab 使用 type=1，index 为偏移量：0, 20, ...。"""
    return _reservation_records(client, token, 1, first_index=0,
                                next_index=lambda index: index + 20)


def all_reservation_records(client, token: str) -> dict:
    """同时获取预约记录与违约记录。"""
    return {
        "appointments": user_reservations(client, token),
        "breaches": breach_reservations(client, token),
    }


def _auth_headers(client, token: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "Referer": client.base + "/kyq-v/",
    }


def query_all_rooms(client, token: str) -> list:
    """Return venue list with nested roomList from /room/queryAllRoomList/."""
    j = client.get_json(
        client.base + "/kyq/static/frontApi/room/queryAllRoomList/",
        headers=_auth_headers(client, token),
    )
    if j.get("status") is not True or j.get("code") != 200:
        raise ReservationQueryError(j.get("message") or "房间列表查询失败")
    return j.get("data") or []


def query_system_settings(client, token: str) -> dict:
    """Return system settings used by the front-end booking page."""
    j = client.post_json(
        client.base + "/kyq/static/public/query/getSystemSettings",
        {"authorization": True},
        token=token,
    )
    if j.get("status") is not True or j.get("code") != 200:
        raise ReservationQueryError(j.get("message") or "系统设置查询失败")
    return j.get("data") or {}


def query_room_non_appointment_time(client, token: str, room_id: str) -> list:
    """Return dates blocked for a room; empty list means no blocked dates."""
    j = client.get_json(
        client.base + "/kyq/static/frontApi/room/queryAllRoom/nonAppointmentTime/" + str(room_id),
        headers=_auth_headers(client, token),
    )
    if j.get("status") is not True or j.get("code") != 200:
        raise ReservationQueryError(j.get("message") or "不可预约日期查询失败")
    return j.get("data") or []


def query_seat_reservation_time(client, token: str, room_id: str, seat_no: int) -> list:
    """Return per-seat unavailable date entries from /seatReservationTime/{roomId}/{seatId}."""
    j = client.get_json(
        client.base + f"/kyq/static/frontApi/room/queryAllRoom/seatReservationTime/{room_id}/{seat_no}",
        headers=_auth_headers(client, token),
    )
    if j.get("status") is not True or j.get("code") != 200:
        raise ReservationQueryError(j.get("message") or "座位可用时间查询失败")
    return j.get("data") or []


def reservation_summary(record: dict) -> dict:
    """提取适合命令行展示的预约字段，不包含手机号。"""
    return {
        "id": record.get("id", ""),
        "venue": record.get("venueName", ""),
        "room": record.get("roomName", ""),
        "seat": record.get("seatNum", ""),
        "begin": record.get("begin", ""),
        "end": record.get("end", ""),
        "status": record.get("status"),
        "signedIn": record.get("signIn", False),
        "breachDate": record.get("breachDate", ""),
        "breachReason": record.get("breachReason", ""),
    }


def query_seats(client, token: str, room_id: str, date_str: str) -> list:
    """返回 [{no, status}]；status=0 可约。"""
    body = {"endDate": date_str, "roomId": room_id,
            "statDate": date_str, "authorization": True}
    j = client.post_json(client.base + "/kyq/static/frontApi/room/getRoomList",
                         body, token=token)
    if j.get("status") is not True or j.get("code") != 200:
        raise ReservationQueryError(j.get("message") or "座位查询失败")
    return j.get("data") or []


def available_seats(client, token: str, room_id: str, date_str: str) -> list:
    return sorted((s["no"] for s in query_seats(client, token, room_id, date_str)
                   if s.get("status") == 0))


def submit_reservation(client, token: str, room_id: str, seat_no: int,
                       date_str: str, slider_code: str) -> dict:
    body = {"code": slider_code, "endDate": date_str,
            "onDate": date_str, "roomId": room_id, "mobilePhone": "",
            "seatNumber": seat_no, "authorization": True}
    j = client.post_json(client.base + "/kyq/static/frontApi/reservation/saveReservation",
                         body, token=token)
    return j


def order_candidates(seats: list, cfg: dict) -> list:
    """按 config 座位偏好排序：偏好区间优先，其余按 seatOrder(asc) 排列。"""
    pref_lo = cfg.get("seatPreferLow", 1)
    pref_hi = cfg.get("seatPreferHigh", 0)
    excluded = set(cfg.get("seatExclude", []))
    seats = [s for s in seats if s not in excluded]

    def key(no):
        in_pref = pref_hi > 0 and pref_lo <= no <= pref_hi
        return (0 if in_pref else 1, no)
    return sorted(seats, key=key)
