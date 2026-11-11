"""Notification service for Family AI OS.

The ``Notifier`` dispatches ``Result`` objects to configured channels.
Currently supported channel types:

- **file** -- writes a Markdown report to ``data/inbox/``.
- **email** -- stub that logs the notification (future implementation).
- **wechat** -- sends 企业微信 group-bot Markdown via webhook.
- **serverchan** -- 把家庭群消息镜像到个人微信（Server酱 / ServerChan Turbo，
  2026-10-18 新增）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from family_os.kernel import outbox
from family_os.kernel.logging_utils import redact_text
from family_os.kernel.skill import Result

if __name__ == "__main__":  # noqa: pragma: no cover
    __package__ = "family_os.kernel"

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────────────

class Notifier:
    """Dispatch skill results to configured notification channels.

    Parameters
    ----------
    config : Config
        Application configuration.  ``config.app.data_dir`` determines the
        base directory under which ``inbox/`` is created.
    """

    def __init__(self, config) -> None:
        self._config = config

    # ── Public methods ────────────────────────────────────────────────────────

    def send(self, result: Result, plugin_name: str,
             notify_groups: list[str] | None = None) -> list[Path]:
        """Dispatch *result* to every enabled notification channel.

        Parameters
        ----------
        result : Result
            The result to notify about.
        plugin_name : str
            Name of the plugin that produced the result.
        notify_groups : list[str] | None
            Override the plugin's ``notify_on`` config.  When set, only
            the named webhooks receive the message.  ``None`` (default)
            uses the plugin's configured ``notify_on`` list.

        Returns
        -------
        list[Path]
            Paths to any files that were created.
        """
        created: list[Path] = []
        for channel in self._config.notifier.channels:
            if not channel.enabled:
                continue

            if channel.type == "file":
                path = self.send_to_inbox(result, plugin_name)
                created.append(path)
            elif channel.type == "email":
                logger.info(
                    "[email] would send notification for plugin=%s status=%s",
                    plugin_name,
                    result.status,
                )
            elif channel.type == "wechat":
                self._send_wechat(result, plugin_name,
                                  notify_groups=notify_groups)
            elif channel.type == "serverchan":
                self._send_serverchan(result, plugin_name, channel,
                                      notify_groups=notify_groups)
            else:
                logger.warning("Unknown notification channel type: %s", channel.type)

        return created

    def send_to_inbox(self, result: Result, plugin_name: str) -> Path:
        """Write a Markdown report for *result* to ``data/inbox/``.

        The output file is named ``<YYYYMMDD_HHMMSS>_<plugin_name>.md``.

        Parameters
        ----------
        result : Result
            The result to write.
        plugin_name : str
            Name of the plugin that produced the result.

        Returns
        -------
        Path
            The absolute path of the created file.
        """
        inbox = self._inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{plugin_name}.md"
        path = inbox / filename

        content = self._format_as_markdown(result, plugin_name)
        path.write_text(content, encoding="utf-8")

        logger.info("Wrote notification to %s", path)
        return path

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _inbox_dir(self) -> Path:
        """Return the ``data/inbox/`` directory path."""
        data_dir = Path(self._config.app.data_dir)
        return data_dir / "inbox"

    @staticmethod
    def _format_as_markdown(result: Result, plugin_name: str) -> str:
        """Format a ``Result`` as a structured Markdown document.

        The report includes:

        - A header with the plugin name and ISO timestamp.
        - The result status.
        - Suggestions (if any), each prefixed with a priority icon.
        - Action items (if any), rendered as checklists with deadlines.
        - A JSON code block with the full ``data`` dict.
        """
        lines: list[str] = []
        now_str = datetime.now().isoformat(timespec="seconds")

        # ── Header ────────────────────────────────────────────────────────
        lines.append(f"# Notification: {plugin_name}")
        lines.append("")
        lines.append(f"- **Plugin:** {plugin_name}")
        lines.append(f"- **Timestamp:** {now_str}")
        lines.append(f"- **Status:** {result.status}")
        lines.append("")

        # ── Suggestions ───────────────────────────────────────────────────
        if result.suggestions:
            lines.append("## Suggestions")
            lines.append("")
            for s in result.suggestions:
                priority = s.get("priority", "medium")
                icon = _PRIORITY_ICONS.get(priority, "🟡")
                title = s.get("title", "")
                detail = s.get("detail", "")
                lines.append(f"- {icon} **{title}**")
                if detail:
                    lines.append(f"  - {detail}")
            lines.append("")

        # ── Todos ─────────────────────────────────────────────────────────
        if result.todos:
            lines.append("## Todos")
            lines.append("")
            for t in result.todos:
                action = t.get("action", "")
                deadline = t.get("deadline", "")
                deadline_suffix = f" (截止: {deadline})" if deadline else ""
                lines.append(f"- [ ] {action}{deadline_suffix}")
            lines.append("")

        # ── Data ──────────────────────────────────────────────────────────
        if result.data:
            lines.append("## Data")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(result.data, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")

        return "\n".join(lines)

    # ── WeChat channel ─────────────────────────────────────────────────────

    def _post_webhook(self, url: str, content: str) -> bool:
        """Post a single Markdown message to a 企业微信 webhook.

        Returns ``True`` on success, ``False`` on failure.
        Network errors are logged but never raised.
        """
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        try:
            resp = httpx.post(url, json=payload, timeout=10.0)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("[wechat] webhook post failed: %s", exc)
            return False

    def _resolve_webhook_url(self, webhook_name: str) -> str | None:
        """Resolve the configured URL for a named webhook.

        Looks up *webhook_name* in ``config.webhooks`` and reads the URL from
        ``config.secrets`` using the webhook's ``env_key`` (lower-cased to
        match the pydantic-settings field name).  Returns ``None`` — with a
        logged warning — when the webhook is unknown or its URL is not
        configured.
        """
        wh_cfg = self._config.webhooks.get(webhook_name)
        if wh_cfg is None:
            logger.warning(
                "[wechat] unknown webhook '%s' — check config.yaml webhooks",
                webhook_name,
            )
            return None
        url = getattr(self._config.secrets, wh_cfg.env_key.lower(), None)
        if not url:
            logger.warning(
                "[wechat] webhook '%s' URL not configured (env_key=%s)",
                webhook_name, wh_cfg.env_key,
            )
            return None
        return url

    def _effective_groups(self, plugin_name: str,
                          notify_groups: list[str] | None) -> list[str]:
        """本次要发送的群（webhook 名）列表；无配置时返回空列表。

        ``notify_groups`` 显式给定时以它为准（CLI `--notify-family/--notify-invest`
        路径），否则读插件的 ``notify_on``。wechat 与 serverchan 渠道共用同一口径。
        """
        if notify_groups is not None:
            return list(notify_groups)
        instance_cfg = self._config.plugin_instances.get(plugin_name)
        if not instance_cfg or not instance_cfg.notify_on:
            return []
        return list(instance_cfg.notify_on)

    def _group_messages(self, result: Result, plugin_name: str,
                        group: str) -> list[str]:
        """该群本次要发的 Markdown 消息列表（单来源，供 wechat/serverchan 共用）。

        优先取 ``result.data["_messages"][group]``；
        插件未提供时退回通用主消息构造（``_build_primary_message``）。
        """
        messages_dict = (result.data or {}).get("_messages", {})
        if not isinstance(messages_dict, dict):
            messages_dict = {}

        content = messages_dict.get(group)
        # _messages[group] 优先；插件未提供则回退通用构造。
        if content:
            return [content]
        return [self._build_primary_message(result, plugin_name)]

    def _send_wechat(self, result: Result, plugin_name: str,
                     notify_groups: list[str] | None = None) -> None:
        """Send *result* to 企业微信 group bots via configured webhooks.

        Routing is fully config-driven: the plugin's ``notify_on`` list (from
        ``config.plugin_instances``) names one or more webhooks.  Each named
        webhook receives a single Markdown message; content is taken from
        ``result.data["_messages"][webhook_name]`` when the plugin provides it
        （插件可用 ``_messages`` 为不同群提供不同内容），否则
        the generic primary message builder is used as a fallback.

        Parameters
        ----------
        notify_groups : list[str] | None
            When set, only the named webhooks are sent to (overriding the
            plugin's ``notify_on`` config).  ``None`` uses config as-is.
        """
        groups = self._effective_groups(plugin_name, notify_groups)
        if not groups:
            logger.info(
                "[wechat] plugin '%s' has no notify_on — skipping",
                plugin_name,
            )
            return

        # 显式 skipped（如 LLM 欠费/网络不可用）：静默，不推送
        # 空内容标题（"❌ 插件名"）；具体原因写入 status.json 供本地排查。
        if result.status == "skipped":
            logger.info(
                "[wechat] plugin '%s' status=skipped → 静默跳过（不推空内容，原因见健康周报）",
                plugin_name,
            )
            return

        for webhook_name in groups:
            url = self._resolve_webhook_url(webhook_name)
            if url is None:
                continue

            message_list = self._group_messages(result, plugin_name, webhook_name)

            sent = 0
            for i, msg in enumerate(message_list):
                if self._post_webhook(url, msg):
                    sent += 1
                if i < len(message_list) - 1:
                    time.sleep(0.3)

            if sent > 0:
                logger.info(
                    "[wechat] sent %d/%d to '%s' for plugin=%s status=%s",
                    sent, len(message_list), webhook_name,
                    plugin_name, result.status,
                )

    # ── ServerChan channel（家庭群消息镜像到个人微信，2026-10-18）──────────

    def _send_serverchan(self, result: Result, plugin_name: str, channel,
                         notify_groups: list[str] | None = None) -> None:
        """把发往家庭群的消息镜像到个人微信（Server酱 / ServerChan Turbo）。

        内容与 wechat 渠道同源（``_group_messages``）——群里发什么，个人微信就推什么。
        只镜像 ``channel.mirror_groups`` 列出的群（默认 family）；``skipped`` 静默
        （与企业微信一致）。

        2026-11-11 加固：每次推送前先自动补推 outbox 里「确定未送达」的历史消息；
        本次推送失败则落 outbox + 发企业微信可见告警。任何失败都不抛出、不影响
        其它渠道。
        """
        sendkey = getattr(self._config.secrets, channel.sendkey_env.lower(), None)
        if not sendkey:
            logger.warning(
                "[serverchan] SendKey 未配置（env_key=%s）→ 跳过镜像",
                channel.sendkey_env,
            )
            return

        groups = [g for g in self._effective_groups(plugin_name, notify_groups)
                  if g in set(channel.mirror_groups)]
        if not groups:
            logger.info(
                "[serverchan] plugin '%s' 本次无镜像群（mirror_groups=%s）→ 跳过",
                plugin_name, channel.mirror_groups,
            )
            return

        if result.status == "skipped":
            logger.info(
                "[serverchan] plugin '%s' status=skipped → 静默跳过（与企业微信群一致）",
                plugin_name,
            )
            return

        # 先补旧账：只补「确定未送达」的条目，uncertain 的一律不碰（防重复推送）。
        self.flush_serverchan_outbox(channel)

        for group in groups:
            for msg in self._group_messages(result, plugin_name, group):
                self._post_serverchan(sendkey, msg, channel,
                                      plugin_name=plugin_name, group=group)

    # ── Server酱：超时 / 分类重试 / outbox / 告警（2026-11-11）─────────────

    def _sc_channel(self):
        """取当前启用的 serverchan 渠道配置（供 CLI 手动补推用）。"""
        for ch in self._config.notifier.channels:
            if ch.type == "serverchan" and ch.enabled:
                return ch
        return None

    @staticmethod
    def _is_connect_phase_error(exc: httpx.HTTPError) -> bool:
        """请求**尚未到达服务端**的失败才可安全重试。

        - ConnectTimeout / ConnectError / PoolTimeout：连接或连接池阶段，未发出请求。
        - ReadTimeout / WriteTimeout / 其它：服务端可能已收单，**绝不重试**。
        - 特例：TLS 握手超时（``_ssl.c:983: The handshake operation timed out``）
          有时被 httpcore 包成 ReadTimeout，但握手发生在 POST body 之前，重试安全；
          今早 08:04 的真实事故正是这一型。
        """
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError,
                            httpx.PoolTimeout)):
            return True
        if isinstance(exc, httpx.TimeoutException):
            msg = str(exc).lower()
            if "handshake operation timed out" in msg or "_ssl.c" in msg:
                return True
        return False

    def _sc_request(self, sendkey: str, title: str, desp: str,
                    channel) -> tuple[bool, bool, str]:
        """发一次 Server酱请求（含连接阶段重试）。

        返回 ``(sent, uncertain, reason)``：

        - ``uncertain=True``：请求可能已被服务端接收（读超时 / 非 200 / 响应异常），
          调用方**不得重试或自动补推**，否则会重复推送。
        - ``uncertain=False``：确定未送达（连接阶段失败、或服务端明确 ``code!=0``），
          可以安全补推。

        超时由 ``channel.timeout_s`` 控制（默认 25s，原 10s 太紧）；重试次数由
        ``channel.connect_retries`` 控制，且只对连接阶段生效。
        """
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        payload = {"title": title, "desp": desp}
        timeout = float(getattr(channel, "timeout_s", 25.0))
        attempts = 1 + max(0, int(getattr(channel, "connect_retries", 1)))
        delay = max(0.0, float(getattr(channel, "retry_delay_s", 1.0)))

        for i in range(attempts):
            try:
                resp = httpx.post(url, data=payload, timeout=timeout)
            except httpx.HTTPError as exc:
                reason = redact_text(f"{type(exc).__name__}: {exc}")
                connect_phase = self._is_connect_phase_error(exc)
                if connect_phase and i < attempts - 1:
                    logger.warning(
                        "[serverchan] 连接阶段失败（%s），%.1fs 后重试 %d/%d",
                        reason, delay, i + 1, attempts - 1)
                    if delay:
                        time.sleep(delay)
                    continue
                return False, not connect_phase, reason

            # ── 从这里起请求已被服务端接收：一律不重试 ──────────────────
            if resp.status_code != 200:
                return False, True, f"HTTP {resp.status_code}"
            try:
                body = resp.json()
            except ValueError:
                return False, True, "响应非 JSON"
            if body.get("code") != 0:
                data = body.get("data") or {}
                return (False, False,
                        f"code={body.get('code')} message={body.get('message')} "
                        f"error={data.get('error')}（免费版每天限 5 条）")
            return True, False, ""
        return False, True, "连接阶段重试耗尽"

    def _post_serverchan(self, sendkey: str, markdown: str, channel,
                         plugin_name: str = "", group: str = "") -> bool:
        """把一条 Markdown 消息推到 Server酱；成功返回 ``True``。

        失败一律记 warning、落 ``data/outbox/serverchan/``、发企业微信告警，
        但绝不抛出（不能影响其它渠道）。
        """
        title = self._serverchan_title(
            markdown, channel.title_max_chars, fallback=plugin_name)
        desp = self._safe_trim(markdown, max_bytes=channel.max_desp_bytes)
        sent, uncertain, reason = self._sc_request(sendkey, title, desp, channel)
        if sent:
            logger.info("[serverchan] 已镜像到个人微信（群=%s plugin=%s）：%s",
                        group or "-", plugin_name, title)
            return True

        reason = redact_text(reason)
        kind = "可能已送达，不自动补推" if uncertain else "未送达，已入待补推队列"
        logger.warning("[serverchan] 推送失败（%s）plugin=%s: %s",
                       kind, plugin_name, reason)
        try:
            path = outbox.enqueue(
                self._config.app.data_dir, plugin=plugin_name,
                group=group or "family", title=title, desp=desp,
                reason=reason, uncertain=uncertain)
            logger.warning("[serverchan] 已存 outbox：%s", path.name)
        except OSError as exc:
            logger.warning("[serverchan] outbox 落盘失败（%s），仅剩告警与日志", exc)
        self._alert_serverchan_failure(channel, plugin_name,
                                       group or "family", reason, uncertain)
        return False

    def _alert_serverchan_failure(self, channel, plugin_name: str, group: str,
                                  reason: str, uncertain: bool) -> None:
        """把失败升级为**手机上可见**的告警：发到企业微信群。

        warning 只落 journald（用户看不到），健康周报又要等周六；这里直接推群。
        同一天同一插件只告警一次（marker 文件），避免网络持续抖动刷屏；
        未补推条目仍会在健康周报里持续提示。告警自身失败也只记 warning。
        """
        if not getattr(channel, "alert_enabled", True):
            return
        d = outbox.outbox_dir(self._config.app.data_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
            marker = d / (f".alerted_{datetime.now().strftime('%Y%m%d')}_"
                          f"{outbox._safe(plugin_name)}")
            if marker.exists():
                return
            marker.touch()  # 先落标记再发，避免告警自身失败造成风暴
        except OSError as exc:
            logger.warning("[serverchan] 告警去重标记写入失败: %s", exc)
            return

        url = self._resolve_webhook_url(getattr(channel, "alert_group", "family"))
        if not url:
            return
        action = ("可能已送达，请先在手机确认，勿重复补推"
                  if uncertain else
                  "将在下次推送自动补推；也可手动执行 `run.py outbox flush`")
        text = (
            f"## ⚠️ 个人微信推送失败\n"
            f"> {datetime.now().strftime('%m-%d %H:%M')}\n"
            f"**插件**：{plugin_name}（群 {group}）\n"
            f"**原因**：{reason[:160]}\n"
            f"**处理**：{action}\n"
            f"消息已存入 `data/outbox/serverchan/`，健康周报会持续提示。")
        if not self._post_webhook(url, text):
            logger.warning("[serverchan] 失败告警未能发出（企业微信 webhook 异常）")

    def flush_serverchan_outbox(self, channel=None) -> dict:
        """补推 outbox 中所有「确定未送达」的消息（自动补推 + CLI 手动入口）。"""
        channel = channel or self._sc_channel()
        if channel is None:
            return {"error": "serverchan 渠道未启用"}
        sendkey = getattr(self._config.secrets, channel.sendkey_env.lower(), None)
        if not sendkey:
            return {"error": "Server酱 SendKey 未配置"}

        def _sender(title, desp):
            return self._sc_request(sendkey, title, desp, channel)

        try:
            stats = outbox.flush(
                self._config.app.data_dir, _sender,
                max_age_hours=int(getattr(channel, "outbox_max_age_hours", 24)),
                max_attempts=int(getattr(channel, "outbox_max_attempts", 3)))
        except Exception as exc:  # noqa: BLE001 — 绝不因补推影响主流程
            logger.warning("[serverchan] outbox 补推异常: %s", exc)
            return {"error": str(exc)}
        if any(stats.get(k) for k in ("sent", "failed", "exhausted")):
            logger.info("[serverchan] outbox 补推结果: %s", stats)
        return stats

    @staticmethod
    def _serverchan_title(markdown: str, max_chars: int = 32,
                          fallback: str = "") -> str:
        """取消息首行作 Server酱 标题：去掉 markdown 前缀与强调符，超长截断。

        免费版微信卡片只显示标题，标题需能独立读懂（如「🌡️ A股市场温度v2」）；
        整条消息无有效文本时退回 *fallback*（插件名）。
        """
        for raw in markdown.splitlines():
            line = raw.strip()
            if not line:
                continue
            line = re.sub(r"^[#>\-*•\s]+", "", line)   # 标题/引用/列表前缀
            line = line.replace("**", "").replace("`", "").strip()
            if not line:
                continue
            if len(line) > max_chars:
                return line[: max_chars - 1].rstrip() + "…"
            return line
        return fallback

    @staticmethod
    def _format_for_wechat(result: Result, plugin_name: str) -> list[str]:
        """Format a ``Result`` as one or more 企业微信 Markdown messages.

        Each returned message is guaranteed ≤ 4000 UTF-8 bytes (safe margin
        below the 4096-byte WeChat limit).

        """
        return [Notifier._build_primary_message(result, plugin_name)]

    @staticmethod
    def _build_primary_message(result: Result, plugin_name: str) -> str:
        """Build the primary WeChat message (header + suggestions + todos).

        Todos are filled dynamically up to the 4000-byte budget instead of
        being hard-capped at 5.  Suggestion details are included when space
        permits.
        """
        MAX_BYTES = 4000
        RESERVED = 80  # reserve for truncation note

        now_str = datetime.now().strftime("%m-%d %H:%M")
        lines: list[str] = []

        # Header
        status_icon = "✅" if result.status == "ok" else "❌"
        lines.append(f"## {status_icon} {plugin_name}")
        lines.append(f"> {now_str}")
        lines.append("")

        # Stats line from result data
        if result.data:
            parts = []
            if "week" in result.data:
                parts.append(result.data["week"])
            if "total_fetched" in result.data and "total_selected" in result.data:
                parts.append(
                    f"{result.data['total_fetched']}篇抓取 · "
                    f"{result.data['total_selected']}篇精选"
                )
            if parts:
                lines.append(f"> {'  '.join(parts)}")
                lines.append("")

        # Suggestions (with byte-aware detail inclusion)
        if result.suggestions:
            lines.append("**📋 Suggestions**")
            for s in result.suggestions:
                priority = s.get("priority", "medium")
                icon = _PRIORITY_ICONS.get(priority, "🟡")
                title = s.get("title", "")
                lines.append(f"- {icon} {title}")
                detail = s.get("detail", "")
                body = "\n".join(lines)
                detail_line = f"  {detail}"
                # Include detail if there is room
                if detail and (
                    len(body.encode("utf-8"))
                    + len(detail_line.encode("utf-8"))
                    + 2  # trailing newline
                    < MAX_BYTES - RESERVED
                ):
                    lines.append(detail_line)
            lines.append("")

        # Todos (dynamic count — fill remaining byte budget)
        if result.todos:
            lines.append("**📝 Todos**")
            count = 0
            for t in result.todos:
                action = t.get("action", "")
                deadline = t.get("deadline", "")
                suffix = f" (截止: {deadline})" if deadline else ""
                todo_line = f"- [ ] {action}{suffix}"
                body = "\n".join(lines)
                # Check if adding this todo + potential count note would overflow
                # We need a trailing newline for the todo, then the count note
                overhead = len(todo_line.encode("utf-8")) + 1  # +1 for newline
                count_note = f"- ... 共 {len(result.todos)} 项"
                count_note_bytes = len(count_note.encode("utf-8")) + 1
                remaining_todos = len(result.todos) - count
                if remaining_todos > 1:
                    overhead += count_note_bytes  # reserve for count note
                if len(body.encode("utf-8")) + overhead < MAX_BYTES - RESERVED:
                    lines.append(todo_line)
                    count += 1
                else:
                    break
            if count < len(result.todos):
                lines.append(f"- ... 共 {len(result.todos)} 项")
            lines.append("")

        # Final trim
        content = "\n".join(lines)
        return Notifier._safe_trim(content)

    @staticmethod
    def _safe_trim(text: str, max_bytes: int = 4000) -> str:
        """Truncate *text* to *max_bytes* at a safe character boundary.

        Cuts at the last complete UTF-8 character, then backtracks further
        to the last newline when that preserves most of the content.
        """
        SUFFIX = "\n\n... (已截断)"
        suffix_bytes = len(SUFFIX.encode("utf-8"))

        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text

        # Keep only complete UTF-8 characters within the limit,
        # reserving space for the suffix
        target = max_bytes - suffix_bytes
        truncated = encoded[:target]
        result = truncated.decode("utf-8", errors="ignore")

        # Prefer breaking at last newline when it's not too far back
        last_nl = result.rfind("\n")
        if last_nl > len(result) * 0.7:
            return result[:last_nl] + SUFFIX

        return result + SUFFIX


# ── Constants ────────────────────────────────────────────────────────────────

_PRIORITY_ICONS = {
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
}
