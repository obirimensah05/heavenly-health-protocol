# Connect WHOOP securely

> **Current status:** implemented native connector. Heavenly imports an
> owner-only WHOOP env file, runs browser OAuth with a pasted redirect URL,
> refreshes tokens, and performs bounded pull synchronization into the
> raw-provenance and allowlisted normalized tables. Webhooks are not
> implemented. Reading data requires an active WHOOP membership on the
> authorizing account; authorization alone succeeds without one.

## Connect with the CLI

```bash
install -d -m 700 "$HOME/.config/heavenly"
${EDITOR:-vi} "$HOME/.config/heavenly/whoop.env"   # see keys below; chmod 600
heavenly provider whoop import-client "$HOME/.config/heavenly/whoop.env"
heavenly provider whoop connect
heavenly provider whoop sync --limit 1000
```

`whoop.env` needs exactly these keys from your WHOOP developer app:

```text
WHOOP_CLIENT_ID=...
WHOOP_CLIENT_SECRET=...
WHOOP_REDIRECT_URI=...   # the redirect URL registered on the app
WHOOP_SCOPES=offline read:recovery read:cycles read:sleep read:workout read:profile
```

`connect` opens WHOOP in your browser; after approval, paste the redirected
URL back into the terminal. Tokens go to the operating-system credential
vault, never to disk or Git. WHOOP's edge WAF requires a browser user agent;
the connector handles this automatically.

Use this route when WHOOP—not an Apple/Google mirror—is the authoritative source. This walkthrough uses WHOOP OAuth 2.0 authorization-code flow. Never paste a callback URL, authorization code, client secret, access token, or refresh token into chat.

## 1. Prerequisites

- A WHOOP developer account and application in the WHOOP Developer Dashboard.
- A real WHOOP member designated as a development user for end-to-end testing. A development user is a real member who authorizes the app, not synthetic health data.
- A public privacy-policy URL describing requested data, purpose, processing/storage/sharing, retention/deletion, revocation, and contact. It must contain no credentials or health records.
- A callback receiver chosen before registration.
- For continuous sync, durable encrypted token storage and a scheduler. For webhooks, a separate deployed provider-aware HTTPS receiver.

Future onboarding is designed to detect Supabase CLI, `cloudflared`, compatible MCP clients, and supported APIs. That implementation must preview every dashboard, database, tunnel, legal-site, and webhook change and wait for approval before changing anything external; the current CLI does not perform these actions.

## 2. Select data and least-privilege scopes

| Metric or feature | WHOOP scope | Default? |
| --- | --- | --- |
| Physiological cycles/strain and the cycle identity required to relate recovery | `read:cycles` | Yes |
| Recovery score, resting heart rate, HRV, SpO2, skin temperature when present | `read:recovery` | Yes |
| Sleeps/naps, sleep stages, sleep performance and need | `read:sleep` | Yes |
| Workouts, sport, strain, duration, heart-rate summary | `read:workout` | Yes |
| Refresh token for continuous/background synchronization | `offline` | Yes when continuous sync is selected |
| Member profile/name | `read:profile` | Opt in only if the product truly needs profile data |
| Height, weight, max heart rate | `read:body_measurement` | Opt in only for analyses that use them |

Paste-ready default scope value:

```text
offline read:cycles read:recovery read:sleep read:workout
```

Without `offline`, WHOOP does not return the refresh token required by the
continuous-sync behavior in this guide. Omit it only for an intentionally
short-lived, foreground-only connection. Do not request write, profile, or body
scopes “just in case.” Availability varies by member/device and is verified only
after consent and a real read.

## 3. Register the dashboard application

1. Open the WHOOP Developer Dashboard and create an application.
2. Enter an accurate application name, contact, and public privacy-policy URL.
3. Select only the scopes mapped above.
4. Register the callback that the chosen Heavenly receiver actually serves.

The OAuth callback and webhook are different endpoints:

```text
OAuth callback concept: https://<controlled-heavenly-origin>/providers/whoop/oauth/callback
Webhook concept:        https://<controlled-heavenly-origin>/providers/whoop/webhook
```

The authorization request's `redirect_uri` must be byte-for-byte identical to the dashboard entry: scheme, host, port, path, case, and trailing slash. The callback receives `code` and `state`, validates `state`, exchanges the one-time code, then discards it. It is not an ingestion endpoint. Do not use an Apple Health webhook, `app.whoop.com`, or `id.whoop.com` as a production callback. A `whoop://` callback works only for a shipped native app that owns that URI scheme. If dashboard validation limits accepted URI forms, use only the exact accepted, controlled value; do not invent an unregistered local URI.

For a temporary local-development manual handoff, a registered public page may preserve the returned query string so the user can paste the full URL into the waiting local Heavenly terminal. Never paste it into chat or logs. Production must use a controlled receiver that performs the exchange server-side.

## 4. Store credentials and tokens

- **Native macOS:** import the client ID/secret interactively into macOS Keychain. Heavenly stores access/refresh tokens in Keychain and only non-secret provider/account aliases and sync checkpoints in its state directory. Restrict any unavoidable temporary credential file to the owner (`0600`), import it, then securely remove it.
- **Docker/server:** use the deployment secret manager or an owner-only host secret file mounted read-only under `/run/secrets/`; never put values in Compose, `.env`, image layers, a named data export, or the repository. Encrypt refresh tokens at rest; keep the key outside the database and container image.

