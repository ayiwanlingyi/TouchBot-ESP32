"""
刷视频 — BLE 触摸屏翻页（ESP32 + SSD1306 128x64 版）
  - 可配置滑动间隔和手机屏幕分辨率（从 config.py 读取）
  - 滑动轨迹参数化（SWIPE_TRAVEL_PCT 等），真机可调
  - 自动模式对 interval min/max 做排序防护，避免设置越界时崩溃
BLE 协议与手势部分与 ESP32 版完全一致，仅显示层改为 I2C OLED。
"""
from core.hardware import oled, btn_next, btn_sel, btn_back
from drivers.ssd1306 import SSD1306
from app.uartcmd import poll          # PC 串口遥控（有屏模式同样可用）
import config
import time, random, gc, struct, math

try:
    import bluetooth
except ImportError:
    bluetooth = None

BLACK = SSD1306.BLACK
WHITE = SSD1306.WHITE

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_ENCRYPTION_UPDATE = 28
_IRQ_GET_SECRET = 29
_IRQ_SET_SECRET = 30
_IRQ_PASSKEY_ACTION = 31


class HIDTouch:
    def __init__(self):
        self._ble = None
        self._conn = None
        self._rp = None
        self._cccd = None
        self._connected = False
        self.init_ok = False
        self._err = ""
        if bluetooth is None:
            self._err = "no bt module"
            return
        try:
            self._init()
            self.init_ok = True
        except Exception as e:
            self._err = str(e)
            import sys
            sys.print_exception(e)

    def _init(self):
        gc.collect()
        # 无屏模式（网页控制台）必须保留 WiFi，否则网页连不上
        keep_wifi = (getattr(config, "KEEP_WIFI", False)
                     or getattr(config, "HEADLESS", False))
        if not keep_wifi:
            # BLE 与 WiFi 共存受限，进入刷视频前先关闭 STA
            try:
                import network
                sta = network.WLAN(network.STA_IF)
                if sta.isconnected():
                    sta.disconnect()
                sta.active(False)
            except Exception:
                pass
        gc.collect()
        time.sleep_ms(500)

        self._ble = bluetooth.BLE()
        for i in range(3):
            try:
                self._ble.active(True)
                break
            except OSError:
                if i < 2:
                    time.sleep_ms(500)
                    gc.collect()
                else:
                    raise
        time.sleep_ms(200)

        try:
            self._ble.config(rxbuf=200)
            self._ble.config(mtu=185)
        except Exception:
            pass

        try:
            self._ble.config(bond=True)
            self._ble.config(mitm=False)
            self._ble.config(le_secure=False)
            self._ble.config(io=3)
        except Exception:
            pass

        self._ble.irq(self._irq)

        # HID 触摸报告描述符
        rp = bytes([
            0x05, 0x0D, 0x09, 0x04, 0xA1, 0x01, 0x09, 0x54, 0x15, 0x00, 0x25, 0x01,
            0x75, 0x08, 0x95, 0x01, 0x81, 0x02, 0x09, 0x55, 0x15, 0x00, 0x25, 0x01,
            0x75, 0x08, 0x95, 0x01, 0x81, 0x02, 0x05, 0x0D, 0x09, 0x22, 0xA1, 0x02,
            0x09, 0x51, 0x15, 0x00, 0x25, 0x01, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
            0x09, 0x42, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02,
            0x75, 0x07, 0x95, 0x01, 0x81, 0x03, 0x05, 0x01, 0x09, 0x30, 0x15, 0x00,
            0x26, 0xFF, 0x7F, 0x35, 0x00, 0x46, 0xFF, 0x7F, 0x65, 0x11, 0x55, 0x00,
            0x75, 0x10, 0x95, 0x01, 0x81, 0x02, 0x09, 0x31, 0x15, 0x00, 0x26, 0xFF,
            0x7F, 0x35, 0x00, 0x46, 0xFF, 0x7F, 0x65, 0x11, 0x55, 0x00, 0x75, 0x10,
            0x95, 0x01, 0x81, 0x02, 0xC0, 0xC0])

        _CCCD = (bluetooth.UUID(0x2902),
                 bluetooth.FLAG_READ | bluetooth.FLAG_WRITE)
        h = self._ble.gatts_register_services([
            (bluetooth.UUID(0x1812), [
                (bluetooth.UUID(0x2A4A), bluetooth.FLAG_READ),
                (bluetooth.UUID(0x2A4B), bluetooth.FLAG_READ),
                (bluetooth.UUID(0x2A4C), bluetooth.FLAG_WRITE),
                (bluetooth.UUID(0x2A4D),
                 bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY, (_CCCD,)),
                (bluetooth.UUID(0x2A4E),
                 bluetooth.FLAG_READ | bluetooth.FLAG_WRITE_NO_RESPONSE)])])
        sv = h[0]
        self._rp = sv[3]
        self._cccd = sv[4]

        self._ble.gatts_set_buffer(self._rp, 8)

        self._ble.gatts_write(sv[1], rp)
        self._ble.gatts_write(sv[0], struct.pack('<HBB', 0x0111, 0, 0))
        self._ble.gatts_write(sv[5], struct.pack('B', 1))
        self._ble.gatts_write(self._rp, bytearray(8))
        self._ble.gatts_write(self._cccd, struct.pack('<H', 0))

        # 广播数据：完整本地名 + HID 服务标志 + 外观（HID 触摸板）
        n = b"esp-bot"
        a = bytearray()
        a.append(2); a.append(1); a.append(6)
        a.append(len(n) + 1); a.append(9); a.extend(n)
        a.extend(b"\x03\x03\x12\x18")
        a.extend(b"\x03\x19\xC3\x03")
        self._adv_data = a
        self._ble.gap_advertise(100, a)

    def _irq(self, e, d):
        if e == _IRQ_CENTRAL_CONNECT:
            self._conn = d[0]
            self._connected = True
        elif e == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            self._connected = False
            try:
                self._ble.gap_advertise(100, self._adv_data)
            except Exception:
                pass
        elif e == _IRQ_GATTS_WRITE:
            try:
                attr_handle = d[1]
                if self._cccd is not None and attr_handle == self._cccd:
                    v = self._ble.gatts_read(self._cccd)
                    print("BLE 订阅 CCCD =", list(v))
            except Exception:
                pass
        elif e == _IRQ_ENCRYPTION_UPDATE:
            try:
                conn, enc, auth, bond, ks = d
                print("BLE 加密状态: enc=%s auth=%s bond=%s ks=%s"
                      % (enc, auth, bond, ks))
            except Exception:
                pass
        elif e == _IRQ_PASSKEY_ACTION:
            try:
                self._ble.gap_passkey(d[0], d[1], d[2])
            except Exception:
                pass
        elif e == _IRQ_SET_SECRET:
            return True
        elif e == _IRQ_GET_SECRET:
            return None

    def is_connected(self):
        return self._connected

    def deinit(self):
        try:
            if self._ble is not None:
                self._ble.active(False)
                time.sleep_ms(500)
                self._ble = None
        except Exception:
            pass
        self._connected = False
        gc.collect()

    def send_touch(self, c, tip, x, y):
        """发送触摸坐标，x/y 为手机屏幕像素坐标，自动映射到 HID 0-32767"""
        if not self._connected or self._rp is None:
            return False
        try:
            scr_w = config.PHONE_SCREEN_W
            scr_h = config.PHONE_SCREEN_H
            hx = int(x * 32767 / scr_w)
            hy = int(y * 32767 / scr_h)
            r = struct.pack('BBBBHH', c, 1, 0, tip, hx, hy)
            self._ble.gatts_write(self._rp, r)
            self._ble.gatts_notify(self._conn, self._rp)
            return True
        except Exception:
            return False

    def swipe_up(self):
        """
        向上滑动一次（拟人化轨迹）。
        行程：起点 SWIPE_START_PCT% 屏高 → 上移 SWIPE_TRAVEL_PCT%。
        拟人化（config.SWIPE_HUMANIZE）：每次滑动的水平落点、行程长短、
        速度曲线（sin 缓动与匀速按随机权重混合 → 随机加速度）、
        按压时长与采样节奏都有随机抖动，模拟真人手势。
        报文间隔 >= 30ms（匹配 BLE 连接间隔，避免 notify 被覆盖丢弃）。
        """
        scr_w = config.PHONE_SCREEN_W
        scr_h = config.PHONE_SCREEN_H
        hz = getattr(config, "SWIPE_HUMANIZE", True)    # 拟人化开关

        cx = scr_w // 2
        y0 = scr_h * config.SWIPE_START_PCT // 100
        travel = scr_h * config.SWIPE_TRAVEL_PCT // 100
        if hz:
            cx += random.randint(-scr_w // 25, scr_w // 25)      # 落点横移 ±4%
            travel = travel * random.randint(90, 110) // 100     # 行程 ±10%
        y1 = y0 - travel

        # 按下并保持，让手机确认这是一次有效拖拽
        press = config.SWIPE_PRESS_MS
        if hz:
            press = max(20, press + random.randint(-15, 25))
        self.send_touch(1, 1, cx, y0)
        time.sleep_ms(press)

        steps = config.SWIPE_STEPS
        base = config.SWIPE_STEP_MS
        # 随机加速度：sin 缓动（起步慢→中段快→收尾慢）与匀速按随机权重
        # 混合，每次快慢节奏都不同；speed=0 即退回原匀速轨迹
        speed = (0.5 + random.uniform(0, 0.5)) if hz else 0.0
        _pi = 3.14159265
        for i in range(1, steps + 1):
            t = i / steps
            p = (1 - math.cos(_pi * t)) / 2
            p = t + (p - t) * speed
            ny = y0 - int(travel * p)
            if hz:
                # 手指横向轻微漂移 + 每步节奏抖动（保持 >=30ms）
                self.send_touch(1, 1,
                                cx + random.randint(-scr_w // 100,
                                                    scr_w // 100), ny)
                time.sleep_ms(base + random.randint(0, 20))
            else:
                self.send_touch(1, 1, cx, ny)
                time.sleep_ms(base)

        # 到达终点稍作停顿再抬手，保证完整的手势结束时序
        time.sleep_ms(20)
        self.send_touch(1, 0, cx, y1)
        time.sleep_ms(config.SWIPE_RELEASE_MS)
        self.send_touch(0, 0, cx, y1)
        return True


# ══ UI 部分（SSD1306 128x64）══
# 8x8 字体，每行 16 字符；可用行 Y：14 / 25 / 36 / 47 / 56
TITLE_H     = 11
TITLE_TXT_Y = 2
R_RES       = 14   # 手机分辨率
R_BLE       = 25   # BLE 连接状态
R_INT       = 36   # 滑动间隔
R_MSG1      = 47   # 状态 / 提示 1
R_MSG2      = 56   # 提示 2


def _clear_row(y):
    oled.fillrect((0, y), (128, 11), BLACK)


def _draw_title():
    oled.fill(BLACK)
    oled.fillrect((0, 0), (128, TITLE_H), WHITE)
    oled.text((4, TITLE_TXT_Y), "SWIPE VIDEO", BLACK)
    oled.text((100, TITLE_TXT_Y), "v2", BLACK)
    oled.show()


def _line(y, s, show=True):
    _clear_row(y)
    oled.text((0, y), s, WHITE)
    if show:
        oled.show()


def _draw_resolution():
    _line(R_RES, "Phone:%dx%d" % (config.PHONE_SCREEN_W,
                                  config.PHONE_SCREEN_H))


def _draw_status(connected):
    _line(R_BLE, "BLE:Connected" if connected else "BLE:Waiting..")


def _draw_interval():
    _line(R_INT, "Int:%d-%ds" % (config.SWIPE_INTERVAL_MIN,
                                 config.SWIPE_INTERVAL_MAX))


def _draw_idle_hints():
    _clear_row(R_MSG1)
    oled.text((0, R_MSG1), "SEL: Start/Stop", WHITE)
    _clear_row(R_MSG2)
    oled.text((0, R_MSG2), "BACK: Exit", WHITE)
    oled.show()


def _draw_auto_header(cnt, wait):
    _clear_row(R_MSG1)
    oled.text((0, R_MSG1), "Auto Cnt:%d" % cnt, WHITE)
    _clear_row(R_MSG2)
    oled.text((0, R_MSG2), "W:%ds SEL:Stop" % wait, WHITE)
    oled.show()


# ── 主入口 ──
def run():
    _draw_title()
    _draw_resolution()
    _draw_status(False)

    h = HIDTouch()
    try:
        if not h.init_ok:
            _clear_row(R_INT)
            oled.text((0, R_INT), "BLE Error!", WHITE)
            _clear_row(R_MSG1)
            oled.text((0, R_MSG1), h._err[:16], WHITE)
            _clear_row(R_MSG2)
            oled.text((0, R_MSG2), "Btn: exit", WHITE)
            oled.show()
            while not (btn_next.was_pressed() or btn_sel.was_pressed()):
                time.sleep_ms(100)
            return

        _draw_interval()
        _line(R_MSG1, "BLE ready")

        auto = False
        _idle_drawn = False
        _last_conn = None

        # 串口遥控控制器：START / STOP 直接翻转这里的 auto 标志
        class _Ctl:
            def start(self):
                nonlocal auto
                if h.is_connected():
                    auto = True
                    return True
                return False

            def stop(self):
                nonlocal auto
                auto = False
                return True

            def status_text(self):
                return "有屏 | BLE:%s | %s" % (
                    "已连接" if h.is_connected() else "未连接",
                    "滑动中" if auto else "已停止")

        ctl = _Ctl()

        while True:
            poll(h, ctl)                # PC 串口遥控（与无屏模式同一套指令）
            _conn = h.is_connected()
            if _conn != _last_conn:
                _draw_status(_conn)
                _last_conn = _conn

            if auto and h.is_connected():
                _idle_drawn = False
                cnt = 0
                _nxt_exit = False
                _last_cnt = -1
                _last_s = -1
                _draw_auto_header(cnt, 0)
                _last_cnt = cnt
                _last_s = 0

                while True:
                    if btn_sel.was_pressed():
                        auto = False
                        break
                    if btn_next.was_pressed() or btn_back.was_pressed():
                        auto = False
                        _nxt_exit = True
                        break
                    if not h.is_connected():
                        auto = False
                        break
                    h.swipe_up()
                    cnt += 1
                    # 排序防护：设置界面可能保存 min > max
                    lo, hi = sorted((config.SWIPE_INTERVAL_MIN,
                                     config.SWIPE_INTERVAL_MAX))
                    wait = random.randint(lo, hi)
                    for s in range(wait, 0, -1):
                        if btn_sel.was_pressed():
                            auto = False
                            break
                        if btn_next.was_pressed() or btn_back.was_pressed():
                            auto = False
                            _nxt_exit = True
                            break
                        if not h.is_connected():
                            auto = False
                            break
                        # 每秒拆成 10 × 100ms，串口指令最多等 100ms 就被处理
                        for _t in range(10):
                            poll(h, ctl)
                            if btn_sel.was_pressed():
                                auto = False
                                break
                            if btn_next.was_pressed() or btn_back.was_pressed():
                                auto = False
                                _nxt_exit = True
                                break
                            if not h.is_connected():
                                auto = False
                                break
                            time.sleep_ms(100)
                        if not auto:
                            break
                        if cnt != _last_cnt or s != _last_s:
                            _draw_auto_header(cnt, s)
                            _last_cnt = cnt
                            _last_s = s
                    if not auto:
                        break

                if _nxt_exit:
                    return
                if not h.is_connected():
                    auto = False
                    _idle_drawn = False
            else:
                if not _idle_drawn:
                    _draw_idle_hints()
                    _idle_drawn = True

            if btn_sel.was_pressed():
                if h.is_connected():
                    auto = not auto
                    _idle_drawn = False
                else:
                    return

            # BACK / NXT 退出刷视频，返回主菜单
            if btn_back.was_pressed() or btn_next.was_pressed():
                return

            # 长按 SEL 退出
            if btn_sel.is_pressed():
                t0 = time.ticks_ms()
                while btn_sel.is_pressed():
                    if time.ticks_diff(time.ticks_ms(), t0) >= 2000:
                        return
                    time.sleep_ms(20)
                btn_sel.was_pressed()

            time.sleep_ms(50)
    finally:
        h.deinit()
