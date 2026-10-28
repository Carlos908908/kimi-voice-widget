#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Kimi 订阅用量抓取 + 本地仪表盘

通过 kimi-webbridge 守护进程（127.0.0.1:10086）借用已登录的浏览器会话
调用 Kimi 订阅接口，把用量追加到本地历史（history.jsonl），并重新生成
可视化仪表盘（dashboard.html）。

用法:
  python kimi_usage.py              抓数据 → 存历史 → 生成仪表盘并打开
  python kimi_usage.py --no-open    不自动打开浏览器
  python kimi_usage.py --json       只打印原始 JSON
  python kimi_usage.py login-status 检查登录态

token 只存在浏览器 localStorage 里，由页面 JS 现场读取现场调用，
本脚本不保存、不打印任何凭证。
"""

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlreq
from urllib.error import URLError

DAEMON_URL = "http://127.0.0.1:10086"
SESSION = "kimi-usage"
KIMI_HOME = "https://www.kimi.com/"
SUB_PAGE = "https://www.kimi.com/settings/subscription"
API_BASE = "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/"

HERE = Path(__file__).resolve().parent
HISTORY_FILE = HERE / "history.jsonl"
DASHBOARD_FILE = HERE / "dashboard.html"
RUN_LOG = HERE / "run.log"

JS_FETCH = r"""
(async () => {
  const t = localStorage.getItem('access_token');
  if (!t) return JSON.stringify({fatal:'no_token'});
  const h = {'Content-Type':'application/json','connect-protocol-version':'1','Authorization':'Bearer '+t};
  const call = async (path, body) => {
    const r = await fetch('https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/' + path,
      {method:'POST', headers:h, body: JSON.stringify(body||{})});
    let d = null; const txt = await r.text();
    try { d = JSON.parse(txt); } catch(e) { d = txt.slice(0, 300); }
    return {status: r.status, data: d};
  };
  const stats  = await call('GetSubscriptionStats');
  const sub    = await call('GetSubscription');
  const actions= await call('ListBalanceActions', {page_size: 50, filter: {unit:'UNIT_CREDIT', balance_types:['SUBSCRIPTION']}});
  return JSON.stringify({stats, sub, actions});
})()
"""


class CliError(Exception):
    pass


def daemon_call(action, args, daemon_url, timeout=30):
    body = json.dumps({"action": action, "args": args, "session": SESSION}).encode("utf-8")
    req = urlreq.Request(
        daemon_url + "/command", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            env = json.loads(resp.read().decode("utf-8", "replace"))
    except URLError as e:
        raise CliError(
            f"连不上 kimi-webbridge 守护进程（{daemon_url}）：{e}\n"
            "请先启动它：%USERPROFILE%\\.kimi-webbridge\\bin\\kimi-webbridge.exe start\n"
            "并确认 Chrome 里的 Kimi 浏览器扩展已连接。")
    except Exception as e:
        raise CliError(f"守护进程请求失败：{e}")
    if not env.get("ok"):
        err = env.get("error", {})
        raise CliError(f"浏览器扩展报错 [{err.get('code','?')}]: {err.get('message','?')}")
    return env.get("data")


def ensure_tab(daemon_url):
    """复用本会话已开的 kimi.com 标签页；没有就新开一个并导航到订阅页。"""
    try:
        data = daemon_call("find_tab", {"url": KIMI_HOME}, daemon_url)
        if data.get("success"):
            return
    except CliError:
        pass  # 本会话没有打开的 kimi.com 标签页 → 新开一个
    daemon_call("navigate", {"url": SUB_PAGE, "newTab": True, "group_title": "Kimi 用量"}, daemon_url)
    time.sleep(3)


def fetch_usage(daemon_url):
    """在页面里现场取 token 调接口。401 时刷新页面重试一次（页面加载会自动续 token）。"""
    last_raw = None
    for attempt in range(2):
        data = daemon_call("evaluate", {"code": JS_FETCH}, daemon_url, timeout=45)
        if data.get("type") != "string" or not data.get("value"):
            raise CliError(f"页面返回异常：{json.dumps(data, ensure_ascii=False)[:200]}")
        raw = json.loads(data["value"])
        last_raw = raw
        if raw.get("fatal") == "no_token":
            raise CliError("浏览器里没有 Kimi 登录态，请先在 Chrome 登录 kimi.com")
        statuses = [raw.get(k, {}).get("status") for k in ("stats", "sub", "actions")]
        if all(s == 200 for s in statuses):
            return raw
        if 401 in statuses and attempt == 0:
            daemon_call("navigate", {"url": SUB_PAGE}, daemon_url)
            time.sleep(5)
            continue
        raise CliError(f"接口返回异常：statuses={statuses} body={json.dumps(raw, ensure_ascii=False)[:300]}")
    raise CliError(f"刷新重试后仍 401，请打开 {SUB_PAGE} 确认登录态。原始返回：{json.dumps(last_raw, ensure_ascii=False)[:300]}")


def parse_record(raw):
    stats = raw["stats"]["data"]
    sub_d = raw["sub"]["data"].get("subscription") or {}
    bal = stats.get("subscriptionBalance") or {}
    wallet = (stats.get("boosterWallets") or [{}])[0]
    actions = raw["actions"]["data"].get("actions") or []

    def cents(c):
        try:
            return int(c.get("priceInCents", "0"))
        except Exception:
            return 0

    now = datetime.now(timezone.utc).astimezone()
    return {
        "ts": now.isoformat(timespec="seconds"),
        "total_ratio": round(float(bal.get("amountUsedRatio", 0)) * 100, 4),
        "code_ratio": round(float(bal.get("kimiCodeUsedRatio", 0)) * 100, 4),
        "monthly_reset": bal.get("expireTime"),
        "h5_reset": (stats.get("ratelimitCode5h") or {}).get("resetTime"),
        "plan": (sub_d.get("goods") or {}).get("title", "?"),
        "plan_price_cents": cents(((sub_d.get("goods") or {}).get("amounts") or [{}])[0]),
        "valid_until": sub_d.get("currentEndTime"),
        "next_billing": sub_d.get("nextBillingTime"),
        "auto_renew": sub_d.get("status") != "SUBSCRIPTION_STATUS_CANCEL",
        "sub_active": bool(sub_d.get("active")),
        "wallet_left_cents": cents(wallet.get("moneyLeft", {})),
        "wallet_month_used_cents": cents(wallet.get("monthlyUsed", {})),
        "wallet_enabled": wallet.get("status") != "STATUS_DISABLED",
        "recent": [
            {
                "time": a.get("timestamp"),
                "feature": a.get("feature", ""),
                "title": a.get("title", ""),
                "ratio": round(sum(float(i.get("amountRatio", 0)) for i in a.get("items", [])) * 100, 4),
            }
            for a in actions[:20]
        ],
    }


def append_history(rec):
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        slim = {k: v for k, v in rec.items() if k != "recent"}
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")


def load_history(limit=400):
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def fmt_local(iso, fmt="%m-%d %H:%M"):
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime(fmt)
    except Exception:
        return iso


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_chart_svg(hist, width=920, height=230):
    pts_total, pts_code = [], []
    times = []
    for h in hist:
        try:
            t = datetime.fromisoformat(h["ts"]).timestamp()
        except Exception:
            continue
        times.append(t)
        pts_total.append((t, float(h.get("total_ratio", 0))))
        pts_code.append((t, float(h.get("code_ratio", 0))))
    if len(pts_total) < 2:
        return '<div class="empty-chart">历史点还不够（再多跑几次就能画出曲线）</div>', ""
    pad_l, pad_r, pad_t, pad_b = 46, 14, 14, 26
    t0, t1 = min(times), max(times)
    vmax = max(10.0, max(v for _, v in pts_total) * 1.25)
    vmax = ((int(vmax) // 10) + 1) * 10
    span = max(1.0, t1 - t0)

    def xy(tv):
        t, v = tv
        x = pad_l + (t - t0) / span * (width - pad_l - pad_r)
        y = pad_t + (1 - v / vmax) * (height - pad_t - pad_b)
        return f"{x:.1f},{y:.1f}"

    total_line = " ".join(xy(p) for p in pts_total)
    code_line = " ".join(xy(p) for p in pts_code)

    def xlabel(ts):
        return datetime.fromtimestamp(ts).astimezone().strftime("%m-%d %H:%M")

    grid = ""
    for i in range(1, 4):
        y = pad_t + i * (height - pad_t - pad_b) / 4
        val = vmax * (1 - i / 4)
        grid += f'<line x1="{pad_l}" y1="{y:.0f}" x2="{width-pad_r}" y2="{y:.0f}" class="grid"/>'
        grid += f'<text x="{pad_l-6}" y="{y+4:.0f}" class="axis" text-anchor="end">{val:.0f}%</text>'

    svg = f'''<svg viewBox="0 0 {width} {height}" class="chart">
{grid}
<polyline points="{total_line}" class="line-total"/>
<polyline points="{code_line}" class="line-code"/>
<circle cx="{xy(pts_total[-1]).split(",")[0]}" cy="{xy(pts_total[-1]).split(",")[1]}" r="3.5" class="dot-total"/>
<text x="{pad_l}" y="{height-8}" class="axis">{esc(xlabel(t0))}</text>
<text x="{width-pad_r}" y="{height-8}" class="axis" text-anchor="end">{esc(xlabel(t1))}</text>
</svg>'''
    legend = ('<span class="lg lg-total">■ 总用量</span>'
              '<span class="lg lg-code">■ Code 用量</span>')
    return svg, legend


def render_dashboard(rec, hist):
    svg, legend = build_chart_svg(hist)
    rows = ""
    for a in rec["recent"]:
        rows += (f"<tr><td>{esc(fmt_local(a['time'], '%m-%d %H:%M:%S'))}</td>"
                 f"<td>{esc(a['feature'].replace('FEATURE_', ''))}</td>"
                 f"<td class='t'>{esc(a['title'])}</td>"
                 f"<td>{a['ratio']:.2f}%</td></tr>")
    renew_badge = ("<span class='badge ok'>自动续费中</span>" if rec["auto_renew"]
                   else "<span class='badge warn'>已关闭自动续费</span>")
    wallet_badge = ("<span class='badge ok'>已开启</span>" if rec["wallet_enabled"]
                    else "<span class='badge off'>已关闭</span>")
    total = rec["total_ratio"]
    code = rec["code_ratio"]
    bar_color = "#37c28a" if total < 50 else ("#e6b450" if total < 80 else "#e05d5d")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Kimi 用量仪表盘</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #0f1115; color: #d7dae0; font: 14px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif; padding: 28px; max-width: 1000px; margin: 0 auto; }}
h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
.sub {{ color: #7c8494; font-size: 12px; margin-bottom: 20px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 16px; }}
.card {{ background: #171a21; border: 1px solid #232733; border-radius: 12px; padding: 16px 18px; }}
.card h3 {{ font-size: 12px; color: #8b93a5; font-weight: 500; margin-bottom: 8px; }}
.big {{ font-size: 34px; font-weight: 700; }}
.big small {{ font-size: 15px; color: #8b93a5; font-weight: 400; }}
.bar {{ height: 8px; background: #232733; border-radius: 4px; margin-top: 10px; overflow: hidden; }}
.bar i {{ display: block; height: 100%; border-radius: 4px; background: {bar_color}; width: {min(total, 100):.2f}%; }}
.kv {{ color: #aab1c0; font-size: 13px; }}
.kv b {{ color: #e8ebf1; font-weight: 600; float: right; }}
.badge {{ font-size: 11px; padding: 2px 8px; border-radius: 10px; margin-left: 6px; vertical-align: 2px; }}
.badge.ok {{ background: #143726; color: #37c28a; }}
.badge.warn {{ background: #3a2f14; color: #e6b450; }}
.badge.off {{ background: #262a35; color: #8b93a5; }}
.panel {{ background: #171a21; border: 1px solid #232733; border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }}
.panel h2 {{ font-size: 14px; font-weight: 600; margin-bottom: 10px; color: #e8ebf1; }}
.chart {{ width: 100%; height: auto; }}
.grid {{ stroke: #20242e; stroke-width: 1; }}
.axis {{ fill: #7c8494; font-size: 11px; }}
.line-total {{ fill: none; stroke: #4f8cff; stroke-width: 2; }}
.line-code {{ fill: none; stroke: #37c28a; stroke-width: 1.6; opacity: .85; }}
.dot-total {{ fill: #4f8cff; }}
.lg {{ font-size: 12px; margin-right: 14px; }}
.lg-total {{ color: #4f8cff; }} .lg-code {{ color: #37c28a; }}
.empty-chart {{ color: #7c8494; padding: 30px 0; text-align: center; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
td, th {{ padding: 6px 8px; border-bottom: 1px solid #20242e; text-align: left; white-space: nowrap; }}
td.t {{ max-width: 380px; overflow: hidden; text-overflow: ellipsis; }}
th {{ color: #8b93a5; font-weight: 500; }}
</style></head><body>
<h1>Kimi {esc(rec['plan'])} 用量仪表盘</h1>
<div class="sub">更新于 {esc(fmt_local(rec['ts'], '%Y-%m-%d %H:%M:%S'))}（每 5 分钟自动刷新页面）</div>
<div class="cards">
  <div class="card"><h3>总使用量（月度）</h3>
    <div class="big">{total:.2f}<small>%</small></div>
    <div class="bar"><i></i></div>
    <div class="kv" style="margin-top:8px">月度重置：<b>{esc(fmt_local(rec['monthly_reset'], '%m-%d %H:%M'))}</b></div>
  </div>
  <div class="card"><h3>Code 用量（月度）</h3>
    <div class="big">{code:.2f}<small>%</small></div>
    <div class="kv">5 小时窗口重置：<b data-countdown="{esc(rec['h5_reset'] or '')}">{esc(fmt_local(rec['h5_reset'], '%m-%d %H:%M'))}</b></div>
  </div>
  <div class="card"><h3>套餐</h3>
    <div class="kv">价格：<b>¥{rec['plan_price_cents']/100:.0f}/月</b></div>
    <div class="kv">有效期至：<b>{esc(fmt_local(rec['valid_until'], '%Y-%m-%d'))}</b></div>
    <div class="kv">下次扣款：<b>{esc(fmt_local(rec['next_billing'], '%m-%d %H:%M'))}</b></div>
    <div class="kv">续费状态：{renew_badge}</div>
  </div>
  <div class="card"><h3>额度加油包 {wallet_badge}</h3>
    <div class="kv">当前余额：<b>¥{rec['wallet_left_cents']/100:.2f}</b></div>
    <div class="kv">本周期消费：<b>¥{rec['wallet_month_used_cents']/100:.2f}</b></div>
  </div>
</div>
<div class="panel"><h2>用量走势</h2>{legend}{svg}</div>
<div class="panel"><h2>最近消耗明细</h2>
<table><tr><th>时间</th><th>功能</th><th>项目</th><th>消耗</th></tr>{rows}</table>
</div>
<script>
document.querySelectorAll('[data-countdown]').forEach(el => {{
  const iso = el.getAttribute('data-countdown');
  if (!iso) return;
  const tick = () => {{
    const left = new Date(iso).getTime() - Date.now();
    if (left <= 0) {{ el.textContent = '已重置'; return; }}
    const m = Math.floor(left/60000), h = Math.floor(m/60);
    el.textContent = (h>0? h+'小时':'') + (m%60) + '分钟后';
  }};
  tick(); setInterval(tick, 30000);
}});
</script>
</body></html>"""
    DASHBOARD_FILE.write_text(html, encoding="utf-8")


