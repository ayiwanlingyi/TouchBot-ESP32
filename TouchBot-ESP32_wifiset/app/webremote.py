"""
Web 远程配置 — 无屏模式（HEADLESS）
==================================
用「长按 BOOT」在两种状态之间循环，WiFi 只在需要配置时开启：

    ┌───────────────── 长按 BOOT（2 秒）─────────────────┐
    ↓                                                    │
 [运行态] BLE 常开、WiFi 关闭、自动滑动            [配置态] WiFi 热点 + 强制门户网页
                                                         │ 网页点「启动」
                                                         └→ 关 WiFi → 回运行态并开始滑动

强制门户（连上 WiFi 自动弹网页）靠两点：
    1. DNS 服务（UDP 53）把所有域名解析到本机
    2. HTTP 对未知路径返回 302 跳转到控制台；iOS 的探测路径直接返回页面

状态语义（重要）：
    · 默认就在「运行态」= 等待启动，BLE 常开，随时可以开始滑动 / 遥控
    · 只有长按 BOOT 开了热点的那一段才是「配置态」——BLE 必须关，
      串口指令也一并停掉，退出靠网页「保存设置」
    · 串口遥控不受状态影响：REMOTE 之后既能发触摸指令（T/P/M/R/S），
      也能改配置（GET/SET/PROFILE/SAVE）。配置指令只改内存、立即生效、
      不打断运行；要落盘再发 SAVE
"""

import socket
import sys
import time
import random
import gc
import config

try:
    import json
except ImportError:
    json = None

from app.swipe import HIDTouch
from core import wifi
from core.hardware import btn_sel


def _sorted_interval():
    a, b = config.SWIPE_INTERVAL_MIN, config.SWIPE_INTERVAL_MAX
    return (a, b) if a <= b else (b, a)


# 串口指令解析在 app/uartcmd.py（有屏 / 无屏共用同一份）
from app.uartcmd import GESTURE_LIMITS, poll, is_active


# ══════════════════ 强制门户 DNS ══════════════════

