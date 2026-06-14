"""UC_09 ConfirmPileFault & UC_10 ResumePile 业务逻辑。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .config import FAULT_DISPATCH_POLICY, REQUEST_CODE_PREFIX
from .models import (
    AbnormalReport,
    ChargingPile,
    ChargingRequest,
    ChargingSession,
    FaultRecord,
    PileStatus,
    RequestStatus,
    SessionStatus,
)
from .pricing import generate_bill
from .scheduler import (
    PILE_SLOT_STATUSES,
    _refresh_pile_status,
    advance_active_sessions,
    try_dispatch,
)


def _make_request_code() -> str:
    return f"{REQUEST_CODE_PREFIX}{datetime.now().strftime('%Y%m%d')}{uuid.uuid4().hex[:6].upper()}"


def _assign_queue_number(db: Session, mode) -> str:
    """给重调度请求生成排队号。模式前缀 + 当日累计计数（不补零）。"""
    prefix = "F" if mode.value == "fast" else "T"
    today = datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    count = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.mode == mode,
            ChargingRequest.submitted_at >= start,
        )
        .count()
    )
    return f"{prefix}{count + 1}"


def confirm_pile_fault(
    db: Session,
    *,
    pile_id: int,
    fault_type: str,
    fault_time: Optional[datetime],
    source_report_id: Optional[int],
    admin_user_id: int,
) -> tuple[FaultRecord, Optional[int], int]:
    """处理桩故障确认。返回 (故障记录, 被中断会话 id, 受影响重调度请求数)。"""
    pile = db.get(ChargingPile, pile_id)
    if pile is None:
        raise ValueError("ChargingPile not found")
    if pile.status == PileStatus.FAULT:
        raise ValueError("Pile is already marked as FAULT")

    now = datetime.now()
    effective_fault_time = fault_time or now

    # 推进当前会话进度到故障时刻
    advance_active_sessions(db, now)

    # 1) 创建故障记录
    fault = FaultRecord(
        pile_id=pile.id,
        fault_type=fault_type,
        fault_time=effective_fault_time,
        confirmed_by=admin_user_id,
        source_report_id=source_report_id,
    )
    db.add(fault)

    # 关联的异常上报：标记为已确认
    if source_report_id is not None:
        report = db.get(AbnormalReport, source_report_id)
        if report is not None:
            report.acknowledged = True

    interrupted_session_id: Optional[int] = None
    rescheduled_count = 0

    # 2) 处理当前正在充电的会话
    active_session = (
        db.query(ChargingSession)
        .filter(
            ChargingSession.pile_id == pile.id,
            ChargingSession.status == SessionStatus.CHARGING,
        )
        .first()
    )
    if active_session is not None:
        original_req = active_session.request
        # 冻结电量、生成账单（已充电部分）
        active_session.status = SessionStatus.INTERRUPTED
        active_session.ended_at = now
        if active_session.charged_kwh > 0:
            bill = generate_bill(db, active_session)
            pile.total_revenue = round(pile.total_revenue + bill.total_amount, 2)
            pile.total_charged_kwh = round(pile.total_charged_kwh + active_session.charged_kwh, 4)
            pile.total_sessions += 1

        original_req.status = RequestStatus.FAULT_INTERRUPTED

        # 为剩余电量创建重调度请求（spec：进入故障队列，享最高优先级，不占等候区）
        remaining = round(active_session.target_kwh - active_session.charged_kwh, 4)
        if remaining > 0.01:
            new_req = ChargingRequest(
                request_code=_make_request_code(),
                user_id=original_req.user_id,
                vehicle_id=original_req.vehicle_id,
                mode=original_req.mode,
                target_amount_kwh=remaining,
                status=RequestStatus.FAULT_QUEUED,
                # 保留原始排队优先信息 —— 这是公平性关键
                priority_time=original_req.priority_time,
                queue_number=_assign_queue_number(db, original_req.mode),
                submitted_at=now,
                rescheduled_from_id=original_req.id,
                # 显示用：暂时挂在故障桩下，被重派后会更新到新桩
                assigned_pile_id=pile.id,
            )
            db.add(new_req)
            rescheduled_count += 1
        interrupted_session_id = active_session.id

    # 3) 处理已分桩但未开始充电的请求（DISPATCHED + QUEUING_PILE）→ 进入故障队列
    pending = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == pile.id,
            ChargingRequest.status.in_(
                (RequestStatus.DISPATCHED, RequestStatus.QUEUING_PILE)
            ),
        )
        .all()
    )
    for r in pending:
        r.status = RequestStatus.FAULT_QUEUED
        # 保留 assigned_pile_id（指向故障桩）—— 用于显示在原桩列下；
        # 一旦被重派到其它桩，_dispatch_one 会把 assigned_pile_id 改成新桩
        r.pile_queue_arrived_at = None
        r.dispatched_at = None
        r.confirmed_at = None
        # priority_time 不变，保留原始优先级
        rescheduled_count += 1

    # 4) 桩状态 → FAULT
    pile.status = PileStatus.FAULT
    db.flush()

    # 5) 按策略重调度
    if FAULT_DISPATCH_POLICY == "time_order":
        # spec 7.2 时间顺序调度：把其它同类型桩中【尚未开始充电】的 QUEUING_PILE 车
        # 全部撤回到 FAULT_QUEUED，与本次故障腾出的车合并，按 priority_time 重排
        other_pending = (
            db.query(ChargingRequest)
            .join(ChargingPile, ChargingRequest.assigned_pile_id == ChargingPile.id)
            .filter(
                ChargingPile.mode == pile.mode,
                ChargingPile.id != pile.id,
                ChargingRequest.status.in_(
                    (RequestStatus.DISPATCHED, RequestStatus.QUEUING_PILE)
                ),
            )
            .all()
        )
        for r in other_pending:
            r.status = RequestStatus.FAULT_QUEUED
            # 同样保留 assigned_pile_id（指向其原桩）—— 显示用
            r.pile_queue_arrived_at = None
            r.dispatched_at = None
            r.confirmed_at = None
            rescheduled_count += 1
        db.flush()
    # 不管哪种策略，Phase 1 (FAULT_QUEUED) 都按 priority_time 派出，再开等候区
    try_dispatch(db)

    return fault, interrupted_session_id, rescheduled_count


def resume_pile(db: Session, pile_id: int) -> None:
    """恢复桩服务：FAULT → AVAILABLE。

    按 spec §7.3：若其它同类型桩**仍有车辆排队**，需暂停等候区叫号服务，把
    其它同类型桩中【尚未充电】的车辆 (DISPATCHED + QUEUING_PILE) 与本桩合为
    一组，按排队号码（priority_time）顺序重新调度；调度完毕后再恢复等候区。
    """
    pile = db.get(ChargingPile, pile_id)
    if pile is None:
        raise ValueError("ChargingPile not found")
    if pile.status != PileStatus.FAULT:
        raise ValueError("Pile is not currently in FAULT status")

    now = datetime.now()
    open_fault = (
        db.query(FaultRecord)
        .filter(FaultRecord.pile_id == pile.id, FaultRecord.resolved_at.is_(None))
        .order_by(FaultRecord.fault_time.desc())
        .first()
    )
    if open_fault:
        open_fault.resolved_at = now

    pile.status = PileStatus.AVAILABLE
    _refresh_pile_status(db, pile)
    db.flush()

    # spec §7.3：检查是否需要"合并重排"
    other_pending = (
        db.query(ChargingRequest)
        .join(ChargingPile, ChargingRequest.assigned_pile_id == ChargingPile.id)
        .filter(
            ChargingPile.mode == pile.mode,
            ChargingPile.id != pile.id,
            ChargingRequest.status.in_(
                (RequestStatus.DISPATCHED, RequestStatus.QUEUING_PILE)
            ),
        )
        .all()
    )
    if other_pending:
        # 撤回到 FAULT_QUEUED 高优先级队列；按 priority_time 重排
        for r in other_pending:
            r.status = RequestStatus.FAULT_QUEUED
            # 保留 assigned_pile_id（指向其原桩）—— 显示用，重派后会被覆盖
            r.pile_queue_arrived_at = None
            r.dispatched_at = None
            r.confirmed_at = None
        db.flush()

    try_dispatch(db)
