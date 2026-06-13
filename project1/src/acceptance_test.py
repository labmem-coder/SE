#!/usr/bin/env python3
"""验收测试 —— 按 测试用例.csv 模拟全部事件，在每个5分钟时间点输出状态。

特性：
- 直接调用 scheduler / fault 业务函数（绕过 HTTP）
- monkey-patch `datetime.utcnow()`，让模拟时间可控
- TIME_ACCELERATION 设为 1.0，因为我们直接控制时钟
- 提交请求后自动 ConfirmEntry（验收用例不模拟 5 分钟超时）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Optional

# ─── 必须先 monkey-patch 时间，再 import 任何 app 模块 ───
SIM_NOW: datetime = datetime(2026, 6, 9, 6, 0, 0)


def _set_now(dt: datetime) -> None:
    global SIM_NOW
    SIM_NOW = dt


class _FakeDateTime(datetime):
    @classmethod
    def utcnow(cls):
        return SIM_NOW


# 把 sys.path 指向 src 根，以便能 `from app...` import
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 重置 DB 文件
DB_FILE = os.path.join(HERE, "charging_station.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

# 现在 import 全部 app 模块并把 datetime 替换为 _FakeDateTime
from app import scheduler as app_sched
from app import fault as app_fault
from app import pricing as app_pricing
from app import config as app_config
from app.routers import user_api as app_user_api
from app.routers import admin_api as app_admin_api

for mod in (app_sched, app_fault, app_pricing, app_user_api, app_admin_api):
    setattr(mod, "datetime", _FakeDateTime)

# 模拟时直接控制时钟，无需加速
app_config.TIME_ACCELERATION = 1.0
app_sched.TIME_ACCELERATION = 1.0

# 重置 DB（确保 init_db 用新参数）
from app.db import SessionLocal, init_db
from app.auth import hash_password
from app.config import (
    FAST_PILE_COUNT,
    SLOW_PILE_COUNT,
    FAST_PILE_POWER_KW,
    SLOW_PILE_POWER_KW,
    PILE_QUEUE_CAPACITY,
    SERVICE_FEE_YUAN_PER_KWH,
    PRICING_SCHEDULE,
    WAITING_AREA_SIZE,
)
from app.models import (
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

init_db()


# ────────────────────────────────────────────────────────────────────────────
# 初始化：充电桩 + 1 个管理员用户 + V1..V30 车辆
# ────────────────────────────────────────────────────────────────────────────

def setup_world():
    with SessionLocal() as db:
        for i in range(1, FAST_PILE_COUNT + 1):
            db.add(
                ChargingPile(
                    pile_code=f"F{i}",
                    mode=ChargeMode.FAST,
                    power_kw=FAST_PILE_POWER_KW,
                    queue_capacity=PILE_QUEUE_CAPACITY,
                )
            )
        for i in range(1, SLOW_PILE_COUNT + 1):
            db.add(
                ChargingPile(
                    pile_code=f"S{i}",
                    mode=ChargeMode.SLOW,
                    power_kw=SLOW_PILE_POWER_KW,
                    queue_capacity=PILE_QUEUE_CAPACITY,
                )
            )

        admin = User(
            username="tester",
            password_hash=hash_password("tester"),
            is_admin=True,
        )
        db.add(admin)
        db.flush()

        for i in range(1, 31):
            db.add(Vehicle(license_plate=f"V{i}", owner_id=admin.id))
        db.commit()


# ────────────────────────────────────────────────────────────────────────────
# 事件处理
# ────────────────────────────────────────────────────────────────────────────

def _vehicle_id(db, code: str) -> int:
    v = db.query(Vehicle).filter(Vehicle.license_plate == code).first()
    return v.id


def _pile_id(db, code: str) -> int:
    # 测试用例使用 T 前缀代表慢充；本仓库当前 seed 用 S 前缀
    candidates = [code]
    if code.startswith("T"):
        candidates.append("S" + code[1:])
    elif code.startswith("S"):
        candidates.append("T" + code[1:])
    p = (
        db.query(ChargingPile)
        .filter(ChargingPile.pile_code.in_(candidates))
        .first()
    )
    if p is None:
        raise ValueError(f"pile {code} not found (tried {candidates})")
    return p.id


def _auto_confirm_dispatched(db, when: datetime) -> None:
    """验收测试中所有 DISPATCHED 立刻 ConfirmEntry。"""
    dispatched = (
        db.query(ChargingRequest)
        .filter(ChargingRequest.status == RequestStatus.DISPATCHED)
        .all()
    )
    if not dispatched:
        return
    for req in dispatched:
        req.status = RequestStatus.QUEUING_PILE
        req.confirmed_at = when
        req.pile_queue_arrived_at = when
    db.flush()
    app_sched.try_dispatch(db)


def _active_request_for_vehicle(db, vehicle_id: int) -> Optional[ChargingRequest]:
    return (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.vehicle_id == vehicle_id,
            ChargingRequest.status.in_(
                (
                    RequestStatus.WAITING,
                    RequestStatus.FAULT_QUEUED,
                    RequestStatus.DISPATCHED,
                    RequestStatus.QUEUING_PILE,
                    RequestStatus.CHARGING,
                )
            ),
        )
        .first()
    )


def apply_event(when: datetime, ev_type: str, obj: str, mode_op: str, value: float) -> str:
    """返回事件标签后缀，例如 '' 表示成功，'REJECTED' 表示被拒收。"""
    _set_now(when)
    with SessionLocal() as db:
        # 关键：在处理事件之前先把"已经自然发生"的状态推进 + 调度完。
        # 比如 V5 在 08:25:00 自然充满 → V8 接上 → V25 应入 T2。
        # 这样后续的 T1 故障事件再发生时，V1 已经抢不到 T2 的空位
        # （符合 spec "故障发生后才停止等候区调度"）。
        app_sched.try_dispatch(db)
        _auto_confirm_dispatched(db, when)

        if ev_type == "A":
            vehicle_id = _vehicle_id(db, obj)
            if value == 0:
                # 取消
                existing = _active_request_for_vehicle(db, vehicle_id)
                if existing is None:
                    db.commit()
                    return
                affected_pile_id = existing.assigned_pile_id
                # 若正在充电：结算并出账单
                if existing.status == RequestStatus.CHARGING and existing.session is not None:
                    sess = existing.session
                    sess.status = SessionStatus.COMPLETED
                    sess.ended_at = when
                    if sess.charged_kwh > 0:
                        bill = app_pricing.generate_bill(db, sess)
                        pile = sess.pile
                        pile.total_sessions += 1
                        pile.total_charged_kwh = round(pile.total_charged_kwh + sess.charged_kwh, 4)
                        pile.total_revenue = round(pile.total_revenue + bill.total_amount, 2)
                existing.status = RequestStatus.CANCELLED
                existing.cancelled_at = when
                existing.assigned_pile_id = None
                existing.pile_queue_arrived_at = None
                db.flush()
                if affected_pile_id is not None:
                    pile = db.get(ChargingPile, affected_pile_id)
                    if pile is not None:
                        app_sched._refresh_pile_status(db, pile)
                app_sched.try_dispatch(db)
                _auto_confirm_dispatched(db, when)
                db.commit()
                return ""

            # 新请求：先尝试将已有等候车派出去，再判断等候区是否已满
            app_sched.try_dispatch(db)
            # 验收测试中假设 ConfirmEntry 立刻发生（无 5 分钟超时风险）
            _auto_confirm_dispatched(db, when)
            # N 校验只对 WAITING 计数；FAULT_QUEUED 属于"损坏桩队列"，
            # 按 spec 不占等候区名额。
            current_waiting = (
                db.query(ChargingRequest)
                .filter(ChargingRequest.status == RequestStatus.WAITING)
                .count()
            )
            if current_waiting >= WAITING_AREA_SIZE:
                db.commit()
                return "REJECTED"

            chmode = ChargeMode.FAST if mode_op == "F" else ChargeMode.SLOW
            req = ChargingRequest(
                request_code=f"REQ-{obj}-{when.strftime('%H%M%S')}",
                user_id=1,
                vehicle_id=vehicle_id,
                mode=chmode,
                target_amount_kwh=float(value),
                status=RequestStatus.WAITING,
                priority_time=when,
                queue_number=obj,
                submitted_at=when,
            )
            db.add(req)
            db.flush()
            app_sched.try_dispatch(db)
            _auto_confirm_dispatched(db, when)
            db.commit()
            return ""

        elif ev_type == "C":
            vehicle_id = _vehicle_id(db, obj)
            existing = _active_request_for_vehicle(db, vehicle_id)
            if existing is None:
                db.commit()
                return "NO_REQ"

            new_mode = None
            if mode_op == "F":
                new_mode = ChargeMode.FAST
            elif mode_op == "T":
                new_mode = ChargeMode.SLOW
            # "O" = 不变

            new_amount = (
                float(value)
                if (value is not None and value != -1)
                else existing.target_amount_kwh
            )

            mode_changed = new_mode is not None and new_mode != existing.mode

            if mode_changed:
                affected_pile_id = existing.assigned_pile_id
                existing.status = RequestStatus.CANCELLED
                existing.cancelled_at = when
                existing.assigned_pile_id = None
                existing.pile_queue_arrived_at = None
                db.flush()
                new_req = ChargingRequest(
                    request_code=f"REQ-{obj}-{when.strftime('%H%M%S')}",
                    user_id=1,
                    vehicle_id=vehicle_id,
                    mode=new_mode,
                    target_amount_kwh=new_amount,
                    status=RequestStatus.WAITING,
                    priority_time=when,
                    queue_number=obj,
                    submitted_at=when,
                )
                db.add(new_req)
                db.flush()
                if affected_pile_id is not None:
                    pile = db.get(ChargingPile, affected_pile_id)
                    if pile is not None:
                        app_sched._refresh_pile_status(db, pile)
                app_sched.try_dispatch(db)
                _auto_confirm_dispatched(db, when)
            else:
                # 仅改电量
                if existing.status == RequestStatus.CHARGING and existing.session is not None:
                    # 改正在充电的 session 目标
                    existing.session.target_kwh = new_amount
                    if existing.session.charged_kwh + 1e-9 >= new_amount:
                        # 已经超过新目标 → 直接完成
                        app_sched.handle_completed_sessions(db)
                existing.target_amount_kwh = new_amount
                app_sched.try_dispatch(db)
                _auto_confirm_dispatched(db, when)
            db.commit()
            return ""

        elif ev_type == "B":
            pid = _pile_id(db, obj)
            if value == 0:
                # 故障
                app_fault.confirm_pile_fault(
                    db,
                    pile_id=pid,
                    fault_type="acceptance-test",
                    fault_time=when,
                    source_report_id=None,
                    admin_user_id=1,
                )
                _auto_confirm_dispatched(db, when)
                db.commit()
                return ""
            else:
                # 恢复
                app_fault.resume_pile(db, pid)
                _auto_confirm_dispatched(db, when)
                db.commit()
                return ""

        # 取消分支已 return；其它分支默认成功
        return ""


# ────────────────────────────────────────────────────────────────────────────
# 状态渲染
# ────────────────────────────────────────────────────────────────────────────


def _current_fee_of_session(sess: ChargingSession) -> float:
    cf = app_pricing.calculate_charging_fee(
        sess.started_at, sess.charged_kwh, sess.power_kw
    )
    sf = app_pricing.calculate_service_fee(sess.charged_kwh)
    return round(cf + sf, 2)


def _pile_cars(db, pile: ChargingPile, when: datetime):
    reqs = (
        db.query(ChargingRequest)
        .filter(
            ChargingRequest.assigned_pile_id == pile.id,
            ChargingRequest.status.in_(
                (RequestStatus.QUEUING_PILE, RequestStatus.CHARGING)
            ),
        )
        .all()
    )

    def sort_key(r: ChargingRequest):
        charging_flag = 0 if r.status == RequestStatus.CHARGING else 1
        arrived = r.pile_queue_arrived_at or r.dispatched_at or r.submitted_at
        return (charging_flag, arrived)

    reqs.sort(key=sort_key)
    out = []
    for r in reqs:
        plate = r.vehicle.license_plate
        if r.status == RequestStatus.CHARGING and r.session is not None:
            kwh = round(r.session.charged_kwh, 2)
            fee = _current_fee_of_session(r.session)
            out.append(f"({plate},{kwh:.2f},{fee:.2f})")
        else:
            out.append(f"({plate},0.00,0.00)")
    return out


def _waiting_area(db):
    """只显示真正的等候区（WAITING）。
    FAULT_QUEUED 属于"损坏桩队列"，不计入 N=10，也不显示在等候区列。"""
    rows = (
        db.query(ChargingRequest)
        .filter(ChargingRequest.status == RequestStatus.WAITING)
        .order_by(ChargingRequest.priority_time.asc(), ChargingRequest.id.asc())
        .all()
    )
    out = []
    for r in rows:
        m = "F" if r.mode == ChargeMode.FAST else "T"
        out.append(f"({r.vehicle.license_plate},{m},{r.target_amount_kwh:.2f})")
    return out


def _fault_queue(db):
    """损坏桩队列：故障腾出尚未派入其他桩的车。仅用于诊断打印，不入 CSV 主表。"""
    rows = (
        db.query(ChargingRequest)
        .filter(ChargingRequest.status == RequestStatus.FAULT_QUEUED)
        .order_by(ChargingRequest.priority_time.asc(), ChargingRequest.id.asc())
        .all()
    )
    out = []
    for r in rows:
        m = "F" if r.mode == ChargeMode.FAST else "T"
        out.append(f"({r.vehicle.license_plate},{m},{r.target_amount_kwh:.2f})")
    return out


def render_state(when: datetime):
    _set_now(when)
    with SessionLocal() as db:
        app_sched.advance_active_sessions(db, when)
        app_sched.handle_completed_sessions(db)
        # 防御性：再确认一遍所有 DISPATCHED（验收测试假定立刻 ConfirmEntry）
        _auto_confirm_dispatched(db, when)
        db.commit()

    with SessionLocal() as db:
        piles = (
            db.query(ChargingPile).order_by(ChargingPile.pile_code.asc()).all()
        )
        # 顺序：F1, F2, T1, T2, T3
        ordered = {p.pile_code: _pile_cars(db, p, when) for p in piles}
        waiting = _waiting_area(db)
        fault_q = _fault_queue(db)
    return ordered, waiting, fault_q


# ────────────────────────────────────────────────────────────────────────────
# 事件列表（来自 测试用例.csv）
# ────────────────────────────────────────────────────────────────────────────

# (time_str, type, target_code, mode_or_op, value)
EVENTS = [
    ("06:00", "A", "V1", "T", 40),
    ("06:05", "A", "V2", "T", 30),
    ("06:10", "A", "V3", "F", 100),
    ("06:15", "A", "V4", "F", 120),
    ("06:20", "A", "V2", "O", 0),   # cancel V2
    ("06:25", "A", "V5", "T", 20),
    ("06:30", "A", "V6", "T", 20),
    ("06:35", "A", "V7", "F", 110),
    ("06:40", "A", "V8", "T", 20),
    ("06:45", "A", "V9", "F", 105),
    ("06:50", "A", "V10", "T", 10),
    ("06:55", "A", "V11", "F", 110),
    ("07:00", "A", "V12", "F", 90),
    ("07:05", "A", "V13", "F", 110),
    ("07:10", "A", "V14", "F", 95),
    ("07:15", "A", "V15", "T", 10),
    ("07:20", "A", "V16", "F", 60),
    ("07:25", "A", "V17", "T", 10),
    ("07:30", "A", "V18", "T", 7.5),
    ("07:35", "A", "V19", "F", 75),
    ("07:40", "A", "V20", "F", 95),
    ("07:45", "A", "V21", "F", 95),
    ("07:50", "A", "V22", "F", 70),
    ("07:55", "A", "V23", "F", 80),
    ("08:00", "A", "V24", "T", 5),
    ("08:20", "A", "V25", "T", 15),
    ("08:25", "B", "T1", "O", 0),     # T1 fault
    ("08:30", "A", "V26", "T", 20),
    ("08:35", "A", "V27", "T", 25),
    ("08:50", "B", "F1", "O", 0),     # F1 fault
    ("09:00", "A", "V28", "F", 30),
    ("09:10", "A", "V1", "O", 0),     # cancel V1
    ("09:15", "B", "T1", "O", 1),     # T1 resume
    ("09:20", "A", "V27", "O", 0),    # cancel V27
    ("09:25", "C", "V21", "O", 35),   # change V21 amount to 35
    ("09:30", "A", "V19", "O", 0),    # cancel V19
]


def _parse_time(hhmm: str, base: datetime) -> datetime:
    h, m = hhmm.split(":")
    return base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def _cell_rows(items, max_rows=3):
    """把车辆列表分配到 max_rows 行；不足填 -。"""
    out = items[:max_rows] + ["-"] * (max_rows - len(items[:max_rows]))
    return out


def _wait_rows(items, max_rows=3):
    """等候区放一行用 - 连接；超过 max_rows 行的也合并。"""
    rows = ["-"] * max_rows
    if items:
        rows[0] = "-".join(items)
    return rows


def _normalize_cell(s: str) -> str:
    """忽略空格/分隔符差异（- vs /）后比较单元格。"""
    if s is None:
        return ""
    s = s.strip().strip('"').replace(" ", "")
    if s == "-" or s == "":
        return ""
    return s.replace("/", "-")


# ────────────────────────────────────────────────────────────────────────────
# 期望样本（直接来自 测试用例.csv 中已经填写好的格子）
# 元组：(时刻, 列索引 0=F1 1=F2 2=T1 3=T2 4=T3 5=等候区, 行内偏移, 期望值)
# ────────────────────────────────────────────────────────────────────────────

EXPECTED_SAMPLES = [
    ("06:00", 2, 0, "(V1,0.00,0.00)"),   # 慢充1 第一行
    ("06:05", 2, 0, "(V1,0.83,1.00)"),
    ("06:05", 3, 0, "(V2,0.00,0.00)"),
    ("07:05", 5, 0, "(V13,F,110.00)"),
    ("07:10", 5, 0, "(V13,F,110.00)-(V14,F,95.00)"),
    ("07:15", 5, 0, "(V13,F,110.00)-(V14,F,95.00)"),
]


def main():
    setup_world()

    # 通过环境变量切换故障调度策略；同时影响输出文件名后缀
    fault_policy = os.environ.get("FAULT_POLICY", app_config.FAULT_DISPATCH_POLICY)
    app_config.FAULT_DISPATCH_POLICY = fault_policy
    # 同步给已 import 的 fault 模块（属于"运行时" override）
    app_fault.FAULT_DISPATCH_POLICY = fault_policy

    suffix_map = {
        "priority":   "_优先级调度",
        "time_order": "_时间顺序调度",
    }
    suffix = suffix_map.get(fault_policy, f"_{fault_policy}")

    base_day = datetime(2026, 6, 9, 0, 0, 0)
    event_by_time = {_parse_time(e[0], base_day): e for e in EVENTS}

    # 覆盖 06:00 ~ 09:30，每 5 分钟一格
    timepoints = []
    t = base_day.replace(hour=6, minute=0)
    end = base_day.replace(hour=9, minute=30)
    while t <= end:
        timepoints.append(t)
        t += timedelta(minutes=5)

    print("=== 验收测试模拟（参数：快充 {fp}kW×{fc}，慢充 {sp}kW×{sc}，M={m}，故障策略={pol}） ===".format(
        fp=FAST_PILE_POWER_KW, fc=FAST_PILE_COUNT,
        sp=SLOW_PILE_POWER_KW, sc=SLOW_PILE_COUNT,
        m=PILE_QUEUE_CAPACITY,
        pol=fault_policy,
    ))

    # 记录每个时间点每列各行的值，便于后续校验
    table: dict[str, list[list[str]]] = {}  # tp_str -> 6 列 × 3 行

    # 输出文件按策略命名
    csv_targets = [
        os.path.abspath(os.path.join(HERE, "..", "..", f"测试结果{suffix}.csv")),
        os.path.abspath(os.path.join(HERE, "..", "docs", f"acceptance_test_output{suffix}.csv")),
    ]
    fcsvs = [open(p, "w", encoding="utf-8") for p in csv_targets]

    for fcsv in fcsvs:
        fcsv.write(',,"(车号,已充电量,当前费用)",,,,,"(车号,充电类型,充电量)"\n')
        fcsv.write("时刻,事件,快充1,快充2,慢充1,慢充2,慢充3,等候区(10辆)\n")

    header = f"{'时刻':<8}{'事件':<18}{'F1':<32}{'F2':<32}{'T1':<32}{'T2':<32}{'T3':<32}{'等候区'}"
    print(header)
    print("-" * len(header))

    rejected_log: list[tuple[str, str]] = []

    for tp in timepoints:
        ev = event_by_time.get(tp)
        if ev is not None:
            _, ev_type, obj, mode_op, value = ev
            outcome = apply_event(tp, ev_type, obj, mode_op, value)
            ev_label = f"({ev_type},{obj},{mode_op},{value})"
            if outcome == "REJECTED":
                ev_label += " [拒收:等候区满]"
                rejected_log.append((tp.strftime("%H:%M"), f"({ev_type},{obj},{mode_op},{value})"))
            elif outcome == "NO_REQ":
                ev_label += " [无效:车未在系统]"
                rejected_log.append((tp.strftime("%H:%M"), f"({ev_type},{obj},{mode_op},{value})"))
        else:
            ev_label = ""

        piles_state, waiting, fault_q = render_state(tp)
        # 兼容 seed 用 S 前缀 vs 测试报表用 T 前缀
        def _col(code):
            return piles_state.get(code) or piles_state.get(code.replace("T", "S")) or []
        cells_per_col = [
            _cell_rows(_col("F1")),
            _cell_rows(_col("F2")),
            _cell_rows(_col("T1")),
            _cell_rows(_col("T2")),
            _cell_rows(_col("T3")),
            _wait_rows(waiting),
        ]
        table[tp.strftime("%H:%M")] = cells_per_col

        # 屏幕版（合成单行）
        fmt = lambda lst: "/".join([x for x in lst if x != "-"]) or "-"
        wait_str = " - ".join(waiting) if waiting else "-"
        if fault_q:
            wait_str += f"   [故障队列: {' - '.join(fault_q)}]"
        print(
            f"{tp.strftime('%H:%M'):<8}{ev_label or '-':<18}"
            f"{fmt(cells_per_col[0]):<32}{fmt(cells_per_col[1]):<32}"
            f"{fmt(cells_per_col[2]):<32}{fmt(cells_per_col[3]):<32}{fmt(cells_per_col[4]):<32}"
            f"{wait_str}"
        )

        # CSV 版（3 行/时间点，匹配原表）
        for row_i in range(3):
            time_col = tp.strftime("%H:%M:%S") if row_i == 0 else ""
            event_col = f'"{ev_label}"' if (row_i == 0 and ev_label) else ""
            row_str = ",".join([
                time_col,
                event_col,
                f'"{cells_per_col[0][row_i]}"' if cells_per_col[0][row_i] != "-" else "-",
                f'"{cells_per_col[1][row_i]}"' if cells_per_col[1][row_i] != "-" else "-",
                f'"{cells_per_col[2][row_i]}"' if cells_per_col[2][row_i] != "-" else "-",
                f'"{cells_per_col[3][row_i]}"' if cells_per_col[3][row_i] != "-" else "-",
                f'"{cells_per_col[4][row_i]}"' if cells_per_col[4][row_i] != "-" else "-",
                f'"{cells_per_col[5][row_i]}"' if cells_per_col[5][row_i] != "-" else "",
            ]) + "\n"
            for fcsv in fcsvs:
                fcsv.write(row_str)

    for fcsv in fcsvs:
        fcsv.close()

    # ── 期望样本校验 ──
    col_names = ["快充1", "快充2", "慢充1", "慢充2", "慢充3", "等候区"]
    print("\n=== 期望样本校验（来自 测试用例.csv 已填字段）===")
    passed = 0
    failed = 0
    for tp_str, col, row, expected in EXPECTED_SAMPLES:
        actual = table[tp_str][col][row]
        if _normalize_cell(actual) == _normalize_cell(expected):
            verdict = "PASS"
            passed += 1
        else:
            verdict = "FAIL"
            failed += 1
        print(
            f"  [{verdict}] {tp_str} {col_names[col]}#{row+1}: "
            f"期望={expected}  实际={actual}"
        )

    print(f"\n结果：{passed} 通过 / {failed} 失败 / 共 {passed+failed} 项")
    if rejected_log:
        print(f"\n=== 因等候区已满（N={WAITING_AREA_SIZE}）被拒收/无效的事件 ===")
        for t, ev in rejected_log:
            print(f"  {t}  {ev}")
    print("\nCSV 输出：")
    for p in csv_targets:
        print(f"  - {p}")


if __name__ == "__main__":
    main()
