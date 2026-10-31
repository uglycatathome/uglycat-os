# AGENTS.md — 安装与运行契约（给 AI 代理）

本文件是机器可执行指令，不是介绍文档。**始终按下面顺序执行，用每步"判据"决定是否继续。**

## 支持环境

| 系统 | 最低版本 | 说明 |
|---|---|---|
| Windows | 10 / Server 2019 | PowerShell 5.1+，无需管理员 |
| macOS | 12 | Intel / Apple Silicon 均可 |
| Linux | 任意主流发行版 | 无需 sudo |

Python 必须 >= 3.11。优先使用 `uv`，没有则用 `python -m venv`。

## 安装步骤

1. **体检（不改动系统）**

   ```bash
   ./install/setup.sh --check-only      # macOS / Linux
   .\install\setup.ps1 -CheckOnly       # Windows
   ```

   判据：能输出 Python 版本且 >= 3.11，否则先安装 Python 3.11+ 再重试。

2. **安装依赖**

   ```bash
   ./install/setup.sh                   # macOS / Linux
   .\install\setup.ps1                  # Windows
   ```

   判据：退出码 0，且仓库根目录存在 `.venv/`。
   该脚本会创建启动器 `./uglycat`（Windows 为 `uglycat.cmd`）。

> **启动器**：所有命令都通过仓库根目录的 `./uglycat`（Windows 为 `uglycat.cmd`）执行。
> 它会自动 `cd` 到仓库根目录，因此配置/数据路径始终正确；不要直接 `cd app` 再跑 `run.py`。

3. **配置 LLM Key（可选再加 Server酱）**

   - 若 `.env` 不存在，脚本已从 `.env.example` 复制一份。
   - **向用户索要 Key，只写入 `.env`，不要在对话/日志中回显。**
   - LLM 至少填写一个：`ZHIPU_API_KEY` / `SILICONFLOW_API_KEY` / `DEEPSEEK_API_KEY`。
   - 想在微信收推送：再填 `SERVERCHAN_SENDKEY`（来自 https://sct.ftqq.com）。
     通知渠道**已默认开启**，填了即生效；不填只写本地 `data/inbox/*.md`。

4. **体检（判据来源）**

   ```bash
   ./uglycat doctor --json
   ```

   判据：JSON 里 `"ok": true`。若为 false，按 `checks` 中 `level=error`
   的条目逐项修复后重跑，**不要跳过**。

5. **冒烟测试**

   ```bash
   ./uglycat news probe        # 逐个模型测 LLM 连通性
   ```

   判据：至少一个模型返回 `✅`。若全部 `❌`，回到第 3 步检查 Key。

   可选：完整跑一次（会真实抓取 + 调 LLM + 推送，约 1~3 分钟）

   ```bash
   ./uglycat run news_digest --notify
   ```

   判据：`status=ok`（有新闻）或 `status=skipped` + `no_news`（当日无合格新闻，属正常）。
   若填了 `SERVERCHAN_SENDKEY`，微信应收到一条推送；填写了但没收到，看控制台是否有
   `SendKey 未配置`／`code != 0` 告警。

6. **可选：每天自动推送**

   ```bash
   ./uglycat install-schedule --time 08:00
   ```

   判据：输出包含"已写入/已执行"。机器睡眠错过时间后，执行 `./uglycat catch-up` 补跑当天。

## 后续可按用户要求做的两件事

### A. 扩展新闻源

用户说「再加一个新闻源 / 我想看某个部门的发布」时：

1. 验证目标地址可公开访问（无需登录、无需 Cookie、非订阅制）：
   `curl -s <url> | head -c 800`，RSS 应含 `<item>`，列表页应能看到文章标题
2. 把条目写进 `family_os/config/sources.local.yaml`（**不要改主清单 `sources.yaml`**：
   主清单会被上游更新覆盖/冲突；`sources.local.yaml` 已 gitignore 且自动合并）
   （`web_list` 必须给 `link_pattern`；地方源可设 `title_include` 压噪）
3. 跑 `./uglycat run news_digest`，看「今日抓取 N 条」是否增加
4. 若某源连续失败，先置 `enabled: false`，不要删掉其他源

### B. 优化筛选与解读口径（提示词）

用户说「这条不该推 / 该推的漏了 / 解读太泛」时：

1. 按 `docs/prompts.md`，把问题写成**可判定的规则**，写进 `prompts/<阶段>.md`
   （`select.md` 管精选，`stage1.md` 管黑话解读，`stage3.md` 管一句话建议）
2. 一次只改一条规则，改完立刻 `./uglycat run news_digest` 验证
3. 把这次的问题沉淀成回归测试（见 `CONTRIBUTING.md`），避免退化
4. 不要直接改 `family_os/plugins/news_digest/analyzer.py` 里的内置提示词——
   代码升级会覆盖，且内置规则承载了硬门槛（活动/栏目/非经济主题/地方数据）

## 禁区

- 不要修改 `family_os/config/sources.yaml`（用 `sources.local.yaml`），也不要抓订阅制媒体全文
- 不要把 `.env` 内容写入提交、日志或对话
- 不要用 `sudo` 安装；本项目全部为用户级安装
- 不要跳过 `doctor`；它是唯一的完成判据

## 故障处理

| 现象 | 处理 |
|---|---|
| `python` 版本过低 | 安装 3.11+ 后重跑 `--check-only` |
| 依赖安装失败（网络） | 设置 pip 镜像源后重试；或改用 `uv` |
| `doctor` 报 `llm_key` | 检查 `.env` 变量名与值，确认无空格/引号 |
| `doctor` 报 `sources` 失败 | 网络受限，属正常；不阻塞安装，可后续加 `--network` 复测 |
| 当日无新闻 | 属正常（宁缺毋滥），不是故障 |
