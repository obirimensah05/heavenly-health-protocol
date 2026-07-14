from __future__ import annotations

import plistlib
import subprocess

from heavenly_health.runtime.launchd import LaunchdRuntime


class FakeLaunchctl:
    def __init__(self) -> None:
        self.loaded = False
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[1] == "print":
            return subprocess.CompletedProcess(
                command,
                0 if self.loaded else 113,
                stdout="pid = 4242\nstate = running\n" if self.loaded else "",
                stderr="",
            )
        if command[1] == "bootstrap":
            self.loaded = True
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "kickstart":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "bootout":
            self.loaded = False
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)


def test_install_writes_owner_only_launch_agent_without_secret_values(tmp_path) -> None:
    plist_path = tmp_path / "LaunchAgents" / "com.heavenly-health.mcp.plist"
    runtime = LaunchdRuntime(
        plist_path=plist_path,
        executable=tmp_path / "bin" / "heavenly-mcp",
        log_directory=tmp_path / "logs",
        runner=FakeLaunchctl(),
        listener_active=lambda: False,
    )

    result = runtime.install()

    assert result == plist_path
    assert plist_path.stat().st_mode & 0o777 == 0o600
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == "com.heavenly-health.mcp"
    assert payload["ProgramArguments"] == [str(tmp_path / "bin" / "heavenly-mcp")]
    assert payload["KeepAlive"] is True
    assert payload["RunAtLoad"] is True
    assert "EnvironmentVariables" not in payload
    assert "secret" not in plist_path.read_text(encoding="utf-8").lower()


def test_launchd_runtime_starts_reports_pid_and_stops_only_its_label(tmp_path) -> None:
    launchctl = FakeLaunchctl()
    listener = False

    def listener_active() -> bool:
        return listener

    def sleeper(_seconds: float) -> None:
        nonlocal listener
        listener = launchctl.loaded

    runtime = LaunchdRuntime(
        plist_path=tmp_path / "com.heavenly-health.mcp.plist",
        executable=tmp_path / "heavenly-mcp",
        log_directory=tmp_path / "logs",
        runner=launchctl,
        listener_active=listener_active,
        sleeper=sleeper,
    )
    runtime.install()

    started = runtime.start()
    status = runtime.status()
    stopped = runtime.stop()

    assert started.state == "running" and started.pid == 4242
    assert status.state == "running" and status.pid == 4242
    assert stopped.state == "stopped" and stopped.pid == 4242
    assert [command[1] for command in launchctl.commands].count("bootstrap") == 1
    assert any(
        command == ["launchctl", "bootout", f"gui/{runtime.uid}/{runtime.label}"]
        for command in launchctl.commands
    )


def test_launchd_runtime_fails_when_job_never_claims_listener(tmp_path) -> None:
    launchctl = FakeLaunchctl()
    runtime = LaunchdRuntime(
        plist_path=tmp_path / "com.heavenly-health.mcp.plist",
        executable=tmp_path / "heavenly-mcp",
        log_directory=tmp_path / "logs",
        runner=launchctl,
        listener_active=lambda: False,
        sleeper=lambda _seconds: None,
        startup_attempts=2,
    )
    runtime.install()

    try:
        runtime.start()
    except RuntimeError as error:
        assert "did not become ready" in str(error)
    else:
        raise AssertionError("launchd readiness failure was accepted")

    assert launchctl.loaded is False
