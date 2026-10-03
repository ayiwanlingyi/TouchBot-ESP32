# 创意钥匙扣 — ESP32 版（初代 ESP32 + SSD1306 128x64 OLED / I2C）

适配 **SPE-32S / ESP-WROOM-32 / ESP-32S** 等初代 ESP32（双核 LX6）模组，
搭配 SSD1306 128x64 I2C 单色 OLED（无屏也可运行，见「无屏模式」）。

核心功能：

- **BLE HID 触摸设备**：模拟真人手指对手机点击 / 滑动，广播名 `esp-bot`
- **PC 串口遥控**：115200 行协议，支持单击、拖拽、滑动与配置读写
- **无屏模式**（默认）：长按 BOOT 进网页配置态，短按暂停 / 继续
- **拟人化滑动**：随机落点 / 行程 / 节奏抖动，场景档案一键套用

## 硬件引脚（config.py 可改）

| 功能 | GPIO | 说明 |
| --- | --- | --- |
| OLED SDA | **21** | ESP32 默认 I2C 数据脚 |
| OLED SCL | **22** | ESP32 默认 I2C 时钟脚，400kHz |
| **SEL** | **0（板载 BOOT）** | 有屏：确认 / 编辑开关 / 开始停止（Setting 里长按保存）<br>无屏：短按暂停继续；**长按 2 秒进配网** |
| **NEXT** | **33** | 下移 / +1 |
| **PREV** | **32** | 上移 / -1 |
| **BACK** | **25** | 返回 / 退出（Setting 下会自动保存） |

按键在不同模式下的完整定义见下文「菜单与按键」（有屏）和「按键定义（无屏模式）」。

按键内部上拉，按下接 GND。OLED 地址默认 `0x3C`，少数模块为 `0x3D`（改 `OLED_ADDR`）。

**接不满也能跑**：没接线的 GPIO 靠内部上拉保持高电平，不会误触发。
只接「BOOT + NEXT」两键时，PREV / BACK 的功能由「长按 NEXT = -1」和
「长按 SEL = 保存退出」兼容替代 —— 接得越多操作越顺手，接得少也不会失效。

> ⚠️ **BOOT 键的唯一限制（重要）**
> 上电或复位**瞬间**若 GPIO0 被按住，芯片会进 ROM 下载模式，MicroPython 不会运行
> （屏幕不亮、串口无输出，表现像"刷坏了"）。**开机瞬间手指别按着。**
> 启动完成后它就是普通输入脚，`Pin(0, Pin.IN, Pin.PULL_UP)` 与其他 GPIO 无区别。

### 初代 ESP32 选脚注意

| GPIO | 状态 | 原因 |
| --- | --- | --- |
| **6 ~ 11** | ❌ 禁用 | 片外 SPI flash 占用（本模组 32Mbit / 4MB） |
| **34 ~ 39** | ❌ 禁用 | 仅支持输入，且无内部上拉，接不了按键 |
| **2/4/5/12/15** | ⚠️ 避开 | strapping 引脚，接按键可能影响启动 |
| **1 / 3** | ⚠️ 避开 | UART0 TX/RX，留给 REPL 和日志 |
| **0** | ✅ 已用 | 板载 BOOT 键，见上方注意事项 |
| 13/14/16/17/18/19/21/22/23/25/26/27/32/33 | ✅ 可用 | 通用 IO |

> 若你的板子没引出 33，SEL 可改到 **18/19** 或 **13/14**（改 `config.py` 即可）。

## 固件下载与烧录

初代 ESP32 用 **ESP32_GENERIC** 固件：

> https://micropython.org/download/ESP32_GENERIC/

- Features：`BLE, External Flash, WiFi` —— BLE 可用
- 当前已验证的版本：v1.29.0 (2026-08-24)
- **变体选 generic**（双核 / 4MB flash / 无 PSRAM）
  - 若模组带 PSRAM 改选 `spiram`；单核 SOLO 改选 `unicore`；2MB 内置 flash 改选 `d2wd`

```bash
esptool.py erase_flash
esptool.py --baud 921600 write_flash 0x1000 ESP32_GENERIC-<日期>-<版本>.bin
```

