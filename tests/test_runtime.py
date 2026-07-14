"""Lifecycle behavior for the local native and Docker MCP service modes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from heavenly_health.cli import app
from heavenly_health.config import LocalConfigStore, RuntimeConfiguration
from heavenly_health.runtime.manager import RuntimeConflictError, RuntimeManager, RuntimeStatus, _process_identity
from heavenly_health.runtime.docker import DOCKER_START_TIMEOUT_SECONDS, DockerStatus, discover_compose_project_root
from heavenly_health.runtime.native import NativeRuntime, NativeStatus


@dataclass
class Completed:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class RecordingRunner:
    def __init__(self, responses: list[Completed] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.responses = responses or []

    def __call__(self, args: list[str], **kwargs: Any) -> Completed:
        self.calls.append((args, kwargs))
        return self.responses.pop(0) if self.responses else Completed()


class FakeProcess:
    pid = 4815


class RecordingPopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        return FakeProcess()


def native_state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "native.json"


def heavenly_identity(*arguments: str) -> str:
    return json.dumps(
        {
            "arguments": list(arguments or ("heavenly-mcp",)),
            "executable": "/usr/local/bin/python",
            "start": "test-start-time",
        },
        sort_keys=True,
    )


def test_start_uses_selected_runtime_by_default(tmp_path: Path) -> None:
    store = LocalConfigStore(tmp_path / "runtime.json")
    store.save(RuntimeConfiguration(runtime="docker"))
    runner = RecordingRunner()
    manager = RuntimeManager(store, native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False)

    result = manager.start()

    assert result.runtime == "docker"
    assert runner.calls[0][0][-4:] == ["ps", "--format", "json", "heavenly-mcp"]
    assert runner.calls[1][0][-6:] == [
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        str(DOCKER_START_TIMEOUT_SECONDS),
        "heavenly-mcp",
    ]


def test_override_selects_requested_runtime_without_mutating_selection(tmp_path: Path) -> None:
    store = LocalConfigStore(tmp_path / "runtime.json")
    store.save(RuntimeConfiguration(runtime="docker"))
    popen = RecordingPopen()
    listener_states = iter([False, True])
    manager = RuntimeManager(
        store,
        native_state_path(tmp_path),
        tmp_path,
        popen=popen,
        runner=RecordingRunner(),
        listener_active=lambda: next(listener_states),
        process_identity=lambda _pid: heavenly_identity(),
    )

    result = manager.start(runtime="native")

    assert result.runtime == "native"
    assert store.load().runtime == "docker"
    assert popen.calls[0][0] == ["heavenly-mcp"]


def test_native_start_records_owned_pid_waits_for_listener_and_stop_only_kills_matching_identity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-propagate")
    monkeypatch.setenv("HEAVENLY_OIDC_CLIENT_SECRET", "required-by-mcp")
    store = LocalConfigStore(tmp_path / "runtime.json")
    popen = RecordingPopen()
    listener_states = iter([False, True, True, False])
    stopped: list[int] = []
    manager = RuntimeManager(
        store,
        native_state_path(tmp_path),
        tmp_path,
        popen=popen,
        runner=RecordingRunner(),
        listener_active=lambda: next(listener_states),
        process_identity=lambda _pid: None if stopped else heavenly_identity(),
        terminate=lambda pid: stopped.append(pid),
        sleeper=lambda _seconds: None,
    )

    started = manager.start(runtime="native")
    status = manager.status()
    record = json.loads(native_state_path(tmp_path).read_text())
    stopped_result = manager.stop(runtime="native")

    assert started.pid == 4815
    assert record == {
        "command": ["heavenly-mcp"],
        "identity": heavenly_identity(),
        "pid": 4815,
    }
    assert status.native.state == "running"
    assert status.native.pid == 4815
    assert stopped_result.state == "stopped"
    assert stopped == [4815]
    assert not native_state_path(tmp_path).exists()
    assert popen.calls[0][1]["shell"] is False
    assert popen.calls[0][1]["start_new_session"] is True
    assert popen.calls[0][1]["env"]["HEAVENLY_OIDC_CLIENT_SECRET"] == "required-by-mcp"
    assert "UNRELATED_SECRET" not in popen.calls[0][1]["env"]


def test_native_start_allows_cold_listener_more_than_twenty_readiness_checks(tmp_path: Path) -> None:
    readiness_checks = 0
    stopped: list[int] = []

    def listener_active() -> bool:
        nonlocal readiness_checks
        readiness_checks += 1
        return readiness_checks > 25

    runtime = NativeRuntime(
        native_state_path(tmp_path),
        popen=RecordingPopen(),
        listener_active=listener_active,
        process_identity=lambda _pid: heavenly_identity(),
        terminate=lambda pid: stopped.append(pid),
        sleeper=lambda _seconds: None,
    )

    result = runtime.start()

    assert result.state == "running"
    assert result.pid == 4815
    assert readiness_checks == 26
    assert stopped == []


def test_native_stop_refuses_stale_or_unowned_pid_without_signalling_it(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "old", "command": ["heavenly-mcp"]}')
    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"),
        state_path,
        tmp_path,
        runner=RecordingRunner(),
        listener_active=lambda: False,
        process_identity=lambda _pid: "new",
        terminate=lambda pid: stopped.append(pid),
    )

    result = manager.stop(runtime="native")

    assert result.state == "stale"
    assert stopped == []
    assert not state_path.exists()


def test_docker_commands_target_only_project_service_and_preserve_volume(tmp_path: Path) -> None:
    runner = RecordingRunner([Completed(stdout='[{"Name":"project-heavenly-mcp-1","State":"running"}]'), Completed()])
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    status = manager.status()
    stopped = manager.stop(runtime="docker")

    assert status.docker.state == "running"
    assert status.docker.identity == "project-heavenly-mcp-1"
    assert runner.calls[0][0] == ["docker", "compose", "-f", str(tmp_path / "compose.yaml"), "ps", "--format", "json", "heavenly-mcp"]
    assert runner.calls[1][0] == ["docker", "compose", "-f", str(tmp_path / "compose.yaml"), "stop", "heavenly-mcp"]
    assert "down" not in runner.calls[1][0]
    assert "rm" not in runner.calls[1][0]
    assert stopped.state == "stopped"
    assert runner.calls[0][1]["shell"] is False


def test_docker_start_reports_native_listener_conflict_before_compose(tmp_path: Path) -> None:
    runner = RecordingRunner()
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: True
    )

    with pytest.raises(RuntimeConflictError, match="native listener.*127.0.0.1:8791.*runtime stop --runtime native"):
        manager.start(runtime="docker")

    assert runner.calls[0][0][-4:] == ["ps", "--format", "json", "heavenly-mcp"]
    assert len(runner.calls) == 1


@pytest.mark.parametrize("state", ["running", "restarting"])
def test_docker_start_is_idempotent_when_compose_service_is_already_active(tmp_path: Path, state: str) -> None:
    runner = RecordingRunner(
        [Completed(stdout=json.dumps([{"ID": "a1b2c3d4e5f6", "Name": "project-heavenly-mcp-1", "State": state}]))]
    )
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: True
    )

    result = manager.start(runtime="docker")

    assert result.runtime == "docker"
    assert result.state == state
    assert len(runner.calls) == 1
    assert runner.calls[0][0][-4:] == ["ps", "--format", "json", "heavenly-mcp"]


def test_docker_start_recovers_a_created_container_by_running_compose_up(tmp_path: Path) -> None:
    runner = RecordingRunner(
        [
            Completed(stdout=json.dumps([{"ID": "a1b2c3d4e5f6", "Name": "project-heavenly-mcp-1", "State": "created"}])),
            Completed(stdout=""),
        ]
    )
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    result = manager.start(runtime="docker")

    assert result.runtime == "docker"
    assert result.state == "running"
    assert runner.calls[1][0][-6:] == [
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        str(DOCKER_START_TIMEOUT_SECONDS),
        "heavenly-mcp",
    ]


def test_docker_status_exposes_container_id_and_optional_readable_name(tmp_path: Path) -> None:
    runner = RecordingRunner(
        [Completed(stdout='[{"ID":"a1b2c3d4e5f6","Name":"project-heavenly-mcp-1","State":"running"}]')]
    )
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    status = manager.status()

    assert status.docker.container_id == "a1b2c3d4e5f6"
    assert status.docker.name == "project-heavenly-mcp-1"
    assert status.docker.identity is not None
    assert "a1b2c3d4e5f6" in status.docker.identity
    assert "project-heavenly-mcp-1" in status.docker.identity


def test_native_stop_revalidates_identity_immediately_before_signalling(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "owned", "command": ["heavenly-mcp"]}')
    identities = iter(["owned", "replaced"])
    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"),
        state_path,
        tmp_path,
        runner=RecordingRunner(),
        listener_active=lambda: False,
        process_identity=lambda _pid: next(identities),
        terminate=lambda pid: stopped.append(pid),
    )

    result = manager.stop(runtime="native")

    assert result.state == "stale"
    assert stopped == []
    assert not state_path.exists()


def test_process_identity_captures_process_instance_and_command_identity() -> None:
    identity = _process_identity(os.getpid())

    if identity is None:
        pytest.skip("the sandbox does not permit the macOS process inspection needed for a safe identity")
    assert identity is not None
    payload = json.loads(identity)
    assert payload["start"]
    assert payload["executable"]
    assert payload["arguments"]


def test_native_start_reports_running_docker_conflict_before_spawning(tmp_path: Path) -> None:
    runner = RecordingRunner([Completed(stdout='[{"Name":"project-heavenly-mcp-1","State":"running"}]')])
    popen = RecordingPopen()
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, popen=popen, listener_active=lambda: False
    )

    with pytest.raises(RuntimeConflictError, match="Docker service.*runtime stop --runtime docker"):
        manager.start(runtime="native")

    assert popen.calls == []


def test_status_reports_selection_both_modes_listener_and_actionable_conflict(tmp_path: Path) -> None:
    store = LocalConfigStore(tmp_path / "runtime.json")
    store.save(RuntimeConfiguration(runtime="docker"))
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "native", "command": ["heavenly-mcp"]}')
    runner = RecordingRunner([Completed(stdout='[{"Name":"project-heavenly-mcp-1","State":"running"}]')])
    manager = RuntimeManager(
        store, state_path, tmp_path, runner=runner, listener_active=lambda: True, process_identity=lambda _pid: "native"
    )

    status = manager.status()

    assert status.selected == "docker"
    assert status.native.pid == 4815
    assert status.docker.identity == "project-heavenly-mcp-1"
    assert status.listener == "127.0.0.1:8791"
    assert status.conflict is not None
    assert "both native and Docker" in status.conflict


def test_cli_start_override_and_conflict_error_are_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeManager:
        def __init__(self) -> None:
            self.started: list[str | None] = []

        def start(self, runtime: str | None = None):
            self.started.append(runtime)
            raise RuntimeConflictError("Docker service is active; run heavenly runtime stop --runtime docker first.")

    manager = FakeManager()
    monkeypatch.setattr("heavenly_health.cli._runtime_manager", lambda: manager)

    result = CliRunner().invoke(app, ["runtime", "start", "--runtime", "native"])

    assert result.exit_code == 1
    assert manager.started == ["native"]
    assert "runtime stop --runtime docker" in " ".join(result.stdout.split())


def test_cli_status_renders_selected_modes_identity_listener_and_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeManager:
        def status(self) -> RuntimeStatus:
            return RuntimeStatus(
                selected="docker",
                native=NativeStatus("running", 4815),
                docker=DockerStatus("running", container_id="a1b2c3d4e5f6", name="project-heavenly-mcp-1"),
                listener="127.0.0.1:8791",
                conflict="Conflict: both native and Docker Heavenly services are active; stop one before starting another.",
            )

    monkeypatch.setattr("heavenly_health.cli._runtime_manager", FakeManager)

    result = CliRunner().invoke(app, ["runtime", "status"])

    assert result.exit_code == 0
    assert "Selected runtime: docker" in result.stdout
    assert "PID 4815" in result.stdout
    assert "project-heavenly-mcp-1" in result.stdout
    assert "127.0.0.1:8791" in result.stdout
    assert "both native and Docker" in result.stdout


def test_native_start_cleans_up_spawned_process_when_identity_capture_fails(tmp_path: Path) -> None:
    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path,
        popen=RecordingPopen(), runner=RecordingRunner(), listener_active=lambda: False,
        process_identity=lambda _pid: None, terminate=lambda pid: stopped.append(pid),
    )

    with pytest.raises(RuntimeError, match="could not be recorded"):
        manager.start(runtime="native")

    assert stopped == [4815]
    assert not native_state_path(tmp_path).exists()


def test_native_stop_waits_for_exit_before_removing_ownership_state(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "owned", "command": ["heavenly-mcp"]}')

    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), state_path, tmp_path, runner=RecordingRunner(),
        listener_active=lambda: True, process_identity=lambda _pid: "owned",
        terminate=lambda pid: stopped.append(pid), sleeper=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="did not exit"):
        manager.stop(runtime="native")

    assert stopped == [4815]
    assert state_path.exists()


def test_native_stop_signal_failure_retains_ownership_state(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "owned", "command": ["heavenly-mcp"]}')
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), state_path, tmp_path, runner=RecordingRunner(),
        listener_active=lambda: True, process_identity=lambda _pid: "owned",
        terminate=lambda _pid: (_ for _ in ()).throw(PermissionError("not permitted")),
    )

    with pytest.raises(RuntimeError, match="(?i)could not signal"):
        manager.stop(runtime="native")

    assert state_path.exists()


def test_native_stop_treats_process_lookup_as_already_stopped(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "owned", "command": ["heavenly-mcp"]}')
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), state_path, tmp_path, runner=RecordingRunner(),
        listener_active=lambda: False, process_identity=lambda _pid: "owned",
        terminate=lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
    )

    result = manager.stop(runtime="native")

    assert result.state == "stopped"
    assert not state_path.exists()


def test_docker_invocations_have_timeouts_and_preserve_status_diagnostics(tmp_path: Path) -> None:
    runner = RecordingRunner([Completed(returncode=1, stderr="Docker daemon is not running")])
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    status = manager.status().docker

    assert status.state == "unavailable"
    assert status.detail == "Docker daemon is not running"
    assert runner.calls[0][1]["timeout"] > 0


def test_docker_timeout_is_an_actionable_domain_error(tmp_path: Path) -> None:
    def timeout_runner(_args: list[str], **_kwargs: object) -> Completed:
        raise subprocess.TimeoutExpired("docker", 30)

    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=timeout_runner, listener_active=lambda: False
    )

    with pytest.raises(RuntimeError, match="timed out"):
        manager.start(runtime="docker")


def test_docker_nonzero_start_output_is_an_actionable_domain_error(tmp_path: Path) -> None:
    runner = RecordingRunner([Completed(), Completed(returncode=1, stderr="pull access denied")])
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    with pytest.raises(RuntimeError, match="pull access denied"):
        manager.start(runtime="docker")


def test_process_identity_fails_closed_on_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd13")
    monkeypatch.setattr(os, "name", "posix")

    assert _process_identity(os.getpid()) is None


def test_native_state_and_log_are_private(tmp_path: Path) -> None:
    listener_states = iter([False, True])
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path,
        popen=RecordingPopen(), runner=RecordingRunner(), listener_active=lambda: next(listener_states),
        process_identity=lambda _pid: heavenly_identity(),
    )

    manager.start(runtime="native")

    assert native_state_path(tmp_path).stat().st_mode & 0o777 == 0o600
    assert native_state_path(tmp_path).with_suffix(".log").stat().st_mode & 0o777 == 0o600


def test_cli_lifecycle_os_error_is_actionable_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeManager:
        def start(self, runtime: str | None = None):
            raise OSError("executable missing")

    monkeypatch.setattr("heavenly_health.cli._runtime_manager", FakeManager)

    result = CliRunner().invoke(app, ["runtime", "start"])

    assert result.exit_code == 1
    assert "Check runtime prerequisites" in result.stdout
    assert "Traceback" not in result.stdout


def test_native_readiness_timeout_retains_state_until_owned_process_exits_and_listener_releases(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    identity = heavenly_identity()
    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), state_path, tmp_path,
        popen=RecordingPopen(), runner=RecordingRunner(), listener_active=lambda: False,
        process_identity=lambda _pid: identity, terminate=lambda pid: stopped.append(pid), sleeper=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="ownership state was retained.*runtime stop --runtime native"):
        manager.start(runtime="native")

    assert stopped == [4815]
    assert state_path.exists()


def test_native_write_failure_terminates_owned_child_and_confirms_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = native_state_path(tmp_path)
    identity = heavenly_identity()
    identities = iter([identity, identity, None])
    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), state_path, tmp_path,
        popen=RecordingPopen(), runner=RecordingRunner(), listener_active=lambda: False,
        process_identity=lambda _pid: next(identities), terminate=lambda pid: stopped.append(pid), sleeper=lambda _seconds: None,
    )
    monkeypatch.setattr(manager.native, "_write_record", lambda _record: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(RuntimeError, match="ownership record.*cleanup completed"):
        manager.start(runtime="native")

    assert stopped == [4815]
    assert not state_path.exists()


def test_native_stop_treats_pid_reuse_as_owned_exit_once_listener_releases(tmp_path: Path) -> None:
    state_path = native_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"pid": 4815, "identity": "owned", "command": ["heavenly-mcp"]}')
    identities = iter(["owned", "owned", "reused-by-another-process"])
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), state_path, tmp_path, runner=RecordingRunner(),
        listener_active=lambda: False, process_identity=lambda _pid: next(identities),
        terminate=lambda _pid: None, sleeper=lambda _seconds: None,
    )

    result = manager.stop(runtime="native")

    assert result.state == "stopped"
    assert not state_path.exists()


def test_native_start_rejects_identity_that_is_not_plausibly_heavenly(tmp_path: Path) -> None:
    stopped: list[int] = []
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path,
        popen=RecordingPopen(), runner=RecordingRunner(), listener_active=lambda: False,
        process_identity=lambda _pid: heavenly_identity("unrelated-worker"), terminate=lambda pid: stopped.append(pid),
    )

    with pytest.raises(RuntimeError, match="does not match heavenly-mcp"):
        manager.start(runtime="native")

    assert stopped == [4815]
    assert not native_state_path(tmp_path).exists()


def test_docker_status_parses_compose_ndjson_and_surfaces_malformed_output(tmp_path: Path) -> None:
    runner = RecordingRunner(
        [
            Completed(stdout='{"ID":"a1","Name":"project-heavenly-mcp-1","State":"running"}\n'),
            Completed(stdout="not-json\n"),
        ]
    )
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    parsed = manager.status().docker
    malformed = manager.status().docker

    assert parsed.state == "running"
    assert malformed.state == "unavailable"
    assert "parse" in (malformed.detail or "").lower()


def test_docker_start_uses_separate_longer_timeout_and_pull_progress_guidance(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def runner(args: list[str], **kwargs: object) -> Completed:
        calls.append(kwargs)
        if "ps" in args:
            return Completed()
        raise subprocess.TimeoutExpired("docker compose up", cast(int, kwargs["timeout"]))

    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path, runner=runner, listener_active=lambda: False
    )

    with pytest.raises(RuntimeError, match="Compose status and image pull progress") as error:
        manager.start(runtime="docker")

    assert calls[-1]["timeout"] == DOCKER_START_TIMEOUT_SECONDS
    assert DOCKER_START_TIMEOUT_SECONDS > 30
    assert "Docker Desktop is down" not in str(error.value)


def test_compose_discovery_uses_working_project_when_cli_is_installed_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    installed_module = tmp_path / "site-packages" / "heavenly_health" / "cli.py"
    monkeypatch.chdir(tmp_path)

    assert discover_compose_project_root(installed_module) == tmp_path


def test_cli_invalid_runtime_override_is_a_bad_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("heavenly_health.cli._runtime_manager", lambda: pytest.fail("manager should not be constructed"))

    result = CliRunner().invoke(app, ["runtime", "start", "--runtime", "not-a-runtime"])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_native_log_rotates_at_bounded_size(tmp_path: Path) -> None:
    log_path = native_state_path(tmp_path).with_suffix(".log")
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"x" * (1024 * 1024))
    listener_states = iter([False, True])
    manager = RuntimeManager(
        LocalConfigStore(tmp_path / "runtime.json"), native_state_path(tmp_path), tmp_path,
        popen=RecordingPopen(), runner=RecordingRunner(), listener_active=lambda: next(listener_states),
        process_identity=lambda _pid: heavenly_identity(),
    )

    manager.start(runtime="native")

    assert log_path.with_suffix(".log.1").stat().st_size == 1024 * 1024
    assert log_path.stat().st_size == 0
