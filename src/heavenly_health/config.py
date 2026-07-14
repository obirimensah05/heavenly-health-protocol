"""Versioned, non-secret local runtime configuration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

CONFIG_VERSION = 1
VALID_RUNTIMES = frozenset({"native", "docker"})
_CONFIG_FIELDS = frozenset({"version", "runtime", "selected_model_profile", "delivery_references"})


class ConfigError(RuntimeError):
    """A local runtime configuration cannot be safely read or written."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RuntimeConfiguration:
    """Configuration stored locally without credentials or other secret values."""

    version: int = CONFIG_VERSION
    runtime: str = "native"
    selected_model_profile: str | None = None
    delivery_references: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != CONFIG_VERSION:
            raise ValueError(f"Unsupported configuration version: {self.version}")
        if not isinstance(self.runtime, str) or self.runtime not in VALID_RUNTIMES:
            raise ValueError(f"Unsupported runtime: {self.runtime}")
        if self.selected_model_profile is not None and not isinstance(self.selected_model_profile, str):
            raise ValueError("selected_model_profile must be a string or null")
        if self.delivery_references is None:
            references: dict[str, str] = {}
        elif not isinstance(self.delivery_references, Mapping):
            raise ValueError("delivery_references must be a string-to-string mapping")
        else:
            references = dict(self.delivery_references)
            if not all(isinstance(key, str) and isinstance(value, str) for key, value in references.items()):
                raise ValueError("delivery_references must be a string-to-string mapping")
        object.__setattr__(self, "delivery_references", MappingProxyType(references))

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable copy of this configuration."""
        return {
            "version": self.version,
            "runtime": self.runtime,
            "selected_model_profile": self.selected_model_profile,
            "delivery_references": dict(self.delivery_references or {}),
        }


class LocalConfigStore:
    """Read and write the local Heavenly runtime selection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _reject_symlink(self) -> None:
        if self.path.is_symlink():
            raise ConfigError(self.path, "configuration path must not be a symbolic link")

    def load(self) -> RuntimeConfiguration:
        self._reject_symlink()
        if not self.path.exists():
            return RuntimeConfiguration()
        try:
            contents = self.path.read_text()
        except OSError as error:
            raise ConfigError(self.path, f"could not read configuration: {error.strerror or error}") from error
        try:
            payload = json.loads(contents)
        except json.JSONDecodeError as error:
            raise ConfigError(self.path, "configuration must contain valid JSON") from error
        if not isinstance(payload, dict):
            raise ConfigError(self.path, "configuration must be a JSON object")
        unknown_fields = set(payload) - _CONFIG_FIELDS
        if unknown_fields:
            raise ConfigError(self.path, f"configuration has unknown field: {sorted(unknown_fields)[0]}")
        try:
            return RuntimeConfiguration(**payload)
        except (TypeError, ValueError) as error:
            raise ConfigError(self.path, str(error)) from error

    def save(self, configuration: RuntimeConfiguration) -> None:
        self._reject_symlink()
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(configuration.to_dict(), temporary_file, indent=2, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ConfigError(self.path, f"could not write configuration: {error.strerror or error}") from error

    def set_runtime(self, runtime: str) -> RuntimeConfiguration:
        configuration = self.load()
        updated = RuntimeConfiguration(
            runtime=runtime,
            selected_model_profile=configuration.selected_model_profile,
            delivery_references=configuration.delivery_references,
        )
        self.save(updated)
        return updated


def default_config_path() -> Path:
    """Return the standard local configuration path."""
    return Path.home() / ".config" / "heavenly" / "runtime.json"
