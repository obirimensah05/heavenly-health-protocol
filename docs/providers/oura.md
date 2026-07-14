# Connect Oura securely

> **Current status:** manual provider walkthrough and connector specification.
> The checked-in Heavenly CLI does not yet import credentials, run Oura OAuth,
> receive webhooks, or synchronize Oura data.

Use Oura OAuth 2.0 authorization-code flow. Oura Personal Access Tokens were deprecated in December 2025 and are **not** an onboarding route, including for a single-user installation. Never paste a callback URL, authorization code, client secret, or token into chat.

## 1. Prerequisites

- An Oura account and an OAuth application at <https://cloud.ouraring.com/oauth/applications>.
- A callback receiver selected before registration.
- A public application website, privacy policy, and terms of service. The pages must accurately cover use, sharing, retention, deletion, revocation, and contact, with no credentials or health data.
- A real ring/account to verify live member records and sync timing. Oura sandbox data can test schemas but cannot prove real consent, ring data, or webhook delivery.
- Secure durable token storage and a scheduler.

The normal developer application limit is 10 authorized users; obtain Oura review/approval before exceeding the dashboard's current limit.

Heavenly may detect Supabase CLI, `cloudflared`, compatible MCP clients, and supported APIs. Detection is read-only. It previews legal-site publication, database/tunnel changes, dashboard values, and webhook creation and waits for explicit approval.

## 2. Choose exact scopes

Default read-only block:

```text
daily heartrate workout
```

| Scope | Data | Default? |
| --- | --- | --- |
| `daily` | Daily activity, readiness, and sleep summaries and related sleep resources | Yes |
| `heartrate` | Heart-rate time series | Yes |
| `workout` | Workout summaries | Yes |
| `spo2Daily` | Daily SpO2 average | Opt in |
| `personal` | Profile/body details | Opt in only when required |
| `email` | Email identity | Off; avoid when a pseudonymous provider identity is enough |
| `tag` | User-created tags | Opt in |
| `session` | Guided/unguided sessions | Opt in |

Scope names are case-sensitive. Do not request every scope preemptively. Expanding scope later requires a new explicit consent flow.

## 3. Register the application and exact callback

1. Create the application with accurate name, website, privacy, and terms URLs.
2. Register the callback the chosen receiver actually serves:

```text
https://<controlled-heavenly-origin>/providers/oura/oauth/callback
```

3. Copy the resulting client credentials directly into the local/server secret import flow; never into source or chat.

The `redirect_uri` sent to authorization and token exchange must exactly match the registered URI, including scheme, host, port, path, case, and trailing slash. The callback receives only the OAuth result, validates `state`, exchanges the one-time code, and redirects to a non-sensitive success page. It is not the future Oura webhook URL.

For local-only development, use a loopback callback only if the Oura dashboard accepts that exact URI and the local receiver is already listening. Otherwise use a controlled HTTPS callback; do not improvise an unrelated landing page or ask the user to paste the result into chat.

## 4. Secret storage

- **Native macOS:** use macOS Keychain for client secret and token set. Store only a non-secret connection alias, scopes, expiry, and checkpoints in application state. If credentials are downloaded, import them interactively, set the file to owner-only while it exists, then remove it.
- **Docker/server:** inject the client secret through the deployment secret manager or an owner-only host file mounted read-only under `/run/secrets/`. Encrypt user token rows at rest with a key outside the database and image. Do not use Compose environment literals, committed `.env`, image layers, logs, Drive/iCloud, or agent memory.

Diagnostics must redact callback query strings, authorization codes, access/refresh tokens, verification tokens, and client credentials.

## 5. Authorize and rotate refresh tokens safely

1. Generate a high-entropy one-time `state`, bind it to the initiating session, and open:
   `https://cloud.ouraring.com/oauth/authorize`.
2. Request `response_type=code`, the exact `redirect_uri`, client ID, and selected space-delimited scopes.
3. Confirm the consent screen and approve.
4. Verify `state`; exchange the code using a form-encoded request to:
   `https://api.ouraring.com/oauth/token`.
5. Atomically store the returned access token, refresh token, expiry, and granted scopes.

**Critical refresh rule:** Oura refresh tokens are single-use. Serialize refreshes per connection. In one database transaction/compare-and-swap operation, exchange the current refresh token and replace **both** access and refresh tokens. Commit only the new pair. Never retry a timed-out refresh concurrently with the old token: first determine whether another worker already committed the successor. If the successor cannot be recovered, reconnect rather than repeatedly consuming tokens.

## 6. Verify identity and first read

Use a recent bounded UTC/local-date window and paginate:

