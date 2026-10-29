# 插件开发指南

内核只做四件事：配置、调度、LLM 网关、通知。所有业务都是插件。
新增插件**不需要改动内核**。

## 目录结构

```
family_os/plugins/
└── my_plugin/
    ├── __init__.py        # 可空
    └── plugin.py          # 必须：导出一个 Skill 子类
```

插件发现规则（`family_os/kernel/discovery.py`）：

- 扫描 `config.yaml` 里 `plugins.plugin_dir` 指向的目录
- 只认 `<目录>/plugin.py`；以 `_` 或 `.` 开头的目录跳过
- 每个 `plugin.py` 里**只能有一个**具体 `Skill` 子类
- 单个插件加载失败只记 warning，不影响其他插件

## 最小插件

见 `examples/hello_plugin/plugin.py`。核心只有三步：

```python
from family_os.kernel.skill import Result, Skill

class Hello(Skill):
    name = "hello"
    cost_tier = "free"

    def run(self, ctx):
        return Result(status="ok", data={"msg": "hello"},
                      suggestions=[{"title": "示例建议", "priority": "low",
                                    "detail": "来自示例插件"}])

    def register_cli(self, app):
        import typer
        g = typer.Typer(help="示例插件", name="hello")
        @g.command("run")
        def _run():
            typer.echo("hello")
        app.add_typer(g)
```

启用：

```bash
cp -r examples/hello_plugin family_os/plugins/
./uglycat hello run
```

若要定时执行，在 `config.yaml` 里加实例配置：

```yaml
plugins:
  enabled: [news_digest, hello]
  instances:
    hello:
      enabled: true
      schedule: "0 9 * * 1"     # APScheduler：0=周一 .. 6=周日
      notify_on: [family]
```

## 契约与纪律

| 约束 | 说明 |
|---|---|
| `run()` 只能返回 `Result` | 不要返回 dict/str；`Result` 是唯一出口 |
| 依赖只从 `ctx` 取 | `ctx.config` / `ctx.store` / `ctx.llm_gateway` |
| 不直接 import 其他插件 | 插件之间保持隔离 |
| 单源/单步失败不拖垮整体 | 抓取类插件必须按源隔离并记 warning |
| 密钥只从环境变量读 | 同 `docs/notify.md` 的安全约定 |
| 不新增重量级依赖到核心 | 按需放进 `pyproject.toml` 的 optional extra |

## 推送消息

想往群里/微信推消息，在 `Result.data["_messages"]` 里放 `{群名: markdown}`：

```python
return Result(status="ok", data={"_messages": {"family": "## 标题\n正文"}})
```

通知层会按 `config.yaml` 的 `notify_on` 决定发哪些群（本项目默认 `family`）。

## 测试

在 `tests/plugins/test_<name>.py` 里用假 `Context` 覆盖三类场景：

1. 正常路径（返回 `status="ok"`）
2. 上游失败（返回 `skipped` 或 `error`，且不抛异常）
3. 推送内容渲染（若用了 `_messages`）

```bash
python -m pytest tests/plugins/test_my_plugin.py -q
```
