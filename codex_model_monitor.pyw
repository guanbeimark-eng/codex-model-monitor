# -*- coding: utf-8 -*-
"""
Codex 模型可用性监测 —— 桌面悬浮小窗 (Windows)
只用 Python 标准库，无需 pip 安装任何东西。

两层检测：
  1) 目录检测（免费、每 N 分钟）：运行 `codex debug models`，读取你账号当前能看到的模型目录。
     失败时退回读取 %USERPROFILE%\\.codex\\models_cache.json。
  2) 实测（可选、会消耗极少量额度）：对每个模型跑一次 `codex exec` 让它回复 OK，
     能回复 = 真能用。默认每 60 分钟一次，也可右键手动触发。

操作：拖动标题栏移动；右键打开菜单（立即刷新 / 实测全部 / 显示隐藏模型 / 退出）；双击标题栏折叠。
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback

# ======================= 可改的设置 =======================
CATALOG_INTERVAL_MIN = 10      # 目录检测间隔（分钟），免费
PROBE_INTERVAL_MIN = 60        # 实测间隔（分钟），0 = 只手动实测
PROBE_ON_START = False         # 启动时是否立刻实测一次
PROBE_TIMEOUT_SEC = 120        # 单个模型实测超时
CATALOG_TIMEOUT_SEC = 60       # 目录检测超时
CODEX_PATH = ""                # 留空自动查找；找不到时填完整路径，如 r"C:\Users\你\AppData\Roaming\npm\codex.cmd"
SHOW_HIDDEN = False            # 是否默认显示目录里标记为隐藏的模型
# =========================================================

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "codex_model_monitor")
STATE_FILE = os.path.join(APP_DIR, "state.json")
LOG_FILE = os.path.join(APP_DIR, "monitor.log")
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.join(os.path.expanduser("~"), ".codex"))
IS_WIN = sys.platform.startswith("win")

os.makedirs(APP_DIR, exist_ok=True)


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 512 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


# ----------------------- 运行 codex -----------------------

def find_codex():
    if CODEX_PATH and os.path.exists(CODEX_PATH):
        return CODEX_PATH
    for name in ("codex", "codex.cmd", "codex.exe"):
        p = shutil.which(name)
        if p:
            return p
    # npm 全局目录兜底
    guess = os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd")
    return guess if os.path.exists(guess) else None


def run_codex(args, timeout, cwd=None):
    """返回 (returncode, stdout, stderr)；超时返回 (-9, '', 'timeout')。永不抛异常。"""
    exe = find_codex()
    if not exe:
        return -1, "", "找不到 codex 命令（请在脚本顶部填写 CODEX_PATH）"
    if exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd.exe", "/d", "/c", exe] + args
    else:
        argv = [exe] + args
    flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW：不弹黑框
    try:
        p = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=flags,
        )
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
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=0x08000000)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


# ----------------------- 解析模型目录 -----------------------

def parse_json_loose(text):
    """从可能夹杂日志的输出里取出第一段 JSON。"""
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
                obj, _ = dec.raw_decode(text[i:])
                return obj
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
    """不依赖固定格式：找出最像"模型列表"的数组，返回 [{id, label, hidden}]。"""
    lists = list(_candidate_lists(obj))
    if not lists:
        return []
    best = max(lists, key=len)
    models, seen = [], set()
    for d in best:
        mid = next((d[k] for k in ID_KEYS if isinstance(d.get(k), str) and d[k].strip()), None)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        label = d.get("display_name") or d.get("displayName") or mid
        vis = str(d.get("visibility", "")).lower()
        hidden = (vis in ("hide", "hidden", "none")
                  or d.get("hidden") is True
                  or d.get("show_in_picker") is False
                  or d.get("deprecated") is True)
        models.append({"id": mid, "label": str(label), "hidden": bool(hidden)})
    return models


def check_catalog():
    """返回 dict(ok, models, source, error)。"""
    rc, out, err = run_codex(["debug", "models"], CATALOG_TIMEOUT_SEC)
    if rc == 0:
        models = extract_models(parse_json_loose(out))
        if models:
            return {"ok": True, "models": models, "source": "实时目录", "error": ""}
        log(f"debug models 输出无法解析: {out[:500]!r}")
    else:
        log(f"debug models 失败 rc={rc}: {(err or out)[:500]}")
    # 退回缓存文件
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


# ----------------------- 界面 -----------------------

C = {
    "bg": "#1e1f22", "head": "#2b2d31", "fg": "#e6e6e6", "dim": "#8a8f98",
    "ok": "#3ddc84", "catalog": "#5aa9ff", "fail": "#ff5c5c", "warn": "#f5b942",
    "new": "#ffd84d",
}


class App:
    def __init__(self):
        self.state = load_state()
        self.models = self.state.get("models", [])            # 上次目录
        self.probe = self.state.get("probe", {})              # id -> [level, text, ts]
        self.show_hidden = self.state.get("show_hidden", SHOW_HIDDEN)
        self.status = "启动中…"
        self.changes = ""
        self.updated = self.state.get("updated", "")
        self.busy = threading.Lock()
        self.q = queue.Queue()
        self.collapsed = False
        self.next_catalog = 0
        self.next_probe = time.time() + (0 if PROBE_ON_START else PROBE_INTERVAL_MIN * 60)

        r = self.root = tk.Tk()
        r.title("Codex 模型")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        try:
            r.attributes("-alpha", 0.94)
        except Exception:
            pass
        r.configure(bg=C["bg"])
        pos = self.state.get("pos", "+40+120")
        r.geometry(pos)

        self.head = tk.Frame(r, bg=C["head"])
        self.head.pack(fill="x")
        self.title_lbl = tk.Label(self.head, text="Codex 模型", bg=C["head"], fg=C["fg"],
                                  font=("Microsoft YaHei UI", 10, "bold"), padx=8, pady=4)
        self.title_lbl.pack(side="left")
        self.time_lbl = tk.Label(self.head, text="", bg=C["head"], fg=C["dim"],
                                 font=("Microsoft YaHei UI", 8), padx=6)
        self.time_lbl.pack(side="right")
        self.body = tk.Frame(r, bg=C["bg"], padx=8, pady=4)
        self.body.pack(fill="both")
        self.foot = tk.Label(r, text="", bg=C["bg"], fg=C["dim"], anchor="w", justify="left",
                             font=("Microsoft YaHei UI", 8), padx=8, pady=3, wraplength=260)
        self.foot.pack(fill="x")

        for w in (self.head, self.title_lbl, self.time_lbl):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save_pos())
            w.bind("<Double-Button-1>", lambda e: self.toggle_collapse())
        for w in (r, self.head, self.title_lbl, self.time_lbl, self.body, self.foot):
            w.bind("<Button-3>", self.menu)

        self.render()
        r.after(300, self.tick)
        r.after(200, self.drain)

    # ---- 拖动 / 菜单 ----
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _save_pos(self):
        self.state["pos"] = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        save_state(self.state)

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新目录", command=lambda: self.start_job(probe=False))
        m.add_command(label="实测全部模型（耗少量额度）", command=lambda: self.start_job(probe=True))
        m.add_checkbutton(label="显示隐藏模型", command=self.toggle_hidden,
                          variable=tk.BooleanVar(value=self.show_hidden))
        m.add_command(label="折叠/展开", command=self.toggle_collapse)
        m.add_command(label="打开日志", command=lambda: os.startfile(LOG_FILE) if IS_WIN and os.path.exists(LOG_FILE) else None)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self.state["show_hidden"] = self.show_hidden
        save_state(self.state)
        self.render()

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        self.render()

    def quit(self):
        self._save_pos()
        self.root.destroy()

    # ---- 调度 ----
    def tick(self):
        now = time.time()
        if not self.busy.locked():
            if PROBE_INTERVAL_MIN > 0 and now >= self.next_probe:
                self.start_job(probe=True)
            elif now >= self.next_catalog:
                self.start_job(probe=False)
        self.root.after(5000, self.tick)

    def start_job(self, probe):
        if self.busy.locked():
            self.status = "正在检测中，请稍候…"
            self.render()
            return
        threading.Thread(target=self.job, args=(probe,), daemon=True).start()

    def job(self, probe):
        with self.busy:
            try:
                self.q.put(("status", "正在读取模型目录…"))
                cat = check_catalog()
                self.q.put(("catalog", cat))
                self.next_catalog = time.time() + CATALOG_INTERVAL_MIN * 60
                if probe and cat["ok"]:
                    targets = [m for m in cat["models"] if self.show_hidden or not m["hidden"]]
                    for i, m in enumerate(targets, 1):
                        self.q.put(("status", f"实测 {i}/{len(targets)}：{m['id']}"))
                        level, text = probe_model(m["id"])
                        self.q.put(("probe", (m["id"], level, text)))
                    self.next_probe = time.time() + max(PROBE_INTERVAL_MIN, 1) * 60
                    self.q.put(("status", f"实测完成 {time.strftime('%H:%M')}"))
                elif probe:
                    self.next_probe = time.time() + 5 * 60
            except Exception:
                log(traceback.format_exc())
                self.q.put(("status", "检测出错（已记录日志），下次自动重试"))

    def drain(self):
        changed = False
        try:
            while True:
                kind, data = self.q.get_nowait()
                changed = True
                if kind == "status":
                    self.status = data
                elif kind == "catalog":
                    self.on_catalog(data)
                elif kind == "probe":
                    mid, level, text = data
                    self.probe[mid] = [level, text, time.strftime("%H:%M")]
                    self.state["probe"] = self.probe
                    save_state(self.state)
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
            self.root.deiconify()
            self.root.lift()
            if IS_WIN:
                import winsound
                winsound.MessageBeep()
        except Exception:
            pass

    # ---- 绘制 ----
    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        shown = [m for m in self.models if self.show_hidden or not m.get("hidden")]
        n_ok = sum(1 for m in shown if self.probe.get(m["id"], [""])[0] == "ok")
        self.title_lbl.config(text=f"Codex 模型  {len(shown)}个" + (f" · 实测可用{n_ok}" if n_ok else ""))
        self.time_lbl.config(text=f"更新 {self.updated}" if self.updated else "")
        if self.collapsed:
            self.body.pack_forget()
            self.foot.pack_forget()
            return
        self.body.pack(fill="both")
        self.foot.pack(fill="x")
        if not shown:
            tk.Label(self.body, text="暂无数据", bg=C["bg"], fg=C["dim"],
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        for m in shown:
            pr = self.probe.get(m["id"])
            if pr is None:
                color, note = C["catalog"], "在目录中"
            else:
                color = {"ok": C["ok"], "fail": C["fail"]}.get(pr[0], C["warn"])
                note = ("可用 " if pr[0] == "ok" else "") + f"{pr[1]} @{pr[2]}"
            row = tk.Frame(self.body, bg=C["bg"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text="●", fg=color, bg=C["bg"], font=("Segoe UI", 10)).pack(side="left")
            name_fg = C["dim"] if m.get("hidden") else C["fg"]
            tk.Label(row, text=m["id"], fg=name_fg, bg=C["bg"],
                     font=("Consolas", 10)).pack(side="left", padx=(4, 10))
            tk.Label(row, text=note, fg=C["dim"], bg=C["bg"],
                     font=("Microsoft YaHei UI", 8)).pack(side="right")
            for w in row.winfo_children() + [row]:
                w.bind("<Button-3>", self.menu)
        foot = self.status
        if self.changes:
            foot = self.changes + "\n" + foot
        self.foot.config(text=foot, fg=C["new"] if self.changes else C["dim"])

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
        return None


if __name__ == "__main__":
    _lock = single_instance()
    if _lock is None:
        sys.exit(0)
    try:
        App().run()
    except Exception:
        log(traceback.format_exc())
        raise
