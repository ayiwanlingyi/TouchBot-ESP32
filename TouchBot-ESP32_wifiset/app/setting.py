"""
设置界面 — 滑动间隔 / 手机分辨率配置（SSD1306 128x64 版）
持久化到 settings.json，内存写入 config 模块

布局：128x64 / 8x8 字体
    0..10    标题栏（反白，右侧显示 BACK 提示）
    14/25/36 三个设置项（选中项反白）
    47       当前数值
    56       按键提示 / 保存反馈

按键：
    PREV / NEXT  非编辑=上下移动光标，编辑=-1 / +1
    SEL          进入 / 退出编辑；长按=保存退出
    BACK         保存并退出
"""
from core.hardware import oled, btn_sel, btn_next, btn_prev, btn_back
from drivers.ssd1306 import SSD1306
import config
import time, gc, struct

BLACK = SSD1306.BLACK
WHITE = SSD1306.WHITE

# ── 持久化 ──
# 主存储：machine.mem_backup()（v1.29+，ESP32 上 = 2048 字节 RTC 慢速内存）
#   · 改的是内存，不擦写 flash，没有寿命问题，读写也快
#   · ⚠️ ESP32 的 RTC 内存【没有电池后备】：掉电、按 EN/RESET 键都会丢
#     （官方文档 esp32 行 Battery-backed = no）
# 所以默认同时镜像一份到 settings.json，保证掉电后仍能恢复。
# 想要"纯内存"就把 MIRROR_TO_FLASH 改 False（掉电会回到默认参数）。
SETTINGS_FILE   = "settings.json"
MIRROR_TO_FLASH = True
_MAGIC          = 0xA55A      # 有效数据标志
_FMT            = "<HHHHHH"   # magic, min, max, w, h, checksum
_NBYTES         = 12


def _mem():
    """取得后备内存 memoryview；固件不支持时返回 None"""
    try:
        import machine
        return machine.mem_backup()
    except Exception:
        return None


def _raw_read(mem, nbytes):
    """按 itemsize(1 或 4) 读出 nbytes 字节；容量不足返回 None"""
    itemsize = mem.itemsize
    nwords = (nbytes + itemsize - 1) // itemsize
    if nwords > len(mem):
        return None
    buf = bytearray()
    for i in range(nwords):
        buf += mem[i].to_bytes(itemsize, "little")
    return bytes(buf[:nbytes])


def _raw_write(mem, data):
    """按 itemsize(1 或 4) 写入任意字节；容量不足返回 False"""
    itemsize = mem.itemsize
    pad = bytearray(data)
    while len(pad) % itemsize:
        pad.append(0)
    nwords = len(pad) // itemsize
    if nwords > len(mem):
        return False
    for i in range(nwords):
        mem[i] = int.from_bytes(pad[i * itemsize:(i + 1) * itemsize], "little")
    return True


def _read_backup():
    """后备内存 -> dict；无有效数据返回 None"""
    mem = _mem()
    if mem is None:
        return None
    try:
        raw = _raw_read(mem, _NBYTES)
        if raw is None:
            return None
        magic, vmin, vmax, w, h, chk = struct.unpack(_FMT, raw)
        if magic != _MAGIC:
            return None
        # 校验和
        if (magic + vmin + vmax + w + h) & 0xFFFF != chk:
            return None
        # 合理性检查，防止读到别人写的乱码
        if not (0 <= vmin <= 300 and 0 <= vmax <= 300
                and 100 <= w <= 4000 and 100 <= h <= 6000):
            return None
        return {"interval_min": vmin, "interval_max": vmax,
                "screen_w": w, "screen_h": h}
    except Exception:
        return None


def _write_backup(d):
    """dict -> 后备内存；成功返回 True"""
    mem = _mem()
    if mem is None:
        return False
    try:
        vals = (d["interval_min"], d["interval_max"],
                d["screen_w"], d["screen_h"])
        chk = (_MAGIC + sum(vals)) & 0xFFFF
        raw = struct.pack(_FMT, _MAGIC, vals[0], vals[1], vals[2], vals[3], chk)
        return _raw_write(mem, raw)
    except Exception:
        return False


def _current_dict():
    return {
        "interval_min": config.SWIPE_INTERVAL_MIN,
        "interval_max": config.SWIPE_INTERVAL_MAX,
        "screen_w": config.PHONE_SCREEN_W,
        "screen_h": config.PHONE_SCREEN_H,
        "travel_pct": getattr(config, "SWIPE_TRAVEL_PCT", 40),
        "start_pct": getattr(config, "SWIPE_START_PCT", 70),
        "steps": getattr(config, "SWIPE_STEPS", 10),
        "step_ms": getattr(config, "SWIPE_STEP_MS", 30),
        "press_ms": getattr(config, "SWIPE_PRESS_MS", 80),
        "release_ms": getattr(config, "SWIPE_RELEASE_MS", 40),
    }


