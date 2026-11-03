import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import book, login


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeClient:
    base = "https://yxkj.ruc.edu.cn"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def raw(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return FakeResponse(self.response)


class TestTokenProbe(unittest.TestCase):
    def test_probe_distinguishes_alive_expired_and_indeterminate(self):
        self.assertIs(
            login.probe_token(FakeClient({"data": [{"id": 1}]}), "token"),
            login.TokenProbeStatus.ALIVE,
        )
        self.assertIs(
            login.probe_token(
                FakeClient({"status": False, "code": 20003, "message": "token访问过期"}),
                "token",
            ),
            login.TokenProbeStatus.EXPIRED,
        )
        self.assertIs(
            login.probe_token(FakeClient(error=OSError("network")), "token"),
            login.TokenProbeStatus.INDETERMINATE,
        )
        self.assertIs(
            login.probe_token(FakeClient({"status": False, "code": 500, "message": "unknown"}), "token"),
            login.TokenProbeStatus.INDETERMINATE,
        )

    def test_get_token_reuses_cache_when_probe_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump({"token": "cached"}, handle)
            with mock.patch.object(
                login,
                "probe_token",
                return_value=login.TokenProbeStatus.INDETERMINATE,
            ), mock.patch.object(login, "_cas_login") as cas_login:
                token = login.get_token(object(), "user", "pass", state_path)

        self.assertEqual(token, "cached")
        cas_login.assert_not_called()

    def test_get_token_refreshes_only_when_probe_is_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump({"token": "old"}, handle)
            with mock.patch.object(
                login,
                "probe_token",
                return_value=login.TokenProbeStatus.EXPIRED,
            ), mock.patch.object(login, "_ocr_engine", return_value=object()), \
                    mock.patch.object(login, "_cas_login") as cas_login, \
                    mock.patch.object(login, "_oauth_params", return_value={}), \
                    mock.patch.object(login, "_applet_bind", return_value="new"):
                token = login.get_token(object(), "user", "pass", state_path)
            with open(state_path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)

        self.assertEqual(token, "new")
        self.assertEqual(saved["token"], "new")
        cas_login.assert_called_once()

    def test_token_expiry_detection_accepts_all_supported_value_shapes(self):
        query_error = book.ReservationQueryError(
            {"code": 20003, "message": "unrelated wording"},
            "query failed",
        )
        self.assertTrue(login.is_token_expired({"code": 401}))
        self.assertTrue(login.is_token_expired(query_error))
        self.assertTrue(login.is_token_expired("Unauthorized"))
        self.assertFalse(login.is_token_expired({"code": 500, "message": "busy"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
