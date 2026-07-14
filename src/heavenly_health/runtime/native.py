"""Native process ownership and lifecycle operations."""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

_SAFE_PARENT_ENV_NAMES = {
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
}


def _child_environment() -> dict[str, str]:
    """Pass only MCP configuration and narrowly required process settings."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _SAFE_PARENT_ENV_NAMES or name.startswith("HEAVENLY_")
    }
    environment.setdefault("PATH", os.defpath)
    return environment


class ProcessLike(Protocol):
    pid: int


@dataclass(frozen=True)
class NativeStatus:
    state: str
    pid: int | None = None


@dataclass(frozen=True)
class NativeResult:
    state: str
    pid: int | None = None


class NativeRuntime:
    """Manage only a process recorded by this application in its private state file."""

    command = ["heavenly-mcp"]
    startup_attempts = 120
    startup_interval = 0.1
    shutdown_attempts = 20
    shutdown_interval = 0.1
    log_max_bytes = 1024 * 1024

    def __init__(
        self,
        state_path: Path,
        *,
        popen: Callable[..., ProcessLike] = subprocess.Popen,
        listener_active: Callable[[], bool],
        process_identity: Callable[[int], str | None],
        terminate: Callable[[int], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state_path = state_path
        self._popen = popen
        self._listener_active = listener_active
        self._process_identity = process_identity
        self._terminate = terminate or self._default_terminate
        self._sleeper = sleeper

    def status(self) -> NativeStatus:
        record = self._read_record()
        if record is None:
            return NativeStatus("stopped")
        pid, identity = record
        if self._process_identity(pid) != identity:
            return NativeStatus("stale", pid)
        return NativeStatus("running", pid)

    def start(self) -> NativeResult:
        current = self.status()
        if current.state == "running":
            return NativeResult("running", current.pid)
        if current.state == "stale":
            self._remove_state()

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_path.parent.chmod(0o700)
        except OSError:
            pass
        with self._open_private_log() as log_file:
            process = self._popen(
                list(self.command),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=_child_environment(),
                shell=False,
                start_new_session=True,
            )
        identity = self._process_identity(process.pid)
        if identity is None:
            self._terminate_unmanaged_process(process.pid)
            raise RuntimeError(
                "Native MCP ownership could not be recorded; cleanup was attempted for the spawned process. "
                "Inspect the runtime log and run heavenly runtime status before retrying."
            )
        if not self._identity_is_plausibly_heavenly(identity):
            cleaned_up = self._cleanup_after_launch(process.pid, identity)
            outcome = "cleanup completed" if cleaned_up else "cleanup could not be confirmed"
            raise RuntimeError(
                f"Native MCP ownership identity does not match heavenly-mcp; {outcome}. "
                "Inspect the runtime log and run heavenly runtime status before retrying."
            )
        try:
            self._write_record({"pid": process.pid, "identity": identity, "command": list(self.command)})
        except OSError as error:
            cleaned_up = self._cleanup_after_launch(process.pid, identity)
            outcome = "cleanup completed" if cleaned_up else "cleanup could not be confirmed"
            raise RuntimeError(
                f"Native MCP ownership record could not be written; {outcome}. "
                "Inspect the runtime log and run heavenly runtime status before retrying."
            ) from error
        for _ in range(self.startup_attempts):
            if self._listener_active():
                return NativeResult("running", process.pid)
            self._sleeper(self.startup_interval)
        self._terminate_if_owned(process.pid, identity)
        if self._wait_for_exit_and_listener_release(process.pid, identity):
            self._remove_state()
            raise RuntimeError("Native MCP process did not become ready on 127.0.0.1:8791; cleanup completed. See runtime log.")
        raise RuntimeError(
            "Native MCP process did not become ready on 127.0.0.1:8791 and cleanup could not be confirmed; "
            "ownership state was retained. Inspect the process and retry heavenly runtime stop --runtime native."
        )

    def stop(self) -> NativeResult:
        record = self._read_record()
        if record is None:
            return NativeResult("stopped")
        pid, identity = record
        if self._process_identity(pid) != identity:
            self._remove_state()
            return NativeResult("stale", pid)
        # Revalidate immediately before signalling to narrow the PID-reuse race.
        if self._process_identity(pid) != identity:
            self._remove_state()
            return NativeResult("stale", pid)
        try:
            self._terminate(pid)
        except ProcessLookupError:
            self._remove_state()
            return NativeResult("stopped", pid)
        except OSError as error:
            raise RuntimeError(
                f"Could not signal owned native MCP PID {pid}; ownership state was retained. "
                "Check permissions and retry heavenly runtime stop --runtime native."
            ) from error

        if self._wait_for_exit_and_listener_release(pid, identity):
            self._remove_state()
            return NativeResult("stopped", pid)
        raise RuntimeError(
            f"Native MCP PID {pid} did not exit or release 127.0.0.1:8791 after SIGTERM; "
            "ownership state was retained. Inspect the process and retry heavenly runtime stop --runtime native."
        )

    def _cleanup_after_launch(self, pid: int, identity: str | None) -> bool:
        """Terminate only the just-observed process instance and confirm it released the listener."""
        if identity is None:
            return False
        self._terminate_if_owned(pid, identity)
        return self._wait_for_exit_and_listener_release(pid, identity)

    def _terminate_unmanaged_process(self, pid: int) -> None:
        """Best-effort cleanup when no stable identity could be captured after launch."""
        try:
            self._terminate(pid)
        except (ProcessLookupError, OSError):
            pass

    def _terminate_if_owned(self, pid: int, identity: str) -> bool:
        """Revalidate directly before SIGTERM; a PID is never sufficient ownership proof."""
        if self._process_identity(pid) != identity:
            return False
        try:
            self._terminate(pid)
        except ProcessLookupError:
            return True
        return True

    def _wait_for_exit_and_listener_release(self, pid: int, identity: str) -> bool:
        """A changed identity includes PID reuse: the owned process instance has exited."""
        for _ in range(self.shutdown_attempts):
            if self._process_identity(pid) != identity and not self._listener_active():
                return True
            self._sleeper(self.shutdown_interval)
        return False

    def _identity_is_plausibly_heavenly(self, identity: str) -> bool:
        try:
            payload = json.loads(identity)
        except json.JSONDecodeError:
            return False
        arguments = payload.get("arguments") if isinstance(payload, dict) else None
        if not isinstance(arguments, list) or not all(isinstance(argument, str) for argument in arguments):
            return False
        tokens: list[str] = []
        for argument in arguments:
            try:
                tokens.extend(shlex.split(argument))
            except ValueError:
                return False
        return any(Path(token).name == self.command[0] for token in tokens)

    def _open_private_log(self):
        log_path = self.state_path.with_suffix(".log")
        self._rotate_log(log_path)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(log_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def _rotate_log(self, log_path: Path) -> None:
        """Keep at most one bounded previous log without following a user-controlled link."""
        try:
            if log_path.is_symlink() or not log_path.exists() or log_path.stat().st_size < self.log_max_bytes:
                return
            os.replace(log_path, log_path.with_suffix(".log.1"))
        except OSError:
            # Logging must not prevent the lifecycle error path from retaining safe ownership state.
            return

    def _read_record(self) -> tuple[int, str] | None:
        if self.state_path.is_symlink() or not self.state_path.exists():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        pid, identity, command = payload.get("pid"), payload.get("identity"), payload.get("command")
        if type(pid) is not int or not isinstance(identity, str) or command != self.command:
            return None
        return pid, identity

    def _write_record(self, record: dict[str, object]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                descriptor = None
                json.dump(record, state_file, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self.state_path)
            self._fsync_directory()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(self.state_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _remove_state(self) -> None:
        if not self.state_path.is_symlink():
            self.state_path.unlink(missing_ok=True)

    @staticmethod
    def _default_terminate(pid: int) -> None:
        os.kill(pid, signal.SIGTERM)
