"""Load narrowly scoped runtime values from protected owner-only files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import MutableMapping

_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_INLINE_COMMENT = re.compile(r"\s+#.*$")
_DIRECT_SECRET_NAMES = frozenset({"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"})
_INCLUDE_NAME = "HEAVENLY_SECRET_FILES"
_MAX_SECRET_FILE_BYTES = 64 * 1024
_MAX_INCLUDE_DEPTH = 8


class SecretFileError(RuntimeError):
    """A runtime environment file is missing, malformed, or insufficiently private."""


def load_runtime_environment(
    path: str | os.PathLike[str],
    *,
    environ: MutableMapping[str, str] | None = None,
) -> set[str]:
    """Load allowed values without replacing explicitly supplied process values.

    Only ``HEAVENLY_*`` settings and the two Supabase connection variables are
    imported. ``HEAVENLY_SECRET_FILES`` is a control value containing an
    ``os.pathsep``-separated list of additional protected files; it is never
    exported to the MCP process environment.
    """
    target = environ if environ is not None else os.environ
    values = _load_file(Path(path), stack=())
    loaded: set[str] = set()
    for name, value in values.items():
        if name not in target:
            target[name] = value
            loaded.add(name)
    return loaded


def _load_file(path: Path, *, stack: tuple[Path, ...]) -> dict[str, str]:
    if not path.is_absolute():
        raise SecretFileError("Runtime secret file path must be absolute")
    normalized = Path(os.path.abspath(path))
    if normalized in stack:
        raise SecretFileError("Runtime secret file include cycle detected")
    if len(stack) >= _MAX_INCLUDE_DEPTH:
        raise SecretFileError("Runtime secret file include depth exceeds the safe limit")

    contents = _read_private_file(normalized)
    parsed = _parse_assignments(contents, normalized)
    combined: dict[str, str] = {}
    includes = parsed.pop(_INCLUDE_NAME, "")
    if includes:
        for raw_include in includes.split(os.pathsep):
            include = raw_include.strip()
            if not include:
                continue
            combined.update(_load_file(Path(include), stack=(*stack, normalized)))
    for name, value in parsed.items():
        if _is_allowed_runtime_name(name):
            combined[name] = value
    return combined


def _read_private_file(path: Path) -> str:
    if path.is_symlink():
        raise SecretFileError(f"Runtime secret file must not be a symbolic link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SecretFileError(f"Runtime secret file is not readable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretFileError(f"Runtime secret file must be a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise SecretFileError(f"Runtime secret file must be owned by the current user: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise SecretFileError(f"Runtime secret file must be owner-only (mode 0600): {path}")
        if metadata.st_size > _MAX_SECRET_FILE_BYTES:
            raise SecretFileError(f"Runtime secret file exceeds the safe size limit: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            descriptor = -1
            return file.read(_MAX_SECRET_FILE_BYTES + 1)
    except UnicodeError as exc:
        raise SecretFileError(f"Runtime secret file must contain UTF-8 text: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_assignments(contents: str, path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            raise SecretFileError(f"Malformed runtime secret assignment at {path}, line {line_number}")
        name, raw_value = match.groups()
        parsed[name] = _parse_value(raw_value, path, line_number)
    return parsed


def _parse_value(raw_value: str, path: Path, line_number: int) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise SecretFileError(f"Malformed quoted value at {path}, line {line_number}")
        return value[1:-1]
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SecretFileError(f"Malformed quoted value at {path}, line {line_number}") from exc
        if not isinstance(decoded, str):
            raise SecretFileError(f"Malformed quoted value at {path}, line {line_number}")
        return decoded
    return _INLINE_COMMENT.sub("", value).rstrip()


def _is_allowed_runtime_name(name: str) -> bool:
    return name in _DIRECT_SECRET_NAMES or (name.startswith("HEAVENLY_") and name != _INCLUDE_NAME)
