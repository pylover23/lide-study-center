import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import book


class FakeClient:
    base = "https://yxkj.ruc.edu.cn"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response


    def post_json(self, url, body, **kwargs):
        self.calls.append((url, body, kwargs))
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response

def record(record_id):
    return {
        "properties": {}, "createdDate": "2026-09-01 07:00:00",
        "updatedDate": "2026-09-01 07:00:00", "version": "1",
        "id": record_id, "begin": "2026-09-02 08:00:00",
        "end": "2026-09-02 23:00:00", "signIn": False, "status": 0,
        "seatNum": "12", "venueName": "立德楼", "roomName": "15层",
        "breachReason": "", "breachDate": "", "mobilePhone": "",
        "cabinetOpen": False, "cabinetNum": "", "useTime": "",
    }


class TestUserReservations(unittest.TestCase):
    def test_fetches_every_page_of_reservation_history(self):
        base = FakeClient.base + "/kyq/static/frontApi/reservation/userReservation/record/0"
        client = FakeClient({
            base + "?index=1&pageSize=20": {
                "status": True, "code": 200, "message": "操作成功",
                "data": {"result": [record("first")], "next": True},
            },
            base + "?index=2&pageSize=20": {
                "status": True, "code": 200, "message": "操作成功",
                "data": {"result": [record("second")], "next": False},
            },
        })

        records = book.user_reservations(client, "token-value")

        self.assertEqual([item["id"] for item in records], ["first", "second"])
        self.assertEqual(
            [url for url, _ in client.calls],
            [base + "?index=1&pageSize=20", base + "?index=2&pageSize=20"],
        )
        self.assertEqual(
            client.calls[0][1]["headers"]["Authorization"], "Bearer token-value",
        )

    def test_rejects_a_business_error_response(self):
        base = FakeClient.base + "/kyq/static/frontApi/reservation/userReservation/record/0"
        client = FakeClient({
            base + "?index=1&pageSize=20": {
                "status": False, "code": 20003, "message": "token访问过期",
            },
        })

        with self.assertRaisesRegex(book.ReservationQueryError, "token访问过期"):
            book.user_reservations(client, "token-value")

    def test_retries_a_rate_limited_history_page(self):
        base = FakeClient.base + "/kyq/static/frontApi/reservation/userReservation/record/0"
        client = FakeClient({
            base + "?index=1&pageSize=20": [
                {"status": False, "code": 429, "message": "请勿频繁操作"},
                {
                    "status": True, "code": 200, "message": "操作成功",
                    "data": {"result": [record("first")], "next": False},
                },
            ],
        })

        with mock.patch("time.sleep"):
            records = book.user_reservations(client, "token-value")

        self.assertEqual([item["id"] for item in records], ["first"])

    def test_fetches_breach_records_with_frontend_offset_pagination(self):
        base = FakeClient.base + "/kyq/static/frontApi/reservation/userReservation/record/1"
        client = FakeClient({
            base + "?index=0&pageSize=20": {
                "status": True, "code": 200, "message": "操作成功",
                "data": {"result": [record("breach-first")], "next": True},
            },
            base + "?index=20&pageSize=20": {
                "status": True, "code": 200, "message": "操作成功",
                "data": {"result": [record("breach-second")], "next": False},
            },
        })

        records = book.breach_reservations(client, "token-value")

        self.assertEqual([item["id"] for item in records], ["breach-first", "breach-second"])
        self.assertEqual(
            [url for url, _ in client.calls],
            [base + "?index=0&pageSize=20", base + "?index=20&pageSize=20"],
        )

    def test_fetches_all_record_categories(self):
        appointment = record("appointment")
        breach = record("breach")
        with mock.patch.object(book, "user_reservations", return_value=[appointment]) as user_fn, \
                mock.patch.object(book, "breach_reservations", return_value=[breach]) as breach_fn:
            records = book.all_reservation_records(object(), "token-value")

        self.assertEqual(records, {
            "appointments": [appointment],
            "breaches": [breach],
        })
        user_fn.assert_called_once()
        breach_fn.assert_called_once()

    def test_summarizes_reservation_without_private_phone_data(self):
        item = record("first")
        item.update({
            "venueName": "立德楼",
            "roomName": "研学中心学生工位（15层）",
            "seatNum": "23",
            "status": 1,
            "signIn": True,
            "breachDate": "",
            "breachReason": "",
        })

        summary = book.reservation_summary(item)

        self.assertEqual(summary, {
            "id": "first",
            "venue": "立德楼",
            "room": "研学中心学生工位（15层）",
            "seat": "23",
            "begin": "2026-09-02 08:00:00",
            "end": "2026-09-02 23:00:00",
            "status": 1,
            "signedIn": True,
            "breachDate": "",
            "breachReason": "",
        })


