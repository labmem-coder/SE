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


class UserOut(ORMBase):
    id: int
    username: str
    display_name: str
    is_admin: bool
    vehicles: list[VehicleOut] = []


# ────────────────── UC_01 SubmitChargeRequest ──────────────────


class SubmitChargeRequestIn(BaseModel):
    vehicleId: int = Field(..., description="车辆 ID")
    mode: ChargeMode
    targetAmount: float = Field(..., gt=0, description="目标充电量 (kWh)")
    entryToken: str = Field(..., min_length=1, description="等候区入场凭证")


class QueueInfo(BaseModel):
    """提交 / 查询时返回的排队视图。"""

    requestId: int
    requestCode: str
    status: RequestStatus
    mode: ChargeMode
    queueNumber: str
    waitingPosition: Optional[int] = None     # 在等待队列中的位置，1-indexed；已离开等待队列为 None
    estimatedWaitMinutes: Optional[float] = None
    assignedPileCode: Optional[str] = None
    pileQueuePosition: Optional[int] = None   # 在桩排队中的位置（1=正在充电）


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
    totalRevenue: float


class QueryPileStatusOut(BaseModel):
    piles: list[PileStatusEntry]
    waitingQueueFast: int
    waitingQueueSlow: int
    pendingAbnormalReports: int


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
