# Kimi 用量悬浮窗 (kimi-voice-widget)

Windows 桌面悬浮窗：实时显示 Kimi（kimi.com）账户用量百分比 + 按住说话就能把语音转文字发进 Kimi 桌面 App 当前会话。零 API token，全部本地完成。

## 功能

- **用量显示**：每 60 秒自动从 `kimi.com/settings/subscription` 读取月度用量，蓝字百分比 + 更新时间，超出阈值变色（60% 黄 / 85% 红 / 数据超 5 分钟琥珀）
- **语音输入**：按住右 Ctrl（可改）说话，松手自动：本地 faster-whisper 转写 → 激活 Kimi 窗口 → 点击输入框 → 粘贴 → 回车发送
- **一键自愈**：点开即自动拉起整条链路（WebBridge 守护 → Chrome 用量页 → 扩展连接），20 秒心跳保活 + 断链自动修复 + 守护卡死自动重启
- **单窗口设计**：直接复用你正在看的用量标签页抓数，不多开窗口
- **轻量透明**：约 60KB 代码，透明背景小条，贴边自动吸附，可开机自启

## 快速开始

环境：Windows + Python 3.10+（含 tkinter）、Chrome + [Kimi WebBridge](https://cdn.kimi.com/webbridge/latest/skills/kimi-webbridge.tar.gz) 扩展、Kimi 桌面 App（语音发送目标）。

```powershell
pip install faster-whisper sounddevice keyboard numpy
```

1. 把 `scripts/` 里三个 py 文件复制到任意稳定目录（如 `%USERPROFILE%\.kimi_openclaw\workspace\tools\kimi-usage\`）
2. 建快捷方式：目标填 `pythonw.exe "目录\usage_widget.pyw"`，想开机自启就放进 `shell:startup`
3. 双击运行。首次运行自动生成 `config.json`

语音首用会下载 whisper 模型（国内已配 hf-mirror 加速），之后常驻内存。

## 配置（config.json）

| 字段 | 默认 | 说明 |
|------|------|------|
| `hotkey` | `"right ctrl"` | 按住说话的热键（keyboard 库键名） |
| `model` | `"small"` | faster-whisper 模型：tiny / base / small / medium |
| `click_fx` / `click_fy` | `0.59` / `0.90` | Kimi 窗口输入框位置（宽/高比例），粘贴打偏就调它 |

## 工作原理

```
悬浮窗 ──HTTP──▶ 127.0.0.1:10086 WebBridge 守护 ──WS──▶ Chrome 扩展 ──evaluate──▶ 已登录的用量页
语音：热键 ──▶ 录音 ──▶ faster-whisper 本地转写 ──▶ 剪贴板 ──▶ ctypes 激活 Kimi 窗口 ──▶ 粘贴 ──▶ Enter
```

不是浏览器调试：不开 DevTools、不走远程调试端口，扩展在页面里跑读取脚本（类似油猴），借你的登录态读数。

## 目录结构

```
├── SKILL.md                  # 给 AI 助手的完整使用/排障指南（OpenClaw Skill 格式）
├── scripts/
│   ├── usage_widget.pyw      # 悬浮窗主程序（入口，双击/快捷方式运行）
│   ├── kimi_usage.py         # 抓数 + 解析 + 历史（也可 CLI 单跑）
│   ├── kimi_sender.py        # ctypes 窗口激活 + 粘贴 + 回车（带 --selftest 自检）
│   └── config.example.json   # 配置模板
├── assets/
│   └── gauge.ico             # 快捷方式图标
└── references/
    └── kimi-api.md           # Kimi 订阅 Connect-RPC 接口参考（维护用）
```

## 常见问题

- **数据变琥珀/停更**：扩展休眠或守护卡死，悬浮窗 90 秒内自愈；急用手动 `kimi-webbridge.exe stop` 再 `start`
- **语音转写了但没发出去**：输入框坐标偏了，调 `click_fx/click_fy`
- **首次语音慢**：模型下载加载，仅一次
- 更多见 [SKILL.md](SKILL.md) 的排障表

## License

MIT
