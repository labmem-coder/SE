"""ORM 模型 —— 对应 hw1_report_v3.md 第一章中的领域模型。

简化说明：
- 充电站/等候区/充电区为逻辑容器，不单独建表（用配置项 + 视图查询体现）。
- 等待队列也是逻辑视图，由 ChargingRequest.priority_time 排序得到。
- 分时电价规则 / 服务费规则保存在 config.py（值很少且无需持久化）。
- 快充桩/慢充桩用 ChargingPile.mode 鉴别，避免无谓的继承表。
- 日报表为按需聚合查询，不单独存表。
- 管理员 = User.is_admin。
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


# ────────────────────────────────────────────────────────────────────────────
# 枚举
# ────────────────────────────────────────────────────────────────────────────


class ChargeMode(str, enum.Enum):
    FAST = "fast"
    SLOW = "slow"


class PileStatus(str, enum.Enum):
    AVAILABLE = "available"   # 空闲
    OCCUPIED = "occupied"     # 正在充电或有排队
    FAULT = "fault"           # 故障中


class RequestStatus(str, enum.Enum):
    WAITING = "waiting"                # 在等待队列中（未调度）
    FAULT_QUEUED = "fault_queued"      # 因桩故障被腾出的车，享最高调度优先级；不计入等候区 N
    DISPATCHED = "dispatched"          # 已分桩，等待用户响应叫号（5 分钟窗口）
    QUEUING_PILE = "queuing_pile"      # 用户已确认入场，在某桩的排队队列里
    CHARGING = "charging"              # 正在充电
    COMPLETED = "completed"            # 充电完成
    CANCELLED = "cancelled"            # 用户主动取消 或 5 分钟超时
    FAULT_INTERRUPTED = "fault_interrupted"  # 充电过程中桩故障终止


class SessionStatus(str, enum.Enum):
    CHARGING = "charging"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"        # 因桩故障中断


class BillStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"


# ────────────────────────────────────────────────────────────────────────────
# 用户与车辆
# ────────────────────────────────────────────────────────────────────────────


class User(Base):
    """充电用户 / 管理员（is_admin=True 即为管理员）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(50), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    vehicles: Mapped[list["Vehicle"]] = relationship(back_populates="owner")
    requests: Mapped[list["ChargingRequest"]] = relationship(back_populates="user")
    bills: Mapped[list["Bill"]] = relationship(back_populates="user")


class Vehicle(Base):
    """充电用户的车辆。同一时刻一辆车仅能有一条进行中的请求（业务约束，靠服务层判断）。"""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    license_plate: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # 车辆电池总容量（度）—— 管理员"查看等候服务车辆信息" spec 字段
    battery_capacity_kwh: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)

    owner: Mapped[User] = relationship(back_populates="vehicles")
    requests: Mapped[list["ChargingRequest"]] = relationship(back_populates="vehicle")


# ────────────────────────────────────────────────────────────────────────────
# 充电桩
# ────────────────────────────────────────────────────────────────────────────


class ChargingPile(Base):
    """充电桩 —— 快充/慢充通过 mode 鉴别。"""

    __tablename__ = "charging_piles"

    id: Mapped[int] = mapped_column(primary_key=True)
    pile_code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # e.g. F1, T1
    mode: Mapped[ChargeMode] = mapped_column(SAEnum(ChargeMode), nullable=False)
    power_kw: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[PileStatus] = mapped_column(
        SAEnum(PileStatus), default=PileStatus.AVAILABLE, nullable=False
    )
    queue_capacity: Mapped[int] = mapped_column(Integer, default=4, nullable=False)

    # 累计运营统计（运营报表用，避免每次扫所有 session）
    total_sessions: Mapped[int] = mapped_column(Integer, default=0)
    total_charged_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    total_revenue: Mapped[float] = mapped_column(Float, default=0.0)

    requests: Mapped[list["ChargingRequest"]] = relationship(back_populates="assigned_pile")
    sessions: Mapped[list["ChargingSession"]] = relationship(back_populates="pile")
    fault_records: Mapped[list["FaultRecord"]] = relationship(back_populates="pile")
    abnormal_reports: Mapped[list["AbnormalReport"]] = relationship(back_populates="pile")


# ────────────────────────────────────────────────────────────────────────────
# 充电请求 / 充电会话 / 账单
# ────────────────────────────────────────────────────────────────────────────


