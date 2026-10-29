"""Approval state machine — manages the lifecycle of approval decisions.

Provides a stateful workflow for submitting, approving, and rejecting
decisions, with support for auto-approval based on configuration.

State flow::

    pending ──approve──> approved ──> done
        └──reject──> rejected (with reason)
"""

from family_os.kernel.store import Store
from family_os.kernel.config import Config


class ApprovalStateMachine:
    """Manages approval decisions with a simple state machine.

    Parameters
    ----------
    store : Store
        Persistent storage for decisions.
    config : Config
        Application configuration, including approval settings.
    """

    def __init__(self, store: Store, config: Config) -> None:
        self._store = store
        self._config = config

    def submit(
        self,
        plugin_name: str,
        title: str,
        suggestion: str,
        priority: str = "medium",
        auto_approve: bool = False,
    ) -> int:
        """Submit a new decision for approval.

        Prepends ``[plugin_name]`` to the *title*.  If both *auto_approve*
        is ``True`` *and* ``config.approval.auto_approve_free`` is ``True``
        the decision is immediately marked as ``approved``; otherwise it
        starts as ``pending``.

        Parameters
        ----------
        plugin_name : str
            Name of the plugin submitting the decision.
        title : str
            Short human-readable title.
        suggestion : str
            The suggestion or rationale for the decision.
        priority : str
            Priority level (default ``"medium"``).
        auto_approve : bool
            Whether the plugin requests auto-approval (default ``False``).

        Returns
        -------
        int
            The new decision's database id.
        """
        full_title = f"[{plugin_name}] {title}"

        if auto_approve and self._config.approval.auto_approve_free:
            status = "approved"
        else:
            status = "pending"

        decision_id = self._store.add_decision(
            entity_id=None,
            title=full_title,
            rationale=suggestion,
            status=status,
        )
        return decision_id

    def approve(self, decision_id: int, approved_by: str) -> None:
        """Approve a pending decision.

        Parameters
        ----------
        decision_id : int
            The decision to approve.
        approved_by : str
            Name or identifier of the approver.
        """
        self._store.update_decision_status(
            decision_id, "approved", approved_by=approved_by,
        )

    def reject(
        self, decision_id: int, reason: str, rejected_by: str = "unknown"
    ) -> None:
        """Reject a pending decision with a reason.

        Parameters
        ----------
        decision_id : int
            The decision to reject.
        reason : str
            Explanation for the rejection.
        rejected_by : str
            Name or identifier of the person who rejected
            (default ``"unknown"``).
        """
        self._store.update_decision_status(
            decision_id, "rejected", rejected_reason=reason,
        )

    def list_pending(self) -> list[dict]:
        """Return all decisions with ``pending`` status.

        Delegates to ``Store.get_pending_decisions``.

        Returns
        -------
        list[dict]
            Pending decisions (dict-like row objects from the store).
        """
        return self._store.get_pending_decisions()
