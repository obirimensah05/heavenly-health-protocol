# Connect Google Health API v4 securely

> **Current status:** implemented native connector. Heavenly imports a protected
> Google Web OAuth client into the operating-system credential vault, runs an
> exact one-shot loopback callback with state and PKCE, refreshes/revokes tokens,
> and performs bounded paginated Google Health API v4 synchronization into the
> existing raw-provenance and allowlisted normalized tables.

Use this route for supported Fitbit/Google Health data linked to the user's Google account. This is the **Google Health API v4** at `health.googleapis.com/v4`, not Google Fit, the legacy Fitbit Web API, Android Health Connect, or Google Cloud Healthcare API. The Google Health API is the new consumer API; Health Connect remains device-local, and Cloud Healthcare API is for clinical FHIR/DICOM workloads.

## 1. Prerequisites

- A Google account whose health source is linked to Fitbit/Google Health and has synced data. OAuth can succeed while returning no data if the selected account is not linked or has no records.
- A Google Cloud project whose product/application name is exactly **Heavenly**.
- Google Health API enabled and a **Web application / Web server** OAuth client.
- OAuth consent/branding, public privacy policy and terms appropriate for restricted health scopes.
- For Testing, the authorizing account explicitly listed as a test user.
- A configured Heavenly Supabase route and explicit metric allowlist.
- Port 8791 available temporarily for the one-shot local callback.

All Google Health API scopes are Restricted. Moving the consent screen to In Production avoids Testing's short refresh-token lifetime, but **publishing status and Google verification are separate**. Do not claim verified/approved access merely because the app says In Production. Complete the current Google verification, privacy, security, and launch requirements before serving users beyond the allowed test/limited audience.

Heavenly does not create or modify the Google Cloud project. The operator creates
the project/client in Google, downloads the Web OAuth JSON, and imports it with a
local command. Local provider OAuth needs no tunnel and the AI/MCP client never
receives the Google client secret or tokens.

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

The connector is native. Put the downloaded file outside the repository, make it
owner-only, then import it by absolute path:

```bash
chmod 600 "/absolute/path/to/google-oauth-client.json"
heavenly provider google-health import-client "/absolute/path/to/google-oauth-client.json"
```

The command validates a Web client, Google's official OAuth origins, and the
exact callback before copying the client into the operating-system credential
vault. It prints only redacted status. Heavenly does not automatically delete
the downloaded file; remove it after a successful import if it is no longer
needed.

Non-secret scope, expiry, identity alias, and checkpoint metadata may live in Heavenly's state directory; refresh/access tokens do not.

The checked-in Docker MCP profile intentionally does not receive provider
credentials and cannot run this connector. Use native Heavenly for provider
OAuth and synchronization.

## 5. Authorize for offline access

The authorization request uses Google's standard authorization-code flow with:

```text
response_type=code
access_type=offline
redirect_uri=http://127.0.0.1:8791/providers/google-health/oauth/callback
```

Use `prompt=consent` only for the **initial** connection, recovery when a refresh token is missing/revoked, or explicit scope expansion. Do not send it on routine sign-in or refresh; repeated forced consent creates unnecessary grants/tokens and user friction.

Stop the local MCP while the one-shot callback uses port 8791, then connect:

```bash
heavenly runtime stop --runtime native
heavenly provider google-health connect
```

Heavenly selects scopes from `HEAVENLY_ALLOWED_METRICS`, opens Google's consent
page, validates state and PKCE, exchanges the code without logging it, validates
the Google Health identity, and stores the token set in the credential vault.
The initial flow requests offline access and consent; refreshes retain the prior
refresh token when Google does not rotate it.

### Testing versus Production

- **Testing:** only configured test users can authorize. Refresh tokens expire after **7 days**, so a reconnect during development is expected.
- **In Production:** use for continuous access; refresh tokens generally persist until revoked, unused for a prolonged period, or invalidated by account/client changes. Publishing does not grant API verification. Track verification separately and do not widen availability until it is complete.

## 6. Verify identity before importing health records

First call an identity endpoint under `users/me`/the Google Health user context (for example the API's user identity resource), then a narrow data read. Do not request People API/profile scopes merely to obtain an email.

Run the first bounded synchronization, inspect redacted status, and restart MCP:

```bash
heavenly provider google-health sync --limit 1000
heavenly provider status
heavenly runtime start --runtime native
heavenly runtime status
```

The connector calls `/v4/users/me/identity`, stores only a SHA-256 identity
binding in local state, reads mapped data types for the most recent bounded
window, follows pagination, saves each raw record before normalized events, and
commits checkpoints only after storage succeeds. The MCP
`health_connector_status` and `health_sync_source` tools can then report or
trigger the same provider runtime without exposing credentials or raw payloads.

Expected command result:

```text
Google Health connection verified
API: v4
Identity: stable users/me binding recorded (email and tokens not stored in events)
Scopes: only the read-only scopes required by the configured metric allowlist
Read: API succeeded; records imported or a valid empty window reported
```

If identity succeeds but reads are empty, confirm that the same Google account is linked to Fitbit/Google Health and has synced data. Do not treat an unrelated Google login as a complete health connection.

## 7. Backfill, retries, deduplication, and delayed behavior

- Backfill bounded data-type/time windows and follow `nextPageToken`; commit the checkpoint only after durable storage.
- Retry transport errors, `429`, and `5xx` with bounded exponential backoff and jitter, honoring `Retry-After`. Refresh once on an expired access token. Do not loop on `invalid_grant`, `401`, or scope/verification `403`.
- Store immutable raw resources under `google-health:{data_type}:{native_resource_id}`. If no stable ID exists, hash canonical provider, connection pseudonym, type, original timestamps, value, and unit. Derive normalized metric IDs from the raw ID.
- Upsert on stable IDs. Overlap polling windows to catch delayed/revised sync without duplicates. Preserve data source/provenance.
- Fitbit/device → Google cloud sync and computed daily metrics may lag. Record `data_through`; missing is unknown, not zero. If the device or Heavenly is offline, resume from the durable checkpoint with overlap after reconnect.
- Notifications/webhooks are not implemented. The connector uses explicit
  bounded pull synchronization with a one-hour checkpoint overlap.

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

1. Stop active synchronization and run `heavenly provider google-health
   disconnect --yes`. Add `--remove-client` only when the reusable OAuth client
   should also leave the credential vault.
2. Heavenly calls Google's revocation endpoint without putting the token on the
   command line, deletes the local token and connector state, and optionally
   removes the client.
3. Delete the downloaded JSON if retained. Optionally delete/rotate the OAuth
   client in Google Cloud; this affects every user of that client.
4. Provider disconnect intentionally does not delete already ingested raw or
   normalized records. Apply the owner's separate data-retention/deletion policy
   when historical rows and backups must also be removed.

## Official references

- <https://developers.google.com/health/about>
- <https://developers.google.com/health/setup>
- <https://developers.google.com/health/scopes>
- <https://developers.google.com/health/endpoints>
- <https://developers.google.com/health/app-verification>
