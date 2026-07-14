# Connect Google Health API v4 securely

> **Current status:** manual provider walkthrough and connector specification.
> The checked-in Heavenly CLI does not yet import the client, run the Google
> callback, refresh tokens, or synchronize Google Health data.

Use this route for supported Fitbit/Google Health data linked to the user's Google account. This is the **Google Health API v4** at `health.googleapis.com/v4`, not Google Fit, the legacy Fitbit Web API, Android Health Connect, or Google Cloud Healthcare API. The Google Health API is the new consumer API; Health Connect remains device-local, and Cloud Healthcare API is for clinical FHIR/DICOM workloads.

## 1. Prerequisites

- A Google account whose health source is linked to Fitbit/Google Health and has synced data. OAuth can succeed while returning no data if the selected account is not linked or has no records.
- A Google Cloud project whose product/application name is exactly **Heavenly**.
- Google Health API enabled and a **Web application / Web server** OAuth client.
- OAuth consent/branding, public privacy policy and terms appropriate for restricted health scopes.
- For Testing, the authorizing account explicitly listed as a test user.
- A local callback listener on port 8791 for this walkthrough.

All Google Health API scopes are Restricted. Moving the consent screen to In Production avoids Testing's short refresh-token lifetime, but **publishing status and Google verification are separate**. Do not claim verified/approved access merely because the app says In Production. Complete the current Google verification, privacy, security, and launch requirements before serving users beyond the allowed test/limited audience.

Heavenly may auto-detect the Supabase CLI, `cloudflared`, compatible MCP clients, and supported Google/cloud APIs. It only inspects availability. Every project/API, consent-screen, database, tunnel, or remote-access change is previewed and requires user approval. Local OAuth needs no tunnel.

## 2. Supported data and exact scopes

Paste this read-only three-scope block into consent/configuration:

```text
https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
https://www.googleapis.com/auth/googlehealth.sleep.readonly
```

| Scope | Typical selected data |
| --- | --- |
| `googlehealth.activity_and_fitness.readonly` | Steps, distance, exercise/workouts, active-zone minutes, floors, calories and related activity data |
| `googlehealth.health_metrics_and_measurements.readonly` | Heart rate, resting heart rate, HRV, respiratory rate, SpO2, temperature derivations, weight/body measurements when available |
| `googlehealth.sleep.readonly` | Sleep sessions/stages and related sleep data |

Request only scopes needed by selected metrics. Do not add write scopes. Keep `.ecg.readonly`, `.irn.readonly`, `.location.readonly`, `.nutrition.readonly`, and `.profile.readonly` off by default. If the user later opts into another category, show the new scope and purpose and obtain incremental consent.

## 3. Create the GCP application and OAuth client

1. Create/select a Google Cloud project and set the OAuth product name to **Heavenly**.
2. Enable **Google Health API** (v4), not Fitness API or Cloud Healthcare API.
3. Configure OAuth consent/branding, support contact, privacy policy, terms, and the three selected Restricted scopes.
4. While publishing status is **Testing**, add each authorizing account under **Test users**.
5. Create an OAuth client of type **Web application / Web server**.
6. Register this callback exactly:

```text
http://127.0.0.1:8791/providers/google-health/oauth/callback
```

Do not substitute `localhost`, another port, HTTPS, a trailing slash, or a public MCP hostname. The authorization request and code exchange must use that exact URI. Start the local receiver before opening consent.

Google's current codelab/setup wizard may temporarily instruct developers to use `https://www.google.com` as the authorized redirect URI to display/copy an authorization code. That is a codelab convenience, not Heavenly's production callback. Use it only while following that temporary manual codelab, never as the continuous Heavenly callback, and never paste the returned URL/code into chat. Replace/register the exact loopback URI above for Heavenly.

## 4. Import and store the downloaded client JSON

Download the Web OAuth client JSON from Google Cloud. It is a secret-bearing file.

### Native macOS

1. Leave the file in Downloads only long enough for Heavenly's interactive import.
2. Select it through the setup file picker/local prompt; verify the metadata preview says Web client and shows the expected project/product without printing the secret.
3. Heavenly imports client credentials and tokens into macOS Keychain, then asks approval before deleting the downloaded copy. Do not move it into the repository or paste its contents.
4. If manual cleanup is chosen, ensure the file is owner-readable only while present and remove it after successful Keychain import.

Non-secret scope, expiry, identity alias, and checkpoint metadata may live in Heavenly's state directory; refresh/access tokens do not.

### Docker/server

Use this exact owner-only host path:

```text
~/.config/heavenly/secrets/google-health/client.json
```

Set mode `0600`, keep the parent directory owner-only, and mount the file read-only at:

```text
/run/secrets/google-health-client.json
```

The container must read that runtime secret and must not copy it into an image layer, named volume, logs, `.env`, Compose YAML, backup export, or database. Store user refresh tokens in encrypted server-side token storage with the encryption key supplied separately by the deployment secret manager.

## 5. Authorize for offline access

The authorization request uses Google's standard authorization-code flow with:

```text
response_type=code
access_type=offline
redirect_uri=http://127.0.0.1:8791/providers/google-health/oauth/callback
```

Use `prompt=consent` only for the **initial** connection, recovery when a refresh token is missing/revoked, or explicit scope expansion. Do not send it on routine sign-in or refresh; repeated forced consent creates unnecessary grants/tokens and user friction.

