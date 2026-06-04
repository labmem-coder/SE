# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Software Engineering coursework (波普特大学) for a **智能充电桩调度计费系统** — Smart Charging Pile Scheduling and Billing System. Spec (HW1) and dynamic/static design (HW2) are both delivered; a runnable FastAPI implementation lives in `project1/src/`. All design documents are written in Chinese; preserve Chinese terminology when naming user-facing fields, and use the explicit English programmable names defined in the operation contracts for code identifiers.

Authoritative sources:
- `project1/docs/overview.md` — top-level requirements (Chinese)
- `project1/docs/hw1_report_v3.md` — full HW1 deliverable: domain model + use case model + SSDs + operation contracts (§2.2 has the canonical system-event signatures)
- `project1/docs/hw2/hw2_report.md` — HW2: architecture, sequence diagrams for every system event, design class diagram
- `project1/docs/acceptance_demo.md` + `project1/docs/test_report.md` — acceptance script and test report
- `project1/docs/media/generated/*.svg` — UML diagrams (domain class, activity, use-case, 9 SSDs)

## Commands

All commands run from `project1/src/`:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python server.py seed                       # create tables, 2 fast + 3 slow piles, 4 test users
python server.py serve                      # http://127.0.0.1:8000  (Swagger at /docs)
python server.py serve 0.0.0.0 8765         # custom host/port

python demo_acceptance.py                   # multi-user end-to-end demo (service must be running)
BASE_URL=http://127.0.0.1:8765 python demo_acceptance.py
```

There is **no test suite** — the project is validated by `demo_acceptance.py` and the manual acceptance script in `docs/acceptance_demo.md`. There is also no linter/formatter configured.

Test accounts (all seeded with username == password): `admin`, `alice`, `bob`, `carol`, `dave`.

## System Constants (fixed by spec, tunable at acceptance)

Centralized in `app/config.py`:

- `FAST_PILE_COUNT=2`, `SLOW_PILE_COUNT=3`, `PILE_QUEUE_CAPACITY=4` (charging + behind-pile queue)
- `FAST_PILE_POWER_KW=30`, `SLOW_PILE_POWER_KW=7`
- `ENTRY_CONFIRM_TIMEOUT_SECONDS=300` — 5-min叫号 window (uses real wall time)
- `PRICING_SCHEDULE` (time-of-use 谷/平/峰) + `SERVICE_FEE_YUAN_PER_KWH=0.8`
- `TIME_ACCELERATION=60.0` — **demo speedup only affects charging progress, NOT the 5-min timeout**. So a 15 kWh fast charge finishes in ~30 real seconds, but a dispatched user still has 5 real minutes to ACK.
- `BACKGROUND_TICK_SECONDS=5` — APScheduler tick period
- Scheduling objective: minimize **完成充电所需时间 = 等待时长 + 自己充电时长** for the dispatched vehicle (NOT total system time). Implemented in `scheduler.estimate_finish_hours`.

## Architecture

Three-tier C/S, all server-side:

- **FastAPI** HTTP + auto OpenAPI (`app/main.py`)
- **SQLAlchemy 2.x** + **SQLite** (`charging_station.db` at repo root of `src/`) — `app/db.py`, `app/models.py`
- **APScheduler** background tick every 5s drives time and re-runs the dispatcher — `app/tick.py` → `scheduler.try_dispatch`
- Single-page HTML/JS frontend at `web/index.html`, served from `/`

Request lifecycle (state machine in `models.RequestStatus`):

```
WAITING ──dispatch──> DISPATCHED ──ConfirmEntry──> QUEUING_PILE ──pile-FIFO──> CHARGING ──> COMPLETED
   │                       │                                                       │
   │ cancel                │ 5-min timeout (real time)                              │ pile fault mid-session
   └──> CANCELLED <────────┘                                                       └──> FAULT_INTERRUPTED + new request