class ChargingRequest(Base):
    """一次充电请求，从提交到结束全程跟踪。

    状态机：
        WAITING ──(调度)──> DISPATCHED ──(5min 内 ConfirmEntry)──> QUEUING_PILE
                                │                                    │
                            (超时)                              (桩空闲)
                                ▼                                    ▼
                            CANCELLED                            CHARGING ──> COMPLETED
                                                                     │
                                                              (桩故障)
                                                                     ▼
                                                            FAULT_INTERRUPTED
        任何排队/调度态都可被 CancelChargeRequest 转 CANCELLED。
    """

    __tablename__ = "charging_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), nullable=False)

    mode: Mapped[ChargeMode] = mapped_column(SAEnum(ChargeMode), nullable=False)
    target_amount_kwh: Mapped[float] = mapped_column(Float, nullable=False)

    status: Mapped[RequestStatus] = mapped_column(
        SAEnum(RequestStatus), default=RequestStatus.WAITING, nullable=False
    )

    # 等待队列排序键：默认 = submitted_at；故障重调度时 = 原请求的 submitted_at（保留原优先级）
    priority_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 用户可见的排队号（按提交全站递增），如 "F12"
    queue_number: Mapped[str] = mapped_column(String(10), nullable=False, default="")

    assigned_pile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("charging_piles.id"), nullable=True
    )
    # 进入桩排队的时间（用于桩内 FIFO 排序），= ConfirmEntry 时间
    pile_queue_arrived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 若由故障重调度产生，指向原请求
    rescheduled_from_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("charging_requests.id"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="requests")
    vehicle: Mapped[Vehicle] = relationship(back_populates="requests")
    assigned_pile: Mapped[Optional[ChargingPile]] = relationship(back_populates="requests")
    session: Mapped[Optional["ChargingSession"]] = relationship(
        back_populates="request", uselist=False
    )


class ChargingSession(Base):
    """充电会话 —— 在请求真正开始充电时创建。"""

    __tablename__ = "charging_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("charging_requests.id"), unique=True, nullable=False
    )
    pile_id: Mapped[int] = mapped_column(ForeignKey("charging_piles.id"), nullable=False)

    status: Mapped[SessionStatus] = mapped_column(
        SAEnum(SessionStatus), default=SessionStatus.CHARGING, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    target_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    charged_kwh: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_tick_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    power_kw: Mapped[float] = mapped_column(Float, nullable=False)

    request: Mapped[ChargingRequest] = relationship(back_populates="session")
    pile: Mapped[ChargingPile] = relationship(back_populates="sessions")
    bill: Mapped[Optional["Bill"]] = relationship(back_populates="session", uselist=False)


class Bill(Base):
    """账单 —— 会话结束（含故障中断）即生成。"""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("charging_sessions.id"), unique=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    charged_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    charging_fee: Mapped[float] = mapped_column(Float, nullable=False)   # 充电费（分时累加）
    service_fee: Mapped[float] = mapped_column(Float, nullable=False)    # 服务费
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)

    status: Mapped[BillStatus] = mapped_column(
        SAEnum(BillStatus), default=BillStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pay_channel: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    session: Mapped[ChargingSession] = relationship(back_populates="bill")
    user: Mapped[User] = relationship(back_populates="bills")


# ────────────────────────────────────────────────────────────────────────────
# 异常与故障
# ────────────────────────────────────────────────────────────────────────────


class AbnormalReport(Base):
    """用户上报的"疑似异常"。不直接改变桩状态，仅供管理员核查。"""

    __tablename__ = "abnormal_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    pile_id: Mapped[int] = mapped_column(ForeignKey("charging_piles.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    pile: Mapped[ChargingPile] = relationship(back_populates="abnormal_reports")


class FaultRecord(Base):
    """管理员确认后产生的正式故障记录。"""

    __tablename__ = "fault_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    pile_id: Mapped[int] = mapped_column(ForeignKey("charging_piles.id"), nullable=False)
    fault_type: Mapped[str] = mapped_column(String(100), nullable=False)
    fault_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_report_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("abnormal_reports.id"), nullable=True
    )

    pile: Mapped[ChargingPile] = relationship(back_populates="fault_records")
    source_report: Mapped[Optional[AbnormalReport]] = relationship()


__all__ = [
    "ChargeMode",
    "PileStatus",
    "RequestStatus",
    "SessionStatus",
    "BillStatus",
    "User",
    "Vehicle",
    "ChargingPile",
    "ChargingRequest",
    "ChargingSession",
    "Bill",
    "AbnormalReport",
    "FaultRecord",
]
