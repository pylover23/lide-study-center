import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui_workers import TkTaskRunner


class FakeRoot:
    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)

    def pump_until(self, predicate, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            callbacks, self.callbacks = self.callbacks, []
            for callback in callbacks:
                callback()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition not reached")


class TestTkTaskRunner(unittest.TestCase):
    def test_run_invokes_success_callback_from_after_poll(self):
        root = FakeRoot()
        runner = TkTaskRunner(root)
        results = []

        runner.run("work", lambda: "done", results.append)
        root.pump_until(lambda: results)

        self.assertEqual(results, ["done"])

    def test_run_invokes_error_callback_with_exception_message(self):
        root = FakeRoot()
        runner = TkTaskRunner(root)
        errors = []

        def fail():
            raise RuntimeError("boom")

        runner.run("work", fail, lambda _: None, on_error=errors.append)
        root.pump_until(lambda: errors)

        self.assertEqual(errors, ["boom"])

    def test_concurrent_runs_keep_results_bound_to_their_callbacks(self):
        root = FakeRoot()
        runner = TkTaskRunner(root)
        slow_can_finish = threading.Event()
        slow_results = []
        fast_results = []

        def slow():
            slow_can_finish.wait(1)
            return "slow-result"

        runner.run("slow", slow, slow_results.append)
        runner.run("fast", lambda: "fast-result", fast_results.append)
        root.pump_until(lambda: slow_results or fast_results)
        slow_can_finish.set()
        root.pump_until(lambda: slow_results and fast_results)

        self.assertEqual(slow_results, ["slow-result"])
        self.assertEqual(fast_results, ["fast-result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