class CaptiveDNS:
    """把所有域名 A 记录应答为本机 IP"""

    def __init__(self, ip):
        self.ip = ip
        self.addr = bytes(int(x) for x in ip.split("."))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 53))
        self.sock.settimeout(0)

    def poll(self):
        try:
            data, addr = self.sock.recvfrom(512)
        except Exception:
            return
        try:
            if len(data) < 12:
                return
            resp = (data[0:2]                       # 事务 ID
                    + b"\x81\x80"                   # 标志：应答 + 递归可用
                    + b"\x00\x01\x00\x01\x00\x00\x00\x00"
                    + data[12:]                     # 原样带回 question
                    + b"\xc0\x0c"                   # 名字指针
                    + b"\x00\x01\x00\x01"           # A 记录 / IN
                    + b"\x00\x00\x00\x3c"           # TTL 60s
                    + b"\x00\x04" + self.addr)      # 4 字节 IPv4
            self.sock.sendto(resp, addr)
        except Exception:
            pass

    def stop(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ══════════════════ 滑动服务（非阻塞）══════════════════

class SwipeService:
    """滑动服务。

    严格遵守「WiFi 与 BLE 互斥」：
        · 运行态：持有 HIDTouch（BLE 开），WiFi 必须是关的
        · 配置态：detach() 把 BLE 彻底关掉，才允许开 WiFi
    两者绝不共存。
    """

    def __init__(self):
        self.h = None
        self.ok = False
        self.err = "BLE 未启动"
        self.running = False
        self.count = 0
        self.next_ms = 0

    # ── 生命周期 ──

    def attach(self):
        """进入运行态：启动 BLE。调用前必须已关闭 WiFi。
        失败时清掉句柄，run() 主循环会隔几秒自动重试。"""
        if self.ok:
            return True
        gc.collect()
        h = HIDTouch()
        self.ok = h.init_ok
        self.err = getattr(h, "_err", "") if not self.ok else ""
        self.h = h if self.ok else None    # 失败不留句柄，允许重试
        self.running = False
        self.count = 0
        if self.ok:
            print("[BLE] 已启动，广播名 esp-bot")
        else:
            print("[BLE] 启动失败:", self.err)
        return self.ok

    def detach(self):
        """进入配置态前调用：彻底关闭 BLE，把资源让给 WiFi。"""
        self.running = False
        if self.h is not None:
            try:
                self.h.deinit()
            except Exception:
                pass
            self.h = None
        self.ok = False
        self.err = "BLE 已关闭（配置态）"
        gc.collect()
        print("[BLE] 已关闭")

    # ── 查询 ──

    def connected(self):
        if self.h is None:
            return False
        try:
            return self.h.is_connected()
        except Exception:
            return False

    def status(self):
        left = 0
        if self.running and self.ok:
            left = max(0, time.ticks_diff(self.next_ms, time.ticks_ms()) // 1000)
        return {"ble": self.connected(), "ble_ok": self.ok, "err": self.err,
                "ble_off": self.h is None,
                "running": self.running, "count": self.count, "next": left}

    def status_text(self):
        """给串口 STATUS 指令用的一行状态"""
        s = self.status()
        ble = "已关闭" if s.get("ble_off") else ("已连接" if s["ble"]
                                              else "未连接")
        text = "运行态 | BLE:%s | %s | 已滑:%d" % (
            ble, "滑动中" if s["running"] else "已停止", s["count"])
        if s["running"]:
            text += " | 下次:%ds" % s["next"]
        return text

    # ── 控制 ──

    def start(self):
        # 必须真的连上手机：否则会进入「滑动中」却一次都滑不动的假状态
        if not (self.ok and self.connected()):
            return False
        self.running = True
        self.count = 0
        self.next_ms = time.ticks_ms()      # 立刻滑第一次
        return True

    def stop(self):
        self.running = False
        return True

    def toggle(self):
        return self.stop() if self.running else self.start()

    def once(self):
        if not (self.ok and self.connected()):
            return False
        self.h.swipe_up()
        self.count += 1
        return True

    def tick(self):
        if not (self.running and self.ok and self.connected()):
            return
        if time.ticks_diff(time.ticks_ms(), self.next_ms) < 0:
            return
        self.h.swipe_up()
        self.count += 1
        lo, hi = _sorted_interval()
        self.next_ms = time.ticks_add(time.ticks_ms(),
                                      random.randint(lo, hi) * 1000)

    def deinit(self):
        self.detach()


# ══════════════════ 网页 ══════════════════

_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP32 刷视频控制台</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:14px;font-family:system-ui,sans-serif;background:#12141c;color:#e8eaf0}
h1{font-size:17px;margin:0 0 12px;color:#7cc4ff}
.card{background:#1c2030;border-radius:10px;padding:12px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;padding:5px 0;font-size:14px}
.row span:last-child{font-weight:600}
.on{color:#4ade80}.off{color:#94a3b8}.warn{color:#fbbf24}
button{width:100%;padding:12px;margin:5px 0;border:0;border-radius:8px;
 font-size:15px;font-weight:600;background:#2a3550;color:#e8eaf0}
button.pri{background:#16a34a;color:#fff}
button.stop{background:#dc2626;color:#fff}
label{display:block;font-size:13px;margin:9px 0 3px;color:#9aa4bd}
input,select{width:100%;padding:9px;border-radius:7px;border:1px solid #37405c;
 background:#0f1320;color:#e8eaf0;font-size:14px}
.tip{font-size:12px;color:#8b96b0;margin-top:10px;line-height:1.6}
</style></head><body>
<h1>ESP32 刷视频控制台</h1>

<div class="card">
 <label>场景模式（点一下即套用间隔与手势，留在配置态）</label>
 <div id="scenes"></div>
</div>

<div class="card">
 <label>最小间隔（秒）</label>
 <input id="vmin" type="number" min="0" max="300">
 <label>最大间隔（秒）</label>
 <input id="vmax" type="number" min="0" max="300">
 <label>手机分辨率</label>
 <select id="res"></select>
 <div id="crow" style="display:none">
  <input id="cw" type="number" min="100" max="4000" placeholder="宽（如 1080）">
  <input id="ch" type="number" min="100" max="6000" placeholder="高（如 2340）">
 </div>
 <button class="pri" onclick="save()">保存设置（关 WiFi → 开蓝牙）</button>
</div>

<div class="tip" id="msg">
 本页只负责保存设置与模式。<br>
 <b>点「保存设置」即退出配置</b>：关 WiFi → 开蓝牙 → 开始滑动（网页随即断开）。<br>
 配置期间蓝牙关闭（与 WiFi 互斥），手机连不上属正常；<br>
 退出后用 <b>短按 BOOT</b> 暂停 / 继续。蓝牙连上与否看手机蓝牙列表。
</div>

<script>
async function load(){
  try{
    const r=await fetch('/status'); const st=await r.json();
    document.getElementById('vmin').value=st.interval_min;
    document.getElementById('vmax').value=st.interval_max;
    const sel=document.getElementById('res');
    st.presets.forEach(p=>{const o=document.createElement('option');
      o.value=p;o.textContent=p;sel.appendChild(o);});
    const co=document.createElement('option');
    co.value='custom';co.textContent='自定义…';sel.appendChild(co);
    sel.onchange=function(){
      document.getElementById('crow').style.display=
        sel.value=='custom'?'block':'none';};
    if(st.presets.indexOf(st.screen_w+'x'+st.screen_h)>=0){
      sel.value=st.screen_w+'x'+st.screen_h;
    }else{
      sel.value='custom';
      document.getElementById('cw').value=st.screen_w;
      document.getElementById('ch').value=st.screen_h;
      document.getElementById('crow').style.display='block';
    }
    const sc=document.getElementById('scenes');
    (st.scenes||[]).forEach(s=>{
      const b=document.createElement('button');
      b.textContent=s.name+'　'+s.interval_min+'-'+s.interval_max+' 秒';
      b.onclick=function(){preset(s.name)};
      sc.appendChild(b);});
  }catch(e){document.getElementById('msg').textContent='读取当前设置失败';}
}
async function post(b){
  const r=await fetch('/api',{method:'POST',body:JSON.stringify(b)});
  return await r.json();
}
async function save(){
  try{
    const sel=document.getElementById('res');
    let w,h;
    if(sel.value=='custom'){
      w=+document.getElementById('cw').value;
      h=+document.getElementById('ch').value;
    }else{
      const p=sel.value.split('x');w=+p[0];h=+p[1];
    }
    const j=await post({cmd:'save',
      interval_min:+document.getElementById('vmin').value,
      interval_max:+document.getElementById('vmax').value,
      screen_w:w,screen_h:h});
    if(j.ok){
      document.getElementById('msg').innerHTML =
        '已保存，正在 <b>关闭 WiFi → 开蓝牙 → 开始滑动</b>…<br>本页即将断开，去手机蓝牙配对 esp-bot';
    }else{
      document.getElementById('msg').textContent='保存失败：'+(j.err||'');
    }
  }catch(e){document.getElementById('msg').textContent='保存失败';}
}
async function preset(n){
  try{
    const j=await post({cmd:'set',profile:n});
    if(j.ok){
      document.getElementById('vmin').value=j.interval_min;
      document.getElementById('vmax').value=j.interval_max;
      document.getElementById('msg').textContent=
        '已套用「'+n+'」（含手势参数，已保存，可继续修改）';
    }else{
      document.getElementById('msg').textContent='套用失败：'+(j.err||'');
    }
  }catch(e){document.getElementById('msg').textContent='套用失败';}
}
load();
</script></body></html>"""


def _resp(conn, code, ctype, body, extra=b""):
    if isinstance(body, str):
        body = body.encode("utf-8")
    data = (b"HTTP/1.1 " + code + b"\r\n"
            + b"Content-Type: " + ctype + b"\r\n"
            + b"Content-Length: %d\r\n" % len(body)
            + b"Connection: close\r\n" + extra + b"\r\n" + body)
    n = len(data)
    sent = 0
    while sent < n:                    # send 可能部分发送，必须循环补齐
        try:
            sent += conn.send(data[sent:])
        except Exception:
            break


class Server:
    def __init__(self, svc, ip):
        self.svc = svc
        self.ip = ip
        self.base = b"http://" + str(ip).encode()   # 直接存 bytes，便于拼 HTTP 头
        self.sock = None
        self.exit_req = False      # 网页「保存设置」置位 → 退出配置态

    def start(self, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.listen(3)
        self.sock.settimeout(0.3)
        print("HTTP 服务已启动 %s:%d" % (self.ip, port))

    def stop(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def poll(self):
        if self.sock is None:
            return
        try:
            conn, _ = self.sock.accept()
        except Exception:
            return
        try:
            self._handle(conn)
        except Exception as e:
            print("HTTP 处理出错:", e)
            sys.print_exception(e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── 路由 ──

    def _handle(self, conn):
        conn.settimeout(3)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = conn.recv(256)
            if not chunk:
                return
            data += chunk

        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except Exception:
                    length = 0
        body = rest
        while len(body) < length:
            chunk = conn.recv(256)
            if not chunk:
                break
            body += chunk

        parts = head.split(b" ")
        method = parts[0] if parts else b""
        path = (parts[1] if len(parts) > 1 else b"/").split(b"?")[0]

        # iOS / macOS 探测：直接返回页面，系统小窗会显示它
        if path in (b"/hotspot-detect.html", b"/hotspot-detect.html/",
                    b"/library/test/success.html", b"/success.html",
                    b"/popup.html"):
            _resp(conn, b"200 OK", b"text/html; charset=utf-8", _PAGE)

        # Android / Windows 探测：302 到控制台，系统弹出「需要登录」提示
        elif path in (b"/generate_204", b"/gen_204", b"/ncsi.txt",
                      b"/connecttest.txt", b"/redirect", b"/favicon.ico",
                      b"/mobile/status.php"):
            _resp(conn, b"302 Found", b"text/plain", "redirect",
                  b"Location: " + self.base + b"/\r\n")

        elif path in (b"/", b"/index.html"):
            _resp(conn, b"200 OK", b"text/html; charset=utf-8", _PAGE)
        elif path == b"/status":
            self._status(conn)
        elif path == b"/api" and method == b"POST":
            self._api(conn, body)
        else:
            # 兜底：任何未知地址都跳回控制台
            _resp(conn, b"302 Found", b"text/plain", "redirect",
                  b"Location: " + self.base + b"/\r\n")

    def _status(self, conn):
        d = self.svc.status()
        d.update({
            "interval_min": config.SWIPE_INTERVAL_MIN,
            "interval_max": config.SWIPE_INTERVAL_MAX,
            "screen_w": config.PHONE_SCREEN_W,
            "screen_h": config.PHONE_SCREEN_H,
            "presets": ["%dx%d" % r[:2] for r in config.PHONE_RESOLUTIONS],
            "scenes": [{"name": n,
                        "interval_min": pf.get("interval_min",
                                               config.SWIPE_INTERVAL_MIN),
                        "interval_max": pf.get("interval_max",
                                               config.SWIPE_INTERVAL_MAX)}
                       for n, pf in getattr(config, "SWIPE_PROFILES",
                                            {}).items()],
        })
        _resp(conn, b"200 OK", b"application/json", json.dumps(d))

    def _api(self, conn, body):
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            _resp(conn, b"400 Bad Request", b"application/json",
                  '{"ok":false,"err":"bad json"}')
            return

        cmd = obj.get("cmd", "")
        if cmd == "start":
            r = {"ok": self.svc.start()}
        elif cmd == "stop":
            r = {"ok": self.svc.stop()}
        elif cmd == "toggle":
            r = {"ok": self.svc.toggle()}
        elif cmd == "once":
            r = {"ok": self.svc.once()}
        elif cmd == "set":
            r = self._set(obj)          # 场景预设：应用+保存，留在配置态
        elif cmd == "save":
            r = self._set(obj)          # 保存设置：保存并退出配置态
            if r.get("ok"):
                self.exit_req = True
                r["exit"] = True
        else:
            r = {"ok": False, "err": "unknown cmd"}
        _resp(conn, b"200 OK", b"application/json", json.dumps(r))

    # 手势参数：json 键名 -> config 属性 -> 允许范围（与串口共用）
    _GESTURES = GESTURE_LIMITS

    def _set(self, obj):
        try:
            # 场景档案：把档案字段并入请求，再走统一的钳制流程
            prof = obj.get("profile")
            if prof:
                pf = getattr(config, "SWIPE_PROFILES", {}).get(prof)
                if pf is None:
                    return {"ok": False, "err": "unknown profile"}
                for k, v in pf.items():
                    obj[k] = v
            if "interval_min" in obj:
                v = int(obj["interval_min"])
                if 0 <= v <= 300:
                    config.SWIPE_INTERVAL_MIN = v
            if "interval_max" in obj:
                v = int(obj["interval_max"])
                if 0 <= v <= 300:
                    config.SWIPE_INTERVAL_MAX = v
            if "screen_w" in obj:
                v = int(obj["screen_w"])
                if 100 <= v <= 4000:
                    config.PHONE_SCREEN_W = v
            if "screen_h" in obj:
                v = int(obj["screen_h"])
                if 100 <= v <= 6000:
                    config.PHONE_SCREEN_H = v
            for key, attr, lo, hi in self._GESTURES:
                if key in obj:
                    v = int(obj[key])
                    if lo <= v <= hi:
                        setattr(config, attr, v)
            if config.SWIPE_INTERVAL_MIN > config.SWIPE_INTERVAL_MAX:
                config.SWIPE_INTERVAL_MIN, config.SWIPE_INTERVAL_MAX = \
                    config.SWIPE_INTERVAL_MAX, config.SWIPE_INTERVAL_MIN
            try:
                from app.setting import save_settings
                save_settings()
            except Exception as e:
                print("保存失败:", e)
            return {"ok": True, "profile": prof or "",
                    "interval_min": config.SWIPE_INTERVAL_MIN,
                    "interval_max": config.SWIPE_INTERVAL_MAX}
        except Exception as e:
            return {"ok": False, "err": str(e)}


# ══════════════════ 状态机 ══════════════════

def _start_network():
    """开启网络（AP 或 STA），返回 ip；失败返回 None"""
    if getattr(config, "WIFI_MODE", "ap") == "ap":
        print("启动热点 %s（密码 %s）..."
              % (config.WIFI_AP_SSID, config.WIFI_AP_PASSWORD))
        return wifi.start_ap(config.WIFI_AP_SSID, config.WIFI_AP_PASSWORD)
    ok, ip = wifi.connect(None, config.wifi_config)
    return ip if ok else None


def _stop_network():
    wifi.disconnect_ap()
    wifi.disconnect()


def _heartbeat(svc, in_config, ip=None):
    """串口打印当前状态（没有 LED / 屏幕时主要靠它观察设备）"""
    s = svc.status()
    state = "配置态" if in_config else "运行态"
    if s.get("ble_off"):
        ble = "已关闭"
    else:
        ble = "已连接" if s["ble"] else "未连接"
    run = "滑动中" if s["running"] else "已停止"
    line = "[状态] %s | BLE:%s | %s | 已滑:%d" % (state, ble, run, s["count"])
    if s["running"]:
        line += " | 下次:%ds" % s["next"]
    if in_config and ip:
        line += " | 网页:http://%s" % ip
    else:
        line += " | 长按BOOT进配置"
    print(line)


def _boot_event(hold):
    """读一次 BOOT 键：'long' / 'short' / None。会清掉按下标志。"""
    if btn_sel.is_pressed():
        t0 = time.ticks_ms()
        long = False
        while btn_sel.is_pressed():
            if time.ticks_diff(time.ticks_ms(), t0) >= hold:
                long = True
                break
            time.sleep_ms(30)
        btn_sel.was_pressed()          # 清标志
        return "long" if long else "short"
    # 按得太短，轮询没赶上，靠中断置位的标志兜底
    if btn_sel.was_pressed():
        return "short"
    return None


def run():
    print("=" * 50)
    print(" 无屏模式：运行态 ⇄ 配置态（长按 BOOT 切换）")
    print("=" * 50)

    if json is None:
        print("固件缺少 json 模块，无法运行")
        return

    svc = SwipeService()

    # 开机先进运行态：确保 WiFi 关闭 → 再启动 BLE（二者绝不共存）
    _stop_network()
    time.sleep_ms(200)
    svc.attach()

    if getattr(config, "AUTO_START_ON_BOOT", False):
        svc.start()
        print("开机自动开始滑动")

    port = getattr(config, "CONFIG_HTTP_PORT", 80)
    hold = getattr(config, "BOOT_LONG_PRESS_MS", 2000)

    srv = None
    dns = None
    in_config = False
    cfg_ip = None
    last_print = 0
    last_ble = None
    ble_retry = 0              # BLE 启动失败后的下次重试时刻

    print("当前：运行态。长按 BOOT 键 %.1f 秒进入配置模式" % (hold / 1000))
    print("串口遥控就绪：PC 端 115200 发 REMOTE 进入遥控，END 退出")

    try:
        while True:
            now = time.ticks_ms()

            # BLE 连接状态一变就打印，不必等心跳
            ble_now = svc.connected()
            if ble_now != last_ble:
                print("[BLE] %s" % ("已连接" if ble_now else "已断开"))
                last_ble = ble_now
                last_print = 0          # 立刻刷新一行完整状态
                if not ble_now and svc.running:
                    svc.stop()          # 手机断开后别再挂着「滑动中」
                    print("[提示] 蓝牙已断开，自动滑动已停止")

            # 定时心跳
            period = getattr(config, "STATUS_PRINT_MS", 5000)
            if period and (last_print == 0
                           or time.ticks_diff(now, last_print) >= period):
                last_print = now
                _heartbeat(svc, in_config, cfg_ip)

            if is_active():
                # 遥控模式：按键一律无效。长按 2 秒会进配置态并关掉 BLE，直接打断遥控；
                # 这里只清按下标志，不做长按判定（长按等待会阻塞串口指令处理）
                btn_sel.was_pressed()
                ev = None
            else:
                ev = _boot_event(hold)

            if in_config:
                srv.poll()
                dns.poll()
                if srv.exit_req:
                    # 网页「保存设置」触发。设置已写入后备内存/flash，
                    # ESP32 上 WiFi→BLE 热切换常因控制器资源未释放而报
                    # "controller init failed"，软复位后 BLE 全新初始化最可靠。
                    # 复位后 WiFi 不会自启，开机即开蓝牙，并自动开始滑动。
                    print("网页保存 → 设置已保存，正在重启进入运行态（开蓝牙）...")
                    srv.stop()
                    dns.stop()
                    _stop_network()
                    time.sleep_ms(500)
                    from machine import reset
                    reset()
                elif ev == "long":
                    print("[提示] 配置态下长按无效（WiFi 已开）")
                    print("       退出配置请用网页「保存设置」")
                elif ev == "short":
                    print("[提示] 配置态下短按无效；退出请用网页「保存设置」")
                time.sleep_ms(30)
            else:
                svc.tick()
                # BLE 启动失败（偶发的 controller init failed）时每 10 秒重试
                if not svc.ok and time.ticks_diff(now, ble_retry) >= 10000:
                    ble_retry = now
                    print("[提示] 重试启动 BLE ...")
                    svc.attach()
                # PC 串口遥控：先发 REMOTE 握手，之后每行是一条指令。
                # 解析在 app/uartcmd.py，有屏模式共用同一套指令
                line = poll(svc.h, svc)
                if line == "REMOTE":
                    print("遥控模式：按键已禁用（发 END 或按板子 RST 恢复）")
                if ev == "long":
                    print("长按 BOOT → 进入配置模式：关 BLE → 开 WiFi")
                    svc.detach()            # 先彻底关 BLE，绝不共存
                    time.sleep_ms(300)
                    ip = _start_network()
                    if ip:
                        srv = Server(svc, ip)
                        srv.start(port)
                        dns = CaptiveDNS(ip)
                        in_config = True
                        cfg_ip = ip
                        print("配置模式就绪： http://%s" % ip)
                        print("（连上 WiFi 后手机通常会自动弹出配置页）")
                    else:
                        print("网络启动失败，回到运行态并重启 BLE")
                        _stop_network()
                        time.sleep_ms(200)
                        svc.attach()
                elif ev == "short":
                    # 运行态：WiFi 已关，可以安全地开始 / 暂停
                    if svc.running:
                        svc.stop()
                        print("[短按] 暂停自动滑动")
                    elif svc.start():
                        print("[短按] 开始自动滑动")
                    else:
                        print("[短按] 未连接蓝牙，无法启动")
                        print("       先让手机在蓝牙列表里连上 esp-bot 再按")
                    last_print = 0
                time.sleep_ms(30)
    except KeyboardInterrupt:
        pass
    finally:
        if srv:
            srv.stop()
        if dns:
            dns.stop()
        _stop_network()
        svc.deinit()
        gc.collect()
