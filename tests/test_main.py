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



class TestScheduledPreheat(unittest.TestCase):
    def test_scheduled_preheat_authenticates_without_generating_slider(self):
        cfg = {
            "username": "user",
            "password": "pass",
            "preWarmSec": 37,
        }
        client = object()
        events = []

        def grab(_cfg, target):
            events.append(("query", target))
            main.book.available_seats(client, "token", "room-1", target)
            code = main.solve_slider(client, "user")
            events.append(("solve", code))
            main.book.submit_reservation(client, "token", "room-1", 23, target, code)
            events.append(("submit", 23))
            return main.EXIT_OK

        def schedule(hhmm, job, on_tick=None, pre_warm_sec=60):
            self.assertEqual((hhmm, pre_warm_sec), ("07:00", 37))
            on_tick()
            self.assertEqual(events, [])
            solve.assert_not_called()
            submit.assert_not_called()
            job()

        args = argparse.Namespace(at="07:00", day="today")
        with mock.patch.object(main, "load_cfg", return_value=cfg), \
                mock.patch.object(main, "HttpClient", return_value=client), \
                mock.patch.object(main.login, "get_token", return_value="token") as get_token, \
                mock.patch.object(main.book, "available_seats", return_value=[23]), \
                mock.patch.object(main, "solve_slider", return_value="fresh-code") as solve, \
                mock.patch.object(main.book, "submit_reservation",
                                  return_value={"status": True, "code": 200}) as submit, \
                mock.patch.object(main, "do_grab", side_effect=grab), \
                mock.patch("lib.scheduler.run_daily_at", side_effect=schedule):
            self.assertEqual(main.cmd_run(args), main.EXIT_OK)

        get_token.assert_called_once_with(client, "user", "pass", main.STATE_FILE)
        self.assertEqual([event[0] for event in events], ["query", "solve", "submit"])


class FakeGrabClient:
    def __init__(self):
        self.request_count = 0
        self.stats = {
            "slider_fail": 0,
            "submit_fail": 0,
            "captcha_reject": 0,
            "rate_limit": 0,
            "token_refresh": 0,
            "query_fail": 0,
        }


