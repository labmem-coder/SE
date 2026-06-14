"""分时电价 & 账单生成。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import (
    BILL_CODE_PREFIX,
    PRICING_SCHEDULE,
    SERVICE_FEE_YUAN_PER_KWH,
)
from .models import Bill, BillStatus, ChargingSession


def _hour_fraction(dt: datetime) -> float:
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0 + dt.microsecond / 3_600_000_000.0


def rate_for(dt: datetime) -> float:
    """返回某一时刻对应的电价 (yuan/kWh)。"""
    h = _hour_fraction(dt)
    for start, end, rate in PRICING_SCHEDULE:
        if start <= h < end:
            return rate
    return PRICING_SCHEDULE[-1][2]


def _next_boundary_hour(hour_frac: float) -> float:
    for start, end, _ in PRICING_SCHEDULE:
        if start <= hour_frac < end:
            return end
    return 24.0


def calculate_charging_fee(start_dt: datetime, charged_kwh: float, power_kw: float) -> float:
    """按分时电价计算 charged_kwh 的电费。

    假设功率恒定 power_kw、从 start_dt 起连续充电，将充电时段切分到
    PRICING_SCHEDULE 的各档位中分别计费。
    """
    if charged_kwh <= 0 or power_kw <= 0:
        return 0.0

    duration_seconds = charged_kwh / power_kw * 3600.0
    end_dt = start_dt + timedelta(seconds=duration_seconds)

    fee = 0.0
    cur = start_dt
    # 防御性循环上限：分时档位最多 7 个 × 多日，按日数 + 8 即可
    guard_days = max(1, int((end_dt - start_dt).total_seconds() // 86400) + 2)
    max_iters = (len(PRICING_SCHEDULE) + 1) * guard_days + 8
    iters = 0

    while cur < end_dt and iters < max_iters:
        iters += 1
        h = _hour_fraction(cur)
        rate = rate_for(cur)
        boundary_h = _next_boundary_hour(h)
        day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        boundary_dt = day_start + timedelta(hours=boundary_h)
        if boundary_dt <= cur:
            boundary_dt = boundary_dt + timedelta(days=1)
        seg_end = min(boundary_dt, end_dt)
        seg_hours = (seg_end - cur).total_seconds() / 3600.0
        fee += seg_hours * power_kw * rate
        cur = seg_end

    return round(fee, 4)


def calculate_service_fee(charged_kwh: float) -> float:
    return round(max(charged_kwh, 0.0) * SERVICE_FEE_YUAN_PER_KWH, 4)


def estimate_charging_fee(start_dt: datetime, target_kwh: float, power_kw: float) -> float:
    """用于排队视图：粗略估算用户应付电费（按目标电量、当前时刻）。"""
    return calculate_charging_fee(start_dt, target_kwh, power_kw)


def _make_bill_code() -> str:
    return f"{BILL_CODE_PREFIX}{datetime.now().strftime('%Y%m%d')}{uuid.uuid4().hex[:6].upper()}"


def generate_bill(db: Session, session: ChargingSession) -> Bill:
    """根据 ChargingSession 的实际充电数据生成账单（已扣款部分计费）。

    可同时用于：正常完成（status=COMPLETED）和故障中断（status=INTERRUPTED）。
    """
    charging_fee = calculate_charging_fee(
        session.started_at, session.charged_kwh, session.power_kw
    )
    service_fee = calculate_service_fee(session.charged_kwh)
    total = round(charging_fee + service_fee, 2)

    bill = Bill(
        bill_code=_make_bill_code(),
        session_id=session.id,
        user_id=session.request.user_id,
        charged_kwh=round(session.charged_kwh, 4),
        charging_fee=round(charging_fee, 2),
        service_fee=round(service_fee, 2),
        total_amount=total,
        status=BillStatus.PENDING,
        created_at=datetime.now(),
    )
    db.add(bill)
    db.flush()  # 拿到 id
    return bill
