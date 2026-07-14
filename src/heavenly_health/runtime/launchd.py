"""Persistent macOS LaunchAgent backend for the native MCP server."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import time
from typing import Callable
from uuid import uuid4

from heavenly_health.runtime.native import NativeResult, NativeStatus

DEFAULT_LABEL = "com.heavenly-health.mcp"
_PID = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)


class LaunchdRuntime:
    """Install and control exactly one owner-scoped macOS LaunchAgent."""

    def __init__(
        self,
        *,
        plist_path: Path,
        executable: Path,
        log_directory: Path,
        label: str = DEFAULT_LABEL,
        uid: int | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        listener_active: Callable[[], bool],
        sleeper: Callable[[float], None] = time.sleep,
        startup_attempts: int = 120,
        shutdown_attempts: int = 40,
    ) -> None:
        self.plist_path = plist_path
        self.executable = executable
        self.log_directory = log_directory
        self.label = label
        self.uid = os.getuid() if uid is None else uid
        self._runner = runner
        self._listener_active = listener_active
        self._sleeper = sleeper
        self.startup_attempts = startup_attempts
        self.shutdown_attempts = shutdown_attempts

    @property
    def target(self) -> str:
        return f"gui/{self.uid}/{self.label}"

    def is_installed(self) -> bool:
        return self.plist_path.is_file() and not self.plist_path.is_symlink()

    def install(self) -> Path:
        """Write a secret-free, owner-only LaunchAgent definition atomically."""
        if self.plist_path.is_symlink():
            raise RuntimeError("Native LaunchAgent path must not be a symbolic link")
        self.plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.log_directory.chmod(0o700)
        except OSError as exc:
            raise RuntimeError("Native LaunchAgent log directory must be owner-only") from exc
        payload = {
            "Label": self.label,
            "ProgramArguments": [str(self.executable)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "ThrottleInterval": 5,
            "StandardOutPath": str(self.log_directory / "launchd.out.log"),
            "StandardErrorPath": str(self.log_directory / "launchd.err.log"),
        }
        encoded = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
        temporary = self.plist_path.parent / f".{self.plist_path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as file:
                descriptor = -1
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.plist_path)
            self.plist_path.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return self.plist_path

    def status(self) -> NativeStatus:
        result = self._run(["launchctl", "print", self.target])
        if result.returncode != 0:
            return NativeStatus("stopped")
        match = _PID.search(result.stdout or "")
        pid = int(match.group(1)) if match else None
        return NativeStatus("running" if self._listener_active() else "starting", pid)

    def start(self) -> NativeResult:
        if not self.is_installed():
            raise RuntimeError("Native LaunchAgent is not installed; run heavenly runtime install-service")
        current = self.status()
        if current.state == "running":
            return NativeResult("running", current.pid)
        if current.state == "stopped":
            bootstrapped = self._run(
                ["launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist_path)]
            )
            if bootstrapped.returncode != 0:
                raise RuntimeError("Could not bootstrap the native Heavenly LaunchAgent")
        kicked = self._run(["launchctl", "kickstart", "-k", self.target])
        if kicked.returncode != 0:
            self._run(["launchctl", "bootout", self.target])
            raise RuntimeError("Could not start the native Heavenly LaunchAgent")
        for _ in range(self.startup_attempts):
            self._sleeper(0.1)
            status = self.status()
            if status.state == "running":
                return NativeResult("running", status.pid)
        self._run(["launchctl", "bootout", self.target])
        raise RuntimeError("Native Heavenly LaunchAgent did not become ready on 127.0.0.1:8791")

    def stop(self) -> NativeResult:
        current = self.status()
        if current.state == "stopped":
            return NativeResult("stopped")
        result = self._run(["launchctl", "bootout", self.target])
        if result.returncode != 0:
            raise RuntimeError("Could not stop the native Heavenly LaunchAgent")
        for _ in range(self.shutdown_attempts):
            self._sleeper(0.1)
            if not self._listener_active():
                return NativeResult("stopped", current.pid)
        raise RuntimeError("Native Heavenly LaunchAgent did not release 127.0.0.1:8791")

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise RuntimeError("launchctl is unavailable") from exc
