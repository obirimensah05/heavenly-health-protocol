"""Stable identifiers for records imported from health providers."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json


def _part(value: object, label: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    if ":" in normalized and label in {"provider", "resource"}:
        raise ValueError(f"{label} must not contain ':'")
    return normalized


def provider_source_record_id(provider: str, resource: str, native_id: str) -> str:
    """Preserve a provider's immutable record ID in a namespaced form."""
    return ":".join(
        (
            _part(provider, "provider"),
            _part(resource, "resource"),
            _part(native_id, "native_id"),
        )
    )


def normalized_event_source_record_id(raw_source_record_id: str, metric_type: str) -> str:
    """Create the event identity when one provider record yields many metrics."""
    return f"{_part(raw_source_record_id, 'raw_source_record_id')}:metric:{_part(metric_type, 'metric_type')}"


def deterministic_source_record_id(
    provider: str,
    resource: str,
    stable_fields: Mapping[str, object],
) -> str:
    """Create a repeatable ID only when a source supplies no immutable ID.

    Callers must include only stable source facts such as metric type, original
    start/end timestamps, value, unit, and a device/account pseudonym if needed.
    """
    if not stable_fields:
        raise ValueError("stable_fields must not be empty")
    canonical = json.dumps(
        stable_fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    return provider_source_record_id(provider, resource, f"sha256:{digest}")
