# 贡献指南

欢迎 Issue 与 PR。这个项目刻意保持「小内核 + 插件化」，请先读
[架构](docs/architecture.md) 与 [插件开发](docs/plugin-dev.md)。

## 本地开发

```bash
# 任选其一
./install/setup.sh           # macOS / Linux
.\install\setup.ps1          # Windows

# 跑测试（必须用项目 venv）
.venv/bin/python -m pytest -q              # macOS / Linux
.venv\Scripts\python.exe -m pytest -q      # Windows
```

- 需联网的用例已标 `network`，默认跳过；要跑请加 `-m network`
- 新增功能请同时补测试；修 bug 请先补一条能复现的用例

## PR 约定

- 一个 PR 只做一件事；标题用 `feat:` / `fix:` / `docs:` / `chore:` 前缀
- 不提交 `.env`、`data/`、任何真实密钥或个人信息
- 不改动 `family_os/config/sources.yaml` 里不存在的站点；新增源必须可公开访问且无需登录
- 不引入重量级依赖到核心：按需放到 `pyproject.toml` 的 optional extra
- 改动内核接口（`Skill` / `Context` / `Result`）需要在 PR 描述里说明影响面

## 新增插件

1. 复制 `examples/hello_plugin` 到 `family_os/plugins/<name>/`
2. 实现 `run(ctx) -> Result` 与 `register_cli(app)`
3. 在 `config.yaml` 的 `plugins.enabled` 与 `instances` 里注册
4. 补 `tests/plugins/test_<name>.py`：正常路径 + 上游失败 + 推送渲染

## 隐私红线

- 家庭/个人数据只允许通过 `ctx.store` 读写，禁止写进插件目录或提交
- `sensitivity=secret` 的数据不得进入任何 LLM prompt（见 `kernel/privacy.py`）
- 日志里不要打印密钥；带密钥的 URL 走 `redact_url`
