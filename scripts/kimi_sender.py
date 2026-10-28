#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kimi_sender.py — 把文字送进当前打开的 Kimi 桌面端对话框并回车发送（纯本地，零 token）

流程：找 Kimi 主窗口 -> 激活 -> 按配置比例点击输入框 -> 剪贴板粘贴 -> Enter
坐标用窗口内比例（默认 0.59, 0.90，来自 2026-10-28 实测截图），窗口大小/位置变化都稳。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
CFG_PATH = HERE / "config.json"
CFG = {"click_fx": 0.59, "click_fy": 0.90}
if CFG_PATH.exists():
    try:
        CFG.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass

KIMI_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Kimi" / "Kimi.exe"
TITLE = "Kimi"          # 主窗口标题（"Kimi Widget" 是悬浮窗，排除）

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _log(msg):
    try:
        with open(HERE / "voice.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + " [sender] " + msg + "\n")
    except Exception:
        pass


# ── 找窗口 ─────────────────────────────────────────────
def _enum_windows():
    """所有标题为 Kimi 的主窗口 [(hwnd, rect, visible)]，不排除最小化（IsWindowVisible 对最小化窗口返回 False）"""
    found = []

    def cb(hwnd, _):
        t = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, t, 256)
        if t.value.strip().lower() != TITLE.lower():
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, r, bool(user32.IsWindowVisible(hwnd))))
        return True

    CF = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    user32.EnumWindows(CF(cb), 0)
    return found


def _area(r):
    return max(0, r.right - r.left) * max(0, r.bottom - r.top)


def find_window(wait=0.0, launch=True):
    """找 Kimi 主窗口；wait>0 时找不到会尝试拉起应用并等待窗口出现"""
    deadline = time.time() + wait
    launched = False
    while True:
        wins = _enum_windows()
        if wins:
            # 优先可见的，面积大的（排除最小化到任务栏的小缩略）
            wins.sort(key=lambda x: (x[2], _area(x[1])), reverse=True)
            return wins[0][0]
        if time.time() >= deadline:
            return None
        if launch and not launched and KIMI_EXE.exists():
            launched = True
            try:
                subprocess.Popen([str(KIMI_EXE)], shell=False)
                _log("launched Kimi.exe")
            except Exception as e:
                _log(f"launch fail: {e}")
        time.sleep(0.6)


# ── 激活窗口 ───────────────────────────────────────────
def _fg():
    return user32.GetForegroundWindow()


def _try_attach_foreground(hwnd):
    fg = _fg()
    if fg == hwnd:
        return True
    t_cur = kernel32.GetCurrentThreadId()
    pid = wt.DWORD()
    t_fg = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    ok = user32.AttachThreadInput(t_cur, t_fg, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(t_cur, t_fg, False)
    time.sleep(0.4)
    hit = _fg() == hwnd
    _log(f"attach trick ok={hit} (attach={ok}, fg={fg})")
    return hit


def _dummy_input():
    """SendInput 相对位移(0,0)：注册一次用户输入解除前台锁，鼠标不动"""
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", wt.POINTER(wt.ULONG))]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("mi", MOUSEINPUT)]
    inp = INPUT()
    inp.type = 0  # INPUT_MOUSE
    inp.mi = MOUSEINPUT(0, 0, 0, 0x0001, 0, None)  # MOUSEEVENTF_MOVE
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def activate(hwnd):
    """软件手段尽量激活；真激活不了返回 False（交给点击兜底）"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.7)
    if _fg() == hwnd:
        return True

    if _try_attach_foreground(hwnd):
        _log("activate via attach")
        return True

    _dummy_input()  # 合成输入 → 本进程获得"最后输入"资格
    time.sleep(0.15)
    if _try_attach_foreground(hwnd):
        _log("activate via input-unlock")
        return True

    if not user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        time.sleep(0.5)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(0.8)
    if _fg() == hwnd:
        _log("activate via minimize/restore")
        return True
    if _try_attach_foreground(hwnd):
        _log("activate via attach round2")
        return True
    _log("software activate FAILED, falling back to click")
    return False


# ── 点击 ───────────────────────────────────────────────
def click_input(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    fx = float(CFG.get("click_fx", 0.59))
    fy = float(CFG.get("click_fy", 0.90))
    x = int(r.left + (r.right - r.left) * fx)
    y = int(r.top + (r.bottom - r.top) * fy)
    user32.SetCursorPos(x, y)
    time.sleep(0.08)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    _log(f"click ({x},{y}) win=({r.left},{r.top},{r.right},{r.bottom})")
    time.sleep(0.25)


# ── 剪贴板（ctypes，零依赖）──────────────────────────────
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = wt.LPVOID
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
user32.SetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]


def set_clipboard(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    for attempt in range(8):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.08)
    else:
        raise RuntimeError("clipboard busy")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise RuntimeError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


# ── 主入口 ─────────────────────────────────────────────
def send(text):
    """把 text 粘贴进 Kimi 对话框并回车。返回 (ok, detail)"""
    try:
        import keyboard  # 已有依赖，发组合键比裸 SendInput 省心
    except Exception:
        return False, "keyboard lib missing"

    hwnd = find_window(wait=12.0)
    if not hwnd:
        return False, "kimi window not found"
    _log(f"hwnd={hwnd}")

    if not activate(hwnd):
        _log("note: software activate failed, click will decide")
    click_input(hwnd)
    time.sleep(0.3)
    if _fg() != hwnd:
        _log("click activate FAILED")
        return False, "activate failed (kimi 未能置前)"

    try:
        set_clipboard(text)
    except Exception as e:
        return False, f"clipboard: {e}"
    _log(f"paste {len(text)} chars")

    time.sleep(0.15)
    keyboard.send("ctrl+v")
    time.sleep(0.35)
    keyboard.send("enter")
    time.sleep(0.2)
    _log("sent")
    return True, "ok"


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "【语音输入自检】链路正常"
    ok, detail = send(msg)
    print("OK" if ok else f"FAIL: {detail}")
    sys.exit(0 if ok else 1)
