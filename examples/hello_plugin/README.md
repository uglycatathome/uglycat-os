# hello_plugin

最小插件示例，用来验证「发现 → 运行 → 推送」链路是否通畅。

## 试一下

```bash
# 1) 复制到插件目录
cp -r examples/hello_plugin family_os/plugins/hello

# 2) 加进启用列表（config.yaml → plugins.enabled 追加 hello）

# 3) 直接运行（结果会写入 data/inbox/，填了 SendKey 还会推微信）
./uglycat run hello

# 4) 或在 CLI 里用插件自带的子命令
./uglycat hello run
```

## 看点

- `name` 必须唯一，`run()` 只返回 `Result`
- 想推送内容，就放进 `Result.data["_messages"]`
- `register_cli()` 里可以挂任意 Typer 子命令

完整说明见 [docs/plugin-dev.md](../../docs/plugin-dev.md)。
