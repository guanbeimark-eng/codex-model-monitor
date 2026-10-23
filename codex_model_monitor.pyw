# -*- coding: utf-8 -*-
"""
Codex 模型可用性监测 —— 桌面小猫悬浮窗 (Windows)
只用 Python 标准库，无需 pip 安装任何东西。

两层检测：
  1) 目录检测（免费）：`codex debug models`，失败时读 %USERPROFILE%\\.codex\\models_cache.json
  2) 实测（消耗极少额度）：对每个模型跑一次 `codex exec`，能回复 = 真能用；多个模型并行测

三种显示：
  面板模式：完整列表；鼠标移开自动变淡
  小猫模式：只剩一只小猫（双击标题栏进入，单击小猫展开），表情代表状态
  完全隐藏：右键菜单选择，按 Ctrl+Alt+M 唤回/隐藏
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "1.1.0"

# ======================= 可改的设置 =======================
CATALOG_INTERVAL_MIN = 2       # 目录检测间隔（分钟），免费
PROBE_INTERVAL_MIN = 15        # 全部实测间隔（分钟），0 = 只手动实测
RETRY_BAD_MIN = 5              # 失败/限流的模型多久重测一次（分钟），0 = 不单独重测
PROBE_WORKERS = 3              # 同时实测几个模型
PROBE_ON_START = True          # 启动时是否立刻实测一次
PROBE_TIMEOUT_SEC = 120        # 单个模型实测超时
CATALOG_TIMEOUT_SEC = 60       # 目录检测超时
IDLE_ALPHA = 0.35              # 鼠标不在窗口上时的透明度（隐身程度，越小越透明）
ACTIVE_ALPHA = 0.96            # 鼠标悬停时的透明度
CODEX_PATH = ""                # 留空自动查找；也可在右键菜单里手动选择
SHOW_HIDDEN = False            # 是否默认显示目录里标记为隐藏的模型
# =========================================================

REPO_RAW = "https://raw.githubusercontent.com/guanbeimark-eng/codex-model-monitor/main/codex_model_monitor.pyw"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "codex_model_monitor")
STATE_FILE = os.path.join(APP_DIR, "state.json")
LOG_FILE = os.path.join(APP_DIR, "monitor.log")
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.join(os.path.expanduser("~"), ".codex"))
IS_WIN = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WIN else 0

os.makedirs(APP_DIR, exist_ok=True)


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 512 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


# ----------------------- 查找 codex -----------------------

USER_CODEX = ""        # 用户在菜单里手动选择的位置（启动时从状态文件读）
_codex_cache = None


def _registry_path():
    """读注册表里的 PATH：开机自启时进程拿到的 PATH 可能是旧的。"""
    if not IS_WIN:
        return ""
    import winreg
    parts = []
    for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except Exception:
            pass
    return os.pathsep.join(parts)


def _walk_find(base, names, max_depth=3):
    if not base or not os.path.isdir(base):
        return None
    base_depth = base.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(base):
        if root.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d.lower() not in ("node_modules", "cache", "temp", "logs")]
        for n in files:
            if n.lower() in names:
                return os.path.join(root, n)
    return None


def find_codex():
    global _codex_cache
    for p in (USER_CODEX, CODEX_PATH, _codex_cache):
        if p and os.path.exists(p):
            return p
    path = os.environ.get("PATH", "") + os.pathsep + _registry_path()
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name, path=path)
        if p:
            _codex_cache = p
            return p
    env = os.environ.get
    candidates = [
        os.path.join(env("APPDATA", ""), "npm"),
        os.path.join(env("LOCALAPPDATA", ""), "pnpm"),
        os.path.join(env("LOCALAPPDATA", ""), "Volta", "bin"),
        os.path.join(os.path.expanduser("~"), "scoop", "shims"),
        os.path.join(os.path.expanduser("~"), ".codex"),
        os.path.join(os.path.expanduser("~"), ".local", "bin"),
        os.path.join(env("LOCALAPPDATA", ""), "Programs"),
        os.path.join(env("LOCALAPPDATA", ""), "OpenAI"),
        os.path.join(env("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
        env("ProgramFiles", ""),
    ]
    for base in candidates:
        p = _walk_find(base, ("codex.cmd", "codex.exe"))
        if p:
            _codex_cache = p
            log(f"自动找到 codex: {p}")
            return p
    return None


# ----------------------- 运行 codex -----------------------

def run_codex(args, timeout, cwd=None):
    """返回 (returncode, stdout, stderr)；超时返回 (-9, '', 'timeout')。永不抛异常。"""
    exe = find_codex()
    if not exe:
        return -1, "", "找不到 codex（右键 → 选择 codex 位置）"
    if exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd.exe", "/d", "/c", exe] + args
    else:
        argv = [exe] + args
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    except Exception as e:
        return -1, "", f"启动 codex 失败: {e}"
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p.pid)
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        return -9, "", "timeout"
    dec = lambda b: (b or b"").decode("utf-8", errors="replace")
    return p.returncode, dec(out), dec(err)


def kill_tree(pid):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


# ----------------------- 解析模型目录 -----------------------

def parse_json_loose(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except Exception:
                continue
    return None


ID_KEYS = ("slug", "id", "model", "name")


def _candidate_lists(obj, depth=0):
    if depth > 6:
        return
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and sum(1 for d in dicts if any(isinstance(d.get(k), str) for k in ID_KEYS)) >= max(1, len(dicts) // 2):
            yield dicts
        for x in obj:
            yield from _candidate_lists(x, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _candidate_lists(v, depth + 1)


def extract_models(obj):
    lists = list(_candidate_lists(obj))
    if not lists:
        return []
    models, seen = [], set()
    for d in max(lists, key=len):
        mid = next((d[k] for k in ID_KEYS if isinstance(d.get(k), str) and d[k].strip()), None)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        vis = str(d.get("visibility", "")).lower()
        hidden = (vis in ("hide", "hidden", "none") or d.get("hidden") is True
                  or d.get("show_in_picker") is False or d.get("deprecated") is True)
        models.append({"id": mid, "label": str(d.get("display_name") or d.get("displayName") or mid),
                       "hidden": bool(hidden)})
    return models


def check_catalog():
    rc, out, err = run_codex(["debug", "models"], CATALOG_TIMEOUT_SEC)
    if rc == 0:
        models = extract_models(parse_json_loose(out))
        if models:
            return {"ok": True, "models": models, "source": "实时目录", "error": ""}
        log(f"debug models 输出无法解析: {out[:500]!r}")
    else:
        log(f"debug models 失败 rc={rc}: {(err or out)[:500]}")
    cache = os.path.join(CODEX_HOME, "models_cache.json")
    try:
        with open(cache, "r", encoding="utf-8") as f:
            models = extract_models(json.load(f))
        if models:
            age = int((time.time() - os.path.getmtime(cache)) / 60)
            return {"ok": True, "models": models, "source": f"本地缓存({age}分钟前)",
                    "error": short_reason(err or out)}
    except Exception as e:
        log(f"读取缓存失败: {e}")
    return {"ok": False, "models": [], "source": "", "error": short_reason(err or out) or "无法获取模型目录"}


# ----------------------- 实测单个模型 -----------------------

def short_reason(text):
    t = (text or "").lower()
    table = [
        (("找不到 codex",), "找不到 codex（右键 → 选择 codex 位置）"),
        (("not logged in", "login", "unauthorized", "401"), "未登录/登录失效"),
        (("usage limit", "rate limit", "429", "quota"), "额度/限流"),
        (("not supported", "does not exist", "not found", "unknown model",
          "model_not_found", "invalid model", "404"), "账号不可用该模型"),
        (("timeout",), "超时"),
        (("network", "connect", "dns", "ssl", "tls"), "网络问题"),
    ]
    for keys, msg in table:
        if any(k in t for k in keys):
            return msg
    line = next((l.strip() for l in (text or "").splitlines()[::-1] if l.strip()), "")
    return line[:60]


def probe_model(model_id):
    """返回 ('ok'|'fail'|'warn', 说明)。"""
    tmp = tempfile.mkdtemp(prefix="codex_probe_")
    args = ["exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "--json", "-C", tmp, "-m", model_id, "Reply with exactly: OK"]
    t0 = time.time()
    rc, out, err = run_codex(args, PROBE_TIMEOUT_SEC, cwd=tmp)
    secs = time.time() - t0
    shutil.rmtree(tmp, ignore_errors=True)
    if rc == 0 and ("agent_message" in out or "OK" in out):
        return "ok", f"{secs:.0f}s"
    reason = short_reason(err + "\n" + out) if rc != -9 else "超时"
    log(f"probe {model_id} rc={rc}: {(err or out)[-800:]}")
    level = "warn" if reason in ("额度/限流", "超时", "网络问题") else "fail"
    return level, reason or f"退出码 {rc}"


# ----------------------- 自动更新 -----------------------

def fetch_remote(timeout=15):
    req = urllib.request.Request(REPO_RAW + f"?t={int(time.time())}",
                                 headers={"User-Agent": "codex-model-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def remote_version(src):
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else None


def ver_tuple(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


# ----------------------- 状态保存 -----------------------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"保存状态失败: {e}")


# ----------------------- 全局热键 Ctrl+Alt+M -----------------------

def hotkey_thread(q):
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
        if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, ord("M")):
            log("热键 Ctrl+Alt+M 注册失败（可能被其他软件占用）")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                q.put(("hotkey", None))
    except Exception:
        log(traceback.format_exc())


# ----------------------- 小猫 -----------------------

C = {
    "bg": "#1e1f22", "head": "#2b2d31", "fg": "#e6e6e6", "dim": "#8a8f98",
    "ok": "#3ddc84", "catalog": "#5aa9ff", "fail": "#ff5c5c", "warn": "#f5b942",
    "new": "#ffd84d", "key": "#010203",
}
FUR, FUR_DARK, LINE, PINK, MUZZLE = "#f2a65a", "#d9853b", "#3a2a20", "#f7b6c2", "#fff4e6"


def draw_cat(cv, size, mood, blink=False, badge=None, badge_color=None, bg=None):
    """在画布上画一只橘猫。mood: happy / normal / checking / sad / sleepy"""
    cv.delete("all")
    k = size / 64.0
    S = lambda *p: [v * k for v in p]
    w = max(1, 1.4 * k)
    if bg:
        cv.configure(bg=bg)
    # 耳朵
    cv.create_polygon(S(9, 28, 13, 3, 29, 17), fill=FUR, outline=LINE, width=w)
    cv.create_polygon(S(55, 28, 51, 3, 35, 17), fill=FUR, outline=LINE, width=w)
    cv.create_polygon(S(14, 23, 15, 10, 24, 18), fill=PINK, outline="")
    cv.create_polygon(S(50, 23, 49, 10, 40, 18), fill=PINK, outline="")
    # 头
    cv.create_oval(S(5, 13, 59, 59), fill=FUR, outline=LINE, width=w)
    for x0, x1 in ((32, 32), (26, 27), (38, 37)):
        cv.create_line(S(x0, 15, x1, 21), fill=FUR_DARK, width=max(1, 2 * k), capstyle="round")
    cv.create_oval(S(21, 38, 43, 54), fill=MUZZLE, outline="")
    # 眼睛
    if mood == "sleepy" or blink:
        cv.create_line(S(18, 33, 27, 33), fill=LINE, width=max(1, 2 * k), capstyle="round")
        cv.create_line(S(37, 33, 46, 33), fill=LINE, width=max(1, 2 * k), capstyle="round")
    elif mood == "happy":
        cv.create_arc(S(18, 29, 28, 39), start=0, extent=180, style="arc", outline=LINE, width=max(1, 2 * k))
        cv.create_arc(S(36, 29, 46, 39), start=0, extent=180, style="arc", outline=LINE, width=max(1, 2 * k))
    else:
        dx = 2 if mood == "checking" else 0
        for x in (18, 37):
            cv.create_oval(S(x, 28, x + 9, 38), fill=LINE, outline="")
            cv.create_oval(S(x + 2 + dx, 30, x + 5 + dx, 33), fill="white", outline="")
    if mood == "sad":
        cv.create_line(S(17, 27, 26, 24), fill=LINE, width=max(1, 1.6 * k))
        cv.create_line(S(47, 27, 38, 24), fill=LINE, width=max(1, 1.6 * k))
        cv.create_oval(S(52, 16, 57, 23), fill="#7cc6ff", outline="")   # 汗珠
    # 腮红
    if mood == "happy":
        cv.create_oval(S(12, 40, 19, 45), fill=PINK, outline="")
        cv.create_oval(S(45, 40, 52, 45), fill=PINK, outline="")
    # 鼻子嘴巴
    cv.create_polygon(S(29.5, 40, 34.5, 40, 32, 43), fill="#e07a8a", outline="")
    if mood == "sad":
        cv.create_arc(S(28, 45, 36, 51), start=20, extent=140, style="arc", outline=LINE, width=w)
    else:
        cv.create_line(S(32, 43, 29, 46), fill=LINE, width=w, capstyle="round")
        cv.create_line(S(32, 43, 35, 46), fill=LINE, width=w, capstyle="round")
    # 胡须
    for a, b in (((21, 44), (5, 41)), ((21, 47), (6, 49)), ((43, 44), (59, 41)), ((43, 47), (58, 49))):
        cv.create_line(S(*a, *b), fill=LINE, width=max(1, 0.9 * k))
    # 角标
    if badge is not None:
        cv.create_oval(S(43, 44, 63, 63), fill=badge_color or C["catalog"], outline="white", width=max(1, 1.2 * k))
        cv.create_text(*S(53, 53.5), text=str(badge), fill="white",
                       font=("Segoe UI", max(7, int(9 * k)), "bold"))


# ----------------------- 界面 -----------------------

class App:
    def __init__(self):
        global USER_CODEX
        self.state = load_state()
        USER_CODEX = self.state.get("codex_path", "")
        self.models = self.state.get("models", [])
        self.probe = self.state.get("probe", {})              # id -> [level, text, ts]
        self.show_hidden = self.state.get("show_hidden", SHOW_HIDDEN)
        self.mode = self.state.get("mode", "panel")           # panel / cat
        self.status = "启动中…"
        self.changes = ""
        self.update_note = ""
        self.updated = self.state.get("updated", "")
        self.busy = threading.Lock()
        self.working = False
        self.q = queue.Queue()
        self.hidden = False
        self.blink = False
        now = time.time()
        self.next_catalog = 0
        self.next_probe = now + (3 if PROBE_ON_START else PROBE_INTERVAL_MIN * 60)
        self.next_retry = now + RETRY_BAD_MIN * 60

        r = self.root = tk.Tk()
        r.title("Codex 模型")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        if IS_WIN:
            try:
                r.attributes("-transparentcolor", C["key"])
            except Exception:
                pass
        self.alpha = None
        self.set_alpha(ACTIVE_ALPHA)
        r.configure(bg=C["bg"])
        r.geometry(self.state.get("pos", "+40+120"))

        # 面板
        self.panel = tk.Frame(r, bg=C["bg"])
        self.head = tk.Frame(self.panel, bg=C["head"])
        self.head.pack(fill="x")
        self.mini_cat = tk.Canvas(self.head, width=26, height=26, bg=C["head"], highlightthickness=0)
        self.mini_cat.pack(side="left", padx=(6, 0), pady=2)
        self.title_lbl = tk.Label(self.head, text="Codex 模型", bg=C["head"], fg=C["fg"],
                                  font=("Microsoft YaHei UI", 10, "bold"), padx=6, pady=4)
        self.title_lbl.pack(side="left")
        self.time_lbl = tk.Label(self.head, text="", bg=C["head"], fg=C["dim"],
                                 font=("Microsoft YaHei UI", 8), padx=6)
        self.time_lbl.pack(side="right")
        self.body = tk.Frame(self.panel, bg=C["bg"], padx=8, pady=4)
        self.body.pack(fill="both")
        self.foot = tk.Label(self.panel, text="", bg=C["bg"], fg=C["dim"], anchor="w", justify="left",
                             font=("Microsoft YaHei UI", 8), padx=8, pady=3, wraplength=270)
        self.foot.pack(fill="x")
        # 小猫
        self.big_cat = tk.Canvas(r, width=72, height=72, bg=C["key"], highlightthickness=0)

        for w in (self.head, self.mini_cat, self.title_lbl, self.time_lbl, self.big_cat):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
        for w in (self.head, self.mini_cat, self.title_lbl, self.time_lbl):
            w.bind("<ButtonRelease-1>", lambda e: self._save_pos())
            w.bind("<Double-Button-1>", lambda e: self.set_mode("cat"))
        self.big_cat.bind("<ButtonRelease-1>", self._cat_release)
        for w in (r, self.head, self.mini_cat, self.title_lbl, self.time_lbl, self.body, self.foot, self.big_cat):
            w.bind("<Button-3>", self.menu)

        self.set_mode(self.mode, save=False)
        threading.Thread(target=hotkey_thread, args=(self.q,), daemon=True).start()
        threading.Thread(target=self.check_update, args=(False,), daemon=True).start()
        r.after(300, self.tick)
        r.after(200, self.drain)
        r.after(400, self.hover_loop)
        r.after(3000, self.blink_loop)

    # ---- 窗口行为 ----
    def set_alpha(self, a):
        if a != self.alpha:
            self.alpha = a
            try:
                self.root.attributes("-alpha", a)
            except Exception:
                pass

    def hover_loop(self):
        try:
            x, y = self.root.winfo_pointerxy()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            inside = rx <= x < rx + self.root.winfo_width() and ry <= y < ry + self.root.winfo_height()
            self.set_alpha(ACTIVE_ALPHA if inside else IDLE_ALPHA)
        except Exception:
            pass
        self.root.after(250, self.hover_loop)

    def blink_loop(self):
        self.blink = True
        self.draw_cats()
        self.root.after(160, self._unblink)
        self.root.after(2500 + int(time.time() * 1000) % 2500, self.blink_loop)

    def _unblink(self):
        self.blink = False
        self.draw_cats()

    def _press(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
        self._sx, self._sy = e.x_root, e.y_root

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _cat_release(self, e):
        if abs(e.x_root - self._sx) < 4 and abs(e.y_root - self._sy) < 4:
            self.set_mode("panel")
        else:
            self._save_pos()

    def _save_pos(self):
        self.state["pos"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        save_state(self.state)

    def set_mode(self, mode, save=True):
        self.mode = mode
        if mode == "cat":
            self.panel.pack_forget()
            self.root.configure(bg=C["key"] if IS_WIN else C["bg"])
            self.big_cat.pack()
        else:
            self.big_cat.pack_forget()
            self.root.configure(bg=C["bg"])
            self.panel.pack(fill="both")
        if save:
            self.state["mode"] = mode
            save_state(self.state)
        self.render()

    def toggle_hidden_window(self):
        self.hidden = not self.hidden
        if self.hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新目录", command=lambda: self.start_job("catalog"))
        m.add_command(label="实测全部模型（耗少量额度）", command=lambda: self.start_job("all"))
        m.add_separator()
        m.add_command(label="展开面板" if self.mode == "cat" else "缩成小猫",
                      command=lambda: self.set_mode("panel" if self.mode == "cat" else "cat"))
        m.add_command(label="完全隐藏（Ctrl+Alt+M 唤回）", command=self.toggle_hidden_window)
        m.add_checkbutton(label="显示隐藏模型", command=self.toggle_show_hidden,
                          variable=tk.BooleanVar(value=self.show_hidden))
        m.add_separator()
        m.add_command(label="选择 codex 位置…", command=self.pick_codex)
        m.add_command(label=f"从 GitHub 更新（当前 v{VERSION}）",
                      command=lambda: threading.Thread(target=self.check_update, args=(True,), daemon=True).start())
        m.add_command(label="打开日志", command=self.open_log)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def open_log(self):
        if IS_WIN and os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)

    def pick_codex(self):
        global USER_CODEX, _codex_cache
        from tkinter import filedialog
        init = os.path.join(os.environ.get("APPDATA", ""), "npm")
        p = filedialog.askopenfilename(title="选择 codex.cmd 或 codex.exe",
                                       initialdir=init if os.path.isdir(init) else None,
                                       filetypes=[("codex", "codex.cmd codex.exe codex"), ("所有文件", "*.*")])
        if p:
            USER_CODEX = os.path.normpath(p)
            _codex_cache = None
            self.state["codex_path"] = USER_CODEX
            save_state(self.state)
            self.status = f"已设置 codex：{USER_CODEX}"
            self.render()
            self.start_job("all")

    def toggle_show_hidden(self):
        self.show_hidden = not self.show_hidden
        self.state["show_hidden"] = self.show_hidden
        save_state(self.state)
        self.render()

    def quit(self):
        self._save_pos()
        self.root.destroy()

    # ---- 更新 ----
    def check_update(self, apply):
        try:
            src = fetch_remote()
            rv = remote_version(src)
            if not rv:
                raise ValueError("远程文件没有版本号")
            if ver_tuple(rv) <= ver_tuple(VERSION):
                if apply:
                    self.q.put(("status", f"已是最新版本 v{VERSION}"))
                return
            if not apply:
                self.q.put(("update_note", f"有新版本 v{rv}：右键 → 从 GitHub 更新"))
                return
            compile(src, "remote", "exec")              # 语法有问题就不替换
            me = os.path.abspath(__file__)
            shutil.copy2(me, me + ".bak")
            tmp = me + ".new"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            os.replace(tmp, me)
            log(f"已更新到 v{rv}")
            self.q.put(("restart", rv))
        except Exception as e:
            log(f"检查更新失败: {e}")
            if apply:
                self.q.put(("status", f"更新失败：{str(e)[:50]}"))

    def restart(self):
        self._save_pos()
        try:
            LOCK.close()
        except Exception:
            pass
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--wait"], creationflags=NO_WINDOW)
        self.root.destroy()

    # ---- 调度 ----
    def bad_models(self):
        return [m["id"] for m in self.shown() if self.probe.get(m["id"], [""])[0] in ("fail", "warn")]

    def tick(self):
        now = time.time()
        if not self.busy.locked():
            if PROBE_INTERVAL_MIN > 0 and now >= self.next_probe:
                self.start_job("all")
            elif RETRY_BAD_MIN > 0 and now >= self.next_retry and self.bad_models():
                self.start_job("bad")
            elif now >= self.next_catalog:
                self.start_job("catalog")
        self.root.after(3000, self.tick)

    def start_job(self, kind):
        if self.busy.locked():
            self.status = "正在检测中，请稍候…"
            self.render()
            return
        threading.Thread(target=self.job, args=(kind,), daemon=True).start()

    def job(self, kind):
        with self.busy:
            self.q.put(("working", True))
            try:
                if kind == "bad":
                    targets = self.bad_models()
                    self.next_retry = time.time() + RETRY_BAD_MIN * 60
                else:
                    self.q.put(("status", "正在读取模型目录…"))
                    cat = check_catalog()
                    self.q.put(("catalog", cat))
                    self.next_catalog = time.time() + CATALOG_INTERVAL_MIN * 60
                    targets = []
                    if kind == "all":
                        if cat["ok"]:
                            targets = [m["id"] for m in cat["models"] if self.show_hidden or not m["hidden"]]
                            self.next_probe = time.time() + max(PROBE_INTERVAL_MIN, 1) * 60
                            self.next_retry = time.time() + RETRY_BAD_MIN * 60
                        else:
                            self.next_probe = time.time() + 5 * 60
                if targets:
                    done = 0
                    self.q.put(("status", f"实测中 0/{len(targets)}…"))
                    with ThreadPoolExecutor(max_workers=max(1, PROBE_WORKERS)) as ex:
                        futs = {ex.submit(probe_model, mid): mid for mid in targets}
                        for f in as_completed(futs):
                            done += 1
                            try:
                                level, text = f.result()
                            except Exception as e:
                                level, text = "warn", str(e)[:40]
                            self.q.put(("probe", (futs[f], level, text)))
                            self.q.put(("status", f"实测中 {done}/{len(targets)}…"))
                    self.q.put(("status", f"实测完成 {time.strftime('%H:%M')}"))
            except Exception:
                log(traceback.format_exc())
                self.q.put(("status", "检测出错（已记录日志），稍后自动重试"))
            finally:
                self.q.put(("working", False))

    def drain(self):
        changed = False
        try:
            while True:
                kind, data = self.q.get_nowait()
                changed = True
                if kind == "status":
                    self.status = data
                elif kind == "working":
                    self.working = data
                elif kind == "catalog":
                    self.on_catalog(data)
                elif kind == "probe":
                    mid, level, text = data
                    old = self.probe.get(mid, [None])[0]
                    self.probe[mid] = [level, text, time.strftime("%H:%M")]
                    self.state["probe"] = self.probe
                    save_state(self.state)
                    if old == "ok" and level == "fail":
                        self.changes = f"{mid} 变为不可用 ({time.strftime('%H:%M')})"
                        self.alert()
                    elif old in ("fail", "warn") and level == "ok":
                        self.changes = f"{mid} 恢复可用 ({time.strftime('%H:%M')})"
                elif kind == "hotkey":
                    self.toggle_hidden_window()
                elif kind == "update_note":
                    self.update_note = data
                elif kind == "restart":
                    self.restart()
                    return
        except queue.Empty:
            pass
        if changed:
            self.render()
        self.root.after(300, self.drain)

    def on_catalog(self, cat):
        if not cat["ok"]:
            self.status = f"⚠ {cat['error']}（显示上次结果）"
            return
        old = {m["id"] for m in self.models if not m.get("hidden")}
        new = {m["id"] for m in cat["models"] if not m.get("hidden")}
        added, removed = sorted(new - old), sorted(old - new)
        if self.models and (added or removed):
            parts = []
            if added:
                parts.append("新增: " + ", ".join(added))
            if removed:
                parts.append("消失: " + ", ".join(removed))
            self.changes = "  ".join(parts) + time.strftime("  (%H:%M)")
            log("目录变化 " + self.changes)
            self.alert()
        self.models = cat["models"]
        self.updated = time.strftime("%H:%M")
        self.status = f"来源：{cat['source']}" + (f"｜{cat['error']}" if cat["error"] else "")
        self.state.update(models=self.models, updated=self.updated)
        save_state(self.state)

    def alert(self):
        try:
            if IS_WIN:
                import winsound
                winsound.MessageBeep()
        except Exception:
            pass

    # ---- 绘制 ----
    def shown(self):
        return [m for m in self.models if self.show_hidden or not m.get("hidden")]

    def summary(self):
        shown = self.shown()
        levels = [self.probe.get(m["id"], [None])[0] for m in shown]
        n_ok = levels.count("ok")
        n_bad = sum(1 for l in levels if l in ("fail", "warn"))
        if self.working:
            mood = "checking"
        elif not shown:
            mood = "sleepy"
        elif n_bad:
            mood = "sad"
        elif n_ok:
            mood = "happy"
        else:
            mood = "normal"
        if n_ok or n_bad:
            badge, color = n_ok, (C["fail"] if levels.count("fail") else C["warn"] if n_bad else C["ok"])
        else:
            badge, color = len(shown), C["catalog"]
        return shown, n_ok, mood, badge, color

    def draw_cats(self):
        _, _, mood, badge, color = self.summary()
        if self.mode == "cat":
            draw_cat(self.big_cat, 72, mood, self.blink, badge, color)
        else:
            draw_cat(self.mini_cat, 26, mood, self.blink)

    def render(self):
        shown, n_ok, mood, badge, color = self.summary()
        self.draw_cats()
        if self.mode == "cat":
            return
        for w in self.body.winfo_children():
            w.destroy()
        self.title_lbl.config(text=f"Codex 模型  {len(shown)}个" + (f" · 可用{n_ok}" if n_ok else ""))
        self.time_lbl.config(text=f"更新 {self.updated}" if self.updated else "")
        if not shown:
            tk.Label(self.body, text="暂无数据", bg=C["bg"], fg=C["dim"],
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        for m in shown:
            pr = self.probe.get(m["id"])
            if pr is None:
                dot, note = C["catalog"], "在目录中"
            else:
                dot = {"ok": C["ok"], "fail": C["fail"]}.get(pr[0], C["warn"])
                note = ("可用 " if pr[0] == "ok" else "") + f"{pr[1]} @{pr[2]}"
            row = tk.Frame(self.body, bg=C["bg"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text="●", fg=dot, bg=C["bg"], font=("Segoe UI", 10)).pack(side="left")
            tk.Label(row, text=m["id"], fg=C["dim"] if m.get("hidden") else C["fg"], bg=C["bg"],
                     font=("Consolas", 10)).pack(side="left", padx=(4, 10))
            tk.Label(row, text=note, fg=C["dim"], bg=C["bg"], font=("Microsoft YaHei UI", 8)).pack(side="right")
            for w in row.winfo_children() + [row]:
                w.bind("<Button-3>", self.menu)
        lines = [x for x in (self.changes, self.status, self.update_note) if x]
        self.foot.config(text="\n".join(lines), fg=C["new"] if (self.changes or self.update_note) else C["dim"])

    def run(self):
        self.root.mainloop()


def single_instance():
    """占用一个本机端口，已在运行就直接退出，避免开出两个窗口。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47321))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


LOCK = None

if __name__ == "__main__":
    for _ in range(20):                 # 自更新重启时等旧进程释放端口
        LOCK = single_instance()
        if LOCK is not None:
            break
        if "--wait" not in sys.argv:
            sys.exit(0)
        time.sleep(0.5)
    try:
        App().run()
    except Exception:
        log(traceback.format_exc())
        raise
