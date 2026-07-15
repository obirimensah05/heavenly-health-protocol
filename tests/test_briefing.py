from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
from zoneinfo import ZoneInfo

from heavenly_health.briefing import FETCH_LEAD_MINUTES, briefing_schedule


def _write_answers(path: Path, schedule: dict, metrics: list[str] | None = None) -> None:
    payload = {"schedule": schedule, "metrics": metrics or ["steps", "sleep_analysis"]}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("UTC"))


def test_missing_answers_file_reports_unconfigured(tmp_path: Path) -> None:
    result = briefing_schedule(tmp_path / "absent.json", now=datetime(2026, 7, 15, 6, 0, tzinfo=ZoneInfo("UTC")))

    assert result == {"configured": False}


def test_daily_schedule_computes_next_briefing_and_fetch_lead(tmp_path: Path) -> None:
    answers = tmp_path / "onboarding.json"
    _write_answers(
        answers,
        {"frequency": "daily", "arrival": "morning", "time": "09:30", "timezone": "Europe/Berlin"},
    )

    # 07:00 Berlin is before the 09:30 delivery, so the brief is later today.
    now = datetime(2026, 7, 15, 5, 0, tzinfo=ZoneInfo("UTC"))
    result = briefing_schedule(answers, now=now)

    assert result["configured"] is True
    assert result["frequency"] == "daily"
    assert result["frequency_days"] == 1
    assert result["local_time"] == "09:30"
    assert result["timezone"] == "Europe/Berlin"
    assert result["fetch_lead_minutes"] == FETCH_LEAD_MINUTES
    assert result["next_briefing_at"] == "2026-07-15T09:30:00+02:00"
    assert result["recommended_fetch_at"] == "2026-07-15T09:20:00+02:00"
    assert result["metrics"] == ["steps", "sleep_analysis"]


def test_next_briefing_rolls_to_tomorrow_after_the_time_passes(tmp_path: Path) -> None:
    answers = tmp_path / "onboarding.json"
    _write_answers(
        answers,
        {"frequency": "daily", "arrival": "morning", "time": "09:30", "timezone": "Europe/Berlin"},
    )

    # 10:00 Berlin is past today's 09:30, so the next brief is tomorrow.
    now = datetime(2026, 7, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
    result = briefing_schedule(answers, now=now)

    assert result["next_briefing_at"] == "2026-07-16T09:30:00+02:00"
    assert result["recommended_fetch_at"] == "2026-07-16T09:20:00+02:00"


def test_weekly_frequency_exposes_cadence_days(tmp_path: Path) -> None:
    answers = tmp_path / "onboarding.json"
    _write_answers(
        answers,
        {
            "frequency": "weekly",
            "arrival": "evening",
            "time": "20:00",
            "timezone": "UTC",
            "anchor_date": "2026-07-15",
        },
    )

    result = briefing_schedule(answers, now=_utc(2026, 7, 15, 6))

    assert result["frequency"] == "weekly"
    assert result["frequency_days"] == 7
    assert result["anchor_date"] == "2026-07-15"
    assert result["next_briefing_at"] == "2026-07-15T20:00:00+00:00"


def test_weekly_next_briefing_respects_cadence_not_tomorrow(tmp_path: Path) -> None:
    # Anchored to Monday 2026-07-13; the only weekly briefing days are 13, 20, 27...
    answers = tmp_path / "onboarding.json"
    _write_answers(
        answers,
        {
            "frequency": "weekly",
            "arrival": "morning",
            "time": "09:00",
            "timezone": "UTC",
            "anchor_date": "2026-07-13",
        },
    )

    # Wednesday 2026-07-15 is NOT a briefing day; the next must be 2026-07-20, not tomorrow.
    result = briefing_schedule(answers, now=_utc(2026, 7, 15, 6))

    assert result["next_briefing_at"] == "2026-07-20T09:00:00+00:00"
    assert result["recommended_fetch_at"] == "2026-07-20T08:50:00+00:00"


def test_every_3_days_lands_on_anchor_cadence(tmp_path: Path) -> None:
    # Anchor 2026-07-15; briefing days are 15, 18, 21...
    answers = tmp_path / "onboarding.json"
    _write_answers(
        answers,
        {
            "frequency": "every_3_days",
            "arrival": "morning",
            "time": "07:30",
            "timezone": "UTC",
            "anchor_date": "2026-07-15",
        },
    )

    # 2026-07-17 is off-cadence; next is 2026-07-18.
    result = briefing_schedule(answers, now=_utc(2026, 7, 17, 12))

    assert result["frequency_days"] == 3
    assert result["next_briefing_at"] == "2026-07-18T07:30:00+00:00"


def test_daily_after_time_rolls_one_day_even_with_anchor(tmp_path: Path) -> None:
    answers = tmp_path / "onboarding.json"
    _write_answers(
        answers,
        {
            "frequency": "daily",
            "arrival": "morning",
            "time": "09:30",
            "timezone": "UTC",
            "anchor_date": "2026-07-01",
        },
    )

    result = briefing_schedule(answers, now=_utc(2026, 7, 15, 10))

    assert result["next_briefing_at"] == "2026-07-16T09:30:00+00:00"


def test_malformed_time_or_timezone_reports_unconfigured(tmp_path: Path) -> None:
    answers = tmp_path / "onboarding.json"
    _write_answers(answers, {"frequency": "daily", "time": "not-a-time", "timezone": "Europe/Berlin"})
    assert briefing_schedule(answers, now=datetime(2026, 7, 15, 6, 0, tzinfo=ZoneInfo("UTC"))) == {
        "configured": False
    }

    _write_answers(answers, {"frequency": "daily", "time": "09:30", "timezone": "Mars/Phobos"})
    assert briefing_schedule(answers, now=datetime(2026, 7, 15, 6, 0, tzinfo=ZoneInfo("UTC"))) == {
        "configured": False
    }
