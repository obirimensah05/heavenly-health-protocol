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

    result = CliRunner().invoke(app, ["briefing", "today"])

    assert result.exit_code == 0
    assert '"headline": "Recovery-leaning day"' in result.stdout
    assert '"primary_action"' in result.stdout
