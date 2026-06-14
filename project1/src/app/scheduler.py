"""调度算法 / 会话推进 / 桩状态维护。

核心调度规则（来自 overview.md）：
    被调度车辆完成充电所需时间 = 等待时长 + 自己充电时长，**最短**。

工作流程：
    try_dispatch() 是入口；任何会改变排队/桩状态的事件后调用即可：
      - 新请求提交
      - 用户取消
      - 充电完成
      - 桩故障恢复
      - 后台 tick
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import (
    ENTRY_CONFIRM_TIMEOUT_SECONDS,
    EXTENDED_SCHEDULE_POLICY,
    PILE_QUEUE_CAPACITY,
    TIME_ACCELERATION,
    WAITING_AREA_SIZE,
)
from .models import (
    ChargeMode,
    ChargingPile,
    ChargingRequest,
    ChargingSession,
    PileStatus,
    RequestStatus,
    SessionStatus,
)
from .pricing import generate_bill


# 在某桩上"占用一个车位"的请求状态集合
PILE_SLOT_STATUSES = (
    RequestStatus.DISPATCHED,
    RequestStatus.QUEUING_PILE,
    RequestStatus.CHARGING,
)


# ────────────────────────────────────────────────────────────────────────────
# 工具：会话剩余时间、桩排队
# ────────────────────────────────────────────────────────────────────────────


def remaining_hours_for_session(session: ChargingSession) -> float:
    """正在充电会话的剩余时长 (h)。"""
    remaining = max(session.target_kwh - session.charged_kwh, 0.0)
    if session.power_kw <= 0:
        return 0.0
    return remaining / session.power_kw


def pile_slot_count(db: Session, pile_id: int) -> int:
    """桩上占用的车位数（DISPATCHED + QUEUING_PILE + CHARGING）。"""
    return (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == pile_id,
            ChargingRequest.status.in_(PILE_SLOT_STATUSES),
        )
        .count()
    )


def pile_queue_wait_hours(db: Session, pile: ChargingPile) -> float:
    """桩上所有"已占车位但尚未轮到的"请求总等待时长 (h)。

    - 正在充电的：剩余电量 / 功率
    - 在桩排队的（QUEUING_PILE）：完整目标电量 / 功率
    - 已调度但未确认的（DISPATCHED）：完整目标电量 / 功率（保守估计）
    """
    total = 0.0
    rows = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == pile.id,
            ChargingRequest.status.in_(PILE_SLOT_STATUSES),
        )
        .all()
    )
    for req in rows:
        if req.status == RequestStatus.CHARGING and req.session is not None:
            total += remaining_hours_for_session(req.session)
        else:
            total += req.target_amount_kwh / pile.power_kw
    return total


def estimate_finish_hours(db: Session, pile: ChargingPile, candidate_req: ChargingRequest) -> float:
    """若把 candidate_req 派到该桩，从现在算它完成充电的总时间 (h)。"""
    own = candidate_req.target_amount_kwh / pile.power_kw
    return pile_queue_wait_hours(db, pile) + own


def pile_queue_position(db: Session, req: ChargingRequest) -> Optional[int]:
    """请求在桩排队中的 1-indexed 位置（1 = 正在充电）。"""
    if req.assigned_pile_id is None or req.status not in PILE_SLOT_STATUSES:
        return None
    # 按"已确认入场时间"或"调度时间"排序（CHARGING 最优先，再按 confirmed_at / dispatched_at）
    rows = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == req.assigned_pile_id,
            ChargingRequest.status.in_(PILE_SLOT_STATUSES),
        )
        .all()
    )
    # 排序：CHARGING 第一，其余按 pile_queue_arrived_at（若有），再按 dispatched_at
    def sort_key(r: ChargingRequest):
        charging_flag = 0 if r.status == RequestStatus.CHARGING else 1
        arrived = r.pile_queue_arrived_at or r.dispatched_at or r.submitted_at
        return (charging_flag, arrived)

    rows.sort(key=sort_key)
    for i, r in enumerate(rows, start=1):
        if r.id == req.id:
            return i
    return None


# ────────────────────────────────────────────────────────────────────────────
# 充电进度推进 / 会话完成
# ────────────────────────────────────────────────────────────────────────────


def advance_active_sessions(db: Session, now: Optional[datetime] = None) -> None:
    """根据实际经过时间 × TIME_ACCELERATION，推进所有 CHARGING 会话的 charged_kwh。"""
    now = now or datetime.utcnow()
    sessions = (
        db.query(ChargingSession)
        .filter(ChargingSession.status == SessionStatus.CHARGING)
        .all()
    )
    for s in sessions:
        elapsed_real_seconds = (now - s.last_tick_at).total_seconds()
        if elapsed_real_seconds <= 0:
            continue
        sim_seconds = elapsed_real_seconds * TIME_ACCELERATION
        added_kwh = sim_seconds / 3600.0 * s.power_kw
        new_charged = min(s.charged_kwh + added_kwh, s.target_kwh)
        s.charged_kwh = new_charged
        s.last_tick_at = now


def _complete_session(db: Session, session: ChargingSession) -> None:
    """充电完成的扫尾：账单、桩统计、状态。"""
    session.charged_kwh = session.target_kwh
    session.status = SessionStatus.COMPLETED
    # 模拟的 end 时刻（用于账单分时电价的"虚拟时间窗"）
    sim_duration_seconds = session.target_kwh / session.power_kw * 3600.0
    session.ended_at = session.started_at + timedelta(seconds=sim_duration_seconds)

    req: ChargingRequest = session.request
    req.status = RequestStatus.COMPLETED

    pile: ChargingPile = session.pile
    pile.total_sessions += 1
    pile.total_charged_kwh = round(pile.total_charged_kwh + session.charged_kwh, 4)

    bill = generate_bill(db, session)
    pile.total_revenue = round(pile.total_revenue + bill.total_amount, 2)

    db.flush()


def handle_completed_sessions(db: Session) -> int:
    """检查所有 CHARGING 会话是否已达目标电量，若是则结算并启动下一辆。"""
    sessions = (
        db.query(ChargingSession)
        .filter(ChargingSession.status == SessionStatus.CHARGING)
        .all()
    )
    closed = 0
    affected_piles: set[int] = set()
    for s in sessions:
        if s.charged_kwh + 1e-9 >= s.target_kwh:
            pile_id = s.pile_id
            _complete_session(db, s)
            affected_piles.add(pile_id)
            closed += 1

    for pid in affected_piles:
        pile = db.get(ChargingPile, pid)
        if pile:
            _maybe_start_next_at_pile(db, pile)
            _refresh_pile_status(db, pile)

    return closed


# ────────────────────────────────────────────────────────────────────────────
# 桩内排队 → 启动下一辆
# ────────────────────────────────────────────────────────────────────────────


def _maybe_start_next_at_pile(db: Session, pile: ChargingPile) -> bool:
    """若桩可用且无在充会话，则按 FIFO 启动桩排队队首的 QUEUING_PILE 请求。"""
    if pile.status == PileStatus.FAULT:
        return False

    active = (
        db.query(ChargingSession)
        .filter(
            ChargingSession.pile_id == pile.id,
            ChargingSession.status == SessionStatus.CHARGING,
        )
        .first()
    )
    if active:
        return False

    next_req = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == pile.id,
            ChargingRequest.status == RequestStatus.QUEUING_PILE,
        )
        .order_by(ChargingRequest.pile_queue_arrived_at.asc())
        .first()
    )
    if not next_req:
        return False

    now = datetime.utcnow()
    session = ChargingSession(
        request_id=next_req.id,
        pile_id=pile.id,
        status=SessionStatus.CHARGING,
        started_at=now,
        target_kwh=next_req.target_amount_kwh,
        charged_kwh=0.0,
        last_tick_at=now,
        power_kw=pile.power_kw,
    )
    db.add(session)
    next_req.status = RequestStatus.CHARGING
    pile.status = PileStatus.OCCUPIED
    db.flush()
    return True


def _refresh_pile_status(db: Session, pile: ChargingPile) -> None:
    """若桩非故障，则根据是否有占位请求设置 AVAILABLE / OCCUPIED。"""
    if pile.status == PileStatus.FAULT:
        return
    occupied = pile_slot_count(db, pile.id) > 0
    pile.status = PileStatus.OCCUPIED if occupied else PileStatus.AVAILABLE


# ────────────────────────────────────────────────────────────────────────────
# 调度超时检查
# ────────────────────────────────────────────────────────────────────────────


def handle_dispatch_timeouts(db: Session, now: Optional[datetime] = None) -> int:
    """5 分钟未 ConfirmEntry 的 DISPATCHED 请求 → 自动取消。"""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=ENTRY_CONFIRM_TIMEOUT_SECONDS)
    timed_out = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.status == RequestStatus.DISPATCHED,
            ChargingRequest.dispatched_at < cutoff,
        )
        .all()
    )
    cancelled = 0
    affected_piles: set[int] = set()
    for req in timed_out:
        if req.assigned_pile_id is not None:
            affected_piles.add(req.assigned_pile_id)
        req.status = RequestStatus.CANCELLED
        req.cancelled_at = now
        req.assigned_pile_id = None
        cancelled += 1

    for pid in affected_piles:
        pile = db.get(ChargingPile, pid)
        if pile:
            _refresh_pile_status(db, pile)

    db.flush()
    return cancelled


# ────────────────────────────────────────────────────────────────────────────
# 主调度入口
# ────────────────────────────────────────────────────────────────────────────


def try_dispatch(db: Session) -> int:
    """把尽可能多的等待请求派发到充电区。返回派发数量。

    幂等：任何时候都可调用。先推进时间与超时，再做派发。

    根据 config.EXTENDED_SCHEDULE_POLICY 切换调度算法：
        normal       —— 标准单车顺序贪心（默认）
        multi_short  —— spec §8.1 单次多车总充电时长最短
        batch_short  —— spec §8.2 批量调度（充电区+等候区满时触发，混合快慢）
    """
    advance_active_sessions(db)
    handle_completed_sessions(db)
    handle_dispatch_timeouts(db)

    dispatched_total = 0

    if EXTENDED_SCHEDULE_POLICY == "batch_short":
        # 8.2 批量：仅当全部车位被占满才触发
        if _batch_full(db):
            dispatched_total += _dispatch_batch_mixed(db)
        # 否则按 normal 兜底
        if dispatched_total == 0:
            for mode in (ChargeMode.FAST, ChargeMode.SLOW):
                dispatched_total += _dispatch_mode(db, mode)
    elif EXTENDED_SCHEDULE_POLICY == "multi_short":
        for mode in (ChargeMode.FAST, ChargeMode.SLOW):
            dispatched_total += _dispatch_mode_multi_short(db, mode)
    else:
        for mode in (ChargeMode.FAST, ChargeMode.SLOW):
            dispatched_total += _dispatch_mode(db, mode)

    # 推进一下：派发后可能让一些桩立即开始充电
    for pile in db.query(ChargingPile).filter(
        ChargingPile.status != PileStatus.FAULT
    ).all():
        _maybe_start_next_at_pile(db, pile)

    db.flush()
    return dispatched_total


def _dispatch_one(db: Session, req: ChargingRequest) -> bool:
    """把单个请求派到该模式下"完成时间最短"的桩。成功返回 True。"""
    piles = (
        db.query(ChargingPile)
        .filter(
            ChargingPile.mode == req.mode,
            ChargingPile.status != PileStatus.FAULT,
        )
        .all()
    )
    best = None  # (finish_hours, pile)
    for pile in piles:
        slots_used = pile_slot_count(db, pile.id)
        if slots_used >= pile.queue_capacity:
            continue
        fh = estimate_finish_hours(db, pile, req)
        if best is None or fh < best[0]:
            best = (fh, pile)
    if best is None:
        return False
    _, chosen_pile = best
    now = datetime.utcnow()
    req.status = RequestStatus.DISPATCHED
    req.assigned_pile_id = chosen_pile.id
    req.dispatched_at = now
    if chosen_pile.status == PileStatus.AVAILABLE:
        chosen_pile.status = PileStatus.OCCUPIED
    db.flush()
    return True


def _dispatch_mode(db: Session, mode: ChargeMode) -> int:
    """对单一模式重复派发：
       Phase 1 故障队列：spec 要求"优先调度损坏充电桩队列里的车直至全部进入充电区"。
       Phase 2 等候区：仅在故障队列被全部排空后，才调度普通等候区。"""
    count = 0
    # ── Phase 1: FAULT_QUEUED 优先 ──
    while True:
        req = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.mode == mode,
                ChargingRequest.status == RequestStatus.FAULT_QUEUED,
            )
            .order_by(ChargingRequest.priority_time.asc())
            .first()
        )
        if not req:
            break
        if not _dispatch_one(db, req):
            # 故障队列还有车未派出去 → spec 要求暂停等候区调度
            return count
        count += 1

    # ── Phase 2: WAITING ──
    while True:
        req = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.mode == mode,
                ChargingRequest.status == RequestStatus.WAITING,
            )
            .order_by(ChargingRequest.priority_time.asc())
            .first()
        )
        if not req:
            return count
        if not _dispatch_one(db, req):
            return count
        count += 1


# ────────────────────────────────────────────────────────────────────────────
# 排队视图（QueryQueueStatus 用）
# ────────────────────────────────────────────────────────────────────────────


def waiting_queue_position(db: Session, req: ChargingRequest) -> Optional[int]:
    """请求在该模式等待队列中的 1-indexed 位置；非 WAITING 返回 None。"""
    if req.status != RequestStatus.WAITING:
        return None
    earlier = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.mode == req.mode,
            ChargingRequest.status == RequestStatus.WAITING,
            ChargingRequest.priority_time < req.priority_time,
        )
        .count()
    )
    return earlier + 1


def estimate_wait_minutes(db: Session, req: ChargingRequest) -> float:
    """对 WAITING / DISPATCHED / QUEUING_PILE 请求估算"距离开始充电"剩余分钟。"""
    if req.status == RequestStatus.CHARGING:
        return 0.0
    if req.status in (RequestStatus.COMPLETED, RequestStatus.CANCELLED, RequestStatus.FAULT_INTERRUPTED):
        return 0.0

    if req.status == RequestStatus.WAITING:
        # 等：先等到分配车位（保守估：模式下所有桩当前总等待时长的平均最小值）
        piles = (
            db.query(ChargingPile)
            .filter(
                ChargingPile.mode == req.mode,
                ChargingPile.status != PileStatus.FAULT,
            )
            .all()
        )
        if not piles:
            return float("inf")
        # 估算：等同模式车队提前于自己的请求都先进入充电区后，再轮到自己进入并充电
        ahead_in_waiting = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.mode == req.mode,
                ChargingRequest.status == RequestStatus.WAITING,
                ChargingRequest.priority_time < req.priority_time,
            )
            .count()
        )
        # 用"该模式下桩平均当前等待时长 + (前面排队人数 / 桩数 × 平均充电时长)"粗略估算
        avg_wait_h = sum(pile_queue_wait_hours(db, p) for p in piles) / len(piles)
        avg_charge_h = req.target_amount_kwh / piles[0].power_kw
        expected_h = avg_wait_h + (ahead_in_waiting + 1) * avg_charge_h / len(piles)
        return round(expected_h * 60.0, 2)

    if req.status in (RequestStatus.DISPATCHED, RequestStatus.QUEUING_PILE):
        if req.assigned_pile_id is None:
            return 0.0
        pile = db.get(ChargingPile, req.assigned_pile_id)
        if pile is None:
            return 0.0
        # 已分桩：等本桩前面排队
        ahead = 0.0
        rows = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.assigned_pile_id == pile.id,
                ChargingRequest.status.in_(PILE_SLOT_STATUSES),
                ChargingRequest.id != req.id,
            )
            .all()
        )
        for r in rows:
            # 仅统计排在我前面的
            cmp_self = (req.pile_queue_arrived_at or req.dispatched_at or req.submitted_at)
            cmp_other = (r.pile_queue_arrived_at or r.dispatched_at or r.submitted_at)
            if r.status == RequestStatus.CHARGING:
                ahead += remaining_hours_for_session(r.session) if r.session else 0.0
            elif cmp_other < cmp_self:
                ahead += r.target_amount_kwh / pile.power_kw
        return round(ahead * 60.0, 2)

    return 0.0


# ────────────────────────────────────────────────────────────────────────────
# 扩展调度 §8.1 单次多车总充电时长最短
# ────────────────────────────────────────────────────────────────────────────


def _eligible_piles(db: Session, mode: ChargeMode):
    """返回该模式下非故障桩的 (pile, free_slots, current_wait_h, power_kw) 列表。"""
    piles = (
        db.query(ChargingPile)
        .filter(
            ChargingPile.mode == mode,
            ChargingPile.status != PileStatus.FAULT,
        )
        .all()
    )
    info = []
    for p in piles:
        used = pile_slot_count(db, p.id)
        free = p.queue_capacity - used
        if free <= 0:
            continue
        info.append({
            "pile": p,
            "free": free,
            "wait_h": pile_queue_wait_hours(db, p),
            "power": p.power_kw,
        })
    return info


def _dispatch_mode_multi_short(db: Session, mode: ChargeMode) -> int:
    """spec §8.1：先派故障队列（保持优先级），再一次性把等候区前 K 辆
    （K=可用桩位数）按"总完成时间最短"分配，桩内按 SPT 顺序。"""
    count = 0
    # Phase 1: FAULT_QUEUED 仍按 priority_time 单辆派
    while True:
        req = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.mode == mode,
                ChargingRequest.status == RequestStatus.FAULT_QUEUED,
            )
            .order_by(ChargingRequest.priority_time.asc())
            .first()
        )
        if not req:
            break
        if not _dispatch_one(db, req):
            return count
        count += 1

    # Phase 2: WAITING 多车一次性分配
    pile_info = _eligible_piles(db, mode)
    if not pile_info:
        return count
    waiting = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.mode == mode,
            ChargingRequest.status == RequestStatus.WAITING,
        )
        .order_by(ChargingRequest.priority_time.asc())
        .all()
    )
    if not waiting:
        return count

    total_free = sum(p["free"] for p in pile_info)
    K = min(total_free, len(waiting))
    cars = waiting[:K]

    # SPT-LIST 算法（多项式时间，identical machine 下证明最优 SUM(C_i)）：
    # 1) 把车按 target 升序（短的优先）
    # 2) 每辆车分给当前 wait_h 最小的桩（earliest available）
    # 3) 该桩 wait_h += own_h
    sorted_cars = sorted(cars, key=lambda c: c.target_amount_kwh)
    # 复制 pile_info 的 wait_h 与 free 字段做局部状态
    state = [{"info": p, "wait_h": p["wait_h"], "free": p["free"]} for p in pile_info]
    assignments = []  # (car, pile_info, order_in_pile)
    pile_order_counter = [0] * len(state)
    for c in sorted_cars:
        # 找 free>0 且 wait_h 最小的桩
        cand = [(s["wait_h"], idx) for idx, s in enumerate(state) if s["free"] > 0]
        if not cand:
            break
        _, idx = min(cand)
        own_h = c.target_amount_kwh / state[idx]["info"]["power"]
        state[idx]["wait_h"] += own_h
        state[idx]["free"] -= 1
        assignments.append((c, state[idx]["info"], pile_order_counter[idx]))
        pile_order_counter[idx] += 1

    if not assignments:
        return count

    # 派遣 —— dispatched_at 按 SPT 顺序微增，保证桩内 FIFO 与 SPT 一致
    now = datetime.utcnow()
    for car, info, order in assignments:
        car.status = RequestStatus.DISPATCHED
        car.assigned_pile_id = info["pile"].id
        car.dispatched_at = now + timedelta(microseconds=order)
        if info["pile"].status == PileStatus.AVAILABLE:
            info["pile"].status = PileStatus.OCCUPIED
        count += 1
    db.flush()
    return count


# ────────────────────────────────────────────────────────────────────────────
# 扩展调度 §8.2 批量调度（全车位满才触发，混合快慢）
# ────────────────────────────────────────────────────────────────────────────


def _batch_full(db: Session) -> bool:
    """spec §8.2 触发条件：到达充电站车辆数 == 全部车位（充电区 M·总桩 + 等候区 N）。"""
    piles = db.query(ChargingPile).filter(ChargingPile.status != PileStatus.FAULT).all()
    total_capacity = sum(p.queue_capacity for p in piles) + WAITING_AREA_SIZE
    in_system = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.status.in_(
                (
                    RequestStatus.WAITING,
                    RequestStatus.FAULT_QUEUED,
                    RequestStatus.DISPATCHED,
                    RequestStatus.QUEUING_PILE,
                    RequestStatus.CHARGING,
                )
            )
        )
        .count()
    )
    return in_system >= total_capacity


def _dispatch_batch_mixed(db: Session) -> int:
    """spec §8.2：全部车位满 → 一次性把等候区所有车（不分快慢）按
    "总完成时长最短"分配到所有桩（可分配任意类型桩）。桩内 SPT。"""
    piles = db.query(ChargingPile).filter(ChargingPile.status != PileStatus.FAULT).all()
    pile_info = []
    for p in piles:
        used = pile_slot_count(db, p.id)
        free = p.queue_capacity - used
        if free <= 0:
            continue
        pile_info.append({
            "pile": p,
            "free": free,
            "wait_h": pile_queue_wait_hours(db, p),
            "power": p.power_kw,
        })
    if not pile_info:
        return 0
    waiting = (
        db.query(ChargingRequest)
        .filter(ChargingRequest.status == RequestStatus.WAITING)
        .order_by(ChargingRequest.priority_time.asc())
        .all()
    )
    if not waiting:
        return 0
    total_free = sum(p["free"] for p in pile_info)
    K = min(total_free, len(waiting))
    cars = waiting[:K]

    # SPT-LIST 算法，跨模式（spec §8.2 明确"所有车辆均可分配任意类型充电桩"）
    sorted_cars = sorted(cars, key=lambda c: c.target_amount_kwh)
    state = [{"info": p, "wait_h": p["wait_h"], "free": p["free"]} for p in pile_info]
    assignments = []
    pile_order_counter = [0] * len(state)
    for c in sorted_cars:
        cand = [(s["wait_h"], idx) for idx, s in enumerate(state) if s["free"] > 0]
        if not cand:
            break
        _, idx = min(cand)
        own_h = c.target_amount_kwh / state[idx]["info"]["power"]
        state[idx]["wait_h"] += own_h
        state[idx]["free"] -= 1
        assignments.append((c, state[idx]["info"], pile_order_counter[idx]))
        pile_order_counter[idx] += 1

    if not assignments:
        return 0

    count = 0
    now = datetime.utcnow()
    for car, info, order in assignments:
        car.status = RequestStatus.DISPATCHED
        car.assigned_pile_id = info["pile"].id
        car.dispatched_at = now + timedelta(microseconds=order)
        if info["pile"].status == PileStatus.AVAILABLE:
            info["pile"].status = PileStatus.OCCUPIED
        count += 1
    db.flush()
    return count
