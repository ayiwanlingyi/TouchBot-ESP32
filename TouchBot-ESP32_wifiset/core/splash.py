"""
启动动画 — SSD1306 128x64 单色版
WWZU 文字 + 向内收缩的边框
"""
from drivers.ssd1306 import SSD1306
import gc

BLACK = SSD1306.BLACK
WHITE = SSD1306.WHITE


def run(oled):
    """启动动画：嵌套矩形 + WWZU 文字（适配 128x64）
    无屏模式（oled=None，OLED 未接或初始化失败）时跳过动画"""
    if oled is None:
        print("无屏模式：跳过开机动画")
        return
    oled.fill(BLACK)
    oled.text_centered("WWZU", 28, WHITE)

    # 由外向内画嵌套矩形，形成开机动画
    x, y = 0, 0
    w, h = oled.width - 2, oled.height - 2
    while w > 12 and h > 12:
        oled.rect((x, y), (w, h), WHITE)
        x += 3
        y += 2
        w -= 6
        h -= 4

    oled.show()
    gc.collect()
