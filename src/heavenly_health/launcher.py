"""Credential-safe process entrypoint for the native Heavenly MCP server."""

from __future__ import annotations

import os
from pathlib import Path

from heavenly_health.secret_loader import load_runtime_environment

DEFAULT_RUNTIME_ENV = Path.home() / ".config" / "heavenly" / "runtime.env"


def run() -> None:
    """Load an optional protected runtime file before importing the MCP app."""
    configured = os.environ.get("HEAVENLY_SECRET_FILE", "").strip()
    runtime_file = Path(configured).expanduser() if configured else DEFAULT_RUNTIME_ENV
    if configured or runtime_file.exists():
        load_runtime_environment(runtime_file)

    from heavenly_health.mcp_server import run as run_mcp

    run_mcp()
