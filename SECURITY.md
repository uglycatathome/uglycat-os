# 安全策略

## 报告漏洞

**不要开公开 Issue 贴密钥或可复现的攻击细节。**
请使用 GitHub 的私下报告入口：仓库 → Security → Advisories → Report a vulnerability。

我们关注的类型：

- 密钥/凭据泄露（SendKey、API Key、webhook URL）
- 日志或推送内容泄露本地敏感信息
- 抓取逻辑越过公开访问边界（伪造登录态、绕过付费墙等）
- 插件加载/配置解析导致的任意代码执行

## 凭据处理约定

| 项 | 约定 |
|---|---|
| LLM Key | 只放 `.env`（`ZHIPU_API_KEY` / `SILICONFLOW_API_KEY` / `DEEPSEEK_API_KEY`） |
| Server酱 SendKey | 只放 `.env`（`SERVERCHAN_SENDKEY`） |
| 日志 | 带密钥 URL 一律经 `kernel/logging_utils.redact_url` 掩码 |
| 测试 | **禁止**使用真实密钥；一律用 `SCT_TEST_KEY_DUMMY` 这类假值 |
| 仓库 | `.env`、`data/`、`*.db` 永久忽略 |

## 数据边界

- 所有数据留在本地 `data/`，项目不采集遥测
- `sensitivity=secret` 的家庭事实不进入任何 LLM 调用
- 不抓订阅制媒体全文（`sources.yaml` 已剔除）
