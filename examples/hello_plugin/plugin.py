"""最小可用插件示例。

放到 `family_os/plugins/hello/plugin.py` 即可被发现并启用：
    cp -r examples/hello_plugin family_os/plugins/hello
    ./uglycat hello run

约定：一个 plugin.py 里只有一个 Skill 子类；run() 只返回 Result。
"""

from datetime import datetime

from family_os.kernel.skill import Result, Skill


class Hello(Skill):
    """打印当前时间，并把结果写进 Result（不调 LLM、不联网）。"""

    name = "hello"
    cost_tier = "free"

    def run(self, ctx) -> Result:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return Result(
            status="ok",
            data={"_messages": {"family": f"## 👋 Hello\n\n当前时间：{now}"}},
            suggestions=[{
                "title": "示例建议",
                "priority": "low",
                "detail": "这条建议来自 examples/hello_plugin，可删可改。",
            }],
        )

    def register_cli(self, app) -> None:
        import typer

        group = typer.Typer(help="示例插件", name="hello")

        @group.command("run")
        def hello_run():
            """直接执行示例插件。"""
            typer.echo("hello：示例插件运行成功（完整流程请用 uglycat run hello）")

        app.add_typer(group)
