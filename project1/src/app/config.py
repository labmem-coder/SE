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
ENTRY_CONFIRM_TIMEOUT_SECONDS = 300   # 叫号后用户响应超时：5 分钟
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

# 演示加速倍率：1 表示真实时间；10 表示真实 1 秒 = 模拟 10 秒
# 验收标准比例尺 1:10 —— 真实 30 分钟 = 模拟 5 小时
# 仅影响充电进度推进与超时判定；不影响分时电价的"现实时刻"判定。
TIME_ACCELERATION = 10.0

# 请求编号前缀
REQUEST_CODE_PREFIX = "REQ"
BILL_CODE_PREFIX = "BILL"
