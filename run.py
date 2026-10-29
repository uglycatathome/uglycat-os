"""CLI entry point for Family AI OS.

Usage:
    family --help

Ties together all kernel services and provides a unified command-line
interface via Typer with subcommand groups for schedule, inbox, review,
cost, fact, and ingest operations.
"""

import sys
from pathlib import Path
from typing import Optional

import typer

# Windows GBK terminal can't render emoji — switch to UTF-8.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

app = typer.Typer(
    name="family",
    help="Family AI OS — 家庭组织操作系统",
)

# 日志脱敏：带密钥的 URL（Server酱 SendKey / 企业微信 webhook key）不得进入日志。
# 见 kernel/logging_utils.py —— httpx 异常消息里含完整 URL，失败告警会落 journald。
try:
    from family_os.kernel.logging_utils import install_redaction

    install_redaction()
except Exception:  # noqa: BLE001 — 脱敏失败不应阻断 CLI 启动
    pass

# Sub-command groups
schedule_app = typer.Typer(help="Manage scheduled jobs")
inbox_app = typer.Typer(help="Read inbox messages")
review_app = typer.Typer(help="Review pending decisions")
cost_app = typer.Typer(help="Cost reporting")
fact_app = typer.Typer(help="Manage facts")
ingest_app = typer.Typer(help="Ingest data")

app.add_typer(schedule_app, name="schedule")
app.add_typer(inbox_app, name="inbox")
app.add_typer(review_app, name="review")
app.add_typer(cost_app, name="cost")
app.add_typer(fact_app, name="fact")
app.add_typer(ingest_app, name="ingest")


# ── Register plugin CLI commands ─────────────────────────────────────────────

def _register_plugin_cli() -> None:
    """Discover plugins and register their CLI commands onto the root app."""
    try:
        from family_os.kernel.discovery import discover_plugins
        from family_os.kernel.config import Config
        from pathlib import Path

        config_path = Path("family_os/config/config.yaml")
        if not config_path.exists():
            return
        config = Config.load(config_path)
        plugins = discover_plugins(config.plugins.plugin_dir)
        for plugin_cls in plugins:
            try:
                instance = plugin_cls()
                instance.register_cli(app)
            except Exception:
                pass  # Plugin might fail without network; register CLI anyway
    except Exception:
        pass  # Non-blocking: CLI works even if discovery fails

_register_plugin_cli()

# ── Web Portal CLI（用户管理/指数回填，不依赖插件网络）────────────────
try:
    from family_os.web.cli import register_web_cli
    register_web_cli(app)
except Exception:
    pass  # Non-blocking：缺失依赖时 CLI 仍可用


# ── Bootstrap helpers (private) ──────────────────────────────────────────────


def _get_config():
    """Load the application Config from the YAML file."""
    from family_os.kernel.config import Config

    config_path = Path("family_os/config/config.yaml")
    if not config_path.exists():
        typer.echo("Error: config.yaml not found. Run from app/ directory.",
                   err=True)
        raise typer.Exit(1)
    return Config.load(config_path)


def _get_store():
    """Create and initialize the Store (SQLite database)."""
    from family_os.kernel.store import Store

    config = _get_config()
    db_path = Path(config.app.data_dir) / "family.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    store.initialize()
    return store


def _get_context():
    """Build a Context with all kernel services wired together."""
    from family_os.kernel.context import Context
    from family_os.kernel.llm_gateway import LLMGateway

    config = _get_config()
    store = _get_store()
    gateway = LLMGateway(store=store, config=config)
    return Context(store=store, config=config, llm_gateway=gateway)


def _get_scheduler():
    """Discover plugins and return a configured Scheduler."""
    from family_os.kernel.scheduler import Scheduler
    from family_os.kernel.discovery import discover_plugins

    config = _get_config()
    plugins = discover_plugins(config.plugins.plugin_dir)
    return Scheduler(config, plugins)


# ── family run ────────────────────────────────────────────────────────────────


@app.command()
def run(
    plugin_name: str,
    notify: bool = typer.Option(
        False, "--notify",
        help="Send WeChat to all configured notify_on groups",
    ),
    notify_family: bool = typer.Option(
        False, "--notify-family",
        help="Send WeChat to family group only",
    ),
    notify_invest: bool = typer.Option(
        False, "--notify-invest",
        help="Send WeChat to invest group only",
    ),
):
    """Run a plugin by name. Use --notify-* for end-to-end WeChat sending."""
    ctx = _get_context()
    scheduler = _get_scheduler()
    result = scheduler.run_plugin(plugin_name, ctx)
    typer.echo(f"Result: {result.model_dump_json(indent=2)}")

    if notify or notify_family or notify_invest:
        from family_os.kernel.notifier import Notifier
        notifier = Notifier(ctx.config)

        if notify:
            notifier.send(result, plugin_name)
        else:
            groups: list[str] = []
            if notify_family:
                groups.append("family")
            if notify_invest:
                groups.append("invest")
            if groups:
                notifier.send(result, plugin_name, notify_groups=groups)

        typer.echo("Notification sent.")


