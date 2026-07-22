import json

from rich.console import Console
from typer.testing import CliRunner

from heavenly_health.cli import app


def test_briefing_today_prints_the_bounded_delivery_contract(monkeypatch) -> None:
    class FakeHealthStore:
        def daily_briefing(self):
            return {
                "status": "ready",
                "headline": "Recovery-leaning day",
                "primary_action": {"title": "Choose recovery movement"},
            }

    monkeypatch.setattr("heavenly_health.cli._configured_health_store", lambda: FakeHealthStore())
    monkeypatch.setattr("heavenly_health.cli.console", Console(width=40, force_terminal=False))

    result = CliRunner().invoke(app, ["briefing", "today"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["headline"] == "Recovery-leaning day"
    assert payload["primary_action"] == {"title": "Choose recovery movement"}


def test_briefing_today_falls_back_to_the_local_google_health_api_when_storage_is_unavailable(monkeypatch) -> None:
    from heavenly_health.health_storage import HealthStorageError

    class FakeHealthStore:
        settings = type(
            "Settings",
            (),
            {"allowed_metrics": frozenset({"resting_heart_rate", "heart_rate_variability"})},
        )()

        def daily_briefing(self):
            raise HealthStorageError("Supabase health storage request failed")

    class FakeRuntime:
        def google_health_events(self, allowed_metrics):
            assert allowed_metrics == frozenset({"resting_heart_rate", "heart_rate_variability"})
            return [
                {
                    "metric_type": "resting_heart_rate",
                    "value_numeric": 50,
                    "event_at": "2026-07-20T08:00:00+00:00",
                }
            ]

    monkeypatch.setattr("heavenly_health.cli._configured_health_store", lambda: FakeHealthStore())
    monkeypatch.setattr("heavenly_health.cli._provider_runtime", lambda: FakeRuntime())

    result = CliRunner().invoke(app, ["briefing", "today"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["headline"] == "No recovery adjustment suggested"
    assert payload["data_quality"]["source"] == "google_health_api"
    assert payload["data_quality"]["limitations"][-1] == "Read directly from Google Health API because normalized storage was unavailable."
