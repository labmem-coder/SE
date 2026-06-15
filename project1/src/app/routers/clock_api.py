"""虚拟时钟控制 API。

GET  /api/clock          — 获取当前虚拟时间与流速
POST /api/clock/speed    — 设置流速倍率（0 表示暂停）
POST /api/clock/advance  — 向前跳跃指定分钟
POST /api/clock/reset    — 复位为 06:00 + 流速 0
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import clock

router = APIRouter(prefix="/api/clock", tags=["clock"])


class ClockState(BaseModel):
    virtual_time: str
    speed: float


class SetSpeedIn(BaseModel):
    speed: float = Field(..., ge=0, le=30, description="流速倍率，0=暂停")


class AdvanceIn(BaseModel):
    minutes: float = Field(..., gt=0, description="向前跳跃的分钟数")


def _state() -> ClockState:
    return ClockState(
        virtual_time=clock.get_time().isoformat(),
        speed=clock.get_speed(),
    )


@router.get("", response_model=ClockState)
def get_clock_state() -> ClockState:
    return _state()


@router.post("/speed", response_model=ClockState)
def set_clock_speed(payload: SetSpeedIn) -> ClockState:
    clock.set_speed(payload.speed)
    return _state()


@router.post("/advance", response_model=ClockState)
def advance_clock(payload: AdvanceIn) -> ClockState:
    clock.advance(payload.minutes)
    return _state()


@router.post("/reset", response_model=ClockState)
def reset_clock() -> ClockState:
    clock.reset()
    return _state()
