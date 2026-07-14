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

RED checkpoint: `2a41e7d`. Additional RED checkpoints cover persistence,
operator lifecycle, MCP/storage exposure, provider session paging, stored
credential tampering, and callback failure behavior.

## GREEN

The implementation now includes:

- exact Google Web OAuth client validation and protected Keychain/keyring import;
- state, PKCE, offline authorization, token refresh, and revocation;
- Google Health v4 identity verification and supported data-type mapping from
  the explicit metric allowlist;
- bounded seven-day pull sync, one-hour checkpoint overlap, pagination, and the
  provider's 25-record sleep/session page limit;
- immutable raw provenance before allowlisted normalized event upserts;
- redacted CLI status/import/connect/sync/disconnect commands and MCP runtime
  dispatch through the existing health storage boundary.

Primary GREEN checkpoints: `0bb68de`, `811d71e`, `c146ce3`, `d9e7e8c`,
`391abe9`, `3d45517`, and `b227f5b`.

## Verification

```text
uv run pytest --cov=src/heavenly_health --cov-report=term-missing --cov-fail-under=80
240 passed; total coverage 81.23%

uv run ruff check src tests
passed

uv run pyright src
0 errors, 0 warnings
```

Live Google OAuth/data verification is intentionally performed during owner
onboarding because no OAuth client, token, or health record belongs in this
repository or its CI. The connector contract is tested with isolated HTTP and
credential-vault doubles; the operator guide provides the exact live acceptance
sequence.
