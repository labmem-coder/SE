"""系统级常量。验收时数量可调，集中在此修改即可。"""

# 充电桩部署（验收时可调）
FAST_PILE_COUNT = 2
SLOW_PILE_COUNT = 3
PILE_QUEUE_CAPACITY = 3          # 充电桩排队队列长度 M（含正在充电的1个）
WAITING_AREA_SIZE = 10           # 等候区最大容量 N

# 充电功率（度/小时）
FAST_PILE_POWER_KW = 30.0        # 快充：30 度/小时
SLOW_PILE_POWER_KW = 10.0        # 慢充：10 度/小时

# 业务规则
ENTRY_CONFIRM_TIMEOUT_SECONDS = 1800  # 叫号后用户响应超时：默认 30 分钟（演示时可在 admin 配置中调整）
BILL_OVERDUE_HOURS = 24               # 账单生成多久未付即视为"超期"

# 分时电价（峰/平/谷），yuan/kWh
PRICING_SCHEDULE = [
    (0,  7,  0.4),   # 谷时
    (7,  10, 0.7),   # 平时
    (10, 15, 1.0),   # 峰时
    (15, 18, 0.7),   # 平时
    (18, 21, 1.0),   # 峰时
    (21, 23, 0.7),   # 平时
    (23, 24, 0.4),   # 谷时
]
SERVICE_FEE_YUAN_PER_KWH = 0.8

# 数据库
DATABASE_URL = "sqlite:///./charging_station.db"

# 后台 tick 周期（秒）
BACKGROUND_TICK_SECONDS = 5

# 演示加速倍率。
# 现在时间源是 app.clock.VirtualClock，已经能控制流速（0=暂停 / 1× / 10× / …），
# 所以业务侧的 charged_kwh 计算用 1.0 即可：1 虚拟秒 = 1 秒充电。
# 实际"加速演示"由 VirtualClock.set_speed() 决定，验收时 +5min 按钮也是 5 虚拟分钟。
TIME_ACCELERATION = 1.0

# 请求编号前缀
REQUEST_CODE_PREFIX = "REQ"
BILL_CODE_PREFIX = "BILL"

# 故障调度策略 —— spec §7：两种都要实现，验收随机选
#   "priority"   : 优先级调度（暂停等候区，先派故障队列）
#   "time_order" : 时间顺序调度（故障队列 + 其它同类型桩未充电车辆合并按号码顺序重调）
FAULT_DISPATCH_POLICY: str = "priority"

# 扩展调度（spec §8 选做）—— 默认关闭
#   "normal"       : 标准单车顺序调度（默认）
#   "multi_short"  : 8.1 单次多车总充电时长最短
#   "batch_short"  : 8.2 批量调度（充电区+等候区满时触发）
EXTENDED_SCHEDULE_POLICY: str = "normal"

# 前端扩展调度测试用：默认自动调度；开启后新请求只进入 WAITING，
# 由管理员手动点击"一次性调度"触发 try_dispatch()。
MANUAL_DISPATCH_MODE: bool = False
