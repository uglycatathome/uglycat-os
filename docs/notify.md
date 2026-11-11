# 通知渠道

UglyCat OS 默认只做两件事：**写本地文件** + **推 Server酱**。
默认不启用企业微信、也不提供 Web 面板（内核保留了通用 webhook 通道，但未配置即完全不生效）。

## 渠道一览

| 渠道 | 默认 | 需要配置 | 行为 |
|---|---|---|---|
| `file` | 开启 | 无 | 每条消息写 `data/inbox/<时间戳>_<插件>.md` |
| `serverchan` | 开启 | `.env` 里 `SERVERCHAN_SENDKEY` | 推送到你的微信（Server酱 Turbo） |

`serverchan` 默认 `enabled: true`，**只判断 SendKey 是否存在**：
填了就推，没填就静默跳过并写一条 warning，不影响主流程。

## 配置 Server酱

1. 打开 <https://sct.ftqq.com>，微信扫码登录
2. 复制页面上的 **SendKey**（形如 `SCTxxxxxx`）
3. 写进 `.env`：

```bash
SERVERCHAN_SENDKEY=SCT你的SendKey
```

4. 验证：

```bash
./uglycat run news_digest --notify
```

微信应收到一条「每日新闻解读」（内容形如 [examples/sample_digest.md](../examples/sample_digest.md)，
一条纯经济类新闻的完整解读）。没收到时看控制台 warning：

| 日志 | 含义 | 处理 |
|---|---|---|
| `SendKey 未配置` | `.env` 没读到变量 | 确认文件在仓库根目录、变量名无空格 |
| `code != 0` | Server酱 返回错误 | 免费版每天 5 条上限，或 SendKey 失效 |
| 连接超时 / `HTTP 4xx` | 网络或接口异常 | 连接阶段自动重试 1 次；仍失败进 outbox 待补发 |

## 推送失败时会怎样

失败不会中断主流程，本地 `data/inbox/` 一定有产物。2026-11-11 起的行为：

- 单次请求超时 25s（`timeout_s` 可配）；连接/握手阶段失败重试 1 次
  （`connect_retries`）。**读超时不重试**：服务端可能已收单，重发会重复推送。
- 失败消息落到 `data/outbox/serverchan/*.json`，含标题、正文、原因与状态：
  `pending` 在下次推送前自动补发；`uncertain` 只保留等人工确认；
  `sending` 是补发中断的残留，一律降级为 `uncertain`；超过
  `outbox_max_age_hours`（默认 24h）或 `outbox_max_attempts`（默认 3 次）不再自动补发。
- 若配置了 `alert_group` 指向的 webhook 且 `alert_enabled: true`，失败时会发一条
  告警（同日同插件去重）；本模板默认把该开关设为 `false`，告警不会触发。
- `code != 0`（免费版每天 5 条用尽）是明确拒绝，会进 `pending` 等次日自动补发。

手动处理：

```bash
./uglycat outbox list          # 查看待补推 / 待确认条目
./uglycat outbox flush         # 立即补发确定未送达的条目
./uglycat outbox confirm <id>  # 人工确认某条确实没收到后再补发（id 可用前缀）
```

## 只想本地看

不填 `SERVERCHAN_SENDKEY` 即可：消息仍然写入 `data/inbox/`，
用任意 Markdown 阅读器打开。

## 想加别的渠道

内核的通知分发在 `family_os/kernel/notifier.py`，渠道类型由
`config.yaml` 的 `notifier.channels[].type` 决定。新增渠道的做法：

1. 在 `Notifier._send()` 的分支里加一个 `elif channel.type == "xxx"`
2. 实现一个 `_send_xxx(result, plugin_name, channel)`，**失败只记 warning、不抛出**
3. 密钥一律从 `.env` 读（`channel.sendkey_env`），不要写进 YAML

> 默认不配置企业微信/Telegram 等渠道：渠道越多，密钥泄露面越大，
> 且与「本地优先、最小依赖」的定位冲突。需要时按上面三步自行扩展即可。

## 安全约定

- SendKey 只存在 `.env`（已在 `.gitignore`）
- 日志默认脱敏：带密钥的 URL 不会落盘（`family_os/kernel/logging_utils.py`）
- 不要在任何 Issue/PR 里贴 SendKey 或完整 webhook URL
