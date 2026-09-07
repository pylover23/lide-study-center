"""scheduler 单测：FakeDT 完全控制时钟，不真等待。"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import scheduler


def make_fake_dt(times):
    """按给定序列返回 now()；耗尽后固定返回最后一个值（防 StopIteration）。"""
    it = iter(times)
    last = times[-1]

    class FakeDT(datetime):
        @classmethod
        def now(cls):
            try:
                return next(it)
            except StopIteration:
                return last
    return FakeDT


BASE = datetime(2026, 8, 31, 23, 0)  # 触发时刻 23:58 之前


class TestScheduler(unittest.TestCase):
    def test_job_exception_swallowed(self):
        """job 抛普通异常：被捕获，循环继续到第二轮。"""
        times = [
            BASE,                                        # 轮1 计算 target
            BASE, BASE + timedelta(minutes=58),          # 轮1 等待循环/退出
            BASE + timedelta(minutes=59),                # 轮2 计算 -> 顺延明天
            BASE + timedelta(days=1),                    # 轮2 顺延判断
            BASE + timedelta(days=1, minutes=58),        # 轮2 等待退出
        ]
        executed = []

        def job():
            executed.append(1)
            if len(executed) == 1:
                raise RuntimeError("first-round-fail")
            raise KeyboardInterrupt

        with mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(scheduler, "datetime", make_fake_dt(times)):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.run_daily_at("23:58", job)
        self.assertEqual(len(executed), 2)

    def test_two_rounds_two_different_days(self):
        """连续两轮的目标日期必须不同（每日至多一次的不变量）。"""
        times = [
            datetime(2026, 8, 31, 8, 0),                 # 轮1：目标今天 07:10 已过->顺延
            datetime(2026, 9, 1, 7, 10),                 # 轮1 等待退出
            datetime(2026, 9, 1, 7, 11),                 # 轮2：今天已执行->顺延
            datetime(2026, 9, 2, 7, 10),                 # 轮2 等待退出
        ]
        executed = []

        def job():
            executed.append(1)
            if len(executed) == 2:
                raise KeyboardInterrupt

        with mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(scheduler, "datetime", make_fake_dt(times)):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.run_daily_at("07:10", job)
        self.assertEqual(len(executed), 2)

    def test_past_target_postponed_not_loop(self):
        """启动时触发时刻已过 -> 顺延执行一次，不无限连跑（实测缺陷回归）。"""
        times = [
            datetime(2026, 8, 31, 8, 0),    # 目标 00:00 已过 -> 顺延明天
            datetime(2026, 9, 1, 0, 0),     # 等待退出
        ]
        executed = []

        def job():
            executed.append(1)
            raise KeyboardInterrupt

        with mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(scheduler, "datetime", make_fake_dt(times)):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.run_daily_at("00:00", job)
        self.assertEqual(len(executed), 1)

    def test_on_tick_called(self):
        # wait 降到 <=60 秒时触发预热钩子（正常路径）
        times = [BASE, BASE, BASE + timedelta(minutes=57), BASE + timedelta(minutes=58)]
        ticks = []

        def job():
            raise KeyboardInterrupt

        with mock.patch("time.sleep", lambda s: None), \
                mock.patch.object(scheduler, "datetime", make_fake_dt(times)):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.run_daily_at("23:58", job, on_tick=lambda: ticks.append(1))
        self.assertGreaterEqual(len(ticks), 1)

    def test_invalid_hhmm_raises(self):
        with self.assertRaises(ValueError):
            scheduler.run_daily_at("abc", lambda: None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
