from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from heavenly_health.agent_sandbox import (
    AgentSandboxError,
    AgentSandboxSpec,
    DockerAgentSandbox,
)


@dataclass
class Completed:
    returncode: int = 0


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> Completed:
        self.calls.append((command, kwargs))
        return Completed()


def test_agent_sandbox_defaults_to_no_network_no_secrets_and_read_only_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    spec = AgentSandboxSpec(
        image="example/agent:1.0",
        workspace=workspace,
        command=("agent-cli", "chat"),
    )

    command = DockerAgentSandbox().command(spec, environ={"OPENAI_API_KEY": "must-not-leak"})

    rendered = " ".join(command)
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges:true" in command
    assert "--user=1000:1000" in command
    assert any(value.startswith("--tmpfs=/home/agent:rw,exec,") for value in command)
    assert "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=512m" in command
    assert f"type=bind,src={workspace},dst=/workspace,readonly" in rendered
    assert "/var/run/docker.sock" not in rendered
    assert str(Path.home()) not in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert "must-not-leak" not in rendered
    assert command[-3:] == ["example/agent:1.0", "agent-cli", "chat"]


def test_agent_sandbox_requires_explicit_write_network_and_secret_grants(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    state = tmp_path / "agent-state"
    workspace.mkdir()
    state.mkdir(mode=0o700)
    spec = AgentSandboxSpec(
        image="example/agent:1.0",
        workspace=workspace,
        command=("agent-cli",),
        network="bridge",
        write_workspace=True,
        secret_env=("OPENAI_API_KEY",),
        state_dir=state,
    )

    command = DockerAgentSandbox().command(
        spec,
        environ={"OPENAI_API_KEY": "runtime-only-value"},
    )

    rendered = " ".join(command)
    assert "--network=bridge" in command
    assert f"type=bind,src={workspace},dst=/workspace" in rendered
    assert f"type=bind,src={state},dst=/home/agent" in rendered
    assert f"type=bind,src={workspace},dst=/workspace,readonly" not in rendered
    assert any(
        command[index : index + 2] == ["--env", "OPENAI_API_KEY"]
        for index in range(len(command) - 1)
    )
    assert "runtime-only-value" not in rendered


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"image": "--privileged"}, "image"),
        ({"network": "host"}, "network"),
        ({"secret_env": ("BAD-NAME",)}, "secret"),
        ({"secret_env": ("OPENAI_API_KEY",)}, "not present"),
        ({"user": "0:0"}, "non-root"),
    ],
)
def test_agent_sandbox_rejects_escape_prone_or_implicit_configuration(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    values: dict[str, object] = {
        "image": "example/agent:1.0",
        "workspace": workspace,
        "command": ("agent-cli",),
    }
    values.update(changes)
    spec = AgentSandboxSpec(**values)  # type: ignore[arg-type]

    with pytest.raises(AgentSandboxError, match=message):
        DockerAgentSandbox().command(spec, environ={})


def test_agent_sandbox_invokes_docker_without_shell_and_with_minimal_client_environment(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runner = RecordingRunner()
    sandbox = DockerAgentSandbox(runner=runner)
    spec = AgentSandboxSpec(
        image="example/agent:1.0",
        workspace=workspace,
        command=("agent-cli",),
        secret_env=("OPENAI_API_KEY",),
    )

    result = sandbox.run(
        spec,
        environ={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "OPENAI_API_KEY": "runtime-only-value",
            "HEAVENLY_SUPABASE_SERVICE_KEY": "must-not-propagate",
        },
        interactive=False,
    )

    assert result == 0
    command, kwargs = runner.calls[0]
    assert command[:2] == ["docker", "run"]
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["cwd"] == workspace
    assert kwargs["env"] == {
        "HOME": "/home/tester",
        "OPENAI_API_KEY": "runtime-only-value",
        "PATH": "/usr/bin",
    }


def test_agent_state_rejects_a_symlink_reached_through_home_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    real_state = tmp_path / "real-state"
    workspace.mkdir()
    real_state.mkdir(mode=0o700)
    (tmp_path / "agent-link").symlink_to(real_state, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = AgentSandboxSpec(
        image="example/agent:1.0",
        workspace=workspace,
        command=("agent-cli",),
        state_dir=Path("~/agent-link"),
    )

    with pytest.raises(AgentSandboxError, match="symbolic link"):
        DockerAgentSandbox().command(spec, environ={})