> 参数与仓库根目录 `esp32_tool.py` 的烧录菜单保持一致（波特率 921600、地址 0x1000）。
> ⚠️ **ESP32烧写地址是 `0x1000`**（ESP32-C3 才是 `0`），别搞混。
> Windows 上程序可能叫 `esptool`；识别不到串口加 `--port COMx`；
> 烧写到一半失败就把 `--baud 921600` 降到 `460800` 再试。

刷完先进 REPL 确认 BLE 可用：

```python
>>> import bluetooth      # 不报错 = OK
```

## 目录结构

```
├── boot.py              # 启动入口：硬件初始化 → splash（不连 WiFi）
├── main.py              # 开机直进 Swipe，退出后主菜单调度（分发表）
├── config.py            # 引脚 / 菜单 / WiFi / 滑动参数配置
│
├── app/
│   ├── swipe.py         # BLE 触控刷视频（协议逻辑与彩屏版一致）
│   ├── setting.py       # 间隔/分辨率设置，持久化 settings.json
│   ├── web.py           # 按需连 WiFi + 启动 HTTP 配置服务
│   └── http_server.py   # 轻量 HTTP 服务（不含显示逻辑）
│
├── core/
│   ├── hardware.py      # I2C/OLED/Button 初始化
│   ├── wifi.py          # WiFi 按需连接（扫描优先）/ 断开
│   └── splash.py        # 启动动画
│
└── drivers/
    ├── ssd1306.py       # SSD1306 I2C 驱动 + 兼容层
    └── Button.py
```

## 屏幕布局约定（128x64，8x8 字体）

| Y | 用途 |
| --- | --- |
| 0~10 | 标题栏（反白） |
| 14 / 25 / 36 | 正文行 1/2/3 |
| 47 | 正文行 4（数值 / 状态） |
| 56 | 底部提示行 |

**改 UI 时务必保证每行字符串 ≤ 16 字符**，超出部分会被右侧截断。

## 无屏模式（默认，用网页 + 串口操作）

`config.py` 默认 `HEADLESS = True`：开机**跳过 OLED 和菜单**，
串口遥控 + Web 控制台完成全部操作（有屏时改成 `False` 进菜单界面）。

```python
HEADLESS         = True        # 默认无屏模式
WIFI_MODE        = "ap"        # 自建热点，手机直连，不用查 IP
WIFI_AP_SSID     = "esp-bot"
WIFI_AP_PASSWORD = "12345678"  # 至少 8 位
```

### 状态循环（WiFi 只在配置时开）

```
        ┌──────────── 长按 BOOT 2 秒 ────────────┐
        ↓                                          │
   【运行态】                                 【配置态】
   BLE 开 / WiFi 关                        WiFi 开 / BLE 关
   短按=暂停/继续                          网页改完点「保存设置」
                                                  │ 网页保存
                                                  └→ 关 WiFi → 开 BLE → 回运行态

   ★ 两个状态下 WiFi 与 BLE 都只有一个在工作，切换时先关后开，绝不共存。
```

**完整流程**

1. 上电 → 进入**运行态**（BLE 广播，不自动滑）
2. **长按 BOOT 2 秒** → 进入**配置态**，关 BLE、开热点 `esp-bot`
3. 手机连上该 WiFi —— **通常自动弹出配置页**（强制门户）；没弹就访问 `http://192.168.4.1`
4. 网页里改参数 / 选场景模式
5. 点「保存设置」→ **关 WiFi → 开 BLE → 开始滑动**（网页随即断开）
6. 手机蓝牙配对 `esp-bot`（BLE 已开，看手机蓝牙列表即可）
7. **短按 BOOT** → 暂停 / 继续自动滑动
8. 想再改 → 回到第 2 步（再长按 BOOT）

### 按键定义（无屏模式，默认）

无屏模式只有板载 BOOT 键（GPIO0）参与操作：

| 操作 | 运行态（WiFi 已关） | 配置态（WiFi 开着） | 遥控模式（PC 已发 REMOTE） |
| --- | --- | --- | --- |
| **长按 BOOT 2 秒** | 进入配置态：关 BLE → 开热点 `esp-bot` → 弹配置网页 | 无效（退出请用网页「保存设置」） | 无效 |
| **短按 BOOT** | **暂停 / 继续**自动滑动 | 无效 | 无效 |

- 长按 BOOT **只在运行态**触发配网；配置态下再长按无效
- 遥控模式下所有按键被屏蔽（防止 PC 遥控与手指操作互相干扰），
  发 `END` 退出遥控或按 RST 复位后恢复
