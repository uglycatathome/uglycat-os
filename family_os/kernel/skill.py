"""Skill contract for Family AI OS.

Every plugin in the system is a ``Skill`` subclass.  The ``Result`` model
is the **only** return type that ``Skill.run()`` may return -- plugins
must never return raw dicts, strings, or other ad-hoc structures.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

if TYPE_CHECKING:
    from family_os.kernel.context import Context


__all__ = ["Result", "Skill"]


# ── Result ────────────────────────────────────────────────────────────────────

class Result(BaseModel):
    """Canonical return value for every plugin's ``run()`` method.

    Parameters
    ----------
    status : str, optional
        ``"ok"`` or ``"error"`` (default ``"ok"``).
    data : dict, optional
        Arbitrary structured output from the plugin (default ``{}``).
    suggestions : list[dict], optional
        Actionable suggestions each with ``title``, ``priority``
        (``"high"`` | ``"medium"`` | ``"low"``), and ``detail``.
    todos : list[dict], optional
        Action items each with ``action``, ``deadline`` (ISO date string),
        and ``category``.
    error : str | None, optional
        Human-readable error message when ``status == "error"``.
    """

    status: str = "ok"
    data: dict = {}
    suggestions: list[dict] = []
    todos: list[dict] = []
    error: Optional[str] = None


# ── Skill (abstract base) ─────────────────────────────────────────────────────

class Skill(ABC):
    """Abstract base class for all Family AI OS plugins.

    Every plugin module must export **exactly one** concrete subclass of
    ``Skill``.  The plugin-discovery machinery (see
    :func:`~family_os.kernel.discovery.discover_plugins`) locates these
    subclasses automatically.

    Class attributes (set on the subclass)
    --------------------------------------
    name : str
        Globally unique identifier for the plugin.
    cost_tier : str
        ``"free"``, ``"low_freq_llm"``, or ``"on_demand_llm"``.
    """

    name: str
    cost_tier: str = "free"

    @abstractmethod
    def run(self, ctx: "Context") -> Result:
        """Execute the plugin's core logic.

        Parameters
        ----------
        ctx : Context
            Dependency-injection container providing access to ``store``,
            ``config``, and optional ``knowledge_base`` / ``llm_gateway``.

        Returns
        -------
        Result
            The canonical result structure.
        """
        ...

    @abstractmethod
    def register_cli(self, app) -> None:
        """Register CLI commands for this plugin.

        Parameters
        ----------
        app
            The CLI application object (e.g. a Click or Typer group).
        """
        ...
