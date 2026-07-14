"""Polished terminal onboarding for Heavenly Health Protocol."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from heavenly_health.agent_sandbox import AgentSandboxError, AgentSandboxSpec, DockerAgentSandbox
from heavenly_health.approvals import ApprovalError, ApprovalStore, approval_state_path
from heavenly_health.config import ConfigError, LocalConfigStore, VALID_RUNTIMES, default_config_path
from heavenly_health.runtime.manager import RuntimeConflictError, RuntimeManager
from heavenly_health.runtime.docker import discover_compose_project_root
from heavenly_health.runtime.launchd import LaunchdRuntime
from heavenly_health.runtime.manager import _listener_active
from heavenly_health.cloudflare_access import (
    CloudflareAccessClient,
    CloudflareAccessConfigurationError,
    CloudflareManagedOAuthClient,
    normalize_email,
)
from heavenly_health.cloudflare_managed_oauth import (
    CloudflareManagedOAuthError,
    configure_runtime_from_access_assertion,
)
from heavenly_health.launcher import DEFAULT_RUNTIME_ENV
from heavenly_health.health_storage import HealthStorageError, SupabaseHealthStore, SupabaseSettings
from heavenly_health.providers.common import ProviderConfigurationError
from heavenly_health.providers.runtime import ProviderRuntime
from heavenly_health.secret_loader import SecretFileError, load_runtime_environment

app = typer.Typer(
    add_completion=False,
    help="Private, LLM-agnostic health-data setup.",
    no_args_is_help=True,
)
console = Console()
access_app = typer.Typer(
    help="Manage a preconfigured Cloudflare Access email allowlist.",
    no_args_is_help=True,
)
access_oauth_app = typer.Typer(
    help="Plan or reconcile Cloudflare Managed OAuth for an MCP application.",
    no_args_is_help=True,
)
runtime_app = typer.Typer(help="Inspect or select the local execution runtime.", no_args_is_help=True)
approval_app = typer.Typer(help="Review and confirm staged health mutations.", no_args_is_help=True)
agent_app = typer.Typer(
    help="Run any CLI agent in an explicitly constrained Docker container.",
    no_args_is_help=True,
)
provider_app = typer.Typer(
    help="Connect, synchronize, inspect, or disconnect health data providers.",
    no_args_is_help=True,
)
google_provider_app = typer.Typer(
    help="Manage the Google Health API v4 connector.",
    no_args_is_help=True,
)
garmin_provider_app = typer.Typer(
    help="Manage an approved Garmin Connect Health API integration.",
    no_args_is_help=True,
)
app.add_typer(access_app, name="access")
access_app.add_typer(access_oauth_app, name="oauth")
app.add_typer(runtime_app, name="runtime")
app.add_typer(approval_app, name="approval")
app.add_typer(agent_app, name="agent")
app.add_typer(provider_app, name="provider")
provider_app.add_typer(google_provider_app, name="google-health")
provider_app.add_typer(garmin_provider_app, name="garmin")


@app.callback()
def main() -> None:
    """Private, LLM-agnostic health-data setup."""


def _runtime_store() -> LocalConfigStore:
    return LocalConfigStore(default_config_path())


def _approval_store() -> ApprovalStore:
    return ApprovalStore(approval_state_path(os.environ))


def _managed_oauth_client() -> CloudflareManagedOAuthClient:
    return CloudflareManagedOAuthClient.from_environment()


def _agent_sandbox() -> DockerAgentSandbox:
    return DockerAgentSandbox()


def _provider_runtime() -> ProviderRuntime:
    return ProviderRuntime()


def _load_cli_runtime_environment() -> None:
    configured = os.environ.get("HEAVENLY_SECRET_FILE", "").strip()
    runtime_file = Path(configured).expanduser() if configured else DEFAULT_RUNTIME_ENV
    if configured or runtime_file.exists():
        load_runtime_environment(runtime_file)


def _configured_health_store() -> SupabaseHealthStore:
    _load_cli_runtime_environment()
    settings = SupabaseSettings.from_environ(os.environ)
    if settings is None:
        raise ProviderConfigurationError(
            "Supabase storage is required before provider synchronization"
        )
    return SupabaseHealthStore(settings, provider_runtime=_provider_runtime())


def _provider_output(action: Callable[[], object]) -> None:
    try:
        result = action()
    except (ProviderConfigurationError, HealthStorageError, SecretFileError) as error:
        console.print(f"[red]Provider operation failed: {error}[/red]")
        raise typer.Exit(code=1) from error
    console.print(json.dumps(result, indent=2, sort_keys=True))


@provider_app.command("status")
def provider_status() -> None:
    """Show redacted connection and synchronization status."""
    _provider_output(lambda: {"providers": _provider_runtime().statuses()})


@google_provider_app.command("import-client")
def google_import_client(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Import one owner-only Google Web OAuth client JSON into the system vault."""
    _provider_output(lambda: _provider_runtime().import_google_client(path))


