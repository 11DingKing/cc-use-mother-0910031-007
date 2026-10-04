"""可注入时钟。

领域服务不直接读取系统时间，而是依赖 ``Clock`` 抽象：

* 生产环境使用 :class:`SystemClock`（UTC）；
* 测试与到期作业的重放使用 :class:`FixedClock`；
* 调度器在调用到期作业时可以显式注入任意时刻。

这样"定时到期"的行为完全可测、可重放，且与机器时钟解耦。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前时刻（带时区，建议 UTC）。"""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定时钟：测试和到期作业重放使用，可手动推进。"""

    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = parse_ts(moment)
        if moment.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的时间")
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> datetime:
        """按 timedelta 支持的关键字（days/seconds/...）推进时钟。"""
        self.moment += timedelta(**kwargs)
        return self.moment


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 时间字符串；无时区后缀按 UTC 处理。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_ts(dt: datetime) -> str:
    """统一输出带毫秒的 UTC ISO-8601（Z 结尾）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
