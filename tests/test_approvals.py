from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from heavenly_health.approvals import ApprovalError, ApprovalStore


NOW = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)


def proposal_payload() -> dict[str, object]:
    return {
        "source": "manual",
        "metric_type": "resting_heart_rate",
        "event_at": "2026-07-14T05:55:00Z",
        "value_numeric": 55,
        "value_text": None,
        "unit": "bpm",
        "source_record_id": "assigned-at-execution",
        "metadata": {"schema_version": "1.0"},
        "is_synthetic": False,
        "ingest_mode": "manual",
    }


def test_proposal_requires_separate_owner_approval_and_exact_once_consumption(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals", clock=lambda: NOW)

    proposal = store.propose_health_event(
        proposal_payload(),
        preview={"metric_type": "resting_heart_rate", "value": "55 bpm"},
    )

    assert proposal["status"] == "pending"
    assert proposal["approval_id"]
    assert proposal["preview"] == {"metric_type": "resting_heart_rate", "value": "55 bpm"}
    approval_id = str(proposal["approval_id"])
    with pytest.raises(ApprovalError, match="not owner-approved"):
        store.consume_approved(approval_id)

    approved = store.approve(approval_id)
    assert approved["status"] == "approved"
    consumed = store.consume_approved(approval_id)
    assert consumed["payload"]["metric_type"] == "resting_heart_rate"
    assert consumed["payload"]["source_record_id"] == f"heavenly-proposal:{approval_id}"

    store.mark_executed(approval_id, result_reference="event-id")
    with pytest.raises(ApprovalError, match="already executed"):
        store.consume_approved(approval_id)


def test_approval_records_and_signing_key_are_owner_only_and_tampering_is_rejected(tmp_path) -> None:
    root = tmp_path / "approvals"
    store = ApprovalStore(root, clock=lambda: NOW)
    proposal = store.propose_health_event(proposal_payload(), preview={"value": "55 bpm"})
    approval_id = str(proposal["approval_id"])

    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / ".signing-key").stat().st_mode & 0o777 == 0o600
    record_path = root / f"{approval_id}.json"
    assert record_path.stat().st_mode & 0o777 == 0o600

    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["payload"]["value_numeric"] = 999
    record_path.write_text(json.dumps(record), encoding="utf-8")
    record_path.chmod(0o600)

    with pytest.raises(ApprovalError, match="integrity"):
        store.get(approval_id)


def test_expired_rejected_and_unknown_proposals_cannot_execute(tmp_path) -> None:
    current = NOW
    store = ApprovalStore(tmp_path / "approvals", clock=lambda: current)
    proposal = store.propose_health_event(
        proposal_payload(),
        preview={"value": "55 bpm"},
        ttl=timedelta(minutes=5),
    )
    approval_id = str(proposal["approval_id"])

    current = NOW + timedelta(minutes=6)
    with pytest.raises(ApprovalError, match="expired"):
        store.approve(approval_id)

    current = NOW
    another = store.propose_health_event(proposal_payload(), preview={"value": "55 bpm"})
    rejected_id = str(another["approval_id"])
    store.reject(rejected_id)
    with pytest.raises(ApprovalError, match="rejected"):
        store.consume_approved(rejected_id)

    with pytest.raises(ApprovalError, match="Unknown approval"):
        store.get("00000000-0000-4000-8000-000000000099")


def test_audit_history_is_bounded_and_excludes_mutation_payloads(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals", clock=lambda: NOW)
    store.propose_health_event(proposal_payload(), preview={"value": "55 bpm"})

    history = store.audit_history(limit=1000)

    assert len(history) == 1
    assert history[0]["status"] == "pending"
    assert history[0]["preview"] == {"value": "55 bpm"}
    assert "payload" not in history[0]


def test_pending_proposals_are_capped_so_an_agent_cannot_bury_the_owner(tmp_path) -> None:
    """Proposing is the one unattended write, so it needs a ceiling."""
    from heavenly_health import approvals

    monkey_limit = 3
    original = approvals._MAX_PENDING_PROPOSALS
    approvals._MAX_PENDING_PROPOSALS = monkey_limit
    try:
        store = ApprovalStore(tmp_path / "approvals")
        for _ in range(monkey_limit):
            store.propose_daily_feedback(
                daily_state="recover",
                feedback="done",
                data_through=None,
            )

        with pytest.raises(ApprovalError, match="awaiting owner review"):
            store.propose_daily_feedback(
                daily_state="recover",
                feedback="done",
                data_through=None,
            )
    finally:
        approvals._MAX_PENDING_PROPOSALS = original


def test_deciding_a_proposal_frees_capacity_again(tmp_path) -> None:
    from heavenly_health import approvals

    original = approvals._MAX_PENDING_PROPOSALS
    approvals._MAX_PENDING_PROPOSALS = 2
    try:
        store = ApprovalStore(tmp_path / "approvals")
        first = store.propose_daily_feedback(
            daily_state="recover", feedback="done", data_through=None
        )
        store.propose_daily_feedback(
            daily_state="recover", feedback="done", data_through=None
        )
        store.reject(first["approval_id"])

        accepted = store.propose_daily_feedback(
            daily_state="recover", feedback="done", data_through=None
        )

        assert accepted["status"] == "pending"
    finally:
        approvals._MAX_PENDING_PROPOSALS = original
