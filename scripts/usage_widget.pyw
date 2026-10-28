#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kimi 用量 + 语音输入 悬浮窗（整合版，2026-10-28）：单进程全搞定。

- 每 60 秒抓一次用量（WebBridge 借 Chrome 登录态），显示百分比 + 更新时间
- 按住 config.json 的 hotkey（默认右 Ctrl）说话 → 本地 whisper 转写 →
  自动激活 Kimi 窗口、粘贴进当前会话并回车发送（kimi_sender，纯本地零 token）
- 录音时大标签切红色 REC 计时；转写/发送/成败都有状态显示，结束自动恢复用量
- 透明背景 + 右侧竖把手；左键拖动、松手吸附屏幕边缘；× 关闭
- --selftest：启动约 2 秒后发送一条自检消息（验证发送链路）
"""
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk

try:  # 必须在创建 Tk 之前设置，Tk 才能拿到真实 DPI
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LATEST = HERE / "latest.json"
LOGF = HERE / "voice.log"

CFG_PATH = HERE / "config.json"
CFG = {"hotkey": "right ctrl", "model": "small", "click_fx": 0.59, "click_fy": 0.90}
if CFG_PATH.exists():
    try:
        CFG.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
try:
    CFG_PATH.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
except Exception:
    pass

SELFTEST = "--selftest" in sys.argv

# 单实例：悬浮窗已在跑就直接退出（双击两次不会开两个）
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "kimi_usage_widget_v1")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)


def log(msg):
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


log(f"=== widget start selftest={SELFTEST}")

# ── 配色（背景色键全透明，只剩文字和右把手）──────────
CHROMA = "#010101"    # 色键：窗口里这个颜色全部穿透
HANDLE = "#3d74c9"    # 右侧竖把手（淡蓝）
ACCENT = "#4da3ff"    # 主蓝
DIM = "#6d87ab"
WARN = "#ffb454"
DANGER = "#ff5f56"
OK = "#4caf7d"

FONT_MONO = "Consolas"
FONT_STATUS = ("Microsoft YaHei UI", 10, "bold")   # 状态文字：雅黑，中文和符号都比 Consolas 回退好看
FONT_USAGE = (FONT_MONO, 11, "bold")

DAEMON_URL = "http://127.0.0.1:10086"
BRIDGE_EXE = Path.home() / ".kimi-webbridge" / "bin" / "kimi-webbridge.exe"
CHROME_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
UI_INTERVAL = 5000
STALE_MIN = 5
SNAP_PX = 20
SR = 16000
SNAP_PX = 20
SR = 16000

S = 1.0
W, H = 124, 32

_boot = {"done": threading.Event(), "lock": threading.Lock(), "last": 0.0}


def start_bootstrap(reason, depth=0, force=False):
    """带 45s 节流 + 互斥的引导触发（心跳/连败/重启都可能喊，别叠加）"""
    now = time.time()
    if not force and now - _boot["last"] < 45:
        log(f"bootstrap skip ({reason}): too soon")
        return
    if not _boot["lock"].acquire(blocking=False):
        log(f"bootstrap skip ({reason}): running")
        return
    _boot["last"] = now

    def _wrap():
        try:
            bootstrap(depth)
        finally:
            _boot["lock"].release()

    threading.Thread(target=_wrap, daemon=True).start()
    log(f"bootstrap: {reason}")


def _wb_call(action, args, timeout=45):
    import urllib.request as urlreq
    body = json.dumps({"action": action, "args": args, "session": "kimi-usage"}).encode()
    req = urlreq.Request(DAEMON_URL + "/command", data=body,
                        headers={"Content-Type": "application/json"}, method="POST")
    with urlreq.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _bridge_status(timeout=6):
    """CLI status：守护活没活/扩展连没连。走 CLI 自己的通道，不依赖扩展命令链路
    （扩展消失时 list_tabs 之类全 502，但守护其实活得好好的）"""
    if not BRIDGE_EXE.exists():
        return None
    try:
        r = subprocess.run([str(BRIDGE_EXE), "status"], capture_output=True,
                           timeout=timeout)
        txt = r.stdout.decode("utf-8", "replace")
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            return json.loads(txt[i:j + 1])
    except Exception:
        pass
    return None


def _daemon_up():
    s = _bridge_status(timeout=6)
    return bool(s and s.get("running"))


def _chrome_running():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                           capture_output=True, timeout=10)
        return b"chrome.exe" in r.stdout.lower()
    except Exception:
        return False


def _wait_extension(timeout=40):
    """轮询直到扩展真能用：CLI status 报 connected，或真命令能打——双信号取快"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _bridge_status(timeout=5)
        if s and s.get("extension_connected"):
            return True
        try:
            _wb_call("list_browser_tabs", {}, timeout=6)
            return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _probe_loop(tries=4, gap=3):
    """快速探测链路：evaluate 打一次试试。卡死不等，交给重启"""
    for i in range(tries):
        try:
            r = _wb_call("evaluate", {"code": "1"}, timeout=15)
            if r.get("ok"):
                log(f"probe ok at try {i + 1}")
                return True
        except Exception:
            pass
        time.sleep(gap)
    return False


