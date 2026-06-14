"""管理员客户端 API —— 实现 UC_08 ~ UC_11。

UC_08 QueryPileStatus       GET  /api/admin/piles
UC_09 ConfirmPileFault      POST /api/admin/piles/{pile_id}/fault
UC_10 ResumePile            POST /api/admin/piles/{pile_id}/resume
UC_11 QueryOperationReport  GET  /api/admin/reports
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import admin_required
from ..db import get_db
from ..fault import confirm_pile_fault, resume_pile
from ..models import (
    AbnormalReport,
    Bill,
    BillStatus,
    ChargeMode,
    ChargingPile,
    ChargingRequest,
    ChargingSession,
    FaultRecord,
    PileStatus,
    RequestStatus,
    SessionStatus,
    User,
)
from ..scheduler import (
    PILE_SLOT_STATUSES,
    advance_active_sessions,
    try_dispatch,
)
from ..schemas import (
    ConfirmPileFaultIn,
    ConfirmPileFaultOut,
    OperationReportOut,
    PileQueueDetailOut,
    PileQueuedVehicle,
    PileStatusEntry,
    QueryPileStatusOut,
    ResumePileOut,
)


router = APIRouter(prefix="/api/admin", tags=["admin"])


# ────────────────────────────────────────────────────────────────────────────
# UC_08 QueryPileStatus
# ────────────────────────────────────────────────────────────────────────────


@router.get("/piles", response_model=QueryPileStatusOut)
def query_pile_status(
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
) -> QueryPileStatusOut:
    advance_active_sessions(db)
    try_dispatch(db)
    db.commit()

    entries: list[PileStatusEntry] = []
    for pile in db.query(ChargingPile).order_by(ChargingPile.id).all():
        slot_count = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.assigned_pile_id == pile.id,
                ChargingRequest.status.in_(PILE_SLOT_STATUSES),
            )
            .count()
        )
        active_session = (
            db.query(ChargingSession)
            .filter(
                ChargingSession.pile_id == pile.id,
                ChargingSession.status == SessionStatus.CHARGING,
            )
            .first()
        )
        active_request_code: Optional[str] = None
        active_license_plate: Optional[str] = None
        progress: Optional[float] = None
        target: Optional[float] = None
        if active_session is not None:
            active_request_code = active_session.request.request_code
            active_license_plate = active_session.request.vehicle.license_plate
            progress = round(active_session.charged_kwh, 3)
            target = active_session.target_kwh

        # 累计充电时长（小时）—— 已结束的会话精确 ended-started，进行中的按已充进度估算
        total_h = 0.0
        all_sessions = (
            db.query(ChargingSession)
            .filter(ChargingSession.pile_id == pile.id)
            .all()
        )
        for s in all_sessions:
            if s.ended_at and s.started_at:
                total_h += (s.ended_at - s.started_at).total_seconds() / 3600.0
            elif s.power_kw > 0:
                total_h += s.charged_kwh / s.power_kw

        entries.append(
            PileStatusEntry(
                pileId=pile.id,
                pileCode=pile.pile_code,
                mode=pile.mode,
                powerKw=pile.power_kw,
                status=pile.status,
                chargingRequestCode=active_request_code,
                chargingLicensePlate=active_license_plate,
                chargingProgressKwh=progress,
                chargingTargetKwh=target,
                queueLength=slot_count,
                queueCapacity=pile.queue_capacity,
                totalSessions=pile.total_sessions,
                totalChargedKwh=round(pile.total_charged_kwh, 3),
                totalChargingHours=round(total_h, 3),
                totalRevenue=round(pile.total_revenue, 2),
            )
        )

    waiting_fast = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.mode == ChargeMode.FAST,
            ChargingRequest.status == RequestStatus.WAITING,
        )
        .count()
    )
    waiting_slow = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.mode == ChargeMode.SLOW,
            ChargingRequest.status == RequestStatus.WAITING,
        )
        .count()
    )
    pending_reports = (
        db.query(AbnormalReport).filter(AbnormalReport.acknowledged.is_(False)).count()
    )

    return QueryPileStatusOut(
        piles=entries,
        waitingQueueFast=waiting_fast,
        waitingQueueSlow=waiting_slow,
        pendingAbnormalReports=pending_reports,
    )


# ────────────────────────────────────────────────────────────────────────────
# UC_08b 查看各充电桩等候服务的车辆信息（spec §5.3 必需字段）
# ────────────────────────────────────────────────────────────────────────────


@router.get("/piles/{pile_id}/queue", response_model=PileQueueDetailOut)
def query_pile_queue_detail(
    pile_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
) -> PileQueueDetailOut:
    pile = db.get(ChargingPile, pile_id)
    if pile is None:
        raise HTTPException(status_code=404, detail="pile not found")

    advance_active_sessions(db)
    db.commit()

    rows = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == pile.id,
            ChargingRequest.status.in_(PILE_SLOT_STATUSES),
        )
        .all()
    )
    # CHARGING 优先排首位，其余按到达顺序
    def _key(r: ChargingRequest):
        charging = 0 if r.status == RequestStatus.CHARGING else 1
        arrived = r.pile_queue_arrived_at or r.dispatched_at or r.submitted_at
        return (charging, arrived)
    rows.sort(key=_key)

    now = datetime.now()
    vehicles: list[PileQueuedVehicle] = []
    for r in rows:
        duration_min = (now - r.submitted_at).total_seconds() / 60.0
        vehicles.append(
            PileQueuedVehicle(
                userId=r.user_id,
                licensePlate=r.vehicle.license_plate,
                batteryCapacityKwh=r.vehicle.battery_capacity_kwh,
                requestedAmountKwh=r.target_amount_kwh,
                queueDurationMinutes=round(duration_min, 2),
                status=r.status,
                queueNumber=r.queue_number,
            )
        )
    return PileQueueDetailOut(pileId=pile.id, pileCode=pile.pile_code, vehicles=vehicles)


# ────────────────────────────────────────────────────────────────────────────
# UC_09 ConfirmPileFault
# ────────────────────────────────────────────────────────────────────────────


@router.post("/piles/{pile_id}/fault", response_model=ConfirmPileFaultOut)
def confirm_pile_fault_endpoint(
    pile_id: int,
    payload: ConfirmPileFaultIn,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_required),
) -> ConfirmPileFaultOut:
    try:
        fault, interrupted_session_id, rescheduled = confirm_pile_fault(
            db,
            pile_id=pile_id,
            fault_type=payload.faultType,
            fault_time=payload.faultTime,
            source_report_id=payload.sourceReportId,
            admin_user_id=admin.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return ConfirmPileFaultOut(
        accepted=True,
        message="fault confirmed",
        faultRecordId=fault.id,
        interruptedSessionId=interrupted_session_id,
        rescheduledRequests=rescheduled,
    )


# ────────────────────────────────────────────────────────────────────────────
# UC_10 ResumePile
# ────────────────────────────────────────────────────────────────────────────


@router.post("/piles/{pile_id}/resume", response_model=ResumePileOut)
def resume_pile_endpoint(
    pile_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
) -> ResumePileOut:
    try:
        resume_pile(db, pile_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return ResumePileOut(accepted=True, message="pile resumed to service")


# ────────────────────────────────────────────────────────────────────────────
# UC_11 QueryOperationReport
# ────────────────────────────────────────────────────────────────────────────


@router.get("/reports", response_model=OperationReportOut)
def query_operation_report(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
) -> OperationReportOut:
    now = datetime.now()
    if to_date is None:
        to_date = now
    if from_date is None:
        from_date = to_date - timedelta(days=7)
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from date must precede to date")

    # 时间范围内已完成 / 已中断会话
    sessions = (
        db.query(ChargingSession)
        .filter(
            ChargingSession.status.in_((SessionStatus.COMPLETED, SessionStatus.INTERRUPTED)),
            ChargingSession.started_at >= from_date,
            ChargingSession.started_at <= to_date,
        )
        .all()
    )
    bills = (
        db.query(Bill)
        .filter(
            Bill.created_at >= from_date,
            Bill.created_at <= to_date,
        )
        .all()
    )

    total_charged = sum(s.charged_kwh for s in sessions)
    paid_bills = sum(1 for b in bills if b.status == BillStatus.PAID)
    pending_bills = sum(1 for b in bills if b.status == BillStatus.PENDING)
    total_charging_fee = sum(b.charging_fee for b in bills)
    total_service_fee = sum(b.service_fee for b in bills)
    total_revenue = total_charging_fee + total_service_fee

    fault_count = (
        db.query(FaultRecord)
        .filter(
            FaultRecord.fault_time >= from_date,
            FaultRecord.fault_time <= to_date,
        )
        .count()
    )

    # 按桩分组 —— spec §5.3 报表字段
    pile_breakdown: list[dict] = []
    for pile in db.query(ChargingPile).order_by(ChargingPile.id).all():
        pile_sessions = [s for s in sessions if s.pile_id == pile.id]
        pile_bills = [b for b in bills if b.session.pile_id == pile.id]  # type: ignore[union-attr]
        # 累计充电时长（小时）：会话已结束 → ended_at-started_at；否则按 charged_kwh/power 推算
        total_duration_h = 0.0
        for s in pile_sessions:
            if s.ended_at is not None and s.started_at is not None:
                total_duration_h += (s.ended_at - s.started_at).total_seconds() / 3600.0
            elif s.power_kw > 0:
                total_duration_h += s.charged_kwh / s.power_kw
        pile_breakdown.append(
            {
                "pileCode": pile.pile_code,
                "mode": pile.mode.value,
                "sessions": len(pile_sessions),
                "chargedKwh": round(sum(s.charged_kwh for s in pile_sessions), 3),
                "chargingDurationHours": round(total_duration_h, 3),
                "chargingFee": round(sum(b.charging_fee for b in pile_bills), 2),
                "serviceFee": round(sum(b.service_fee for b in pile_bills), 2),
                "revenue": round(sum(b.total_amount for b in pile_bills), 2),
                "faults": db.query(FaultRecord)
                .filter(
                    FaultRecord.pile_id == pile.id,
                    FaultRecord.fault_time >= from_date,
                    FaultRecord.fault_time <= to_date,
                )
                .count(),
            }
        )

    return OperationReportOut(
        fromDate=from_date,
        toDate=to_date,
        totalSessions=len(sessions),
        totalChargedKwh=round(total_charged, 3),
        totalChargingFee=round(total_charging_fee, 2),
        totalServiceFee=round(total_service_fee, 2),
        totalRevenue=round(total_revenue, 2),
        paidBills=paid_bills,
        pendingBills=pending_bills,
        faultCount=fault_count,
        pilesBreakdown=pile_breakdown,
    )


# ────────────────────────────────────────────────────────────────────────────
# 辅助：查看异常上报列表（管理员决定是否升级为故障）
# ────────────────────────────────────────────────────────────────────────────


@router.get("/abnormal-reports")
def list_abnormal_reports(
    only_unack: bool = True,
    db: Session = Depends(get_db),
    _: User = Depends(admin_required),
):
    q = db.query(AbnormalReport)
    if only_unack:
        q = q.filter(AbnormalReport.acknowledged.is_(False))
    rows = q.order_by(AbnormalReport.reported_at.desc()).all()
    return [
        {
            "id": r.id,
            "pileId": r.pile_id,
            "pileCode": r.pile.pile_code,
            "userId": r.user_id,
            "description": r.description,
            "reportedAt": r.reported_at.isoformat(),
            "acknowledged": r.acknowledged,
        }
        for r in rows
    ]
