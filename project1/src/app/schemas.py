"""Pydantic 请求 / 响应 schema —— 命名对齐 hw1_report_v3.md 的操作契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    BillStatus,
    ChargeMode,
    PileStatus,
    RequestStatus,
    SessionStatus,
)


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ────────────────── 认证 ──────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user_id: int
    username: str
    is_admin: bool


# ────────────────── 用户车辆 ──────────────────


class VehicleOut(ORMBase):
    id: int
    license_plate: str
    battery_capacity_kwh: float = 60.0


class VehicleCreate(BaseModel):
    license_plate: str = Field(..., min_length=1, max_length=20, description="车牌号")
    battery_capacity_kwh: float = Field(default=60.0, gt=0, description="电池总容量 (kWh)")


class UserOut(ORMBase):
    id: int
    username: str
    display_name: str
    is_admin: bool
    vehicles: list[VehicleOut] = []


# ────────────────── UC_01 SubmitChargeRequest ──────────────────


class SubmitChargeRequestIn(BaseModel):
    vehicleId: Optional[int] = Field(default=None, description="已有车辆 ID")
    licensePlate: Optional[str] = Field(default=None, min_length=1, max_length=20, description="新车牌号（新建车辆时使用）")
    batteryCapacity: Optional[float] = Field(default=60.0, gt=0, description="新车电池容量（新建车辆时使用）")
    mode: ChargeMode
    targetAmount: float = Field(..., gt=0, description="目标充电量 (kWh)")
    entryToken: str = Field(..., min_length=1, description="等候区入场凭证")


class QueueInfo(BaseModel):
    """提交 / 查询时返回的排队视图。"""

    requestId: int
    requestCode: str
    status: RequestStatus
    mode: ChargeMode
    targetAmount: float = 0.0                # 请求充电量 (kWh)
    queueNumber: str
    vehicleId: Optional[int] = None
    licensePlate: Optional[str] = None
    waitingPosition: Optional[int] = None     # 在等待队列中的位置，1-indexed；已离开等待队列为 None
    estimatedWaitMinutes: Optional[float] = None
    assignedPileCode: Optional[str] = None
    pileQueuePosition: Optional[int] = None   # 在桩排队中的位置（1=正在充电）
    dispatchedAt: Optional[datetime] = None   # 派遣时刻 —— 客户端按此排序保证 FIFO confirm


class SubmitChargeRequestOut(BaseModel):
    accepted: bool
    message: str
    queueInfo: Optional[QueueInfo] = None


# ────────────────── UC_02 UpdateChargeRequest ──────────────────


class UpdateChargeRequestIn(BaseModel):
    newMode: Optional[ChargeMode] = None
    newTargetAmount: Optional[float] = Field(default=None, gt=0)


class UpdateChargeRequestOut(BaseModel):
    accepted: bool
    message: str
    queueInfo: Optional[QueueInfo] = None
    modeChanged: bool = False


# ────────────────── UC_03 CancelChargeRequest ──────────────────


class CancelChargeRequestOut(BaseModel):
    accepted: bool
    message: str


# ────────────────── UC_05 ConfirmEntry ──────────────────


class ConfirmEntryOut(BaseModel):
    accepted: bool
    message: str
    queueInfo: Optional[QueueInfo] = None


# ────────────────── UC_06 ReportDeviceAbnormal ──────────────────


class ReportDeviceAbnormalIn(BaseModel):
    pileId: int
    description: str = Field(..., min_length=1, max_length=500)


class ReportDeviceAbnormalOut(BaseModel):
    accepted: bool
    reportId: int
    message: str


# ────────────────── UC_07 QueryBill / ConfirmPayment ──────────────────


class BillOut(ORMBase):
    """详单 —— spec §5.2 用户客户端"充电详单"必需字段。"""

    id: int
    bill_code: str
    session_id: int
    charged_kwh: float
    charging_fee: float
    service_fee: float
    total_amount: float
    status: BillStatus
    created_at: datetime
    paid_at: Optional[datetime] = None
    pay_channel: Optional[str] = None
    # spec 必需字段：充电桩编号、启动时间、停止时间、充电时长
    pile_code: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    charging_duration_hours: Optional[float] = None


class ConfirmPaymentIn(BaseModel):
    payChannel: str = Field(..., min_length=1, max_length=50)


class ConfirmPaymentOut(BaseModel):
    accepted: bool
    message: str
    bill: Optional[BillOut] = None


# ────────────────── UC_08 QueryPileStatus ──────────────────


class PileStatusEntry(BaseModel):
    pileId: int
    pileCode: str
    mode: ChargeMode
    powerKw: float
    status: PileStatus
    chargingRequestCode: Optional[str] = None     # 正在充电的请求编号
    chargingLicensePlate: Optional[str] = None    # 正在充电汽车的车牌号
    chargingProgressKwh: Optional[float] = None
    chargingTargetKwh: Optional[float] = None
    queueLength: int = 0                           # 含正在充电
    queueCapacity: int
    totalSessions: int
    totalChargedKwh: float
    totalChargingHours: float = 0.0               # 系统启动以来累计充电时长(小时) —— spec §5.3
    totalRevenue: float


class QueryPileStatusOut(BaseModel):
    piles: list[PileStatusEntry]
    waitingQueueFast: int
    waitingQueueSlow: int
    pendingAbnormalReports: int


class PileQueuedVehicle(BaseModel):
    """管理员"查看各充电桩等候服务的车辆信息" spec 要求字段。"""

    userId: int
    licensePlate: str
    batteryCapacityKwh: float           # 车辆电池总容量(度)
    requestedAmountKwh: float           # 请求充电量(度)
    queueDurationMinutes: float         # 排队时长（提交至今的分钟数）
    status: RequestStatus
    queueNumber: str


class PileQueueDetailOut(BaseModel):
    pileId: int
    pileCode: str
    vehicles: list[PileQueuedVehicle]


# ────────────────── UC_09 ConfirmPileFault / UC_10 ResumePile ──────────────────


class ConfirmPileFaultIn(BaseModel):
    faultType: str = Field(..., min_length=1, max_length=100)
    faultTime: Optional[datetime] = None
    sourceReportId: Optional[int] = None


class ConfirmPileFaultOut(BaseModel):
    accepted: bool
    message: str
    faultRecordId: int
    interruptedSessionId: Optional[int] = None
    rescheduledRequests: int = 0


class ResumePileOut(BaseModel):
    accepted: bool
    message: str


# ────────────────── UC_11 QueryOperationReport ──────────────────


class OperationReportOut(BaseModel):
    fromDate: datetime
    toDate: datetime
    totalSessions: int
    totalChargedKwh: float
    totalChargingFee: float
    totalServiceFee: float
    totalRevenue: float
    paidBills: int
    pendingBills: int
    faultCount: int
    pilesBreakdown: list[dict]


# ────────────────── 系统配置 ──────────────────


class SystemConfigOut(BaseModel):
    """当前系统配置（管理员可见）。"""
    faultDispatchPolicy: str
    extendedSchedulePolicy: str
    manualDispatchMode: bool = False
    fastPileCount: int
    slowPileCount: int
    fastPilePowerKw: float
    slowPilePowerKw: float
    pileQueueCapacity: int
    waitingAreaSize: int
    hasActiveSessions: bool = False


class SystemConfigUpdateIn(BaseModel):
    """更新系统配置。"""
    faultDispatchPolicy: Optional[str] = Field(default=None, description="priority | time_order")
    extendedSchedulePolicy: Optional[str] = Field(default=None, description="normal | multi_short | batch_short")
    manualDispatchMode: Optional[bool] = None
    fastPileCount: Optional[int] = Field(default=None, ge=0, le=20)
    slowPileCount: Optional[int] = Field(default=None, ge=0, le=20)
    fastPilePowerKw: Optional[float] = Field(default=None, gt=0, le=500)
    slowPilePowerKw: Optional[float] = Field(default=None, gt=0, le=500)
    pileQueueCapacity: Optional[int] = Field(default=None, ge=1, le=20)
    waitingAreaSize: Optional[int] = Field(default=None, ge=1, le=200)
