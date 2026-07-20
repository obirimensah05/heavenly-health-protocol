"""Deterministic, provenance-friendly daily health state classification.

This module deliberately avoids proprietary composite scores. It compares a small
set of fresh, user-approved signals against the owner's own recent baseline and
returns an explainable action band rather than a medical conclusion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping, Sequence

_CURRENT_WINDOW = timedelta(hours=36)
_BASELINE_START = timedelta(days=3)
_BASELINE_END = timedelta(days=30)
_MIN_BASELINE_SAMPLES = 7
DAILY_STATE_METRICS = ("resting_heart_rate", "heart_rate_variability")


def evaluate_daily_state(
    events: Sequence[Mapping[str, object]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return an explainable daily action band from fresh recovery signals.

    Only resting heart rate and HRV are assessed in v1 because their comparison
    against a personal baseline is transparent. Missing, stale, or insufficient
    data never produces a recommendation.
    """
    reference = _aware_now(now)
    observations = _parse_observations(events)
    data_through = max((at for _, _, at in observations), default=None)
    signals = [_signal(metric, observations, reference) for metric in DAILY_STATE_METRICS]
    usable = [signal for signal in signals if signal is not None]

    if len(usable) != len(DAILY_STATE_METRICS):
        return {
            "status": "insufficient_data",
            "daily_state": "unknown",
            "primary_action": None,
            "data_confidence": "low",
            "data_through": data_through.isoformat() if data_through else None,
            "signals": usable,
        }

    trends = {str(signal["metric"]): str(signal["trend"]) for signal in usable}
    reduced_recovery = (
        trends["resting_heart_rate"] == "elevated"
        and trends["heart_rate_variability"] == "suppressed"
    )
    if reduced_recovery:
        daily_state = "recover"
        action = {
            "kind": "recovery",
            "title": "Choose recovery movement",
            "reason": "Resting heart rate is elevated and HRV is suppressed versus your recent baseline.",
        }
    else:
        daily_state = "maintain"
        action = {
            "kind": "maintain",
            "title": "Maintain planned movement",
            "reason": "Current recovery signals are not jointly reduced versus your recent baseline.",
        }

    return {
        "status": "ready",
        "daily_state": daily_state,
        "primary_action": action,
        "data_confidence": "high",
        "data_through": data_through.isoformat() if data_through else None,
        "signals": usable,
    }


def _signal(metric: str, observations: Sequence[tuple[str, float, datetime]], now: datetime) -> dict[str, object] | None:
    current_cutoff = now - _CURRENT_WINDOW
    baseline_start = now - _BASELINE_END
    baseline_end = now - _BASELINE_START
    current = [(value, at) for name, value, at in observations if name == metric and at >= current_cutoff and at <= now]
    baseline = [
        value
        for name, value, at in observations
        if name == metric and baseline_start <= at <= baseline_end
    ]
    if not current or len(baseline) < _MIN_BASELINE_SAMPLES:
        return None

    current_value, current_at = max(current, key=lambda item: item[1])
    baseline_value = median(baseline)
    if metric == "resting_heart_rate":
        trend = "elevated" if current_value > baseline_value * 1.1 else "stable"
    else:
        trend = "suppressed" if current_value < baseline_value * 0.9 else "stable"
    return {
        "metric": metric,
        "current": current_value,
        "baseline": baseline_value,
        "trend": trend,
        "observed_at": current_at.isoformat(),
    }


def _parse_observations(events: Sequence[Mapping[str, object]]) -> list[tuple[str, float, datetime]]:
    parsed: list[tuple[str, float, datetime]] = []
    for event in events:
        metric = event.get("metric_type")
        value = event.get("value_numeric")
        timestamp = event.get("event_at")
        if not isinstance(metric, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not isinstance(timestamp, str):
            continue
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed_at.tzinfo is None:
            continue
        parsed.append((metric, float(value), observed_at.astimezone(timezone.utc)))
    return parsed


def _aware_now(value: datetime | None) -> datetime:
    reference = value or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return reference.astimezone(timezone.utc)
