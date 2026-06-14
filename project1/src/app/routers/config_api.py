"""管理员配置 API —— 故障调度策略 / 扩展调度策略 / 充电桩配置。

GET  /api/admin/config   →  SystemConfigOut
PUT  /api/admin/config   →  SystemConfigOut
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import admin_required
from ..db import SessionLocal, engine, get_db
from ..models import (
    ChargeMode,
    ChargingPile,
    ChargingRequest,
    ChargingSession,
    RequestStatus,
    SessionStatus,
    User,
    Vehicle,
)
from ..schemas import SystemConfigOut, SystemConfigUpdateIn

router = APIRouter(prefix="/api/admin", tags=["admin-config"])


def _count_active_sessions(db: Session) -> int:
    return (
        db.query(ChargingSession)
        .filter(ChargingSession.status == SessionStatus.CHARGING)
        .count()
    )


def _count_active_requests(db: Session) -> int:
    """任一桩有占位请求（DISPATCHED / QUEUING_PILE / CHARGING）即为'工作中'。"""
    return (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.status.in_(
                (RequestStatus.DISPATCHED, RequestStatus.QUEUING_PILE, RequestStatus.CHARGING)
            )
        )
        .count()
    )


def _update_module_configs(
    fault_policy: Optional[str] = None,
    extended_policy: Optional[str] = None,
    fast_count: Optional[int] = None,
    slow_count: Optional[int] = None,
    fast_power: Optional[float] = None,
    slow_power: Optional[float] = None,
    queue_cap: Optional[int] = None,
    waiting_size: Optional[int] = None,
) -> None:
    """更新所有模块中 import 的配置变量（Python 模块级变量需跨模块同步）。"""
    import app.config as cfg
    import app.scheduler as sched
    import app.fault as flt

    if fault_policy is not None:
        cfg.FAULT_DISPATCH_POLICY = fault_policy
        flt.FAULT_DISPATCH_POLICY = fault_policy
    if extended_policy is not None:
        cfg.EXTENDED_SCHEDULE_POLICY = extended_policy
        sched.EXTENDED_SCHEDULE_POLICY = extended_policy
    if fast_count is not None:
        cfg.FAST_PILE_COUNT = fast_count
    if slow_count is not None:
        cfg.SLOW_PILE_COUNT = slow_count
    if fast_power is not None:
        cfg.FAST_PILE_POWER_KW = fast_power
    if slow_power is not None:
        cfg.SLOW_PILE_POWER_KW = slow_power
    if queue_cap is not None:
        cfg.PILE_QUEUE_CAPACITY = queue_cap
        sched.PILE_QUEUE_CAPACITY = queue_cap
    if waiting_size is not None:
        cfg.WAITING_AREA_SIZE = waiting_size
        sched.WAITING_AREA_SIZE = waiting_size


def _full_reset(new_fast_count: int, new_slow_count: int, new_fast_power: float, new_slow_power: float, new_queue_cap: int) -> None:
    """重置数据库：删表 → 重建 → 按新配置播种。

    保留用户/车辆数据（从旧库备份并恢复），只重置充电桩与请求/会话/账单。
    不删除 DB 文件（因为后台 tick 线程可能持有连接），而是 drop_all + create_all。
    """
    from ..db import Base

    # 1) 备份用户与车辆
    old_users: list[dict] = []
    old_vehicles: list[dict] = []
    try:
        old_db = SessionLocal()
        for u in old_db.query(User).all():
            old_users.append({
                "username": u.username,
                "password_hash": u.password_hash,
                "display_name": u.display_name,
                "is_admin": u.is_admin,
            })
        for v in old_db.query(Vehicle).all():
            old_vehicles.append({
                "license_plate": v.license_plate,
                "owner_username": v.owner.username if v.owner else None,
                "battery_capacity_kwh": v.battery_capacity_kwh,
            })
        old_db.close()
    except Exception:
        pass

    # 2) 关闭引擎连接池，然后 drop + create 表
    engine.dispose()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    # 3) 更新模块变量（在重建之后，确保后续 seed 等逻辑读到新值）
    _update_module_configs(
        fast_count=new_fast_count,
        slow_count=new_slow_count,
        fast_power=new_fast_power,
        slow_power=new_slow_power,
        queue_cap=new_queue_cap,
    )

    # 4) 重新播种
    with SessionLocal() as db:
        # 恢复用户
        user_map: dict[str, User] = {}
        for ud in old_users:
            u = User(
                username=ud["username"],
                password_hash=ud["password_hash"],
                display_name=ud["display_name"],
                is_admin=ud["is_admin"],
            )
            db.add(u)
            db.flush()
            user_map[ud["username"]] = u

        # 恢复车辆
        for vd in old_vehicles:
            owner = user_map.get(vd["owner_username"]) if vd["owner_username"] else None
            if owner:
                v = Vehicle(
                    license_plate=vd["license_plate"],
                    owner_id=owner.id,
                    battery_capacity_kwh=vd["battery_capacity_kwh"],
                )
                db.add(v)

        # 按新配置创建充电桩
        for i in range(1, new_fast_count + 1):
            db.add(ChargingPile(
                pile_code=f"F{i}",
                mode=ChargeMode.FAST,
                power_kw=new_fast_power,
                queue_capacity=new_queue_cap,
            ))
        for i in range(1, new_slow_count + 1):
            db.add(ChargingPile(
                pile_code=f"T{i}",
                mode=ChargeMode.SLOW,
                power_kw=new_slow_power,
                queue_capacity=new_queue_cap,
            ))
        db.commit()


@router.get("/config", response_model=SystemConfigOut)
def get_config(
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
) -> SystemConfigOut:
    # 从 config 模块动态读取（因为 update_config 可能在运行时修改了这些值）
    import app.config as cfg
    has_active = _count_active_requests(db) > 0
    return SystemConfigOut(
        faultDispatchPolicy=cfg.FAULT_DISPATCH_POLICY,
        extendedSchedulePolicy=cfg.EXTENDED_SCHEDULE_POLICY,
        fastPileCount=cfg.FAST_PILE_COUNT,
        slowPileCount=cfg.SLOW_PILE_COUNT,
        fastPilePowerKw=cfg.FAST_PILE_POWER_KW,
        slowPilePowerKw=cfg.SLOW_PILE_POWER_KW,
        pileQueueCapacity=cfg.PILE_QUEUE_CAPACITY,
        waitingAreaSize=cfg.WAITING_AREA_SIZE,
        hasActiveSessions=has_active,
    )


@router.put("/config", response_model=SystemConfigOut)
def update_config(
    payload: SystemConfigUpdateIn,
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
) -> SystemConfigOut:
    # 0) 若任何桩在工作（有占位请求），禁止修改
    if _count_active_requests(db) > 0:
        raise HTTPException(status_code=409, detail="有充电桩正在工作中，无法修改配置。请等待所有充电完成后重试。")

    import app.config as cfg

    # 解析新值（未提供则沿用旧值）
    new_fault = payload.faultDispatchPolicy or cfg.FAULT_DISPATCH_POLICY
    new_extended = payload.extendedSchedulePolicy or cfg.EXTENDED_SCHEDULE_POLICY
    new_fast_count = payload.fastPileCount if payload.fastPileCount is not None else cfg.FAST_PILE_COUNT
    new_slow_count = payload.slowPileCount if payload.slowPileCount is not None else cfg.SLOW_PILE_COUNT
    new_fast_power = payload.fastPilePowerKw if payload.fastPilePowerKw is not None else cfg.FAST_PILE_POWER_KW
    new_slow_power = payload.slowPilePowerKw if payload.slowPilePowerKw is not None else cfg.SLOW_PILE_POWER_KW
    new_queue_cap = payload.pileQueueCapacity if payload.pileQueueCapacity is not None else cfg.PILE_QUEUE_CAPACITY
    new_waiting = payload.waitingAreaSize if payload.waitingAreaSize is not None else cfg.WAITING_AREA_SIZE

    # 验证策略值
    if new_fault not in ("priority", "time_order"):
        raise HTTPException(status_code=400, detail="faultDispatchPolicy 必须为 priority 或 time_order")
    if new_extended not in ("normal", "multi_short", "batch_short"):
        raise HTTPException(status_code=400, detail="extendedSchedulePolicy 必须为 normal / multi_short / batch_short")

    pile_count_changed = (
        new_fast_count != cfg.FAST_PILE_COUNT
        or new_slow_count != cfg.SLOW_PILE_COUNT
    )

    if pile_count_changed:
        # 充电桩数量变更 → 完整重置数据库
        _full_reset(new_fast_count, new_slow_count, new_fast_power, new_slow_power, new_queue_cap)
        # 同时更新非桩配置
        _update_module_configs(
            fault_policy=new_fault,
            extended_policy=new_extended,
            waiting_size=new_waiting,
        )
    else:
        # 数量未变 → 仅更新模块变量 + 更新现有桩的属性
        _update_module_configs(
            fault_policy=new_fault,
            extended_policy=new_extended,
            fast_power=new_fast_power,
            slow_power=new_slow_power,
            queue_cap=new_queue_cap,
            waiting_size=new_waiting,
        )
        # 更新已有桩的功率与队列容量
        for pile in db.query(ChargingPile).all():
            if pile.mode == ChargeMode.FAST:
                pile.power_kw = new_fast_power
            else:
                pile.power_kw = new_slow_power
            pile.queue_capacity = new_queue_cap
        db.commit()

    return SystemConfigOut(
        faultDispatchPolicy=new_fault,
        extendedSchedulePolicy=new_extended,
        fastPileCount=new_fast_count,
        slowPileCount=new_slow_count,
        fastPilePowerKw=new_fast_power,
        slowPilePowerKw=new_slow_power,
        pileQueueCapacity=new_queue_cap,
        waitingAreaSize=new_waiting,
        hasActiveSessions=False,
    )
