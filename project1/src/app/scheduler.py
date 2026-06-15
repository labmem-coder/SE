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

from .clock import get_time
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
    now = now or get_time()
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
        # 优先 pile_queue_arrived_at；当多车在同一虚拟时刻被批量 ConfirmEntry
        # （演示时钟暂停下常见），用 dispatched_at（带微秒）保证派遣顺序 = 桩内 FIFO。
        .order_by(
            ChargingRequest.pile_queue_arrived_at.asc(),
            ChargingRequest.dispatched_at.asc(),
        )
        .first()
    )
    if not next_req:
        return False

    now = get_time()
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
    """5 分钟未 ConfirmEntry 的 DISPATCHED 请求 → 自动取消。

    注意：超时判定使用真实时间（datetime.now），不跟随虚拟时钟，确保
    用户始终有完整的 5 真实分钟来响应叫号。
    """
    now = now or datetime.now()
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
        # 8.2 批量：先把已被纳入"当前批"的计划车按计划顺序滴灌入桩。
        dispatched_total += _drain_planned_batch(db)
        # 触发条件：到站车辆 ≥ 充电区 + 等候区全部车位 → 把尚未规划的等候车
        # 一次性最优规划到全部桩上（无 per-pile 上限），让 ∑Cj 真正含全部 K 辆。
        if _batch_full(db) and _has_unplanned_waiting(db):
            _plan_batch(db)
            dispatched_total += _drain_planned_batch(db)
        # 未达批量触发时维持 normal 兜底，避免空场空桩造成系统空转
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


