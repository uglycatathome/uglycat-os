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

## 一条推送长什么样

```
📰 每日新闻解读 · 2026-10-30
今日抓取 128 条 · 精选 1 条

### 📌 今日要闻
1. **[9月CPI同比上涨0.4% 涨幅连续三个月收窄](链接)**
   来源：中新网·财经
   涨幅比上月收窄 0.2 个百分点；核心 CPI 同比上涨 0.7%，服务价格环比上涨 0.3%。
   为什么重要：物价读数直接决定利率预期，影响存款利息与房贷成本。

### 🔍 官方措辞解读
【核心判断】「涨幅收窄」与「核心CPI回升」并列：整体压力减轻，但服务类在涨价。
1. 涨幅收窄 → 涨得比上月慢，同样的钱贬值速度在变缓。
2. 核心CPI → 去掉食品和能源后的物价，更能看出真实需求。
3. 同比 → 只和去年同月比，避免被季节性误读。

### ⚠️ 历史措辞变化（同主题对比）
1. 「温和上涨」→【力度变化】本月改为「涨幅收窄」，定调由「回升」转向「低位运行」。
2. 「需求偏弱」→【新提法】改为「有效需求仍待巩固」，语气缓和但指向未变。

### 💡 老百姓一句话建议
💡 别急着囤米面油（食品在降价），服务类该锁价就锁价；有房贷的盯住下一次 LPR 报价。
```

完整示例见 [examples/sample_digest.md](examples/sample_digest.md)。

---

## 文档

| 文档 | 内容 |
|---|---|
| [架构](docs/architecture.md) | 内核与插件的关系、数据与隐私边界 |
| [让 AI 帮你装](docs/ai-install.md) | 给 AI 的提示词与检查点 |
| [通知渠道](docs/notify.md) | Server酱 配置、失败排查、自定义渠道 |
| [扩展新闻源](docs/sources.md) | sources.yaml 字段、加源验证流程、给 AI 的模板 |
| [调优提示词](docs/prompts.md) | 用 `prompts/*.md` 持续优化筛选与解读口径 |
| [插件开发](docs/plugin-dev.md) | 插件契约、目录结构、最小示例 |
| [示例输出](examples/sample_digest.md) | 一条推送长什么样 |
| [部署到服务器](docs/deploy-vm.md) | 可选：7×24 常驻（systemd） |

---

## 升级与自定义（不会被覆盖）

```bash
git pull        # 升级到最新：代码与默认规则、默认源清单
```

你自己的东西都放在不会被上游覆盖的位置：

| 你的内容 | 放哪 | 升级时 |
|---|---|---|
| 自加的 RSS 源 | `family_os/config/sources.local.yaml` | ✅ 自动合并，不冲突 |
| 调优的筛选/解读规则 | `prompts/*.md` | ✅ 不冲突 |
| 密钥 | `.env` | ✅ 不冲突 |

---

## 维护说明

个人开源项目：会持续更新，但不承诺固定的 Issue 响应时间；欢迎直接提 PR（见
[CONTRIBUTING.md](CONTRIBUTING.md)）。如果某一期推送质量明显下降，欢迎带截图开 Issue。

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
