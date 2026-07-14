# Garmin connector TDD evidence

## Journeys

- As an approved Garmin Connect Developer Program operator, I can import the
  partner-issued OAuth endpoints, scopes, and resource paths without committing
  the client secret.
- As an owner, I can authorize through OAuth 2.0 with state, PKCE, an exact
  loopback callback, refresh, and revocation.
- As an owner, I can pull bounded Garmin Health resources and normalize only
  explicitly allowlisted metrics into the existing provenance pipeline.
- As an MCP user, I can trigger Garmin synchronization without access to Garmin
  credentials or raw payloads.

## RED

The initial focused test imports `heavenly_health.providers.garmin`, which does
not exist before implementation. Collection failure is the intended compile-time
RED checkpoint.

RED checkpoint: `70da718`. Additional RED checkpoints cover the operator
lifecycle and shared MCP/storage exposure.

## GREEN

The implementation now includes:

- owner-only import of Garmin partner-issued OAuth/API configuration into the
  operating-system credential vault;
- exact loopback callback, state, PKCE, token exchange/refresh, and optional
  remote revocation;
- public-HTTPS endpoint and relative resource-path validation;
- partner-configured identity and dailies/sleeps/body-composition/epochs/
  Pulse-Ox/respiration pull resources;
- bounded seven-day pull sync, one-hour checkpoint overlap, pagination,
  immutable raw provenance, and allowlisted normalization;
- redacted CLI status/import/connect/sync/disconnect commands and MCP runtime
  dispatch through the existing health storage boundary.

Primary GREEN checkpoints: `e8943bb`, `1c4c862`, and `d9e7e8c`.

## Verification

```text
uv run pytest --cov=src/heavenly_health --cov-report=term-missing --cov-fail-under=80
244 passed; total coverage 81.16%

uv run ruff check src tests
passed

uv run pyright src
0 errors, 0 warnings
```

Garmin live verification cannot be performed from public documentation or CI.
Garmin exposes its evaluation environment, endpoint reference, and project
configuration only after Developer Program approval. The adapter therefore
requires imported partner-issued values and fails closed instead of embedding
or guessing them. Live approval/evaluation remains an explicit operator gate.
