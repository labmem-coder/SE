#!/usr/bin/env python3
"""扩展调度（spec §8 选做）测试用例。

测试两个场景：
  §8.1 单次多车总充电时长最短（multi_short）
  §8.2 批量调度（充电区+等候区全满才触发，混合快慢）

每个场景对比两种策略下的派车结果与"所有车 SUM 完成时长"，
扩展策略的 SUM 必须 ≤ normal 策略的 SUM。

运行：
  cd project1/src && source .venv/bin/activate
  python extended_schedule_test.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# ── monkey-patch 时间，与 acceptance_test.py 同方法 ──
SIM_NOW: datetime = datetime(2026, 6, 14, 6, 0, 0)


def _set_now(dt):
    global SIM_NOW
    SIM_NOW = dt


class _FakeDT(datetime):
    @classmethod
    def utcnow(cls):
        return SIM_NOW


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

# 用独立 DB 文件，避免与运行中的 server 进程抢锁
DB_FILE = os.path.join(HERE, "extended_test.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

# 必须在 import app.db 之前 patch config.DATABASE_URL
from app import config as app_config
app_config.DATABASE_URL = f"sqlite:///./{os.path.basename(DB_FILE)}"
# 同时把 sys.modules 里如果已 import 过的 app.db 干掉，确保后续 import 用新 URL
sys.modules.pop("app.db", None)

from app import scheduler as app_sched
from app import fault as app_fault
from app import pricing as app_pricing
from app.routers import user_api as app_user_api
from app.routers import admin_api as app_admin_api

for m in (app_sched, app_fault, app_pricing, app_user_api, app_admin_api):
    setattr(m, "datetime", _FakeDT)

app_config.TIME_ACCELERATION = 1.0
app_sched.TIME_ACCELERATION = 1.0

from app.db import SessionLocal, init_db  # type: ignore  # noqa: E402
from app.models import (
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
from app.auth import hash_password
from app.config import (
    FAST_PILE_COUNT,
    SLOW_PILE_COUNT,
    FAST_PILE_POWER_KW,
    SLOW_PILE_POWER_KW,
    PILE_QUEUE_CAPACITY,
)

init_db()


# ────────────────────────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────────────────────────


def reset_world():
    """删 DB → 重建桩 + admin + 30 辆车。"""
    # 先 dispose engine，关闭所有遗留连接，再删 file
    from app.db import engine as _eng
    _eng.dispose()
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    init_db()
    with SessionLocal() as db:
        for i in range(1, FAST_PILE_COUNT + 1):
            db.add(ChargingPile(
                pile_code=f"F{i}", mode=ChargeMode.FAST,
                power_kw=FAST_PILE_POWER_KW, queue_capacity=PILE_QUEUE_CAPACITY,
            ))
        for i in range(1, SLOW_PILE_COUNT + 1):
            db.add(ChargingPile(
                pile_code=f"T{i}", mode=ChargeMode.SLOW,
                power_kw=SLOW_PILE_POWER_KW, queue_capacity=PILE_QUEUE_CAPACITY,
            ))
        admin = User(
            username="tester", password_hash=hash_password("tester"), is_admin=True,
        )
        db.add(admin)
        db.flush()
        for i in range(1, 31):
            db.add(Vehicle(license_plate=f"V{i}", owner_id=admin.id, battery_capacity_kwh=60.0))
        db.commit()


def submit_waiting(db, plate: str, mode: ChargeMode, target_kwh: float, priority_time: datetime) -> ChargingRequest:
    """直接在 WAITING 状态插一条请求（绕过 try_dispatch，方便后续批量派）。"""
    v = db.query(Vehicle).filter(Vehicle.license_plate == plate).first()
    req = ChargingRequest(
        request_code=f"REQ-{plate}-{priority_time.strftime('%H%M%S')}",
        user_id=1,
        vehicle_id=v.id,
        mode=mode,
        target_amount_kwh=target_kwh,
        status=RequestStatus.WAITING,
        priority_time=priority_time,
        queue_number=plate,
        submitted_at=priority_time,
    )
    db.add(req)
    db.flush()
    return req


def snapshot(db):
    """返回每桩的派车列表：{pile_code: [(plate, target, role)]}, role ∈ {C=充电, Q=排队}"""
    out = {}
    for pile in db.query(ChargingPile).order_by(ChargingPile.id).all():
        cars = []
        rows = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.assigned_pile_id == pile.id,
                ChargingRequest.status.in_(
                    (RequestStatus.DISPATCHED, RequestStatus.QUEUING_PILE, RequestStatus.CHARGING)
                ),
            )
            .order_by(ChargingRequest.dispatched_at.asc())
            .all()
        )
        for r in rows:
            role = "C" if r.status == RequestStatus.CHARGING else "Q"
            cars.append((r.vehicle.license_plate, r.target_amount_kwh, role))
        out[pile.pile_code] = cars
    return out


def sum_finish_hours(state_dict, power_map):
    """给定每桩的派车列表，按入桩顺序累加每辆车的 finish time，并求和。"""
    total = 0.0
    detail = []
    for pile_code, cars in state_dict.items():
        if pile_code not in power_map:
            continue
        cumulative = 0.0
        for plate, target, _ in cars:
            cumulative += target / power_map[pile_code]
            total += cumulative
            detail.append((plate, pile_code, round(cumulative, 3)))
    return round(total, 3), detail


def auto_confirm(db, when):
    rows = db.query(ChargingRequest).filter(
        ChargingRequest.status == RequestStatus.DISPATCHED
    ).all()
    for r in rows:
        r.status = RequestStatus.QUEUING_PILE
        r.confirmed_at = when
        r.pile_queue_arrived_at = when
    db.flush()


# ────────────────────────────────────────────────────────────────────────────
# 测试用例 §8.1
# ────────────────────────────────────────────────────────────────────────────


CASE_8_1_DESC = """
═══════════════════════════════════════════════════════════════════════
  测试用例 §8.1：单次多车总充电时长最短