1. Start the loopback listener and generate high-entropy, one-time `state` (and PKCE if supported by the client implementation).
2. Open Google's authorization page with the selected scopes, `access_type=offline`, and initial `prompt=consent`.
3. Confirm the account is the one linked to Fitbit/Google Health and review the exact scope list.
4. The callback validates `state`, rejects replay/expiry, and exchanges the one-time code server-side.
5. Store access token, refresh token, granted scopes, and expiry atomically. Never log the callback URL/code or token response.
6. Refresh without `prompt=consent`; serialize refresh per connection and retain the prior refresh token if Google returns no replacement.

### Testing versus Production

- **Testing:** only configured test users can authorize. Refresh tokens expire after **7 days**, so a reconnect during development is expected.
- **In Production:** use for continuous access; refresh tokens generally persist until revoked, unused for a prolonged period, or invalidated by account/client changes. Publishing does not grant API verification. Track verification separately and do not widen availability until it is complete.

## 6. Verify identity before importing health records

First call an identity endpoint under `users/me`/the Google Health user context (for example the API's user identity resource), then a narrow data read. Do not request People API/profile scopes merely to obtain an email.

Verification sequence:

1. Call the v4 user identity endpoint and persist only its stable, pseudonymized subject/connection binding.
2. Confirm API base/version is `https://health.googleapis.com/v4/`.
3. Query one selected data type in a narrow recent interval, such as `steps`, `sleep`, or `heart-rate`, and page results.
4. Validate native resource name/ID, data source, timestamps/offset, units, and granted scope before normalization.

Expected result:

```text
Google Health connection verified
API: v4
Identity: stable users/me binding recorded (email and tokens not stored in events)
Scopes: 3 read-only scopes granted
Read: API succeeded; records imported or a valid empty window reported
```

If identity succeeds but reads are empty, confirm that the same Google account is linked to Fitbit/Google Health and has synced data. Do not treat an unrelated Google login as a complete health connection.

## 7. Backfill, retries, deduplication, and delayed behavior

- Backfill bounded data-type/time windows and follow `nextPageToken`; commit the checkpoint only after durable storage.
- Retry transport errors, `429`, and `5xx` with bounded exponential backoff and jitter, honoring `Retry-After`. Refresh once on an expired access token. Do not loop on `invalid_grant`, `401`, or scope/verification `403`.
- Store immutable raw resources under `google-health:{data_type}:{native_resource_id}`. If no stable ID exists, hash canonical provider, connection pseudonym, type, original timestamps, value, and unit. Derive normalized metric IDs from the raw ID.
- Upsert on stable IDs. Overlap polling windows to catch delayed/revised sync without duplicates. Preserve data source/provenance.
- Fitbit/device → Google cloud sync and computed daily metrics may lag. Record `data_through`; missing is unknown, not zero. If the device or Heavenly is offline, resume from the durable checkpoint with overlap after reconnect.
- Add Google Health notifications/webhooks only after a provider-aware receiver exists, the initial backfill works, and external subscription creation has been previewed and approved. Notifications trigger authoritative refetch; they are not a reason to expose a local callback publicly.

## 8. Troubleshooting

| Symptom | Action |
| --- | --- |
| `redirect_uri_mismatch` | Register and send exactly `http://127.0.0.1:8791/providers/google-health/oauth/callback`; check listener port and trailing slash |
| Callback cannot connect | Start Heavenly first; verify port 8791 is free and bound only to loopback |
| `access_denied` / app blocked | Confirm user is in Test users, consent configuration is complete, scopes are allowed, and verification status supports the audience |
| Refresh fails after 7 days | Expected in Testing; reconnect. Move to In Production only when ready, without claiming verification is complete |
| No refresh token | Re-run initial/recovery consent once with `access_type=offline&prompt=consent`; do not force consent routinely |
| `403` on data | Check exact scope and project/API/verification status; do not substitute old Google Fit scopes |
| Identity works, no health rows | Confirm the same account is linked to Fitbit/Google Health, device/app has synced, type exists, filter/timezone is correct, and pages are followed |
| Duplicate rows | Use native resource identity and overlap-safe upsert, not random IDs |
| Client JSON found in Git/logs | Revoke/rotate the OAuth client secret, purge unsafe copies through the approved incident process, and reimport securely |

## 9. Revoke and delete

1. Pause polling, refresh, notification, and backfill jobs.
2. Revoke the grant from the user's Google Account third-party connections/security controls and, where supported, call Google's token revocation endpoint through Heavenly without exposing the token in a command line.
3. Delete Keychain/encrypted token entries, pending state, identity mapping, and notification subscriptions. Delete the host client JSON only if the OAuth client is no longer needed by any approved connection; otherwise retain it at `0600`.
4. Optionally delete/rotate the OAuth client in Google Cloud only after preview and explicit approval; this affects every user of that client.
5. Offer separate deletion of normalized data, restricted immutable raw data, analyses, checkpoints, and backups under the published retention policy. Keep only a non-sensitive deletion audit marker.
6. Verify refresh/read fail and jobs cannot recreate the connection. Reconnection requires new consent, and deleted history is not reimported unless the user requests a new backfill.

## Official references

- <https://developers.google.com/health/about>
- <https://developers.google.com/health/setup>
- <https://developers.google.com/health/scopes>
- <https://developers.google.com/health/endpoints>
- <https://developers.google.com/health/app-verification>
