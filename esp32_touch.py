#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PC → ESP32 → 手机 BLE 触摸遥控库
================================
电脑端脚本用它把点击/滑动指令经 USB 串口发给 ESP32，由 ESP32 以
蓝牙 HID 触摸的形式在手机上执行——系统层面等同真人手指（非输入注入）。

快速上手：
    from esp32_touch import RemoteTouch
    rt = RemoteTouch("COM18")             # 板子插在电脑上，已进入运行态
    rt.tap(540, 1400)                     # 单击（手机屏幕物理像素坐标）
    rt.swipe(540, 1400, 540, 600, 300)    # 上滑，300ms
    rt.press(300, 800)                    # 自由轨迹拖拽：
    rt.move(320, 810)                     #   按下 → 多次 move → 抬起
    rt.release(320, 810)
    rt.close()

图像识别联动（识别在 PC 端做，点击永远走 BLE）：
    截屏来源三选一：
      1) HDMI 采集卡 + cv2.VideoCapture  —— 零痕迹，最隐蔽
      2) scrcpy 投屏窗口 + mss 截窗口     —— 需开 ADB，但只投屏、无输入注入
      3) adb exec-out screencap -p       —— 只读 ADB，痕迹比 input 注入小得多
    模板匹配示例：
      import cv2
      img = <截屏 BGR 图>
      res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
      _, score, _, loc = cv2.minMaxLoc(res)
      if score > 0.8:
          rt.tap(loc[0] + tpl_w // 2, loc[1] + tpl_h // 2)

串口指令集（115200，行协议，坐标为手机屏幕物理像素）：
    REMOTE / END        进入 / 退出遥控模式
    T x y               单击
    P x y / M x y / R x y   按下 / 移动 / 抬起
    S x1 y1 x2 y2 [ms]  滑动（默认 300ms）
    STOP / START        停止 / 开始自动滑动
    GET                 读当前配置          → rt.get()
    SET key value       改配置，内存立即生效 → rt.set()
    PROFILE 名称        套用场景档案        → rt.profile()
    SAVE                配置落盘            → rt.save()
    STATUS              读一行状态          → rt.status()

状态语义：默认就是「运行态」——BLE 常开、等待启动，串口随时可用；
只有长按 BOOT 开了热点的「配置态」才必须关 BLE，那时串口指令也不处理。
"""
import math
import random
import time

import serial


class SwipeSim:
    """拇指 / 食指拟人滑动轨迹模拟器（PC 端计算，ESP 只按坐标执行）。

    单手模式（two_hand=False）：
        单手握机时是大拇指在划，拇指绕手掌转动 → 轨迹是一条
        **凸向持机手一侧的弧**，弧度大：起点偏向手侧 6~10% 屏宽，
        越往上越回正（像拇指从下方转到上方）。
    双手模式（two_hand=True）：
        食指划动，轨迹直得多：起点只偏 2~3.5%，弓高 1~2%。
    换手计时器：
        每 30~45 分钟随机取值；**计时未到，弧线方向（左/右手）保持
        不变**；到点后整条弧镜像到另一侧并重新随机计时，
        模拟左右手互换。参数 swap_min/swap_max 单位是分钟，
        测试时可改小（如 0.2~0.3）快速看到镜像效果。
    每次滑动的弓高、偏移、时长、节奏、末端落点都独立随机，
    两次滑动不会完全一样（不是固定的 S 形演示线）。
    """

    def __init__(self, two_hand=False, swap_min=30, swap_max=45):
        self.two_hand = two_hand
        self.swap_min = swap_min
        self.swap_max = swap_max
        self.side = random.choice((1, -1))   # 1=右手（弧凸向右），-1=左手
        self._t0 = time.time()
        self._swap_s = random.uniform(swap_min, swap_max) * 60

    @property
    def hand(self):
        return "右手" if self.side > 0 else "左手"

    def check_swap(self):
        """到点换手：弧线镜像到另一侧并重置随机计时。返回是否发生换手"""
        if time.time() - self._t0 >= self._swap_s:
            self.side = -self.side
            self._t0 = time.time()
            self._swap_s = random.uniform(self.swap_min, self.swap_max) * 60
            return True
        return False

    def arc_points(self, w, cx, y0, y1, ms, gap=0.04):
        """生成一次滑动的采样点 [(x, y), ...]。

        轨迹 = 二阶贝塞尔弧（起点偏手侧 → 控制点给弓高 → 终点回正），
        弧的形状由 side（左/右手）和 two_hand（拇指/食指）决定，
        每次调用都随机；时间轴上叠加缓动（慢-快-慢）与 ±2px 抖动。
        w=屏宽（弧度按比例取），ms=总时长，gap=采样间隔秒。
        """
        self.check_swap()
        s = self.side
        if self.two_hand:
            off0 = s * random.uniform(0.020, 0.035) * w    # 起点偏移
            bow = s * random.uniform(0.010, 0.022) * w     # 弓高（弧度）
        else:
            off0 = s * random.uniform(0.060, 0.100) * w
            bow = s * random.uniform(0.035, 0.060) * w
        off1 = s * random.uniform(-0.010, 0.010) * w       # 终点回正
        x0, xc, x1 = cx + off0, cx + bow, cx + off1
        y0 += random.randint(-10, 10)
        y1 += random.randint(-10, 10)
        n = max(10, int(ms / 1000 / gap))
        pts = []
        for i in range(n + 1):
            t = i / n
            e = (1 - math.cos(math.pi * t)) / 2   # 缓动：起步慢→中段快→收尾慢
            u = 1 - e
            x = u * u * x0 + 2 * u * e * xc + e * e * x1
            y = y0 + (y1 - y0) * e
            pts.append((int(x + random.randint(-2, 2)),
                        int(y + random.randint(-2, 2))))
        return pts


class RemoteTouch:
    """ESP32 串口遥控客户端。指令最小间隔 40ms，匹配 BLE 连接间隔。"""

    MIN_GAP = 0.04

    def __init__(self, port, baud=115200, timeout=3, debug=True):
        self.debug = debug
        self.ser = serial.Serial(port, baud, timeout=timeout)
        # 关键：pyserial 打开串口时 Windows 默认拉高 DTR/RTS，这块板的
        # 复位电路会被按住（芯片进复位/下载态、静默无输出）。被动释放
        # 不够，必须用与 esptool 相同的脉冲序列强制一次正常复位：
        self.ser.setDTR(False)           # IO0=高（正常启动，非下载模式）
        self.ser.setRTS(True)            # EN=低（按住复位）
        time.sleep(0.1)
        self.ser.setRTS(False)           # EN=高（释放，芯片重新启动）
        self._last = 0.0
        # 这块的复位电路不总是响应上面的脉冲，板子可能停在 REPL 没跑 main.py，
        # 所以不能死等固定 2 秒，改成"盯着串口等就绪"
        self._wait_ready()
        self._expect("REMOTE ON", "REMOTE")
        self._expect("OK", "STOP")       # 遥控前先停掉自动滑动，避免触摸冲突

    # ── 底层 ──

    def _wait_ready(self, timeout=20.0):
        """等板子进入"可以遥控"的状态再返回。

        两种情况都要处理（实测都遇到过）：
        1. 板子停在 REPL（>>>），main.py 没跑 —— DTR/RTS 复位脉冲不是每次
           都生效。这时发 Ctrl-D 软复位，让它重新跑 boot.py + main.py。
        2. 板子正在启动 —— 一直读到 webremote 打出的「串口遥控就绪」再动手，
           比死等固定秒数可靠。
        """
        deadline = time.time() + timeout
        pending = b""
        kicked = False
        while time.time() < deadline:
            n = self.ser.in_waiting
            if not n:
                time.sleep(0.1)
                continue
            chunk = self.ser.read(n)
            if self.debug:
                print("  .. %s" % chunk.decode("utf-8", "replace")
                      .replace("\r", "").strip())
            pending += chunk
            if len(pending) > 4096:
                pending = pending[-2048:]

            if b">>>" in pending and not kicked:
                # 板子在 REPL，没在跑主程序
                kicked = True
                if self.debug:
                    print("  [提示] 板子停在 REPL，发 Ctrl-D 软复位启动 main.py")
                self.ser.write(b"\x04")
                pending = b""
                time.sleep(2.5)
                continue

            if "串口遥控就绪".encode("utf-8") in pending:
                if self.debug:
                    print("  [提示] 板子已就绪")
                self.ser.reset_input_buffer()
                return True
        if self.debug:
            print("  [提示] %.0f 秒内没等到就绪行，继续尝试握手" % timeout)
        self.ser.reset_input_buffer()
        return False

    def _send(self, s):
        gap = self.MIN_GAP - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self.ser.write((s + "\n").encode())
        self.ser.flush()
        self._last = time.time()

    @staticmethod
    def _dump(lines):
        """超时/失败时把板子说过的话全打出来。
        用来一眼分辨：「板子完全没响应」（端口不对/程序没跑）
        还是「板子在跑别的程序」（比如停在 OLED 菜单里，没人接管串口）"""
        if not lines:
            print("  [诊断] 串口上没收到任何数据 —— 端口不对？板子没跑？波特率不对？")
            return
        print("  [诊断] 板子最近输出 %d 行：" % len(lines))
        for i, s in enumerate(lines, 1):
            print("    %2d| %s" % (i, s))

    def _expect(self, want, cmd, tries=10):
        """发指令并等指定应答（只用于 REMOTE / END 这类握手行）。
        跳过板子的状态打印/心跳行；超时时把板子说过的所有行都打印出来。
        debug=True 时逐行回显收发内容（诊断黑箱问题用）"""
        last = ""
        seen = []
        for _ in range(tries):
            if self.debug:
                print("  >> %s" % cmd)
            self._send(cmd)
            deadline = time.time() + 1.0
            while True:
                line = self.ser.readline().decode("utf-8", "replace").strip()
                if not line:
                    if time.time() >= deadline:
                        break
                    continue
                if self.debug:
                    print("  << %s" % line)
                if line == want:
                    return True
                last = line
                seen.append(line)
                if len(seen) > 60:
                    seen.pop(0)
                if time.time() >= deadline:
                    break
        self._dump(seen)
        raise TimeoutError("ESP32 未应答 %r；板子最后应答: %s"
                           % (want, last or "(无任何输出)"))

    def _collect(self, cmd, tries=10, timeout=1.0):
        """发一条指令并收集回显行，直到板子给出 OK / ERR。

        返回 (ok, lines, err)：
            ok     True=板子回 OK，False=回 ERR
            lines  OK/ERR 之前的输出行（GET / STATUS 的内容在这里）
            err    ERR 后面的原因文本，无则 ""
        好处是能直接拿到板子的失败原因（如「ble未连接」），
        不用干等十次重试才知道结果。"""
        last = ""
        seen = []
        for _ in range(tries):
            lines = []
            if self.debug:
                print("  >> %s" % cmd)
            self._send(cmd)
            deadline = time.time() + timeout
            while True:
                line = self.ser.readline().decode("utf-8", "replace").strip()
                if not line:
                    if time.time() >= deadline:
                        break
                    continue
                if self.debug:
                    print("  << %s" % line)
                if line == "OK":
                    return True, lines, ""
                if line.startswith("ERR"):
                    return False, lines, line[3:].strip()
                lines.append(line)
                last = line
                seen.append(line)
                if len(seen) > 60:
                    seen.pop(0)
                if time.time() >= deadline:
                    break
        self._dump(seen)
        raise TimeoutError("ESP32 未应答 %r；板子最后应答: %s"
                           % (cmd, last or "(无任何输出)"))

    def _ok(self, cmd):
        """发一条 OK/ERR 协议的指令；失败时把板子给的原因抛出来"""
        ok, lines, err = self._collect(cmd)
        if not ok:
            raise RuntimeError("指令 %r 失败：%s" % (cmd, err or "未知原因"))
        return lines

    # ── 动作 ──

    def tap(self, x, y, ms=70):
        """单击；ms = 按住时长，>200ms 相当于长按"""
        self._ok("T %d %d %d" % (x, y, ms))

    def press(self, x, y):
        """按下（开始拖拽）"""
        self._ok("P %d %d" % (x, y))

    def move(self, x, y):
        """移动（拖拽中，调用间隔 >= 40ms）"""
        self._ok("M %d %d" % (x, y))

    def release(self, x, y):
        """抬起（结束拖拽）"""
        self._ok("R %d %d" % (x, y))

    def swipe(self, x1, y1, x2, y2, ms=300):
        """从 (x1,y1) 滑到 (x2,y2)，耗时 ms 毫秒。
        板端自带拟人化（缓动+横漂+节奏抖动），不是机械直线"""
        self._ok("S %d %d %d %d %d" % (x1, y1, x2, y2, ms))

    def play(self, pts, gap=0.04):
        """把 PC 端算好的轨迹点流式发给板子（ESP 不做任何计算）：
        按下首点 → 逐点 M → 末点抬起。gap = 点间隔秒（约 25 点/秒，
        与 BLE 连接间隔匹配）。"""
        old = self.MIN_GAP
        self.MIN_GAP = gap
        try:
            x, y = pts[0]
            self._ok("P %d %d" % (x, y))
            for x, y in pts[1:-1]:
                self._ok("M %d %d" % (x, y))
            x, y = pts[-1]
            self._ok("R %d %d" % (x, y))
        finally:
            self.MIN_GAP = old

    def swipe_sim(self, sim, w, cx, y0, y1, ms=700, gap=0.04):
        """用 SwipeSim 生成弧线轨迹并流式执行（见 SwipeSim 说明）"""
        self.play(sim.arc_points(w, cx, y0, y1, ms, gap), gap)

    def waypoints(self, pts, ms=300):
        """途经多点画轨迹（模拟手抖 / 绕弧）。
        pts = [(x1,y1), (x2,y2), ...]，至少 2 个点"""
        a = []
        for x, y in pts:
            a += [str(int(x)), str(int(y))]
        self._ok("W " + " ".join(a) + " %d" % ms)

    def bezier(self, x1, y1, cx, cy, x2, y2, ms=300):
        """二阶贝塞尔曲线滑动：控制点 (cx,cy) 决定弯曲方向。
        例：向右上滑但收尾向右甩 → bezier(540,1400, 700,900, 560,576)"""
        self._ok("B %d %d %d %d %d %d %d"
                 % (x1, y1, cx, cy, x2, y2, ms))

    def stop_auto(self):
        """停止自动滑动（避免与遥控手势冲突）"""
        self._ok("STOP")

    def start_auto(self):
        """恢复自动滑动（蓝牙必须已连上，否则板子回 ERR ble未连接）"""
        self._ok("START")

    # ── 配置（不要求蓝牙已连，改完立即生效，不打断运行）──

    def get(self):
        """读当前配置，返回 dict（值保持字符串，便于原样查看）"""
        cfg = {}
        for line in self._ok("GET"):
            for kv in line.split():
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    cfg[k] = v
        return cfg

    def set(self, key, value):
        """改一项配置（内存立即生效，掉电保存要再调 save()）。
        键名见 get()：interval_min / interval_max / screen_w / screen_h /
        travel_pct / start_pct / steps / step_ms / press_ms / release_ms"""
        self._ok("SET %s %s" % (key, value))
        return True

    def profile(self, name):
        """套用场景档案：短视频 / 长视频 / 看小说 / 快速划"""
        self._ok("PROFILE %s" % name)
        return True

    def save(self):
        """把当前配置写进后备内存 + settings.json（掉电不丢）"""
        self._ok("SAVE")
        return True

    def status(self):
        """读一行板子状态，返回字符串"""
        return "\n".join(self._ok("STATUS"))

    def close(self):
        try:
            self._send("END")
        except Exception:
            pass
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else "COM18"
    print("连接 %s ..." % port)

    results = []          # (测试名, 是否通过)

    def run(name, fn):
        """跑一项测试：报错记 FAIL 但不中断后续测试"""
        print("\n--- 测试: %s ---" % name)
        try:
            fn()
            print("  [PASS] %s" % name)
            results.append((name, True))
        except Exception as e:
            print("  [FAIL] %s -> %s" % (name, e))
            results.append((name, False))

    def wait_ble(rt, timeout=60):
        """等手机连上 BLE；超时返回 False"""
        print("等待手机连接 esp-bot ...（手机蓝牙列表里点它）")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if "BLE:已连接" in rt.status():
                print("蓝牙已连接")
                return True
            time.sleep(1)
        return False

    with RemoteTouch(port) as rt:
        # ════ 配置类 API（不需要蓝牙）════
        run("status() 读状态", lambda: rt.status())

        cfg = rt.get()
        w, h = (int(v) for v in cfg.get("screen", "1080x1920").split("x"))

        def t_set_get():
            old = rt.get().get("interval_min")
            rt.set("interval_min", "7")
            got = rt.get().get("interval_min")
            assert got == "7", "SET 未生效，读到 %s" % got
            rt.set("interval_min", old or "10")     # 改回原值
        run("set()/get() 读写校验", t_set_get)

        run("profile('短视频')", lambda: rt.profile("短视频"))
        run("save() 配置落盘", lambda: rt.save())

        # ════ 触摸类 API（需要蓝牙已连接）════
        cx = w // 2
        y0 = h * int(cfg.get("start_pct", "70")) // 100
        y1 = y0 - h * int(cfg.get("travel_pct", "40")) // 100

        if wait_ble(rt):
            print("\n提示：把手机停在一个可以滑动的界面（短视频/列表），"
                  "以下动作肉眼应可见\n")
            time.sleep(2)

            run("tap() 单击（看屏幕中央）", lambda: rt.tap(cx, h // 2))
            time.sleep(1.5)
            run("tap(ms=500) 长按", lambda: rt.tap(cx, h // 2, 500))
            time.sleep(1.5)

            def t_drag():
                """慢速大步长拖拽：150ms 一步，肉眼可见手指移动"""
                rt.press(cx, h - 500)
                for i in range(1, 6):
                    time.sleep(0.15)
                    rt.move(cx, h - 500 - i * 200)
                time.sleep(0.15)
                rt.release(cx, h - 1500)
            run("press/move/release 慢速拖拽", t_drag)
            time.sleep(1.5)

            def t_swipe():
                rt.swipe(cx, y0, cx, y1, 900)    # 板端 S 指令（API 覆盖用）
            run("swipe() 板端拟人化（API 测试）", t_swipe)
            time.sleep(1)

            # ── 新算法：PC 端弧线轨迹 + 流式执行（ESP 只按坐标操作）──
            sim1 = SwipeSim(two_hand=False)      # 单手拇指：弧度大
            def t_thumb():
                for i in range(4):
                    if sim1.check_swap():
                        print("    [换手] 弧线镜像 → %s" % sim1.hand)
                    rt.swipe_sim(sim1, w, cx, y0, y1,
                                 ms=random.randint(600, 900))
                    print("    拇指弧线第 %d 次（%s）" % (i + 1, sim1.hand))
                    time.sleep(2)
            run("单手拇指弧线 x4", t_thumb)
            time.sleep(1)

            sim2 = SwipeSim(two_hand=True)       # 双手食指：弧度小
            def t_index():
                for i in range(3):
                    rt.swipe_sim(sim2, w, cx, y0, y1,
                                 ms=random.randint(500, 800))
                    print("    食指微弧第 %d 次（%s）" % (i + 1, sim2.hand))
                    time.sleep(2)
            run("双手食指微弧 x3", t_index)

            def t_waypoints():
                pts = [(cx, y0),
                       (cx + 60, (y0 + y1) // 2),
                       (cx - 60, y1 + 200),
                       (cx, y1)]
                rt.waypoints(pts, 1200)
            run("waypoints() 多点轨迹（1.2 秒）", t_waypoints)
            time.sleep(1.5)

            run("bezier() 曲线滑动（1.2 秒）",
                lambda: rt.bezier(cx, y0, cx + 200, (y0 + y1) // 2,
                                  cx, y1, 1200))
            time.sleep(1.5)

            def t_auto():
                rt.start_auto()      # 开始自动滑动（板子立即滑第一次）
                time.sleep(5)        # 滑一两次观察
                rt.stop_auto()
            run("start_auto()/stop_auto()", t_auto)
        else:
            print("60 秒内蓝牙未连接，触摸类测试全部跳过")
            for n in ("tap", "tap(长按)", "press/move/release",
                      "swipe", "waypoints", "bezier",
                      "start_auto/stop_auto"):
                results.append((n + "(未测)", False))

    print("\n" + "=" * 40)
    print(" 测试结果汇总")
    print("=" * 40)
    for name, good in results:
        print("  [%s] %s" % ("PASS" if good else "FAIL", name))
    ok = sum(1 for _, good in results if good)
    print("-" * 40)
    print(" 通过 %d / %d" % (ok, len(results)))
