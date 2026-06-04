"""后台周期 tick：推进充电会话、检查超时、触发调度。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import BACKGROUND_TICK_SECONDS
from .db import session_scope
from .scheduler import try_dispatch

log = logging.getLogger("ticker")


def _tick() -> None:
    try:
        with session_scope() as db:
            n = try_dispatch(db)
            if n > 0:
                log.info("dispatched %d request(s) this tick", n)
    except Exception:
        log.exception("background tick failed")


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _tick,
        "interval",
        seconds=BACKGROUND_TICK_SECONDS,
        id="ev_station_tick",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
