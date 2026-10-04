"""可注入时钟：服务内所有时间读取（含定时到期）都经过 Clock。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def ensure_aware(value: datetime) -> datetime:
    """把无时区时间按 UTC 处理，保证全链路时间可比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Clock(Protocol):
    """时钟协议：返回当前时间（UTC）。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class MutableClock:
    """测试与演示时钟：可手动推进，用于验证到期与幂等行为。"""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_aware(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = ensure_aware(value)

    def advance(self, **kwargs: float) -> datetime:
        """按 timedelta 参数推进，例如 advance(days=30)。"""
        self._now = self._now + timedelta(**kwargs)
        return self._now