def _dispatch_one(db: Session, req: ChargingRequest, microsecond_offset: int = 0) -> bool:
    """把单个请求派到该模式下"完成时间最短"的桩。成功返回 True。

    microsecond_offset：在同一 tick 内派多辆车时，传递自增偏移，保证 dispatched_at
    严格递增 —— 桩内显示/调度 FIFO 才会与 priority_time 顺序一致。
    """
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
    # dispatched_at 使用真实时间，因为超时检查（handle_dispatch_timeouts）
    # 基于真实 wall-clock 时间，确保用户有完整 5 分钟响应窗口
    now = datetime.now() + timedelta(microseconds=microsecond_offset)
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
       Phase 2 等候区：仅在故障队列被全部排空后，才调度普通等候区。

       dispatched_at 在同一 tick 内按派出顺序微秒级递增，确保桩内 FIFO 与
       spec §7.2 "按排队号码先后顺序" 一致。"""
    count = 0
    offset = 0  # 同 tick 内微秒级递增
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
        if not _dispatch_one(db, req, microsecond_offset=offset):
            # 故障队列还有车未派出去 → spec 要求暂停等候区调度
            return count
        offset += 1
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
        if not _dispatch_one(db, req, microsecond_offset=offset):
            return count
        offset += 1
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
# 扩展调度 §8.1 / §8.2 —— 精确最优 ∑Cj
#
# 把"指派 K 辆车到 m 台桩"等价为：
#   - 每台桩 i (功率 s_i, 当前剩余排队负载 L_i, 空位 free_i)
#     若分到 n_i 辆车，新到的 n_i 辆按 SPT 排（小电量在前），
#     则第 k 辆（位置 1..n_i）的完工时刻 = L_i + (1/s_i) * Σ_{j≤k} p_(j)
#   - 桩 i 上总 ΣCj = n_i * L_i + (1/s_i) * Σ_{k} (n_i - k + 1) * p_(k)
#     = n_i * L_i + (1/s_i) * Σ slot_weight(e) * p_paired，其中 e=1..n_i
# 全局：枚举所有合法 (n_1,...,n_m)，重排不等式配对 → 取最小总和。
# 同等机器 (§8.1 同 mode) 是 P||ΣCj，匀速机器 (§8.2 跨 mode) 是 Q||ΣCj，
# 这套精确算法都是最优解。
# ────────────────────────────────────────────────────────────────────────────


def _eligible_piles(db: Session, mode: Optional[ChargeMode] = None):
    """返回非故障桩的 {pile, free, wait_h, power}（free > 0）。

    mode=None 时跨模式取所有桩（§8.2 用）。
    """
    q = db.query(ChargingPile).filter(ChargingPile.status != PileStatus.FAULT)
    if mode is not None:
        q = q.filter(ChargingPile.mode == mode)
    info = []
    for p in q.all():
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


def _enumerate_partitions(free_caps: list[int], K: int):
    """枚举 sum=K, 0 ≤ n_i ≤ free_caps[i] 的 (n_1,...,n_m)。"""
    m = len(free_caps)

    def rec(idx: int, remaining: int, current: list[int]):
        if idx == m:
            if remaining == 0:
                yield tuple(current)
            return
        upper = min(free_caps[idx], remaining)
        # 剩余项必须 ≥ remaining - sum(free_caps[idx+1:])
        rest_cap = sum(free_caps[idx + 1:])
        lower = max(0, remaining - rest_cap)
        for n in range(lower, upper + 1):
            current.append(n)
            yield from rec(idx + 1, remaining - n, current)
            current.pop()

    yield from rec(0, K, [])


def _optimal_assignment(pile_info: list[dict], cars: list[ChargingRequest]):
    """对给定的 K 辆车（cars 已按 priority_time 排过）做最优分桩。

    返回 [(car, pile_info_entry, position_from_start_0idx), ...]。
    """
    if not cars or not pile_info:
        return []
    total_free = sum(p["free"] for p in pile_info)
    K = min(len(cars), total_free)
    if K == 0:
        return []
    selected = cars[:K]
    # 按 p 升序，但保留对原 car 的引用（用于回填 assigned_pile_id 等）
    p_sorted = sorted([(c.target_amount_kwh, c) for c in selected], key=lambda x: x[0])

    m = len(pile_info)
    free_caps = [p["free"] for p in pile_info]
    L_list = [p["wait_h"] for p in pile_info]
    s_list = [p["power"] for p in pile_info]

    best_cost = float("inf")
    best_partition: Optional[tuple[int, ...]] = None
    best_slots: Optional[list[tuple[float, int, int]]] = None

    for partition in _enumerate_partitions(free_caps, K):
        # 生成 slot (multiplier, machine_idx, e)
        slots = []
        for i, n in enumerate(partition):
            inv_s = 1.0 / s_list[i]
            for e in range(1, n + 1):
                slots.append((e * inv_s, i, e))
        # 按 multiplier 降序，与 p 升序配对（重排不等式 → ∑mp 最小）
        slots.sort(key=lambda x: -x[0])
        pair_cost = sum(slots[k][0] * p_sorted[k][0] for k in range(K))
        overhead = sum(partition[i] * L_list[i] for i in range(m))
        cost = pair_cost + overhead
        if cost < best_cost:
            best_cost = cost
            best_partition = partition
            best_slots = slots

    if best_slots is None:
        return []

    # 按 best_slots 顺序恢复 (car -> machine, e)；同机内 e 大者居前（SPT）
    per_machine: dict[int, list[tuple[ChargingRequest, int]]] = {i: [] for i in range(m)}
    for k in range(K):
        _mult, i, e = best_slots[k]
        per_machine[i].append((p_sorted[k][1], e))

    assignments: list[tuple[ChargingRequest, dict, int]] = []
    for i, lst in per_machine.items():
        lst.sort(key=lambda x: -x[1])  # e 降序 = 从前往后充电
        for pos, (car, _e) in enumerate(lst):
            assignments.append((car, pile_info[i], pos))
    return assignments


def _apply_assignments(db: Session, assignments) -> int:
    """统一回填 DISPATCHED 状态 + 微秒级 dispatched_at 单调递增。"""
    if not assignments:
        return 0
    now = datetime.now()
    # 同机内按 position 排序保证微秒递增 = 桩内 FIFO
    by_pile: dict[int, list] = {}
    for car, info, pos in assignments:
        by_pile.setdefault(info["pile"].id, []).append((car, info, pos))
    count = 0
    offset = 0
    for pile_id, items in by_pile.items():
        items.sort(key=lambda x: x[2])  # position asc
        for car, info, _pos in items:
            car.status = RequestStatus.DISPATCHED
            car.assigned_pile_id = info["pile"].id
            car.dispatched_at = now + timedelta(microseconds=offset)
            if info["pile"].status == PileStatus.AVAILABLE:
                info["pile"].status = PileStatus.OCCUPIED
            offset += 1
            count += 1
    db.flush()
    return count


def _dispatch_mode_multi_short(db: Session, mode: ChargeMode) -> int:
    """spec §8.1：同模式多车一次性最优派遣。FAULT_QUEUED 仍单独按优先级派。"""
    count = 0
    # Phase 1: 故障队列保持最高优先级，逐辆派
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
        if not _dispatch_one(db, req, microsecond_offset=count):
            return count
        count += 1

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
    assignments = _optimal_assignment(pile_info, waiting)
    count += _apply_assignments(db, assignments)
    return count


# ────────────────────────────────────────────────────────────────────────────
# §8.2 批量调度（全车位满才触发，跨模式）
# ────────────────────────────────────────────────────────────────────────────


def _batch_full(db: Session) -> bool:
    """spec §8.2 触发条件：在系统中的车辆 ≥ 全部车位（充电区 M·总桩 + 等候区 N）。"""
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


def _has_unplanned_waiting(db: Session) -> bool:
    return (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.status == RequestStatus.WAITING,
            ChargingRequest.batch_plan_order.is_(None),
        )
        .first()
        is not None
    )


def _eligible_piles_unbounded(db: Session, K: int):
    """像 _eligible_piles 一样，但每桩的容量上限放宽到 K：
       桩内 FIFO 的物理容量只在 drain 时受限，规划阶段允许给桩排进任意多辆车。"""
    info = []
    for p in (
        db.query(ChargingPile)
        .filter(ChargingPile.status != PileStatus.FAULT)
        .order_by(ChargingPile.id)
        .all()
    ):
        info.append({
            "pile": p,
            "free": K,
            "wait_h": pile_queue_wait_hours(db, p),
            "power": p.power_kw,
        })
    return info


def _plan_batch(db: Session) -> int:
    """§8.2：把当前所有未规划的 WAITING 车一次性最优地分配到全部桩，
       写入 (assigned_pile_id, batch_plan_order)。本身不改变 status。

       规划目标：全部 K 辆车的 ∑Cj 最小（含尚未进入 pile FIFO 的"队尾"）。
       桩内 SPT、桩间重排不等式、partition 暴搜 → P/Q‖∑Cj 精确最优。"""
    waiting = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.status == RequestStatus.WAITING,
            ChargingRequest.batch_plan_order.is_(None),
        )
        .order_by(ChargingRequest.priority_time.asc())
        .all()
    )
    if not waiting:
        return 0
    pile_info = _eligible_piles_unbounded(db, K=len(waiting))
    if not pile_info:
        return 0
    assignments = _optimal_assignment(pile_info, waiting)
    for car, info, pos in assignments:
        car.assigned_pile_id = info["pile"].id
        car.batch_plan_order = pos
    db.flush()
    return len(assignments)


def _drain_planned_batch(db: Session) -> int:
    """§8.2：把已被 _plan_batch 写好计划的 WAITING 车按计划顺序灌进 DISPATCHED，
       gated by 桩 FIFO 容量。每桩一次只灌满容量内可容纳的若干辆。

       会话 autoflush=False，故每桩只查一次 planned 列表，按容量切片取头部。"""
    count = 0
    offset = 0
    now = datetime.now()
    for pile in (
        db.query(ChargingPile)
        .filter(ChargingPile.status != PileStatus.FAULT)
        .order_by(ChargingPile.id)
        .all()
    ):
        used = pile_slot_count(db, pile.id)
        free = pile.queue_capacity - used
        if free <= 0:
            continue
        planned = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.status == RequestStatus.WAITING,
                ChargingRequest.assigned_pile_id == pile.id,
                ChargingRequest.batch_plan_order.is_not(None),
            )
            .order_by(ChargingRequest.batch_plan_order.asc())
            .limit(free)
            .all()
        )
        for req in planned:
            req.status = RequestStatus.DISPATCHED
            req.dispatched_at = now + timedelta(microseconds=offset)
            if pile.status == PileStatus.AVAILABLE:
                pile.status = PileStatus.OCCUPIED
            offset += 1
            count += 1
    if count:
        db.flush()
    return count