- 配置态的唯一出口是网页「保存设置」：保存后自动重启回运行态并开蓝牙

> 这个分工保证了「**WiFi 一定先关闭，再启动滑动**」：
> 网页只负责保存设置与模式，启动权交给按键，
> 不会出现"一边开 WiFi 一边刷视频"的状态。

### 强制门户（连 WiFi 自动弹网页）原理

靠两件事配合：

1. **DNS 服务（UDP 53）**：`CaptiveDNS` 把手机查询的**任何域名**都解析回本机 `192.168.4.1`
2. **HTTP 跳转**：
   - Android 探测 `/generate_204`、Windows 探测 `/ncsi.txt` → 返回 **302** 跳到控制台
   - iOS 探测 `/hotspot-detect.html` → **直接返回页面**（系统小窗会显示它）
   - 其余未知路径一律 302 兜底

> 端口用 **80**（`CONFIG_HTTP_PORT`）才符合系统探测的预期，改成 8080 弹窗会失效。

### 网页能做什么

网页**只负责保存设置与模式**，不提供启动按钮（启动归 BOOT 短按）。

| 区域 | 内容 |
| --- | --- |
| 场景模式 | 短视频 10-40s / 长视频 100-300s / 看小说 10-20s，点一下即套用并保存 |
| 设置 | 最小 / 最大间隔、手机分辨率（下拉选预设） |
| 保存 | 「保存设置」= 保存并退出配置（关 WiFi → 开 BLE → 开始滑动） |

### 关键实现

- **`app/webremote.py`**：HTTP 服务 + `SwipeService`（非阻塞滑动），
  在 WiFi 与 BLE 之间做硬切换，二者绝不共存
- **`core/hardware.py`**：`HEADLESS=True` 时不初始化 OLED；
  即使有屏但屏幕没接好也只告警，不让整机起不来
- **`app/uartcmd.py`**：串口指令解析（PC 遥控），
  通过 UART2 挂在 GPIO3 上非阻塞接收，不占线程、不影响 REPL

⚠️ **注意**：无屏模式下 **BLE 与 WiFi 严格互斥**——配置态关 BLE 开 WiFi，
运行态关 WiFi 开 BLE，从不在同一时刻共存（切换时先关后开，见上方状态循环）。
好处是省内存、互不干扰；代价是**配置态里手机连不上蓝牙**，
配对要在退出配置、回到运行态（BLE 已开）后进行。

## 串口遥控（PC → ESP32，115200 行协议）

PC 端用仓库根目录的 `esp32_touch.py`（遥控库 `RemoteTouch` + 测试脚本），
固件端解析在 `app/uartcmd.py`。坐标为**手机屏幕物理像素**，映射到 HID 0-32767。

### 接收机制（重要）

- 指令经 **UART2** 接收（RX 挂在 GPIO3，与 REPL 的 UART0 RX 同一引脚，
  GPIO 矩阵允许一路输入喂多个外设），`uart.read()` 非阻塞读取，
  **不占线程、不与 BLE 抢内存**；回包仍走 REPL 的 `print`
- 开机前 3 秒（`ARM_GRACE_MS`）串口留给上传工具（Ctrl-C 进 raw REPL），
  之后固件接管串口进入遥控（接管后 Ctrl-C 失效，上传代码须在重启后的 3 秒内连接；
  `esp32_tool.py` 连接前会先复位板子，不受影响）
- `REMOTE` 握手成功后板子屏蔽物理按键；`END` 或按 RST 恢复

### 指令集

```
REMOTE                进入遥控模式（回 REMOTE ON）
END                   退出遥控模式（回 REMOTE OFF）
T x y [ms]            单击；ms=按住时长（默认 70），>200ms 即长按
P x y / M x y / R x y 按下 / 移动 / 抬起（原始直线轨迹，逐条发）
S x1 y1 x2 y2 [ms]    滑动（板端拟人化：缓动+横漂+节奏抖动），默认 300ms
W x1 y1 x2 y2 ... [ms] 途经多点画轨迹（模拟手抖 / 绕弧），默认 300ms
B x1 y1 cx cy x2 y2 [ms]  二阶贝塞尔曲线滑动，(cx,cy) 控制弯曲方向
START / STOP          开始 / 停止自动滑动（需蓝牙已连接）
GET                   打印当前配置（多行）后回 OK
SET key value         改配置，立即生效（落盘用 SAVE）
PROFILE 名称          套用场景档案：短视频 / 长视频 / 看小说 / 快速划
SAVE                  配置写入后备内存与 settings.json
STATUS                打印一行状态后回 OK
```

