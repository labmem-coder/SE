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
from app import clock as app_clock
from app import models as app_models
from app.routers import user_api as app_user_api
from app.routers import admin_api as app_admin_api

for m in (app_sched, app_fault, app_pricing, app_user_api, app_admin_api):
    setattr(m, "datetime", _FakeDT)


# 同时把 clock.get_time() 也劫持为 SIM_NOW，让 session.last_tick_at 跟着模拟时间走
def _fake_get_time():
    return SIM_NOW


setattr(app_clock, "get_time", _fake_get_time)
for m in (app_sched, app_fault, app_pricing, app_user_api, app_admin_api, app_models):
    if hasattr(m, "get_time"):
        setattr(m, "get_time", _fake_get_time)

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


def full_plan_snapshot(db):
    """§8.2 专用：把全部 batch_plan_order != NULL 的车按 (assigned_pile_id, batch_plan_order)
       排好，返回 {pile_code: [(plate, kwh), ...]}。包含 WAITING 也包含已派出去的。"""
    out = {}
    for pile in db.query(ChargingPile).order_by(ChargingPile.id).all():
        rows = (
            db.query(ChargingRequest)
            .filter(
                ChargingRequest.assigned_pile_id == pile.id,
                ChargingRequest.batch_plan_order.is_not(None),
            )
            .order_by(ChargingRequest.batch_plan_order.asc())
            .all()
        )
        out[pile.pile_code] = [(r.vehicle.license_plate, r.target_amount_kwh) for r in rows]
    return out


def normal_full_simulate(initial_cars, fast_piles, slow_piles, fast_kw, slow_kw, fast_cap, slow_cap):
    """对 normal 策略做完全模拟：所有车按 priority 派到同模式桩；
       桩 FIFO 满时排队等空位。返回 {pile_code: [(plate, kwh), ...]} 按桩 FIFO 全部顺序。

       initial_cars: [(plate, kwh, mode, priority_idx)]  按 priority_idx 升序送入
    """
    pile_caps = {**{p: fast_cap for p in fast_piles}, **{p: slow_cap for p in slow_piles}}
    pile_kw = {**{p: fast_kw for p in fast_piles}, **{p: slow_kw for p in slow_piles}}
    pile_seq = {p: [] for p in fast_piles + slow_piles}  # 派遣顺序

    def candidate_piles(mode):
        return fast_piles if mode == "fast" else slow_piles

    def finish_time(pile, idx):
        """该桩第 idx 号车的完工时刻（在当前 pile_seq 下）"""
        cumulative = 0.0
        for k in range(idx + 1):
            cumulative += pile_seq[pile][k][1] / pile_kw[pile]
        return cumulative

    def best_pile_for(mode, kwh):
        """模拟 _dispatch_one：选当前队列下"我加入后完工时间最早"的桩。"""
        best = None
        for pile in candidate_piles(mode):
            wait = sum(c[1] for c in pile_seq[pile]) / pile_kw[pile]
            charge = kwh / pile_kw[pile]
            ft = wait + charge
            if best is None or ft < best[0]:
                best = (ft, pile)
        return best[1] if best else None

    # 第一波：填满每桩到 FIFO 容量
    leftover = []
    for plate, kwh, mode, _ in initial_cars:
        target = best_pile_for(mode, kwh)
        if target and len(pile_seq[target]) < pile_caps[target]:
            pile_seq[target].append((plate, kwh))
        else:
            leftover.append((plate, kwh, mode))

    # 后续：每当任一桩中某车完工，腾出 FIFO 槽 → leftover 中的下一辆同模式车进入
    # （priority 顺序）
    # 模拟思路：用 events 表示每桩"释放第 k 个槽"的时刻
    # release_time[pile][k] = pile 上第 (k+1) 辆车完工时间（同模式 leftover 第 N+1 辆才能上）
    # 我们贪心：按时间顺序处理释放事件，每次放 leftover 队首匹配模式的车
    leftover_fast = [c for c in leftover if c[2] == "fast"]
    leftover_slow = [c for c in leftover if c[2] == "slow"]

    def process_leftover(piles, fifo_cap, lo):
        events = []
        for pile in piles:
            for k in range(min(fifo_cap, len(pile_seq[pile]))):
                events.append((finish_time(pile, k), pile))
        events.sort()
        for t, pile in events:
            if not lo:
                break
            plate, kwh, _m = lo.pop(0)
            pile_seq[pile].append((plate, kwh))
            # 派进去后，下一次它释放槽的时间 = 它自己完工
            new_idx = len(pile_seq[pile]) - 1
            events.append((finish_time(pile, new_idx), pile))
            events.sort()

    process_leftover(fast_piles, fast_cap, leftover_fast)
    process_leftover(slow_piles, slow_cap, leftover_slow)
    return pile_seq


