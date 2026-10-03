#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ESP32 MicroPython 部署工具（Windows / CH340 通用）
=================================================
烧固件用 esptool（缺失时自动 pip 安装）；
传文件用自实现的 MicroPython raw REPL 协议，只依赖 pyserial，无需 mpremote / Thonny。

用法：
    python esp32_tool.py              # 交互式菜单
    python esp32_tool.py --port COM3  # 跳过串口选择
"""
import os
import re
import sys
import time
import subprocess
import posixpath

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("缺少 pyserial，请先执行： python -m pip install pyserial")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))

# 默认上传目录：本工具同级的 ESP32 版本固件目录
DEFAULT_UPLOAD_DIR = os.path.join(HERE, "TouchBot-ESP32_wifiset")

# 固件 .bin 搜索路径
FIRMWARE_DIRS = [HERE, os.path.join(HERE, "000固件和说明"), os.getcwd()]

# 上传时跳过的目录 / 文件
SKIP_DIRS = {"__pycache__", ".git", ".codebuddy", ".vscode", ".idea"}
SKIP_EXT = {".pyc", ".mpy", ".tmp", ".bak"}
# 文档类文件：普通上传（菜单 3/6）排除；量产模式（99）全量上传
DOC_EXT = {".md", ".txt"}

BAUD_REPL = 115200    # REPL 波特率
BAUDS = (115200,)     # 连接时使用的波特率（仅 115200）
CHUNK = 512          # raw REPL 单帧写入字节数
FLASH_ADDR = "0x1000"  # 初代 ESP32 固件起始地址（ESP32-C3 用 0x0）


# ══════════════════ 串口 ══════════════════

def scan_ports():
    """扫描串口，返回 [(device, desc), ...]"""
    items = []
    for p in sorted(serial.tools.list_ports.comports()):
        desc = " ".join(x for x in (p.description, p.manufacturer, p.hwid)
                        if x)
        items.append((p.device, desc))
    return items


def choose_port():
    ports = scan_ports()
    if not ports:
        print("\n未扫描到任何串口。请检查：")
        print("  1. USB 转串口模块已插入")
        print("  2. CH340 驱动已安装（设备管理器里应出现 COMx）")
        return None

    print("\n扫描到的串口：")
    for i, (dev, desc) in enumerate(ports, 1):
        mark = "  <-- CH340/CH341" if ("CH34" in desc.upper()) else ""
        print("  [%d] %-6s %s%s" % (i, dev, desc[:60], mark))

    if len(ports) == 1:
        sel = input("\n只有 1 个串口，回车使用 %s： " % ports[0][0]).strip()
        if sel == "":
            return ports[0][0]
    else:
        sel = input("\n选择串口序号（直接回车选第 1 个）： ").strip()
        if sel == "":
            return ports[0][0]

    try:
        return ports[int(sel) - 1][0]
    except (ValueError, IndexError):
        print("输入无效")
        return None


# ══════════════════ esptool ══════════════════

def ensure_esptool():
    """确保 esptool 可用，缺失则自动安装。返回命令前缀 list 或 None"""
    try:
        import esptool  # noqa
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    if subprocess.call([sys.executable, "-m", "esptool", "version"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL) == 0:
        return [sys.executable, "-m", "esptool"]

    print("\n未检测到 esptool（烧固件需要它）。")
    ans = input("是否现在自动安装？(Y/n) ").strip().lower()
    if ans not in ("", "y", "yes"):
        return None
    print("正在安装 esptool ...")
    r = subprocess.call([sys.executable, "-m", "pip", "install", "-U", "esptool"])
    if r != 0:
        print("安装失败，请手动执行： python -m pip install -U esptool")
        return None
    return [sys.executable, "-m", "esptool"]


def run_esptool(cmd, port, extra):
    full = cmd + ["--port", port] + extra
    print("\n> " + " ".join(full))
    return subprocess.call(full)


def find_firmware():
    """在常见目录里找 .bin 固件"""
    found = []
    seen = set()
    for d in FIRMWARE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(".bin") and name not in seen:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    seen.add(name)
                    found.append(p)
    return found


def pick_firmware():
    bins = find_firmware()
    if not bins:
        path = input("\n未自动找到 .bin，请手动输入固件完整路径： ").strip().strip('"')
        return path if os.path.isfile(path) else None
    print("\n找到固件：")
    for i, p in enumerate(bins, 1):
        print("  [%d] %s  (%.1f KB)" % (i, os.path.basename(p),
                                        os.path.getsize(p) / 1024))
    sel = input("\n选择序号（直接回车选第 1 个）： ").strip()
    try:
        idx = int(sel) - 1 if sel else 0
        return bins[idx]
    except (ValueError, IndexError):
        print("输入无效")
        return None


# ══════════════════ MicroPython raw REPL ══════════════════

class Board:
    """通过 raw REPL 与 MicroPython 通信"""

    def __init__(self, port, baud=BAUD_REPL):
        self.ser = serial.Serial(port, baud, timeout=0.5, write_timeout=5)
        # 复位一下，确保处于 REPL
        self.ser.setDTR(False)
        self.ser.setRTS(True)
        time.sleep(0.1)
        self.ser.setRTS(False)
        time.sleep(0.8)                 # 等芯片完成启动，太早发 Ctrl-C/A 会被吞掉
        self.ser.reset_input_buffer()   # 丢弃启动横幅，保持输入干净
        self.in_raw = False

    def close(self):
        try:
            if self.in_raw:
                self.exit_raw()
        except Exception:
            pass
        self.ser.close()

    # ── 底层 ──

    def _read_until(self, ending, timeout=15):
        data = b""
        deadline = time.time() + timeout
        while not data.endswith(ending):
            if time.time() > deadline:
                raise TimeoutError("等待 %r 超时，已收到: %r" % (ending, data[:200]))
            b = self.ser.read(1)
            if b:
                data += b
        return data

    def enter_raw(self):
        if self.in_raw:
            return
        for attempt in (1, 2):
            try:
                self.ser.reset_input_buffer()
                for _ in range(2):
                    self.ser.write(b"\r\x03")   # Ctrl-C 打断正在跑的程序
                    time.sleep(0.15)
                self.ser.reset_input_buffer()
                self.ser.write(b"\r\x01")       # Ctrl-A 进 raw REPL
                self._read_until(b"raw REPL; CTRL-B to exit\r\n>", timeout=4)
                self.in_raw = True
                return
            except TimeoutError:
                if attempt == 2:
                    raise
                time.sleep(0.5)                 # 芯片可能还在启动，稍等再试一轮

    def exit_raw(self):
        if not self.in_raw:
            return
        self.ser.write(b"\x02")           # Ctrl-B 回友好 REPL
        time.sleep(0.1)
        self.in_raw = False

    def exec_raw(self, code, timeout=30):
        """在 raw REPL 里执行一段代码，返回输出（含 \\x04 之后的内容）"""
        self.enter_raw()
        if isinstance(code, str):
            code = code.encode("utf-8")
        for i in range(0, len(code), CHUNK):
            self.ser.write(code[i:i + CHUNK])
            self.ser.flush()
            time.sleep(0.002)
        self.ser.write(b"\x04")           # Ctrl-D 执行
        self.ser.flush()

        out = b""
        deadline = time.time() + timeout
        last = time.time()
        while time.time() < deadline:
            n = self.ser.in_waiting
            if n:
                out += self.ser.read(n)
                last = time.time()
                if out.endswith(b"\x04>"):
                    break
            else:
                if out and time.time() - last > 1.5:
                    break
                time.sleep(0.02)
        return out

    # ── 文件操作 ──

    def mkdirs(self, dirs):
        if not dirs:
            return
        code = "import os\n"
        for d in dirs:
            parts = d.split("/")
            cur = ""
            for p in parts:
                cur = (cur + "/" + p) if cur else p
                code += "try:\n os.mkdir('%s')\nexcept OSError:\n pass\n" % cur
        self.exec_raw(code)

    def put_file(self, local, remote, progress=None, batch=8):
        """上传文件。batch = 每次 raw REPL 往返打包的写入块数（8 块 ≈ 4KB，
        把往返开销摊薄，接近 115200 串口的极限速度）"""
        data = open(local, "rb").read()
        total = len(data)
        self.exec_raw("f=open('%s','wb')\n" % remote)
        sent = 0
        i = 0
        while i < total:
            code = ""
            for _ in range(batch):
                if i >= total:
                    break
                part = data[i:i + CHUNK]
                code += "f.write(%r)\n" % part
                i += len(part)
            out = self.exec_raw(code)
            if out[:1] == b"E":          # raw REPL 出错时以 E 开头
                raise OSError("板子写入出错，请重试（或把 batch 调小）")
            sent = i
            if progress:
                progress(sent, total)
        self.exec_raw("f.close()\n")
        if progress:
            progress(total, total)
        # 校验大小（用 SIZE: 标记定位，raw REPL 应答带 OK 前缀和控制字符，
        # 旧的 split("\r\n")[-2] 会解析到 "OK373" 而必然失败）
        out = self.exec_raw(
            "import os\nprint('SIZE:', os.stat('%s')[6])\n" % remote)
        m = re.search(rb"SIZE:\s*(\d+)", out)
        got = int(m.group(1)) if m else -1
        return got == len(data), len(data), got

    def free_space(self):
        """查询板子文件系统剩余空间（字节）；读取失败返回 -1"""
        out = self.exec_raw(
            "import os\ns = os.statvfs('/')\nprint('FREE:', s[1] * s[3])\n")
        m = re.search(rb"FREE:\s*(\d+)", out)
        return int(m.group(1)) if m else -1

    def get_file(self, remote, local, expected=-1):
        """从板子下载文件到本地（base64 传输，膨胀 1.33x + 大小校验）。
        返回 (成功, 字节数)"""
        code = ("import binascii\n"
                "print('BEGIN')\n"
                "print(binascii.b2a_base64(open('%s','rb').read())"
                ".decode().strip())\n"
                "print('END')\n" % remote)
        out = self.exec_raw(code, timeout=60)
        txt = out.decode("utf-8", "replace")
        m = re.search(r"BEGIN\r\n([A-Za-z0-9+/=]+)\r\nEND", txt)
        if not m:
            return False, 0
        try:
            data = bytes.fromhex(m.group(1))
        except Exception:
            return False, 0
        if expected >= 0 and len(data) != expected:
            return False, len(data)
        d = os.path.dirname(local)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(local, "wb") as f:
            f.write(data)
        return True, len(data)


def connect_board(port, retries=2):
    """打开串口并确认 REPL 可用（固定 115200），
    失败自动重试（刚烧完固件/复位后常见连不上）。成功返回 Board。"""
    for _attempt in range(1, retries + 1):
        # 最多原地试 3 次：收到 rst 启动日志说明连接没错，
        # 只是板子还在启动，应等一等再重试
        for _k in range(3):
            sys.stdout.write("  [连接] %d baud ... " % BAUD_REPL)
            sys.stdout.flush()
            board = None
            retry_same = False
            try:
                board = Board(port, BAUD_REPL)
                out = board.exec_raw("print('connected')\n", timeout=5)
                if b"connected" in out:
                    print("OK")
                    return board
                print("无应答，重试")
            except TimeoutError as e:
                if "rst:0x" in str(e) or "POWERON_RESET" in str(e):
                    print("板子启动中，原地重试")
                    retry_same = True
                else:
                    print("无数据，重试")
            except Exception as e:
                print("失败(%s)" % e)
            if board is not None:
                try:
                    board.close()
                except Exception:
                    pass
            if not retry_same:
                break
        time.sleep(1.5)
    return None


# ══════════════════ 上传目录 ══════════════════

def collect_files(root, include_boot=True, all_files=False):
    """收集要上传的文件：[(本地绝对路径, 板内相对路径), ...]

    all_files=False（普通上传，菜单 3/6）：排除 .md/.txt 等文档文件；
    all_files=True（量产模式，菜单 99）：全量上传，含文档与 boot.py。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXT:
                continue
            if not all_files and ext in DOC_EXT:
                continue
            local = os.path.join(dirpath, fn)
            rel = os.path.relpath(local, root).replace("\\", "/")
            out.append((local, rel))
    # boot.py 永远放最后传，避免启动阶段出错导致反复重启
    out.sort(key=lambda x: (x[1] == "boot.py"))
    if not include_boot:
        out = [x for x in out if x[1] != "boot.py"]
    return out


