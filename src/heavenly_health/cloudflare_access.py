"""Narrow Cloudflare Access policy automation for Heavenly MCP.

The API token is intentionally read only at runtime. It is never written to
project files, logs, or CLI output.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Mapping

import httpx

API_BASE_URL = "https://api.cloudflare.com/client/v4"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class CloudflareAccessConfigurationError(ValueError):
    """Raised when required runtime-only Cloudflare configuration is absent."""


def load_cloudflare_api_token(
    environ: Mapping[str, str] | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    platform: str = sys.platform,
) -> str:
    """Load a token from process memory or the dedicated macOS Keychain item."""
    values = os.environ if environ is None else environ
    token = values.get("CLOUDFLARE_API_TOKEN", "").strip()
    if token:
        return token
    if platform != "darwin":
        raise CloudflareAccessConfigurationError(
            "CLOUDFLARE_API_TOKEN is missing and Keychain fallback is unavailable"
        )
    command = [
        "security",
        "find-generic-password",
        "-s",
        "heavenly-cloudflare-api-token",
        "-w",
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CloudflareAccessConfigurationError(
            "Cloudflare API token is unavailable from the environment and Keychain"
        ) from error
    token = result.stdout.strip() if result.returncode == 0 else ""
    if not token:
        raise CloudflareAccessConfigurationError(
            "Cloudflare API token is unavailable from the environment and Keychain"
        )
    return token


@dataclass(frozen=True)
class ManagedOAuthPlan:
    """Validated, redacted plan for one exact self-hosted Access application."""

    application_id: str
    application_name: str
    application_type: str
    domain: str
    managed_oauth_enabled: bool
    exact_owner_identities: int
    audience: str = dataclass_field(repr=False)
    allowed_emails: frozenset[str] = dataclass_field(repr=False)

    def summary(self) -> dict[str, str | bool | int]:
        """Return only values safe for CLI preview and logs."""
        return {
            "application_id": self.application_id,
            "application_name": self.application_name,
            "application_type": self.application_type,
            "domain": self.domain,
            "managed_oauth_enabled": self.managed_oauth_enabled,
            "exact_owner_identities": self.exact_owner_identities,
        }


def normalize_email(email: str) -> str:
    """Return a normalized email address suitable for an exact Access rule."""
    normalized = email.strip().lower()
    if not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("A valid email address is required")
    return normalized


def add_email_to_include_rules(rules: list[dict[str, Any]], email: str) -> list[dict[str, Any]]:
    """Append one exact-email rule without disturbing unrelated Access rules."""
    normalized_email = normalize_email(email)
    updated_rules = [dict(rule) for rule in rules]

    for rule in rules:
        email_rule = rule.get("email")
        if isinstance(email_rule, dict) and str(email_rule.get("email", "")).lower() == normalized_email:
            return updated_rules

    updated_rules.append({"email": {"email": normalized_email}})
    return updated_rules


def _validated_path_segment(value: str, name: str) -> str:
    if PATH_SEGMENT_PATTERN.fullmatch(value) is None:
        raise CloudflareAccessConfigurationError(f"{name} must be one opaque identifier")
    return value


@dataclass
class CloudflareManagedOAuthClient:
    """Plan and reconcile Managed OAuth on one exact Access application."""

    account_id: str
    application_id: str
    api_token: str
    http_client: httpx.Client | None = None

    def __post_init__(self) -> None:
        self.account_id = _validated_path_segment(
            self.account_id,
            "HEAVENLY_CLOUDFLARE_ACCOUNT_ID",
        )
        self.application_id = _validated_path_segment(
            self.application_id,
            "HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID",
        )
        if not self.api_token.strip():
            raise CloudflareAccessConfigurationError("CLOUDFLARE_API_TOKEN must not be empty")

    @classmethod
    def from_environment(
        cls,
        *,
        api_token: str | None = None,
    ) -> CloudflareManagedOAuthClient:
        required = {
            "CLOUDFLARE_API_TOKEN": api_token or load_cloudflare_api_token(),
            "HEAVENLY_CLOUDFLARE_ACCOUNT_ID": os.environ.get(
                "HEAVENLY_CLOUDFLARE_ACCOUNT_ID"
            ),
            "HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID": os.environ.get(
                "HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID"
            ),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise CloudflareAccessConfigurationError(
                "Missing runtime configuration: " + ", ".join(missing)
            )
        return cls(
            account_id=required["HEAVENLY_CLOUDFLARE_ACCOUNT_ID"] or "",
            application_id=required["HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID"] or "",
            api_token=required["CLOUDFLARE_API_TOKEN"] or "",
        )

    @property
    def application_path(self) -> str:
        return f"/accounts/{self.account_id}/access/apps/{self.application_id}"

    def _request(self, method: str, **kwargs: Any) -> dict[str, Any]:
        client = self.http_client or httpx.Client(timeout=30)
        close_client = self.http_client is None
        try:
            response = client.request(
                method,
                f"{API_BASE_URL}{self.application_path}",
                headers={"Authorization": f"Bearer {self.api_token}"},
                **kwargs,
            )
            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeError("Cloudflare returned a non-JSON response") from error
            if response.is_error or not payload.get("success", False):
                messages = payload.get("errors") or [{"message": response.reason_phrase}]
                message = "; ".join(
                    str(item.get("message", "Cloudflare API request failed"))
                    for item in messages
                )
                raise RuntimeError(f"Cloudflare Access API request failed: {message}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Cloudflare Access API returned an unexpected response")
            return result
        finally:
            if close_client:
                client.close()

    def get_application(self) -> dict[str, Any]:
        """Fetch the one configured application."""
        return self._request("GET")

    def _validated_application(
        self,
        public_host: str,
    ) -> tuple[dict[str, Any], ManagedOAuthPlan]:
        host = public_host.strip().rstrip(".").lower()
        if not host or any(character in host for character in ":/@?#[]"):
            raise CloudflareAccessConfigurationError(
                "HEAVENLY_MCP_PUBLIC_HOST must be a DNS hostname"
            )
        application = self.get_application()
        if application.get("id") != self.application_id:
            raise RuntimeError("Cloudflare Access returned a different application")
        if application.get("type") != "self_hosted":
            raise RuntimeError("Cloudflare Access target must be a self-hosted application")
        if str(application.get("domain", "")).rstrip(".").lower() != host:
            raise RuntimeError("Cloudflare Access application does not match the configured public host")
        name = application.get("name")
        audience = application.get("aud")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("Cloudflare Access application must have a readable name")
        if not isinstance(audience, str) or not audience.strip():
            raise RuntimeError("Cloudflare Access application must have an audience tag")
        allowed_emails = _exact_owner_emails(application.get("policies"))
        oauth_configuration = application.get("oauth_configuration")
        enabled = isinstance(oauth_configuration, dict) and (
            oauth_configuration.get("enabled") is True
        )
        return application, ManagedOAuthPlan(
            application_id=self.application_id,
            application_name=name,
            application_type="self_hosted",
            domain=host,
            managed_oauth_enabled=enabled,
            exact_owner_identities=len(allowed_emails),
            audience=audience,
            allowed_emails=allowed_emails,
        )

    def managed_oauth_plan(self, public_host: str) -> ManagedOAuthPlan:
        """Validate and summarize the exact application without changing Cloudflare."""
        _application, plan = self._validated_application(public_host)
        return plan

    def enable_managed_oauth(self, public_host: str) -> bool:
        """Reconcile secure CLI-compatible Managed OAuth settings idempotently."""
        application, _plan = self._validated_application(public_host)
        update_payload = deepcopy(application)
        existing_oauth = update_payload.get("oauth_configuration")
        oauth = dict(existing_oauth) if isinstance(existing_oauth, dict) else {}
        oauth["enabled"] = True
        existing_registration = oauth.get("dynamic_client_registration")
        registration = (
            dict(existing_registration) if isinstance(existing_registration, dict) else {}
        )
        registration.update(
            {
                "enabled": True,
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
            }
        )
        oauth["dynamic_client_registration"] = registration
        existing_grant = oauth.get("grant")
        grant = dict(existing_grant) if isinstance(existing_grant, dict) else {}
        grant.setdefault("access_token_lifetime", "15m")
        grant.setdefault("session_duration", "336h")
        oauth["grant"] = grant
        if oauth == existing_oauth:
            return False
        update_payload["oauth_configuration"] = oauth
        self._request("PUT", json=update_payload)
        return True


def _exact_owner_emails(policies_value: object) -> frozenset[str]:
    if not isinstance(policies_value, list) or not policies_value:
        raise RuntimeError("Cloudflare Access application must use exact email allow policies")
    allowed_emails: set[str] = set()
    for policy in policies_value:
        if not isinstance(policy, dict):
            raise RuntimeError("Cloudflare Access application must use exact email allow policies")
        decision = policy.get("decision")
        if decision == "bypass":
            raise RuntimeError("Cloudflare Access application must use exact email allow policies")
        if decision != "allow":
            continue
        include = policy.get("include")
        if not isinstance(include, list) or not include:
            raise RuntimeError("Cloudflare Access application must use exact email allow policies")
        for rule in include:
            if not isinstance(rule, dict) or set(rule) != {"email"}:
                raise RuntimeError("Cloudflare Access application must use exact email allow policies")
            email_rule = rule.get("email")
            if not isinstance(email_rule, dict) or set(email_rule) != {"email"}:
                raise RuntimeError("Cloudflare Access application must use exact email allow policies")
            try:
                allowed_emails.add(normalize_email(str(email_rule["email"])))
            except (KeyError, ValueError) as error:
                raise RuntimeError(
                    "Cloudflare Access application must use exact email allow policies"
                ) from error
    if not allowed_emails or len(allowed_emails) > 20:
        raise RuntimeError("Cloudflare Access application must use exact email allow policies")
    return frozenset(allowed_emails)


@dataclass
class CloudflareAccessClient:
    """Manage exactly one pre-existing Cloudflare Access policy."""

    account_id: str
    application_id: str
    policy_id: str
    api_token: str
    http_client: httpx.Client | None = None

    @classmethod
    def from_environment(cls) -> "CloudflareAccessClient":
        required = {
            "CLOUDFLARE_API_TOKEN": os.environ.get("CLOUDFLARE_API_TOKEN"),
            "HEAVENLY_CLOUDFLARE_ACCOUNT_ID": os.environ.get("HEAVENLY_CLOUDFLARE_ACCOUNT_ID"),
            "HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID": os.environ.get(
                "HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID"
            ),
            "HEAVENLY_CLOUDFLARE_ACCESS_POLICY_ID": os.environ.get(
                "HEAVENLY_CLOUDFLARE_ACCESS_POLICY_ID"
            ),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise CloudflareAccessConfigurationError(
                "Missing runtime configuration: " + ", ".join(missing)
            )
        return cls(
            account_id=required["HEAVENLY_CLOUDFLARE_ACCOUNT_ID"] or "",
            application_id=required["HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID"] or "",
            policy_id=required["HEAVENLY_CLOUDFLARE_ACCESS_POLICY_ID"] or "",
            api_token=required["CLOUDFLARE_API_TOKEN"] or "",
        )

    @property
    def policy_path(self) -> str:
        return (
            f"/accounts/{self.account_id}/access/apps/"
            f"{self.application_id}/policies/{self.policy_id}"
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client = self.http_client or httpx.Client(timeout=30)
        close_client = self.http_client is None
        try:
            response = client.request(
                method,
                f"{API_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {self.api_token}"},
                **kwargs,
            )
            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeError("Cloudflare returned a non-JSON response") from error
            if response.is_error or not payload.get("success", False):
                messages = payload.get("errors") or [{"message": response.reason_phrase}]
                message = "; ".join(str(item.get("message", "Cloudflare API request failed")) for item in messages)
                raise RuntimeError(f"Cloudflare Access API request failed: {message}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Cloudflare Access API returned an unexpected response")
            return result
        finally:
            if close_client:
                client.close()

    def get_policy(self) -> dict[str, Any]:
        """Read the configured Access policy without exposing the API token."""
        return self._request("GET", self.policy_path)

    def _validated_allow_policy(self) -> dict[str, Any]:
        """Return only the exact configured allow policy, failing closed otherwise."""
        policy = self.get_policy()
        if policy.get("id") != self.policy_id:
            raise RuntimeError("Cloudflare Access returned a different policy than the configured policy ID")
        if policy.get("decision") != "allow":
            raise RuntimeError("Cloudflare Access target must be an exact allow policy")
        if not isinstance(policy.get("name"), str) or not policy["name"].strip():
            raise RuntimeError("Cloudflare Access allow policy must have a readable name")
        if not isinstance(policy.get("include"), list):
            raise RuntimeError("Cloudflare Access policy has no valid include rules")
        return policy

    def policy_summary(self) -> dict[str, str]:
        """Return a secret-free identity summary for the mandatory preview step."""
        policy = self._validated_allow_policy()
        return {
            "account_id": self.account_id,
            "application_id": self.application_id,
            "policy_id": self.policy_id,
            "policy_name": str(policy["name"]),
            "decision": str(policy["decision"]),
        }

    def allow_email(self, email: str) -> bool:
        """Add an exact email to the policy. Returns False when it already exists."""
        policy = self._validated_allow_policy()
        include = policy.get("include")
        if not isinstance(include, list):
            raise RuntimeError("Cloudflare Access policy has no valid include rules")
        updated_include = add_email_to_include_rules(include, email)
        if updated_include == include:
            return False

        mutable_fields = (
            "name",
            "decision",
            "precedence",
            "include",
            "exclude",
            "require",
            "approval_required",
            "approval_groups",
            "isolation_required",
            "purpose_justification_required",
            "purpose_justification_prompt",
            "session_duration",
        )
        update_payload = {field: policy[field] for field in mutable_fields if field in policy}
        update_payload["include"] = updated_include
        self._request("PUT", self.policy_path, json=update_payload)
        return True
