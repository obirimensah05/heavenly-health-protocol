from __future__ import annotations

from datetime import datetime, timedelta, timezone

from heavenly_health.daily_state import evaluate_daily_state


def _event(metric_type: str, value: float, at: datetime) -> dict[str, object]:
    return {
        "metric_type": metric_type,
        "value_numeric": value,
        "event_at": at.isoformat(),
    }


def test_daily_state_reports_insufficient_data_without_events() -> None:
    result = evaluate_daily_state([], now=datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc))

    assert result["status"] == "insufficient_data"
    assert result["daily_state"] == "unknown"
    assert result["primary_action"] is None
    assert result["data_confidence"] == "low"


def test_daily_state_recommends_recovery_when_resting_hr_rises_and_hrv_falls_against_baseline() -> None:
    now = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)
    events = [
        _event("resting_heart_rate", 50, now - timedelta(days=day))
        for day in range(3, 17)
    ] + [
        _event("heart_rate_variability", 60, now - timedelta(days=day))
        for day in range(3, 17)
    ] + [
        _event("resting_heart_rate", 58, now - timedelta(hours=3)),
        _event("heart_rate_variability", 45, now - timedelta(hours=3)),
    ]

    result = evaluate_daily_state(events, now=now)

    assert result["status"] == "ready"
    assert result["daily_state"] == "recover"
    assert result["primary_action"]["kind"] == "recovery"
    assert result["data_confidence"] == "high"
    assert {signal["trend"] for signal in result["signals"]} == {"elevated", "suppressed"}


def test_daily_state_recommends_maintaining_planned_movement_when_signals_match_baseline() -> None:
    now = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)
    events = [
        _event("resting_heart_rate", 50, now - timedelta(days=day))
        for day in range(3, 17)
    ] + [
        _event("heart_rate_variability", 60, now - timedelta(days=day))
        for day in range(3, 17)
    ] + [
        _event("resting_heart_rate", 51, now - timedelta(hours=3)),
        _event("heart_rate_variability", 61, now - timedelta(hours=3)),
    ]

    result = evaluate_daily_state(events, now=now)

    assert result["status"] == "ready"
    assert result["daily_state"] == "maintain"
    assert result["primary_action"]["kind"] == "maintain"
    assert result["data_through"] == (now - timedelta(hours=3)).isoformat()
