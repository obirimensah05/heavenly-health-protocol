# Google Health connector TDD evidence

## Journeys

- As an owner, I can import a protected Google Web OAuth client without exposing
  its secret.
- As an owner, I can authorize read-only Google Health access with state, PKCE,
  offline refresh, exact callback validation, and revocation.
- As an owner, I can sync bounded, paginated Google Health API v4 records into
  immutable raw provenance and allowlisted normalized events.
- As an MCP user, I can request a Google Health sync without receiving provider
  credentials or raw payloads.

## RED

`uv run pytest tests/test_provider_common.py tests/test_google_health.py`
fails during collection because the new `heavenly_health.providers` package does
not exist. This is the intended compile-time RED for the connector boundary.

GREEN, integration, coverage, and release evidence will be appended after the
implementation passes.

