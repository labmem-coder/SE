"""用户客户端 API —— 实现 UC_01 ~ UC_07。

路径映射（操作契约 → HTTP）：
    UC_01 SubmitChargeRequest   POST   /api/requests
    UC_02 UpdateChargeRequest   PUT    /api/requests/{request_id}
    UC_03 CancelChargeRequest   DELETE /api/requests/{request_id}
    UC_04 QueryQueueStatus      GET    /api/requests/{request_id}
    UC_05 ConfirmEntry          POST   /api/requests/{request_id}/confirm
    UC_06 ReportDeviceAbnormal  POST   /api/reports
    UC_07 QueryBill             GET    /api/bills/by-request/{request_id}
    UC_07 ConfirmPayment        POST   /api/bills/{bill_id}/pay
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import current_user, make_token, verify_password
from ..config import BILL_OVERDUE_HOURS, REQUEST_CODE_PREFIX, WAITING_AREA_SIZE
from ..db import get_db
from ..models import (
    AbnormalReport,
    Bill,
    BillStatus,
    ChargeMode,
    ChargingPile,
    ChargingRequest,
    ChargingSession,
    PileStatus,
    RequestStatus,
    SessionStatus,
    User,
    Vehicle,
)
from ..scheduler import (
    PILE_SLOT_STATUSES,
    _refresh_pile_status,
    advance_active_sessions,
    try_dispatch,
)
from ..schemas import (
    BillOut,
    CancelChargeRequestOut,
    ConfirmEntryOut,
    ConfirmPaymentIn,
    ConfirmPaymentOut,
    LoginRequest,
    LoginResponse,
    QueueInfo,
    ReportDeviceAbnormalIn,
    ReportDeviceAbnormalOut,
    SubmitChargeRequestIn,
    SubmitChargeRequestOut,
    UpdateChargeRequestIn,
    UpdateChargeRequestOut,
    UserOut,
)
from ..views import build_queue_info


router = APIRouter(prefix="/api", tags=["user"])


# ────────────────────────────────────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────────────────────────────────────


def _make_request_code() -> str:
    return f"{REQUEST_CODE_PREFIX}{datetime.utcnow().strftime('%Y%m%d')}{uuid.uuid4().hex[:6].upper()}"


def _assign_queue_number(db: Session, mode: ChargeMode) -> str:
    """排队号：spec §1 示例 "F1, F2, T1, T2" —— 自然递增，不补零。"""
    prefix = "F" if mode == ChargeMode.FAST else "T"
    today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    count = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.mode == mode,
            ChargingRequest.submitted_at >= today_start,
        )
        .count()
    )
    return f"{prefix}{count + 1}"


def _user_has_overdue_unpaid_bill(db: Session, user_id: int) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=BILL_OVERDUE_HOURS)
    return (
        db.query(Bill)
        .filter(
            Bill.user_id == user_id,
            Bill.status == BillStatus.PENDING,
            Bill.created_at < cutoff,
        )
        .first()
        is not None
    )


def _vehicle_has_active_request(db: Session, vehicle_id: int) -> bool:
    return (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.vehicle_id == vehicle_id,
            ChargingRequest.status.in_(
                (
                    RequestStatus.WAITING,
                    RequestStatus.DISPATCHED,
                    RequestStatus.QUEUING_PILE,
                    RequestStatus.CHARGING,
                )
            ),
        )
        .first()
        is not None
    )


def _waiting_area_count(db: Session) -> int:
    """当前等候区中的车辆数。

    注意：故障腾出的车在 FAULT_QUEUED 状态，按 spec 享受最高调度优先级，
    属于"损坏桩队列"而非"等候区"，不计入 N=10 上限。
    """
    return (
        db.query(ChargingRequest)
        .filter(ChargingRequest.status == RequestStatus.WAITING)
        .count()
    )


def _validate_entry_token(token: str) -> bool:
    """等候区入场凭证。课程项目中接受任何非空字符串。"""
    return bool(token and token.strip())


def _bill_to_out(bill: Bill) -> BillOut:
    """把 Bill ORM 转 BillOut，附加 spec 必需的会话字段（桩号、起止时间、时长）。"""
    base = BillOut.model_validate(bill).model_dump()
    sess = bill.session
    if sess is not None:
        base["pile_code"] = sess.pile.pile_code if sess.pile else None
        base["started_at"] = sess.started_at
        base["ended_at"] = sess.ended_at
        if sess.ended_at is not None and sess.started_at is not None:
            duration_h = (sess.ended_at - sess.started_at).total_seconds() / 3600.0
            base["charging_duration_hours"] = round(duration_h, 4)
    return BillOut.model_validate(base)


# ────────────────────────────────────────────────────────────────────────────
# 登录 / 个人信息
# ────────────────────────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.query(User).filter(User.username == payload.username).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return LoginResponse(
        token=make_token(user.id),
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
    )


@router.get("/me", response_model=UserOut)
def whoami(user: User = Depends(current_user)) -> User:
    return user


# ────────────────────────────────────────────────────────────────────────────
# UC_01 SubmitChargeRequest
# ────────────────────────────────────────────────────────────────────────────


@router.post("/requests", response_model=SubmitChargeRequestOut)
def submit_charge_request(
    payload: SubmitChargeRequestIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> SubmitChargeRequestOut:
    # 前置条件 1+3：入场凭证
    if not _validate_entry_token(payload.entryToken):
        raise HTTPException(status_code=400, detail="invalid entryToken")

    # 前置条件 2：车辆属于该用户
    vehicle = db.get(Vehicle, payload.vehicleId)
    if vehicle is None or vehicle.owner_id != user.id:
        raise HTTPException(status_code=404, detail="vehicle not found")

    # 前置条件 4：该车暂无进行中的请求
    if _vehicle_has_active_request(db, vehicle.id):
        raise HTTPException(status_code=409, detail="vehicle already has an active request")

    # 前置条件 5：用户无超期未支付账单
    if _user_has_overdue_unpaid_bill(db, user.id):
        raise HTTPException(
            status_code=402, detail="user has overdue unpaid bill, please settle first"
        )

    # 前置条件 6：等候区容量 N=WAITING_AREA_SIZE 不能超。
    # spec："等候区外的请求暂时不考虑" —— 等候区满时新请求被拒收。
    # 注意：FAULT_QUEUED（损坏桩队列）按 spec 不占等候区名额，本检查只看 WAITING。
    try_dispatch(db)
    if _waiting_area_count(db) >= WAITING_AREA_SIZE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"waiting area is full (capacity={WAITING_AREA_SIZE}); "
                "request rejected as it would be outside the waiting area"
            ),
        )

    # 创建请求
    now = datetime.utcnow()
    req = ChargingRequest(
        request_code=_make_request_code(),
        user_id=user.id,
        vehicle_id=vehicle.id,
        mode=payload.mode,
        target_amount_kwh=payload.targetAmount,
        status=RequestStatus.WAITING,
        priority_time=now,
        queue_number=_assign_queue_number(db, payload.mode),
        submitted_at=now,
    )
    db.add(req)
    db.flush()

    # 立即尝试调度
    try_dispatch(db)
    db.commit()
    db.refresh(req)

    return SubmitChargeRequestOut(
        accepted=True,
        message="charge request accepted",
        queueInfo=build_queue_info(db, req),
    )


# ────────────────────────────────────────────────────────────────────────────
# UC_04 QueryQueueStatus
# ────────────────────────────────────────────────────────────────────────────


@router.get("/requests/{request_id}", response_model=QueueInfo)
def query_queue_status(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> QueueInfo:
    req = db.get(ChargingRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(status_code=404, detail="request not found")
    # 顺手推进会话进度，使展示更"实时"
    advance_active_sessions(db)
    db.commit()
    db.refresh(req)
    return build_queue_info(db, req)


@router.get("/me/requests", response_model=list[QueueInfo])
def list_my_requests(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[QueueInfo]:
    advance_active_sessions(db)
    db.commit()
    rows = (
        db.query(ChargingRequest)
        .filter(ChargingRequest.user_id == user.id)
        .order_by(ChargingRequest.submitted_at.desc())
        .all()
    )
    return [build_queue_info(db, r) for r in rows]


# ────────────────────────────────────────────────────────────────────────────
# UC_02 UpdateChargeRequest
# ────────────────────────────────────────────────────────────────────────────


@router.put("/requests/{request_id}", response_model=UpdateChargeRequestOut)
def update_charge_request(
    request_id: int,
    payload: UpdateChargeRequestIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> UpdateChargeRequestOut:
    req = db.get(ChargingRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(status_code=404, detail="request not found")
    # 前置条件 2：仅在"未开始充电"前可改
    if req.status not in (
        RequestStatus.WAITING,
        RequestStatus.FAULT_QUEUED,
        RequestStatus.DISPATCHED,
        RequestStatus.QUEUING_PILE,
    ):
        raise HTTPException(
            status_code=409, detail=f"cannot modify request in status {req.status.value}"
        )
    if payload.newMode is None and payload.newTargetAmount is None:
        raise HTTPException(status_code=400, detail="nothing to update")

    now = datetime.utcnow()
    mode_changed = payload.newMode is not None and payload.newMode != req.mode

    if mode_changed:
        # 变更模式 → 取消原请求 + 新建一条新请求（公平：到新队列末尾）
        # 注意：若已分桩，释放桩位
        affected_pile_id = req.assigned_pile_id
        req.status = RequestStatus.CANCELLED
        req.cancelled_at = now
        req.assigned_pile_id = None
        req.pile_queue_arrived_at = None
        db.flush()

        new_target = payload.newTargetAmount if payload.newTargetAmount is not None else req.target_amount_kwh
        new_req = ChargingRequest(
            request_code=_make_request_code(),
            user_id=user.id,
            vehicle_id=req.vehicle_id,
            mode=payload.newMode,
            target_amount_kwh=new_target,
            status=RequestStatus.WAITING,
            priority_time=now,                                # 排到新队列末尾
            queue_number=_assign_queue_number(db, payload.newMode),
            submitted_at=now,
        )
        db.add(new_req)
        db.flush()

        if affected_pile_id is not None:
            pile = db.get(ChargingPile, affected_pile_id)
            if pile is not None:
                _refresh_pile_status(db, pile)

        try_dispatch(db)
        db.commit()
        db.refresh(new_req)
        return UpdateChargeRequestOut(
            accepted=True,
            message="mode changed; original request cancelled and resubmitted",
            queueInfo=build_queue_info(db, new_req),
            modeChanged=True,
        )

    # 仅改电量：直接更新
    if payload.newTargetAmount is not None:
        # 不允许把电量改到比"已充电量"还低（这里 WAITING/DISPATCHED/QUEUING_PILE 都还没真正充电，跳过）
        req.target_amount_kwh = payload.newTargetAmount

    try_dispatch(db)
    db.commit()
    db.refresh(req)
    return UpdateChargeRequestOut(
        accepted=True,
        message="target updated",
        queueInfo=build_queue_info(db, req),
        modeChanged=False,
    )


# ────────────────────────────────────────────────────────────────────────────
# UC_03 CancelChargeRequest
# ────────────────────────────────────────────────────────────────────────────


@router.delete("/requests/{request_id}", response_model=CancelChargeRequestOut)
def cancel_charge_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CancelChargeRequestOut:
    req = db.get(ChargingRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(status_code=404, detail="request not found")
    if req.status in (
        RequestStatus.COMPLETED,
        RequestStatus.CANCELLED,
        RequestStatus.FAULT_INTERRUPTED,
    ):
        raise HTTPException(
            status_code=409, detail=f"cannot cancel request in status {req.status.value}"
        )

    now = datetime.utcnow()
    affected_pile_id = req.assigned_pile_id

    # 用户在充电中取消：停止会话，按已充电量出账单（spec 允许）。
    if req.status == RequestStatus.CHARGING and req.session is not None:
        advance_active_sessions(db, now)
        sess = req.session
        sess.status = SessionStatus.COMPLETED
        sess.ended_at = now
        if sess.charged_kwh > 0:
            from .. import pricing as _pricing
            bill = _pricing.generate_bill(db, sess)
            sess.pile.total_sessions += 1
            sess.pile.total_charged_kwh = round(
                sess.pile.total_charged_kwh + sess.charged_kwh, 4
            )
            sess.pile.total_revenue = round(
                sess.pile.total_revenue + bill.total_amount, 2
            )

    req.status = RequestStatus.CANCELLED
    req.cancelled_at = now
    req.assigned_pile_id = None
    req.pile_queue_arrived_at = None
    db.flush()

    if affected_pile_id is not None:
        pile = db.get(ChargingPile, affected_pile_id)
        if pile is not None:
            _refresh_pile_status(db, pile)

    try_dispatch(db)
    db.commit()
    return CancelChargeRequestOut(accepted=True, message="cancelled")


# ────────────────────────────────────────────────────────────────────────────
# UC_05 ConfirmEntry
# ────────────────────────────────────────────────────────────────────────────


@router.post("/requests/{request_id}/confirm", response_model=ConfirmEntryOut)
def confirm_entry(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConfirmEntryOut:
    req = db.get(ChargingRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(status_code=404, detail="request not found")
    if req.status != RequestStatus.DISPATCHED:
        raise HTTPException(
            status_code=409, detail=f"only DISPATCHED requests can be confirmed (now: {req.status.value})"
        )
    if req.assigned_pile_id is None:
        raise HTTPException(status_code=500, detail="dispatched request without pile")

    now = datetime.utcnow()
    req.status = RequestStatus.QUEUING_PILE
    req.confirmed_at = now
    req.pile_queue_arrived_at = now
    db.flush()

    # 触发：如果桩此时空闲，可立刻开始充电
    try_dispatch(db)
    db.commit()
    db.refresh(req)
    return ConfirmEntryOut(
        accepted=True,
        message="entry confirmed",
        queueInfo=build_queue_info(db, req),
    )


# ────────────────────────────────────────────────────────────────────────────
# UC_06 ReportDeviceAbnormal
# ────────────────────────────────────────────────────────────────────────────


@router.post("/reports", response_model=ReportDeviceAbnormalOut)
def report_device_abnormal(
    payload: ReportDeviceAbnormalIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ReportDeviceAbnormalOut:
    pile = db.get(ChargingPile, payload.pileId)
    if pile is None:
        raise HTTPException(status_code=404, detail="pile not found")

    # 软性校验：用户当前正在或最近使用过该桩
    related = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.user_id == user.id,
            ChargingRequest.assigned_pile_id == pile.id,
            ChargingRequest.status.in_(
                (
                    RequestStatus.DISPATCHED,
                    RequestStatus.QUEUING_PILE,
                    RequestStatus.CHARGING,
                    RequestStatus.COMPLETED,
                    RequestStatus.FAULT_INTERRUPTED,
                )
            ),
        )
        .first()
    )
    if related is None:
        # 不阻塞，仅作为软警告
        pass

    report = AbnormalReport(
        user_id=user.id,
        pile_id=pile.id,
        description=payload.description,
    )
    db.add(report)
    db.flush()
    db.commit()
    return ReportDeviceAbnormalOut(
        accepted=True,
        reportId=report.id,
        message="report submitted; admin will review",
    )


# ────────────────────────────────────────────────────────────────────────────
# UC_07 QueryBill / ConfirmPayment
# ────────────────────────────────────────────────────────────────────────────


@router.get("/bills/by-request/{request_id}", response_model=BillOut)
def query_bill_by_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> BillOut:
    req = db.get(ChargingRequest, request_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(status_code=404, detail="request not found")
    session = (
        db.query(ChargingSession)
        .filter(ChargingSession.request_id == req.id)
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="no session for this request yet")
    bill = db.query(Bill).filter(Bill.session_id == session.id).first()
    if bill is None:
        raise HTTPException(status_code=404, detail="bill not generated yet")
    return _bill_to_out(bill)


@router.get("/me/bills", response_model=list[BillOut])
def list_my_bills(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[BillOut]:
    bills = (
        db.query(Bill)
        .filter(Bill.user_id == user.id)
        .order_by(Bill.created_at.desc())
        .all()
    )
    return [_bill_to_out(b) for b in bills]


@router.post("/bills/{bill_id}/pay", response_model=ConfirmPaymentOut)
def confirm_payment(
    bill_id: int,
    payload: ConfirmPaymentIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ConfirmPaymentOut:
    bill = db.get(Bill, bill_id)
    if bill is None or bill.user_id != user.id:
        raise HTTPException(status_code=404, detail="bill not found")
    if bill.status == BillStatus.PAID:
        return ConfirmPaymentOut(accepted=True, message="bill already paid", bill=_bill_to_out(bill))

    bill.status = BillStatus.PAID
    bill.paid_at = datetime.utcnow()
    bill.pay_channel = payload.payChannel
    db.commit()
    db.refresh(bill)
    return ConfirmPaymentOut(
        accepted=True,
        message="payment recorded",
        bill=_bill_to_out(bill),
    )
