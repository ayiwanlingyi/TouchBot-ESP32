"""
串口遥控指令解析（PC → ESP32，115200 行协议）
==============================================
有屏模式（app/swipe.py + main.py）与无屏模式（app/webremote.py）共用这一份，
两边指令集完全一致，不会漂移。

    REMOTE / END            进入 / 退出遥控（必须先 REMOTE 握手）
    T x y [ms]              单击；ms=按住时长，>200ms 即长按
    P x y / M x y / R x y   按下 / 移动 / 抬起（原始直线轨迹，逐条发）
    S x1 y1 x2 y2 [ms]      滑动（拟人化：缓动 + 横漂 + 节奏抖动），默认 300ms
    W x1 y1 x2 y2 ... [ms]  途经多点画轨迹（模拟手抖 / 绕弧），默认 300ms
    B x1 y1 cx cy x2 y2 [ms]  二阶贝塞尔曲线滑动，(cx,cy) 控制弯曲方向
    START / STOP            开始 / 停止自动滑动（需要 ctl）
    GET                     打印当前配置
    SET key value           改配置（内存立即生效）
    PROFILE 名称            套用场景档案
    SAVE                    配置落盘
    STATUS                  打印一行状态

坐标是手机屏幕物理像素，按 config.PHONE_SCREEN_W/H 映射到 HID 0-32767。
配置类指令不要求蓝牙已连接，也不会打断正在进行的滑动。
"""
import sys
import time
import math
import random
import config

# 手势参数：串口/网页键名 -> config 属性 -> 允许范围（两边共用同一份）
GESTURE_LIMITS = (
    ("travel_pct",  "SWIPE_TRAVEL_PCT",  20, 90),
    ("start_pct",   "SWIPE_START_PCT",   30, 95),
    ("steps",       "SWIPE_STEPS",        5, 25),
    ("step_ms",     "SWIPE_STEP_MS",     30, 120),
    ("press_ms",    "SWIPE_PRESS_MS",    50, 300),
    ("release_ms",  "SWIPE_RELEASE_MS",  20, 200),
)

_buf = ""
_warned = False
_active = False
_start_ms = time.ticks_ms()
_uart = None                  # UART2（RX 挂 GPIO3），非阻塞读的主力通道
_rx = ""                      # 后台线程兜底路径的缓冲
_rx_lock = None
_thread_started = False
_go = False                   # 接管标志（线程路径用它放行读取）
_kbd_off = False              # kbd_intr(-1) 是否已生效
_arm_fail_ms = 0              # 上次接管失败的时间（失败后限频重试）
_arm_warned = False           # 接管失败的警告只打一次，避免刷屏

# 开机后先留这么长时间给上传工具（它靠 Ctrl-C 进 raw REPL），之后才接管串口。
# 接管后 Ctrl-C 失效，必须留这个窗口，否则以后没法再传代码。
ARM_GRACE_MS = 3000
ARM_RETRY_MS = 3000           # 线程创建失败后，隔这么久再试一次


def _reader_thread():
    """后台线程：阻塞读串口，逐字符塞进共享缓冲。

    REPL 的 USB 串口上 sys.stdin 只能阻塞读——放主线程会把主循环卡死
    （心跳停止、只剩 BLE 中断在打印）；放到独立线程里阻塞就无所谓了，
    主循环只从缓冲里非阻塞地取。

    线程在模块导入时就建好（BLE 还没启动，FreeRTOS 堆最富裕），
    但先挂在 _go 上空转；宽限期结束才放行——太早开始读会把上传工具
    用来进 raw REPL 的 Ctrl-C 字节吃掉。
    """
    global _rx
    while not _go:
        time.sleep_ms(50)
    while True:
        try:
            c = sys.stdin.read(1)
            if c:
                _rx_lock.acquire()
                if isinstance(c, bytes):
                    c = c.decode("utf-8", "replace")
                _rx += c
                if len(_rx) > 256:        # 防止一直没有换行时无限膨胀
                    _rx = _rx[-128:]
                _rx_lock.release()
            else:
                time.sleep_ms(10)
        except Exception:
            try:
                _rx_lock.release()
            except Exception:
                pass
            time.sleep_ms(100)            # 出错也不退出线程，稍后重试


