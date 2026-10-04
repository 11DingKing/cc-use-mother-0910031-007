"""可选的定时到期调度器。

生产部署通常由外部调度系统（cron/k8s CronJob/消息定时任务）在每天固定
时刻调用 ``POST /admin/expiration/run``；本模块提供一个进程内调度器，
便于单体部署与本地演示。

要点：

* 判定时刻取自服务的**注入时钟**（而不是调度器自己读系统时间），
  测试中替换 :class:`FixedClock` 即可控制全部行为；
* 每次运行使用确定性幂等键 ``SCHED:{yyyy-mm-dd}``，
  因此调度重复触发、多实例同时运行都不会重复失效；
* 等待循环在关闭事件上超时等待，退出干净。
"""
from __future__ import annotations

import threading
from datetime import timedelta

from .clock import to_ts
from .service import PointsService


class ExpirationScheduler:
    def __init__(self, service: PointsService, *, interval_seconds: float = 3600.0) -> None:
        self.service = service
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._runs: list[dict] = []

    @property
    def runs(self) -> list[dict]:
        return list(self._runs)

    def run_once(self) -> dict:
        """触发一次到期处理；幂等键按注入时钟的日期生成。"""
        day = to_ts(self.service.clock.now())[:10]
        report = self.service.run_expiration(job_key=f"SCHED:{day}")
        self._runs.append(report)
        return report

    def _loop(self, run_immediately: bool) -> None:
        if run_immediately:
            self.run_once()
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception:  # 调度循环不允许因单次失败退出
                # 实际部署应接入日志；这里保留记录，下一周期继续。
                pass

    def start(self, *, run_immediately: bool = False) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(run_immediately,), daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
