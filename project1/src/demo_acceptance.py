"""Acceptance demo script for multi-user charging scenarios.

Run after starting the service:

    python demo_acceptance.py

Optional:

    BASE_URL=http://127.0.0.1:8765 python demo_acceptance.py

Run repeatedly against the local demo database:

    python demo_acceptance.py --reset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib import error, request


BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ACTIVE_STATUSES = {"waiting", "dispatched", "queuing_pile", "charging"}


def http(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload


def must(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> Any:
    status, payload = http(method, path, body, token)
    if status >= 400:
        raise RuntimeError(f"{method} {path} failed: HTTP {status}, {payload}")
    return payload


def login(username: str, password: str) -> str:
    payload = must("POST", "/api/login", {"username": username, "password": password})
    return payload["token"]


def reset_local_demo_data() -> None:
    """Clear demo runtime data in the local SQLite database.

    This is intended for acceptance rehearsals where the service was started from
    project1/src and uses the default sqlite:///./charging_station.db database.
    """
    from app.db import SessionLocal
    from app.models import (
        AbnormalReport,
        Bill,
        ChargingPile,
        ChargingRequest,
        ChargingSession,
        FaultRecord,
        PileStatus,
    )

    with SessionLocal() as db:
        db.query(Bill).delete()
        db.query(ChargingSession).delete()
        db.query(FaultRecord).delete()
        db.query(AbnormalReport).delete()
        db.query(ChargingRequest).delete()
        for pile in db.query(ChargingPile).all():
            pile.status = PileStatus.AVAILABLE
            pile.total_sessions = 0
            pile.total_charged_kwh = 0.0
            pile.total_revenue = 0.0
        db.commit()


def find_active_requests(tokens: dict[str, str]) -> list[str]:
    active: list[str] = []
    for owner, token in tokens.items():
        status, payload = http("GET", "/api/me/requests", token=token)
        if status >= 400:
            continue
        for row in payload:
            if row["status"] in ACTIVE_STATUSES:
                active.append(
                    f"{owner}: requestId={row['requestId']} status={row['status']} "
                    f"pile={row.get('assignedPileCode') or '-'}"
                )
    return active


def print_step(title: str) -> None:
    print(f"\n=== {title} ===")


def print_queue(owner: str, queue_info: dict[str, Any]) -> None:
    print(
        f"{owner:<5} requestId={queue_info['requestId']:<3} "
        f"mode={queue_info['mode']:<4} status={queue_info['status']:<13} "
        f"queueNo={queue_info['queueNumber']:<4} "
        f"pile={queue_info.get('assignedPileCode') or '-':<3} "
        f"pilePos={queue_info.get('pileQueuePosition') or '-'}"
    )


def print_piles(admin_token: str) -> None:
    piles = must("GET", "/api/admin/piles", token=admin_token)
    for pile in piles["piles"]:
        active = pile["chargingRequestCode"] or "-"
        print(
            f"{pile['pileCode']:<2} mode={pile['mode']:<4} status={pile['status']:<9} "
            f"queue={pile['queueLength']}/{pile['queueCapacity']} active={active}"
        )
    print(
        f"waitingFast={piles['waitingQueueFast']} "
        f"waitingSlow={piles['waitingQueueSlow']} "
        f"pendingReports={piles['pendingAbnormalReports']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the multi-user acceptance demo.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear local demo requests/sessions/bills/faults before running",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.reset:
        reset_local_demo_data()
        print("local demo data reset.")

    print(f"BASE_URL={BASE_URL}")
    status, health = http("GET", "/health")
    if status != 200:
        print(f"service is not ready: HTTP {status}, {health}", file=sys.stderr)
        return 1

    admin = login("admin", "admin")
    tokens = {
        "alice": login("alice", "alice"),
        "bob": login("bob", "bob"),
        "carol": login("carol", "carol"),
        "dave": login("dave", "dave"),
    }

    active = find_active_requests(tokens)
    if active:
        print("\n检测到已有进行中的演示请求，本次不继续提交新请求：")
        for row in active:
            print(f"- {row}")
        print("\n这正是系统的重复活跃请求保护。若需要重复演示，请运行：")
        print("  ./.venv/bin/python demo_acceptance.py --reset")
        return 2

    print_step("1. 多用户同时提交充电请求")
    requests = {
        "alice": must(
            "POST",
            "/api/requests",
            {"vehicleId": 1, "mode": "fast", "targetAmount": 3, "entryToken": "IN"},
            tokens["alice"],
        )["queueInfo"],
        "bob": must(
            "POST",
            "/api/requests",
            {"vehicleId": 2, "mode": "fast", "targetAmount": 5, "entryToken": "IN"},
            tokens["bob"],
        )["queueInfo"],
        "carol": must(
            "POST",
            "/api/requests",
            {"vehicleId": 3, "mode": "slow", "targetAmount": 2, "entryToken": "IN"},
            tokens["carol"],
        )["queueInfo"],
        "dave": must(
            "POST",
            "/api/requests",
            {"vehicleId": 4, "mode": "fast", "targetAmount": 4, "entryToken": "IN"},
            tokens["dave"],
        )["queueInfo"],
    }
    for owner, queue_info in requests.items():
        print_queue(owner, queue_info)

    print_step("2. 管理员查看调度结果")
    print_piles(admin)

    print_step("3. 四个用户响应叫号")
    for owner, queue_info in requests.items():
        confirmed = must(
            "POST",
            f"/api/requests/{queue_info['requestId']}/confirm",
            token=tokens[owner],
        )["queueInfo"]
        requests[owner] = confirmed
        print_queue(owner, confirmed)

    print_step("4. 管理员查看充电区队列")
    print_piles(admin)
    print("说明：快充只有 2 个桩，因此 3 个快充用户中应至少有 1 个进入桩内排队。")

    print_step("5. 重复提交被拒绝")
    status, payload = http(
        "POST",
        "/api/requests",
        {"vehicleId": 1, "mode": "fast", "targetAmount": 1, "entryToken": "IN"},
        tokens["alice"],
    )
    print(f"alice duplicate submit -> HTTP {status}, detail={payload.get('detail') if isinstance(payload, dict) else payload}")

    print_step("6. 用户上报异常，管理员确认故障")
    alice_pile_code = requests["alice"]["assignedPileCode"]
    piles = must("GET", "/api/admin/piles", token=admin)["piles"]
    alice_pile = next(p for p in piles if p["pileCode"] == alice_pile_code)
    report = must(
        "POST",
        "/api/reports",
        {"pileId": alice_pile["pileId"], "description": "验收演示：用户发现充电桩输出异常"},
        tokens["alice"],
    )
    print(f"abnormal report id={report['reportId']}")
    fault = must(
        "POST",
        f"/api/admin/piles/{alice_pile['pileId']}/fault",
        {"faultType": "验收演示故障", "sourceReportId": report["reportId"]},
        admin,
    )
    print(
        f"fault accepted={fault['accepted']} interruptedSessionId={fault['interruptedSessionId']} "
        f"rescheduledRequests={fault['rescheduledRequests']}"
    )
    print_piles(admin)

    print_step("7. 管理员恢复故障桩并查看运营报表")
    resume = must("POST", f"/api/admin/piles/{alice_pile['pileId']}/resume", token=admin)
    print(f"resume accepted={resume['accepted']}, message={resume['message']}")
    time.sleep(1)
    report = must("GET", "/api/admin/reports", token=admin)
    print(
        f"report totalSessions={report['totalSessions']} "
        f"totalChargedKwh={report['totalChargedKwh']} "
        f"faultCount={report['faultCount']} "
        f"pendingBills={report['pendingBills']}"
    )

    print("\n验收演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