```

`try_dispatch()` (in `app/scheduler.py`) is the single dispatch entry point and is **idempotent** — call it from any event that changes queue/pile state (new request, cancel, completion, fault, fault recovery, background tick). It always: (1) advances active sessions, (2) closes any completed sessions, (3) expires DISPATCHED-but-unconfirmed requests, (4) dispatches per mode picking the pile that minimizes `estimate_finish_hours` for that request, (5) starts the next QUEUING_PILE request at any newly-idle pile.

Server-side BCE layout inside `app/`:

| Role | Files |
|---|---|
| Boundary (HTTP) | `routers/user_api.py` (UC_01..07), `routers/admin_api.py` (UC_08..11), `auth.py`, `schemas.py` |
| Control (services) | `scheduler.py` (dispatch + session advancement), `pricing.py` (TOU billing), `fault.py` (UC_09/UC_10), `views.py` (queue view assembly), `tick.py` |
| Entity (ORM) | `models.py` |
| Infrastructure | `db.py`, `config.py`, `seed.py`, `main.py`, `server.py` |

## Use Case → API Contract → HTTP Mapping

Operation contracts in `hw1_report_v3.md §2.2` define the system event names. The codebase exposes them under the routes below — keep these in sync if you add new events.

| UC | System event (use verbatim as handler identifier) | HTTP |
|---|---|---|
| UC_01 | `SubmitChargeRequest(userId, vehicleId, mode, targetAmount, entryToken)` | `POST /api/requests` |
| UC_02 | `UpdateChargeRequest(requestId, newMode, newTargetAmount)` | `PUT /api/requests/{id}` |
| UC_03 | `CancelChargeRequest(requestId)` | `DELETE /api/requests/{id}` |
| UC_04 | `QueryQueueStatus(requestId)` | `GET /api/requests/{id}` |
| UC_05 | `ConfirmEntry(requestId)` | `POST /api/requests/{id}/confirm` |
| UC_06 | `ReportDeviceAbnormal(userId, pileId, description)` | `POST /api/reports` |
| UC_07 | `QueryBill(requestId)` + `ConfirmPayment(billId, payChannel)` | `GET /api/bills/by-request/{id}`, `POST /api/bills/{id}/pay` |
| UC_08 | `QueryPileStatus()` | `GET /api/admin/piles` |
| UC_09 | `ConfirmPileFault(pileId, faultType, faultTime, sourceReportId)` | `POST /api/admin/piles/{id}/fault` |
| UC_10 | `ResumePile(pileId)` | `POST /api/admin/piles/{id}/resume` |
| UC_11 | `QueryOperationReport(dateRange)` | `GET /api/admin/reports?from=&to=` |

## Domain Model (18 classes — see `media/generated/domain_model.png`)

Composition: 充电站 ▣ 等候区 + 充电区. Aggregation: 等候区 ◇ 2× 等待队列 (fast/slow); 充电区 ◇ many 充电桩. Inheritance: 快充桩, 慢充桩 ▷ 充电桩.

The implementation deliberately collapses several domain classes (documented in `models.py` header and `src/README.md` §"与设计的偏差"):

- `ChargingStation` / `WaitingArea` / `ChargingArea` / `WaitingQueue` / `DailyReport` — not persisted; they are logical containers/views derived from `ChargingRequest.priority_time` ordering and aggregate queries.
- `PricingRule` / `ServiceFeeRule` — kept in `config.py` instead of tables.
- `FastChargingPile` / `SlowChargingPile` — no inheritance table; distinguished via `ChargingPile.mode`. Power and queue capacity differ only by the mode field.
- `Admin` — folded into `User.is_admin`.

When extending the model, prefer following these simplifications unless the new feature genuinely requires the missing tables.

## Invariants the Implementation Must Enforce

These are testable behaviors — when changing scheduling, billing, or fault code, verify each still holds.

1. **No queue-jumping**: WAITING requests are dispatched strictly in `priority_time` order per mode (`scheduler._dispatch_mode`).
2. **5-min dispatch ACK uses real time**: `handle_dispatch_timeouts` ignores `TIME_ACCELERATION`; a stale DISPATCHED row auto-cancels and frees the slot for the next eligible request.
3. **Mode change = re-submit**: `UpdateChargeRequest` with a new mode cancels the original and creates a new request at the tail of the new mode's queue (`routers/user_api.update_charge_request`). Only target-kWh changes update in place.
4. **Overdue bill blocks new requests**: `SubmitChargeRequest` precondition rejects users with unpaid + overdue bills (`user_api._user_has_overdue_unpaid_bill`, threshold = `BILL_OVERDUE_HOURS`).
5. **Release-on-completion**: the pile is freed the moment charging ends, NOT when the bill is paid (`_complete_session` + `_maybe_start_next_at_pile`). Payment is asynchronous.
6. **Fault rescheduling preserves priority**: when a pile faults mid-session, the in-flight session is marked `INTERRUPTED` with a stage bill snapshot, and a NEW request for the **remaining** kWh inherits the original `priority_time` so it keeps its place in the same-mode waiting queue (`fault.confirm_pile_fault`).
7. **Abnormal report ≠ fault**: `ReportDeviceAbnormal` only creates an `AbnormalReport` row. Only `ConfirmPileFault` by an admin transitions a pile to `FAULT`.
8. **TOU billing must cross rate boundaries correctly**: `pricing.calculate_charging_fee` segments the session by clock hour against `PRICING_SCHEDULE`; bills must always reference both the TOU rate and the flat service fee.

## Conventions

- New system events: add to `routers/*.py`, keep the handler name identical to the operation-contract message name (CamelCase from §2.2). Wire any state-mutating handler to call `try_dispatch(db)` before returning so queues stay current.
- Schemas use Pydantic v2; place request/response models in `schemas.py`, never in router modules.
- Background work goes through `tick.py` so APScheduler owns the loop — do not spawn threads.
- The Chinese terms in `overview.md` are the user-facing source of truth (e.g. 充电区, 等候区, 故障, 排队号). API field names use the English equivalents from §2.2; keep both vocabularies aligned when adding fields.

## Team

Group lead: 唐振桓; members: 杨凌俊, 袁炜途, 马迪轩, 刘宇轩. HW1 dated 2026/04/10, HW2 dated 2026/06/02.
