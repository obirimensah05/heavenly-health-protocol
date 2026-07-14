import pytest

from heavenly_health.identity import (
    deterministic_source_record_id,
    normalized_event_source_record_id,
    provider_source_record_id,
)


def test_preserves_provider_native_identifier() -> None:
    assert provider_source_record_id("whoop", "workout", "550e8400-e29b-41d4-a716-446655440000") == (
        "whoop:workout:550e8400-e29b-41d4-a716-446655440000"
    )


def test_fallback_identity_is_deterministic_for_sources_without_native_ids() -> None:
    fields = {
        "metric_type": "steps",
        "start_time_utc": "2026-07-13T05:00:00Z",
        "end_time_utc": "2026-07-13T05:15:00Z",
        "value": 124,
        "unit": "count",
    }

    first = deterministic_source_record_id("apple_health", "quantity_sample", fields)
    second = deterministic_source_record_id("apple_health", "quantity_sample", dict(reversed(list(fields.items()))))

    assert first == second
    assert first.startswith("apple_health:quantity_sample:sha256:")
    assert len(first.rsplit(":", 1)[1]) == 64


def test_normalized_event_identity_is_unique_per_metric_from_one_raw_record() -> None:
    raw_id = "whoop:cycle:123456"

    assert normalized_event_source_record_id(raw_id, "recovery_score") == (
        "whoop:cycle:123456:metric:recovery_score"
    )
    assert normalized_event_source_record_id(raw_id, "hrv_rmssd") != normalized_event_source_record_id(
        raw_id, "recovery_score"
    )


def test_rejects_blank_identity_parts() -> None:
    with pytest.raises(ValueError):
        provider_source_record_id("", "sleep", "native-id")