def _log_run(line):
    """往 run.log 追加一行运行记录（保留最近 100 行），静默后台跑的时候排错用。"""
    try:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        if RUN_LOG.exists():
            lines = RUN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(f"[{stamp}] {line}")
        RUN_LOG.write_text("\n".join(lines[-100:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def cmd_fetch(args):
    ensure_tab(args.daemon_url)
    raw = fetch_usage(args.daemon_url)
    rec = parse_record(raw)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    append_history(rec)
    render_dashboard(rec, load_history())
    (HERE / "latest.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    _log_run(f"OK total={rec['total_ratio']:.2f}% code={rec['code_ratio']:.2f}%")
    print(f"总使用量 {rec['total_ratio']:.2f}% ｜ Code {rec['code_ratio']:.2f}% ｜ "
          f"月度重置 {fmt_local(rec['monthly_reset'])} ｜ 5h窗口重置 {fmt_local(rec['h5_reset'])}")
    print(f"套餐 {rec['plan']} ¥{rec['plan_price_cents']/100:.0f}/月，"
          f"有效期至 {fmt_local(rec['valid_until'], '%Y-%m-%d')}，"
          f"{'自动续费中' if rec['auto_renew'] else '⚠️ 自动续费已关'}")
    print(f"仪表盘：{DASHBOARD_FILE}")
    if not args.no_open:
        webbrowser.open(DASHBOARD_FILE.as_uri())
    return 0


def cmd_login_status(args):
    try:
        ensure_tab(args.daemon_url)
        raw = fetch_usage(args.daemon_url)
        sub = raw["sub"]["data"].get("subscription") or {}
        print(json.dumps({"ok": True, "data": {
            "logged_in": True,
            "user": sub.get("status", ""),
            "plan": (sub.get("goods") or {}).get("title"),
        }}, ensure_ascii=False))
        return 0
    except CliError as e:
        print(json.dumps({"ok": False, "error": {"code": "not_logged_in", "message": str(e)}},
                         ensure_ascii=False))
        return 1


def _pause_if_gui():
    """双击 bat 启动时（KIMI_USAGE_PAUSE=1），结束前停住窗口让他能看清输出。"""
    if not os.environ.get("KIMI_USAGE_PAUSE"):
        return
    try:
        input("\n按回车键关闭窗口...")
    except EOFError:
        pass


def _fix_stdio_encoding():
    """输出被重定向（非控制台）时按 UTF-8 写，避免 GBK 编码不了 ¥ 之类的字符崩掉。
    真正的控制台不动——Windows 控制台走 Unicode API，中文本来就没问题。"""
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main():
    _fix_stdio_encoding()
    p = argparse.ArgumentParser(description="Kimi 订阅用量抓取 + 本地仪表盘")
    p.add_argument("--daemon-url", default=DAEMON_URL, help="kimi-webbridge 守护进程地址")
    p.add_argument("--no-open", action="store_true", help="生成仪表盘后不自动打开浏览器")
    p.add_argument("--json", action="store_true", help="只输出原始 JSON")
    sub_p = p.add_subparsers(dest="cmd")
    sub_p.add_parser("login-status", help="检查 Kimi 登录态")
    args = p.parse_args()
    try:
        if args.cmd == "login-status":
            return cmd_login_status(args)
        return cmd_fetch(args)
    except CliError as e:
        _log_run(f"FAIL {e}")
        print(json.dumps({"ok": False, "error": {"code": "error", "message": str(e)}},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        import traceback
        _log_run("FAIL " + traceback.format_exc()[-500:].replace("\n", " | "))
        return 1
    finally:
        _pause_if_gui()


if __name__ == "__main__":
    sys.exit(main())
