# 智能充电桩调度计费系统 —— 实现

按 `project1/docs/overview.md` 老师要求 + `project1/docs/hw1_report_v3.md` 设计文档实现的可运行系统。

## 技术栈

- **Python 3.10+**
- **FastAPI** —— HTTP + 自动生成 OpenAPI 文档
- **SQLAlchemy 2.x** + **SQLite** —— ORM 与持久化
- **APScheduler** —— 后台周期任务（推进充电进度、超时检查、调度触发）
- 单页 HTML/JS 前端，用于演示

## 快速开始

```bash
cd project1/src
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1) 初始化数据库（创建表、2 快 + 3 慢 共 5 个桩、测试用户）
python server.py seed

# 2) 启动服务
python server.py serve
# 默认 http://127.0.0.1:8000
```

启动后：

- 浏览器打开 <http://127.0.0.1:8000/> 使用演示前端
- API 文档：<http://127.0.0.1:8000/docs>

## 测试账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `admin` | 充电站管理员 |
| `alice` | `alice` | 充电用户（车牌：京A·EV001） |
| `bob` | `bob` | 充电用户 |
| `carol` | `carol` | 充电用户 |
| `dave` | `dave` | 充电用户 |

## 关键配置（`app/config.py`）

- `FAST_PILE_COUNT` / `SLOW_PILE_COUNT` —— 快/慢充桩数量
- `PILE_QUEUE_CAPACITY` —— 每个桩的车位数（含充电中那 1 个）
- `FAST_PILE_POWER_KW` / `SLOW_PILE_POWER_KW` —— 充电功率
- `ENTRY_CONFIRM_TIMEOUT_SECONDS = 300` —— 5 分钟叫号响应窗口
- `PRICING_SCHEDULE` + `SERVICE_FEE_YUAN_PER_KWH` —— 分时电价 + 服务费
- `TIME_ACCELERATION = 60` —— 演示用加速倍率：1 真实秒 = 60 仿真秒。**仅影响电量增长速度**，5 分钟超时仍按真实时间。
- `BACKGROUND_TICK_SECONDS = 5` —— 后台 tick 周期

## 11 个用例 / API 映射

| 用例 | 操作契约消息 | HTTP |
| --- | --- | --- |
| UC_01 提交请求 | `SubmitChargeRequest` | `POST /api/requests` |
| UC_02 修改请求 | `UpdateChargeRequest` | `PUT /api/requests/{id}` |
| UC_03 取消请求 | `CancelChargeRequest` | `DELETE /api/requests/{id}` |
| UC_04 查看排队 | `QueryQueueStatus` | `GET /api/requests/{id}` |
| UC_05 响应叫号 | `ConfirmEntry` | `POST /api/requests/{id}/confirm` |
| UC_06 上报异常 | `ReportDeviceAbnormal` | `POST /api/reports` |
| UC_07 查账单 | `QueryBill` | `GET /api/bills/by-request/{id}` |
| UC_07 支付 | `ConfirmPayment` | `POST /api/bills/{id}/pay` |
| UC_08 桩状态 | `QueryPileStatus` | `GET /api/admin/piles` |
| UC_09 确认故障 | `ConfirmPileFault` | `POST /api/admin/piles/{id}/fault` |
| UC_10 恢复桩 | `ResumePile` | `POST /api/admin/piles/{id}/resume` |
| UC_11 运营报表 | `QueryOperationReport` | `GET /api/admin/reports?from=&to=` |

## 关键业务规则的落地位置

| 规则 | 实现位置 |
| --- | --- |
| 等待队列模式分区，FIFO 严格不插队 | `scheduler.py: _dispatch_mode` 按 `priority_time` 排序 |
| 调度策略：被调度车辆完成时间最短 | `scheduler.py: estimate_finish_hours` |
| 5 分钟超时自动取消 | `scheduler.py: handle_dispatch_timeouts`（后台 tick） |
| 修改模式 = 取消原请求 + 重新提交（公平） | `routers/user_api.py: update_charge_request` |
| 充电结束立即释放桩，与支付无关 | `scheduler.py: _complete_session` + `_maybe_start_next_at_pile` |
| 用户上报不直接改桩状态，需管理员确认 | `routers/user_api.py: report_device_abnormal` 仅创建 `AbnormalReport` |
| 故障重调度：保留原始排队优先级 | `fault.py: confirm_pile_fault` 新建请求 `priority_time = 原 submitted_at` |
| 故障会话立即停止计量并生成阶段账单 | `fault.py` 调用 `pricing.generate_bill` |
| 分时电价：跨档位正确分段计费 | `pricing.py: calculate_charging_fee` |
| 欠费超期阻止新请求 | `routers/user_api.py: _user_has_overdue_unpaid_bill` |