# ── family doctor / catch-up / install-schedule ───────────────────────────────


@app.command()
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="机器可读输出（AI 安装代理用这个判断是否装好）"),
    network: bool = typer.Option(
        False, "--network", help="额外探测新闻源可达性（默认不联网）"),
):
    """体检部署环境：Python/依赖/配置/数据目录/LLM Key/调度/通知。"""
    from family_os.kernel.doctor import dumps, format_checks, run_doctor

    result = run_doctor(Path("family_os/config/config.yaml"), network=network)
    typer.echo(dumps(result) if json_output else format_checks(result))
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("catch-up")
def catch_up_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="只判断是否该补跑，不执行"),
):
    """启动时补跑当日漏掉的新闻任务（机器睡眠/关机错过 08:00 时使用）。"""
    from family_os.kernel.catch_up import maybe_catch_up

    ctx = _get_context()
    result = maybe_catch_up(ctx, dry_run=dry_run)
    if result.get("ran"):
        typer.echo(f"已补跑：{result.get('reason')}（status={result.get('status')}）")
    else:
        typer.echo(f"未补跑：{result.get('reason')}")


@app.command("install-schedule")
def install_schedule_cmd(
    time_str: str = typer.Option("08:00", "--time", help="每天触发时间 HH:MM"),
    command: str = typer.Option(
        "run news_digest --notify", "--command", help="要执行的 CLI 子命令"),
    uninstall: bool = typer.Option(False, "--uninstall", help="移除已注册的定时任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印将做什么"),
):
    """注册操作系统原生定时任务（Windows 任务计划 / macOS launchd / Linux cron）。"""
    from family_os.kernel.native_schedule import (
        apply_plan, build_plan, detect_platform,
    )

    platform = detect_platform()
    plan = build_plan(platform, time_str, repo_root=Path.cwd(), command=command)
    for line in apply_plan(plan, dry_run=dry_run, uninstall=uninstall):
        typer.echo(line)


# ── family schedule ───────────────────────────────────────────────────────────


@schedule_app.command("list")
def schedule_list():
    """List all scheduled jobs."""
    scheduler = _get_scheduler()
    jobs = scheduler.list_jobs()
    if not jobs:
        typer.echo("No scheduled jobs.")
        return
    for job in jobs:
        typer.echo(
            f"{job['plugin']:20s} "
            f"schedule={job['schedule']}  "
            f"cost_tier={job['cost_tier']}"
        )


@schedule_app.command("daemon")
def schedule_daemon():
    """Start the scheduler daemon (background)."""
    import signal
    import threading

    scheduler = _get_scheduler()
    ctx = _get_context()
    scheduler.start_daemon(ctx)
    typer.echo("Scheduler daemon started. Press Ctrl+C to stop.")

    # BackgroundScheduler uses daemon threads — we must block the main
    # thread or the process exits immediately.
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda sig, frame: stop.set())
    signal.signal(signal.SIGTERM, lambda sig, frame: stop.set())
    stop.wait()
    typer.echo("Scheduler daemon stopped.")


# ── family inbox ──────────────────────────────────────────────────────────────


@inbox_app.command("list")
def inbox_list():
    """List inbox messages (data/inbox/*.md)."""
    inbox_dir = Path("data/inbox")
    if not inbox_dir.is_dir():
        typer.echo("No inbox messages yet. Run a plugin first.")
        return
    files = sorted(inbox_dir.glob("*.md"))
    if not files:
        typer.echo("No inbox messages.")
        return
    for i, f in enumerate(files, 1):
        typer.echo(f"  {i:3d}. {f.name}")


@inbox_app.command("read")
def inbox_read(index: int):
    """Read an inbox message by list index."""
    inbox_dir = Path("data/inbox")
    if not inbox_dir.is_dir():
        typer.echo("No inbox directory found.")
        raise typer.Exit(1)
    files = sorted(inbox_dir.glob("*.md"))
    if not files or index < 1 or index > len(files):
        typer.echo("Invalid index.")
        raise typer.Exit(1)
    content = files[index - 1].read_text(encoding="utf-8")
    typer.echo(content)


# ── family review ─────────────────────────────────────────────────────────────


@review_app.command("pending")
def review_pending():
    """List all pending decisions."""
    from family_os.kernel.approval import ApprovalStateMachine

    store = _get_store()
    config = _get_config()
    asm = ApprovalStateMachine(store, config)
    pending = asm.list_pending()
    if not pending:
        typer.echo("No pending decisions.")
        return
    for d in pending:
        typer.echo(f"  {d['id']:5d}  {d['title']}")


@review_app.command("approve")
def review_approve(decision_id: int):
    """Approve a pending decision."""
    from family_os.kernel.approval import ApprovalStateMachine

    store = _get_store()
    config = _get_config()
    asm = ApprovalStateMachine(store, config)
    asm.approve(decision_id, "cli-user")
    typer.echo(f"Decision {decision_id} approved.")


