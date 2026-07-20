from __future__ import annotations

from datetime import datetime, timezone

from heavenly_health.daily_briefing import build_daily_briefing


def test_daily_briefing_turns_a_ready_recovery_state_into_an_explainable_action_loop() -> None:
    state = {
        "status": "ready",
        "daily_state": "recover",
        "primary_action": {
            "kind": "recovery",
            "title": "Choose recovery movement",
            "reason": "Resting heart rate is elevated and HRV is suppressed versus your recent baseline.",
        },
        "data_confidence": "high",
        "data_through": "2026-07-20T07:00:00+00:00",
        "signals": [
            {"metric": "resting_heart_rate", "current": 66, "baseline": 57, "trend": "elevated"},
            {"metric": "heart_rate_variability", "current": 34, "baseline": 45, "trend": "suppressed"},
        ],
    }

    result = build_daily_briefing(state, now=datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc))

    assert result["status"] == "ready"
    assert result["headline"] == "Recovery-leaning day"
    assert result["primary_action"] == state["primary_action"]
    assert result["evidence"] == state["signals"]
    assert result["data_quality"] == {
        "confidence": "high",
        "data_through": "2026-07-20T07:00:00+00:00",
        "limitations": [],
    }
    assert result["feedback"] == {
        "allowed_values": ["done", "partly", "skipped", "not_useful"],
        "instruction": "Reply with one feedback value after acting; feedback is stored only after local owner approval.",
    }
    assert result["generated_at"] == "2026-07-20T09:30:00+00:00"


def test_daily_briefing_is_candid_when_recovery_evidence_is_insufficient() -> None:
    result = build_daily_briefing(
        {
            "status": "insufficient_data",
            "daily_state": "unknown",
            "primary_action": None,
            "data_confidence": "low",
            "data_through": None,
            "signals": [],
        },
        now=datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc),
    )

    assert result["status"] == "insufficient_data"
    assert result["headline"] == "No recovery adjustment suggested"
    assert result["primary_action"] is None
    assert result["data_quality"]["limitations"] == [
        "Fresh resting heart rate and HRV observations with enough personal baseline data are required."
    ]
