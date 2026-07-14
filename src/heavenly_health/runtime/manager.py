"""Selection-aware native/Docker lifecycle coordinator."""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from heavenly_health.config import LocalConfigStore, VALID_RUNTIMES
from heavenly_health.runtime.docker import DockerResult, DockerRuntime, DockerStatus
from heavenly_health.runtime.native import NativeResult, NativeRuntime, NativeStatus, ProcessLike

LISTENER_HOST = "127.0.0.1"
LISTENER_PORT = 8791


class RuntimeConflictError(RuntimeError):
    """The requested runtime would collide with an active local service."""


class NativeRuntimeLike(Protocol):
    def status(self) -> NativeStatus: ...

    def start(self) -> NativeResult: ...

    def stop(self) -> NativeResult: ...


@dataclass(frozen=True)
class LifecycleResult:
    runtime: str
    state: str
    pid: int | None = None


@dataclass(frozen=True)
class RuntimeStatus:
    selected: str
    native: NativeStatus
    docker: DockerStatus
    listener: str | None
    conflict: str | None


class RuntimeManager:
    """Keep runtime selection separate from terminal-independent service control."""

    def __init__(
        self,
        config_store: LocalConfigStore,
        native_state_path: Path,
        project_root: Path,
        *,
        runner: Callable[..., object] = subprocess.run,
        popen: Callable[..., ProcessLike] = subprocess.Popen,
        listener_active: Callable[[], bool] | None = None,
        process_identity: Callable[[int], str | None] | None = None,
        terminate: Callable[[int], None] | None = None,
        sleeper: Callable[[float], None] | None = None,
        native_runtime: NativeRuntimeLike | None = None,
    ) -> None:
        self.config_store = config_store
        self._listener_active = listener_active or _listener_active
        self.native: NativeRuntimeLike = native_runtime or NativeRuntime(
            native_state_path,
            popen=popen,
            listener_active=self._listener_active,
            process_identity=process_identity or _process_identity,
            terminate=terminate,
            **({"sleeper": sleeper} if sleeper is not None else {}),
        )
        self.docker = DockerRuntime(project_root, runner=runner)

    def start(self, runtime: str | None = None) -> LifecycleResult:
        selected = self._resolve_runtime(runtime)
        if selected == "docker":
            docker = self.docker.status()
            if docker.state in {"running", "restarting"}:
                return LifecycleResult("docker", docker.state)
            native = self.native.status()
            if self._listener_active() and native.state != "running":
                raise RuntimeConflictError(
                    "A native listener is already using 127.0.0.1:8791; "
                    "run heavenly runtime stop --runtime native or choose another port before Docker start."
                )
            if native.state == "running":
                raise RuntimeConflictError(
                    "Native Heavenly MCP is running on 127.0.0.1:8791; "
                    "run heavenly runtime stop --runtime native before Docker start."
                )
            docker_result = self.docker.start()
            return LifecycleResult("docker", docker_result.state)

        docker = self.docker.status()
        if docker.state in {"running", "restarting"}:
            raise RuntimeConflictError(
                "Docker service heavenly-mcp is active; "
                "run heavenly runtime stop --runtime docker before native start."
            )
        native = self.native.status()
        if native.state == "running":
            return LifecycleResult("native", "running", native.pid)
        if self._listener_active():
            raise RuntimeConflictError(
                "A listener is already using 127.0.0.1:8791; "
                "inspect it, or stop Heavenly Docker with heavenly runtime stop --runtime docker."
            )
        native_result = self.native.start()
        return LifecycleResult("native", native_result.state, native_result.pid)

    def stop(self, runtime: str | None = None) -> LifecycleResult:
        selected = self._resolve_runtime(runtime)
        if selected == "native":
            native_result: NativeResult = self.native.stop()
            return LifecycleResult("native", native_result.state, native_result.pid)
        docker_result: DockerResult = self.docker.stop()
        return LifecycleResult("docker", docker_result.state)

    def status(self) -> RuntimeStatus:
        native = self.native.status()
        docker = self.docker.status()
        listener = f"{LISTENER_HOST}:{LISTENER_PORT}" if self._listener_active() else None
        conflict = None
        if native.state == "running" and docker.state in {"running", "restarting"}:
            conflict = "Conflict: both native and Docker Heavenly services are active; stop one before starting another."
        elif listener and native.state != "running" and docker.state not in {"running", "restarting"}:
            conflict = "Conflict: an unowned listener occupies 127.0.0.1:8791; Heavenly will not stop it."
        return RuntimeStatus(self.config_store.load().runtime, native, docker, listener, conflict)

    def _resolve_runtime(self, runtime: str | None) -> str:
        if runtime is None:
            return self.config_store.load().runtime
        if runtime not in VALID_RUNTIMES:
            raise ValueError(f"Unsupported runtime: {runtime}")
        return runtime