@google_provider_app.command("connect")
def google_connect() -> None:
    """Authorize Google Health through a one-shot loopback OAuth callback."""
    def connect() -> object:
        store = _configured_health_store()
        return _provider_runtime().connect_google(store.settings.allowed_metrics)

    _provider_output(connect)


@google_provider_app.command("sync")
def google_sync(
    limit: int = typer.Option(1000, min=1, max=10_000),
) -> None:
    """Synchronize a bounded Google Health window into configured storage."""
    _provider_output(
        lambda: _provider_runtime().sync(
            "google_health",
            _configured_health_store(),
            limit=limit,
        )
    )


@google_provider_app.command("disconnect")
def google_disconnect(
    yes: bool = typer.Option(False, "--yes", help="Confirm revocation and local token deletion."),
    remove_client: bool = typer.Option(
        False,
        "--remove-client",
        help="Also remove the reusable Google OAuth client from the system vault.",
    ),
) -> None:
    """Revoke the Google grant and remove local connection state."""
    if not yes and not typer.confirm("Revoke Google Health access and delete the local token?"):
        raise typer.Exit(code=1)
    _provider_output(
        lambda: _provider_runtime().disconnect_google(remove_client=remove_client)
    )


@garmin_provider_app.command("import-client")
def garmin_import_client(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Import owner-only Garmin partner OAuth/API JSON into the system vault."""
    _provider_output(lambda: _provider_runtime().import_garmin_client(path))


@garmin_provider_app.command("connect")
def garmin_connect() -> None:
    """Authorize Garmin through a one-shot loopback OAuth callback."""
    def connect() -> object:
        store = _configured_health_store()
        return _provider_runtime().connect_garmin(store.settings.allowed_metrics)

    _provider_output(connect)


@garmin_provider_app.command("sync")
def garmin_sync(
    limit: int = typer.Option(1000, min=1, max=10_000),
) -> None:
    """Synchronize a bounded Garmin Health window into configured storage."""
    _provider_output(
        lambda: _provider_runtime().sync(
            "garmin",
            _configured_health_store(),
            limit=limit,
        )
    )


@garmin_provider_app.command("disconnect")
def garmin_disconnect(
    yes: bool = typer.Option(False, "--yes", help="Confirm revocation and local token deletion."),
    remove_client: bool = typer.Option(
        False,
        "--remove-client",
        help="Also remove the reusable Garmin partner client from the system vault.",
    ),
) -> None:
    """Revoke Garmin access where configured and remove local connection state."""
    if not yes and not typer.confirm("Disconnect Garmin and delete the local token?"):
        raise typer.Exit(code=1)
    _provider_output(
        lambda: _provider_runtime().disconnect_garmin(remove_client=remove_client)
    )


def _managed_oauth_host(host: str | None) -> str:
    resolved = (host or os.environ.get("HEAVENLY_MCP_PUBLIC_HOST", "")).strip()
    if not resolved:
        raise CloudflareAccessConfigurationError(
            "HEAVENLY_MCP_PUBLIC_HOST is missing; pass --host or set it in the environment"
        )
    return resolved


def _runtime_manager() -> RuntimeManager:
    launchd = _launchd_runtime()
    return RuntimeManager(
        _runtime_store(),
        Path.home() / ".local" / "state" / "heavenly" / "native-mcp.json",
        discover_compose_project_root(Path(__file__)),
        native_runtime=launchd if launchd is not None and launchd.is_installed() else None,
    )


def _launchd_runtime() -> LaunchdRuntime | None:
    if sys.platform != "darwin":
        return None
    executable = shutil.which("heavenly-mcp")
    if executable is None:
        return None
    return LaunchdRuntime(
        plist_path=Path.home() / "Library" / "LaunchAgents" / "com.heavenly-health.mcp.plist",
        executable=Path(executable),
        log_directory=Path.home() / ".local" / "state" / "heavenly",
        listener_active=_listener_active,
    )


def _handle_approval_error(error: ApprovalError) -> NoReturn:
    console.print(f"[red]Approval operation failed: {error}[/red]")
    raise typer.Exit(code=1) from error


@approval_app.command("list")
def approval_list(limit: int = typer.Option(20, min=1, max=200)) -> None:
    """List recent staged mutation previews and their state."""
    try:
        history = _approval_store().audit_history(limit=limit)
    except ApprovalError as error:
        _handle_approval_error(error)
    console.print(json.dumps({"mutations": history, "count": len(history)}, indent=2))


@approval_app.command("show")
def approval_show(approval_id: str) -> None:
    """Show one redacted mutation preview before owner confirmation."""
    try:
        record = _approval_store().review(approval_id)
    except ApprovalError as error:
        _handle_approval_error(error)
    console.print(json.dumps(record, indent=2))


@approval_app.command("approve")
def approval_approve(approval_id: str) -> None:
    """Confirm one exact staged mutation from the owner's local terminal."""
    try:
        store = _approval_store()
        record = store.review(approval_id)
    except ApprovalError as error:
        _handle_approval_error(error)
    console.print(json.dumps(record, indent=2))
    if not typer.confirm("Approve this exact health mutation?"):
        console.print("Mutation was not approved.")
        raise typer.Exit(code=1)
    try:
        approved = store.approve(approval_id)
    except ApprovalError as error:
        _handle_approval_error(error)
    console.print(f"[green]Approved:[/green] {approved['approval_id']}")


@approval_app.command("reject")
def approval_reject(approval_id: str) -> None:
    """Reject a staged mutation so it cannot execute."""
    try:
        rejected = _approval_store().reject(approval_id)
    except ApprovalError as error:
        _handle_approval_error(error)
    console.print(f"[yellow]Rejected:[/yellow] {rejected['approval_id']}")


def _report_config_error(error: ConfigError) -> None:
    console.print(
        f"[red]Cannot use runtime configuration: {error}.[/red]\n"
        f"Repair the configuration at [bold]{error.path}[/bold] or remove it and rerun the command."
    )


@runtime_app.command("show")
def runtime_show() -> None:
    """Show the currently selected local runtime."""
    try:
        console.print(_runtime_store().load().runtime)
    except ConfigError as error:
        _report_config_error(error)
        raise typer.Exit(code=1) from error


@runtime_app.command("use")
def runtime_use(runtime: str) -> None:
    """Select the local execution runtime."""
    try:
        configuration = _runtime_store().set_runtime(runtime)
    except ConfigError as error:
        _report_config_error(error)
        raise typer.Exit(code=1) from error
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="runtime") from error
    console.print(f"Runtime set to: {configuration.runtime}")


