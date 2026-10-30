# UglyCat OS

[![CI](https://github.com/uglycatathome/uglycat-os/actions/workflows/ci.yml/badge.svg)](https://github.com/uglycatathome/uglycat-os/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

一个跑在你自己电脑上的**每日新闻解读助手**：抓取公开新闻源 → 语义去重 → 只留国家级大事 →
解读官方措辞变化 → 给普通人一句可执行的建议 → 通过 Server酱 推到你微信。

- 不抓订阅制媒体全文，不采集任何遥测
- 数据全部留在本地 `data/` 目录
- 默认每天 08:00 跑一次；机器睡眠错过时，下次启动自动补跑

---

## 30 秒安装（交给 AI 执行）

把仓库链接丢给任意编码 AI（Trae / Cursor / Claude Code / Codex），然后说：

> 请按仓库根目录 `AGENTS.md` 安装本项目。

AI 会依次执行 `install/setup.* --check-only`、安装、写入 `.env`，直到
`./uglycat doctor --json` 返回 `{"ok": true}`。

**手动安装**

```bash
# macOS / Linux
./install/setup.sh

# Windows PowerShell
.\install\setup.ps1
```

然后在 `.env` 里填两样东西（都只存在本地）：

```bash
# 1) LLM（必填，任选一个；推荐智谱免费模型 glm-4.7-flash）
ZHIPU_API_KEY=你的Key
# 2) Server酱（想在微信收推送就填，不填则只写本地文件）
SERVERCHAN_SENDKEY=SCT你的SendKey
```

验证并跑一次：

```bash
./uglycat doctor                  # 体检，最后一行应显示「体检通过 ✓」
./uglycat run news_digest --notify  # 立即跑一次：写 data/inbox/ 并推送到微信
```

> Windows 下把 `./uglycat` 换成 `uglycat.cmd`（如 `uglycat.cmd doctor`）。

---

## 每天自动推送

```bash
./uglycat install-schedule --time 08:00   # 注册系统原生定时任务（三平台）
./uglycat install-schedule --uninstall    # 移除
```

定时任务执行的是 `run news_digest --notify`：会跑完整流程并推送 Server酱。
机器睡眠错过 08:00 时，下次执行 `./uglycat catch-up` 会补跑当天。

---

## 文档

| 文档 | 内容 |
|---|---|
| [架构](docs/architecture.md) | 内核与插件的关系、数据与隐私边界 |
| [让 AI 帮你装](docs/ai-install.md) | 给 AI 的提示词与检查点 |
| [通知渠道](docs/notify.md) | Server酱 配置、失败排查、自定义渠道 |
| [插件开发](docs/plugin-dev.md) | 插件契约、目录结构、最小示例 |
| [示例输出](examples/sample_digest.md) | 一条推送长什么样 |
| [部署到服务器](docs/deploy-vm.md) | 可选：7×24 常驻（systemd） |

---

## 工作原理

```
公开 RSS / 机构列表页
        │
        ▼
抓取 → 日期去重 → 内容去重 → 语义去重（LLM）
        │
        ▼
精选（≤3 条，只认国家级发布）
        │
        ▼
措辞提取 → 官方措辞解读 → 历史措辞对比 → 一句话建议
        │
        ▼
data/inbox/*.md  +  Server酱
```

核心代码：`family_os/kernel/`（配置、调度、LLM 网关、通知）与
`family_os/plugins/news_digest/`（新闻插件）。插件只通过 `Context` 访问内核服务，
新增插件不需要改动内核。

---

## 隐私与合规

- 不内置任何账号、Cookie 或签名逻辑，只访问公开源
- 不保存订阅制媒体正文（配置里已排除财新等来源）
- 日志默认脱敏（带密钥的 URL 不会落盘）
- 数据目录 `data/`、密钥 `.env` 均被 `.gitignore` 排除

## 许可证

[MIT](LICENSE) —— 随便用、改、商用、再分发，保留版权声明即可。