def _kimi_tabs():
    try:
        r = _wb_call("list_browser_tabs", {}, timeout=15)
        return r.get("data", {}).get("tabs", []) or []
    except Exception:
        return []


def _ensure_tab():
    """保证 session 里有用量标签。优先收编用户已开着的可见标签页（单窗口），
    关掉自己多余的背景标签；真没有才新开"""
    have_id = None
    try:
        r = _wb_call("find_tab", {"url": "https://www.kimi.com/"}, timeout=15)
        have_id = (r.get("data") or {}).get("tabId")
    except Exception:
        pass
    tabs = [t for t in _kimi_tabs() if "kimi.com" in str(t.get("url", ""))]
    subs = [t for t in tabs if "settings/subscription" in str(t.get("url", ""))]
    keep = None
    for t in subs:                     # 优先留正在前台看的用量页
        if t.get("active"):
            keep = t
            break
    if keep is None and subs:
        keep = subs[0]
    if have_id and keep and have_id == keep.get("tabId"):
        return                         # 已是同一个标签，完美
    if have_id and keep:
        try:                           # 关掉自己之前开的背景标签
            _wb_call("close_tab", {}, timeout=15)
        except Exception:
            log("close dup session tab fail")
    if keep:
        try:
            _wb_call("attach_tab", {"tabId": keep["tabId"]}, timeout=15)
            log(f"attach tab {keep['tabId']}")
            return
        except Exception:
            log("attach fail: " + traceback.format_exc())
    _wb_call("navigate", {"url": "https://www.kimi.com/settings/subscription",
                          "newTab": True, "group_title": "Kimi 工具"}, timeout=20)


def bootstrap(depth=0):
    """点开悬浮窗一条龙：WebBridge 守护 → Chrome（用量页）→ 等扩展连上 →
    kimi.com 标签 → 探针验证链路真通。平时全健康时几秒完事。"""
    started = False
    handed = False   # 移交给 restart_bridge 时不再记 done（避免日志双份）
    try:
        if not _daemon_up():
            started = True
            if BRIDGE_EXE.exists():
                try:
                    subprocess.Popen([str(BRIDGE_EXE), "start"], shell=False)
                    for _ in range(30):
                        time.sleep(0.5)
                        if _daemon_up():
                            break
                except Exception:
                    log("bridge start fail: " + traceback.format_exc())
        if not _chrome_running():
            started = True
            for c in CHROME_CANDIDATES:
                if c.exists():
                    try:
                        subprocess.Popen([str(c), "https://www.kimi.com/settings/subscription"],
                                         shell=False)
                    except Exception:
                        log("chrome start fail: " + traceback.format_exc())
                    break
            for _ in range(40):
                time.sleep(0.5)
                if _chrome_running():
                    break
        # 关键：等扩展真正连上再动标签页，否则 navigate/attach 白打
        if not _wait_extension(timeout=45 if started else 8):
            log("extension not connected in time")
        try:
            _ensure_tab()
            started = True
        except Exception:
            log("ensure tab fail: " + traceback.format_exc())
        time.sleep(2)
        # 探针：链路必须真通才算完，不通就重启守护再来一轮
        if not _probe_loop():
            if depth < 1:
                log("probe fail, restarting bridge once")
                handed = True
                restart_bridge(depth + 1)
                return
            log("probe fail, giving up this round")
    finally:
        if not handed:
            log(f"bootstrap done started={started}")
        _boot["done"].set()


