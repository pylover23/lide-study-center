import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from lib import book
from lib.httpclient import HttpClient, RequestLimitError


class TestHttpClientBoundaries(unittest.TestCase):
    def test_third_physical_request_is_blocked_before_session(self):
        client = HttpClient(max_requests=2)
        client.session = mock.Mock()
        client.session.request.return_value = mock.Mock()

        client.raw("GET", "https://example.test/one")
        client.raw("GET", "https://example.test/two")
        with self.assertRaises(RequestLimitError):
            client.raw("GET", "https://example.test/three")

        self.assertEqual(client.session.request.call_count, 2)
        self.assertEqual(client.request_count, 2)

    def test_normal_request_retries_transport_failure(self):
        client = HttpClient()
        response = mock.Mock()
        client.session = mock.Mock()
        client.session.request.side_effect = [
            requests.ConnectionError("temporary"),
            response,
        ]

        with mock.patch("lib.httpclient.time.sleep"):
            actual = client.raw("GET", "https://example.test/read")

        self.assertIs(actual, response)
        self.assertEqual(client.session.request.call_count, 2)
        self.assertEqual(client.request_count, 2)

    def test_submit_transport_failure_is_not_replayed(self):
        client = HttpClient(base="https://example.test")
        client.session = mock.Mock()
        client.session.request.side_effect = requests.ConnectionError("lost response")

        with self.assertRaises(requests.ConnectionError):
            book.submit_reservation(
                client,
                "token",
                "room-1",
                23,
                "2026-11-04",
                "single-use-slider",
            )

        self.assertEqual(client.session.request.call_count, 1)
        self.assertEqual(client.request_count, 1)

    def test_slider_budget_exhaustion_is_reported_as_request_limit(self):
        client = type("Client", (), {
            "request_count": 2,
            "stats": {
                "slider_fail": 0,
                "submit_fail": 0,
                "captcha_reject": 0,
                "rate_limit": 0,
                "token_refresh": 0,
                "query_fail": 0,
            },
        })()
        watchdog = mock.Mock()
        summaries = []
        cfg = {
            "username": "user",
            "password": "pass",
            "rooms": [{"roomId": "room-1", "name": "room"}],
            "maxRequests": 2,
        }

        with mock.patch.object(main, "HttpClient", return_value=client), \
                mock.patch.object(main, "start_watchdog", return_value=watchdog), \
                mock.patch.object(main.login, "load_state", return_value={}), \
                mock.patch.object(main.login, "get_token", return_value="token"), \
                mock.patch.object(main.book, "available_seats", return_value=[23]), \
                mock.patch.object(main, "solve_slider",
                                  side_effect=RequestLimitError("limit")), \
                mock.patch.object(main.book, "submit_reservation") as submit, \
                mock.patch.object(
                    main,
                    "_summary",
                    side_effect=lambda _cfg, _target, result, *_args: summaries.append(result),
                ):
            result = main.do_grab(cfg, "2026-11-04")

        self.assertEqual(result, main.EXIT_FULL)
        self.assertEqual(summaries, ["req_limit"])
        submit.assert_not_called()
        watchdog.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
