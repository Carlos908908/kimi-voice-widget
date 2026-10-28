# Kimi 订阅接口参考（Connect-RPC）

数据源头：`https://www.kimi.com/settings/subscription` 页面。页面本身调 Connect-RPC 接口（`connect-protocol-version: 1` 头 + JSON body），当前实现走 WebBridge evaluate 在页面上下文里 fetch（自动带登录态）。

| 功能 | 接口 | body | 说明 |
|---|---|---|---|
| 用量总览 | `POST /apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats` | `{}` | `subscriptionBalance.amountUsedRatio`（月度总用量）、`kimiCodeUsedRatio`（Code 月度）、`ratelimitCode5h.resetTime`（5h 窗口重置时间）、`boosterWallets`（加油包） |
| 套餐信息 | `POST .../MembershipService/GetSubscription` | `{}` | 套餐名/价格/有效期/下次扣款/状态（`SUBSCRIPTION_STATUS_CANCEL` = 自动续费已关但本期有效） |
| 消耗明细 | `POST .../MembershipService/ListBalanceActions` | `{"page_size":50,"filter":{"unit":"UNIT_CREDIT","balance_types":["SUBSCRIPTION"]}}` | 分页 `nextPageToken`，每条含 feature/title/timestamp/items[].amountRatio |

## 认证

`Authorization: Bearer <localStorage.access_token>`（页面 JS 现场读取，不落盘）。
kimi-auth cookie 里的 token 是旧的（过期），不能靠 cookie。不带 Bearer 会 401 `REASON_INVALID_AUTH_TOKEN`。
token 失效时刷新一次页面即可——页面加载会用 `localStorage.refresh_token` 自动换新。

## CLI 模式（kimi_usage.py 直接跑）

```powershell
python kimi_usage.py            # 抓取 + 存历史 + 生成 dashboard.html
python kimi_usage.py --no-open  # 不自动打开
python kimi_usage.py --json     # 原始 JSON
python kimi_usage.py login-status
```

依赖：WebBridge 守护（127.0.0.1:10086）+ Chrome 扩展在线 + 已登录 kimi.com。
历史存 `history.jsonl`；悬浮窗模式不需要 dashboard.html。