def start_thread():
    """兜底路径：创建阻塞读 stdin 的后台线程（栈已压到 4KB）。
    BLE 运行时 FreeRTOS 堆建不了线程（8/16/4KB 实测都失败），
    所以它只是 UART2 挂不上时的最后手段。"""
    global _thread_started, _rx_lock, _arm_fail_ms, _arm_warned
    if _thread_started:
        return True
    try:
        import gc
        gc.collect()
        import _thread
        _rx_lock = _thread.allocate_lock()
        try:
            _thread.stack_size(4096)
        except Exception:
            pass
        _thread.start_new_thread(_reader_thread, ())
        _thread_started = True
        return True
    except Exception as e:
        _arm_fail_ms = time.ticks_ms()
        if not _arm_warned:
            _arm_warned = True
            print("[警告] 无法启动串口读取线程，遥控不可用: %s" % e)
        return False


def _init_uart2():
    """UART2 的 RX 挂到 GPIO3 —— 与 UART0（REPL）的 RX 是同一个引脚。

    ESP32 的 GPIO 矩阵允许一个输入脚同时喂多个外设，PC 发来的每个字节
    会同时进两路：REPL 的环形缓冲（程序运行期间没人读、读满了丢弃，
    无害）和 UART2 的驱动缓冲（uart.any()/read() 天然非阻塞）。
    因此完全不需要后台线程，也就绕开了「BLE 运行时建不了线程」的死结。
    TX 放在空闲的 GPIO17（板上未接任何东西），通信回包仍走 REPL 的 print。
    """
    global _uart
    if _uart is not None:
        return True
    try:
        import machine
        _uart = machine.UART(2, baudrate=115200, rx=3, tx=17, rxbuf=512)
        print("[uartcmd] UART2 通道已挂载 GPIO3")
        return True
    except Exception as e:
        _uart = None
        if not _arm_warned:
            _arm_warned = True
            print("[警告] UART2 初始化失败，串口遥控退回线程方案: %s" % e)
        return False


_init_uart2()     # 导入即挂 UART2：不占 FreeRTOS 堆，与 BLE 无资源冲突


def _arm():
    """宽限期结束后接管串口：关 Ctrl-C 键盘中断 + 清 UART2 积压 + 放行。

    UART2 挂不上时退回线程方案（隔 ARM_RETRY_MS 重试）。
    """
    global _go, _kbd_off
    if _go:
        return True
    if time.ticks_diff(time.ticks_ms(), _start_ms) < ARM_GRACE_MS:
        return False
    if not _kbd_off:
        try:
            import micropython
            micropython.kbd_intr(-1)      # 别再让 REPL 抢 Ctrl-C 字节
            _kbd_off = True
        except Exception as e:
            print("[警告] 无法关闭键盘中断: %s" % e)
    if _uart is None and not _thread_started:
        # UART2 没挂上 → 线程兜底
        if _arm_fail_ms and time.ticks_diff(time.ticks_ms(),
                                            _arm_fail_ms) < ARM_RETRY_MS:
            return False
        if not start_thread():
            return False
    if not _go:
        _go = True
        if _uart is not None:
            while _uart.any():            # 清掉开机以来积压的杂字节
                _uart.read()
        print("[遥控] 已接管串口，Ctrl-C 关闭（上传代码请在开机 3 秒内连接）")
    return True


def is_active():
    """是否已进入遥控模式（无屏模式用它决定按键是否让位给串口）"""
    return _active


