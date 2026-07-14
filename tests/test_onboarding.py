import json
import stat

from typer.testing import CliRunner

from heavenly_health.cli import app
from heavenly_health.onboarding import (
    OnboardingAnswers,
    answers_payload,
    connect_instructions,
    metrics_for_sources,
    render_runtime_env,
)


runner = CliRunner()


def _answers(**overrides) -> OnboardingAnswers:
    base = dict(
        devices=("apple_watch",),
        source_app="apple_health",
        destination="supabase",
        agent="claude_code",
        agent_location="local",
        frequency="daily",
        arrival="morning",
        briefing_time="09:30",
        timezone="Europe/Berlin",
    )
    base.update(overrides)
    return OnboardingAnswers(**base)


def test_metrics_follow_the_selected_source_app() -> None:
    apple = metrics_for_sources(("apple_health",))
    assert "steps" in apple
    assert "sleep_analysis" in apple
    assert "body_battery" not in apple

    garmin = metrics_for_sources(("garmin",))
    assert "body_battery" in garmin
    assert "stress_level" in garmin


def test_unimplemented_sources_grant_no_metrics() -> None:
    assert metrics_for_sources(("health_connect",)) == ()


def test_whoop_and_oura_sources_grant_their_implemented_metrics() -> None:
    whoop = metrics_for_sources(("whoop",))
    assert "heart_rate_variability" in whoop
    assert "steps" not in whoop
    oura = metrics_for_sources(("oura",))
    assert "steps" in oura
    assert "oxygen_saturation" in oura


def test_runtime_env_renders_storage_and_a_bounded_allowlist() -> None:
    content = render_runtime_env(
        _answers(),
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-secret",
        apple_delivery_table="hae_payloads",
    )
    assert "SUPABASE_URL=https://example.supabase.co" in content
    assert "SUPABASE_SERVICE_ROLE_KEY=service-role-secret" in content
    assert "HEAVENLY_APPLE_HEALTH_DELIVERY_TABLE=hae_payloads" in content
    allowlist_line = next(
        line for line in content.splitlines() if line.startswith("HEAVENLY_ALLOWED_METRICS=")
    )
    assert "steps" in allowlist_line
    assert "body_battery" not in allowlist_line


def test_runtime_env_without_storage_falls_back_to_status_only() -> None:
    content = render_runtime_env(_answers(source_app="google_health", devices=("fitbit_pixel",)))
    assert "SUPABASE_URL=\n" in content
    assert "SUPABASE_SERVICE_ROLE_KEY=\n" in content


def test_saved_answers_never_contain_secrets() -> None:
    payload = answers_payload(_answers(agent="hermes", source_app="google_health"))
    serialized = json.dumps(payload).lower()
    assert "service_role" not in serialized
    assert "secret" not in serialized
    assert payload["agent"] == "hermes"
    assert payload["source_app"] == "google_health"
    assert payload["schedule"]["time"] == "09:30"


def test_every_agent_connect_instruction_points_at_the_local_mcp_url() -> None:
    for agent in ("claude_code", "claude", "chatgpt", "codex", "hermes", "openclaw", "other"):
        joined = "\n".join(connect_instructions(agent, remote=False))
        assert "http://127.0.0.1:8791/mcp" in joined


def test_remote_agents_are_pointed_at_the_advanced_docs_not_a_fake_url() -> None:
    joined = "\n".join(connect_instructions("chatgpt", remote=True))
    assert "docs/deployment.md" in joined


def test_setup_wizard_starts_with_the_device_question_and_defers_agent_connection(
    tmp_path, monkeypatch
) -> None:
    runtime_env = tmp_path / "runtime.env"
    answers_file = tmp_path / "onboarding.json"
    monkeypatch.setattr("heavenly_health.cli.DEFAULT_RUNTIME_ENV", runtime_env)
    monkeypatch.setattr("heavenly_health.cli.ONBOARDING_ANSWERS_PATH", answers_file)

    wizard_input = "\n".join(
        [
            "1",  # device: Apple Watch
            "1",  # tracking app: Apple Health
            "1",  # destination: Supabase
            "n",  # no Supabase credentials yet -> status-only until added
            "1",  # agent: Claude Code
            "1",  # agent runs on this computer
            "1",  # frequency: every day
            "1",  # arrival: morning briefing
            "",  # time: default 09:30
            "",  # metrics: accept defaults
            "n",  # do not start the service now
            "y",  # show agent connection steps
            "",  # advanced extras: default no
        ]
    )
    result = runner.invoke(app, ["setup"], input=wizard_input + "\n")

    assert result.exit_code == 0, result.stdout
    device_at = result.stdout.index("device")
    connect_at = result.stdout.index("Connect your AI agent")
    assert device_at < connect_at
    assert "http://127.0.0.1:8791/mcp" in result.stdout

    assert runtime_env.exists()
    assert stat.S_IMODE(runtime_env.stat().st_mode) == 0o600
    assert "SUPABASE_SERVICE_ROLE_KEY=\n" in runtime_env.read_text()

    saved = json.loads(answers_file.read_text())
    assert saved["devices"] == ["apple_watch"]
    assert saved["source_app"] == "apple_health"
    assert saved["agent"] == "claude_code"
