from __future__ import annotations

from datetime import datetime, timezone

from heavenly_health.approvals import ApprovalStore


NOW = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)


def test_daily_feedback_is_pending_until_local_owner_approval_then_becomes_learning_history(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals", clock=lambda: NOW)

    proposal = store.propose_daily_feedback(
        daily_state="recover",
        feedback="partly",
        data_through="2026-07-20T07:00:00+00:00",
    )

    assert proposal["status"] == "pending"
    assert proposal["operation"] == "record_daily_feedback"
    assert proposal["preview"] == {
        "operation": "record_daily_feedback",
        "daily_state": "recover",
        "feedback": "partly",
        "data_through": "2026-07-20T07:00:00+00:00",
        "confirmation": "Run heavenly approval approve <approval-id> locally",
    }
    assert store.feedback_history() == []

    store.approve(str(proposal["approval_id"]))

    assert store.feedback_history() == [
        {
            "daily_state": "recover",
            "feedback": "partly",
            "reported_at": "2026-07-20T09:30:00+00:00",
            "data_through": "2026-07-20T07:00:00+00:00",
        }
    ]


def test_daily_feedback_rejects_unknown_values_and_never_exposes_internal_payload(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals", clock=lambda: NOW)

    for feedback in ("", "yes", "DONE", "not-useful"):
        try:
            store.propose_daily_feedback(daily_state="maintain", feedback=feedback, data_through=None)
        except ValueError as error:
            assert "feedback" in str(error)
        else:
            raise AssertionError(f"invalid feedback accepted: {feedback}")
