from typer.testing import CliRunner

from heavenly_health.approvals import ApprovalStore
from heavenly_health.cli import app


runner = CliRunner()


def test_setup_preview_shows_the_user_first_two_questions() -> None:
    result = runner.invoke(app, ["setup", "--preview"])

    assert result.exit_code == 0
    assert "Heavenly Health Protocol" in result.stdout
    assert "Your devices and health sources" in result.stdout
    assert "Your AI and data destination" in result.stdout


def test_setup_preview_explains_capability_driven_metrics() -> None:
    result = runner.invoke(app, ["setup", "--preview"])

    assert result.exit_code == 0
    assert "Metrics are shown only for the sources you select" in result.stdout


def test_setup_preview_distinguishes_implemented_provider_routes() -> None:
    result = runner.invoke(app, ["setup", "--preview"])

    assert result.exit_code == 0
    assert "Google Health API v4: implemented native connector" in result.stdout
    assert "Garmin: implemented connector; Developer Program approval required" in result.stdout
    assert "WHOOP and Oura: reviewed specifications" in result.stdout


def test_access_allow_defaults_to_a_non_mutating_preview() -> None:
    result = runner.invoke(app, ["access", "allow", "new.user@example.com"])

    assert result.exit_code == 0
    assert "No Cloudflare policy was changed" in result.stdout
    assert "--apply" in result.stdout


def test_access_allow_preview_shows_the_validated_policy_target(monkeypatch) -> None:
    class FakeClient:
        def policy_summary(self):
            return {
                "account_id": "account-id",
                "application_id": "application-id",
                "policy_id": "policy-id",
                "policy_name": "Private owner allowlist",
                "decision": "allow",
            }

    monkeypatch.setattr(
        "heavenly_health.cli.CloudflareAccessClient.from_environment",
        lambda: FakeClient(),
    )

    result = runner.invoke(app, ["access", "allow", "new.user@example.com"])

    assert result.exit_code == 0
    assert "Private owner allowlist" in result.stdout
    assert "Decision: allow" in result.stdout
    assert "account-id / application-id / policy-id" in result.stdout


def test_access_oauth_plan_prints_only_the_redacted_reconciliation_target(monkeypatch) -> None:
    class FakePlan:
        def summary(self):
            return {
                "application_id": "application-id",
                "application_name": "Heavenly Health MCP",
                "application_type": "self_hosted",
                "domain": "health-mcp.example.com",
                "managed_oauth_enabled": False,
                "exact_owner_identities": 1,
            }

    class FakeClient:
        def managed_oauth_plan(self, public_host: str):
            assert public_host == "health-mcp.example.com"
            return FakePlan()

    monkeypatch.setattr("heavenly_health.cli._managed_oauth_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["access", "oauth", "plan", "--host", "health-mcp.example.com"],
    )

    assert result.exit_code == 0
    assert "Heavenly Health MCP" in result.stdout
    assert "No Cloudflare application was changed" in result.stdout
    assert "owner@example.com" not in result.stdout


def test_access_oauth_apply_is_explicit_and_idempotent(monkeypatch) -> None:
    observed: list[str] = []

    class FakeClient:
        def enable_managed_oauth(self, public_host: str):
            observed.append(public_host)
            return False

    monkeypatch.setattr("heavenly_health.cli._managed_oauth_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["access", "oauth", "apply", "--host", "health-mcp.example.com"],
    )

    assert result.exit_code == 0
    assert observed == ["health-mcp.example.com"]
    assert "already configured" in result.stdout.lower()


