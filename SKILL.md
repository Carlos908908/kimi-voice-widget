---
name: kimi-voice-widget
description: Windows 桌面悬浮窗：显示 Kimi（kimi.com）账户用量百分比（每 60 秒自动抓取），并支持按住热键语音输入（本地 faster-whisper 转写后自动把文字送进 Kimi 桌面 App 当前对话）。通过 Kimi WebBridge 浏览器扩展读取页面，自带一键引导（守护进程→Chrome→用量页→扩展连接）与断链自愈。Use when installing, running, configuring (hotkey / whisper model / click position), or troubleshooting this widget on a Windows machine — blank or stale usage data, HTTP 502 from the WebBridge daemon, bridge start failure popups, voice text not reaching the Kimi app, drag jitter.
---

# Kimi 用量悬浮窗

一个 pythonw 常驻悬浮窗，两条功能：

1. **用量显示**：蓝字百分比 + 抓取时间，每 60 秒经 Kimi WebBridge 扩展从 `kimi.com/settings/subscription` 页面 evaluate 读取，页面由脚本自动保活（单标签页，直接复用用户可见的用量页）。
2. **语音输入**：按住热键（默认右 Ctrl）说话 → 本地 faster-whisper 转写 → 自动激活 Kimi 桌面 App 窗口、点击输入框、剪贴板粘贴、回车发送。全程零 API token，不走任何网关。

## 环境要求

- Windows + Python 3.10+（含 tkinter；`D:\Python\Python313\pythonw.exe` 是典型路径）
- pip 依赖：`faster-whisper sounddevice keyboard numpy`
- [Kimi WebBridge](https://cdn.kimi.com/webbridge/latest/skills/kimi-webbridge.tar.gz) 守护进程 + Chrome 扩展（装好后扩展 id `fldmhceldgbpfpkbgopacenieobmligc`，daemon 在 `127.0.0.1:10086`）
- Chrome（抓数载体）、Kimi 桌面 App（语音发送目标）

## 安装

1. 把 `scripts/` 三个 py 文件 + `assets/gauge.ico` 复制到稳定目录（如 `%USERPROFILE%\.kimi_openclaw\workspace\tools\kimi-usage\`）。
2. `pip install faster-whisper sounddevice keyboard numpy`（whisper 模型首次运行自动下载，走 `HF_ENDPOINT=https://hf-mirror.com` 加速）。
3. 建快捷方式：目标 `pythonw.exe "<目录>\usage_widget.pyw"`，图标用 gauge.ico；想开机自启就放 shell:startup。
4. 双击运行。首次运行自动在脚本旁生成 `config.json`（模板见 `scripts/config.example.json`）。

单实例互斥（mutex `kimi_usage_widget_v1`）：重复双击不会开第二个。`--selftest` 参数跑一次语音通路自检（不真实发送，最后一步会弹记事本验证激活+粘贴）。

## 配置（config.json）

| 字段 | 默认 | 说明 |
|------|------|------|
| `hotkey` | `"right ctrl"` | keyboard 库键名，按住说话松手发送 |
| `model` | `"small"` | faster-whisper 模型（tiny/base/small/medium） |
| `click_fx` / `click_fy` | `0.59` / `0.90` | Kimi 窗口输入框位置（窗口宽/高的比例）。粘贴没打进输入框就调这两个数 |

## 工作原理（排障前先读）

```
悬浮窗 ──HTTP──▶ 127.0.0.1:10086 daemon ──WS──▶ Chrome 扩展 ──evaluate──▶ 已登录的用量页
语音：hotkey ──▶ sounddevice 录音 ──▶ faster-whisper ──▶ 剪贴板
      ──▶ ctypes 激活 Kimi 窗口（attach-thread-input + 合成点击）──▶ 粘贴 ──▶ Enter
```

- 不是「调试浏览器」：不开 DevTools、不走远程调试端口；扩展在页面里跑读取脚本（类似油猴），借用户登录态。
- 冷启动引导链（bootstrap）：daemon 检测（走 CLI status，**不能**用命令通道探测——扩展消失时命令全 502 但守护活着，误判会触发多余的 start 并弹原生报错）→ 没 Chrome 就拉起并打开用量页 → 双信号等扩展真连上（CLI connected 或 `list_browser_tabs` 通）→ `find_tab` 找会话标签，没有就 `attach_tab` 收编用户已开的用量页（单窗口的关键），再没有才 `navigate` 新开 → evaluate 探针确认链路真通（12s 不通就重启 daemon 再来一轮）。
- 运行时自愈：每 20 秒心跳轻戳扩展（防 MV3 service worker 休眠假死——休眠后 daemon 保留假 socket，status 显示 connected 但命令全 502）；抓取连败 2 次重跑引导，4 次重启 daemon；引导触发有 45s 节流 + 互斥锁防风暴。
- 时间线预期：健康启动 ≤5 秒出数；Chrome 冷启动约 2 秒弹网页 + 10~25 秒等扩展连接出真数；悬浮窗同时显示上次数据（超 5 分钟变琥珀色）。

## 日志

脚本同目录：`voice.log`（语音+引导）、`run.log`（每次抓取结果/报错）、`latest.json`（最近一次数据）。悬浮窗本身无窗口标题栏日志，排障先 tail 这三个文件。

## 常见问题

| 现象 | 处理 |
|------|------|
| 数据停在旧值/琥珀色 | 扩展休眠或 daemon 卡死。等 1~2 个自愈周期（≤90 秒）；不行就 `kimi-webbridge.exe stop` 再 `start`，然后重开悬浮窗 |
| 弹「bridge 打开失败」 | 守护 start 失败（多半是对已运行的守护重复 start）。现已用 CLI status 规避；若再出现先看 `kimi-webbridge.exe status` 的 running 字段 |
| 语音转写了但没发进 Kimi | 输入框坐标偏了：改 config.json 的 click_fx/click_fy（窗口宽/高比例）。跑 `python kimi_sender.py --selftest` 看点击落在哪 |
| 首次语音特别慢 | whisper 模型在下载/加载，之后常驻内存 |
| 按住热键没反应 | 以管理员权限运行过一次 keyboard 依赖的进程会独占钩子；或热键名写错（keyboard 库键名表） |
| 拖动乱飞 | 旧版坐标系 bug，已修（屏幕坐标 e.x_root/e.y_root）。若复现检查 scripts 是否最新 |

## 注意

- 语音发送目标 = **当前打开的 Kimi 会话**，发前看清窗口。
- `peak < 0.003` 判定「没听清」直接丢弃，不会发空消息。
- 悬浮窗拖到屏幕左右边缘会吸附贴边（松手触发）。