def restart_bridge(depth=0):
    """扩展卡死时重启守护（stop→start），再补一次引导"""
    _boot["last"] = time.time()
    try:
        if BRIDGE_EXE.exists():
            subprocess.run([str(BRIDGE_EXE), "stop"], capture_output=True, timeout=20)
            time.sleep(2)
            subprocess.Popen([str(BRIDGE_EXE), "start"], shell=False)
            time.sleep(4)
            log("bridge restarted")
    except Exception:
        log("bridge restart fail: " + traceback.format_exc())
    start_bootstrap(f"restart depth={depth}", depth=depth, force=True)


def fs(pt):
    return int(pt)


def _do_fetch():
    import kimi_usage as ku
    raw = ku.fetch_usage(DAEMON_URL)
    rec = ku.parse_record(raw)
    ku.append_history(rec)
    tmp = LATEST.with_suffix(".json.tmp")   # 原子写入
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LATEST)
    ku._log_run(f"OK total={rec['total_ratio']:.2f}% code={rec['code_ratio']:.2f}%")


def fetch_loop():
    _boot["done"].wait(timeout=150)   # 等一条龙引导完再抓，避免冷启动连环失败
    fails = 0
    while True:
        try:
            _do_fetch()
            fails = 0
        except Exception:
            fails += 1
            try:
                import kimi_usage as ku
                ku._log_run("FAIL " + traceback.format_exc()[-400:].replace("\n", " | "))
            except Exception:
                pass
            if fails == 2:
                start_bootstrap("fetch 连败 2")
            elif fails >= 4:
                log("fetch 连败 4 次，重启 WebBridge 守护")
                fails = 0
                restart_bridge()
        # 20 秒心跳：轻戳扩展保持服务工人活跃，断链尽早暴露
        for _ in range(3):
            time.sleep(20)
            try:
                _wb_call("evaluate", {"code": "1"}, timeout=10)
            except Exception:
                log("heartbeat fail")
                start_bootstrap("heartbeat fail")
                break


