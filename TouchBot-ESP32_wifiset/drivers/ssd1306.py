"""
SSD1306 128x64 OLED (I2C) 驱动（C3 / ESP32 通用）
================================================
在原生 SSD1306 之上提供一层「兼容 API」，调用方式与原来的 ST7735 完全一致，
因此上层 UI 代码只需改坐标，不需要改函数名：

    oled.fill(BLACK)
    oled.fillrect((x, y), (w, h), color)
    oled.text((x, y), "hello", WHITE, None, 1)
    oled.show()

单色屏说明：
    单色 OLED 只有两个取值：BLACK = 0（像素熄灭），WHITE = 1（像素点亮）。
    高亮选中行 = 先 fill_rect(1) 再 text(..., BLACK)，即反白显示。
    没有 RED / GREEN 等彩色常量（已移除）；从 ESP32 彩屏版搬运 UI 代码时，
    请把其中的彩色引用改成 BLACK 或 WHITE。

字体：使用 MicroPython framebuf 内置的 8x8 等宽字体，
    每行最多 128 / 8 = 16 个字符。
"""
from machine import Pin, I2C, SoftI2C
import framebuf


class SSD1306:
    # ── 单色屏取值 ──
    BLACK = 0   # 像素熄灭
    WHITE = 1   # 像素点亮

    CHAR_W = 8      # 内置字体字符宽度
    CHAR_H = 8      # 内置字体字符高度

    def __init__(self, width=128, height=64, i2c=None, addr=0x3C,
                 scl=None, sda=None, freq=400000):
        self.width = width
        self.height = height
        self.addr = addr

        if i2c is None:
            # 优先用硬件 I2C；若不可用（固件裁剪/引脚冲突），退回软件 I2C
            try:
                i2c = I2C(0, scl=Pin(scl), sda=Pin(sda), freq=freq)
            except Exception:
                i2c = SoftI2C(scl=Pin(scl), sda=Pin(sda), freq=freq)
        self.i2c = i2c

        self.buffer = bytearray(height * width // 8)
        self._fb = framebuf.FrameBuffer(self.buffer, width, height, framebuf.MVLSB)
        self._init_display()

    # ── 底层 ──

    def _init_display(self):
        cmds = [
            0xAE,             # display off
            0xD5, 0x80,       # 时钟分频
            0xA8, self.height - 1,   # 多路复用比
            0xD3, 0x00,       # 显示偏移
            0x40,             # 起始行
            0x8D, 0x14,       # 电荷泵使能
            0x20, 0x00,       # 水平寻址模式
            0xA1,             # 段重映射
            0xC8,             # 扫描方向
            0xDA, 0x12 if self.height == 64 else 0x02,   # COM 引脚配置
            0x81, 0xCF,       # 对比度
            0xD9, 0xF1,       # 预充电周期
            0xDB, 0x40,       # VCOMH
            0xA4,             # 全屏点亮关闭
            0xA6,             # 正常显示（非反色）
            0xAF,             # display on
        ]
        for c in cmds:
            self.write_cmd(c)
        self.fill(0)
        self.show()

    def write_cmd(self, cmd):
        self.i2c.writeto(self.addr, b'\x00' + bytearray([cmd]))

    def write_data(self, data):
        self.i2c.writeto(self.addr, b'\x40' + data)

    def show(self):
        """把显存推送到屏幕。所有绘制完成后必须调用一次。"""
        for page in range(self.height // 8):
            self.write_cmd(0xB0 + page)
            self.write_cmd(0x00)
            self.write_cmd(0x10)
            start = page * self.width
            self.write_data(self.buffer[start:start + self.width])

    # ── 兼容 API（签名对齐原 ST7735 驱动）──

    def fill(self, color):
        self._fb.fill(color)

    def fillrect(self, pos, size, color):
        """pos=(x,y) size=(w,h)"""
        self._fb.fill_rect(pos[0], pos[1], size[0], size[1], color)

    def rect(self, pos, size, color):
        self._fb.rect(pos[0], pos[1], size[0], size[1], color)

    def text(self, pos, s, color=1, font=None, size=1, nowrap=False):
        """pos=(x,y)；font/size 仅为兼容保留，单色屏忽略（固定 8x8）"""
        self._fb.text(s, pos[0], pos[1], color)

    def pixel(self, pos, color):
        self._fb.pixel(pos[0], pos[1], color)

    def line(self, x0, y0, x1, y1, color):
        self._fb.line(x0, y0, x1, y1, color)

    def size(self):
        return (self.width, self.height)

    # ── 扩展工具 ──

    def text_centered(self, s, y, color=1):
        """按 8x8 字体水平居中绘制"""
        x = max(0, (self.width - len(s) * self.CHAR_W) // 2)
        self._fb.text(s, x, y, color)

    def invert(self, enable=True):
        self.write_cmd(0xA7 if enable else 0xA6)

    def contrast(self, value):
        self.write_cmd(0x81)
        self.write_cmd(value & 0xFF)

    def poweroff(self):
        self.write_cmd(0xAE)

    def poweron(self):
        self.write_cmd(0xAF)
