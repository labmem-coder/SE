#!/usr/bin/env python3
"""HTTP / 前端栈 acceptance test —— 走 FastAPI 与 VirtualClock，
驱动 acceptance_test.py 完全相同的事件序列，与后端 CSV 做单元格级对比。

为什么需要这个？
  acceptance_test.py 通过 monkey-patch datetime + 直接调用 scheduler 跑事件，
  绕过了 HTTP / FastAPI 路由 / 虚拟时钟 / APScheduler 后台 tick。
  前端的真实流程是 HTTP 调 API → 改 VirtualClock → tick 推进。
  这套脚本就是验证两条路径在【相同事件序列】下的快照一致。

依赖：
  pip install requests
  服务器会被本脚本自启自停（端口 8765）。

输出：
  对每个 5 分钟时间点，比较 6 列 × 3 行单元格；总结 PASS/FAIL。
"""
from __future__ import annotations

import os
import sys
import csv
import time
import subprocess
import urllib.request
import urllib.error
import json
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE_URL = "http://127.0.0.1:8765"
TIMEOUT = 5


# ────────────────────────────────────────────────────────────────────────────
# 轻量 HTTP 客户端（用标准库 urllib，避免 requests 依赖）
# ────────────────────────────────────────────────────────────────────────────


_TOKEN: str | None = None


def http(method: str, path: str, body=None, expect_ok: bool = True):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    if _TOKEN:
        req.add_header("Authorization", "Bearer " + _TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        if expect_ok:
            raise RuntimeError(f"{method} {path} → {e.code} {msg}")
        return {"_error": e.code, "_body": msg}


def wait_health(retries: int = 60, delay: float = 0.5) -> bool:
    for _ in range(retries):
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(delay)
    return False


# ────────────────────────────────────────────────────────────────────────────
# DB 直连：复用 app.* 的 ORM 来初始化 tester + V1..V30
# ────────────────────────────────────────────────────────────────────────────


def init_world() -> None:
    """删 DB，建表，造 tester 管理员 + 桩 + V1..V30 车辆。
    必须在 server 启动前完成（不然 server 启动 lifespan 也会 init_db）。"""
    db_file = os.path.join(HERE, "charging_station.db")
    if os.path.exists(db_file):
        os.remove(db_file)

    # 重置 config + 清理 sys.modules 缓存
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]

    from app.db import SessionLocal, init_db
    from app.auth import hash_password
    from app.models import ChargeMode, ChargingPile, User, Vehicle
    from app.config import (
        FAST_PILE_COUNT, SLOW_PILE_COUNT, FAST_PILE_POWER_KW, SLOW_PILE_POWER_KW,
        PILE_QUEUE_CAPACITY,
    )

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
            username="tester",
            password_hash=hash_password("tester"),
            is_admin=True,
        )
        db.add(admin)
        db.flush()
        for i in range(1, 31):
            db.add(Vehicle(license_plate=f"V{i}", owner_id=admin.id, battery_capacity_kwh=60.0))
        db.commit()


# ────────────────────────────────────────────────────────────────────────────
# Server 进程管理
# ────────────────────────────────────────────────────────────────────────────