## 工程结构

```
src/
├── server.py                    # CLI 入口（seed / serve）
├── requirements.txt
├── README.md
├── app/
│   ├── config.py                # 系统常量（验收时调）
│   ├── db.py                    # SQLAlchemy 引擎/会话
│   ├── models.py                # ORM 模型 = 领域类
│   ├── schemas.py               # Pydantic in/out
│   ├── auth.py                  # 简化的 token 鉴权
│   ├── pricing.py               # 分时电价 + 账单
│   ├── scheduler.py             # 调度算法 + 会话推进
│   ├── fault.py                 # 故障与重调度
│   ├── tick.py                  # APScheduler 后台 tick
│   ├── views.py                 # 排队视图组装
│   ├── seed.py                  # DB 初始化
│   ├── main.py                  # FastAPI 应用
│   └── routers/
│       ├── user_api.py          # UC_01 ~ UC_07
│       └── admin_api.py         # UC_08 ~ UC_11
└── web/
    └── index.html               # 单页演示前端
```

## 演示脚本

启动服务后用前端就能跑通完整流程。命令行示例：

```bash
# 1) 登录
TOKEN=$(curl -s -X POST localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alice"}' | jq -r .token)

# 2) 提交一笔快充请求
curl -X POST localhost:8000/api/requests \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"vehicleId":1,"mode":"fast","targetAmount":15,"entryToken":"DEMO"}'

# 3) 查询排队状态
curl localhost:8000/api/me/requests -H "Authorization: Bearer $TOKEN"

# 4) 在 5 分钟内确认入场（id 替换为返回的 requestId）
curl -X POST localhost:8000/api/requests/1/confirm -H "Authorization: Bearer $TOKEN"

# 5) 等几秒（TIME_ACCELERATION=60 时，15kWh / 30kW = 30 分钟仿真 ≈ 30 真实秒）
curl localhost:8000/api/me/bills -H "Authorization: Bearer $TOKEN"

# 6) 管理员视角：看桩状态、运营报表
ADMIN=$(curl -s -X POST localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}' | jq -r .token)
curl localhost:8000/api/admin/piles -H "Authorization: Bearer $ADMIN"
curl localhost:8000/api/admin/reports -H "Authorization: Bearer $ADMIN"
```

## 验收演示

为了现场清晰展示多用户同时提交请求、快慢充分队列、调度分桩、桩内排队、异常上报、故障确认、故障重调度、故障恢复和运营报表，可参考：

- `project1/docs/acceptance_demo.md` —— 验收讲解顺序、展示点和预期现象
- `demo_acceptance.py` —— 服务启动后一键运行的多用户命令行演示

运行方式：

```bash
cd project1/src
python demo_acceptance.py
# 若服务端口不是 8000：
BASE_URL=http://127.0.0.1:8765 python demo_acceptance.py
# 若需要重复演示并清空上一次演示状态：
python demo_acceptance.py --reset
```

## 与 hw1_report_v3.md 设计的偏差

- **领域类合并**：`充电站 / 等候区 / 充电区 / 等待队列 / 日报表 / 分时电价规则 / 服务费规则` 不单独建表 —— 它们或为单例（一个站）、或为逻辑视图（按 `priority_time` 查询）、或在 `config.py` 中集中维护。在领域分析层上仍存在，工程层做了简化。
- **管理员**：归并到 `User.is_admin`，避免双表带来的鉴权割裂。
- **快充/慢充继承**：未做 SQL 继承表，仅用 `ChargingPile.mode` 鉴别 —— 在面向对象建模中这两个子类的行为差异只有功率，子类化带来的复杂度不值得。
- **入场凭证 `entryToken`**：演示中接受任何非空字符串。真实部署可对接校园一卡通/入场闸机签发的 JWT。


⚠️ 防止再次踩坑

acceptance_test.py 和 web UI 用同一个 DB 文件，互相会污染。建议规则：

┌──────────────────┬───────────────────────────────────────────────────────────────────────────┐
│     想做的事     │                                   步骤                                    │
├──────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ 跑验收测试看 CSV │ rm charging_station.db && FAULT_POLICY=priority python acceptance_test.py │
├──────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ 用 UI 体验系统   │ rm charging_station.db && python server.py seed && python server.py serve │
├──────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ 来回切换         │ 每次切换前都 rm charging_station.db                                       │
└──────────────────┴───────────────────────────────────────────────────────────────────────────┘

要不要我把这个一键脚本加上？比如：

# clean_and_serve.sh
rm -f charging_station.db
python server.py seed
python server.py serve

或者改 acceptance_test.py 跑完自动 seed 标准账号？这样两套数据可以共存（虽然 acceptance_test 的脏数据仍在）。哪个更方便你？