轨迹类指令怎么选：

| 场景 | 用哪个 |
| --- | --- |
| 普通翻页 / 点按钮 | `S`（自带拟人化，最省事） |
| 模拟手抖、走 S 形 | `W`，中间塞几个偏移点 |
| 带弧度的"甩"尾滑动 | `B`，控制点往甩的方向偏 |
| 精确控制每个采样点（识别联动、PC 端拟人算法） | `P` / `M` / `R` 自己逐条发 |

除握手外，所有指令以 `OK` 或 `ERR 原因` 应答（如蓝牙未连时 `ERR ble未连接`）。

### Python API（esp32_touch.RemoteTouch）

```python
from esp32_touch import RemoteTouch

rt = RemoteTouch("COM18")     # 复位 → 等就绪 → REMOTE 握手，一步到位
rt.tap(540, 1400)             # 单击
rt.tap(540, 1400, 500)        # 长按 500ms
rt.swipe(540, 1344, 540, 576) # 上滑（板端拟人化），默认 300ms
rt.waypoints([(540, 1344), (560, 1100), (520, 800), (540, 576)])
                              # 途经多点：模拟手抖 / 绕弧
rt.bezier(540, 1344, 700, 900, 560, 576)
                              # 贝塞尔曲线：收尾向右"甩"的上滑
rt.set("interval_min", "20")  # 改配置（立即生效，掉电保存再调 save()）
rt.close()                    # 发 END 并关串口（也支持 with 语句）
```

| 方法 | 对应指令 |
| --- | --- |
| `tap(x, y, ms=70)` | `T x y [ms]` |
| `press` / `move` / `release(x, y)` | `P` / `M` / `R` |
| `swipe(x1, y1, x2, y2, ms=300)` | `S` |
| `waypoints(pts, ms=300)` | `W` |
| `bezier(x1, y1, cx, cy, x2, y2, ms=300)` | `B` |
| `play(pts, gap=0.04)` | `P`/`M`/`R` 流式（ESP 零计算） |
| `swipe_sim(sim, w, cx, y0, y1, ms)` | `play` + `SwipeSim` 组合 |
| `start_auto()` / `stop_auto()` | `START` / `STOP` |
| `get()` / `set(key, value)` | `GET` / `SET` |
| `profile(name)` / `save()` | `PROFILE` / `SAVE` |
| `status()` | `STATUS` |
| `close()` | `END` |

### 拟人滑动算法（PC 端计算，ESP 只按坐标执行）

高级轨迹在 PC 端用 `SwipeSim` 生成，逐点经 `play()` 以 `P`/`M`/`R`
流式下发，ESP 端不做任何曲线 / 抖动计算：

- **单手模式** `SwipeSim(two_hand=False)`：单手握机时是大拇指在划，
  轨迹为凸向持机手一侧的**大弧**（起点偏手侧 6~10% 屏宽，向上回正）
- **双手模式** `SwipeSim(two_hand=True)`：食指划动，**微弧**近似直线
  （起点偏 2~3.5%，弓高 1~2%）
- **换手计时器**：每 30~45 分钟随机；计时未到弧线方向保持不变，
  到点整条弧镜像到另一侧，模拟左右手互换（构造参数
  `swap_min`/`swap_max` 单位为分钟，测试时可改小快速验证）
- 每次滑动的弓高、偏移、时长、落点、±2px 抖动独立随机，不会重复

```python
from esp32_touch import RemoteTouch, SwipeSim

rt = RemoteTouch("COM18")
sim = SwipeSim(two_hand=False)              # 单手拇指
for _ in range(5):
    rt.swipe_sim(sim, w=1080, cx=540, y0=1344, y1=576,
                 ms=random.randint(600, 900))
```

## 菜单与按键（有屏模式，`HEADLESS = False`）

