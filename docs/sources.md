# 自定义新闻源

新闻源清单在 `family_os/config/sources.yaml`。**只加公开可访问的源**：不需要登录、
不绕过付费墙、不伪造 Cookie/Token。

> **你自己的源请写进 `family_os/config/sources.local.yaml`**（已被 `.gitignore` 忽略）。
> 它和主清单自动合并：同名条目覆盖字段（可用 `enabled: false` 关掉某个源），
> 新名字条目追加。这样以后 `git pull` 更新主清单时，你的改动不会冲突、也不会被覆盖。

## 一个源长什么样

```yaml
sources:
  - name: 某部委
    category: policy
    type: rss                      # rss | web_list | web_page
    url: https://example.gov.cn/rss.xml
    weight: 0.8                    # 0~1，越大越优先进入精选候选
    enabled: true
```

常见字段：

| 字段 | 说明 |
|---|---|
| `type` | `rss`（标准订阅源）；`web_list`（列表页 HTML，需 `link_pattern`）；`web_page`（单页正文） |
| `link_pattern` | `web_list` 必填，用正则从列表页提取文章链接 |
| `title_include` | 只保留标题包含该词的文章（如「国务院」「印发」），用来压噪 |
| `max_age_days` | 只抓最近 N 天（默认 2~3，避免旧文混入） |
| `weight` | 排序权重，`0.9` 用于最高优先级源（社科院、求是等） |
| `category` | 仅作分组展示，不影响筛选 |

## 加源的推荐流程（可以交给 AI）

1. 先验证源可用：
   ```bash
   curl -s <url> | head -c 800        # RSS 应能看到 <item>；列表页应能看到文章标题
   ```
2. 在 `sources.local.yaml` 追加条目（保持缩进，`enabled: true`）；若文件不存在，新建即可：
   ```yaml
   sources:
     - name: 某部委
       type: rss
       url: https://example.gov.cn/rss.xml
       enabled: true
   ```
3. 跑一次抓取，看这个源是否被命中：
   ```bash
   ./uglycat run news_digest
   # 看输出里的「今日抓取 N 条」是否增加；详情在 data/history/news_digest/<日期>/
   ```
4. 若是 `web_list`，先用浏览器打开列表页，确认链接形如
   `/2026/10/30/123456.shtml`，据此写 `link_pattern`

## 给 AI 的一句话模板

> 帮我给 UglyCat OS 加一个新闻源：`<源名称>`，地址 `<URL>`。
> 先验证它可公开访问（无需登录、无需 Cookie），再按仓库 `docs/sources.md` 的字段
> 写进 `family_os/config/sources.yaml`，然后跑 `./uglycat run news_digest` 确认抓到条数增加。

## 注意事项

- 单源失败会被隔离（不会拖垮整轮），日志里有 warning
- 不要加订阅制媒体（财新等），也不要把正文抓下来转载
- 地方媒体可以做「线索源」，但地方数据/地方政策本身会被筛选门槛剔除
