"""Expose the owner's briefing schedule so a connected agent can self-schedule.

Heavenly is a bridge, not a daemon: it never runs the briefing itself. It only
reports when the owner wants the analysis and when an agent should fetch (a fixed
lead before delivery). A connected agent reads this, wakes itself at the
recommended fetch time, calls ``sync_health_source`` then ``query_health_events``,
and has the analysis ready by ``next_briefing_at``.

Only non-secret schedule fields are read. Credentials never pass through here.
"""

from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from heavenly_health.onboarding import default_answers_path

FETCH_LEAD_MINUTES = 10
_UNCONFIGURED: dict[str, Any] = {"configured": False}
_FREQUENCY_DAYS = {"daily": 1, "every_3_days": 3, "weekly": 7, "custom": 1}


def briefing_schedule(
    answers_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the agent-facing briefing schedule, or ``{"configured": False}``.

    ``now`` must be timezone-aware; it defaults to the current UTC instant.
    """
    path = Path(answers_path) if answers_path is not None else default_answers_path()
    reference = now or datetime.now(ZoneInfo("UTC"))

    schedule = _load_schedule(path)
    if schedule is None:
        return dict(_UNCONFIGURED)

    local_time = _parse_local_time(schedule.get("time"))
    zone = _parse_zone(schedule.get("timezone"))
    if local_time is None or zone is None:
        return dict(_UNCONFIGURED)

    frequency = str(schedule.get("frequency") or "daily")
    frequency_days = _FREQUENCY_DAYS.get(frequency, 1)
    # Non-daily cadences need a fixed reference day; without a persisted anchor we
    # fall back to today, which is only exact for daily schedules.
    anchor = _parse_date(schedule.get("anchor_date")) or reference.astimezone(zone).date()

    next_briefing = _next_briefing(
        local_time,
        zone,
        reference,
        frequency_days=frequency_days,
        anchor=anchor,
    )
    fetch_at = next_briefing - timedelta(minutes=FETCH_LEAD_MINUTES)

    return {
        "configured": True,
        "frequency": frequency,
        "frequency_days": frequency_days,
        "arrival": schedule.get("arrival"),
        "local_time": local_time.strftime("%H:%M"),
        "timezone": schedule.get("timezone"),
        "anchor_date": anchor.isoformat(),
        "next_briefing_at": next_briefing.isoformat(),
        "recommended_fetch_at": fetch_at.isoformat(),
        "fetch_lead_minutes": FETCH_LEAD_MINUTES,
        "metrics": _metrics(schedule),
    }


def _load_schedule(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    schedule = payload.get("schedule")
    if not isinstance(schedule, Mapping):
        return None
    metrics = payload.get("metrics")
    enriched = dict(schedule)
    if isinstance(metrics, list):
        enriched["_metrics"] = [str(metric) for metric in metrics]
    return enriched


def _metrics(schedule: Mapping[str, Any]) -> list[str]:
    metrics = schedule.get("_metrics")
    return list(metrics) if isinstance(metrics, list) else []


def _parse_local_time(value: Any) -> clock_time | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return clock_time(hour=hour, minute=minute)


def _parse_zone(value: Any) -> ZoneInfo | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _next_briefing(
    local_time: clock_time,
    zone: ZoneInfo,
    now: datetime,
    *,
    frequency_days: int,
    anchor: date,
) -> datetime:
    """Next delivery datetime on the cadence, measured in whole days from ``anchor``."""
    local_now = now.astimezone(zone)
    day = max(local_now.date(), anchor)
    # Step forward to the first day that lands on the cadence relative to the anchor.
    lag = (day - anchor).days % frequency_days
    if lag:
        day += timedelta(days=frequency_days - lag)
    candidate = datetime.combine(day, local_time, tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=frequency_days)
    return candidate