def read_line():
    """非阻塞读一行串口输入；没凑齐一行返回 None。

    主力：UART2（RX 与 REPL 的 RX 同引脚）的 uart.read()，天然非阻塞；
    兜底：后台线程阻塞读 stdin 攒进 _rx，这里加锁取走。
    """
    global _buf, _rx
    if not _arm():             # 不到点 / 还没接管时直接跳过
        return None
    try:
        if _uart is not None:
            data = _uart.read()
            if data:
                _buf += data.decode("utf-8", "replace")
        elif _thread_started:
            # 把后台线程攒下的字符一次性取走（加锁，避免和线程同时改）
            _rx_lock.acquire()
            if _rx:
                _buf += _rx
                _rx = ""
            _rx_lock.release()
        else:
            return None
        _buf = _buf.replace("\r", "").replace("\x03", "")
        if len(_buf) > 512:            # 一直没有换行时防止无限膨胀
            _buf = _buf[-256:]
        if "\n" not in _buf:
            return None
        line, _, _buf = _buf.partition("\n")
        return line.strip()
    except Exception as e:
        # 出错静默失效最难查，喊一声（只喊一次）
        if not _warned:
            _warned = True
            print("[警告] 串口遥控读取出错: %s" % e)
        return None


# ══════════════════ 配置 ══════════════════

def _cfg_print():
    """GET：打印当前配置"""
    print("interval_min=%d interval_max=%d screen=%dx%d"
          % (config.SWIPE_INTERVAL_MIN, config.SWIPE_INTERVAL_MAX,
             config.PHONE_SCREEN_W, config.PHONE_SCREEN_H))
    print("travel_pct=%d start_pct=%d steps=%d step_ms=%d "
          "press_ms=%d release_ms=%d"
          % (config.SWIPE_TRAVEL_PCT, config.SWIPE_START_PCT,
             config.SWIPE_STEPS, config.SWIPE_STEP_MS,
             config.SWIPE_PRESS_MS, config.SWIPE_RELEASE_MS))
    print("profiles=%s"
          % ",".join(getattr(config, "SWIPE_PROFILES", {}).keys()))


def _cfg_set(args):
    """SET key value：改内存中的配置，立即生效（不写 flash，落盘用 SAVE）。
    键名或数值越界都返回 False。"""
    if len(args) < 2:
        return False
    key = args[0]
    try:
        v = int(args[1])
    except Exception:
        return False

    if key == "interval_min" and 0 <= v <= 300:
        config.SWIPE_INTERVAL_MIN = v
    elif key == "interval_max" and 0 <= v <= 300:
        config.SWIPE_INTERVAL_MAX = v
    elif key == "screen_w" and 100 <= v <= 4000:
        config.PHONE_SCREEN_W = v
    elif key == "screen_h" and 100 <= v <= 6000:
        config.PHONE_SCREEN_H = v
    else:
        for k, attr, lo, hi in GESTURE_LIMITS:
            if k == key:
                if not (lo <= v <= hi):
                    return False
                setattr(config, attr, v)
                return True
        return False

    # 顺序防护：串口也可能写成 min > max
    if config.SWIPE_INTERVAL_MIN > config.SWIPE_INTERVAL_MAX:
        config.SWIPE_INTERVAL_MIN, config.SWIPE_INTERVAL_MAX = \
            config.SWIPE_INTERVAL_MAX, config.SWIPE_INTERVAL_MIN
    return True


def _cfg_profile(name):
    """PROFILE 名称：套用 config.SWIPE_PROFILES 里的场景档案"""
    pf = getattr(config, "SWIPE_PROFILES", {}).get(name)
    if pf is None:
        return False
    for k, v in pf.items():
        if not _cfg_set([k, str(v)]):
            return False
    return True


def _cfg_save():
    """SAVE：写后备内存 + settings.json，掉电不丢"""
    try:
        from app.setting import save_settings
        save_settings()
        return True
    except Exception as e:
        print("保存失败:", e)
        return False


# ══════════════════ 手势 ══════════════════

def _release(h, x, y):
    """收尾：tip switch 抬起 → contact count 归零（与 swipe_up 一致）。
    收尾允许失败（手指可能已经"抬不起来"，尽力归零即可）"""
    try:
        h.send_touch(1, 0, x, y)
    except Exception:
        pass
    time.sleep_ms(20)
    try:
        h.send_touch(0, 0, x, y)
    except Exception:
        pass


def _st(h, contact, tip, x, y):
    """send_touch 的严格版：发送失败（返回 False）就抛错，
    让上层回 ERR 而不是静默吞掉——否则板子回 OK 手机却没反应，没法查。"""
    if not h.send_touch(contact, tip, x, y):
        raise OSError("触摸报文发送失败（手机可能未订阅通知/已断开）")


