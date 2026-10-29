# 让 AI 帮你装（推荐）

把仓库链接发给任意编码 AI（Trae / Cursor / Claude Code / Codex），粘贴下面这段：

```
请按仓库根目录的 AGENTS.md 安装 UglyCat OS。

1. 先运行 install/setup.* --check-only（Windows 用 -CheckOnly）
2. 再运行安装脚本，等待退出码 0
3. 我的 LLM Key 是：<在这里粘贴你的 Key>
   请写入 .env 的 ZHIPU_API_KEY，不要回显到对话或日志
4. 运行 ./uglycat doctor --json，直到 "ok": true
5. 运行 ./uglycat news probe，至少一个模型显示 ✅
6. 把最终 doctor 的输出和 probe 的结果贴给我
```

## AI 会经历的检查点

| 步骤 | 判据 | 失败时 |
|---|---|---|
| `setup --check-only` | 打印 Python >= 3.11 | 先装 Python 再重试 |
| `setup` | 退出码 0，存在 `.venv/` | 网络问题换 pip 镜像 |
| 写 `.env` | 文件存在且 Key 非空 | 检查是否有多余空格/引号 |
| `doctor --json` | `"ok": true` | 按 `level=error` 项逐条修复 |
| `news probe` | 至少一个 ✅ | Key 错误或模型不可用 |

## 常见问题

- **Windows 下 `.\install\setup.ps1` 被执行策略拦截**
  用 `powershell -ExecutionPolicy Bypass -File .\install\setup.ps1`
- **公司/学校网络装依赖失败**
  先设置 pip 镜像，或安装 `uv` 后重跑（`uv` 自带更稳的重试）
- **`doctor` 报 `sources` 失败**
  只是外网探测未开或受限，不影响安装；加 `--network` 可复测
- **当日 `status=skipped` + `no_news`**
  当天确实没有够格的全国级新闻，是设计行为，不是故障