Redact diagnostics. Logs may report “client configured,” expiry time, granted scopes, and a pseudonymous connection ID; they must not print token values or returned callback URLs.

## 5. Authorize and refresh

1. Heavenly generates a cryptographically random, single-use `state`, binds it to the initiating browser/session, and starts the WHOOP authorization endpoint:
   `https://api.prod.whoop.com/oauth/oauth2/auth`.
2. Confirm that the consent screen lists only the selected scopes, then approve.
3. The callback verifies exact state and redirect, rejects replay/expiry, and exchanges the code at:
   `https://api.prod.whoop.com/oauth/oauth2/token`.
4. Store the token response atomically before reporting success.
5. Refresh before expiry using the stored refresh token. Serialize refreshes per connection so two workers cannot race. On `invalid_grant`, stop automatic retries and ask for reconnection; never loop on a revoked token.

## 6. Verify the first read

Run verification with the development user and a narrow recent window:

1. Read the current member identity only if `read:profile` was explicitly granted; otherwise use the provider's stable token/user context without expanding consent.
2. Fetch recent cycles, then related recovery, sleep, and workout resources for granted scopes.
3. Page until the window is complete; preserve provider IDs and timestamps.
4. Confirm the stored connection's granted scopes and source identity match the authorizing account.

Expected result:

```text
WHOOP connection verified
Scopes: offline, read:cycles, read:recovery, read:sleep, read:workout
Identity: stable provider connection recorded (token redacted)
Read: API succeeded; recent records imported or a valid empty window reported
```

A `200` with no records is not proof of historical-data availability, but it is distinct from an authentication failure. Verify at least one real record before declaring data ingestion complete.

## 7. Backfill, retries, deduplication, and offline behavior

- Backfill bounded windows and paginate; checkpoint only after each page is durably stored.
- Retry network errors, `429`, and provider `5xx` with bounded exponential backoff and jitter, honoring `Retry-After`. Do not retry `400/401/403` blindly.
- Use immutable raw records keyed by `{provider}:{resource}:{native_id}`. A recovery notification references its associated sleep UUID in WHOOP v2; fetch the authoritative cycle/recovery and use the fetched record identities. Derive normalized metric IDs from the raw ID, for example `...:metric:hrv`.
- Upsert on stable IDs so OAuth verification, scheduled polling, webhook fetches, and backfills do not duplicate records.
- The strap/phone can be offline and WHOOP cloud processing can lag. Record source time and `data_through`; a successful empty read should schedule a later bounded retry rather than invent zeros.
- If Heavenly is offline, resume from the durable checkpoint and overlap the previous window slightly; idempotency makes overlap safe.

## 8. Add webhooks only after the receiver exists

Do **not** register webhooks during initial OAuth. First deploy a WHOOP-specific receiver that:

- validates the provider request according to current WHOOP requirements;
- maps the notification to a pseudonymous connected user without embedding tokens;
- acknowledges quickly and queues work;
- fetches the changed v2 resource with that user's OAuth token;
- deduplicates repeated/out-of-order delivery and tolerates fetch-before-processing delay.

After the receiver passes authenticated and replay tests, preview the dashboard registration and ask for approval. Webhooks are change signals, not authoritative health payloads; polling remains the recovery path for missed notifications.

## 9. Troubleshooting

| Symptom | Action |
| --- | --- |
| `redirect_uri` mismatch | Copy the dashboard URI and compare every character with both authorization and token-exchange values |
| State missing/mismatch | Stop; do not exchange the code. Restart from Heavenly to prevent CSRF/replay |
| Token endpoint `403` / edge error `1010` | Before consuming another real code, send a harmless invalid-code probe with `Accept: application/json` and a compatible explicit user agent; if it becomes an OAuth error, fix transport/WAF handling |
| `401 invalid_client` | Re-copy/regenerate client ID and secret from the same app; do not expose them in diagnostics |
| `invalid_grant` | Code was reused/expired, redirect differs, or refresh was revoked; restart authorization once |
| `401` after working | Serialize refresh, refresh once, then reconnect if refresh fails |
| `403` on one resource | Scope was not granted; do not silently broaden it |
| No recent recovery | Verify a scored sleep/cycle exists and allow cloud processing delay |
| Webhook repeats or arrives out of order | Queue and refetch by provider ID; stable upserts make delivery idempotent |

## 10. Revoke and delete

1. Pause schedules and webhook work for the connection.
2. Revoke/disconnect the app through the member's WHOOP connected-app controls and use any current provider revocation endpoint supported by the integration.
3. Delete the server-side access/refresh tokens, client-instance secret material, pending OAuth state, and webhook mapping. Keep shared developer credentials only if other approved connections still need the app.
4. Remove the webhook registration after preview and approval if no connections use it.
5. Offer separate deletion choices for normalized data, immutable restricted raw data, derived analyses, checkpoints, and backups according to the published retention policy. Record only a non-sensitive deletion audit marker.
6. Verify that refresh and read calls fail and no scheduled job can recreate the connection. Reauthorization must create a new consented connection; deleted records must not silently return unless the user explicitly requests a new backfill.

## Official references

- <https://developer.whoop.com/docs/developing/getting-started/>
- <https://developer.whoop.com/docs/developing/oauth/>
- <https://developer.whoop.com/docs/developing/webhooks/>
