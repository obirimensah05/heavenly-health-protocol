"""Shared, credential-safe primitives for health provider connectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse


_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SECRET_FIELD = re.compile(
    r"(?:^|_)(?:access_token|refresh_token|token|secret|password|authorization_code|code_verifier)(?:$|_)",
    re.IGNORECASE,
)
_MAX_STATE_BYTES = 256 * 1024
_MAX_PRIVATE_JSON_BYTES = 128 * 1024


class ProviderConfigurationError(RuntimeError):
    """Provider configuration, local state, or a remote response is unsafe."""


@dataclass(frozen=True)
class OAuthToken:
    """OAuth token set whose secret values never appear in representations."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scopes: frozenset[str] = field(default_factory=frozenset)
    token_type: str = "Bearer"

    @classmethod
    def from_response(
        cls,
        payload: Mapping[str, object],
        *,
        now: datetime,
        previous_refresh_token: str | None = None,
    ) -> OAuthToken:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ProviderConfigurationError("Provider token response omitted an access token")
        token_type = str(payload.get("token_type", "Bearer"))
        if token_type.lower() != "bearer":
            raise ProviderConfigurationError("Provider returned an unsupported token type")
        expires_value = payload.get("expires_in", 3600)
        if isinstance(expires_value, bool):
            raise ProviderConfigurationError("Provider token lifetime is invalid")
        try:
            expires_in = int(expires_value)
        except (TypeError, ValueError) as error:
            raise ProviderConfigurationError("Provider token lifetime is invalid") from error
        if expires_in < 60 or expires_in > 365 * 24 * 60 * 60:
            raise ProviderConfigurationError("Provider token lifetime is outside safe bounds")
        refresh_value = payload.get("refresh_token")
        refresh_token = (
            refresh_value.strip()
            if isinstance(refresh_value, str) and refresh_value.strip()
            else previous_refresh_token
        )
        raw_scopes = payload.get("scope", "")
        if isinstance(raw_scopes, str):
            scopes = frozenset(scope for scope in raw_scopes.split() if scope)
        elif isinstance(raw_scopes, list) and all(isinstance(scope, str) for scope in raw_scopes):
            scopes = frozenset(raw_scopes)
        else:
            raise ProviderConfigurationError("Provider token scopes are invalid")
        return cls(
            access_token=access_token.strip(),
            refresh_token=refresh_token,
            expires_at=_aware(now) + timedelta(seconds=expires_in),
            scopes=scopes,
            token_type="Bearer",
        )

    @classmethod
    def from_json(cls, value: str) -> OAuthToken:
        try:
            payload = json.loads(value)
            expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
            return cls(
                access_token=str(payload["access_token"]),
                refresh_token=(
                    str(payload["refresh_token"]) if payload.get("refresh_token") else None
                ),
                expires_at=_aware(expires_at),
                scopes=frozenset(str(scope) for scope in payload.get("scopes", [])),
                token_type=str(payload.get("token_type", "Bearer")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderConfigurationError("Stored provider token is invalid") from error

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": _timestamp(self.expires_at),
                "scopes": sorted(self.scopes),
                "token_type": self.token_type,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def needs_refresh(self, now: datetime, *, skew: timedelta = timedelta(minutes=2)) -> bool:
        return _aware(now) + skew >= self.expires_at


class SecretStore(Protocol):
    """Minimal provider secret-store contract."""

    def get(self, service: str, account: str) -> str | None: ...

    def set(self, service: str, account: str, value: str) -> None: ...

    def delete(self, service: str, account: str) -> None: ...


class MemorySecretStore:
    """In-memory secret store for isolated tests and ephemeral integrations."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str | None:
        return self._values.get((service, account))

    def set(self, service: str, account: str, value: str) -> None:
        self._values[(service, account)] = value

    def delete(self, service: str, account: str) -> None:
        self._values.pop((service, account), None)

    def __repr__(self) -> str:
        return f"MemorySecretStore(entries={len(self._values)})"


class ProviderStateStore:
    """Persist only non-secret provider state in owner-only JSON files."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        self._prepare_root()

    def load(self, provider: str) -> dict[str, Any]:
        target = self._target(provider)
        if target.is_symlink():
            raise ProviderConfigurationError("Provider state must not be a symbolic link")
        if not target.exists():
            return {}
        try:
            metadata = target.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > _MAX_STATE_BYTES
            ):
                raise ProviderConfigurationError("Provider state must be owner-only and bounded")
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderConfigurationError("Provider state is unreadable") from error
        if not isinstance(payload, dict) or _contains_secret_field(payload):
            raise ProviderConfigurationError("Provider state contains invalid or secret material")
        return payload

    def save(self, provider: str, state: Mapping[str, Any]) -> None:
        target = self._target(provider)
        if target.is_symlink():
            raise ProviderConfigurationError("Provider state must not be a symbolic link")
        payload = dict(state)
        if _contains_secret_field(payload):
            raise ProviderConfigurationError("Provider state must not contain secret material")
        try:
            encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        except (TypeError, ValueError) as error:
            raise ProviderConfigurationError("Provider state must be JSON serializable") from error
        if len(encoded.encode("utf-8")) > _MAX_STATE_BYTES:
            raise ProviderConfigurationError("Provider state exceeds the safe size limit")
        descriptor = -1
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=self.root)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                descriptor = -1
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            temporary = ""
            target.chmod(0o600)
        except OSError as error:
            raise ProviderConfigurationError("Provider state could not be saved") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def delete(self, provider: str) -> None:
        target = self._target(provider)
        if target.is_symlink():
            raise ProviderConfigurationError("Provider state must not be a symbolic link")
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            raise ProviderConfigurationError("Provider state could not be deleted") from error

    def _prepare_root(self) -> None:
        if self.root.is_symlink():
            raise ProviderConfigurationError("Provider state directory must not be a symbolic link")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
            metadata = self.root.stat()
        except OSError as error:
            raise ProviderConfigurationError("Provider state directory is unavailable") from error
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ProviderConfigurationError("Provider state directory must be owner-only")

    def _target(self, provider: str) -> Path:
        if _PROVIDER_NAME.fullmatch(provider) is None:
            raise ProviderConfigurationError("Provider name is invalid")
        return self.root / f"{provider}.json"


def validate_https_url(
    value: str,
    *,
    name: str,
    allowed_hosts: frozenset[str] | None = None,
) -> str:
    """Validate a public HTTPS provider endpoint without credentials or fragments."""
    parsed = urlparse(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip = False
    else:
        is_ip = True
    if (
        parsed.scheme != "https"
        or not host
        or "." not in host
        or is_ip
        or host == "localhost"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
        or (allowed_hosts is not None and host not in allowed_hosts)
    ):
        raise ProviderConfigurationError(f"{name} must be an approved public HTTPS URL")
    return value


def read_private_json(path: Path) -> dict[str, Any]:
    """Read a bounded owner-only JSON object without following symlinks."""
    target = path.expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise ProviderConfigurationError("Provider credential file must be an absolute regular file")
    descriptor = -1
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_PRIVATE_JSON_BYTES
        ):
            raise ProviderConfigurationError(
                "Provider credential file must be owner-only and at most 128 KiB"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            descriptor = -1
            payload = json.load(file)
    except ProviderConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProviderConfigurationError("Provider credential file is unreadable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ProviderConfigurationError("Provider credential file must contain a JSON object")
    return payload


def default_provider_state_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "heavenly" / "providers"


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            _SECRET_FIELD.search(str(key)) is not None or _contains_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderConfigurationError("Provider clock must return a timezone-aware value")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")

