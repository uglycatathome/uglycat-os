# 架构：内核与插件

一句话：**内核只做四件事（配置、调度、LLM、通知），其他全是插件；插件只通过 `ctx` 访问内核。**

## 依赖关系

```mermaid
flowchart TD
    SRC[公开 RSS / 机构列表页] --> P[news_digest 插件]
    P -->|run(ctx) 返回 Result| C[Context]
    C --> ST[(SQLite<br/>data/family.db)]
    C --> LLM[LLM Gateway]
    C --> CFG[config.yaml + .env]
    P -->|Result| SCH[Scheduler]
    SCH --> N[Notifier]
    N --> F[data/inbox/*.md]
    N --> SC[Server酱 → 微信]
```

## 内核（L2，`family_os/kernel/`）

| 模块 | 职责 |
|---|---|
| `config.py` | 加载 `config.yaml` + `.env`，产出 `Config` |
| `context.py` | `Context`：插件唯一依赖注入容器（store / config / llm_gateway） |
| `store.py` | SQLite（`data/family.db`）：facts / entities / events / documents / llm_calls |
| `llm_gateway.py` | 统一 LLM 调用：模型路由、降级链、预算、缓存、日志脱敏 |
| `scheduler.py` | APScheduler 按 cron 调插件；`run_plugin()` 供 CLI 手动触发 |
| `notifier.py` | 渠道分发：`file` 与 `serverchan` |
| `discovery.py` | 扫描 `plugins/*/plugin.py`，加载 `Skill` 子类 |
| `doctor.py` / `catch_up.py` / `native_schedule.py` | 体检 / 补跑 / 原生定时任务 |

## 插件（L3，`family_os/plugins/`）

- 一个插件 = 一个目录 + 一个 `plugin.py` + 一个 `Skill` 子类
- `run(ctx) -> Result` 是唯一执行入口
- `register_cli(app)` 可挂子命令
- 插件之间**不直接 import、不直接调用**；跨插件共享只经两种方式：
  1. **数据库**：`ctx.store`（如家庭事实）
  2. **文件产物**：`data/history/<plugin>/...`（如后续插件读取新闻 digest）

## 数据与隐私

| 内容 | 位置 | 是否进仓库 |
|---|---|---|
| 家庭事实 / 成员 | `data/family.db` | 否（`data/` 与 `*.db` 已忽略） |
| 密钥 | `.env` | 否 |
| 每日产物 | `data/history/`、`data/inbox/` | 否 |
| 新闻源清单 | `family_os/config/sources.yaml` | 是（公开源） |

`sensitivity=secret` 的家庭事实**不会进入任何 LLM 调用**（见 `kernel/privacy.py`）。

## 扩展一个插件

见 [plugin-dev.md](plugin-dev.md)：复制 `examples/hello_plugin`，在 `config.yaml` 注册，
`./uglycat run <name>` 即可运行；不需要改动内核。