def _listener_active() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.15)
        return connection.connect_ex((LISTENER_HOST, LISTENER_PORT)) == 0


def _process_identity(pid: int) -> str | None:
    """Return a serialized process-instance, executable, and argv identity without a shell."""
    if sys.platform.startswith("linux") and os.path.exists(f"/proc/{pid}/stat"):
        return _linux_process_identity(pid)
    if sys.platform == "darwin":
        return _darwin_process_identity(pid)
    # Other POSIX platforms do not expose a sufficiently precise, stable birth marker.
    # Failing closed avoids confusing a same-second PID reuse with our owned process.
    return None


def _linux_process_identity(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        closing_paren = stat.rfind(")")
        fields = stat[closing_paren + 2 :].split()
        start_ticks = fields[19]
        executable = os.readlink(f"/proc/{pid}/exe")
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")[:-1]
    except (IndexError, OSError, ValueError):
        return None
    return _serialize_process_identity(f"linux-ticks:{start_ticks}", executable, argv)


def _darwin_process_identity(pid: int) -> str | None:
    """Use libproc's microsecond birth time plus executable path on macOS."""
    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint32),
            ("status", ctypes.c_uint32),
            ("xstatus", ctypes.c_uint32),
            ("pid", ctypes.c_uint32),
            ("ppid", ctypes.c_uint32),
            ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32),
            ("ruid", ctypes.c_uint32),
            ("rgid", ctypes.c_uint32),
            ("svuid", ctypes.c_uint32),
            ("svgid", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32),
            ("comm", ctypes.c_char * 16),
            ("name", ctypes.c_char * 32),
            ("nfiles", ctypes.c_uint32),
            ("pgid", ctypes.c_uint32),
            ("pjobc", ctypes.c_uint32),
            ("tdev", ctypes.c_uint32),
            ("tpgid", ctypes.c_uint32),
            ("nice", ctypes.c_int32),
            ("start_seconds", ctypes.c_uint64),
            ("start_microseconds", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        # libproc requires the full private struct size; retain only the stable prefix we need.
        info_buffer = ctypes.create_string_buffer(1024)
        size = libproc.proc_pidinfo(pid, 3, 0, info_buffer, len(info_buffer))
        info = ProcBsdInfo.from_buffer_copy(info_buffer.raw)
        executable_buffer = ctypes.create_string_buffer(4096)
        path_size = libproc.proc_pidpath(pid, executable_buffer, len(executable_buffer))
    except OSError:
        return None
    if size < ctypes.sizeof(info) or path_size <= 0 or info.start_seconds <= 0:
        return None
    command = _ps_command(pid)
    if command is None:
        return None
    return _serialize_process_identity(
        f"darwin-usec:{info.start_seconds}.{info.start_microseconds:06d}",
        executable_buffer.value.decode("utf-8", errors="surrogateescape"),
        [command.encode("utf-8", errors="surrogateescape")],
    )


def _ps_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False, shell=False
        )
    except OSError:
        return None
    command = result.stdout.strip() if result.returncode == 0 else ""
    return command or None


def _serialize_process_identity(start: str, executable: str, argv: list[bytes]) -> str:
    return json.dumps(
        {
            "arguments": [argument.decode("utf-8", errors="surrogateescape") for argument in argv],
            "executable": executable,
            "start": start,
        },
        sort_keys=True,
    )