class TestRoomAndSettingsApis(unittest.TestCase):
    def test_queries_all_rooms_with_authorization_headers(self):
        url = FakeClient.base + "/kyq/static/frontApi/room/queryAllRoomList/"
        client = FakeClient({
            url: {"status": True, "code": 200, "data": [{"name": "立德楼", "roomList": []}]},
        })

        rooms = book.query_all_rooms(client, "token-value")

        self.assertEqual(rooms, [{"name": "立德楼", "roomList": []}])
        self.assertEqual(client.calls[0][1]["headers"]["Authorization"], "Bearer token-value")
        self.assertEqual(client.calls[0][1]["headers"]["Referer"], FakeClient.base + "/kyq-v/")

    def test_rejects_all_rooms_business_error(self):
        url = FakeClient.base + "/kyq/static/frontApi/room/queryAllRoomList/"
        client = FakeClient({
            url: {"status": False, "code": 500, "message": "房间错误"},
        })

        with self.assertRaisesRegex(book.ReservationQueryError, "房间错误"):
            book.query_all_rooms(client, "token-value")

    def test_queries_system_settings_with_authorization_body(self):
        url = FakeClient.base + "/kyq/static/public/query/getSystemSettings"
        client = FakeClient({
            url: {"status": True, "code": 200, "data": {"appointmentMethod": "ALLDAY"}},
        })

        settings = book.query_system_settings(client, "token-value")

        self.assertEqual(settings, {"appointmentMethod": "ALLDAY"})
        self.assertEqual(client.calls[0], (
            url,
            {"authorization": True},
            {"token": "token-value"},
        ))

    def test_queries_room_blocked_dates(self):
        url = FakeClient.base + "/kyq/static/frontApi/room/queryAllRoom/nonAppointmentTime/room-1"
        client = FakeClient({
            url: {"status": True, "code": 200, "data": ["2026-09-03"]},
        })

        dates = book.query_room_non_appointment_time(client, "token-value", "room-1")

        self.assertEqual(dates, ["2026-09-03"])

    def test_queries_seat_reservation_time(self):
        url = FakeClient.base + "/kyq/static/frontApi/room/queryAllRoom/seatReservationTime/room-1/12"
        client = FakeClient({
            url: {"status": True, "code": 200, "data": [{"date": "2026-09-03"}]},
        })

        entries = book.query_seat_reservation_time(client, "token-value", "room-1", 12)

        self.assertEqual(entries, [{"date": "2026-09-03"}])


class TestSeatPayloads(unittest.TestCase):
    def test_query_seats_sends_date_string_end_date(self):
        url = FakeClient.base + "/kyq/static/frontApi/room/getRoomList"
        client = FakeClient({
            url: {"status": True, "code": 200, "data": [{"no": 1, "status": 0}]},
        })

        seats = book.query_seats(client, "token-value", "room-1", "2026-09-03")

        self.assertEqual(seats, [{"no": 1, "status": 0}])
        self.assertEqual(client.calls[0], (
            url,
            {
                "endDate": "2026-09-03",
                "roomId": "room-1",
                "statDate": "2026-09-03",
                "authorization": True,
            },
            {"token": "token-value"},
        ))

    def test_query_seats_rejects_business_error_response(self):
        url = FakeClient.base + "/kyq/static/frontApi/room/getRoomList"
        client = FakeClient({
            url: {"status": False, "code": 20003, "message": "token访问过期"},
        })

        with self.assertRaisesRegex(book.ReservationQueryError, "token访问过期"):
            book.query_seats(client, "token-value", "room-1", "2026-09-03")

    def test_submit_reservation_sends_date_string_end_date(self):
        url = FakeClient.base + "/kyq/static/frontApi/reservation/saveReservation"
        client = FakeClient({
            url: {"status": True, "code": 200, "message": "操作成功"},
        })

        response = book.submit_reservation(
            client, "token-value", "room-1", 12, "2026-09-03", "SLIDER_token",
        )

        self.assertEqual(response, {"status": True, "code": 200, "message": "操作成功"})
        self.assertEqual(client.calls[0], (
            url,
            {
                "code": "SLIDER_token",
                "endDate": "2026-09-03",
                "onDate": "2026-09-03",
                "roomId": "room-1",
                "mobilePhone": "",
                "seatNumber": 12,
                "authorization": True,
            },
            {"token": "token-value"},
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
