"""
WiFi / AP 连接 — SSD1306 128x64 版，同时兼容无屏（oled=None）模式
无屏时所有提示走串口 print，不触碰 OLED。
"""
from drivers.ssd1306 import SSD1306
import network, time, gc

WHITE = SSD1306.WHITE
BLACK = SSD1306.BLACK

# ── 128x64 布局常量（8x8 字体，行高 12px）──
L_TITLE  = 0
L_INFO   = 14
L_STATUS = 26
L_DETAIL = 38
L_EXTRA  = 50


def _kill_mdns():
    try:
        import mdns
        mdns.Active(False)
    except Exception:
        pass


_CN = {"Scanning...": "扫描WiFi中...", "No known AP": "附近无已知WiFi",
       "Connected!": "WiFi已连接", "All failed": "全部连接失败",
       "Offline mode": "离线运行"}


def _screen(oled, lines):
    """统一输出：串口打中文，OLED 画原文（内置 ASCII 字体不支持中文）"""
    for y, s in lines:
        if s.startswith("Try "):
            print("[wifi] 正在尝试第 %s 个热点" % s[4:])
        else:
            print("[wifi]", _CN.get(s, s))
    if oled is None:
        return
    try:
        oled.fill(BLACK)
        oled.fillrect((0, L_TITLE), (128, 11), WHITE)
        oled.text((4, 2), "WiFi", BLACK)
        for y, s in lines:
            oled.text((0, y), s[:16], WHITE)
        oled.show()
    except Exception:
        pass


def connect(oled, wifi_config):
    """
    扫描优先的按需连接（STA 模式）。
    返回 (成功bool, ip)；全部失败返回 (False, None)
    """
    sta = network.WLAN(network.STA_IF)
    _kill_mdns()

    _screen(oled, [(L_INFO, "Scanning...")])

    # 先扫描一次，只尝试环境中实际存在的已知 SSID
    try:
        sta.active(True)
        scan_result = sta.scan()
        if not scan_result:
            time.sleep_ms(1000)   # 刚激活可能扫不到，稍候重试一次
            scan_result = sta.scan()
        visible = {ap[0].decode() if isinstance(ap[0], bytes) else ap[0]
                   for ap in scan_result}
        if not visible:
            visible = None        # 扫描结果为空则退回逐个盲试
    except Exception:
        visible = None            # 扫描失败则退回逐个盲试

    candidates = ([ap for ap in wifi_config
                   if visible is None or ap["ssid"] in visible])

    if not candidates:
        _screen(oled, [(L_STATUS, "No known AP")])
        time.sleep(1.5)
        gc.collect()
        return False, None

    total = len(candidates)
    for idx, ap in enumerate(candidates):
        ssid = ap["ssid"]
        pwd = ap["password"]

        _screen(oled, [(L_STATUS, "Try %d/%d" % (idx + 1, total)),
                       (L_DETAIL, ssid)])

        sta.connect(ssid, pwd)
        for tick in range(40):          # 40 × 200ms = 8s
            if sta.isconnected():
                ip = sta.ifconfig()[0]
                _screen(oled, [(L_STATUS, "Connected!"),
                               (L_DETAIL, ssid),
                               (L_EXTRA, ip)])
                time.sleep(1)
                gc.collect()
                return True, ip
            time.sleep_ms(200)

        sta.disconnect()
        time.sleep_ms(50)

    _screen(oled, [(L_STATUS, "All failed"),
                   (L_DETAIL, "Offline mode")])
    time.sleep(1.5)
    gc.collect()
    return False, None


def start_ap(ssid, password):
    """开启 AP 热点（无屏模式推荐）。

    自建热点后手机直接连上来访问固定地址 192.168.4.1，不用查 IP。
    返回 ip；失败返回 None。注意 password 至少 8 位。
    """
    ap = network.WLAN(network.AP_IF)
    try:
        ap.active(True)
        ap.config(essid=ssid, password=password,
                  authmode=network.AUTH_WPA_WPA2_PSK)
        for _ in range(50):                 # 最多等 5s
            if ap.active():
                break
            time.sleep_ms(100)
        time.sleep_ms(300)
        ip = ap.ifconfig()[0]
        print("AP 已启动: %s  密码: %s  ip=%s" % (ssid, password, ip))
        return ip
    except Exception as e:
        print("AP 启动失败:", e)
        return None


def disconnect_ap():
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except Exception:
        pass


def disconnect():
    """断开 WiFi 并关闭 STA（省电，回到离线状态）"""
    try:
        sta = network.WLAN(network.STA_IF)
        if sta.isconnected():
            sta.disconnect()
            time.sleep_ms(100)
        sta.active(False)
    except Exception:
        pass
