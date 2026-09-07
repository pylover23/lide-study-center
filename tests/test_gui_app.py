import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui_app


class TestLideAppTokenExpiry(unittest.TestCase):
    def make_app(self):
        app = gui_app.LideApp.__new__(gui_app.LideApp)
        app.service = type("Service", (), {"token": "token-123"})()
        app.logged_messages = []
        app.login_calls = []
        app._append_log = app.logged_messages.append
        app.show_login = lambda: app.login_calls.append("show_login")
        return app

    def test_append_error_returns_to_login_when_token_is_expired(self):
        app = self.make_app()

        with mock.patch.object(gui_app.messagebox, "showwarning") as showwarning, \
                mock.patch.object(gui_app.messagebox, "showerror") as showerror:
            app._append_error("登录已失效，请重新登录")

        self.assertIsNone(app.service.token)
        self.assertEqual(app.login_calls, ["show_login"])
        self.assertEqual(app.logged_messages, ["登录已失效，请重新登录"])
        showwarning.assert_called_once_with("登录已失效", "登录已失效，请重新登录")
        showerror.assert_not_called()

    def test_reserve_error_returns_to_login_when_token_is_expired(self):
        app = self.make_app()

        with mock.patch.object(gui_app.messagebox, "showwarning") as showwarning, \
                mock.patch.object(gui_app.messagebox, "showerror") as showerror:
            app._reserve_error("登录已失效，请重新登录")

        self.assertIsNone(app.service.token)
        self.assertEqual(app.login_calls, ["show_login"])
        self.assertEqual(app.logged_messages, ["登录已失效，请重新登录"])
        showwarning.assert_called_once_with("登录已失效", "登录已失效，请重新登录")
        showerror.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
