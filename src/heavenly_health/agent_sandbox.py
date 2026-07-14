"""Hardened, LLM-agnostic launcher for user-supplied CLI-agent images."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_NUMERIC_USER = re.compile(r"^(?P<uid>[0-9]+):(?P<gid>[0-9]+)$")
_DOCKER_CLIENT_ENVIRONMENT = frozenset(
    {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "PATH",
        "XDG_CONFIG_HOME",
    }
)


class AgentSandboxError(ValueError):
    """An agent container request is unsafe, incomplete, or cannot start."""


@dataclass(frozen=True)
class AgentSandboxSpec:
    """Explicit capabilities granted to one ephemeral CLI-agent container."""

    image: str
    workspace: Path
    command: tuple[str, ...]
    network: str = "none"
    write_workspace: bool = False
    secret_env: tuple[str, ...] = ()
    state_dir: Path | None = None
    user: str = "1000:1000"


class DockerAgentSandbox:
    """Build and run a Docker command with a narrow, auditable capability set."""

    def __init__(
        self,
        *,
        runner: Callable[..., object] = subprocess.run,
    ) -> None:
        self._runner = runner

    def command(
        self,
        spec: AgentSandboxSpec,
        *,
        environ: Mapping[str, str],
        interactive: bool = False,
    ) -> list[str]:
        workspace = _validated_directory(spec.workspace, "workspace")
        state_dir = (
            _validated_private_state_directory(spec.state_dir)
            if spec.state_dir is not None
            else None
        )
        image = _validated_image(spec.image)
        network = _validated_network(spec.network)
        user = _validated_non_root_user(spec.user)
        secret_names = _validated_secret_names(spec.secret_env, environ)
        if not spec.command:
            raise AgentSandboxError("An in-container agent command is required")

        command = ["docker", "run", "--rm"]
        if interactive:
            command.append("-it")
        command.extend(
            [
                "--init",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=256",
                "--memory=2g",
                "--cpus=2",
                f"--network={network}",
                f"--user={user}",
                "--hostname=heavenly-agent",
                "--workdir=/workspace",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=512m",
                "--env",
                "HOME=/home/agent",
                "--env",
                "HEAVENLY_AGENT_SANDBOX=1",
            ]
        )
        workspace_mount = f"type=bind,src={workspace},dst=/workspace"
        if not spec.write_workspace:
            workspace_mount += ",readonly"
        command.extend(["--mount", workspace_mount])
        if state_dir is None:
            uid, gid = user.split(":", maxsplit=1)
            command.append(
                "--tmpfs=/home/agent:rw,exec,nosuid,nodev,size=1g,"
                f"uid={uid},gid={gid},mode=0700"
            )
        else:
            command.extend(["--mount", f"type=bind,src={state_dir},dst=/home/agent"])
        for name in secret_names:
            command.extend(["--env", name])
        command.extend([image, *spec.command])
        return command

    def run(
        self,
        spec: AgentSandboxSpec,
        *,
        environ: Mapping[str, str] | None = None,
        interactive: bool = True,
    ) -> int:
        values = os.environ if environ is None else environ
        command = self.command(spec, environ=values, interactive=interactive)
        client_environment = {
            name: value
            for name, value in values.items()
            if name in _DOCKER_CLIENT_ENVIRONMENT or name in spec.secret_env
        }
        try:
            result = self._runner(
                command,
                cwd=spec.workspace.resolve(strict=True),
                env=client_environment,
                check=False,
                shell=False,
            )
        except OSError as error:
            raise AgentSandboxError(
                "Docker is unavailable; install or start Docker and retry"
            ) from error
        return int(getattr(result, "returncode", 1))


def _validated_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise AgentSandboxError(f"The {label} directory does not exist") from error
    if not resolved.is_dir():
        raise AgentSandboxError(f"The {label} path must be a directory")
    if "," in str(resolved):
        raise AgentSandboxError(f"The {label} path cannot contain a comma")
    return resolved


def _validated_private_state_directory(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise AgentSandboxError("The agent state directory must not be a symbolic link")
    resolved = _validated_directory(expanded, "agent state")
    metadata = resolved.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise AgentSandboxError(
            "The agent state directory must be owned by the current user and private (mode 0700)"
        )
    return resolved


def _validated_image(value: str) -> str:
    image = value.strip()
    if (
        not image
        or image.startswith("-")
        or len(image) > 512
        or any(character.isspace() or ord(character) < 32 for character in image)
    ):
        raise AgentSandboxError("The agent image must be one explicit OCI image reference")
    return image


def _validated_network(value: str) -> str:
    network = value.strip()
    if network == "host" or _NETWORK_NAME.fullmatch(network) is None:
        raise AgentSandboxError(
            "The network must be none, bridge, or an explicit non-host Docker network"
        )
    return network


def _validated_non_root_user(value: str) -> str:
    match = _NUMERIC_USER.fullmatch(value.strip())
    if match is None or int(match.group("uid")) == 0 or int(match.group("gid")) == 0:
        raise AgentSandboxError("The agent container must use a numeric non-root user and group")
    return value.strip()


def _validated_secret_names(
    names: tuple[str, ...],
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    unique_names: list[str] = []
    for name in names:
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise AgentSandboxError("Every secret grant must be one environment variable name")
        if not environ.get(name):
            raise AgentSandboxError(f"Explicitly granted secret {name} is not present")
        if name not in unique_names:
            unique_names.append(name)
    return tuple(unique_names)