═══════════════════════════════════════════════════════════════════════
场景设计（同模式：均为快充 30 kW）：

  06:00 V1 提交快充 90 度  ← 大单
  06:01 V2 提交快充 60 度  ← 中单
  06:02 V3 提交快充 30 度  ← 小单
  06:03 V4 提交快充 30 度  ← 小单

  全部进入等候区后，一次性同时派遣（模拟"多个车辆空位"场景）。

期望：
  Normal 策略（按到达顺序 + 桩内 FIFO）—— 大车先入第一个桩，后续小车被卡
  §8.1 策略（SPT 桩内 + 暴力枚举最短 SUM）—— 短车先充，大车独享空桩
"""


def run_case_8_1(policy: str):
    """policy ∈ {'normal', 'multi_short'}"""
    reset_world()
    app_config.EXTENDED_SCHEDULE_POLICY = policy
    app_sched.EXTENDED_SCHEDULE_POLICY = policy

    base = datetime(2026, 6, 14, 6, 0, 0)
    _set_now(base)

    with SessionLocal() as db:
        submit_waiting(db, "V1", ChargeMode.FAST, 90, base)
        submit_waiting(db, "V2", ChargeMode.FAST, 60, base + timedelta(minutes=1))
        submit_waiting(db, "V3", ChargeMode.FAST, 30, base + timedelta(minutes=2))
        submit_waiting(db, "V4", ChargeMode.FAST, 30, base + timedelta(minutes=3))
        db.commit()

        _set_now(base + timedelta(minutes=5))
        app_sched.try_dispatch(db)
        auto_confirm(db, base + timedelta(minutes=5))
        db.commit()

        state = snapshot(db)

    return state


def case_8_1():
    print(CASE_8_1_DESC)
    power_map = {"F1": 30.0, "F2": 30.0}

    print("─── 跑 1：normal 策略（贪心）───")
    s_normal = run_case_8_1("normal")
    for p in ("F1", "F2"):
        items = ["(charging "+c[0]+","+str(c[1])+"度)" if c[2]=="C" else "(queued "+c[0]+","+str(c[1])+"度)" for c in s_normal.get(p, [])]
        print(f"  {p}: {' / '.join(items) or '-'}")
    sum_n, detail_n = sum_finish_hours(s_normal, power_map)
    print(f"  每车 finish (h): {detail_n}")
    print(f"  SUM(finish) = {sum_n} h\n")

    print("─── 跑 2：multi_short 策略（spec §8.1）───")
    s_ext = run_case_8_1("multi_short")
    for p in ("F1", "F2"):
        items = ["(charging "+c[0]+","+str(c[1])+"度)" if c[2]=="C" else "(queued "+c[0]+","+str(c[1])+"度)" for c in s_ext.get(p, [])]
        print(f"  {p}: {' / '.join(items) or '-'}")
    sum_e, detail_e = sum_finish_hours(s_ext, power_map)
    print(f"  每车 finish (h): {detail_e}")
    print(f"  SUM(finish) = {sum_e} h\n")

    saved = round(sum_n - sum_e, 3)
    verdict = "✓ §8.1 优于 normal" if saved > 0.001 else ("≈ 持平" if abs(saved) < 0.001 else "✗ 反常：§8.1 反而更差")
    print(f"  >>> 差距：normal {sum_n}h - §8.1 {sum_e}h = 省时 {saved}h {verdict}")
    return saved > 0.001


# ────────────────────────────────────────────────────────────────────────────
# 测试用例 §8.2
# ────────────────────────────────────────────────────────────────────────────


CASE_8_2_DESC = """
═══════════════════════════════════════════════════════════════════════
  测试用例 §8.2：批量调度（全车位满才触发，混合快慢）
