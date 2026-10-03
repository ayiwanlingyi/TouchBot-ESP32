"""
Web 配置服务 — SSD1306 128x64 版
按需连接 WiFi 并启动 HTTP 服务，手机/电脑浏览器远程配置刷视频参数
退出时停止服务并断开 WiFi，回到离线状态
"""
from core.hardware import oled, btn_next, btn_sel, btn_back
from drivers.ssd1306 import SSD1306
import config
import time, gc

BLACK = SSD1306.BLACK
WHITE = SSD1306.WHITE

# ── 布局（8x8 字体）──
TITLE_H     = 11
TITLE_TXT_Y = 2
R_STATE     = 14
R_INFO      = 25
R_PORT      = 36
R_OPEN      = 47
R_EXIT      = 56


def _clear(y, h=10):
    oled.fillrect((0, y), (128, h), BLACK)


def _screen(lines):
    """画整屏：lines = [(y, text), ...]"""
    oled.fill(BLACK)
    oled.fillrect((0, 0), (128, TITLE_H), WHITE)
    oled.text((4, TITLE_TXT_Y), "WEB CONFIG", BLACK)
    for y, s in lines:
        oled.text((0, y), s, WHITE)
    oled.show()


def run():
    from core import wifi
    from app import http_server

    oled.fill(BLACK)
    oled.fillrect((0, 0), (128, TITLE_H), WHITE)
    oled.text((4, TITLE_TXT_Y), "WEB CONFIG", BLACK)
    oled.text((0, R_STATE), "Connecting...", WHITE)
    oled.show()

    ok, ip = wifi.connect(oled, config.wifi_config)
    if not ok:
        _screen([(R_STATE, "WiFi failed"),
                 (R_EXIT, "Btn: exit")])
        while not (btn_next.was_pressed() or btn_back.was_pressed()
                   or btn_sel.was_pressed()):
            time.sleep_ms(100)
        return

    http_server.start()
    gc.collect()

    _screen([(R_STATE, "HTTP running"),
             (R_INFO,  str(ip)[:16]),
             (R_PORT,  "Port: %d" % config.HTTP_PORT),
             (R_OPEN,  "Open in browser"),
             (R_EXIT,  "BACK/NXT: Exit")])

    try:
        while True:
            try:
                http_server.poll()
            except Exception:
                pass
            if btn_back.was_pressed() or btn_next.was_pressed():
                return
            time.sleep_ms(30)
    finally:
        http_server.stop()
        wifi.disconnect()
        gc.collect()
