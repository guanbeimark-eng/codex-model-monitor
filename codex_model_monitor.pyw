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

VERSION = "1.3.0"

# ======================= 可改的设置 =======================
CATALOG_INTERVAL_MIN = 2       # 目录检测间隔（分钟），免费
PROBE_INTERVAL_MIN = 15        # 全部实测间隔（分钟），0 = 只手动实测
RETRY_BAD_MIN = 5              # 失败/限流的模型多久重测一次（分钟），0 = 不单独重测
PROBE_WORKERS = 3              # 同时实测几个模型
PROBE_ON_START = True          # 启动时是否立刻实测一次
PROBE_TIMEOUT_SEC = 120        # 单个模型实测超时
CATALOG_TIMEOUT_SEC = 60       # 目录检测超时
CAT_SIZE = 44                  # 小猫模式的大小（像素）
IDLE_ALPHA = 0.35              # 鼠标不在窗口上时的透明度（隐身程度，越小越透明）
ACTIVE_ALPHA = 0.96            # 鼠标悬停时的透明度
CODEX_PATH = ""                # 留空自动查找；也可在右键菜单里手动选择
SHOW_HIDDEN = False            # 是否默认显示目录里标记为隐藏的模型
# ---- v1.3 准确度相关 ----
PROBE_EFFORT = "auto"          # 实测用的推理强度：auto = 跟 config.toml 一致；也可填 low/medium/high/xhigh；"" = 不指定
SLOW_PROBE_SEC = 60            # 实测超过这么多秒才回复，标为黄色"响应慢"（通常是在排队/重试）
PASSIVE_WATCH = True           # 读取 Codex 会话日志，捕捉你实际使用时的"满载"报错（不耗额度）
PASSIVE_SCAN_SEC = 20          # 多久扫一次会话日志（秒）
PASSIVE_HOLD_MIN = 10          # 实际使用报满载后，该模型保持黄色多少分钟（期间实测通过也不变绿）
STALE_MIN = 35                 # 实测结果超过多少分钟没更新，灯变灰（结果已过期，仅供参考）
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

def error_message(text):
    """从 codex --json 输出里取出真正的错误信息（turn.failed / error 事件）。"""
    msgs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if "fail" in str(ev.get("type", "")) or "error" in str(ev.get("type", "")):
            e = ev.get("error") if isinstance(ev.get("error"), dict) else ev
            m = e.get("message") or ev.get("message")
            if isinstance(m, str):
                inner = parse_json_loose(m)          # message 里有时又包了一层 JSON
                if isinstance(inner, dict):
                    m = (inner.get("error") or {}).get("message") or inner.get("detail") or m
                msgs.append(str(m))
    return msgs[-1] if msgs else ""


def short_reason(text):
    msg = error_message(text)
    t = (msg + "\n" + (text or "")).lower()
    table = [
        (("找不到 codex",), "找不到 codex（右键 → 选择 codex 位置）"),
        (("not logged in", "login", "unauthorized", "401"), "未登录/登录失效"),
        (("usage limit", "rate limit", "429", "quota"), "额度/限流"),
        (("capacity", "overloaded", "503", "try again later"), "模型繁忙"),
        (("not supported", "does not exist", "not found", "unknown model", "not available",
          "model_not_found", "invalid model", "404", "selected model"), "账号不可用该模型"),
        (("timeout",), "超时"),
        (("network", "connect", "dns", "ssl", "tls"), "网络问题"),
    ]
    for keys, msg in table:
        if any(k in t for k in keys):
            return msg
    line = msg or next((l.strip() for l in (text or "").splitlines()[::-1] if l.strip()), "")
    return line[:40]


EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
BUSY_KEYS = ("at capacity", "server_overloaded", "overloaded", "reconnecting", "retrying")