def _apply_dict(d):
    """把 dict 写进 config，并钳制非法组合"""
    if "interval_min" in d:
        config.SWIPE_INTERVAL_MIN = int(d["interval_min"])
    if "interval_max" in d:
        config.SWIPE_INTERVAL_MAX = int(d["interval_max"])
    if "screen_w" in d:
        config.PHONE_SCREEN_W = int(d["screen_w"])
    if "screen_h" in d:
        config.PHONE_SCREEN_H = int(d["screen_h"])
    # 手势参数（settings.json / 场景档案），带范围钳制
    for key, attr, lo, hi in (
            ("travel_pct",  "SWIPE_TRAVEL_PCT",  20, 90),
            ("start_pct",   "SWIPE_START_PCT",   30, 95),
            ("steps",       "SWIPE_STEPS",        5, 25),
            ("step_ms",     "SWIPE_STEP_MS",     30, 120),
            ("press_ms",    "SWIPE_PRESS_MS",    50, 300),
            ("release_ms",  "SWIPE_RELEASE_MS",  20, 200)):
        if key in d:
            v = int(d[key])
            if lo <= v <= hi:
                setattr(config, attr, v)
    # 兜底：可能保存过 min > max 的非法组合
    if config.SWIPE_INTERVAL_MIN > config.SWIPE_INTERVAL_MAX:
        config.SWIPE_INTERVAL_MIN, config.SWIPE_INTERVAL_MAX = \
            config.SWIPE_INTERVAL_MAX, config.SWIPE_INTERVAL_MIN


def load_settings():
    """优先读后备内存（间隔/分辨率），再合并 settings.json（含手势参数）"""
    d = _read_backup()
    src = "后备内存"
    try:
        import json
        with open(SETTINGS_FILE, "r") as f:
            dj = json.load(f)
        if isinstance(dj, dict):
            if d is None:
                d = dj
                src = "flash 文件"
            else:
                # 后备内存只存间隔/分辨率；手势参数从 json 合并
                d.update({k: v for k, v in dj.items()
                          if k not in ("interval_min", "interval_max",
                                       "screen_w", "screen_h")})
                src = "后备内存+flash 文件"
    except Exception:
        pass
    if d is None:
        print("无已存设置，使用默认值")
        return
    _apply_dict(d)
    print("设置已从 %s 加载:" % src, d)


def save_settings():
    """写后备内存；开启镜像时同时写 flash"""
    d = _current_dict()
    ok = _write_backup(d)
    if MIRROR_TO_FLASH:
        try:
            import json
            with open(SETTINGS_FILE, "w") as f:
                json.dump(d, f)
        except Exception as e:
            print("写入 flash 失败:", e)
    print("设置已保存 (后备内存=%s):" % ok, d)


# ── UI 布局常量 ──

TITLE_H      = 11
TITLE_TXT_Y  = 2
_ITEMS       = ["Interval Min", "Interval Max", "Resolution"]
_ITEM_Y      = 14     # 第 1 项起始 Y
_ITEM_H      = 11     # 行间距
_ITEM_RECT_H = 10     # 高亮条高度
_HILITE_X    = 2
_HILITE_W    = 124
_TEXT_X      = 6
_VAL_Y       = 47     # 数值行
_HINT_Y      = 56     # 提示 / 反馈行
_MAX_INTERVAL = 60    # 间隔上限（秒），下限固定为 0


# ── 工具 ──

def _adjust_interval(value, delta, lo, hi):
    """在 [lo, hi] 闭区间内循环调整，保证 min <= max 始终成立"""
    span = hi - lo + 1
    if span < 1:
        return lo
    return lo + (value - lo + delta) % span


def _clear(y, h=10):
    oled.fillrect((0, y), (128, h), BLACK)


def _item_rect_y(i):
    return _ITEM_Y + i * _ITEM_H


def _draw_title():
    oled.fill(BLACK)
    oled.fillrect((0, 0), (128, TITLE_H), WHITE)
    oled.text((4, TITLE_TXT_Y), "SETTINGS", BLACK)
    oled.text((76, TITLE_TXT_Y), "B:Save", BLACK)


def _draw_items(cur):
    """绘制 3 个设置项，选中项反白"""
    for i, name in enumerate(_ITEMS):
        y = _item_rect_y(i)
        _clear(y, _ITEM_H)
        if i == cur:
            oled.fillrect((_HILITE_X, y), (_HILITE_W, _ITEM_RECT_H), WHITE)
            oled.text((_TEXT_X, y + 1), name, BLACK)
        else:
            oled.text((_TEXT_X, y + 1), name, WHITE)


