"""Digest aggregator — collects plugin inbox files and produces a Markdown briefing.

The ``DigestAggregator`` is responsible for:

- Scanning a configured inbox directory for ``.md`` files produced by plugins.
- Parsing the file-name convention ``YYYYMMDD_HHMMSS_plugin.md`` to extract
  timestamps and plugin names.
- Merging all collected items into a human-readable Markdown digest.
- Saving the digest to the history directory and cleaning up processed files.
"""

import datetime
import re
from pathlib import Path

from family_os.kernel.config import Config


# Regex for inbox file names: YYYYMMDD_HHMMSS_plugin.md
_INBOX_FILE_RE = re.compile(r"^(\d{8})_(\d{6})_(.+)\.md$")


class DigestAggregator:
    """Aggregate plugin output into a Markdown briefing.

    Parameters
    ----------
    config : Config
        Application configuration.  ``config.app.data_dir`` is used to
        determine the inbox and history digest directories.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._inbox_dir = Path(config.app.data_dir) / "inbox"
        self._history_dir = Path(config.app.data_dir) / "history" / "digests"

    # ── public API ──────────────────────────────────────────────────────────

    def collect(self, since_hours: int = 24) -> list[dict]:
        """Scan the inbox directory for recent plugin output files.

        Files that match the convention ``YYYYMMDD_HHMMSS_plugin.md`` are
        parsed.  Only files whose timestamp falls within the last
        *since_hours* are returned.

        Parameters
        ----------
        since_hours : int
            How many hours back to consider (default ``24``).

        Returns
        -------
        list[dict]
            Each item has keys ``plugin_name``, ``timestamp`` (ISO-format
            string), ``content``, and ``filepath``.
        """
        if not self._inbox_dir.is_dir():
            return []

        cutoff = datetime.datetime.now() - datetime.timedelta(hours=since_hours)
        items: list[dict] = []

        for path in sorted(self._inbox_dir.iterdir()):
            if not path.suffix == ".md":
                continue

            match = _INBOX_FILE_RE.match(path.name)
            if not match:
                continue

            date_str, time_str, plugin_name = match.group(1, 2, 3)

            try:
                file_dt = datetime.datetime.strptime(
                    f"{date_str}_{time_str}", "%Y%m%d_%H%M%S",
                )
            except ValueError:
                continue  # malformed date/time, skip

            if file_dt < cutoff:
                continue

            content = path.read_text(encoding="utf-8")
            items.append({
                "plugin_name": plugin_name,
                "timestamp": file_dt.isoformat(),
                "content": content,
                "filepath": str(path),
            })

        return items

    def merge(self, items: list[dict]) -> str:
        """Merge collected items into a Markdown briefing.

        Items whose content contains ``🔴`` or the Chinese keyword ``紧急``
        are placed in a high-priority section at the top.

        Parameters
        ----------
        items : list[dict]
            Items returned by :meth:`collect`.

        Returns
        -------
        str
            Markdown-formatted digest.
        """
        if not items:
            return "本周期无新产出。"

        # Determine title based on configured digest mode
        mode = self._config.digest.mode
        if mode == "weekly":
            title = "Family AI OS — 每周简报"
        else:
            title = "Family AI OS — 每日简报"

        # Separate high-priority from normal items
        high_priority: list[dict] = []
        normal: list[dict] = []

        for item in items:
            content = item.get("content", "")
            if "🔴" in content or "紧急" in content:
                high_priority.append(item)
            else:
                normal.append(item)

        lines: list[str] = [
            f"# {title}",
            "",
            f"**生成时间**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

        if high_priority:
            lines.append("## 🔴 高优先级")
            lines.append("")
            for item in high_priority:
                lines.append(f"### [{item['plugin_name']}]")
                lines.append("")
                lines.append(f"- **时间**: {item['timestamp']}")
                lines.append("")
                lines.append(item["content"])
                lines.append("")
            lines.append("---")
            lines.append("")

        if normal:
            lines.append("## 📋 一般信息")
            lines.append("")
            for item in normal:
                lines.append(f"### [{item['plugin_name']}]")
                lines.append("")
                lines.append(f"- **时间**: {item['timestamp']}")
                lines.append("")
                lines.append(item["content"])
                lines.append("")

        return "\n".join(lines).strip()

    def run(self) -> str:
        """Execute the full digest cycle.

        1. :meth:`collect` items from the inbox.
        2. :meth:`merge` them into a Markdown briefing.
        3. Save the result to ``<history_dir>/digest_<timestamp>.md``.
        4. Delete all processed ``.md`` files from the inbox.
        5. Return the merged text.

        Returns
        -------
        str
            The merged Markdown digest.
        """
        items = self.collect()

        if not items:
            return self.merge([])

        merged = self.merge(items)

        # Save to history
        self._history_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        digest_path = self._history_dir / f"digest_{timestamp}.md"
        digest_path.write_text(merged, encoding="utf-8")

        # Delete processed inbox .md files
        for item in items:
            fp = Path(item["filepath"])
            if fp.exists():
                fp.unlink()

        return merged