@runtime_app.command("install-service")
def runtime_install_service() -> None:
    """Install and start native Heavenly as a persistent macOS user service."""
    launchd = _launchd_runtime()
    if launchd is None:
        _handle_lifecycle_error(RuntimeError("Persistent native service installation requires macOS and heavenly-mcp"))
    try:
        path = launchd.install()
        result = launchd.start()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        _handle_lifecycle_error(error)
    console.print(f"Installed native service at {path} (state: {result.state}, PID: {result.pid}).")


def _handle_lifecycle_error(error: Exception) -> NoReturn:
    console.print(f"[red]Runtime operation failed: {error}[/red]\nCheck runtime prerequisites and retry; run heavenly runtime status for details.")
    raise typer.Exit(code=1) from error


def _runtime_override(value: str | None) -> str | None:
    if value is not None and value not in VALID_RUNTIMES:
        raise typer.BadParameter(f"Unsupported runtime: {value}")
    return value


@runtime_app.command("start")
def runtime_start(
    runtime: str | None = typer.Option(
        None, "--runtime", callback=_runtime_override, help="Run native or docker without changing the selection."
    ),
) -> None:
    """Start the selected MCP service mode, or the explicit override."""
    try:
        result = _runtime_manager().start(runtime)
    except (ConfigError, RuntimeConflictError, RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        _handle_lifecycle_error(error)
    console.print(f"Started {result.runtime} MCP service ({result.state}).")


@runtime_app.command("stop")
def runtime_stop(
    runtime: str | None = typer.Option(
        None, "--runtime", callback=_runtime_override, help="Stop native or docker without changing the selection."
    ),
) -> None:
    """Stop only the selected Heavenly MCP service mode."""
    try:
        result = _runtime_manager().stop(runtime)
    except (ConfigError, RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        _handle_lifecycle_error(error)
    suffix = f" PID {result.pid}" if result.pid is not None else ""
    console.print(f"Stopped {result.runtime} MCP service ({result.state}).{suffix}")


@runtime_app.command("status")
def runtime_status() -> None:
    """Show selected mode and the independent native/Docker lifecycle states."""
    try:
        result = _runtime_manager().status()
    except (ConfigError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        _handle_lifecycle_error(error)
    console.print(f"Selected runtime: {result.selected}")
    console.print(f"Native: {result.native.state}" + (f" (PID {result.native.pid})" if result.native.pid else ""))
    console.print(f"Docker: {result.docker.state}" + (f" ({result.docker.identity})" if result.docker.identity else ""))
    if result.docker.detail:
        console.print(f"Docker detail: {result.docker.detail}")
    console.print(f"Listener: {result.listener or 'none'}")
    if result.conflict:
        console.print(f"[yellow]{result.conflict}[/yellow]")


@access_app.command("allow")
def access_allow(
    email: str,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Make the Cloudflare API change. Without this flag, only show a safe preview.",
    ),
) -> None:
    """Add one exact email to the configured Cloudflare Access policy."""
    try:
        normalized_email = normalize_email(email)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="email") from error

    if not apply:
        target = "Target policy: not resolved; configure runtime IDs/token to validate it before --apply."
        try:
            summary = CloudflareAccessClient.from_environment().policy_summary()
        except CloudflareAccessConfigurationError:
            pass
        except RuntimeError as error:
            console.print(f"[red]{error}[/red]")
            raise typer.Exit(code=1) from error
        else:
            target = (
                f"Target: {summary['account_id']} / {summary['application_id']} / {summary['policy_id']}\n"
                f"Policy: {summary['policy_name']}\nDecision: {summary['decision']}"
            )
        console.print(
            Panel(
                f"Would add [bold]{normalized_email}[/bold] to the configured Cloudflare Access allowlist.\n"
                f"{target}\n"
                "No Cloudflare policy was changed. Re-run with [bold]--apply[/bold] after review.",
                title="Cloudflare Access preview",
                border_style="yellow",
            )
        )
        return

    try:
        changed = CloudflareAccessClient.from_environment().allow_email(normalized_email)
    except CloudflareAccessConfigurationError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error
    except RuntimeError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    if changed:
        console.print(f"[green]Allowed:[/green] {normalized_email}")
    else:
        console.print(f"[cyan]Already allowed:[/cyan] {normalized_email}")


@access_oauth_app.command("plan")
def access_oauth_plan(
    host: str | None = typer.Option(None, "--host", help="Exact protected MCP hostname."),
) -> None:
    """Validate the target and print a redacted non-mutating reconciliation plan."""
    try:
        plan = _managed_oauth_client().managed_oauth_plan(_managed_oauth_host(host))
    except (CloudflareAccessConfigurationError, RuntimeError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error
    console.print(json.dumps(plan.summary(), indent=2))
    console.print("No Cloudflare application was changed. Run `heavenly access oauth apply` to reconcile it.")


@access_oauth_app.command("apply")
def access_oauth_apply(
    host: str | None = typer.Option(None, "--host", help="Exact protected MCP hostname."),
) -> None:
    """Idempotently enable Managed OAuth and local-client redirect support."""
    try:
        changed = _managed_oauth_client().enable_managed_oauth(_managed_oauth_host(host))
    except (CloudflareAccessConfigurationError, RuntimeError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error
    if changed:
        console.print("[green]Cloudflare Managed OAuth configured.[/green]")
    else:
        console.print("[cyan]Cloudflare Managed OAuth is already configured.[/cyan]")


@access_oauth_app.command("configure-runtime")
def access_oauth_configure_runtime(
    assertion_file: Path = typer.Option(
        ...,
        "--assertion-file",
        help="Owner-only file containing a current Cloudflare Access JWT.",
    ),
    host: str | None = typer.Option(None, "--host", help="Exact protected MCP hostname."),
    runtime_file: Path = typer.Option(
        DEFAULT_RUNTIME_ENV,
        "--runtime-file",
        help="Existing owner-only runtime environment file.",
    ),
) -> None:
    """Verify an Access JWT and persist its origin trust settings without displaying them."""
    try:
        destination = configure_runtime_from_access_assertion(
            assertion_file,
            public_host=_managed_oauth_host(host),
            runtime_path=runtime_file,
        )
    except (CloudflareAccessConfigurationError, CloudflareManagedOAuthError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error
    console.print(f"[green]Verified Cloudflare origin settings written to {destination}.[/green]")


@agent_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def agent_run(
    context: typer.Context,
    image: str = typer.Option(..., "--image", help="OCI image containing the chosen CLI agent."),
    workspace: Path = typer.Option(
        Path.cwd(),
        "--workspace",
        help="Only host project directory exposed to the agent.",
    ),
    write_workspace: bool = typer.Option(
        False,
        "--write-workspace",
        help="Explicitly let the container modify the selected workspace.",
    ),
    network: str = typer.Option(
        "none",
        "--network",
        help="Docker network: none by default; use bridge or a named network explicitly.",
    ),
    secret_env: list[str] = typer.Option(
        [],
        "--secret-env",
        help="Repeat for each host environment secret explicitly granted to the container.",
    ),
    state_dir: Path | None = typer.Option(
        None,
        "--state-dir",
        help="Optional private mode-0700 directory for persistent agent login/config state.",
    ),
    user: str = typer.Option(
        "1000:1000",
        "--user",
        help="Numeric non-root container UID:GID.",
    ),
) -> None:
    """Launch a user-supplied agent image; put its command after `--`."""
    spec = AgentSandboxSpec(
        image=image,
        workspace=workspace,
        command=tuple(context.args),
        network=network,
        write_workspace=write_workspace,
        secret_env=tuple(secret_env),
        state_dir=state_dir,
        user=user,
    )
    try:
        returncode = _agent_sandbox().run(
            spec,
            environ=os.environ,
            interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        )
    except AgentSandboxError as error:
        console.print(f"[red]Agent sandbox failed: {error}[/red]")
        raise typer.Exit(code=1) from error
    if returncode != 0:
        raise typer.Exit(code=returncode)


def _heading(step: int, title: str) -> None:
    console.print(f"\n[bold cyan][{step}/6][/bold cyan] [bold]{title}[/bold]")


@app.command()
def setup(
    preview: bool = typer.Option(False, "--preview", help="Show the onboarding design without collecting data."),
) -> None:
    """Start privacy-first health-data onboarding."""
    if not preview:
        console.print(
            Panel(
                "Interactive setup is the next implementation slice.\n"
                "Use [bold]heavenly setup --preview[/bold] to review the design now.\n"
                "Implemented Google Health and Garmin operations are under "
                "[bold]heavenly provider[/bold].",
                title="Heavenly Health Protocol",
                border_style="cyan",
            )
        )
        raise typer.Exit()

    console.print(
        Panel(
            Text("Private health data, under your control.", style="bold white"),
            title="[bold cyan]Heavenly Health Protocol[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    _heading(1, "Your devices and health sources")
    console.print("[dim]Select the sources you use. Multiple sources are supported.[/dim]")
    console.print("  Apple Health  •  WHOOP  •  Oura  •  Fitbit  •  Garmin  •  Android Health Connect")

    _heading(2, "Your AI and data destination")
    console.print("[dim]Choose any compatible agent and where normalized health data should live.[/dim]")
    console.print("  Claude Code  •  Codex  •  Hermes  •  OpenClaw  •  ChatGPT  •  Claude  •  Perplexity")
    console.print("  Obsidian  •  Local second brain  •  Supabase  •  Google Drive  •  iCloud Drive")

    _heading(3, "What should Heavenly deliver?")
    console.print("  Analysis only  •  Current-day plan  •  Next-day plan  •  Weekly review")

    _heading(4, "Schedule")
    console.print("  Timezone is detected from your operating system and is always editable.")
    console.print("  Default: daily morning briefing at 09:30 local time.")

    _heading(5, "Tracking permissions")
    console.print("[bold]Metrics are shown only for the sources you select.[/bold]")
    console.print("  Heavenly requests the minimum provider scopes needed for selected metrics.")
    console.print("  Clinical records, medication, reproductive data, ECG, and routes stay off by default.")

    _heading(6, "Provider requirements")
    console.print("  Google Health API v4: implemented native connector.")
    console.print("  Garmin: implemented connector; Developer Program approval required.")
    console.print("  WHOOP and Oura: reviewed specifications; adapters are not implemented.")


if __name__ == "__main__":
    app()