def _draw_value(cur):
    """刷新数值行"""
    _clear(_VAL_Y, 9)
    if cur == 0:
        oled.text((0, _VAL_Y), "Min: %ds" % config.SWIPE_INTERVAL_MIN, WHITE)
    elif cur == 1:
        oled.text((0, _VAL_Y), "Max: %ds" % config.SWIPE_INTERVAL_MAX, WHITE)
    else:
        oled.text((0, _VAL_Y), "%dx%d" % (config.PHONE_SCREEN_W,
                                          config.PHONE_SCREEN_H), WHITE)


def _draw_hint(editing, cur):
    """刷新底部提示行（长度均 <= 16 字符）"""
    _clear(_HINT_Y, 8)
    if editing:
        if cur <= 1:
            oled.text((0, _HINT_Y), "PRV:-1 NXT:+1", WHITE)
        else:
            oled.text((0, _HINT_Y), "PRV/NXT: Change", WHITE)
    else:
        oled.text((0, _HINT_Y), "PRV/NXT SEL:Set", WHITE)


def _feedback(s):
    """底部反馈（Saving... / Saved!）"""
    _clear(_HINT_Y, 8)
    oled.text((0, _HINT_Y), s, WHITE)
    oled.show()


def _find_resolution_index():
    """查找当前分辨率在预设列表中的索引"""
    w = config.PHONE_SCREEN_W
    h = config.PHONE_SCREEN_H
    for i, res in enumerate(config.PHONE_RESOLUTIONS):
        if res[0] == w and res[1] == h:
            return i
    return 0


def _adjust_current(cur, delta):
    """对当前设置项增减一步。
    delta = +1 / -1；分辨率项按预设列表前后循环。
    间隔项受 [lo, hi] 钳制，保证 min <= max 恒成立。
    """
    if cur == 0:
        config.SWIPE_INTERVAL_MIN = _adjust_interval(
            config.SWIPE_INTERVAL_MIN, delta, 0, config.SWIPE_INTERVAL_MAX)
    elif cur == 1:
        config.SWIPE_INTERVAL_MAX = _adjust_interval(
            config.SWIPE_INTERVAL_MAX, delta,
            config.SWIPE_INTERVAL_MIN, _MAX_INTERVAL)
    elif cur == 2:
        idx = _find_resolution_index()
        idx = (idx + (1 if delta > 0 else -1)) % len(config.PHONE_RESOLUTIONS)
        config.PHONE_SCREEN_W = config.PHONE_RESOLUTIONS[idx][0]
        config.PHONE_SCREEN_H = config.PHONE_RESOLUTIONS[idx][1]


# ── 主入口 ──

def run():
    _draw_title()
    cur = 0
    editing = False

    _draw_items(cur)
    _draw_value(cur)
    _draw_hint(editing, cur)
    oled.show()

    while True:
        # ── BACK：保存并退出 ──
        if btn_back.was_pressed():
            save_settings()
            _feedback("Saved!")
            time.sleep(1)
            return

        # ── 长按 SEL 保存并退出（兼容只接 2 个键的情况）──
        if btn_sel.is_pressed():
            t0 = time.ticks_ms()
            _feedback("Saving...")       # 立即显示反馈
            while btn_sel.is_pressed():
                if time.ticks_diff(time.ticks_ms(), t0) >= config.LONG_PRESS_MS:
                    save_settings()
                    _feedback("Saved!")
                    time.sleep(1)
                    return
                time.sleep_ms(20)
            # 松手了，恢复提示区
            gc.collect()
            _draw_hint(editing, cur)
            oled.show()

        # ── 短按 SEL：进入 / 退出编辑 ──
        if btn_sel.was_pressed():
            editing = not editing
            gc.collect()
            _draw_hint(editing, cur)
            oled.show()
            continue

        # ── PREV / NEXT：非编辑移动光标，编辑时 -1 / +1 ──
        for btn, delta in ((btn_next, 1), (btn_prev, -1)):
            if not btn.was_pressed():
                continue
            if editing:
                _adjust_current(cur, delta)
                gc.collect()
                _draw_value(cur)
                oled.show()
            else:
                cur = (cur + delta) % len(_ITEMS)
                gc.collect()
                _draw_items(cur)
                _draw_value(cur)
                oled.show()

        # ── 长按 NEXT 也等于 -1（没接 PREV 时的兼容路径）──
        if editing and cur <= 1 and btn_next.is_pressed():
            t0 = time.ticks_ms()
            while btn_next.is_pressed():
                if time.ticks_diff(time.ticks_ms(), t0) >= 600:
                    _adjust_current(cur, -1)
                    _draw_value(cur)
                    oled.show()
                    # 等用户松手
                    while btn_next.is_pressed():
                        time.sleep_ms(20)
                    btn_next.was_pressed()   # 清除标志
                    break
                time.sleep_ms(20)

        time.sleep_ms(40)
