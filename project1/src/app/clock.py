"""虚拟时钟 —— 替代 datetime.now() 作为全系统统一时间源。

时钟从当天 06:00 启动，默认流速 0（暂停），通过 +5min / 流速 / 复位 按钮推进。
所有业务时间（充电进度、会话起止、账单时间等）都以此钟为准。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

# 默认初始虚拟时刻：当天 06:00:00
INITIAL_HOUR = 6
INITIAL_MINUTE = 0
INITIAL_SECOND = 0
DEFAULT_SPEED = 0.0


class VirtualClock:
    """线程安全的虚拟时钟单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._base_real: datetime = datetime.now()
        self._base_virtual: datetime = self._initial_virtual()
        self._speed: float = DEFAULT_SPEED

    @staticmethod
    def _initial_virtual() -> datetime:
        today = datetime.now().date()
        return datetime(
            today.year, today.month, today.day,
            INITIAL_HOUR, INITIAL_MINUTE, INITIAL_SECOND,
        )

    # ── public API ──────────────────────────────────────────────────────────

    def get_time(self) -> datetime:
        """返回当前虚拟系统时间。"""
        with self._lock:
            return self._compute_unlocked()

    def get_speed(self) -> float:
        """返回当前流速倍率。"""
        return self._speed

    def set_speed(self, speed: float) -> None:
        """修改流速倍率（1 / 10 / 20 / 30 等）。

        更换倍率前先冻结当前虚拟时刻，避免跳变。
        """
        with self._lock:
            current = self._compute_unlocked()
            self._base_virtual = current
            self._base_real = datetime.now()
            self._speed = speed

    def advance(self, minutes: float) -> None:
        """将虚拟时间向前跳跃指定分钟数。"""
        with self._lock:
            current = self._compute_unlocked()
            self._base_virtual = current + timedelta(minutes=minutes)
            self._base_real = datetime.now()

    def reset(self) -> None:
        """复位为初始时间 06:00 + 流速 0。"""
        with self._lock:
            self._base_virtual = self._initial_virtual()
            self._base_real = datetime.now()
            self._speed = DEFAULT_SPEED

    # ── internal ────────────────────────────────────────────────────────────

    def _compute_unlocked(self) -> datetime:
        real_elapsed_seconds = (datetime.now() - self._base_real).total_seconds()
        virtual_seconds = real_elapsed_seconds * self._speed
        return self._base_virtual + timedelta(seconds=virtual_seconds)


# 模块级单例
_clock = VirtualClock()


def get_time() -> datetime:
    """快捷函数 —— 替代 datetime.now()。"""
    return _clock.get_time()


def get_speed() -> float:
    return _clock.get_speed()


def set_speed(speed: float) -> None:
    _clock.set_speed(speed)


def advance(minutes: float) -> None:
    _clock.advance(minutes)


def reset() -> None:
    _clock.reset()