def load():
    try:
        d = json.loads(LATEST.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(d["ts"])
        d["_stale_min"] = (datetime.now().astimezone() - fetched).total_seconds() / 60
        return d
    except Exception:
        return None


class App:
    def __init__(self):
        global S, W, H
        root = tk.Tk()
        self.root = root
        S = max(root.winfo_fpixels("1i") / 96.0, 1.0)
        W, H = int(124 * S), int(32 * S)

        root.title("KimiUsageWidget")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", CHROMA)   # 全透明背景
        root.configure(bg=CHROMA)
        sw = root.winfo_screenwidth()
        root.geometry(f"{W}x{H}+{max(sw - W - int(16 * S), 0)}+{int(40 * S)}")
        root.resizable(False, False)

        # 右侧竖把手：唯一的装饰线 + 拖拽抓手
        handle = tk.Frame(root, bg=HANDLE, width=max(int(4 * S), 3))
        handle.pack(side="right", fill="y")
        handle.pack_propagate(False)

        row = tk.Frame(root, bg=CHROMA)
        row.pack(fill="both", expand=True, padx=int(6 * S),
                pady=(int(4 * S), int(4 * S)))

        self.big = tk.Label(row, text="--.--%", bg=CHROMA, fg=ACCENT,
                            font=FONT_USAGE)
        self.big.pack(side="left")

        right = tk.Frame(row, bg=CHROMA)
        right.pack(side="right")
        self.time_lbl = tk.Label(right, text="--:--", bg=CHROMA, fg=DIM,
                                 font=(FONT_MONO, fs(7)))
        self.time_lbl.pack(side="left", padx=(0, int(5 * S)))
        self.close_lbl = tk.Label(right, text="×", bg=CHROMA, fg=DIM,
                                  font=("Arial", fs(8), "bold"), cursor="hand2")
        self.close_lbl.pack(side="right")
        self.close_lbl.bind("<Button-1>", lambda e: self._quit())
        self.close_lbl.bind("<Enter>", lambda e: self.close_lbl.config(fg=DANGER))
        self.close_lbl.bind("<Leave>", lambda e: self.close_lbl.config(fg=DIM))

        draggables = (root, row, self.big, right, self.time_lbl, handle)
        for wgt in draggables:
            wgt.bind("<ButtonPress-1>", self._down)
            wgt.bind("<B1-Motion>", self._move)
            wgt.bind("<ButtonRelease-1>", self._up)

        self._dx = self._dy = None
        self._wx = self._wy = 0

        # ── 语音输入状态 ──
        self.voice = {"s": "idle", "t0": 0.0}
        self.ui_q = queue.Queue()
        self._frames = []
        self._stream = None
        self._model = None
        self._model_lock = threading.Lock()
        self._bind_hotkey()

        start_bootstrap("startup", force=True)
        threading.Thread(target=fetch_loop, daemon=True).start()
        self.root.after(50, self._drain)
        self.root.after(200, self._voice_tick)
        self.tick()

        if SELFTEST:
            def _self():
                self.ui_q.put(("state", "work", "自检中…"))
                try:
                    import kimi_sender
                    ok, detail = kimi_sender.send("【悬浮窗自检】语音链路正常 ✅")
                except Exception:
                    log("selftest crash: " + traceback.format_exc())
                    ok = False
                log(f"selftest ok={ok}")
                self.ui_q.put(("state", "ok" if ok else "err", ""))
                self.ui_q.put(("later", 1800, ("state", "idle", "")))
            threading.Timer(2.5, _self).start()

    # ── 拖拽 / 吸附 / 图表 ─────────────────────────────
    def _down(self, e):
        if e.widget == self.close_lbl:
            return
        # 用屏幕坐标 + 起点缓存：鼠标拖过不同控件窗口也不会跳
        self._dx, self._dy = e.x_root, e.y_root
        self._wx, self._wy = self.root.winfo_x(), self.root.winfo_y()

    def _move(self, e):
        if e.widget == self.close_lbl or self._dx is None:
            return
        self.root.geometry(
            f"+{self._wx + e.x_root - self._dx}"
            f"+{self._wy + e.y_root - self._dy}")

    def _up(self, e):
        if e.widget == self.close_lbl:
            return
        self._dx = None
        snap = int(SNAP_PX * S)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        nx, ny = x, y
        for ex in (0, sw - W):
            if abs(x - ex) <= snap:
                nx = ex
        for ey in (0, sh - H):
            if abs(y - ey) <= snap:
                ny = ey
        if nx != x or ny != y:
            self.root.geometry(f"+{nx}+{ny}")

    def _quit(self):
        log("quit")
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            if self._stream:
                self._stream.close()
        except Exception:
            pass
        self.root.destroy()

    # ── 语音：热键 ──────────────────────────────────────
    def _bind_hotkey(self):
        try:
            import keyboard
            keyboard.on_press_key(CFG["hotkey"],
                                  lambda _: self.ui_q.put(("rec_start",)),
                                  suppress=False)
            keyboard.on_release_key(CFG["hotkey"],
                                    lambda _: self.ui_q.put(("rec_stop",)),
                                    suppress=False)
            log(f"hotkey ok: {CFG['hotkey']}")
        except Exception:
            log("hotkey fail: " + traceback.format_exc())

    # ── 语音：录音 ──────────────────────────────────────
    def _audio_cb(self, indata, nframes, time_info, status):
        self._frames.append(indata.copy())

    def start_rec(self):
        if self.voice["s"] != "idle":
            return
        self._frames = []
        try:
            import sounddevice as sd
            self._stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                                          blocksize=1024, callback=self._audio_cb)
            self._stream.start()
        except Exception:
            log("mic fail: " + traceback.format_exc())
            self._set_state("err", "麦克风打不开")
            self.ui_q.put(("later", 2400, ("state", "idle", "")))
            return
        self.voice["t0"] = time.time()
        log("rec start")
        self._set_state("rec")

    def stop_rec(self):
        if self.voice["s"] != "rec":
            return
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        import numpy as np
        audio = (np.concatenate(self._frames, axis=0) if self._frames
                 else np.zeros((0, 1), np.int16))
        self._frames = []
        dur = len(audio) / SR
        log(f"rec stop dur={dur:.2f}s")
        if dur < 0.35:
            self._set_state("idle")
            return
        threading.Thread(target=self._worker_transcribe, args=(audio,),
                         daemon=True).start()

    # ── 语音：转写 + 发送 ────────────────────────────────
    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                self.ui_q.put(("state", "work", "模型加载中"))
                from faster_whisper import WhisperModel
                self._model = WhisperModel(CFG["model"], device="cpu",
                                           compute_type="int8")
                log("model loaded")
            return self._model

    def _worker_transcribe(self, audio):
        try:
            import numpy as np
            self.ui_q.put(("state", "work", "转写中…"))
            f32 = audio.astype(np.float32) / 32768.0
            peak = float(np.abs(f32).max()) if f32.size else 0.0
            log(f"peak={peak:.4f}")
            if peak < 0.003:
                self.ui_q.put(("state", "err", "没听清，大点声"))
                self.ui_q.put(("later", 2000, ("state", "idle", "")))
                return
            f32 = (f32 * min(1.0, 0.8 / peak)).astype(np.float32).reshape(-1)
            model = self._get_model()
            segs, _ = model.transcribe(f32, language="zh", beam_size=5,
                                       vad_filter=False,
                                       initial_prompt="以下是普通话的句子。")
            text = "".join(s.text for s in segs).strip()
            log(f"text={text[:40]!r} len={len(text)}")
            if not text:
                self.ui_q.put(("state", "err", "没转出来，再说一次"))
                self.ui_q.put(("later", 2000, ("state", "idle", "")))
                return
            self.ui_q.put(("clip", text))
            self.ui_q.put(("state", "work", "发送中…"))
            try:
                import kimi_sender
                ok, detail = kimi_sender.send(text)
            except Exception:
                log("send crash: " + traceback.format_exc())
                ok, detail = False, "exception"
            log(f"send ok={ok} detail={detail}")
            if ok:
                self.ui_q.put(("state", "ok", ""))
                self.ui_q.put(("later", 1600, ("state", "idle", "")))
            else:
                self.ui_q.put(("state", "err", "发送失败，已复制到剪贴板"))
                self.ui_q.put(("later", 2600, ("state", "idle", "")))
        except Exception:
            log("worker crash: " + traceback.format_exc())
            self.ui_q.put(("state", "err", "内部错误(见 voice.log)"))
            self.ui_q.put(("later", 2600, ("state", "idle", "")))

    # ── 语音：状态显示 + 队列 ────────────────────────────
    def _set_state(self, s, text=""):
        self.voice["s"] = s
        labels = {
            "rec": "● REC 0:00",
            "work": "⏳ " + (text or "转写中…"),
            "ok": "✓ 已发送",
            "err": "✗ " + (text or "失败，已复制"),
        }
        colors = {"rec": DANGER, "work": WARN, "ok": OK, "err": DANGER}
        if s == "idle":
            self.time_lbl.pack(side="left", padx=(0, int(5 * S)))
            self.big.config(font=FONT_USAGE)
            self._render_usage()
        else:
            self.time_lbl.pack_forget()   # 录制/发送期间隐藏用量时间，只留状态
            self.big.config(font=FONT_STATUS, text=labels[s][:12], fg=colors[s])

    def _drain(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                kind = item[0]
                if kind == "state":
                    self._set_state(item[1], item[2])
                elif kind == "clip":
                    try:
                        self.root.clipboard_clear()
                        self.root.clipboard_append(item[1])
                    except Exception:
                        log("clip fail: " + traceback.format_exc())
                elif kind == "rec_start":
                    self.start_rec()
                elif kind == "rec_stop":
                    self.stop_rec()
                elif kind == "later":
                    self.root.after(item[1], lambda a=item[2]: self.ui_q.put(a))
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _voice_tick(self):
        if self.voice["s"] == "rec":
            d = int(time.time() - self.voice["t0"])
            self.big.config(text=f"● REC {d // 60}:{d % 60:02d}", fg=DANGER)
        self.root.after(200, self._voice_tick)

    # ── 用量显示 ──────────────────────────────────────
    def _render_usage(self):
        d = load()
        if d:
            total = float(d.get("total_ratio") or 0)
            stale = max(d.get("_stale_min", 999), 0)
            upd = datetime.fromisoformat(d["ts"]).astimezone().strftime("%H:%M")
            self.time_lbl.config(text=upd)
            self.big.config(text=f"{total:.2f}%")
            if stale > STALE_MIN:
                self.big.config(fg=WARN)
                self.time_lbl.config(fg=WARN)
            else:
                color = ACCENT if total < 60 else (WARN if total < 85 else DANGER)
                self.big.config(fg=color)
                self.time_lbl.config(fg=DIM)
        else:
            self.big.config(text="--.--%", fg=DIM)

    def tick(self):
        if self.voice["s"] == "idle":
            self._render_usage()
        self.root.after(UI_INTERVAL, self.tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
