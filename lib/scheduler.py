"""内置日循环调度：--at HH:MM，进程内 sleep 到点执行，异常不退出循环。

按用户要求不注册系统计划任务：开机后手动启动
  python main.py run --at 07:10
挂着即可；Ctrl+C 停止。

补跑语义（实测修正版）：
- 启动时今日触发时刻已过 -> 顺延明天（不误触发）
- sleep 被系统休眠拉长、唤醒时已越过本次计划时刻 -> 立即补跑一次（幂等检查保证无害）
- 用 last_run_date 强制每日至多一次，杜绝补跑判断反复成立造成无限连跑
  （2026-08-31 实测发现旧实现的无限补跑缺陷，已修正）
"""
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


def run_daily_at(hhmm: str, job, on_tick=None, pre_warm_sec: int = 60) -> None:
    """Run ``job`` daily, optionally warming authentication before the target."""
    h, m = (int(x) for x in hhmm.split(":"))
    last_run_date = None  # 已执行过的目标日期，保证每日至多一次

    while True:
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # 找下一个尚未执行过的触发时刻（启动时今日已过则自然顺延明天）
        if target <= now or (last_run_date and target.date() <= last_run_date):
            today_target = target
            while target <= now or (last_run_date and target.date() <= last_run_date):
                target += timedelta(days=1)
            if target.date() > today_target.date():
                log.info("今日触发时刻 %02d:%02d 已过/已执行，顺延至 %s",
                         h, m, target.strftime("%Y-%m-%d %H:%M"))

        slept = False
        warmed = False
        while True:
            now = datetime.now()
            if now >= target:
                break
            wait = (target - now).total_seconds()
            if not slept:
                log.info("下次执行: %s（等待 %.0f 秒）", target, wait)
                slept = True
            if pre_warm_sec > 0 and not warmed and wait <= pre_warm_sec:
                warmed = True
                if on_tick:
                    on_tick()
                continue
            time.sleep(min(wait, 3600))  # 分段睡，休眠唤醒后可校准

        if pre_warm_sec > 0 and on_tick and not warmed:
            on_tick()  # 休眠唤醒错过预热点时，执行前兜底预热
        started = time.time()
        try:
            job()
        except Exception:
            log.exception("本轮执行失败（不退出，等下一触发时刻）")
        else:
            log.info("本轮结束，耗时 %.1f 秒", time.time() - started)
        if time.time() - started > 90:
            # 本轮整体耗时（含 sleep 被休眠拉长）越过计划时刻 -> 属补跑执行，无害（幂等）
            log.warning("本轮越过计划时刻 %s 执行（休眠/延迟补跑）",
                        target.strftime("%H:%M"))
        last_run_date = target.date()
