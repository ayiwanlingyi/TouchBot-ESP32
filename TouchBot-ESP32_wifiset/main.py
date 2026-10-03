"""
创意钥匙扣 — ESP32 + SSD1306 128x64 主菜单调度
开机直进 Swipe（BLE 优先），退出后进入菜单：Swipe / Setting / Web

布局说明：屏幕 128x64，8x8 字体 → 每行最多 16 字符，共 8 行。
选中项用「反白」表示（白底黑字），OLED 为缓冲区屏，每次绘制后须 oled.show()。
"""
from drivers.ssd1306 import SSD1306
from config import MENU_ITEMS
from app.uartcmd import poll          # 菜单界面也能用串口改配置
import config
import core.hardware as hw
import time, gc

gc.collect()

# 硬件初始化。boot.py 存在时它已经初始化过；没有 boot.py（首次调试常这么做）
# 则这里自己初始化，保证 main.py 单独运行也不会因为 oled 为 None 而崩溃。
if hw.oled is None:
    hw.init()

oled = hw.oled
btn_sel = hw.btn_sel
btn_next = hw.btn_next
btn_prev = hw.btn_prev
btn_back = hw.btn_back

# 硬件就绪后再导入功能层（它们 import 时会绑定 oled / 按键对象）
from app import swipe, setting, web

print("进入应用前可用内存:", gc.mem_free())

BLACK = SSD1306.BLACK
WHITE = SSD1306.WHITE

# ── 菜单布局常量（固定像素坐标，便于局部更新）──
TITLE_H      = 11
TITLE_TXT_Y  = 2
ITEM_START_Y = 14
ITEM_H       = 12
HILITE_H     = ITEM_H - 2
HILITE_X     = 2
HILITE_W     = 124
TEXT_X       = 6
VERSION_Y    = 54

NUM_ITEMS = len(MENU_ITEMS)

DISPATCH = {
    "swipe":   swipe.run,
    "setting": setting.run,
    "web":     web.run,
}


def _title(text):
    oled.fill(BLACK)
    oled.fillrect((0, 0), (128, TITLE_H), WHITE)
    oled.text((4, TITLE_TXT_Y), text, BLACK)
    oled.show()


def draw_menu_first(cur):
    oled.fill(BLACK)

    # 标题栏（反白）
    oled.fillrect((0, 0), (128, TITLE_H), WHITE)
    oled.text((4, TITLE_TXT_Y), "MENU", BLACK)
    oled.text((96, TITLE_TXT_Y), "v2.1", BLACK)

    oled.text((36, VERSION_Y), "esp-bot", WHITE)

    for i, item in enumerate(MENU_ITEMS):
        y = ITEM_START_Y + i * ITEM_H
        if i == cur:
            oled.fillrect((HILITE_X, y), (HILITE_W, HILITE_H), WHITE)
            oled.text((TEXT_X, y + 2), item["name"], BLACK)
        else:
            oled.text((TEXT_X, y + 2), item["name"], WHITE)

    oled.show()


def refresh_menu(old, new):
    """只重绘变化的两行，减少闪烁"""
    y_old = ITEM_START_Y + old * ITEM_H
    oled.fillrect((HILITE_X, y_old), (HILITE_W, HILITE_H), BLACK)
    oled.text((TEXT_X, y_old + 2), MENU_ITEMS[old]["name"], WHITE)

    y_new = ITEM_START_Y + new * ITEM_H
    oled.fillrect((HILITE_X, y_new), (HILITE_W, HILITE_H), WHITE)
    oled.text((TEXT_X, y_new + 2), MENU_ITEMS[new]["name"], BLACK)

    oled.show()


def wait_key():
    """返回 (next, prev, sel)。主菜单是根界面，BACK 无作用，读取后丢弃标志"""
    while True:
        poll()          # 菜单里没有 BLE 句柄，只能跑 GET/SET/SAVE 这类配置指令
        n = btn_next.was_pressed()
        p = btn_prev.was_pressed()
        s = btn_sel.was_pressed()
        btn_back.was_pressed()
        if n or p or s:
            return n, p, s
        time.sleep_ms(30)


def main():
    # 加载持久化设置（覆盖 config 默认值）
    try:
        setting.load_settings()
    except Exception:
        pass
    gc.collect()

    # 无屏模式：HEADLESS 打开，或 OLED 根本没接上。
    # 后者必须兜住 —— 否则下面的菜单代码会在 oled=None 上直接崩掉。
    if getattr(config, "HEADLESS", False) or hw.oled is None:
        if hw.oled is None and not getattr(config, "HEADLESS", False):
            print("未检测到 OLED，自动切换到无屏模式")
        from app import webremote
        webremote.run()
        return

    # 开机直进刷视频模式（BLE 优先），退出后回主菜单
    try:
        swipe.run()
    except Exception as e:
        _title("Swipe Error!")
        oled.text((4, 24), str(e)[:16], WHITE)
        oled.text((4, 40), "Btn: exit", WHITE)
        oled.show()
        time.sleep(2)

    gc.collect()

    cur = 0
    draw_menu_first(cur)

    while True:
        n, p, s = wait_key()

        if n:
            old = cur
            cur = (cur + 1) % NUM_ITEMS
            refresh_menu(old, cur)

        if p:
            old = cur
            cur = (cur - 1) % NUM_ITEMS
            refresh_menu(old, cur)

        if s:
            mid = MENU_ITEMS[cur]["id"]
            handler = DISPATCH.get(mid)
            if handler is None:
                _title("No handler")
                oled.text((4, 30), mid[:16], WHITE)
                oled.show()
                time.sleep(2)
            else:
                try:
                    handler()
                except Exception as e:
                    _title("Error!")
                    oled.text((4, 24), str(e)[:16], WHITE)
                    oled.show()
                    time.sleep(2)

            draw_menu_first(cur)
            gc.collect()


main()
