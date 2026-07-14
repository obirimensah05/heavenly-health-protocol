# Connect Garmin Connect Health API securely

> **Current status:** implemented partner-configurable native connector. The
> OAuth lifecycle, bounded pull client, provenance pipeline, normalization,
> status, and disconnect commands are shipped and tested. A live connection
> requires Garmin Connect Developer Program approval and the endpoint/resource
> details Garmin makes available to the approved project. The public Garmin
> pages do not expose enough detail to invent those values safely.

Garmin Connect APIs are cloud-to-cloud. This connector is for the approved
Garmin Connect Health API, not direct Bluetooth access, Connect IQ, a consumer
password, or a scraped Garmin Connect session.

## 1. Obtain Garmin program access

Apply to the Garmin Connect Developer Program for the Health API. After approval,
use Garmin's evaluation environment and project tools to configure the consent
application, exact callback, data feeds, and pull architecture. Commercial use
may require a license.

Register this callback exactly:

```text
http://127.0.0.1:8791/providers/garmin/oauth/callback
```

Do not substitute `localhost`, another port, a public MCP hostname, a trailing
slash, or an unapproved redirect.

## 2. Configure private storage and metrics

Apply the checked-in Supabase migrations and configure an explicit metric
allowlist in the owner-only Heavenly runtime file. Garmin resources are selected
only when they can produce at least one allowed metric.

| Imported resource key | Allowlisted normalized metrics |
| --- | --- |
| `dailies` | steps, resting heart rate, active energy, stress, Body Battery, oxygen saturation, respiratory rate |
| `sleeps` | sleep analysis |
| `body_compositions` | body mass |
| `epochs` | steps, heart rate, active energy |
| `pulse_ox` | oxygen saturation |
| `respiration` | respiratory rate |

## 3. Build the owner-only partner configuration

Garmin's approved portal/reference is authoritative for every URL, scope, path,
parameter, and payload field. Put those issued values into one JSON file outside
the repository:

```json
{
  "client_id": "<partner-issued client id>",
  "client_secret": "<partner-issued client secret>",
  "authorization_url": "https://<garmin-issued-host>/<authorization-path>",
  "token_url": "https://<garmin-issued-host>/<token-path>",
  "api_base_url": "https://<garmin-issued-api-host>",
  "redirect_uri": "http://127.0.0.1:8791/providers/garmin/oauth/callback",
  "scopes": ["<partner-issued health scope>"],
  "identity_path": "/<partner-issued user-id path>",
  "resource_paths": {
    "dailies": "/<partner-issued dailies path>",
    "sleeps": "/<partner-issued sleeps path>",
    "body_compositions": "/<partner-issued body-composition path>",
    "epochs": "/<partner-issued epochs path>",
    "pulse_ox": "/<partner-issued pulse-ox path>",
    "respiration": "/<partner-issued respiration path>"
  },
  "revocation_url": "https://<garmin-issued-host>/<optional-revocation-path>"
}
```

Include only resource keys enabled for the project. Omit `revocation_url` when
Garmin does not issue one. The connector rejects non-HTTPS remote URLs,
credentials without the exact callback, unknown resource keys, absolute/external
resource URLs, path traversal, empty scopes, loose file permissions, symlinks,
and oversized JSON.

Import by absolute path:

```bash
chmod 600 "/absolute/path/to/garmin-partner.json"
heavenly provider garmin import-client "/absolute/path/to/garmin-partner.json"
```

The command copies the validated configuration into the operating-system
credential vault and prints only the resource count. Heavenly does not delete
the source JSON automatically.

## 4. Authorize, verify, and synchronize

Stop the native MCP while the one-shot loopback callback owns port 8791:

```bash
heavenly runtime stop --runtime native
heavenly provider garmin connect
heavenly provider garmin sync --limit 1000
heavenly provider status
heavenly runtime start --runtime native
heavenly runtime status
```

The connector uses OAuth 2.0 authorization code with state and PKCE, the exact
callback, the imported scopes, server-side token exchange/refresh, and a Garmin
identity call. Only a SHA-256 identity binding and non-secret checkpoints are
stored in owner-only local state; client and token values stay in the credential
vault.

The first sync reads no more than the most recent seven days, uses Garmin's
upload-time epoch parameters, follows partner pagination, and processes at most
the requested limit (hard maximum 10,000). Each raw record is upserted before
allowlisted normalized events. Later syncs overlap each checkpoint by one hour
to catch delayed/revised Garmin Connect uploads without duplicate identities.
Transient transport errors, `429`, and `5xx` responses are retried at most twice
after the initial attempt with a five-second delay cap; authorization and other
client errors fail immediately.

Garmin supports both push and ping/pull architectures, but this release
implements explicit ping/pull only. It does not create subscriptions, expose a
public Garmin callback, schedule background jobs, or guess endpoints that were
not supplied by the approved project.

## 5. Troubleshooting

| Symptom | Action |
| --- | --- |
| Import says configuration is incomplete | Compare every value and enabled resource path with the approved Garmin project/reference; keep the exact loopback callback |
| Callback port unavailable | Stop the native MCP, then rerun `provider garmin connect` |
| OAuth page or token exchange fails | Confirm the client, scopes, callback, evaluation/production environment, and partner-issued endpoints belong to the same Garmin project |
| Identity response is invalid | Configure the approved identity path and confirm it returns `userId`, `user_id`, or `id` as documented for the project |
| Resource is not configured | Add only the approved path for that supported resource key, reimport, and reconnect if the granted scope changed |
| Sync returns no rows | Confirm the user consented, the device synced to Garmin Connect, the feed is enabled, the allowlist maps to it, and the requested time window contains uploaded data |
| `401` or refresh failure | Reconnect; do not put access or refresh tokens into commands, files in Git, chat, or logs |

## 6. Disconnect

```bash
heavenly provider garmin disconnect --yes
# Also remove the reusable partner client from the vault:
heavenly provider garmin disconnect --yes --remove-client
```

When a revocation URL was imported, Heavenly calls it before deleting the local
token. Without one, it reports that remote revocation was unavailable and still
removes the local token/state. Revoke the user/project grant in Garmin's approved
operator tools when required. Provider disconnect intentionally leaves already
ingested records intact; historical deletion is a separate retention action.

## Official references

- <https://developer.garmin.com/gc-developer-program/>
- <https://developer.garmin.com/gc-developer-program/health-api/>
- <https://developer.garmin.com/gc-developer-program/program-faq/>