def sum_finish_hours(state_dict, power_map):
    """给定每桩的派车列表，按入桩顺序累加每辆车的 finish time，并求和。

    支持 (plate, kwh) 与 (plate, kwh, role) 两种 tuple 形式。
    """
    total = 0.0
    detail = []
    for pile_code, cars in state_dict.items():
        if pile_code not in power_map:
            continue
        cumulative = 0.0
        for car in cars:
            plate, target = car[0], car[1]
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


SIZES_25 = (
    [("F", 10)] * 5
    + [("T", 10)] * 5
    + [("F", 30)] * 3
    + [("T", 30)] * 2
    + [("F", 60)] * 3
    + [("T", 60)] * 2
    + [("F", 100)] * 3
    + [("T", 100)] * 2
)


def run_case_8_2_batch_short():
    """§8.2 batch_short：25 辆触发批量调度，返回 full plan {pile_code: [(plate, kwh)…]}。"""
    reset_world()
    app_config.EXTENDED_SCHEDULE_POLICY = "batch_short"
    app_sched.EXTENDED_SCHEDULE_POLICY = "batch_short"

    base = datetime(2026, 6, 14, 6, 0, 0)
    _set_now(base)
    with SessionLocal() as db:
        for i, (m, kwh) in enumerate(SIZES_25, start=1):
            mode = ChargeMode.FAST if m == "F" else ChargeMode.SLOW
            submit_waiting(db, f"V{i}", mode, kwh, base + timedelta(minutes=i))
        db.commit()
        _set_now(base + timedelta(hours=1))
        app_sched.try_dispatch(db)
        db.commit()
        plan = full_plan_snapshot(db)
    return plan


def normal_full_plan_25():
    """对 normal 策略：纯算法模拟完整 25 辆派遣（含 leftover 接力）。"""
    initial = [
        (f"V{i+1}", kwh, "fast" if m == "F" else "slow", i + 1)
        for i, (m, kwh) in enumerate(SIZES_25)
    ]
    return normal_full_simulate(
        initial,
        fast_piles=["F1", "F2"],
        slow_piles=["T1", "T2", "T3"],
        fast_kw=30.0, slow_kw=10.0,
        fast_cap=PILE_QUEUE_CAPACITY, slow_cap=PILE_QUEUE_CAPACITY,
    )


def case_8_2():
    print(CASE_8_2_DESC)
    power_map = {"F1": 30.0, "F2": 30.0, "T1": 10.0, "T2": 10.0, "T3": 10.0}

    print("─── 跑 1：normal 策略（按模式硬分 + priority 兜底，模拟全 25 辆） ───")
    plan_normal = normal_full_plan_25()
    for p in ("F1", "F2", "T1", "T2", "T3"):
        items = [f"{plate}({kwh})" for plate, kwh in plan_normal.get(p, [])]
        print(f"  {p}: {' → '.join(items) or '-'}")
    sum_n, _ = sum_finish_hours(plan_normal, power_map)
    print(f"  全 25 辆 ∑Cj = {sum_n} h\n")

    print("─── 跑 2：batch_short 策略（spec §8.2 全局最优 ∑Cj） ───")
    plan_ext = run_case_8_2_batch_short()
    for p in ("F1", "F2", "T1", "T2", "T3"):
        items = [f"{plate}({kwh})" for plate, kwh in plan_ext.get(p, [])]
        print(f"  {p}: {' → '.join(items) or '-'}")
    sum_e, _ = sum_finish_hours(plan_ext, power_map)
    print(f"  全 25 辆 ∑Cj = {sum_e} h\n")

    saved = round(sum_n - sum_e, 3)
    verdict = "✓ §8.2 优于 normal" if saved > 0.001 else ("≈ 持平" if abs(saved) < 0.001 else "✗ 反常")
    print(f">>> 差距：normal {sum_n}h - §8.2 {sum_e}h = 省时 {saved}h {verdict}")
    print(">>> 说明：现在 ∑Cj 覆盖【全部 25 辆】（含 10 辆按 plan 接续入桩位置）。\n")
    return saved > 0.001


# ────────────────────────────────────────────────────────────────────────────
# 暴力枚举最优性验证（K 较小时）
# ────────────────────────────────────────────────────────────────────────────


def brute_force_optimal(cars_kwh: list[float], pile_kw: list[float]):
    """枚举所有 (车→桩) 分配 + 每桩内 SPT，返回最小 ∑Cj。

    cars_kwh: K 辆车的电量
    pile_kw : m 个桩的功率
    复杂度 m^K * K log K；K ≤ 8、m ≤ 5 时仍可承受。
    """
    K = len(cars_kwh)
    m = len(pile_kw)
    best = float("inf")
    best_assign = None
    # 用 itertools.product 枚举 m^K
    from itertools import product
    for assign in product(range(m), repeat=K):
        per_pile = [[] for _ in range(m)]
        for i, p in enumerate(assign):
            per_pile[p].append(cars_kwh[i])
        total = 0.0
        for j, group in enumerate(per_pile):
            group.sort()  # SPT
            cum = 0.0
            for kwh in group:
                cum += kwh / pile_kw[j]
                total += cum
        if total < best:
            best = total
            best_assign = assign
    return best, best_assign