@review_app.command("reject")
def review_reject(
    decision_id: int,
    note: Optional[str] = typer.Option(None, "--note",
                                        help="Rejection reason"),
):
    """Reject a pending decision with an optional note."""
    from family_os.kernel.approval import ApprovalStateMachine

    store = _get_store()
    config = _get_config()
    asm = ApprovalStateMachine(store, config)
    reason = note or "Rejected via CLI"
    asm.reject(decision_id, reason, "cli-user")
    typer.echo(f"Decision {decision_id} rejected.")


# ── family cost ───────────────────────────────────────────────────────────────


@cost_app.command("report")
def cost_report():
    """Show monthly LLM cost vs budget."""
    ctx = _get_context()
    cost = ctx.llm_gateway.get_monthly_cost()
    budget = ctx.config.llm.monthly_budget_usd
    # DeepSeek bills in CNY
    typer.echo(f"Monthly LLM cost: CNY {cost:.4f} / ${budget:.2f} budget")

    # Show balance if available
    balance = ctx.llm_gateway.get_balance()
    if "balance" in balance:
        typer.echo(f"DeepSeek balance: {balance['balance']:.2f} {balance['currency']}")

    if cost > budget * 7:  # rough CNY→USD conversion for budget check
        typer.echo("OVER BUDGET!")
    elif cost > budget * 7 * 0.8:
        typer.echo("Approaching budget limit.")


@cost_app.command("balance")
def cost_balance():
    """Query DeepSeek account balance."""
    ctx = _get_context()
    result = ctx.llm_gateway.get_balance()
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        return
    typer.echo(f"DeepSeek balance: {result['balance']:.2f} {result['currency']}")


# ── family fact ───────────────────────────────────────────────────────────────


@fact_app.command("add")
def fact_add(
    fact_type: str = typer.Option(..., "--type", help="Fact type"),
    key: str = typer.Option(..., "--key", help="Fact key"),
    value: str = typer.Option(..., "--value", help="Fact value"),
    member: str = typer.Option("全家", "--member",
                                help="Family member"),
):
    """Add a structured fact."""
    from family_os.kernel.ingestion import Normalizer

    store = _get_store()
    normalizer = Normalizer(store)
    fact_id = normalizer.ingest_fact({
        "type": fact_type,
        "key": key,
        "value": value,
        "member": member,
    })
    typer.echo(f"Fact added with id={fact_id}")


@fact_app.command("list")
def fact_list(
    fact_type: str = typer.Option(None, "--type", help="Filter by type"),
    member: str = typer.Option(None, "--member", help="Filter by member"),
):
    """List facts, optionally filtered by type or member."""
    store = _get_store()
    kwargs = {}
    if fact_type:
        kwargs["type"] = fact_type
    if member:
        kwargs["member"] = member
    facts = store.query_facts(**kwargs) if kwargs else store.query_facts()
    if not facts:
        typer.echo("No facts found.")
        return
    for f in facts:
        typer.echo(f"{f['id']:4d}  [{f['type']:12s}]  {f['member']:6s}  {f['key']:14s} = {f['value']}")


@fact_app.command("note")
def fact_note(text: str):
    """Parse a natural-language note and ingest as facts."""
    import json

    from family_os.kernel.ingestion import Normalizer

    ctx = _get_context()
    prompt = (
        "Parse the following family note into structured facts. "
        "Return a JSON list of fact objects, each with type, key, value, "
        "and optionally member.\n\n"
        f"Note: {text}"
    )
    response = ctx.llm_gateway.ask(prompt, plugin_name="cli-note")

    typer.echo(f"Parsed facts:\n{response}")
    confirmed = typer.confirm("Ingest these facts?")
    if not confirmed:
        typer.echo("Aborted.")
        raise typer.Exit()

    store = _get_store()
    normalizer = Normalizer(store)
    try:
        # Strip markdown code blocks if present
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("\n", 1)[0]
        facts = json.loads(cleaned)
        for fact in facts:
            normalizer.ingest_fact(fact)
        typer.echo(f"Ingested {len(facts)} fact(s).")
    except json.JSONDecodeError:
        typer.echo("Failed to parse LLM response as JSON.", err=True)
        raise typer.Exit(1)


# ── family ingest ─────────────────────────────────────────────────────────────


@ingest_app.command("url")
def ingest_url(url: str):
    """Fetch a URL and ingest it as a document."""
    import httpx

    from family_os.kernel.ingestion import Normalizer

    typer.echo(f"Fetching {url} ...")
    try:
        response = httpx.get(url, timeout=30.0)
        response.raise_for_status()
    except Exception as exc:
        typer.echo(f"Error fetching URL: {exc}", err=True)
        raise typer.Exit(1)

    store = _get_store()
    normalizer = Normalizer(store)
    doc_id = normalizer.ingest_document(url, "web_page", tags=["imported"])
    typer.echo(f"Document ingested with id={doc_id}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
