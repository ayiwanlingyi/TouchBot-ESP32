# ============================================================
# 配置文件 — ESP32（初代 / 双核 LX6）+ SSD1306 128x64 OLED (I2C)
#            硬件引脚 / 菜单 / WiFi / 刷视频参数
#            适配 SPE-32S / ESP-WROOM-32 / ESP-32S 等 38 脚模组
# ============================================================

# ===== 硬件引脚（初代 ESP32）=====
# 选脚原则：
#   避开 6~11   —— 片外 SPI flash 占用（本模组为 32Mbit / 4MB 外挂 flash）
#   避开 34~39  —— 仅输入模式，且无内部上拉，接不了按键
#   避开 2/4/5/12/15 —— strapping 引脚，接按键可能影响启动
#   避开 1/3    —— UART0 TX/RX，留给 REPL 和日志
#   安全可用：13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33
#
# 特例：GPIO0（板载 BOOT 键）
#   上电/复位瞬间若 GPIO0 为低，芯片会进 ROM 下载模式，MicroPython 不会运行。
#   这是硬件行为，开机时手指别按着即可；启动完成后它就是普通输入脚，可正常当按键用。
OLED_WIDTH    = 128
OLED_HEIGHT   = 64
OLED_I2C_SCL  = 22       # I2C 时钟（ESP32 默认 I2C 引脚）
OLED_I2C_SDA  = 21       # I2C 数据（ESP32 默认 I2C 引脚）
OLED_I2C_FREQ = 400000
OLED_ADDR     = 0x3C     # SSD1306 常见地址，少数模块为 0x3D

# 四键方案（按键一端接 GPIO，另一端接 GND，内部上拉）
# 若只接了 2 个键（SEL + NEXT），其余功能靠长按兼容，不会失效
BTN_SEL_PIN   = 0        # 板载 BOOT：有屏=确认/编辑开关/开始停止（Setting 长按保存）
                         # 无屏=短按暂停/继续，长按 2 秒进配网；PC 遥控模式下无效
BTN_NEXT_PIN  = 33       # 下移 / 编辑 +1（长按仍等同 -1，兼容少键情况）
BTN_PREV_PIN  = 32       # 上移 / 编辑 -1
BTN_BACK_PIN  = 25       # 返回 / 退出当前界面（Setting 下会自动保存）
LONG_PRESS_MS = 1500     # 长按判定阈值（毫秒）

# ===== 无屏模式：没有 OLED 时，用长按 BOOT 进入网页配置 =====
HEADLESS         = True       # True = 不初始化 OLED，改用「运行 / 配置」两态循环
                              # PC 串口遥控（esp32_touch.py）只在无屏模式下生效
WIFI_MODE        = "ap"       # "ap" = 自建热点（推荐，不用查 IP）；"sta" = 连现有 WiFi
WIFI_AP_SSID     = "esp-bot"
WIFI_AP_PASSWORD = "12345678" # AP 密码至少 8 位，否则 ESP32 会拒绝启动
# BLE 启动前是否保留 WiFi。无屏模式必须保留（否则网页连不上），
# 下面这行不用改，代码会自动按 HEADLESS 判断。
KEEP_WIFI        = False

# REPL 波特率：固定 115200（实测运行时切换不可行，已放弃，勿改）。
# 提速手段保留：批量打包上传 + esptool 烧固件 921600（走 bootloader，独立于 REPL）。
REPL_BAUD        = 0

CONFIG_HTTP_PORT  = 80        # 配置模式端口。用 80 才能让强制门户正常弹窗
BOOT_LONG_PRESS_MS = 2000     # 长按 BOOT 进入配置模式的判定阈值（毫秒）
AUTO_START_ON_BOOT = False    # 开机不自动滑动：由 PC 遥控或短按 BOOT 决定何时开始
                              # （改 True 会开机自动滑，与遥控手势互相干扰）
STATUS_PRINT_MS    = 5000     # 串口状态打印间隔（毫秒）；设 0 关闭打印

# ===== 菜单项目 =====
MENU_ITEMS = [
    {"name": "Swipe",      "id": "swipe"},
    {"name": "Setting",    "id": "setting"},
    {"name": "Web",        "id": "web"},
]

# ===== WiFi 配置 =====
# 仅 WIFI_MODE = "sta"（连家里路由器）时才会用到；默认 "ap" 自建热点，
# 不读这个列表，留空即可。若改用 sta 模式，把你的 WiFi 填进来：
# wifi_config = [{"ssid": "你家WiFi", "password": "密码"}]
wifi_config = []

# ===== 刷视频 — 滑动间隔（秒） =====
SWIPE_INTERVAL_MIN = 10
SWIPE_INTERVAL_MAX = 40

# ===== 刷视频 — 滑动手势参数（真机调优用） =====
# 行程占屏高百分比：过低（<25）短视频 App 常不翻页
SWIPE_TRAVEL_PCT = 40
# 起点占屏高百分比（向上滑，起点在下、终点在上）
SWIPE_START_PCT = 70
# 移动采样点数与间隔（间隔应 >= BLE 连接间隔 30ms，过快会丢报文）
SWIPE_STEPS = 10
SWIPE_STEP_MS = 30
# 按下后保持时长（部分机型需 >=80ms 才承认拖拽）
SWIPE_PRESS_MS = 80
# tip-off 后到 release 的间隔
SWIPE_RELEASE_MS = 40
SWIPE_HUMANIZE   = True       # 拟人化滑动：随机加速度曲线 + 落点/行程/节奏抖动

# ===== 刷视频 — 场景档案（网页「场景模式」一键套用间隔+手势）=====
# 每个场景除间隔外还可覆盖手势参数（未写的字段用上面的全局默认值）：
#   travel_pct 行程% / start_pct 起点% / steps 采样点数 / step_ms 采样间隔ms
#   press_ms 按压时长ms / release_ms 抬手前停顿ms
SWIPE_PROFILES = {
    "短视频": {"interval_min": 10,  "interval_max": 40},
    "长视频": {"interval_min": 100, "interval_max": 300},
    "看小说": {"interval_min": 10,  "interval_max": 20,
              "travel_pct": 30, "steps": 12, "step_ms": 45,
              "press_ms": 120},
    "快速划": {"interval_min": 3,   "interval_max": 8,
              "travel_pct": 45, "steps": 8, "press_ms": 60},
}

# ===== 刷视频 — 目标手机屏幕分辨率 =====
PHONE_SCREEN_W = 1080
PHONE_SCREEN_H = 1920

# 预设分辨率列表 (宽, 高, 名称)——覆盖常见长宽比；
# 没有的机型在网页选「自定义」手输即可，不必改这里重刷
PHONE_RESOLUTIONS = [
    (1080, 1920, "1080x1920"),   # 16:9  老机型
    (720,  1280, "720x1280"),    # 16:9  入门
    (1080, 2160, "1080x2160"),   # 18:9
    (1080, 2280, "1080x2280"),   # 19:9
    (1080, 2340, "1080x2340"),   # 19.5:9
    (1080, 2400, "1080x2400"),   # 20:9
    (720,  1600, "720x1600"),    # 20:9  入门
    (1440, 2560, "1440x2560"),   # 16:9  QHD
    (1440, 3040, "1440x3040"),   # 19:9  QHD+
    (1440, 3120, "1440x3120"),   # 19.5:9 QHD+
    (1440, 3200, "1440x3200"),   # 20:9  QHD+
    (1220, 2712, "1220x2712"),   # 20.2:9 新款 1.5K
]

# ===== HTTP 服务端口 =====
HTTP_PORT = 8080