1. If `personal` was explicitly granted, call the personal-info endpoint and bind its stable provider identity to the connection. Otherwise bind the stable identity available from the authorized token/resource context without requesting profile/email.
2. With `daily`, read `daily_sleep`, `daily_readiness`, and `daily_activity`, plus relevant sleep resources.
3. With `heartrate`, read a narrow heart-rate interval.
4. With `workout`, read recent workouts.
5. Confirm source timestamps, units, IDs, granted scopes, and account binding before normalizing.

Expected result:

```text
Oura connection verified
Scopes: daily, heartrate, workout
Identity: stable provider connection recorded (tokens redacted)
Read: API succeeded; recent records imported or a valid empty window reported
```

A sandbox response proves only schema handling. Mark sandbox rows `is_synthetic=true` and `ingest_mode=synthetic_test`; never mix them into live analysis. A live API success with an empty period is not a failure, but verify at least one real ring record before declaring ingestion complete.

## 7. Backfill, retries, deduplication, and delayed sync

- Backfill bounded date ranges and follow pagination. Advance a durable checkpoint only after a page is stored.
- Retry network failures, `429`, and `5xx` with bounded exponential backoff and jitter; honor `Retry-After`. Do not blindly retry `400`, `401`, `403`, or `invalid_grant`.
- Key immutable raw rows by `oura:{resource}:{native_id}` and normalized metrics by `{raw_id}:metric:{metric_type}`. Upsert on stable identity so backfill, polling, verification, and webhooks converge.
- Ring → phone → Oura cloud sync is not instantaneous. If the ring/phone or Heavenly is offline, leave the checkpoint unchanged, report stale `data_through`, and retry with a small overlap after connectivity returns. Never convert missing/delayed values to zero.
- Accept revised daily scores by updating version/observed metadata while preserving immutable source snapshots or audit history according to the storage contract.

## 8. Add webhooks later

Do not create subscriptions until the Oura-specific HTTPS receiver is deployed and tested. After initial backfill:

1. Preview and approve subscription creation at `https://api.ouraring.com/v2/webhook/subscription`.
2. Use a separate callback such as `https://<controlled-heavenly-origin>/providers/oura/webhook` and a high-entropy verification token stored as a secret.
3. Handle the GET verification challenge.
4. For POST delivery, validate `x-oura-timestamp` and `x-oura-signature` using Oura's current HMAC-SHA256 rule over `timestamp + JSON.stringify(body)` with the client secret; reject stale/replayed messages.
5. Acknowledge quickly, queue work, fetch the authoritative resource, and upsert it.
6. Monitor subscription expiration and preview renewal before changing it.

Webhooks can arrive after mobile sync, repeat, or arrive out of order. Scheduled overlap polling remains the recovery mechanism.

## 9. Troubleshooting

| Symptom | Action |
| --- | --- |
| Callback/`invalid_grant` error | Compare the registered, authorization, and token-exchange `redirect_uri` byte for byte; restart if the code expired or was used |
| State mismatch | Stop and restart; never exchange the code |
| No refresh token | Reauthorize through the initial consent flow; do not use a PAT workaround |
| Refresh works once, then fails | Look for concurrent refresh. Recover the atomically stored successor token pair or reconnect |
| `invalid_scope` / `403` | Check exact scope spelling/case and granted scopes; ask consent before expansion |
| `401` | Refresh once through the serialized path; reconnect if refresh is revoked |
| Empty daily/heart-rate data | Verify date boundaries, pagination, ring-to-phone sync, and whether the chosen metric exists |
| Webhook verification fails | Check public HTTPS reachability and verification token without logging it |
| Signature fails | Validate raw request bytes/JSON serialization, timestamp concatenation, secret, clock skew, and replay window |
| Repeated rows | Upsert on native resource ID; never generate a new random ID on delivery |

## 10. Revoke and delete

1. Pause polling, backfill, refresh, and webhook queues.
2. Revoke the current access token using Oura's revoke endpoint (`https://api.ouraring.com/oauth/revoke?access_token=...`) through the connector so it is never exposed in shell history, and/or disconnect the application in Oura account controls.
3. Delete the stored token pair, pending OAuth state, verification token, account mapping, and per-user subscriptions. Preview shared subscription/application changes before applying them.
4. Offer separate deletion of normalized events, restricted raw records, analyses, checkpoints, and backups under the published retention policy; retain only a non-sensitive deletion audit marker.
5. Verify refresh/read now fail and jobs cannot recreate the connection. A later authorization is a new connection and must not restore deleted history unless the user requests a new backfill.

## Official references

- <https://cloud.ouraring.com/docs/authentication>
- <https://cloud.ouraring.com/v2/docs>
- <https://cloud.ouraring.com/docs/error-handling>
