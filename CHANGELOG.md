# 变更记录

## v0.1.0 — 2026-10-30

首个公开版本：内核 + 新闻解读插件。

### 新增

- **每日新闻解读**：公开源抓取 → 日期/内容去重 → LLM 语义去重 → 国家级精选（≤3 条）
  → 官方措辞解读 → 历史措辞对比 → 普通人一句话建议
- **利率专节**：命中中美货币政策时，额外给出对黄金/股市的方向性影响
- **通知**：本地 `data/inbox/*.md` + Server酱 推送到微信
- **安装**：`install/setup.sh` / `setup.ps1`（Windows/macOS/Linux，用户级、幂等、无需 sudo）
- **CLI**：`doctor`（环境体检，`--json` 供 AI 判定）、`install-schedule`（系统原生定时）、
  `catch-up`（错过推送后补跑）、`news probe`（模型连通性）
- **插件机制**：`Skill` + `Context` 注入，插件只经 ctx 访问内核；示例见 `examples/hello_plugin`
- **隐私**：`sensitivity=secret` 的家庭事实不进入 LLM；日志自动脱敏

### 说明

- 本地优先：默认每天 08:00 跑一次；机器睡眠错过可 `catch-up`
- VM 部署为可选路径，见 `docs/deploy-vm.md`
- 许可证：MIT
