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

微信应收到一条「每日新闻解读」。没收到时看控制台 warning：

| 日志 | 含义 | 处理 |
|---|---|---|
| `SendKey 未配置` | `.env` 没读到变量 | 确认文件在仓库根目录、变量名无空格 |
| `code != 0` | Server酱 返回错误 | 免费版每天 5 条上限，或 SendKey 失效 |
| `HTTP 4xx` | 网络/接口异常 | 稍后重试，不影响本地文件输出 |

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
