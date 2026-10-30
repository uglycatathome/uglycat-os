"""Ingestion layer — Normalizer and SourceRegistry for Family AI OS.

Normalizer
    Wraps the Store with convenience methods for ingesting facts and documents,
    applying sensible defaults and leveraging the Store's built-in dedup logic.

SourceRegistry
    Loads source definitions from a YAML file and provides lookup by tag or id.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_MEMBER = "全家"
_DEFAULT_SENSITIVITY = "private"
_DEFAULT_SOURCE = "manual_note"
_DEFAULT_FORMAT = "structured"


class Normalizer:
    """Wraps a Store instance with convenience methods for ingesting facts
    and documents, applying sensible defaults and leveraging the Store's
    built-in dedup logic.

    Parameters
    ----------
    store : Store
        An initialised ``Store`` instance.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    # ── Fact ingestion ─────────────────────────────────────────────────────────

    def ingest_fact(self, raw: dict) -> int:
        """Ingest a single fact from a raw dictionary.

        Delegates to ``Store.add_fact()`` which performs dedup on
        ``type + key + value``.

        Parameters
        ----------
        raw : dict
            Must contain at least ``type``, ``key``, and ``value``.
            Optional keys (with defaults):

            - ``member``      → ``"全家"``
            - ``sensitivity`` → ``"private"``
            - ``source``      → ``"manual_note"``
            - ``format``      → ``"structured"``

        Returns
        -------
        int
            The fact id (new or existing).
        """
        fact_type = raw["type"]
        key = raw["key"]
        value = raw["value"]

        member = raw.get("member", _DEFAULT_MEMBER)
        sensitivity = raw.get("sensitivity", _DEFAULT_SENSITIVITY)
        source = raw.get("source", _DEFAULT_SOURCE)
        fmt = raw.get("format", _DEFAULT_FORMAT)

        return self._store.add_fact(
            fact_type=fact_type,
            key=key,
            value=value,
            member=member,
            sensitivity=sensitivity,
            source=source,
            fmt=fmt,
        )

    # ── Document ingestion ─────────────────────────────────────────────────────

    def ingest_document(
        self,
        path: str,
        doc_type: str,
        entity_id: int | None = None,
        tags: list[str] | None = None,
    ) -> int:
        """Ingest a document record.

        Parameters
        ----------
        path : str
            Filesystem path to the document.
        doc_type : str
            Arbitrary type label (e.g. ``"note"``, ``"receipt"``).
        entity_id : int | None
            Optional entity to associate the document with.
        tags : list[str] | None
            Optional list of tag strings.

        Returns
        -------
        int
            The document id.
        """
        return self._store.add_document(
            path=path,
            doc_type=doc_type,
            entity_id=entity_id,
            tags=tags,
        )

    # ── Batch ingestion ────────────────────────────────────────────────────────

    def ingest_batch(self, facts: list[dict]) -> list[int]:
        """Ingest multiple facts in a single batch.

        Parameters
        ----------
        facts : list[dict]
            Each dict is forwarded to ``ingest_fact``.

        Returns
        -------
        list[int]
            Fact ids in the same order as *facts*.
        """
        return [self.ingest_fact(raw) for raw in facts]


class SourceRegistry:
    """Loads source definitions from a YAML file and provides lookup by
    tag or id.

    Parameters
    ----------
    sources_path : str | Path
        Path to a YAML file containing a top-level ``sources`` key which
        maps to a list of source objects. Each source object should have
        at minimum an ``id`` and may include ``tags``, ``name``, etc.
    """

    def __init__(self, sources_path: str | Path) -> None:
        self._sources_path = Path(sources_path)
        self._sources: list[dict] = []
        self._load()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Parse the YAML file and populate ``_sources``.

        If the file does not exist a warning is logged and ``_sources``
        remains an empty list.
        """
        if not self._sources_path.exists():
            logger.warning("Sources file not found: %s", self._sources_path)
            return

        with open(self._sources_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self._sources = data.get("sources", []) if data else []
        self._merge_local()

    def _merge_local(self) -> None:
        """合并同目录下的 `sources.local.yaml`（用户私有清单，2026-10-30）。

        用户自加的源写在这里，`git pull` 更新主清单时不会冲突：
          · name 与主清单相同的条目 → 覆盖对应字段（典型用法：`enabled: false` 关掉某源）
          · name 全新的条目 → 追加到末尾
        文件不存在或格式错误时静默跳过，行为与本功能不存在时完全一致。
        """
        local = self._sources_path.with_name("sources.local.yaml")
        if not local.exists():
            return
        try:
            data = yaml.safe_load(local.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("sources.local.yaml 解析失败，已忽略：%s", e)
            return
        extra = (data or {}).get("sources", []) or []
        index = {(s.get("name") or s.get("id")): i
                 for i, s in enumerate(self._sources)}
        for item in extra:
            if not isinstance(item, dict):
                continue
            key = item.get("name") or item.get("id")
            if key and key in index:
                self._sources[index[key]] = {**self._sources[index[key]], **item}
            else:
                self._sources.append(item)
        logger.info("已合并用户私有源 %d 条（sources.local.yaml）", len(extra))

    # ── Queries ────────────────────────────────────────────────────────────────

    def list_all(self) -> list[dict]:
        """Return all registered sources.

        Returns a shallow copy so callers cannot mutate internal state.
        """
        return list(self._sources)

    def get_sources_by_tags(self, tags: list[str]) -> list[dict]:
        """Return sources whose tags intersect with *tags*.

        Parameters
        ----------
        tags : list[str]
            One or more tag values to match against source tags.

        Returns
        -------
        list[dict]
            Matching source objects (empty list if none match).
        """
        tag_set = set(tags)
        return [s for s in self._sources if tag_set & set(s.get("tags", []))]

    def get_source_by_id(self, source_id: str) -> dict | None:
        """Return a source by its id, or ``None`` if not found.

        Parameters
        ----------
        source_id : str
            The unique identifier of the source.

        Returns
        -------
        dict | None
        """
        for s in self._sources:
            if s.get("id") == source_id:
                return s
        return None