def _progress_bar(done, total, label, idx=None, count=None, width=20):
    """在同一行刷新进度条（用 \\r 覆盖）。done==total 时不换行，由调用方补结果行。"""
    pct = (done * 100 // total) if total else 100
    filled = (width * done // total) if total else width
    bar = "#" * filled + "-" * (width - filled)
    head = ("(%d/%d) " % (idx, count)) if (idx is not None and count) else ""
    sys.stdout.write("\r  %s[%-*s] %3d%%  %-20s %7d B"
                     % (head, width, bar, pct, label, done))
    sys.stdout.flush()


def _fmt(n):
    """字节数转可读文本"""
    if n >= 1024 * 1024:
        return "%.2f MB" % (n / 1048576.0)
    return "%.1f KB" % (n / 1024.0)


def ask_ok(prompt):
    """统一确认框：直接回车=是，输入 n/N=否"""
    ans = input(prompt.rstrip("：: ") + " (Y/n，直接回车=是)：").strip().lower()
    return ans not in ("n", "no")


def connect_board_manual(port):
    """连接板子；连不上时引导手动按 RST 重试，输入 q 放弃（返回 None）"""
    while True:
        board = connect_board(port)
        if board is not None:
            return board
        ans = input("\n连不上 REPL。请按一下板上的 RST/复位键（或拔插 USB），"
                    "回车重试（q 放弃）： ").strip().lower()
        if ans in ("q", "quit", "exit"):
            return None


def do_upload(port, directory, include_boot=True, confirm=True, clean=False,
              ask_reset=False, all_files=False):
    if not os.path.isdir(directory):
        print("目录不存在：%s" % directory)
        return False
    files = collect_files(directory, include_boot, all_files)
    if not files:
        print("目录里没有可上传的文件")
        return False

    print("\n待上传 %d 个文件，来自：\n  %s%s"
          % (len(files), directory,
             "" if all_files else "（已排除 .md/.txt 等文档文件）"))
    for _, rel in files:
        print("    %s%s" % (rel, "（最后传）" if rel == "boot.py" else ""))
    if not include_boot:
        print("\n  注：本次【跳过 boot.py】。main.py 会照常开机自启，")
        print("      只是没有开机动画；验证通过后用菜单 9 单独上传 boot.py。")

    if confirm and not ask_ok("\n确认上传？"):
        print("已取消")
        return False

    print("\n连接板子（115200，未连上时最长约 1 分钟）...")
    if ask_reset:
        # 部分板子的 RTS 自动复位电路不可靠，烧录后可能停在下载模式不重启
        ans = input("\n板子可能没有自动重启：如串口无输出，请按一下 RST 键；"
                    "回车开始连接（q 放弃）： ")
        if ans.strip().lower() in ("q", "quit", "exit"):
            print("已放弃")
            return False
    board = connect_board_manual(port)
    if board is None:
        print("\n已放弃上传（未进入 REPL）。")
        return False
    try:
        if clean:
            # 彻底清空：连 boot.py 一起删，板子上只留本次上传的文件
            print("\n清空板子上的全部旧文件（含 boot.py）...")
            if not _wipe_fs(board, None):
                print("清空未确认完成，中止上传（重试本菜单即可）。")
                return False

        # ── 剩余空间检查：不足则降级（量产去掉文档类文件），再不足就报错 ──
        need = sum(os.path.getsize(p) for p, _ in files)
        free = board.free_space()
        if free < 0:
            print("\n[提示] 无法读取板子剩余空间，跳过空间检查")
        else:
            margin = 32 * 1024        # FAT 簇 / 目录项开销余量
            if need + margin > free:
                slim = collect_files(directory, include_boot, all_files=False)
                slim_need = sum(os.path.getsize(p) for p, _ in slim)
                if all_files and slim_need + margin <= free:
                    print("\n[空间] 板子剩余 %s，全量上传需 %s —— "
                          "已自动排除 .md/.txt 等文档（省 %s）"
                          % (_fmt(free), _fmt(need),
                             _fmt(need - slim_need)))
                    files = slim
                    need = slim_need
                else:
                    print("\n[错误] 板子剩余空间不足：剩余 %s，"
                          "本次需 %s（已含开销余量）。中止上传。"
                          % (_fmt(free), _fmt(need + margin)))
                    if not all_files:
                        print("       普通上传已不含文档文件；"
                              "请清空板子或减少上传内容。")
                    return False
            print("[空间] 板子剩余 %s，本次需 %s，上传后约剩 %s"
                  % (_fmt(free), _fmt(need), _fmt(free - need)))

        dirs = sorted({posixpath.dirname(r) for _, r in files if "/" in r})
        if dirs:
            print("\n创建目录：%s" % ", ".join(dirs))
            board.mkdirs(dirs)

        total_files = len(files)
        ok = 0
        for k, (local, rel) in enumerate(files, 1):
            good, want, got = board.put_file(
                local, rel,
                progress=lambda d, t, _r=rel, _k=k, _n=total_files:
                    _progress_bar(d, t, _r, _k, _n))
            flag = "OK" if good else "!! %d!=%d" % (got, want)
            # 进度条最后一行用 \r 覆盖成结果行（补空格盖掉进度条残留）
            sys.stdout.write("\r  [%-12s] %-24s %6d B%24s\n"
                             % (flag, rel, want, ""))
            sys.stdout.flush()
            if good:
                ok += 1
        print("\n完成：%d/%d" % (ok, total_files))
        board.exit_raw()
        return ok == len(files)
    except Exception as e:
        print("\n上传中断：%s" % e)
        print("已传文件保留在板上；可按 RST 后重试本菜单（重复上传是安全的）。")
        return False
    finally:
        board.close()


def _wipe_fs(board, keep=None):
    """删除板子 / 下的所有用户文件（keep 指定保留的根目录文件名，
    如 'boot.py'——清空时保留它以免丢失波特率等开机设置）。
    返回 True 表示确认完成（CLEAN_DONE）。"""
    skip = ""
    if keep:
        skip = ("  if p == '' and n == '%s':\n"
                "   print('keep', n)\n"
                "   continue\n" % keep)
    out = board.exec_raw(
        "import os\n"
        "def _rm(p):\n"
        " for e in os.ilistdir(p):\n"
        "  n = e[0]\n"
        "  f = e[1]\n"
        "  q = (p + '/' + n) if p else n\n"
        + skip +
        "  if f & 0x4000:\n"
        "   _rm(q)\n"
        "   os.rmdir(q)\n"
        "  else:\n"
        "   os.remove(q)\n"
        "   print('del', q)\n"
        "_rm('')\n"
        "print('CLEAN_DONE')\n", timeout=20)
    txt = out.decode("utf-8", "replace")
    txt = txt.replace("OK", "", 1).replace("\x04>", "").strip()
    lines = [l for l in txt.splitlines() if l and l != "CLEAN_DONE"]
    if lines:
        for l in lines:
            print("  " + l)
    else:
        print("  (板上已是空的)")
    return "CLEAN_DONE" in txt


def do_pull(port):
    """把板子上的所有文件下载回电脑（默认保存到 ./板子备份）"""
    dest = input("下载保存到目录（回车 = ./板子备份）： ").strip().strip('"')
    if not dest:
        dest = os.path.join(HERE, "板子备份")
    os.makedirs(dest, exist_ok=True)
    board = connect_board_manual(port)
    if board is None:
        return
    try:
        out = board.exec_raw(
            "import os\n"
            "def w(p):\n"
            " for e in os.ilistdir(p):\n"
            "  n=e[0];f=e[1]\n"
            "  q=(p+'/'+n) if p else n\n"
            "  if f&0x4000: w(q)\n"
            "  else: print('F', q, os.stat(q)[6])\n"
            "w('')\n")
        txt = out.decode("utf-8", "replace").replace("OK", "", 1)
        files = []
        for l in txt.splitlines():
            l = l.strip()
            if l.startswith("F "):
                parts = l.split(" ")
                if len(parts) >= 3:
                    files.append((" ".join(parts[1:-1]), int(parts[-1])))
        if not files:
            print("板子上没有可下载的文件")
            return
        print("\n待下载 %d 个文件，保存到：%s" % (len(files), dest))
        ok = 0
        for rel, size in files:
            local = os.path.join(dest, rel.replace("/", os.sep))
            good, got = board.get_file(rel, local, size)
            flag = "OK" if good else "!!"
            print("  [%-2s] %-28s %6d B" % (flag, rel, got))
            if good:
                ok += 1
        print("\n完成：%d/%d" % (ok, len(files)))
    finally:
        board.close()


def do_mass(port, upload_dir):
    """量产模式：循环执行 菜单6（擦除 + 烧固件 + 清空并上传代码），
    固件只选一次，每块板子按一次回车，直到输入 q 退出。
    上传为全量模式：文件夹下所有文件（含 .md 文档、boot.py）都带上。"""
    print("\n===== 量产模式 =====")
    print("每轮：擦除 flash + 烧录固件 + 清空并上传代码（全量，含 .md / boot.py）。")
    print("换板时按回车继续；输入 q 退出。")
    cmd = ensure_esptool()
    if not cmd:
        return
    fw = pick_firmware()
    if not fw:
        return
    print("\n使用固件：%s" % fw)
    ok_n = fail_n = 0
    n = 0
    while True:
        n += 1
        ans = input("\n[第 %d 块] 接好板子，回车开始（q 退出）： " % n).strip().lower()
        if ans in ("q", "quit", "exit"):
            break
        good = False
        t0 = time.time()
        if run_esptool(cmd, port, ["erase_flash"]) == 0 \
                and run_esptool(cmd, port, ["--baud", "921600",
                                            "write_flash", FLASH_ADDR, fw]) == 0:
            print("\n固件烧录完成，2 秒后开始上传代码...")
            time.sleep(2)
            good = do_upload(port, upload_dir, include_boot=True,
                             confirm=False, clean=True, ask_reset=True,
                             all_files=True)
        if good:
            ok_n += 1
            print(">>> 第 %d 块 OK（用时 %.0f 秒）  累计：成功 %d / 失败 %d"
                  % (n, time.time() - t0, ok_n, fail_n))
        else:
            fail_n += 1
            print(">>> 第 %d 块 失败  累计：成功 %d / 失败 %d"
                  % (n, ok_n, fail_n))
    print("\n量产结束：成功 %d，失败 %d，共 %d 块" % (ok_n, fail_n, n - 1))


def do_list(port):
    board = connect_board_manual(port)
    if board is None:
        return
    try:
        out = board.exec_raw(
            "import os\ndef w(p):\n"
            " for e in sorted(os.ilistdir(p)):\n"
            "  n=e[0];f=e[1]\n"
            "  print(('D ' if f&0x4000 else 'F ')+p+'/'+n)\n"
            "  if f&0x4000: w(p+'/'+n)\n"
            "w('')\n")
        print("\n板子上的文件：")
        print(out.decode("utf-8", "replace").replace("\x04>", "").strip())
    finally:
        board.close()


def _probe_baud(port):
    """探测 REPL 波特率：复位后发 Ctrl-C+回车，回复里乱码占比低即为正确波特率（仅测 115200）"""
    for baud in BAUDS:
        try:
            s = serial.Serial(port, baud, timeout=0.4)
        except Exception:
            continue
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.1)
        s.setRTS(False)
        time.sleep(0.8)
        s.reset_input_buffer()
        s.write(b"\r\x03\r\n")
        time.sleep(0.4)
        data = s.read(300)
        s.close()
        if data:
            txt = data.decode("utf-8", "replace")
            if txt.count("\ufffd") / len(txt) < 0.1:   # 乱码少 → 波特率对了
                return baud
    return BAUD_REPL


def do_repl(port):
    baud = _probe_baud(port)
    print("\n进入 REPL（%d baud），按 Ctrl-] 退出。" % baud)
    ser = serial.Serial(port, baud, timeout=0.2)
    ser.setDTR(False)                    # 释放复位电路，避免芯片被按住
    ser.setRTS(False)
    try:
        ser.write(b"\r\x03")
        while True:
            n = ser.in_waiting
            if n:
                sys.stdout.write(ser.read(n).decode("utf-8", "replace"))
                sys.stdout.flush()
            try:
                import msvcrt
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x1d":      # Ctrl-]
                        break
                    ser.write(ch.encode("utf-8"))
            except ImportError:
                pass
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


# ══════════════════ 菜单 ══════════════════

def menu(port, upload_dir):
    # 默认跳过 boot.py：它开机即执行，万一有错板子会反复重启、串口连不上。
    # 先用菜单 3 传其余文件验证 main.py 能跑，再用菜单 9 单独补传 boot.py。
    upload_boot = False

    while True:
        print("\n" + "=" * 58)
        print(" 串口: %s    上传目录: %s" % (port, os.path.basename(upload_dir)))
        print(" boot.py: %s" % ("包含" if upload_boot else "跳过"))
        print("=" * 58)
        print("  1. esptool_擦除 flash")
        print("  2. esptool_烧录 MicroPython 固件")
        print("  3. 上传py代码目录到板子（注意 boot 开关）")
        print("  6. 一键：1擦除 + 2烧录 + 3上传")
        print("  7. 列出板子上的所有文件")
        print("  8. 下载板子上的文件回电脑")
        print("  9. 单独上传 boot.py（验证 main.py 正确后再做）")
        print("  99. 量产模式：循环执行 6（换板续跑，q 退出）")
        print("  a. 更换上传目录")
        print("  b. 切换 boot.py 上传开关")
        print("  0. 退出")
        sel = input("\n选择： ").strip().lower()

        if sel == "1":
            if not ask_ok("确认擦除 flash？"):
                print("已取消")
                continue
            cmd = ensure_esptool()
            if cmd:
                run_esptool(cmd, port, ["erase_flash"])
        elif sel == "2":
            cmd = ensure_esptool()
            fw = pick_firmware() if cmd else None
            if cmd and fw:
                if not ask_ok("确认烧录 %s？" % os.path.basename(fw)):
                    print("已取消")
                    continue
                run_esptool(cmd, port, ["--baud", "921600", "write_flash",
                                        FLASH_ADDR, fw])
        elif sel == "3":
            clean = ask_ok("上传前清空板子上的全部旧文件（含 boot.py）？")
            do_upload(port, upload_dir, upload_boot, clean=clean)
        elif sel == "6":
            if not ask_ok("\n确认执行：擦除 + 烧录固件 + 清空并上传代码？"):
                print("已取消")
                continue
            cmd = ensure_esptool()
            if cmd:
                run_esptool(cmd, port, ["erase_flash"])
                fw = pick_firmware()
                if fw and run_esptool(cmd, port, ["--baud", "921600",
                                                  "write_flash", FLASH_ADDR, fw]) == 0:
                    print("\n固件烧录完成，2 秒后开始上传代码...")
                    time.sleep(2)
                    do_upload(port, upload_dir, upload_boot,
                              confirm=False, clean=True, ask_reset=True)
        elif sel == "7":
            do_list(port)
        elif sel == "8":
            do_pull(port)
        elif sel == "99":
            do_mass(port, upload_dir)
        elif sel == "9":
            p = os.path.join(upload_dir, "boot.py")
            if not os.path.isfile(p):
                print("未找到：%s" % p)
                continue
            board = connect_board_manual(port)
            if board is None:
                continue
            try:
                good, want, got = board.put_file(p, "boot.py")
                print("  [%-12s] boot.py %6d B" % ("OK" if good else "!!%d!=%d" % (got, want), want))
                board.exit_raw()
            finally:
                board.close()
        elif sel == "r":                      # 隐藏入口：REPL 调试
            do_repl(port)
        elif sel == "a":
            d = input("新的上传目录（回车保持当前）： ").strip().strip('"')
            if d and os.path.isdir(d):
                upload_dir = d
            elif d:
                print("目录不存在：%s" % d)
        elif sel == "b":
            upload_boot = not upload_boot
            print("boot.py 上传：%s" % ("包含" if upload_boot else "跳过"))
        elif sel == "0":
            print("再见")
            return
        else:
            print("无效选择")


def main():
    print("=" * 58)
    print(" ESP32 MicroPython 部署工具")
    print("=" * 58)

    port = None
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            port = sys.argv[i + 1]
    if not port:
        port = choose_port()
    if not port:
        sys.exit(1)

    upload_dir = DEFAULT_UPLOAD_DIR
    if not os.path.isdir(upload_dir):
        upload_dir = HERE

    print("\n默认上传目录：%s" % upload_dir)
    print("（菜单里按 a 可更换）")

    menu(port, upload_dir)


if __name__ == "__main__":
    main()