def test_agent_run_builds_an_explicit_generic_sandbox_request(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    observed = []

    class FakeSandbox:
        def run(self, spec, *, environ, interactive):
            observed.append((spec, environ, interactive))
            return 0

    monkeypatch.setattr("heavenly_health.cli._agent_sandbox", lambda: FakeSandbox())

    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            "--image",
            "example/codex:1.0",
            "--workspace",
            str(workspace),
            "--write-workspace",
            "--network",
            "bridge",
            "--secret-env",
            "OPENAI_API_KEY",
            "--",
            "codex",
            "--help",
        ],
    )

    assert result.exit_code == 0
    spec, _environ, _interactive = observed[0]
    assert spec.image == "example/codex:1.0"
    assert spec.workspace == workspace
    assert spec.write_workspace is True
    assert spec.network == "bridge"
    assert spec.secret_env == ("OPENAI_API_KEY",)
    assert spec.command == ("codex", "--help")


def test_access_oauth_configure_runtime_reports_only_the_destination(tmp_path, monkeypatch) -> None:
    assertion = tmp_path / "access.jwt"
    assertion.write_text("private-token")
    assertion.chmod(0o600)
    runtime = tmp_path / "runtime.env"
    runtime.write_text("HEAVENLY_TEST=1\n")
    runtime.chmod(0o600)
    observed = []

    def configure(assertion_path, *, public_host, runtime_path, team_domain, audience):
        observed.append((assertion_path, public_host, runtime_path, team_domain, audience))
        return runtime_path

    monkeypatch.setattr(
        "heavenly_health.cli.configure_runtime_from_access_assertion",
        configure,
    )

    result = runner.invoke(
        app,
        [
            "access",
            "oauth",
            "configure-runtime",
            "--assertion-file",
            str(assertion),
            "--runtime-file",
            str(runtime),
            "--host",
            "health-mcp.example.com",
            "--team-domain",
            "https://team.cloudflareaccess.com",
            "--audience",
            "a" * 64,
        ],
    )

    assert result.exit_code == 0
    assert observed == [
        (
            assertion,
            "health-mcp.example.com",
            runtime,
            "https://team.cloudflareaccess.com",
            "a" * 64,
        )
    ]
    assert str(runtime) in "".join(result.stdout.split())
    assert "private-token" not in result.stdout


def test_owner_can_review_and_approve_a_pending_health_mutation_from_cli(tmp_path, monkeypatch) -> None:
    store = ApprovalStore(tmp_path / "approvals")
    proposal = store.propose_health_event(
        {
            "source": "manual",
            "metric_type": "steps",
            "event_at": "2026-07-14T06:00:00Z",
            "value_numeric": 50,
            "value_text": None,
            "unit": "count",
            "source_record_id": "assigned-at-execution",
            "metadata": {"schema_version": "1.0"},
            "is_synthetic": False,
            "ingest_mode": "manual",
        },
        preview={"metric_type": "steps", "value": "50 count"},
    )
    monkeypatch.setattr("heavenly_health.cli._approval_store", lambda: store)
    approval_id = str(proposal["approval_id"])

    shown = runner.invoke(app, ["approval", "show", approval_id])
    approved = runner.invoke(app, ["approval", "approve", approval_id], input="y\n")

    assert shown.exit_code == 0
    assert "50 count" in shown.stdout
    assert "source_record_id" not in shown.stdout
    assert approved.exit_code == 0
    assert "approved" in approved.stdout.lower()
    assert store.get(approval_id)["status"] == "approved"


def test_access_oauth_configure_runtime_refuses_to_infer_trust_from_the_assertion(
    tmp_path,
    monkeypatch,
) -> None:
    """Without an operator-supplied team domain and audience there is nothing to verify against."""
    runner = CliRunner()
    assertion = tmp_path / "access.jwt"
    assertion.write_text("private-token")
    assertion.chmod(0o600)
    runtime = tmp_path / "runtime.env"
    runtime.write_text("HEAVENLY_TEST=1\n")
    runtime.chmod(0o600)
    monkeypatch.delenv("HEAVENLY_CLOUDFLARE_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE", raising=False)

    result = runner.invoke(
        app,
        [
            "access",
            "oauth",
            "configure-runtime",
            "--assertion-file",
            str(assertion),
            "--runtime-file",
            str(runtime),
            "--host",
            "health-mcp.example.com",
        ],
    )

    assert result.exit_code == 1
    assert "--team-domain" in result.stdout
