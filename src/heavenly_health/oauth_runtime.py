"""Validated, redacted runtime settings for the optional remote OAuth mode."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlparse

from cryptography.fernet import Fernet

OAUTH_ENVIRONMENT_VARIABLES = (
    "HEAVENLY_OIDC_CONFIG_URL",
    "HEAVENLY_OIDC_CLIENT_ID",
    "HEAVENLY_OIDC_CLIENT_SECRET",
    "HEAVENLY_OIDC_AUDIENCE",
    "HEAVENLY_MCP_BASE_URL",
    "HEAVENLY_OAUTH_JWT_SIGNING_KEY",
    "HEAVENLY_OAUTH_ENCRYPTION_KEY",
)
MIN_JWT_SIGNING_KEY_NON_WHITESPACE_CHARS = 32
_HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_CLOUDFLARE_ACCESS_DISCOVERY_PATH = re.compile(
    r"/cdn-cgi/access/sso/oidc/[A-Za-z0-9._~-]+/.well-known/openid-configuration"
)


class OAuthSettingsError(ValueError):
    """OAuth settings are absent, incomplete, or unsafe for a public deployment."""


@dataclass(frozen=True)
class OAuthRuntimeSettings:
    """OAuth settings that deliberately redact credentials from representations."""

    oidc_config_url: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    audience: str = field(repr=False)
    mcp_base_url: str
    jwt_signing_key: str = field(repr=False)
    encryption_key: str = field(repr=False)
    oauth_state_dir: Path = field(repr=False)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> OAuthRuntimeSettings | None:
        """Return disabled mode for no settings, otherwise require every setting.

        This method intentionally names missing variables but never includes their
        values in an exception, making it safe for startup logging by callers.
        """
        values = {name: environ.get(name, "").strip() for name in OAUTH_ENVIRONMENT_VARIABLES}
        configured = {name for name, value in values.items() if value}
        if not configured:
            return None
        if configured != set(OAUTH_ENVIRONMENT_VARIABLES):
            missing = ", ".join(name for name in OAUTH_ENVIRONMENT_VARIABLES if name not in configured)
            raise OAuthSettingsError(f"Incomplete OAuth configuration; missing: {missing}")

        _validate_cloudflare_access_discovery_url(values["HEAVENLY_OIDC_CONFIG_URL"])
        _validate_https_url("HEAVENLY_MCP_BASE_URL", values["HEAVENLY_MCP_BASE_URL"], origin_only=True)
        if (
            len("".join(values["HEAVENLY_OAUTH_JWT_SIGNING_KEY"].split()))
            < MIN_JWT_SIGNING_KEY_NON_WHITESPACE_CHARS
        ):
            raise OAuthSettingsError(
                "HEAVENLY_OAUTH_JWT_SIGNING_KEY must contain at least "
                f"{MIN_JWT_SIGNING_KEY_NON_WHITESPACE_CHARS} non-whitespace characters"
            )
        try:
            Fernet(values["HEAVENLY_OAUTH_ENCRYPTION_KEY"].encode("ascii"))
        except (UnicodeEncodeError, ValueError, TypeError):
            raise OAuthSettingsError("HEAVENLY_OAUTH_ENCRYPTION_KEY must be a valid Fernet key") from None
        return cls(
            oidc_config_url=values["HEAVENLY_OIDC_CONFIG_URL"],
            client_id=values["HEAVENLY_OIDC_CLIENT_ID"],
            client_secret=values["HEAVENLY_OIDC_CLIENT_SECRET"],
            audience=values["HEAVENLY_OIDC_AUDIENCE"],
            mcp_base_url=values["HEAVENLY_MCP_BASE_URL"].rstrip("/"),
            jwt_signing_key=values["HEAVENLY_OAUTH_JWT_SIGNING_KEY"],
            encryption_key=values["HEAVENLY_OAUTH_ENCRYPTION_KEY"],
            oauth_state_dir=_oauth_state_dir(environ),
        )

    def safe_summary(self) -> dict[str, str | bool]:
        """Return status-only data suitable for diagnostics; never return secrets."""
        return {
            "mode": "oauth",
            "oidc_config_url_configured": True,
            "client_id_configured": True,
            "client_secret_configured": True,
            "audience_configured": True,
            "mcp_base_url": self.mcp_base_url,
            "jwt_signing_key_configured": True,
            "encryption_key_configured": True,
        }


def _validate_https_url(name: str, value: str, *, origin_only: bool) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OAuthSettingsError(f"{name} must be an HTTPS URL without credentials, query, or fragment")
    try:
        port = parsed.port
    except ValueError:
        raise OAuthSettingsError(f"{name} must use a numeric port between 1 and 65535") from None
    if port == 0:
        raise OAuthSettingsError(f"{name} must use a numeric port between 1 and 65535")
    hostname = parsed.hostname
    if not hostname:
        raise OAuthSettingsError(f"{name} must use a valid hostname")
    if origin_only and not _is_public_mcp_hostname(hostname):
        raise OAuthSettingsError(f"{name} must use a public hostname for remote cloud OAuth")
    if not _is_valid_hostname(hostname):
        raise OAuthSettingsError(f"{name} must use a valid hostname")
    if origin_only and parsed.path not in ("", "/"):
        raise OAuthSettingsError(f"{name} must be an HTTPS origin without a path")


def _validate_cloudflare_access_discovery_url(value: str) -> None:
    """Allow only the documented Cloudflare Access SaaS OIDC discovery shape."""
    _validate_https_url("HEAVENLY_OIDC_CONFIG_URL", value, origin_only=False)
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    valid_team_domain = (
        hostname.endswith(".cloudflareaccess.com")
        and hostname != "cloudflareaccess.com"
        and _is_valid_hostname(hostname)
    )
    if not valid_team_domain or not _CLOUDFLARE_ACCESS_DISCOVERY_PATH.fullmatch(parsed.path):
        raise OAuthSettingsError(
            "HEAVENLY_OIDC_CONFIG_URL must be a Cloudflare Access SaaS OIDC discovery URL"
        )


def _oauth_state_dir(environ: Mapping[str, str]) -> Path:
    """Return explicit durable state or a native XDG-style state path."""
    configured = environ.get("HEAVENLY_OAUTH_STATE_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise OAuthSettingsError("HEAVENLY_OAUTH_STATE_DIR must be an absolute path")
        return path
    xdg_state_home = environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "heavenly" / "oauth"
    return Path.home() / ".local" / "state" / "heavenly" / "oauth"


def _is_valid_hostname(hostname: str) -> bool:
    """Accept DNS hostnames only; the remote MCP origin is never addressed by IP."""
    normalized = hostname.rstrip(".")
    return bool(normalized) and len(normalized) <= 253 and all(
        _HOSTNAME_LABEL.fullmatch(label) for label in normalized.split(".")
    )


def _is_public_mcp_hostname(hostname: str) -> bool:
    """Reject localhost, loopback, and every IP literal for the remote cloud endpoint."""
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost") or "." not in normalized:
        return False
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return True
    return False
