from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
from zoneinfo import ZoneInfo

from heavenly_health.briefing import FETCH_LEAD_MINUTES, briefing_schedule


def _write_answers(path: Path, schedule: dict, metrics: list[str] | None = None) -> None:
    payload = {"schedule": schedule, "metrics": metrics or ["steps", "sleep_analysis"]}
    path.write_text(json.dumps(payload), encoding="utf-8")


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
        {"frequency": "weekly", "arrival": "evening", "time": "20:00", "timezone": "UTC"},
    )

    result = briefing_schedule(answers, now=datetime(2026, 7, 15, 6, 0, tzinfo=ZoneInfo("UTC")))

    assert result["frequency"] == "weekly"
    assert result["frequency_days"] == 7
    assert result["next_briefing_at"] == "2026-07-15T20:00:00+00:00"


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
