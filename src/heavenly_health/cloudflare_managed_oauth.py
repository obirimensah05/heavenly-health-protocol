"""Validate Cloudflare Managed OAuth assertions at the private origin boundary."""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import jwt
from starlette.types import ASGIApp, Receive, Scope, Send

from heavenly_health.cloudflare_access import normalize_email


_AUDIENCE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_MANAGED_NAMES = (
    "HEAVENLY_CLOUDFLARE_TEAM_DOMAIN",
    "HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE",
    "HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS",
    "HEAVENLY_MCP_PUBLIC_HOST",
)
_MANAGED_TRUST_NAMES = _MANAGED_NAMES[:-1]
_MAX_ASSERTION_BYTES = 16 * 1024
_DENIED_BODY = json.dumps(
    {"error": "Cloudflare Access authentication failed"},
    separators=(",", ":"),
).encode("utf-8")
_ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_MAX_PRIVATE_FILE_BYTES = 64 * 1024


class CloudflareManagedOAuthError(RuntimeError):
    """Managed OAuth configuration or an origin assertion is invalid."""


@dataclass(frozen=True)
class CloudflareManagedOAuthSettings:
    """Validated public routing plus private identity policy configuration."""

    team_domain: str
    public_host: str
    audience: str = field(repr=False)
    allowed_emails: frozenset[str] = field(repr=False)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
    ) -> CloudflareManagedOAuthSettings | None:
        values = {name: environ.get(name, "").strip() for name in _MANAGED_NAMES}
        configured_trust = [name for name in _MANAGED_TRUST_NAMES if values[name]]
        if not configured_trust:
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise CloudflareManagedOAuthError(
                "Incomplete Cloudflare Managed OAuth configuration; missing: "
                + ", ".join(missing)
            )

        team_domain = _validated_team_domain(values["HEAVENLY_CLOUDFLARE_TEAM_DOMAIN"])
        public_host = _validated_public_host(values["HEAVENLY_MCP_PUBLIC_HOST"])
        audience = values["HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE"]
        if _AUDIENCE.fullmatch(audience) is None:
            raise CloudflareManagedOAuthError(
                "HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE must be a valid Access audience tag"
            )
        try:
            allowed_emails = frozenset(
                normalize_email(value)
                for value in values["HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS"].split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise CloudflareManagedOAuthError(
                "HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS must contain valid exact email addresses"
            ) from exc
        if not allowed_emails or len(allowed_emails) > 20:
            raise CloudflareManagedOAuthError(
                "HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS must contain one to twenty exact identities"
            )
        return cls(
            team_domain=team_domain,
            public_host=public_host,
            audience=audience,
            allowed_emails=allowed_emails,
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.team_domain}/cdn-cgi/access/certs"


class CloudflareAccessJWTVerifier:
    """Verify Access signatures, issuer, audience, token type, and exact identity."""

    def __init__(
        self,
        settings: CloudflareManagedOAuthSettings,
        *,
        signing_key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings
        if signing_key_resolver is None:
            jwks = jwt.PyJWKClient(
                settings.jwks_url,
                cache_keys=True,
                lifespan=300,
                timeout=10,
            )

            def resolve_signing_key(token: str) -> Any:
                return jwks.get_signing_key_from_jwt(token).key

            signing_key_resolver = resolve_signing_key
        self._signing_key_resolver = signing_key_resolver

    def verify(self, token: str) -> dict[str, str]:
        if not token or len(token.encode("utf-8")) > _MAX_ASSERTION_BYTES:
            raise CloudflareManagedOAuthError("Cloudflare Access assertion is invalid")
        try:
            key = self._signing_key_resolver(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.team_domain,
                leeway=30,
                options={
                    "require": ["aud", "email", "exp", "iat", "nbf", "iss", "sub", "type"]
                },
            )
            email = normalize_email(str(claims["email"]))
            subject = str(claims["sub"])
        except (jwt.PyJWTError, KeyError, OSError, TypeError, ValueError) as exc:
            raise CloudflareManagedOAuthError("Cloudflare Access assertion is invalid") from exc
        if claims.get("type") != "app" or not subject or email not in self.settings.allowed_emails:
            raise CloudflareManagedOAuthError("Cloudflare Access assertion is not owner-authorized")
        return {"email": email, "subject": subject}


class CloudflareAccessJWTMiddleware:
    """Require a verified Access assertion for every non-loopback request.

    The only exemption is a request whose real transport peer is a loopback
    address, which is the local-first path the owner drives from their own
    machine. Trust is never inferred from the ``Host`` header or from any other
    caller-supplied value, because those are attacker-controlled at the origin.
    """

    def __init__(self, app: ASGIApp, verifier: CloudflareAccessJWTVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = [(name.lower(), value) for name, value in scope.get("headers", [])]
        remote_peer = not _peer_is_loopback(scope.get("client"))
        public_host = _header_host(headers) == self.verifier.settings.public_host
        cloudflare_forwarded = any(
            name in {b"cf-connecting-ip", b"cf-ray"} for name, _value in headers
        )
        assertions = [
            value.decode("utf-8", errors="strict")
            for name, value in headers
            if name == b"cf-access-jwt-assertion"
        ]
        if remote_peer or public_host or cloudflare_forwarded or assertions:
            try:
                if len(assertions) != 1:
                    raise CloudflareManagedOAuthError("Cloudflare Access assertion is missing")
                identity = await asyncio.to_thread(self.verifier.verify, assertions[0])
            except (CloudflareManagedOAuthError, UnicodeError):
                await _deny(send, websocket=scope["type"] == "websocket")
                return
            forwarded_scope = dict(scope)
            state = dict(scope.get("state", {}))
            state["cloudflare_access_identity"] = identity
            forwarded_scope["state"] = state
            await self.app(forwarded_scope, receive, send)
            return
        await self.app(scope, receive, send)


def configure_runtime_from_access_assertion(
    assertion_path: Path,
    *,
    public_host: str,
    runtime_path: Path,
    team_domain: str,
    audience: str,
    verifier_factory: Callable[
        [CloudflareManagedOAuthSettings], CloudflareAccessJWTVerifier
    ] = CloudflareAccessJWTVerifier,
) -> Path:
    """Verify an Access JWT against operator-supplied trust and persist it.

    The team domain and audience must be supplied out of band by the operator.
    They are never derived from the assertion itself: any Cloudflare Access team
    can mint a well-formed token for its own issuer and audience, so a token that
    selects the trust anchor it is checked against proves nothing. Only the owner
    identity is read from the assertion, and only after the signature verifies
    against the operator's team domain and audience.
    """
    assertion = _read_private_text(assertion_path).strip()
    claims = _bootstrap_claims(assertion)
    expected_team_domain = _validated_team_domain(team_domain)
    if str(claims.get("iss", "")).rstrip("/") != expected_team_domain:
        raise CloudflareManagedOAuthError(
            "Cloudflare Access assertion was not issued by the expected team domain"
        )
    audience_value = claims.get("aud")
    if isinstance(audience_value, list):
        presented_audiences = [item for item in audience_value if isinstance(item, str)]
    elif isinstance(audience_value, str):
        presented_audiences = [audience_value]
    else:
        presented_audiences = []
    if audience not in presented_audiences:
        raise CloudflareManagedOAuthError(
            "Cloudflare Access assertion does not carry the expected Access audience"
        )
    environment = {
        "HEAVENLY_CLOUDFLARE_TEAM_DOMAIN": expected_team_domain,
        "HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE": audience,
        "HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS": str(claims.get("email", "")),
        "HEAVENLY_MCP_PUBLIC_HOST": public_host,
    }
    settings = CloudflareManagedOAuthSettings.from_environ(environment)
    if settings is None:
        raise CloudflareManagedOAuthError("Cloudflare Access assertion is incomplete")
    verifier_factory(settings).verify(assertion)
    _update_private_runtime_file(runtime_path, environment)
    return runtime_path


def _bootstrap_claims(assertion: str) -> dict[str, object]:
    parts = assertion.split(".")
    if len(parts) != 3:
        raise CloudflareManagedOAuthError("Cloudflare Access assertion is invalid")
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise CloudflareManagedOAuthError("Cloudflare Access assertion is invalid") from error
    if not isinstance(payload, dict):
        raise CloudflareManagedOAuthError("Cloudflare Access assertion is invalid")
    return payload


def _read_private_text(path: Path) -> str:
    target = path.expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise CloudflareManagedOAuthError("Private runtime input must be an absolute regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(target, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_PRIVATE_FILE_BYTES
        ):
            raise CloudflareManagedOAuthError(
                "Private runtime input must be owner-only and at most 64 KiB"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            descriptor = -1
            return file.read(_MAX_PRIVATE_FILE_BYTES + 1)
    except OSError as error:
        raise CloudflareManagedOAuthError("Private runtime input is unreadable") from error
    except UnicodeError as error:
        raise CloudflareManagedOAuthError("Private runtime input must be UTF-8") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _update_private_runtime_file(path: Path, updates: Mapping[str, str]) -> None:
    contents = _read_private_text(path)
    rendered_updates = {name: f"{name}={json.dumps(value)}" for name, value in updates.items()}
    seen: set[str] = set()
    lines: list[str] = []
    for line in contents.splitlines():
        match = _ENV_ASSIGNMENT.match(line.strip())
        name = match.group(1) if match is not None else ""
        if name in rendered_updates:
            if name not in seen:
                lines.append(rendered_updates[name])
                seen.add(name)
            continue
        lines.append(line)
    lines.extend(rendered_updates[name] for name in rendered_updates if name not in seen)
    rendered = "\n".join(lines) + "\n"
    descriptor = -1
    temporary_path = ""
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=".runtime.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            descriptor = -1
            file.write(rendered)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise CloudflareManagedOAuthError("Protected runtime settings could not be updated") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


async def _deny(send: Send, *, websocket: bool) -> None:
    if websocket:
        await send({"type": "websocket.close", "code": 1008, "reason": "Access denied"})
        return
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_DENIED_BODY)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _DENIED_BODY})


def _peer_is_loopback(client: object) -> bool:
    """Report whether the real transport peer is a loopback address.

    An absent, non-numeric, or unparsable peer is treated as remote so that
    unusual transports fail closed into requiring a verified assertion.
    """
    if not isinstance(client, (tuple, list)) or not client:
        return False
    host = str(client[0]).strip()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", maxsplit=1)[0])
    except ValueError:
        return False
    return address.is_loopback


def _header_host(headers: list[tuple[bytes, bytes]]) -> str:
    values = [value for name, value in headers if name == b"host"]
    if len(values) != 1:
        return ""
    try:
        parsed = urlparse("//" + values[0].decode("ascii"))
        return (parsed.hostname or "").rstrip(".").lower()
    except (UnicodeError, ValueError):
        return ""


def _validated_team_domain(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not host.endswith(".cloudflareaccess.com")
        or host == "cloudflareaccess.com"
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not _is_public_dns_name(host)
    ):
        raise CloudflareManagedOAuthError(
            "HEAVENLY_CLOUDFLARE_TEAM_DOMAIN must be a Cloudflare Access team origin"
        )
    return f"https://{host}"


def _validated_public_host(value: str) -> str:
    host = value.rstrip(".").lower()
    if not _is_public_dns_name(host):
        raise CloudflareManagedOAuthError(
            "HEAVENLY_MCP_PUBLIC_HOST must be a public DNS hostname"
        )
    return host


def _is_public_dns_name(host: str) -> bool:
    if not host or len(host) > 253 or "." not in host or any(character.isspace() for character in host):
        return False
    if any(character in host for character in ":/@?#[]"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
