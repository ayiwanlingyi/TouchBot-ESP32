"""
硬件初始化抽象层 —— ESP32（初代双核 LX6）+ SSD1306 128x64 OLED (I2C)
所有功能模块从此模块导入 oled / 四个按键对象
避免 I2C/OLED/Button 在各文件中重复初始化

未外接按键的 GPIO 靠内部上拉保持高电平，不会误触发，
所以可以只用「BOOT + 一个外接键」跑起来，接满四键体验更好。
"""
from machine import Pin
from drivers.ssd1306 import SSD1306
from drivers.Button import Button
import config
import gc

oled = None
btn_sel = None    # GPIO0  - 板载 BOOT 键：确认
btn_next = None   # GPIO33 - 下移 / +1
btn_prev = None   # GPIO32 - 上移 / -1
btn_back = None   # GPIO25 - 返回 / 退出


def init():
    """初始化 I2C OLED 和四个按键

    返回 (oled, btn_sel, btn_next, btn_prev, btn_back)
    """
    global oled, btn_sel, btn_next, btn_prev, btn_back

    # 无屏模式不碰 OLED；有屏但屏幕没接好也只是告警，不让整机起不来
    if getattr(config, "HEADLESS", False):
        oled = None
        print("HEADLESS 模式：跳过 OLED 初始化")
    else:
        try:
            oled = SSD1306(config.OLED_WIDTH, config.OLED_HEIGHT,
                           scl=config.OLED_I2C_SCL,
                           sda=config.OLED_I2C_SDA,
                           freq=config.OLED_I2C_FREQ,
                           addr=config.OLED_ADDR)
        except Exception as e:
            oled = None
            print("OLED 初始化失败（继续以无屏方式运行）:", e)
    btn_sel = Button(config.BTN_SEL_PIN)
    btn_next = Button(config.BTN_NEXT_PIN)
    btn_prev = Button(config.BTN_PREV_PIN)
    btn_back = Button(config.BTN_BACK_PIN)
    gc.collect()
    return oled, btn_sel, btn_next, btn_prev, btn_back