class TestBookingRecovery(unittest.TestCase):
    def setUp(self):
        self.client = FakeGrabClient()
        self.watchdog = mock.Mock()
        self.cfg = {
            "username": "user",
            "password": "pass",
            "rooms": [{"roomId": "room-1", "name": "preferred-room"}],
            "sliderRetries": 3,
            "sliderAttempts": 2,
            "maxSeatsTry": 8,
            "maxRequests": 120,
            "watchdogSec": 30,
        }

    def test_captcha_overload_and_token_expiry_keep_preferred_seat(self):
        submissions = [
            {"status": False, "code": 400, "message": "验证码错误"},
            {"status": False, "code": 20003, "message": "token访问过期"},
            {"status": True, "code": 200, "message": "ok"},
        ]
        saved_states = []
        with mock.patch.object(main, "HttpClient", return_value=self.client), \
                mock.patch.object(main, "start_watchdog", return_value=self.watchdog), \
                mock.patch.object(main.login, "load_state",
                                  side_effect=[{}, {"token": "new-token"}]), \
                mock.patch.object(main.login, "save_state",
                                  side_effect=lambda state, _path: saved_states.append(dict(state))), \
                mock.patch.object(main.login, "get_token",
                                  side_effect=["old-token", "new-token"]) as get_token, \
                mock.patch.object(main.book, "available_seats", return_value=[23, 24]), \
                mock.patch.object(
                    main,
                    "solve_slider",
                    side_effect=[
                        "slider-1",
                        main.SliderServiceUnavailable(
                            {"code": 5003, "message": "请求触发过载保护"},
                        ),
                        "slider-2",
                        "slider-3",
                    ],
                ), \
                mock.patch.object(main.book, "submit_reservation",
                                  side_effect=submissions) as submit, \
                mock.patch.object(main, "save_artifact") as save_artifact, \
                mock.patch.object(main, "_summary") as summary, \
                mock.patch.object(main.random, "uniform", return_value=1.5), \
                mock.patch.object(main.time, "sleep") as sleep:
            result = main.do_grab(self.cfg, "2026-11-04")

        self.assertEqual(result, main.EXIT_OK)
        self.assertEqual([call.args[3] for call in submit.call_args_list], [23, 23, 23])
        self.assertEqual(
            [call.args[5] for call in submit.call_args_list],
            ["slider-1", "slider-2", "slider-3"],
        )
        self.assertEqual(save_artifact.call_count, 3)
        self.assertEqual(get_token.call_count, 2)
        self.assertEqual(self.client.stats["captcha_reject"], 1)
        self.assertEqual(self.client.stats["token_refresh"], 1)
        self.assertIn(mock.call(5), sleep.call_args_list)
        self.assertEqual(saved_states[0]["token"], "new-token")
        self.assertEqual(saved_states[0]["lastSuccessDate"], "2026-11-04")
        summary.assert_called_once()
        self.watchdog.cancel.assert_called_once()

    def test_second_token_expiry_stops_after_one_refresh(self):
        expired = {"status": False, "code": 20003, "message": "token访问过期"}
        with mock.patch.object(main, "HttpClient", return_value=self.client), \
                mock.patch.object(main, "start_watchdog", return_value=self.watchdog), \
                mock.patch.object(main.login, "load_state", return_value={}), \
                mock.patch.object(main.login, "get_token",
                                  side_effect=["old-token", "new-token"]) as get_token, \
                mock.patch.object(main.book, "available_seats", return_value=[23]), \
                mock.patch.object(main, "solve_slider",
                                  side_effect=["slider-1", "slider-2"]), \
                mock.patch.object(main.book, "submit_reservation",
                                  side_effect=[expired, expired]) as submit, \
                mock.patch.object(main, "save_artifact"), \
                mock.patch.object(main, "_summary") as summary:
            result = main.do_grab(self.cfg, "2026-11-04")

        self.assertEqual(result, main.EXIT_AUTH)
        self.assertEqual(get_token.call_count, 2)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(summary.call_args.args[2], "auth_fail")
        self.watchdog.cancel.assert_called_once()

    def test_query_token_expiry_refreshes_instead_of_reporting_full(self):
        expired = main.book.ReservationQueryError(
            {"status": False, "code": 20003, "message": "token访问过期"},
            "座位查询失败",
        )
        summaries = []
        with mock.patch.object(main, "HttpClient", return_value=self.client), \
                mock.patch.object(main, "start_watchdog", return_value=self.watchdog), \
                mock.patch.object(main.login, "load_state",
                                  side_effect=[{}, {"token": "new-token"}]), \
                mock.patch.object(main.login, "save_state"), \
                mock.patch.object(main.login, "get_token",
                                  side_effect=["old-token", "new-token"]) as get_token, \
                mock.patch.object(main.book, "available_seats",
                                  side_effect=[expired, [23]]), \
                mock.patch.object(main, "solve_slider", return_value="slider-new"), \
                mock.patch.object(main.book, "submit_reservation",
                                  return_value={"status": True, "code": 200}), \
                mock.patch.object(main, "save_artifact"), \
                mock.patch.object(
                    main,
                    "_summary",
                    side_effect=lambda _cfg, _target, result, *_args: summaries.append(result),
                ):
            result = main.do_grab(self.cfg, "2026-11-04")

        self.assertEqual(result, main.EXIT_OK)
        self.assertEqual(get_token.call_count, 2)
        self.assertEqual(summaries, ["success"])
        self.assertNotIn("full", summaries)

    def test_stale_first_eight_candidates_do_not_starve_later_seats(self):
        submitted = []

        def submit(_client, _token, _room_id, seat, _target, _code):
            submitted.append(seat)
            if seat < 9:
                return {"status": False, "code": 409, "message": "座位已被占用"}
            return {"status": True, "code": 200, "message": "ok"}

        with mock.patch.object(main, "HttpClient", return_value=self.client), \
                mock.patch.object(main, "start_watchdog", return_value=self.watchdog), \
                mock.patch.object(main.login, "load_state", side_effect=[{}, {}]), \
                mock.patch.object(main.login, "save_state"), \
                mock.patch.object(main.login, "get_token", return_value="token"), \
                mock.patch.object(main.book, "available_seats",
                                  return_value=list(range(1, 11))) as query, \
                mock.patch.object(main, "solve_slider",
                                  side_effect=["slider-%d" % i for i in range(1, 10)]), \
                mock.patch.object(main.book, "submit_reservation", side_effect=submit), \
                mock.patch.object(main, "save_artifact"), \
                mock.patch.object(main, "_summary"):
            result = main.do_grab(self.cfg, "2026-11-04")

        self.assertEqual(result, main.EXIT_OK)
        self.assertEqual(submitted, list(range(1, 10)))
        self.assertEqual(query.call_count, 2)

    def test_all_room_queries_failing_returns_query_error(self):
        summaries = []
        with mock.patch.object(main, "HttpClient", return_value=self.client), \
                mock.patch.object(main, "start_watchdog", return_value=self.watchdog), \
                mock.patch.object(main.login, "load_state", return_value={}), \
                mock.patch.object(main.login, "get_token", return_value="token"), \
                mock.patch.object(main.book, "available_seats",
                                  side_effect=RuntimeError("backend down")) as query, \
                mock.patch.object(main, "solve_slider") as solve, \
                mock.patch.object(main.book, "submit_reservation") as submit, \
                mock.patch.object(main.time, "sleep") as sleep, \
                mock.patch.object(
                    main,
                    "_summary",
                    side_effect=lambda _cfg, _target, result, *_args: summaries.append(result),
                ):
            result = main.do_grab(self.cfg, "2026-11-04")

        self.assertEqual(result, main.EXIT_UNKNOWN)
        self.assertEqual(query.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        self.assertEqual(summaries, ["query_fail"])
        solve.assert_not_called()
        submit.assert_not_called()
        self.watchdog.cancel.assert_called_once()

if __name__ == "__main__":
    unittest.main(verbosity=2)
