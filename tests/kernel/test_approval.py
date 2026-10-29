"""Tests for ApprovalStateMachine — decision lifecycle management."""

from family_os.kernel.store import Store
from family_os.kernel.config import (
    Config,
    AppConfig,
    LLMConfig,
    PluginConfig,
    SchedulerConfig,
    NotifierConfig,
    DigestConfig,
    ApprovalConfig,
    Secrets,
)
from family_os.kernel.approval import ApprovalStateMachine


def _make_config(auto_approve_free: bool = True) -> Config:
    """Build a minimal Config with an overridable approval setting."""
    return Config(
        app=AppConfig(),
        llm=LLMConfig(),
        plugins=PluginConfig(),
        scheduler=SchedulerConfig(),
        notifier=NotifierConfig(),
        digest=DigestConfig(),
        approval=ApprovalConfig(auto_approve_free=auto_approve_free),
        secrets=Secrets(),
    )


class TestApprovalStateMachine:
    """Approval state machine — pending / approved / rejected lifecycle."""

    def test_list_pending_returns_empty_initially(self, test_db_path):
        """A fresh machine should have no pending decisions."""
        store = Store(test_db_path)
        store.initialize()
        am = ApprovalStateMachine(store, _make_config())

        assert am.list_pending() == []

    def test_submit_creates_pending_decision(self, test_db_path):
        """submit() should create a pending decision visible in list_pending."""
        store = Store(test_db_path)
        store.initialize()
        am = ApprovalStateMachine(store, _make_config())

        dec_id = am.submit("test_plugin", "Test Title", "Test suggestion")
        pending = am.list_pending()
        assert len(pending) == 1
        assert pending[0]["title"] == "[test_plugin] Test Title"
        assert pending[0]["id"] == dec_id

    def test_approve_moves_out_of_pending(self, test_db_path):
        """Approving a decision should remove it from the pending list."""
        store = Store(test_db_path)
        store.initialize()
        am = ApprovalStateMachine(store, _make_config())

        dec_id = am.submit("test_plugin", "Test", "Suggestion")
        assert len(am.list_pending()) == 1

        am.approve(dec_id, approved_by="Alice")
        assert len(am.list_pending()) == 0

    def test_reject_records_reason(self, test_db_path):
        """Rejecting a decision should remove it from pending."""
        store = Store(test_db_path)
        store.initialize()
        am = ApprovalStateMachine(store, _make_config())

        dec_id = am.submit("test_plugin", "Test", "Suggestion")
        assert len(am.list_pending()) == 1

        am.reject(dec_id, reason="Not needed", rejected_by="Bob")
        assert len(am.list_pending()) == 0

    def test_auto_approve_skips_queue_when_config_allows(self, test_db_path):
        """When auto_approve_free=True, a decision submitted with
        auto_approve=True should never appear in pending list."""
        store = Store(test_db_path)
        store.initialize()
        # auto_approve_free=True by default
        am = ApprovalStateMachine(store, _make_config(auto_approve_free=True))

        dec_id = am.submit("test_plugin", "Test", "Suggestion", auto_approve=True)
        assert len(am.list_pending()) == 0

    def test_auto_approve_not_applied_when_config_disabled(self, test_db_path):
        """When auto_approve_free=False, even auto_approve=True should
        produce a pending decision."""
        store = Store(test_db_path)
        store.initialize()
        am = ApprovalStateMachine(store, _make_config(auto_approve_free=False))

        dec_id = am.submit("test_plugin", "Test", "Suggestion", auto_approve=True)
        assert len(am.list_pending()) == 1
