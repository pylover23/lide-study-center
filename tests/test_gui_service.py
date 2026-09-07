import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import book
from lib.gui_service import DEFAULT_ROOMS, GuiService, load_gui_config, save_gui_config


class TestGuiConfig(unittest.TestCase):
    def test_load_gui_config_returns_default_rooms_when_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_gui_config(os.path.join(tmp, "missing.json"))

        self.assertEqual(cfg["rooms"], DEFAULT_ROOMS)
        self.assertFalse(cfg["rememberCredentials"])

    def test_save_gui_config_round_trips_without_repo_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gui_config.json")
            cfg = {
                "username": "20260001",
                "rememberCredentials": True,
                "password": "secret",
                "rooms": [{"roomId": "room-1", "name": "一层"}],
                "sliderAttempts": 3,
            }

            save_gui_config(cfg, path)
            loaded = load_gui_config(path)

        self.assertEqual(loaded["username"], "20260001")
        self.assertEqual(loaded["password"], "secret")
        self.assertEqual(loaded["rooms"], [{"roomId": "room-1", "name": "一层"}])
        self.assertEqual(loaded["sliderAttempts"], 3)


class TestGuiService(unittest.TestCase):
    def make_service(self, cfg=None):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = os.path.join(self.tmpdir.name, "gui_config.json")
        self.state_path = os.path.join(self.tmpdir.name, "state.json")
        if cfg is not None:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        return GuiService(config_path=self.config_path, state_path=self.state_path)

    @mock.patch("lib.gui_service.login_mod.get_token", return_value="token-123")
    def test_login_without_remembering_password_saves_username_only(self, get_token):
        service = self.make_service()

        token = service.login(" 20260001 ", "secret", remember=False)

        self.assertEqual(token, "token-123")
        self.assertEqual(service.token, "token-123")
        get_token.assert_called_once_with(
            service.client, "20260001", "secret", self.state_path, force_refresh=True,
        )
        with open(self.config_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["username"], "20260001")
        self.assertFalse(saved["rememberCredentials"])
        self.assertNotIn("password", saved)

    def test_records_returns_counts_and_public_summaries_without_phone(self):
        service = self.make_service()
        service.token = "token-123"
        appointment = {
            "id": "a1",
            "begin": "2026-09-02 08:00:00",
            "end": "2026-09-02 23:00:00",
            "roomName": "15层",
            "seatNum": "12",
            "status": 1,
            "signIn": True,
            "mobilePhone": "13800000000",
        }
        breach = {
            "id": "b1",
            "begin": "2026-09-01 08:00:00",
            "end": "2026-09-01 23:00:00",
            "roomName": "14层",
            "seatNum": "9",
            "status": -1,
            "mobilePhone": "13800000000",
            "breachReason": "未签到",
        }
        with mock.patch.object(book, "all_reservation_records", return_value={
            "appointments": [appointment],
            "breaches": [breach],
        }):
            records = service.records()

        self.assertEqual(records["appointmentCount"], 1)
        self.assertEqual(records["breachCount"], 1)
        self.assertEqual(records["appointments"][0]["id"], "a1")
        self.assertEqual(records["breaches"][0]["breachReason"], "未签到")
        self.assertNotIn("mobilePhone", records["appointments"][0])
        self.assertNotIn("mobilePhone", records["breaches"][0])
        self.assertEqual(records["appointments"][0]["statusText"], "已结束")
        self.assertEqual(records["breaches"][0]["statusText"], "已取消")

    def test_records_preserve_unknown_status_code_in_readable_label(self):
        service = self.make_service()
        service.token = "token-123"
        appointment = {"id": "a2", "status": 99}
        with mock.patch.object(book, "all_reservation_records", return_value={
            "appointments": [appointment],
            "breaches": [],
        }):
            records = service.records()

        self.assertEqual(records["appointments"][0]["status"], 99)
        self.assertEqual(records["appointments"][0]["statusText"], "未知状态(99)")

    def test_records_convert_known_status_codes_to_user_facing_labels(self):
        service = self.make_service()
        service.token = "token-123"
        appointments = [
            {"id": "reserved", "status": 0},
            {"id": "using", "status": 3},
            {"id": "completed", "status": 2},
            {"id": "ended", "status": 1},
            {"id": "cancelled", "status": -1},
            {"id": "breach", "status": -3},
        ]
        with mock.patch.object(book, "all_reservation_records", return_value={
            "appointments": appointments,
            "breaches": [],
        }):
            records = service.records()

        self.assertEqual(
            [item["statusText"] for item in records["appointments"]],
            ["已预约", "使用中", "已完成", "已结束", "已取消", ""],
        )

    def test_records_clear_token_when_backend_reports_expired_token(self):
        service = self.make_service()
        service.token = "token-123"
        with mock.patch.object(
            book,
            "all_reservation_records",
            side_effect=book.ReservationQueryError("token访问过期"),
        ):
            with self.assertRaisesRegex(RuntimeError, "登录已失效，请重新登录"):
                service.records()

        self.assertIsNone(service.token)

    @mock.patch("lib.gui_service.book.submit_reservation", return_value={
        "status": False,
        "code": 20003,
        "message": "token访问过期",
    })
    @mock.patch("lib.gui_service.solve_slider", return_value="slider-code")
    def test_reserve_clears_token_when_submit_reports_expired_token(self, solve_slider, submit):
        service = self.make_service({
            "username": "20260001",
            "password": "secret",
            "rooms": DEFAULT_ROOMS,
            "sliderAttempts": 5,
            "rememberCredentials": True,
        })
        service.token = "token-123"

        with self.assertRaisesRegex(RuntimeError, "登录已失效，请重新登录"):
            service.reserve("room-1", 12, "2026-09-02", progress=lambda _message: None)

        self.assertIsNone(service.token)

    def test_rooms_flattens_venue_response_into_room_cards(self):
        service = self.make_service()
        service.token = "token-123"
        with mock.patch.object(book, "query_all_rooms", return_value=[{
            "name": " 立德楼 ",
            "roomList": [{
                "id": "room-1",
                "name": "研学中心学生工位（15层）",
                "seatsNum": 250,
                "ifAppointment": True,
                "openDays": 2,
                "recordToday": False,
            }],
        }]):
            rooms = service.rooms()

        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["roomId"], "room-1")
        self.assertEqual(rooms[0]["name"], "研学中心学生工位（15层）")
        self.assertEqual(rooms[0]["venueName"], "立德楼")
        self.assertEqual(rooms[0]["seatsNum"], 250)
        self.assertTrue(rooms[0]["available"])
        self.assertEqual(rooms[0]["openDays"], 2)

    def test_available_date_options_labels_today_and_tomorrow(self):
        service = self.make_service()

        options = service.available_date_options(
            {"openDays": 2}, {"appointmentMethod": "ALLDAY"}, today=date(2026, 9, 1),
        )

        self.assertEqual([item["label"] for item in options], [
            "今天 07:00-23:00",
            "明天 07:00-23:00",
        ])
        self.assertEqual([item["date"] for item in options], ["2026-09-01", "2026-09-02"])

    @mock.patch("lib.gui_service.book.submit_reservation", return_value={"status": True, "code": 200})
    @mock.patch("lib.gui_service.solve_slider", return_value="slider-code")
    def test_reserve_solves_slider_submits_selected_seat_once_and_reports_progress(self, solve_slider, submit):
        service = self.make_service({
            "username": "20260001",
            "password": "secret",
            "rooms": DEFAULT_ROOMS,
            "sliderAttempts": 5,
            "rememberCredentials": True,
        })
        service.token = "token-123"
        messages = []

        result = service.reserve("room-1", 12, "2026-09-02", progress=messages.append)

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "预约成功")
        solve_slider.assert_called_once_with(
            service.client, "20260001", max_attempts=5, log=messages.append,
        )
        submit.assert_called_once_with(service.client, "token-123", "room-1", 12, "2026-09-02", "slider-code")
        self.assertTrue(any("滑块验证码" in msg for msg in messages))
        self.assertTrue(any("提交预约" in msg for msg in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
