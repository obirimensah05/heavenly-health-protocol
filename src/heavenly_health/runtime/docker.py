"""Docker Compose lifecycle adapter for Heavenly's MCP service."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DOCKER_TIMEOUT_SECONDS = 30
DOCKER_START_TIMEOUT_SECONDS = 300


class DockerComposeParseError(ValueError):
    """Compose returned output that cannot safely be interpreted as service status."""


def discover_compose_project_root(module_path: Path | None = None) -> Path:
    """Find a checkout/packaged Compose file without assuming the CLI source layout."""
    configured = os.environ.get("HEAVENLY_COMPOSE_FILE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    anchors = [Path.cwd()]
    if module_path is not None:
        anchors.append(module_path.resolve().parent)
    else:
        anchors.append(Path(__file__).resolve().parent)
    for anchor in anchors:
        candidates.extend(parent / "compose.yaml" for parent in (anchor, *anchor.parents))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.parent
    raise RuntimeError(
        "Could not locate compose.yaml. Run this command from the Heavenly project directory or set HEAVENLY_COMPOSE_FILE."
    )


@dataclass(frozen=True)
class DockerStatus:
    state: str
    container_id: str | None = None
    name: str | None = None
    detail: str | None = None

    @property
    def identity(self) -> str | None:
        """Human-readable container identity retained for the existing CLI renderer."""
        if self.container_id and self.name:
            return f"{self.container_id} ({self.name})"
        return self.container_id or self.name


@dataclass(frozen=True)
class DockerResult:
    state: str


class DockerRuntime:
    """Wrap the one project-scoped Compose service without removing its volume."""

    service = "heavenly-mcp"

    def __init__(self, project_root: Path, *, runner: Callable[..., object] = subprocess.run) -> None:
        self.project_root = project_root
        self._runner = runner

    def status(self) -> DockerStatus:
        try:
            result = self._run("ps", "--format", "json", self.service)
        except subprocess.TimeoutExpired:
            return DockerStatus("unavailable", detail="docker compose ps timed out; start Docker Desktop and retry.")
        except OSError as error:
            return DockerStatus("unavailable", detail=self._diagnostic(error, "Docker Compose is unavailable; install/start Docker Desktop and retry."))
        if getattr(result, "returncode", 1) != 0:
            return DockerStatus("unavailable", detail=self._result_diagnostic(result, "docker compose ps failed"))
        try:
            records = self._parse_records(str(getattr(result, "stdout", "")))
        except DockerComposeParseError as error:
            return DockerStatus("unavailable", detail=f"Could not parse docker compose ps output: {error}")
        if not records:
            return DockerStatus("stopped")
        record = records[0]
        state = str(record.get("State", "unknown")).lower()
        container_id = record.get("ID")
        name = record.get("Name")
        return DockerStatus(state, str(container_id) if container_id else None, str(name) if name else None)

    def start(self) -> DockerResult:
        result = self._run_action(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(DOCKER_START_TIMEOUT_SECONDS),
            self.service,
            timeout=DOCKER_START_TIMEOUT_SECONDS,
        )
        if getattr(result, "returncode", 1) != 0:
            raise RuntimeError(self._result_diagnostic(result, "docker compose up failed"))
        return DockerResult("running")

    def stop(self) -> DockerResult:
        result = self._run_action("stop", self.service)
        if getattr(result, "returncode", 1) != 0:
            raise RuntimeError(self._result_diagnostic(result, "docker compose stop failed"))
        return DockerResult("stopped")

    def _run_action(self, *arguments: str, timeout: int = DOCKER_TIMEOUT_SECONDS) -> object:
        try:
            return self._run(*arguments, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            if arguments and arguments[0] == "up":
                raise RuntimeError(
                    f"Docker Compose startup timed out after {DOCKER_START_TIMEOUT_SECONDS}s. "
                    "Check Compose status and image pull progress, then retry."
                ) from error
            raise RuntimeError(
                f"Docker Compose {' '.join(arguments[:1])} timed out after {DOCKER_TIMEOUT_SECONDS}s; start Docker Desktop and retry."
            ) from error
        except OSError as error:
            raise RuntimeError("Docker Compose is unavailable; install/start Docker Desktop and retry.") from error

    def _run(self, *arguments: str, timeout: int = DOCKER_TIMEOUT_SECONDS) -> object:
        return self._runner(
            ["docker", "compose", "-f", str(self.project_root / "compose.yaml"), *arguments],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
        )

    @staticmethod
    def _result_diagnostic(result: object, fallback: str) -> str:
        return str(getattr(result, "stderr", "")).strip() or str(getattr(result, "stdout", "")).strip() or fallback

    @staticmethod
    def _diagnostic(error: OSError, fallback: str) -> str:
        return str(error).strip() or fallback

    @staticmethod
    def _parse_records(stdout: str) -> list[dict[str, object]]:
        if not stdout.strip():
            return []
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            records: list[dict[str, object]] = []
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise DockerComposeParseError(str(error)) from error
                if not isinstance(record, dict):
                    raise DockerComposeParseError("expected each NDJSON line to be an object")
                records.append(record)
            if not records:
                raise DockerComposeParseError("output was not JSON")
            return records
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            if not all(isinstance(item, dict) for item in payload):
                raise DockerComposeParseError("expected a JSON array of objects")
            return payload
        raise DockerComposeParseError("expected a JSON object, array, or NDJSON objects")
