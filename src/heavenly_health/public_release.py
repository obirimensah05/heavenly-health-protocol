"""Fail-closed helpers for creating a clean public source export."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_UNSAFE_EXACT_NAMES = frozenset(
    {
        ".env",
        "auth.json",
        "credentials.json",
        "handover.md",
        "runtime.env",
    }
)
_UNSAFE_DIRECTORY_NAMES = frozenset({"secrets", "state"})
_UNSAFE_SUFFIXES = frozenset({".jwt", ".key", ".p12", ".pem"})
_MACOS_HOME_PREFIX = "/" + "Users" + "/"
_LINUX_HOME_PREFIX = "/" + "home" + "/"
_WINDOWS_HOME_SEGMENT = "Users" + "\\"
_HOME_PATH_PATTERNS = (
    re.compile(re.escape(_MACOS_HOME_PREFIX) + r"[^/\s]+/"),
    re.compile(
        re.escape(_LINUX_HOME_PREFIX)
        + r"(?!(?:heavenly|agent)(?:/|\s|$))[^/\s]+/"
    ),
    re.compile(r"[A-Za-z]:\\" + re.escape(_WINDOWS_HOME_SEGMENT) + r"[^\\\s]+\\"),
)
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{32,}"),
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(
        r"(?im)^\s*[A-Za-z_][A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)"
        r"\s*=\s*(?!['\"]?https?://)['\"]?[A-Za-z0-9_./+=:-]{20,}['\"]?"
        r"\s*(?:#.*)?$"
    ),
)


@dataclass(frozen=True, order=True)
class PublicReleaseFinding:
    """One redacted reason a path cannot enter a public release."""

    path: str
    reason: str


class PublicReleaseError(RuntimeError):
    """A public export failed validation without echoing sensitive content."""

    def __init__(
        self,
        message: str,
        findings: Iterable[PublicReleaseFinding] = (),
    ) -> None:
        self.findings = tuple(sorted(set(findings)))
        details = "; ".join(f"{item.path}: {item.reason}" for item in self.findings)
        super().__init__(f"{message}: {details}" if details else message)


def _manifest_path(value: str | Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or not candidate.parts or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise PublicReleaseError(
            "public release validation failed",
            [PublicReleaseFinding("<manifest>", "unsafe manifest path")],
        )
    return candidate


def _unsafe_path_reason(path: Path) -> str | None:
    names = {part.casefold() for part in path.parts}
    filename = path.name.casefold()
    if (
        filename in _UNSAFE_EXACT_NAMES
        or (filename.startswith(".env.") and filename != ".env.example")
        or path.suffix.casefold() in _UNSAFE_SUFFIXES
        or names.intersection(_UNSAFE_DIRECTORY_NAMES)
    ):
        return "unsafe public path"
    return None


def _text_findings(
    path: Path,
    relative_path: Path,
    forbidden_markers: tuple[str, ...],
) -> list[PublicReleaseFinding]:
    try:
        content = path.read_bytes()
    except OSError:
        return [PublicReleaseFinding(relative_path.as_posix(), "unreadable tracked file")]
    if b"\x00" in content:
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []

    findings: list[PublicReleaseFinding] = []
    redacted_path = relative_path.as_posix()
    folded = text.casefold()
    if any(marker in folded for marker in forbidden_markers):
        findings.append(PublicReleaseFinding(redacted_path, "forbidden private marker"))
    if any(pattern.search(text) for pattern in _HOME_PATH_PATTERNS):
        findings.append(PublicReleaseFinding(redacted_path, "absolute user home path"))
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        findings.append(PublicReleaseFinding(redacted_path, "secret-shaped content"))
    return findings


def validate_public_tree(
    source: Path,
    paths: Iterable[str | Path],
    *,
    forbidden_markers: Iterable[str] = (),
) -> tuple[Path, ...]:
    """Validate an explicit tracked-file manifest and return its normalized paths."""
    root = source.resolve(strict=True)
    normalized_markers = tuple(
        marker.strip().casefold() for marker in forbidden_markers if marker.strip()
    )
    normalized_paths: list[Path] = []
    findings: list[PublicReleaseFinding] = []

    for value in paths:
        try:
            relative_path = _manifest_path(value)
        except PublicReleaseError as error:
            findings.extend(error.findings)
            continue
        normalized_paths.append(relative_path)
        unsafe_reason = _unsafe_path_reason(relative_path)
        if unsafe_reason is not None:
            findings.append(PublicReleaseFinding(relative_path.as_posix(), unsafe_reason))
            continue
        absolute_path = source / relative_path
        if absolute_path.is_symlink():
            findings.append(
                PublicReleaseFinding(relative_path.as_posix(), "symbolic links are not exported")
            )
            continue
        try:
            resolved_path = absolute_path.resolve(strict=True)
        except OSError:
            findings.append(PublicReleaseFinding(relative_path.as_posix(), "tracked file is missing"))
            continue
        if root not in resolved_path.parents or not resolved_path.is_file():
            findings.append(PublicReleaseFinding(relative_path.as_posix(), "unsafe manifest path"))
            continue
        findings.extend(
            _text_findings(resolved_path, relative_path, normalized_markers)
        )

    if findings:
        raise PublicReleaseError("public release validation failed", findings)
    return tuple(sorted(set(normalized_paths), key=lambda item: item.as_posix()))


def export_public_tree(
    source: Path,
    destination: Path,
    paths: Iterable[str | Path],
    *,
    forbidden_markers: Iterable[str] = (),
) -> tuple[Path, ...]:
    """Atomically copy one validated tracked-file manifest into a new directory."""
    if destination.exists() or destination.is_symlink():
        raise PublicReleaseError("public export destination must not exist")
    manifest = validate_public_tree(
        source,
        paths,
        forbidden_markers=forbidden_markers,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        for relative_path in manifest:
            target = temporary / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative_path, target)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest
