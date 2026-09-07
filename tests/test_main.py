import argparse
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class TestRecordsCommand(unittest.TestCase):
    def test_prints_sanitized_appointment_and_breach_history(self):
        appointment = {
            "id": "reservation-1", "venueName": "立德楼",
            "roomName": "研学中心学生工位（15层）", "seatNum": "23",
            "begin": "2026-09-02 08:00:00", "end": "2026-09-02 23:00:00",
            "status": 1, "signIn": True, "breachDate": "", "breachReason": "",
            "mobilePhone": "13800000000",
        }
        breach = {
            "id": "breach-1", "venueName": "立德楼",
            "roomName": "研学中心学生工位（14层）", "seatNum": "9",
            "begin": "2026-08-01 08:00:00", "end": "2026-08-01 23:00:00",
            "status": -2, "signIn": False,
            "breachDate": "2026-08-01", "breachReason": "使用时长不足",
            "mobilePhone": "13800000000",
        }
        output = io.StringIO()
        with mock.patch.object(main, "load_cfg", return_value={"username": "user", "password": "pass"}), \
                mock.patch.object(main, "HttpClient", return_value=object()), \
                mock.patch.object(main.login, "get_token", return_value="token"), \
                mock.patch.object(main.book, "all_reservation_records", return_value={
                    "appointments": [appointment],
                    "breaches": [breach],
                }), \
                redirect_stdout(output):
            exit_code = main.cmd_records(argparse.Namespace())

        self.assertEqual(exit_code, main.EXIT_OK)
        self.assertEqual(json.loads(output.getvalue()), {
            "appointmentCount": 1,
            "breachCount": 1,
            "appointments": [{
                "id": "reservation-1", "venue": "立德楼",
                "room": "研学中心学生工位（15层）", "seat": "23",
                "begin": "2026-09-02 08:00:00", "end": "2026-09-02 23:00:00",
                "status": 1, "signedIn": True, "breachDate": "", "breachReason": "",
            }],
            "breaches": [{
                "id": "breach-1", "venue": "立德楼",
                "room": "研学中心学生工位（14层）", "seat": "9",
                "begin": "2026-08-01 08:00:00", "end": "2026-08-01 23:00:00",
                "status": -2, "signedIn": False,
                "breachDate": "2026-08-01", "breachReason": "使用时长不足",
            }],
        })

    def test_returns_auth_exit_code_when_history_login_fails(self):
        with mock.patch.object(main, "load_cfg", return_value={"username": "user", "password": "pass"}), \
                mock.patch.object(main, "HttpClient", return_value=object()), \
                mock.patch.object(main.login, "get_token", side_effect=main.login.AuthError("bad login")):
            exit_code = main.cmd_records(argparse.Namespace())

        self.assertEqual(exit_code, main.EXIT_AUTH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
