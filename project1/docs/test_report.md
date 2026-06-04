# 智能充电桩调度计费系统测试报告

## 1. 测试概述

- 被测项目：`project1` 智能充电桩调度计费系统
- 需求来源：`project1/docs/overview.md`
- 实现说明：`project1/src/README.md`
- 测试人员：Codex
- 测试日期：2026-06-02
- 测试类型：接口端到端测试、业务规则边界测试、编译检查
- 测试结论：通过。本轮共执行 9 组测试，9 组通过，0 组失败，未发现阻塞性缺陷。

## 2. 测试环境

| 项目 | 内容 |
| --- | --- |
| 操作目录 | `/Users/labmem/code/SE/project1/src` |
| Python | 3.14.4 |
| FastAPI | 0.136.3 |
| SQLAlchemy | 2.0.50 |
| Pydantic | 2.13.4 |
| 数据库 | SQLite，临时隔离库 `/private/tmp/project1-api-test/charging_station.db` |
| 服务地址 | `http://127.0.0.1:8765` |
| 测试账号 | `admin/admin`、`alice/alice`、`bob/bob`、`carol/carol`、`dave/dave` |

说明：本次测试使用临时目录启动服务，避免污染项目自带的 `project1/src/charging_station.db`。

## 3. 测试准备

1. 进入临时测试目录：

   ```bash
   mkdir -p /private/tmp/project1-api-test
   cd /private/tmp/project1-api-test
   ```

2. 初始化临时数据库：

   ```bash
   PYTHONPATH=/Users/labmem/code/SE/project1/src \
   /Users/labmem/code/SE/project1/src/.venv/bin/python \
   -c 'from app.seed import seed; seed(); print("seed ok")'
   ```

   实际结果：输出 `seed ok`，成功创建 2 个快充桩、3 个慢充桩、管理员和 4 个测试用户。

3. 启动临时服务：

   ```bash
   PYTHONPATH=/Users/labmem/code/SE/project1/src \
   /Users/labmem/code/SE/project1/src/.venv/bin/python \
   -m uvicorn app.main:app --host 127.0.0.1 --port 8765
   ```

   实际结果：服务启动成功，后台 APScheduler 正常启动。

4. 编译检查：

   ```bash
   cd /Users/labmem/code/SE/project1/src
   ./.venv/bin/python -m compileall app server.py
   ```

   实际结果：`app` 与 `server.py` 编译通过，无语法错误。

## 4. 测试范围

本次测试覆盖 README 中列出的 11 个核心用例：

| 用例 | 接口 | 覆盖情况 |
| --- | --- | --- |
| UC_01 提交请求 | `POST /api/requests` | 已覆盖 |
| UC_02 修改请求 | `PUT /api/requests/{id}` | 已覆盖 |
| UC_03 取消请求 | `DELETE /api/requests/{id}` | 已覆盖 |
| UC_04 查看排队 | `GET /api/requests/{id}` | 已覆盖 |
| UC_05 响应叫号 | `POST /api/requests/{id}/confirm` | 已覆盖 |
| UC_06 上报异常 | `POST /api/reports` | 已覆盖 |
| UC_07 查账单/支付 | `GET /api/bills/by-request/{id}`、`POST /api/bills/{id}/pay` | 已覆盖 |
| UC_08 桩状态 | `GET /api/admin/piles` | 已覆盖 |
| UC_09 确认故障 | `POST /api/admin/piles/{id}/fault` | 已覆盖 |
| UC_10 恢复桩 | `POST /api/admin/piles/{id}/resume` | 已覆盖 |
| UC_11 运营报表 | `GET /api/admin/reports` | 已覆盖 |

同时覆盖以下关键业务规则：

- 登录鉴权与管理员权限控制。
- 入场凭证校验。
- 车辆归属校验。
- 同一车辆不能存在多个进行中请求。
- 充电中请求不能修改或取消。
- 充电完成后自动生成账单，支付后状态变为 `paid`。
- 模式变更采用“取消原请求 + 重新提交”策略。
- 用户异常上报不直接改变桩状态，管理员确认后才进入故障流程。
- 故障会中断当前会话、生成阶段账单、创建剩余电量重调度请求。
- 故障恢复后桩重新参与调度。
- 叫号 5 分钟超时自动取消。
- 跨分时电价档位计费。

## 5. 测试用例与结果

