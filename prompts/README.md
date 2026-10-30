# 自定义提示词目录

把 `select.md` / `stage1.md` 等文件放在这里，内容会作为「用户自定义规则」追加到
对应阶段的提示词，用来持续优化筛选口径与黑话解读。

- 支持的文件名：`dedup.md`、`select.md`、`stage0.md`、`stage1.md`、`stage2.md`、`stage3.md`、`market_impact.md`
- 文件不存在或为空 → 完全使用内置默认行为
- 写法与迭代方法见 [docs/prompts.md](../docs/prompts.md)

示例（`prompts/select.md`）：

```markdown
- 涉及本地生活服务涨价（水电气、公交、停车）的全国性政策优先选中。
- 标题含「试点」且未说明全国推广时间的，最多选 1 条。
```