def configured_effort():
    """实测用的推理强度。auto = 读 config.toml 顶层的 model_reasoning_effort，和你平时用的一致。
    （满载时高推理强度往往先被拒，用默认强度实测会"假绿"。）"""
    e = (PROBE_EFFORT or "").strip().lower()
    if e and e != "auto":
        return e if e in EFFORTS else ""
    if not e:
        return ""
    try:
        with open(os.path.join(CODEX_HOME, "config.toml"), "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return ""
    top = re.split(r"^\s*\[", text, maxsplit=1, flags=re.M)[0]      # 只看顶层，不看 [profiles.x]
    m = re.search(r'^\s*model_reasoning_effort\s*=\s*["\']?(\w+)', top, re.M)
    v = m.group(1).lower() if m else ""
    return v if v in EFFORTS else ""


def _probe_once(model_id, effort):
    tmp = tempfile.mkdtemp(prefix="codex_probe_")
    args = ["exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "--json", "-C", tmp, "-m", model_id]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    args.append("Reply with exactly: OK")
    t0 = time.time()
    rc, out, err = run_codex(args, PROBE_TIMEOUT_SEC, cwd=tmp)
    secs = time.time() - t0
    shutil.rmtree(tmp, ignore_errors=True)
    return rc, out, err, secs


def classify_probe(rc, out, err, secs):
    """根据 codex exec 的结果判断 ('ok'|'fail'|'warn', 说明)。单独拿出来方便测试。"""
    failed = '"turn.failed"' in out
    if rc == 0 and "agent_message" in out and not failed:
        # 最终成功，但中途报过满载/重连 = codex 自动重试掩盖了繁忙，你实际用的时候很可能会撞上
        if any(k in (out + "\n" + err).lower() for k in BUSY_KEYS):
            return "warn", f"繁忙（重试后才通过，{secs:.0f}s）"
        if SLOW_PROBE_SEC and secs > SLOW_PROBE_SEC:
            return "warn", f"响应慢 {secs:.0f}s（可能在排队）"
        return "ok", f"{secs:.0f}s"
    reason = short_reason(err + "\n" + out) if rc != -9 else "超时"
    level = "warn" if reason in ("额度/限流", "超时", "网络问题", "模型繁忙") else "fail"
    return level, reason or f"退出码 {rc}"


def probe_model(model_id):
    """返回 ('ok'|'fail'|'warn', 说明)。用和平时一样的推理强度实测。"""
    effort = configured_effort()
    rc, out, err, secs = _probe_once(model_id, effort)
    level, text = classify_probe(rc, out, err, secs)
    if level == "fail" and effort and re.search(r"reasoning|effort|unsupported value", (out + err).lower()):
        log(f"probe {model_id}: 推理强度 {effort} 不被支持，改用默认强度重测")
        effort = ""
        rc, out, err, secs = _probe_once(model_id, effort)
        level, text = classify_probe(rc, out, err, secs)
    if level != "ok":
        log(f"probe {model_id} effort={effort or 'default'} rc={rc}: {(err + out)[-800:]}")
    if effort and level == "ok":
        text += f" @{effort}"
    return level, text


# ----------------------- 被动检测：读 Codex 会话日志 -----------------------
# Codex（CLI / 桌面版 / VS Code 插件）把每个会话写到 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl。
# turn_context 里有实际使用的模型；满载时会出现 "Selected model is at capacity" / server_overloaded。
# 你真实使用时撞到的报错比任何实测都准，所以用它来纠正实测结果。

def _parse_ts(s):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _all_text(obj, depth=0):
    if depth > 8:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_all_text(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_all_text(v, depth + 1) for v in obj)
    return ""


class SessionWatcher:
    """增量读取最近的 rollout 文件，产出事件 (model, 'capacity'|'ok', 时间戳)。"""
    FIRST_READ_MAX = 4 * 1024 * 1024        # 第一次打开大文件只读最后 4MB

    def __init__(self, root=None):
        self.root = root or os.path.join(CODEX_HOME, "sessions")
        self.files = {}                      # path -> {"pos", "model", "turn_err"}

    def recent_files(self, max_age_sec):
        now = time.time()
        out = []
        for days_ago in (0, 1):              # 今天 + 昨天的目录（跨午夜）
            d = time.localtime(now - days_ago * 86400)
            folder = os.path.join(self.root, f"{d.tm_year:04d}", f"{d.tm_mon:02d}", f"{d.tm_mday:02d}")
            try:
                names = os.listdir(folder)
            except Exception:
                continue
            for n in names:
                if n.startswith("rollout-") and n.endswith(".jsonl"):
                    p = os.path.join(folder, n)
                    try:
                        if now - os.path.getmtime(p) <= max_age_sec:
                            out.append(p)
                    except Exception:
                        pass
        return out

    def scan(self, max_age_sec):
        events = []
        for p in self.recent_files(max_age_sec):
            st = self.files.setdefault(p, {"pos": None, "model": None, "turn_err": False})
            try:
                size = os.path.getsize(p)
                if st["pos"] is None:
                    st["pos"] = max(0, size - self.FIRST_READ_MAX)
                if size < st["pos"]:         # 文件被重写
                    st["pos"] = 0
                if size == st["pos"]:
                    continue
                with open(p, "rb") as f:
                    f.seek(st["pos"])
                    chunk = f.read()
                end = chunk.rfind(b"\n")     # 只处理完整的行，半行留到下次
                if end < 0:
                    continue
                st["pos"] += end + 1
                for raw in chunk[:end].splitlines():
                    events.extend(self._line(st, raw))
            except Exception as e:
                log(f"读取会话日志失败 {p}: {e}")
        return events

    @staticmethod
    def _line(st, raw):
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return []
        if not isinstance(rec, dict):
            return []
        typ = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ptype = str(payload.get("type", ""))
        ts = _parse_ts(rec.get("timestamp")) or time.time()
        if typ == "turn_context":
            if isinstance(payload.get("model"), str):
                st["model"] = payload["model"]
            st["turn_err"] = False
            return []
        if ptype in ("task_started", "turn_started"):
            st["turn_err"] = False
            return []
        # 只看错误类记录；聊天内容里提到 "at capacity" 不算
        is_err = "error" in ptype or "fail" in ptype or "codex_error_info" in payload or typ == "error"
        text = _all_text(payload).lower() if is_err else ""
        if ("at capacity" in text or "server_overloaded" in text) and st["model"]:
            st["turn_err"] = True
            return [(st["model"], "capacity", ts)]
        if ptype in ("task_complete", "turn_complete") and st["model"] and not st["turn_err"]:
            return [(st["model"], "ok", ts)]
        return []


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
    "new": "#ffd84d", "key": "#010203", "stale": "#6b6f78",
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
        self.probe = self.state.get("probe", {})              # id -> [level, text, "HH:MM", epoch]
        self.passive = self.state.get("passive", {})          # id -> [level, text, "HH:MM", epoch]（真实使用）
        self.watcher = SessionWatcher() if PASSIVE_WATCH else None
        self.scanning = threading.Lock()
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

        # 面板（紧凑：标题行 + 每个模型一行"灯 + 名字"）
        self.panel = tk.Frame(r, bg=C["bg"])
        self.head = tk.Frame(self.panel, bg=C["head"])
        self.head.pack(fill="x")
        self.mini_cat = tk.Canvas(self.head, width=18, height=18, bg=C["head"], highlightthickness=0)
        self.mini_cat.pack(side="left", padx=(4, 0), pady=1)
        self.title_lbl = tk.Label(self.head, text="", bg=C["head"], fg=C["fg"],
                                  font=("Segoe UI", 8, "bold"), padx=4)
        self.title_lbl.pack(side="left")
        self.btns = tk.Frame(self.head, bg=C["head"])
        self.btn_widgets = []
        for icon, tip, cmd in (("min", "缩成小猫", lambda: self.set_mode("cat")),
                               ("eye", "隐身（Ctrl+Alt+M 或双击桌面图标唤回）", self.go_invisible),
                               ("close", "关闭", self.quit)):
            b = tk.Canvas(self.btns, width=16, height=16, bg=C["head"], highlightthickness=0, cursor="hand2")
            b.pack(side="left", padx=1)
            self.draw_icon(b, icon, C["dim"])
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b, i=icon: self.draw_icon(w, i, C["fg"]))
            b.bind("<Leave>", lambda e, w=b, i=icon: self.draw_icon(w, i, C["dim"]))
            self.tooltip(b, lambda t=tip: t)
            self.btn_widgets.append(b)
        self.btns_shown = False
        self.body = tk.Frame(self.panel, bg=C["bg"], padx=5, pady=2)
        self.body.pack(fill="both")
        self.foot = tk.Label(self.panel, text="", bg=C["bg"], fg=C["new"], anchor="w", justify="left",
                             font=("Microsoft YaHei UI", 7), padx=5, pady=1, wraplength=150)
        # 小猫
        self.big_cat = tk.Canvas(r, width=CAT_SIZE, height=CAT_SIZE, bg=C["key"], highlightthickness=0)
        self.tip_win = None

        for w in (self.head, self.mini_cat, self.title_lbl, self.big_cat):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
        for w in (self.head, self.mini_cat, self.title_lbl):
            w.bind("<ButtonRelease-1>", lambda e: self._save_pos())
            w.bind("<Double-Button-1>", lambda e: self.set_mode("cat"))
        self.big_cat.bind("<ButtonRelease-1>", self._cat_release)
        for w in (r, self.head, self.mini_cat, self.title_lbl, self.body, self.foot, self.big_cat):
            w.bind("<Button-3>", self.menu)

        self.set_mode(self.mode, save=False)
        threading.Thread(target=hotkey_thread, args=(self.q,), daemon=True).start()
        if LOCK is not None:
            threading.Thread(target=wake_listener, args=(LOCK, self.q), daemon=True).start()
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
            if inside != self.btns_shown and self.mode == "panel":
                self.btns_shown = inside
                if inside:
                    self.btns.pack(side="right")
                else:
                    self.btns.pack_forget()
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

    def toggle_hidden_window(self, show=None):
        self.hidden = (not self.hidden) if show is None else (not show)
        self.hide_tip()
        if self.hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()

    def go_invisible(self):
        """隐身：窗口消失；Ctrl+Alt+M 或双击桌面 Codex Models 图标唤回。"""
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        self.toggle_hidden_window(show=False)
        if not self.state.get("hint_shown"):
            self.state["hint_shown"] = True
            save_state(self.state)
            t = tk.Toplevel(self.root)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            t.geometry(f"+{x}+{y}")
            tk.Label(t, text="已隐身：按 Ctrl+Alt+M\n或双击桌面 Codex Models 唤回", bg=C["head"], fg=C["fg"],
                     font=("Microsoft YaHei UI", 8), padx=8, pady=5, justify="left").pack()
            t.after(3500, t.destroy)

    @staticmethod
    def draw_icon(cv, icon, color):
        cv.delete("all")
        if icon == "min":
            cv.create_line(4, 11, 12, 11, fill=color, width=2)
        elif icon == "eye":                         # 带斜杠的眼睛 = 隐身
            cv.create_oval(2, 5, 14, 12, outline=color, width=1.5)
            cv.create_oval(6, 6.5, 10, 10.5, fill=color, outline="")
            cv.create_line(3, 14, 13, 3, fill=color, width=1.5)
        else:
            cv.create_line(4, 4, 12, 12, fill=color, width=2)
            cv.create_line(12, 4, 4, 12, fill=color, width=2)

    def tooltip(self, widget, text_fn):
        widget.bind("<Enter>", lambda e: self.show_tip(e, text_fn()), add="+")
        widget.bind("<Leave>", lambda e: self.hide_tip(), add="+")

    def show_tip(self, e, text):
        self.hide_tip()
        if not text:
            return
        t = self.tip_win = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.geometry(f"+{e.x_root + 12}+{e.y_root + 10}")
        tk.Label(t, text=text, bg="#111214", fg=C["fg"], font=("Microsoft YaHei UI", 8),
                 padx=6, pady=3, justify="left").pack()

    def hide_tip(self):
        if self.tip_win is not None:
            try:
                self.tip_win.destroy()
            except Exception:
                pass
            self.tip_win = None

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新目录", command=lambda: self.start_job("catalog"))
        m.add_command(label="实测全部模型（耗少量额度）", command=lambda: self.start_job("all"))
        m.add_separator()
        m.add_command(label="展开面板" if self.mode == "cat" else "缩成小猫",
                      command=lambda: self.set_mode("panel" if self.mode == "cat" else "cat"))
        m.add_command(label="隐身（Ctrl+Alt+M 唤回）", command=self.go_invisible)
        m.add_checkbutton(label="显示隐藏模型", command=self.toggle_show_hidden,
                          variable=tk.BooleanVar(value=self.show_hidden))
        m.add_separator()
        m.add_command(label="选择 codex 位置…", command=self.pick_codex)
        m.add_command(label=f"从 GitHub 更新（当前 v{VERSION}）",
                      command=lambda: threading.Thread(target=self.check_update, args=(True,), daemon=True).start())
        m.add_command(label="打开日志", command=self.open_log)
        m.add_separator()
        m.add_command(label="关闭", command=self.quit)
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

    # ---- 合并：实测 + 真实使用 ----
    def effective(self, mid, now=None):
        """返回 [level, 说明] 或 None。level: ok / warn / fail / stale。
        规则：PASSIVE_HOLD_MIN 内真实使用报过满载 → 黄色，就算之后短实测通过也不变绿
              （短请求能过、长任务被拒是常见情况）；真实使用正常且比实测新 → 绿；
              实测结果太旧 → 灰。"""
        now = now or time.time()
        hold = PASSIVE_HOLD_MIN * 60
        p, a = self.probe.get(mid), self.passive.get(mid)
        p_ts = p[3] if p and len(p) > 3 else 0
        a_fresh = a and len(a) > 3 and now - a[3] <= hold
        if a_fresh and a[0] == "warn":
            extra = f"；之后实测通过（{p[2]}）" if p and p[0] == "ok" and p_ts > a[3] else ""
            return ["warn", f"实际使用报满载（{a[2]}）{extra}"]
        if a_fresh and a[0] == "ok" and (not p or a[3] >= p_ts):
            return ["ok", f"实际使用正常（{a[2]}）"]
        if not p:
            return None
        if p_ts and STALE_MIN and now - p_ts > STALE_MIN * 60:
            return ["stale", f"上次实测 {p[2]}：{p[1]}（已过期 {int((now - p_ts) / 60)} 分钟）"]
        prefix = "可用，响应 " if p[0] == "ok" else ""
        return [p[0], f"{prefix}{p[1]}（{p[2]} 测）"]

    def passive_scan(self):
        with self.scanning:
            try:
                ev = self.watcher.scan(max(PASSIVE_HOLD_MIN * 60, PASSIVE_SCAN_SEC * 3))
                if ev:
                    self.q.put(("passive", ev))
            except Exception:
                log(traceback.format_exc())

    def on_passive(self, events):
        now = time.time()
        for mid, kind, ts in sorted(events, key=lambda e: e[2]):
            if now - ts > PASSIVE_HOLD_MIN * 60:
                continue                    # 太旧的报错不算
            old = self.passive.get(mid)
            if old and len(old) > 3 and old[3] > ts:
                continue
            before = (self.effective(mid) or [None])[0]
            hm = time.strftime("%H:%M", time.localtime(ts))
            if kind == "capacity":
                self.passive[mid] = ["warn", "满载", hm, ts]
                if before in ("ok", "stale", None):
                    self.set_change(f"{mid} 实际使用报满载 ({hm})")
                    self.alert()
                log(f"会话日志：{mid} 报满载 {hm}")
            else:
                self.passive[mid] = ["ok", "正常", hm, ts]
        self.state["passive"] = self.passive
        save_state(self.state)

    # ---- 调度 ----
    def bad_models(self):
        return [m["id"] for m in self.shown() if (self.effective(m["id"]) or [""])[0] in ("fail", "warn")]

    def tick(self):
        now = time.time()
        if self.watcher and now >= getattr(self, "next_passive", 0) and not self.scanning.locked():
            self.next_passive = now + PASSIVE_SCAN_SEC
            threading.Thread(target=self.passive_scan, daemon=True).start()
        if int(now // 60) != getattr(self, "_last_min", None) and self.tip_win is None:
            self._last_min = int(now // 60)
            self.render()                   # 每分钟重画一次，让过期的黄灯/灰灯按时变化
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
                    self.probe[mid] = [level, text, time.strftime("%H:%M"), time.time()]
                    self.state["probe"] = self.probe
                    save_state(self.state)
                    if old == "ok" and level == "fail":
                        self.set_change(f"{mid} 变为不可用 ({time.strftime('%H:%M')})")
                        self.alert()
                    elif old in ("fail", "warn") and level == "ok":
                        self.set_change(f"{mid} 恢复可用 ({time.strftime('%H:%M')})")
                elif kind == "passive":
                    self.on_passive(data)
                elif kind == "hotkey":
                    self.toggle_hidden_window()
                elif kind == "wake":
                    self.toggle_hidden_window(show=True)
                elif kind == "clear_changes":
                    self.changes = ""
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
            self.set_change("  ".join(parts) + time.strftime("  (%H:%M)"))
            log("目录变化 " + self.changes)
            self.alert()
        self.models = cat["models"]
        self.updated = time.strftime("%H:%M")
        self.status = f"来源：{cat['source']}" + (f"｜{cat['error']}" if cat["error"] else "")
        self.state.update(models=self.models, updated=self.updated)
        save_state(self.state)

    def set_change(self, text):
        self.changes = text
        self.root.after(10 * 60 * 1000, lambda: self.q.put(("clear_changes", None)) if self.changes == text else None)

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
        levels = [(self.effective(m["id"]) or [None])[0] for m in shown]
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
            draw_cat(self.big_cat, CAT_SIZE, mood, self.blink, badge, color)
        else:
            draw_cat(self.mini_cat, 18, mood, self.blink)

    def render(self):
        shown, n_ok, mood, badge, color = self.summary()
        self.draw_cats()
        if self.mode == "cat":
            return
        self.hide_tip()
        for w in self.body.winfo_children():
            w.destroy()
        self.title_lbl.config(text=f"{n_ok}/{len(shown)}"
                              if any(m["id"] in self.probe or m["id"] in self.passive for m in shown)
                              else f"{len(shown)}")
        if not shown:
            tk.Label(self.body, text="暂无数据", bg=C["bg"], fg=C["dim"],
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        for m in shown:
            ef = self.effective(m["id"])
            if ef is None:
                dot, note = C["catalog"], "在目录中，尚未实测"
            else:
                dot = {"ok": C["ok"], "fail": C["fail"], "stale": C["stale"]}.get(ef[0], C["warn"])
                note = ef[1]
            row = tk.Frame(self.body, bg=C["bg"])
            row.pack(fill="x")
            lamp = tk.Canvas(row, width=10, height=10, bg=C["bg"], highlightthickness=0)
            lamp.create_oval(1, 1, 9, 9, fill=dot, outline="")
            lamp.pack(side="left", padx=(1, 0))
            tk.Label(row, text=m["id"], fg=C["dim"] if m.get("hidden") else C["fg"], bg=C["bg"],
                     font=("Consolas", 9)).pack(side="left", padx=(2, 4))
            for w in row.winfo_children() + [row]:
                w.bind("<Button-3>", self.menu)
                self.tooltip(w, lambda n=note: n)
        important = ("⚠", "找不到", "未登录", "出错", "失败", "已是最新", "已设置")
        msgs = [self.changes, self.update_note] + ([self.status] if any(k in self.status for k in important) else [])
        msgs = [x for x in msgs if x]
        if msgs:
            self.foot.config(text="\n".join(msgs))
            self.foot.pack(fill="x")
        else:
            self.foot.pack_forget()

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


def wake_listener(sock, q):
    """另一个实例启动（比如双击桌面图标）时，把已隐身的窗口唤回来。"""
    while True:
        try:
            c, _ = sock.accept()
            c.close()
            q.put(("wake", None))
        except Exception:
            return


def wake_existing():
    import socket
    try:
        socket.create_connection(("127.0.0.1", 47321), timeout=2).close()
    except Exception:
        pass


LOCK = None

if __name__ == "__main__":
    for _ in range(20):                 # 自更新重启时等旧进程释放端口
        LOCK = single_instance()
        if LOCK is not None:
            break
        if "--wait" not in sys.argv:
            wake_existing()
            sys.exit(0)
        time.sleep(0.5)
    try:
        App().run()
    except Exception:
        log(traceback.format_exc())
        raise
