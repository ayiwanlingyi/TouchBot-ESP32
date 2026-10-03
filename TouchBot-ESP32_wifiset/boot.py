"""
启动入口 — ESP32（初代）+ SSD1306 128x64 版
薄壳：硬件初始化 → splash → 隐式执行 main.py
WiFi 不在开机阶段连接（BLE 优先），仅在 Web 菜单按需连接
"""
import gc

try:
    import mdns
    mdns.Active(False)
except Exception:
    pass

from core.hardware import init
from core.splash import run as splash_run

gc.collect()
oled, _, _, _, _ = init()

gc.collect()
try:
    splash_run(oled)            # 开机动画失败绝不能阻断 main.py
except Exception as e:
    print("开机动画跳过:", e)
gc.collect()

print("启动完成。可用内存:", gc.mem_free())