def case_brute_force_8_2():
    """对小规模实例：把 batch_short 的全局最优与暴力枚举对比。"""
    print("\n═══════════════════════════════════════════════════════════════════════")
    print("  暴力枚举验证 §8.2 最优性（K=8 辆 / m=4 桩，跨模式）")
    print("═══════════════════════════════════════════════════════════════════════")
    # 8 辆车混合电量，跨快/慢
    cars = [10.0, 10.0, 30.0, 30.0, 60.0, 60.0, 100.0, 100.0]
    # 4 桩：2 快 + 2 慢
    pile_kw_list = [30.0, 30.0, 10.0, 10.0]
    # 重置数据库到这套小配置
    from app.db import engine as _eng
    _eng.dispose()
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    init_db()
    with SessionLocal() as db:
        for i, kw in enumerate(pile_kw_list, 1):
            mode = ChargeMode.FAST if kw >= 30 else ChargeMode.SLOW
            code = f"F{i}" if mode == ChargeMode.FAST else f"T{i-2}"
            db.add(ChargingPile(pile_code=code, mode=mode, power_kw=kw, queue_capacity=10))
        u = User(username="brute", password_hash=hash_password("brute"), is_admin=True)
        db.add(u); db.flush()
        for i in range(1, len(cars) + 1):
            db.add(Vehicle(license_plate=f"B{i}", owner_id=u.id, battery_capacity_kwh=60.0))
        db.commit()

    app_config.EXTENDED_SCHEDULE_POLICY = "batch_short"
    app_sched.EXTENDED_SCHEDULE_POLICY = "batch_short"
    base = datetime(2026, 6, 14, 6, 0, 0)
    _set_now(base)
    with SessionLocal() as db:
        # 触发条件：到站车 ≥ 4*10 + 10 = 50（默认 WAITING_AREA_SIZE=10）。
        # 我们用 batch_short 触发不了，那就直接调 _plan_batch 做规划。
        for i, kwh in enumerate(cars, 1):
            mode = ChargeMode.FAST if i % 2 == 1 else ChargeMode.SLOW
            submit_waiting(db, f"B{i}", mode, kwh, base + timedelta(seconds=i))
        db.commit()
        app_sched._plan_batch(db)
        db.commit()
        # 提取 plan
        my_assign = []
        my_sum = 0.0
        per_pile_my = {}
        for pile in db.query(ChargingPile).order_by(ChargingPile.id).all():
            rows = (
                db.query(ChargingRequest)
                .filter(ChargingRequest.assigned_pile_id == pile.id,
                        ChargingRequest.batch_plan_order.is_not(None))
                .order_by(ChargingRequest.batch_plan_order.asc())
                .all()
            )
            per_pile_my[pile.pile_code] = [(r.vehicle.license_plate, r.target_amount_kwh) for r in rows]
            cum = 0.0
            for r in rows:
                cum += r.target_amount_kwh / pile.power_kw
                my_sum += cum
                my_assign.append((r.vehicle.license_plate, pile.pile_code))
    print(f"  我的 plan: {per_pile_my}")
    print(f"  我的 ∑Cj = {round(my_sum,4)}")

    bf_sum, bf_assign = brute_force_optimal(cars, pile_kw_list)
    print(f"  暴力枚举最优 ∑Cj = {round(bf_sum,4)}")
    diff = round(my_sum - bf_sum, 4)
    ok = abs(diff) < 1e-6
    print(f"  差 = {diff} h   →   {'✓ 我的算法达到最优' if ok else '✗ 不是最优（diff > 0）'}")
    return ok


# ────────────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "█" * 72)
    print("  扩展调度（spec §8 选做）测试报告")
    print("█" * 72 + "\n")

    ok1 = case_8_1()
    ok2 = case_8_2()
    ok3 = case_brute_force_8_2()

    print("\n" + "═" * 72)
    print(f"  §8.1 结论：{'通过 ✓ multi_short 优于 normal' if ok1 else '失败 ✗'}")
    print(f"  §8.2 结论：{'通过 ✓ batch_short 优于 normal' if ok2 else '失败 ✗'}")
    print(f"  §8.2 最优性枚举：{'通过 ✓ 与暴力解相等' if ok3 else '失败 ✗'}")
    print("═" * 72 + "\n")
    sys.exit(0 if (ok1 and ok2 and ok3) else 1)