═══════════════════════════════════════════════════════════════════════
场景设计：

  系统容量：5 桩 × M=3 + N=10 = 25 个车位
  策略只在"到达充电站车辆数 == 全车位"时触发批量调度。

  Setup：先填到 25 辆，全部 WAITING，然后触发批量。

  车辆构造（用电量大小差异明显，便于 SPT）：
    V1..V10  小车 10 度
    V11..V15 中车 30 度
    V16..V20 大车 60 度
    V21..V25 巨车 100 度

期望：
  Normal 策略 —— 按模式硬分快/慢，且按到达顺序派
  §8.2 策略 —— 跨模式分配 + SPT 桩内顺序，SUM 应当更小
"""


def run_case_8_2(policy: str):
    """policy ∈ {'normal', 'batch_short'}"""
    reset_world()
    app_config.EXTENDED_SCHEDULE_POLICY = policy
    app_sched.EXTENDED_SCHEDULE_POLICY = policy

    base = datetime(2026, 6, 14, 6, 0, 0)
    _set_now(base)

    with SessionLocal() as db:
        # 25 辆全 WAITING（5 桩 × 3 + 等候区 10 = 25）
        sizes_modes = (
            [("F", 10)] * 5    # V1-V5 小快充
            + [("T", 10)] * 5  # V6-V10 小慢充
            + [("F", 30)] * 3  # V11-V13 中快充
            + [("T", 30)] * 2  # V14-V15 中慢充
            + [("F", 60)] * 3  # V16-V18 大快充
            + [("T", 60)] * 2  # V19-V20 大慢充
            + [("F", 100)] * 3  # V21-V23 巨快充
            + [("T", 100)] * 2  # V24-V25 巨慢充
        )
        for i, (m, kwh) in enumerate(sizes_modes, start=1):
            mode = ChargeMode.FAST if m == "F" else ChargeMode.SLOW
            submit_waiting(
                db, f"V{i}", mode, kwh,
                base + timedelta(minutes=i),
            )
        db.commit()

        _set_now(base + timedelta(hours=1))
        app_sched.try_dispatch(db)
        auto_confirm(db, base + timedelta(hours=1))
        db.commit()

        state = snapshot(db)
        # 统计剩余 WAITING
        leftover = db.query(ChargingRequest).filter(
            ChargingRequest.status == RequestStatus.WAITING
        ).count()
    return state, leftover


def case_8_2():
    print(CASE_8_2_DESC)
    power_map = {"F1": 30.0, "F2": 30.0, "T1": 10.0, "T2": 10.0, "T3": 10.0}

    sums = {}
    for label, policy in [("normal（按模式分）", "normal"), ("batch_short（spec §8.2）", "batch_short")]:
        print(f"─── 跑：{label} ───")
        state, leftover = run_case_8_2(policy)
        for p in ("F1", "F2", "T1", "T2", "T3"):
            items = []
            for plate, target, role in state.get(p, []):
                items.append(f"{plate}({target}度,{role})")
            print(f"  {p}: {', '.join(items) or '-'}")
        s, _ = sum_finish_hours(state, power_map)
        sums[policy] = s
        print(f"  已派 SUM(finish) = {s} h")
        print(f"  剩余 WAITING = {leftover} 辆")
        print()

    saved = round(sums["normal"] - sums["batch_short"], 3)
    verdict = "✓ §8.2 优于 normal" if saved > 0.001 else ("≈ 持平" if abs(saved) < 0.001 else "✗ 反常")
    print(f">>> 差距：normal {sums['normal']}h - §8.2 {sums['batch_short']}h = 省时 {saved}h {verdict}")
    print(">>> 观察：§8.2 跨模式分配 + SPT 桩内顺序，更高效地利用全部 5 个桩。\n")
    return saved > 0.001


# ────────────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "█" * 72)
    print("  扩展调度（spec §8 选做）测试报告")
    print("█" * 72 + "\n")

    ok1 = case_8_1()
    ok2 = case_8_2()

    print("\n" + "═" * 72)
    print(f"  §8.1 结论：{'通过 ✓ multi_short 优于 normal' if ok1 else '失败 ✗'}")
    print(f"  §8.2 结论：{'通过 ✓ batch_short 优于 normal' if ok2 else '失败 ✗'}")
    print("═" * 72 + "\n")
    sys.exit(0 if (ok1 and ok2) else 1)