def _swipe(h, x1, y1, x2, y2, ms):
    """滑动：按 ms 时长插值，采样节奏保持在 BLE 连接间隔之上。
    SWIPE_HUMANIZE=True（默认）时与 swipe_up 同源：缓动 + 横向漂移 +
    节奏抖动，不再是机械直线。"""
    steps = max(3, min(30, ms // 30))
    step = max(30, ms // steps)
    hz = getattr(config, "SWIPE_HUMANIZE", True)
    speed = (0.5 + random.uniform(0, 0.5)) if hz else 0.0
    scr_w = config.PHONE_SCREEN_W
    _pi = 3.14159265
    _st(h, 1, 1, x1, y1)
    for i in range(1, steps + 1):
        t = i / steps
        p = (1 - math.cos(_pi * t)) / 2      # sin 缓动（起步慢→中段快→收尾慢）
        p = t + (p - t) * speed
        x = int(x1 + (x2 - x1) * p)
        y = int(y1 + (y2 - y1) * p)
        if hz:
            x += random.randint(-scr_w // 100, scr_w // 100)   # 落点横漂 ±1%
        _st(h, 1, 1, x, y)
        time.sleep_ms(step + (random.randint(0, 20) if hz else 0))
    _release(h, x2, y2)


def _bezier(h, x1, y1, cx, cy, x2, y2, ms):
    """二阶贝塞尔曲线滑动：控制点 (cx,cy) 决定弯曲方向，用于绕开
    直线轨迹表达不了的弧线手势（如"甩"出去的收尾）。"""
    steps = max(3, min(30, ms // 30))
    step = max(30, ms // steps)
    _st(h, 1, 1, x1, y1)
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        x = u * u * x1 + 2 * u * t * cx + t * t * x2
        y = u * u * y1 + 2 * u * t * cy + t * t * y2
        _st(h, 1, 1, int(x), int(y))
        time.sleep_ms(step)
    _release(h, x2, y2)


def _waypoints(h, pts, ms):
    """途经多点画轨迹（模拟手抖 / 绕弧）：每个点之间线性插值，
    步数按段长比例分配，整体耗时 ms。"""
    n = len(pts)
    seg = []
    total = 0.0
    for i in range(1, n):
        dx = pts[i][0] - pts[i - 1][0]
        dy = pts[i][1] - pts[i - 1][1]
        d = (dx * dx + dy * dy) ** 0.5
        seg.append(d)
        total += d
    if total <= 0:                       # 所有点重合，等于原地按下抬起
        _st(h, 1, 1, pts[0][0], pts[0][1])
        time.sleep_ms(ms)
        _release(h, pts[0][0], pts[0][1])
        return
    total_steps = max(n - 1, min(40, ms // 30))
    step_ms = max(30, ms // total_steps)
    _st(h, 1, 1, pts[0][0], pts[0][1])
    for i in range(1, n):
        seg_steps = max(1, round(total_steps * seg[i - 1] / total))
        for k in range(1, seg_steps + 1):
            t = k / seg_steps
            x = int(pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t)
            y = int(pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t)
            _st(h, 1, 1, x, y)
            time.sleep_ms(step_ms)
    _release(h, pts[-1][0], pts[-1][1])


def _print_status(h, ctl):
    text = None
    if ctl is not None:
        try:
            text = ctl.status_text()
        except Exception:
            text = None
    if text is None:
        try:
            conn = "已连接" if (h is not None and h.is_connected()) else "未连接"
        except Exception:
            conn = "未知"
        text = "BLE:%s" % conn
    print("[状态] %s" % text)


# ══════════════════ 指令分发 ══════════════════

def exec_line(h, line, ctl=None):
    """执行一条指令并打印应答（OK / ERR ...）。

    h    HIDTouch 实例；None 表示当前没有 BLE 句柄（只能跑配置类指令）
    ctl  提供 start() / stop() 的控制器；可选 status_text()
    """
    global _active
    p = line.split()
    if not p:
        return
    c = p[0]

    if c == "REMOTE":
        _active = True
        if ctl is not None:
            try:
                ctl.stop()          # 遥控期间别让自动滑动插进来
            except Exception:
                pass
        print("REMOTE ON")
        return

    if not _active:
        return                      # 握手前一律忽略（串口上可能有别的噪声）

    if c == "END":
        _active = False
        print("REMOTE OFF")
        return

    # ── 配置类：不要求蓝牙已连，也不打断运行 ──
    if c == "GET":
        _cfg_print()
        print("OK")
        return
    if c == "SET":
        print("OK" if _cfg_set(p[1:]) else "ERR 参数无效")
        return
    if c == "PROFILE":
        print("OK" if _cfg_profile(p[1] if len(p) > 1 else "")
              else "ERR 未知场景")
        return
    if c == "SAVE":
        print("OK" if _cfg_save() else "ERR 保存失败")
        return
    if c == "STATUS":
        _print_status(h, ctl)
        print("OK")
        return

    # ── 运行控制 ──
    if c == "STOP":
        if ctl is None:
            print("ERR 无自动滑动控制器")
            return
        ctl.stop()
        print("OK")
        return
    if c == "START":
        ok = False
        if ctl is not None:
            try:
                ok = ctl.start()
            except Exception:
                ok = False
        print("OK" if ok else "ERR ble未连接")
        return

    # ── 触摸类：必须有 BLE 且已连接 ──
    if h is None or not h.is_connected():
        print("ERR ble未连接")
        return
    try:
        if c == "T":                      # 单击；可选第 3 参数=按住毫秒（长按）
            x, y = int(p[1]), int(p[2])
            hold = int(p[3]) if len(p) > 3 else 70
            _st(h, 1, 1, x, y)
            time.sleep_ms(hold)
            if not h.send_touch(1, 0, x, y):      # tip switch 抬起
                raise OSError("触摸报文发送失败（手机可能未订阅通知/已断开）")
            time.sleep_ms(20)
            if not h.send_touch(0, 0, x, y):      # contact count 归零
                raise OSError("触摸报文发送失败（手机可能未订阅通知/已断开）")
        elif c == "P" or c == "M":        # 按下 / 移动（直线原始轨迹）
            _st(h, 1, 1, int(p[1]), int(p[2]))
        elif c == "R":                    # 抬起
            if not h.send_touch(1, 0, int(p[1]), int(p[2])):
                raise OSError("触摸报文发送失败（手机可能未订阅通知/已断开）")
            time.sleep_ms(20)
            if not h.send_touch(0, 0, int(p[1]), int(p[2])):
                raise OSError("触摸报文发送失败（手机可能未订阅通知/已断开）")
        elif c == "S":                    # 滑动（拟人化：缓动+漂移+节奏抖动）
            ms = int(p[5]) if len(p) > 5 else 300
            _swipe(h, int(p[1]), int(p[2]), int(p[3]), int(p[4]), ms)
        elif c == "W":                    # 途经多点轨迹：W x1 y1 x2 y2 ... [ms]
            a = [int(v) for v in p[1:]]
            ms = a.pop() if len(a) % 2 == 1 else 300
            if len(a) < 4:
                print("ERR 至少 2 个点")
                return
            pts = [(a[i], a[i + 1]) for i in range(0, len(a), 2)]
            _waypoints(h, pts, ms)
        elif c == "B":                    # 贝塞尔曲线：B x1 y1 cx cy x2 y2 [ms]
            a = [int(v) for v in p[1:]]
            ms = a.pop() if len(a) == 7 else 300
            if len(a) != 6:
                print("ERR 参数数量")
                return
            _bezier(h, a[0], a[1], a[2], a[3], a[4], a[5], ms)
        else:
            print("ERR ?%s" % c)
            return
        print("OK")
    except Exception as e:
        print("ERR %s" % e)


def poll(h=None, ctl=None):
    """主循环每轮调一次：读一行并执行。返回收到的原始行（没有则 None）"""
    line = read_line()
    if line is None:
        return None
    try:
        exec_line(h, line, ctl)
    except Exception as e:
        print("ERR %s" % e)
    return line