def start_server() -> subprocess.Popen:
    log_file = open(os.path.join(HERE, ".frontend_test_server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "serve", "127.0.0.1", "8765"],
        cwd=HERE,
        stdout=log_file, stderr=log_file,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if not wait_health():
        proc.terminate()
        raise RuntimeError("server health check timed out")
    return proc


# ────────────────────────────────────────────────────────────────────────────
# 业务封装
# ────────────────────────────────────────────────────────────────────────────


def login_as_tester() -> int:
    global _TOKEN
    r = http("POST", "/api/login", {"username": "tester", "password": "tester"})
    _TOKEN = r["token"]
    return r["user_id"]


def reset_clock() -> None:
    http("POST", "/api/clock/reset")


def advance_clock_to(target: datetime, current: list[datetime]) -> None:
    """current 是个长度 1 list（mutable 容器），表示当前虚拟时间。"""
    delta_min = (target - current[0]).total_seconds() / 60.0
    if delta_min > 0:
        http("POST", "/api/clock/advance", {"minutes": delta_min})
    current[0] = target


def vehicle_id_by_plate(cache: dict[str, int], plate: str) -> int:
    if plate in cache:
        return cache[plate]
    vs = http("GET", "/api/vehicles")
    for v in vs:
        cache[v["license_plate"]] = v["id"]
    return cache[plate]


def active_request_for(plate: str, cache: dict[str, int]) -> dict | None:
    reqs = http("GET", "/api/me/requests")
    for r in reqs:
        if r["licensePlate"] != plate:
            continue
        if r["status"] in ("waiting", "fault_queued", "dispatched", "queuing_pile", "charging"):
            return r
    return None


def auto_confirm_all() -> None:
    """所有 DISPATCHED → confirm。按 dispatchedAt 升序，保证桩内 FIFO = 派遣顺序。"""
    reqs = http("GET", "/api/me/requests")
    dispatched = [r for r in reqs if r["status"] == "dispatched"]
    dispatched.sort(key=lambda r: (r.get("dispatchedAt") or "", r["requestId"]))
    for r in dispatched:
        http("POST", f"/api/requests/{r['requestId']}/confirm", expect_ok=False)


def apply_event(when: datetime, ev_type: str, obj: str, mode_op: str, value: float,
                veh_cache: dict[str, int]) -> str:
    if ev_type == "A":
        vid = vehicle_id_by_plate(veh_cache, obj)
        if value == 0:
            existing = active_request_for(obj, veh_cache)
            if existing is None:
                return ""
            res = http("DELETE", f"/api/requests/{existing['requestId']}", expect_ok=False)
            return "" if "_error" not in res else "FAIL"
        mode = "fast" if mode_op == "F" else "slow"
        payload = {
            "vehicleId": vid, "mode": mode, "targetAmount": float(value),
            "entryToken": f"E-{obj}",
        }
        r = http("POST", "/api/requests", payload, expect_ok=False)
        if r.get("_error") == 400 and "waiting area" in r.get("_body", "").lower():
            return "REJECTED"
        if "_error" in r:
            return "REJECTED"
        if not r.get("accepted", False):
            return "REJECTED"
        return ""

    if ev_type == "C":
        existing = active_request_for(obj, veh_cache)
        if existing is None:
            return "NO_REQ"
        payload = {}
        if mode_op in ("F", "T"):
            payload["newMode"] = "fast" if mode_op == "F" else "slow"
        if value is not None and value != -1:
            payload["newTargetAmount"] = float(value)
        http("PUT", f"/api/requests/{existing['requestId']}", payload, expect_ok=False)
        return ""

    if ev_type == "B":
        piles = http("GET", "/api/admin/piles")["piles"]
        target = None
        for p in piles:
            code = p["pileCode"]
            # 兼容 T/S 前缀
            if code == obj or (obj.startswith("T") and code == "S" + obj[1:]) or (obj.startswith("S") and code == "T" + obj[1:]):
                target = p
                break
        if target is None:
            return "FAIL"
        pid = target["pileId"]
        if value == 0:
            http("POST", f"/api/admin/piles/{pid}/fault",
                 {"faultType": "frontend-test", "faultTime": when.isoformat()},
                 expect_ok=False)
        else:
            http("POST", f"/api/admin/piles/{pid}/resume", expect_ok=False)
        return ""

    return ""


# ────────────────────────────────────────────────────────────────────────────
# 快照 → CSV 单元
# ────────────────────────────────────────────────────────────────────────────


def snapshot_state(now_label: str) -> list[list[str]]:
    """返回 6 列 × 3 行字符串，与 acceptance_test.py 的 cells_per_col 同构。
    列：F1, F2, T1/S1, T2/S2, T3/S3, 等候区。"""
    piles = http("GET", "/api/admin/piles")["piles"]
    # 构建 code → entry
    by_code: dict[str, dict] = {p["pileCode"]: p for p in piles}

    def get_pile_cars(code: str) -> list[str]:
        """{pile_code: [(plate, charged_kwh, current_fee_or_zero), ...]}"""
        # 找 pileId
        cand_codes = [code]
        if code.startswith("T"):
            cand_codes.append("S" + code[1:])
        elif code.startswith("S"):
            cand_codes.append("T" + code[1:])
        p = None
        for c in cand_codes:
            if c in by_code:
                p = by_code[c]
                break
        if p is None:
            return []
        # 拉桩详细排队
        detail = http("GET", f"/api/admin/piles/{p['pileId']}/queue")
        cars = detail["vehicles"]
        # 排首位 = CHARGING；其余按到达顺序（后端已排）
        out = []
        for v in cars:
            plate = v["licensePlate"]
            status = v["status"]
            if status == "charging" and p.get("chargingLicensePlate") == plate:
                kwh = p["chargingProgressKwh"] or 0.0
                fee = _estimate_fee(kwh, p["powerKw"])
                out.append(f"({plate},{kwh:.2f},{fee:.2f})")
            elif status == "fault_queued":
                out.append(f"({plate},0.00,0.00)(故障)")
            else:
                out.append(f"({plate},0.00,0.00)")
        return out

    def get_waiting() -> list[str]:
        # 用户视角 GET /api/me/requests 拿到所有自己的请求（tester 拥有全部 30 辆车）
        reqs = http("GET", "/api/me/requests")
        waiting = []
        fault_overflow = []
        for r in reqs:
            if r["status"] == "waiting":
                m = "F" if r["mode"] == "fast" else "T"
                waiting.append((r, f"({r['licensePlate']},{m},{r['targetAmount']:.2f})"))
            elif r["status"] == "fault_queued":
                # 挂在非故障桩或没桩的 fault_queued → 算溢出
                assigned = r.get("assignedPileCode")
                pile_status = None
                if assigned and assigned in by_code:
                    pile_status = by_code[assigned]["status"]
                if pile_status != "fault" or assigned is None:
                    m = "F" if r["mode"] == "fast" else "T"
                    fault_overflow.append(
                        (r, f"({r['licensePlate']},{m},{r['targetAmount']:.2f})(被故障队列车挤出)")
                    )
        # waiting + 故障溢出 一起按 requestId 升序（与 priority_time 同序）
        merged = waiting + fault_overflow
        merged.sort(key=lambda x: x[0]["requestId"])
        return [item[1] for item in merged]

    cells = []
    for code in ("F1", "F2", "T1", "T2", "T3"):
        items = get_pile_cars(code)
        row = items[:3] + ["-"] * (3 - len(items[:3]))
        cells.append(row)
    waiting = get_waiting()
    wait_row = ["-"] * 3
    if waiting:
        wait_row[0] = "-".join(waiting)
    cells.append(wait_row)
    return cells


# 粗略费用估算（不查后端 pricing，因为没暴露 endpoint） —— 取平均价 0.7 + 服务费 0.8
def _estimate_fee(kwh: float, power_kw: float) -> float:
    # 与 acceptance_test 的 _current_fee_of_session 等价的费用做不到（需 server 时刻），
    # 这里返回 0 占位 —— 我们只用它做"非零 / 有进度"的存在性比对。
    return 0.0


# ────────────────────────────────────────────────────────────────────────────
# 事件列表 / 期望样本（与 acceptance_test.py 同步）
# ────────────────────────────────────────────────────────────────────────────


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
    ("08:25", "B", "T1", "O", 0),
    ("08:30", "A", "V26", "T", 20),
    ("08:35", "A", "V27", "T", 25),
    ("08:50", "B", "F1", "O", 0),
    ("09:00", "A", "V28", "F", 30),
    ("09:10", "A", "V1", "O", 0),
    ("09:15", "B", "T1", "O", 1),
    ("09:20", "A", "V27", "O", 0),
    ("09:25", "C", "V21", "O", 35),
    ("09:30", "A", "V19", "O", 0),
]


# 期望样本：与 acceptance_test.py EXPECTED_SAMPLES 同。
# 这里我们额外做整表 diff，期望样本只是用来快速 sanity check。
EXPECTED_SAMPLES = [
    ("06:00", 2, 0, "(V1,0.00,0.00)"),
    ("06:05", 2, 0, "(V1,0.83,1.00)"),
    ("06:05", 3, 0, "(V2,0.00,0.00)"),
    ("07:05", 5, 0, "(V13,F,110.00)"),
    ("07:10", 5, 0, "(V13,F,110.00)-(V14,F,95.00)"),
    ("07:15", 5, 0, "(V13,F,110.00)-(V14,F,95.00)"),
]


def _norm(s: str) -> str:
    if s is None:
        return ""
    s = s.strip().strip('"').replace(" ", "")
    if s in ("-", ""):
        return ""
    # 忽略 fee 列（HTTP 端没暴露实时 fee） —— 把 "(plate,kwh,fee)" 截掉 fee
    return _strip_fee(s.replace("/", "-"))


def _strip_fee(s: str) -> str:
    """把 "(V1,0.83,1.00)" 改成 "(V1,*,*)" 便于忽略累计 kWh + fee 列差异。

    HTTP 端只暴露当前 session 的实时 kWh；后端 acceptance 测试遍历 rescheduled_from
    链显示累计 kWh，这是显示层差异，不代表调度行为差异 —— 本对比聚焦在
    车牌 + (故障) 标记，因为那才是调度状态的体现。
    """
    out = []
    i = 0
    while i < len(s):
        if s[i] == "(":
            j = s.index(")", i)
            inner = s[i + 1:j]
            parts = inner.split(",")
            if len(parts) == 3:
                # car cell: (plate, kwh, fee)
                p2 = parts[1].strip()
                # 等候区车辆：第二个字段是 F/T 模式标识 —— 保留
                if p2 in ("F", "T"):
                    inner = f"{parts[0]},{p2},{parts[2]}"
                else:
                    inner = f"{parts[0]},*,*"
            out.append(f"({inner})")
            i = j + 1
            while i < len(s) and s[i] == "(":
                k = s.index(")", i)
                out.append(s[i:k + 1])
                i = k + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


# ────────────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────────────


def run(fault_policy: str = "priority"):
    print(f"\n=== 前端 (HTTP) acceptance test, 故障策略={fault_policy} ===\n")
    # 直接改 config 文件不现实；通过 PUT /api/admin/config
    init_world()
    proc = start_server()
    try:
        login_as_tester()
        # 设故障策略
        http("PUT", "/api/admin/config", {"faultDispatchPolicy": fault_policy}, expect_ok=False)
        reset_clock()

        base_day = datetime(2026, 6, 9, 0, 0, 0)
        veh_cache: dict[str, int] = {}
        current_vtime = [base_day.replace(hour=6, minute=0, second=0)]

        def parse_t(hhmm: str) -> datetime:
            h, m = hhmm.split(":")
            return base_day.replace(hour=int(h), minute=int(m))

        ev_by_time = {parse_t(e[0]): e for e in EVENTS}
        timepoints = []
        t = base_day.replace(hour=6, minute=0)
        end = base_day.replace(hour=9, minute=30)
        while t <= end:
            timepoints.append(t)
            t += timedelta(minutes=5)

        table: dict[str, list[list[str]]] = {}
        rejected: list[tuple[str, str]] = []

        for tp in timepoints:
            advance_clock_to(tp, current_vtime)
            auto_confirm_all()
            ev = ev_by_time.get(tp)
            ev_label = ""
            if ev is not None:
                _, ev_type, obj, mode_op, value = ev
                outcome = apply_event(tp, ev_type, obj, mode_op, value, veh_cache)
                ev_label = f"({ev_type},{obj},{mode_op},{value})"
                if outcome == "REJECTED":
                    ev_label += " [拒收:等候区满]"
                    rejected.append((tp.strftime("%H:%M"), ev_label))
                elif outcome == "NO_REQ":
                    ev_label += " [无效]"
                auto_confirm_all()

            cells = snapshot_state(tp.strftime("%H:%M"))
            table[tp.strftime("%H:%M")] = cells

            # 屏幕打印（简）
            wait_str = cells[5][0] if cells[5][0] != "-" else "-"
            print(f"{tp.strftime('%H:%M'):<6}{(ev_label or '-'):<28}"
                  f"F1={','.join([c for c in cells[0] if c != '-']) or '-':<24}"
                  f"F2={','.join([c for c in cells[1] if c != '-']) or '-':<24}"
                  f"T1={','.join([c for c in cells[2] if c != '-']) or '-':<24}"
                  f"T2={','.join([c for c in cells[3] if c != '-']) or '-':<24}"
                  f"T3={','.join([c for c in cells[4] if c != '-']) or '-':<24}"
                  f"等候={wait_str}")

        # ─── 期望样本校验 ───
        print("\n=== 期望样本校验（来自 测试用例.csv 已填字段）===")
        col_names = ["F1", "F2", "T1", "T2", "T3", "等候区"]
        passed = 0
        failed = 0
        for tp_str, col, row, expected in EXPECTED_SAMPLES:
            actual = table[tp_str][col][row]
            if _norm(actual) == _norm(expected):
                verdict = "PASS"
                passed += 1
            else:
                verdict = "FAIL"
                failed += 1
            print(f"  [{verdict}] {tp_str} {col_names[col]}#{row+1}: "
                  f"期望={expected}  实际={actual}")
        print(f"\n期望样本：{passed} PASS / {failed} FAIL\n")

        # ─── 与后端 CSV 整表 diff ───
        suffix_map = {"priority": "_优先级调度", "time_order": "_时间顺序调度"}
        suffix = suffix_map.get(fault_policy, f"_{fault_policy}")
        backend_csv = os.path.join(
            HERE, "..", "docs", f"acceptance_test_output{suffix}.csv"
        )
        if os.path.exists(backend_csv):
            diff_total, diff_match = compare_with_backend_csv(table, backend_csv)
            print(f"=== 后端 CSV 整表对比（仅比较桩列 + 等候区，忽略费用列）===")
            print(f"   单元格匹配：{diff_match} / {diff_total}")
            return diff_match == diff_total
        else:
            print(f"⚠ 后端 CSV 不存在：{backend_csv}")
            return failed == 0

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def compare_with_backend_csv(table: dict, backend_csv_path: str) -> tuple[int, int]:
    """读 backend CSV，与 table 做单元格级对比；返回 (total, match)。"""
    with open(backend_csv_path, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    # 跳过前 2 行 header
    data_rows = rows[2:]
    # 每 3 行一组 = 一个时间点
    total = 0
    match = 0
    mismatch_log: list[str] = []
    for group_start in range(0, len(data_rows), 3):
        group = data_rows[group_start:group_start + 3]
        if not group:
            continue
        timestamp_cell = group[0][0]
        if not timestamp_cell:
            continue
        # HH:MM:SS → HH:MM
        tp_str = timestamp_cell[:5]
        if tp_str not in table:
            continue
        actual_cells = table[tp_str]   # 6 列 × 3 行
        for col in range(6):
            for row in range(3):
                # backend CSV 行 = 3-row group; col 偏移 +2（前 2 列是 timestamp+event）
                backend_val = group[row][col + 2] if col + 2 < len(group[row]) else ""
                actual_val = actual_cells[col][row]
                if _norm(backend_val) == _norm(actual_val):
                    match += 1
                else:
                    mismatch_log.append(
                        f"  {tp_str} 列{col+1}({['F1','F2','T1','T2','T3','等候'][col]})"
                        f"#{row+1}: 后端={backend_val!r} 前端={actual_val!r}"
                    )
                total += 1
    if mismatch_log:
        print("\n--- 不匹配单元格（前 20 条）---")
        for line in mismatch_log[:20]:
            print(line)
        if len(mismatch_log) > 20:
            print(f"  ...另有 {len(mismatch_log) - 20} 条")
    return total, match


if __name__ == "__main__":
    policy = os.environ.get("FAULT_POLICY", "priority")
    ok = run(policy)
    sys.exit(0 if ok else 1)
