"""A small, deterministic daily briefing contract built from health_daily_state.

The contract supplies a delivery layer with a stable action, its evidence, data
freshness, and explicit uncertainty. It is not a diagnostic or a medical plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


_FEEDBACK_VALUES = ["done", "partly", "skipped", "not_useful"]
_INSUFFICIENT_LIMITATION = (
    "Fresh resting heart rate and HRV observations with enough personal baseline data are required."
)


def build_daily_briefing(
    state: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return a bounded, explainable delivery contract from a daily state.

    The evaluator owns health interpretation. This layer only makes its decision
    actionable and transparent for a human-facing delivery channel.
    """
    generated_at = _aware_now(now)
    status = str(state.get("status") or "insufficient_data")
    daily_state = str(state.get("daily_state") or "unknown")
    primary_action = state.get("primary_action")
    signals = state.get("signals")
    evidence = list(signals) if isinstance(signals, list) else []

    if status == "ready" and daily_state == "recover" and isinstance(primary_action, Mapping):
        headline = "Recovery-leaning day"
        limitations: list[str] = []
    elif status == "ready" and daily_state == "maintain" and isinstance(primary_action, Mapping):
        headline = "Maintain your planned movement"
        limitations = []
    else:
        status = "insufficient_data"
        headline = "No recovery adjustment suggested"
        primary_action = None
        limitations = [_INSUFFICIENT_LIMITATION]

    return {
        "status": status,
        "headline": headline,
        "primary_action": dict(primary_action) if isinstance(primary_action, Mapping) else None,
        "evidence": evidence,
        "data_quality": {
            "confidence": str(state.get("data_confidence") or "low"),
            "data_through": state.get("data_through"),
            "limitations": limitations,
        },
        "feedback": {
            "allowed_values": list(_FEEDBACK_VALUES),
            "instruction": "Reply with one feedback value after acting; feedback is stored only after local owner approval.",
        },
        "generated_at": generated_at.isoformat(),
    }


def _aware_now(value: datetime | None) -> datetime:
    reference = value or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return reference.astimezone(timezone.utc)
