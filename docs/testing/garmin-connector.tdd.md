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

GREEN, integration, coverage, and release evidence will be appended after the
implementation passes.

