"""可注入时钟。

领域逻辑全部通过时钟取当前时间，测试可换固定时钟，生产用系统时钟。
时间一律使用时区感知的 ISO-8601 字符串（样例沿用 ``+08:00``）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前时区感知时间。"""


class SystemClock:
    """生产时钟：UTC 时间。落库时转为 ISO-8601。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试时钟：永远返回构造时给定的时间。"""

    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        if moment.tzinfo is None:
            raise ValueError("FixedClock 需要时区感知时间")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, delta) -> None:
        from datetime import timedelta

        if isinstance(delta, (int, float)):
            delta = timedelta(seconds=delta)
        self._moment = self._moment + delta