| 界面 | 短按 SEL | 长按 SEL | BACK | PREV / NEXT |
| --- | --- | --- | --- | --- |
| 主菜单 | 进入所选功能 | — | 无作用 | 上下移动光标 |
| Swipe（未连蓝牙） | 退出 | **退出（2 秒）** | 退出 | 退出 |
| Swipe（空闲，已连蓝牙） | 开始自动滑动 | **退出（2 秒）** | 退出 | 退出 |
| Swipe（自动滑动中） | 停止滑动 | — | 停止并退出 | 停止并退出 |
| Setting（非编辑） | 进入编辑 | **保存并退出（1.5 秒）** | **保存并退出** | 移动光标 |
| Setting（编辑中） | 退出编辑 | **保存并退出（1.5 秒）** | **保存并退出** | 数值 -1 / +1 |
| Web | — | — | 退出（断开 WiFi） | — |

- **SEL = 板载 BOOT 键**，其余三个为外接按键
- Setting 里长按 SEL 保存是「只接 BOOT 一个键」时的兼容操作，平时用 BACK 更顺手
- 只接 2 键（BOOT + NEXT）时的兼容：编辑态长按 NEXT = -1，代替 PREV

## 滑动手势参数（config.py，真机调优）

| 常量 | 默认 | 说明 |
| --- | --- | --- |
| `SWIPE_TRAVEL_PCT` | 40 | 行程占屏高百分比；<25 时多数短视频 App 不翻页 |
| `SWIPE_START_PCT` | 70 | 起点占屏高百分比（终点 = 起点 - 行程） |
| `SWIPE_STEPS` | 10 | 移动采样点数 |
| `SWIPE_STEP_MS` | 30 | 采样间隔；应 ≥ BLE 连接间隔（30~50ms），过快报文会被覆盖 |
| `SWIPE_PRESS_MS` | 80 | 按下保持时长；部分机型需 ≥80ms 才承认拖拽 |
| `SWIPE_RELEASE_MS` | 40 | tip-off 到 release 的间隔 |

若仍偶发滑动失败：依次增大 `SWIPE_TRAVEL_PCT`（40→50）、`SWIPE_STEP_MS`（30→40）、`SWIPE_PRESS_MS`（80→100）。

## 部署

```bash
# 推荐方式：仓库根目录的部署工具（烧固件 / 传代码 / 备份 / 量产一条龙）
python esp32_tool.py
```

也可手动用 mpremote：

```bash
# 先烧 MicroPython（ESP32_GENERIC，见上文）
# 建议先传这些，确认能跑起来后再传 boot.py
mpremote cp main.py config.py :/
mpremote cp -r app core drivers :/
mpremote cp boot.py :/
```

## 部署前必改

- ~~`config.py` 的 `wifi_config`~~ 已清空：默认 AP 热点模式用不到；仅当改成 `WIFI_MODE = "sta"` 时才需要填自己的 WiFi
- `PHONE_SCREEN_W/H` 需与手机真实分辨率一致（也可进 Setting 菜单选预设，或用串口 `SET` 在线改）

## 配置存储

优先用 **`machine.mem_backup()`**（v1.29+ 新增，ESP32 上是 2048 字节 RTC 慢速内存），
读写都是内存操作，不擦写 flash。数据结构为 12 字节：

```
magic(2) | interval_min(2) | interval_max(2) | screen_w(2) | screen_h(2) | checksum(2)
```

带魔数 + 校验和 + 数值范围校验，读到脏数据会自动忽略并回退。
代码按 `mem.itemsize`（1 或 4）自动适配字节寻址 / 字寻址两种后备内存。

⚠️ **ESP32 的 RTC 内存没有电池后备**（官方文档 esp32 行 `Battery-backed = no）：

| 场景 | 后备内存是否保留 |
| --- | --- |
| 软复位、Ctrl-D、`machine.reset()`、deepsleep 唤醒 | ✅ 保留 |
| **掉电、按 EN/RESET 键**（芯片报 power-on reset） | ❌ 丢失 |
| 重新烧录固件 | ❌ 丢失 |

所以默认 `MIRROR_TO_FLASH = True`，**改写后备内存时同步写一份 `settings.json`**，
保证关机后参数还在；开机时优先读后备内存（快），读不到再读 flash。
想改成"纯内存、完全不碰 flash"就把 `MIRROR_TO_FLASH` 改成 `False`，
代价是每次断电后回到默认参数（10–40 秒 / 1080x1920）。

刷的固件若早于 v1.29（没有 `mem_backup`），会自动回退到只用 `settings.json`，不会报错。