| 编号 | 测试项 | 操作步骤 | 期望结果 | 实际结果 | 结论 |
| --- | --- | --- | --- | --- | --- |
| TC-001 | 健康检查 | 请求 `GET /health` | 返回 `{"status":"ok"}` | 返回 `{"status":"ok"}` | 通过 |
| TC-002 | 登录成功与失败 | 使用 `admin/admin`、`alice/alice` 登录；使用 `alice/bad` 登录 | 正确账号返回 token；错误密码返回 401 | `admin.is_admin=true`，`alice` 登录成功，错误密码 401 | 通过 |
| TC-003 | 鉴权与角色权限 | `alice` 请求 `/api/me`；`alice` 请求管理员接口；`admin` 请求桩状态 | 普通用户可查自己信息，不能访问管理员接口；管理员可访问 | `/api/me` 返回 `alice`；普通用户访问管理员接口 403；管理员看到 5 个桩 | 通过 |
| TC-004 | 提交、确认、完成、账单、支付 | `alice` 先提交空白入场凭证、错误车辆；再提交合法快充请求，重复提交，确认入场，等待完成，查询账单并支付 | 非法入参被拒绝；合法请求进入调度；重复请求 409；确认后充电；完成后生成待支付账单；支付成功 | 空白凭证 400，错误车辆 404，合法请求 `dispatched`，重复提交 409，确认后 `charging`，最终 `completed`，账单 `pending`，支付后 `paid` | 通过 |
| TC-005 | 个人请求与账单列表 | 查询 `GET /api/me/requests` 与 `GET /api/me/bills` | 返回当前用户历史请求和账单 | `alice` 请求数 1，账单数 1 | 通过 |
| TC-006 | 修改请求与取消请求 | `bob` 提交快充请求；修改目标电量；再改为慢充；取消新请求；查询旧请求和新请求状态 | 修改电量成功；改模式后原请求取消并生成新请求；新请求可取消 | 电量修改成功；`modeChanged=true`；旧请求 `cancelled`；新请求取消成功且状态 `cancelled` | 通过 |
| TC-007 | 异常上报、故障确认、恢复、运营报表 | `carol` 提交并确认快充；上报桩异常；管理员确认故障；查询桩状态；恢复桩；查询运营报表；提交非法时间范围报表请求 | 异常上报进入待处理；确认故障后桩为 `fault`，当前会话中断并重调度；恢复成功；报表正常；非法时间范围 400 | 待处理异常数 1；故障确认成功；存在中断会话；重调度请求数 1；桩状态 `fault`；恢复成功；报表会话数 2，故障数 1；非法范围 400 | 通过 |
| TC-008 | 叫号超时取消 | 构造 `dispatched_at` 超过 301 秒的已调度请求，调用超时处理逻辑 | 请求自动变为 `cancelled` | 取消数量 1，请求状态 `cancelled` | 通过 |
| TC-009 | 跨分时电价计费 | 计算 2026-06-02 09:30 开始、30kW、30kWh 的充电费；计算 30kWh 服务费 | 09:30-10:00 平时 15kWh * 0.7，10:00-10:30 峰时 15kWh * 1.0，充电费 25.5；服务费 24.0 | 充电费 25.5，服务费 24.0 | 通过 |

## 6. 缺陷记录

本轮测试未发现功能性缺陷。

观察项：

- 项目 README 写的是 `python server.py seed`，但本机环境没有 `python` 命令，需要使用 `python3` 或 `.venv/bin/python`。这属于环境命令差异，不是项目功能缺陷。
- `.venv` 中未安装 `pytest` 和 `httpx/httpx2`，因此本轮未使用 FastAPI `TestClient`，改为启动真实 uvicorn 服务做 HTTP 测试。

## 7. 风险与未覆盖项

- 未进行真实浏览器 UI 自动化点击测试；本轮重点验证后端接口与业务流程。
- 未进行并发压力测试，例如多用户同时提交、同时确认入场。
- 未进行长时间运行稳定性测试，例如服务运行数小时后的后台 tick 行为。
- 未进行配置变更验收，例如动态修改快慢充桩数量和车位容量后的全量回归。

## 8. 总结

系统核心接口、权限控制、请求状态机、账单支付、故障重调度、运营报表、超时取消和跨时段计费均按需求表现。按本轮测试结果，`project1` 已具备课程验收演示的基本可用性。
