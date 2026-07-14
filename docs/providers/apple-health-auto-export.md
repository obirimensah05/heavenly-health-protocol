# Connect Apple Health with Health Auto Export

> **Current status:** manual Health Auto Export walkthrough and connector
> specification. The checked-in Heavenly CLI does not yet configure the iPhone,
> provision a receiver, or synchronize Apple Health data itself.

Apple Health has no normal cloud OAuth flow. HealthKit data is local to the iPhone and permission-gated per data type. Health Auto Export reads only the categories the user grants in iOS and sends/files only the categories selected in its export automation.

Use either:

- **REST JSON:** iPhone posts to a protected, generic Heavenly Apple Health ingestion endpoint; best for a cloud destination or multiple agents.
- **iCloud JSON:** the app writes JSON into iCloud Drive; best for a local Mac importer. A cloud-only agent cannot directly read private iCloud Drive.

## 1. Prerequisites

- An iPhone with Apple Health data and Health Auto Export installed.
- Health Auto Export's current feature/subscription level needed for the selected automation.
- A destination chosen before permissions are granted:
  - deployed HTTPS Heavenly receiver for REST; or
  - a dedicated iCloud Drive JSON folder visible to the importing Mac.
- Background App Refresh enabled for Health Auto Export where desired; understand iOS—not the app—decides exact background timing.
- For REST, a high-entropy bearer secret already installed in the receiver's secret store.

Heavenly may detect Supabase CLI, `cloudflared`, compatible MCP clients, and supported APIs. Detection is read-only. It previews receiver/schema/tunnel/Access changes and asks for approval. A temporary Quick Tunnel is not acceptable for private health data. Never publish or change an endpoint, database, MCP exposure, or cloud resource without the user's approval.

## 2. Select privacy-minimized HealthKit permissions

There are no OAuth scopes. In **iPhone Settings/Health → Data Access & Devices → Health Auto Export** (wording can vary by iOS version), grant **Read** only for data selected for Heavenly. HealthKit may show permission prompts inside the app on first export.

Recommended starting set:

| Purpose | Health permission/export categories | Default? |
| --- | --- | --- |
| Activity | Steps, active energy, exercise time, stand time, walking/running distance | Yes, as needed |
| Cardiovascular trends | Heart rate, resting heart rate, heart-rate variability | Yes, as needed |
| Recovery | Sleep analysis, respiratory rate | Yes, as needed |
| Training | Workouts and workout summary (without routes) | Yes, as needed |
| Additional wellness | Blood oxygen, wrist temperature, body mass | Opt in only when the analysis uses them |

Keep clinical records, medications, reproductive/cycle data, ECG, AFib/irregular-rhythm data, symptoms, mindfulness/state-of-mind/mental-health data, blood glucose, nutrition, and workout routes/location off by default. Selecting a metric in HealthKit is not enough: it must also be selected in the Health Auto Export automation. Do not grant Write permissions for this read/export workflow.

## 3A. Configure REST JSON

1. Deploy and test a generic Heavenly Apple Health receiver before configuring the phone. It should conceptually be:

```text
https://<approved-heavenly-origin>/providers/apple-health/ingest
```

Use the exact endpoint value shown by local onboarding; this guide deliberately contains no user-specific URL. The receiver must accept HTTPS POST, enforce a body-size limit, authenticate before parsing/storing, acknowledge durable intake, and keep raw payloads restricted.
2. In Health Auto Export, open **Automated Exports**, create **REST API**, and name it without personal identifiers.
3. Enter the approved endpoint, choose **JSON**, and select only the permitted metrics/workouts.
4. Add this header locally:

```text
Authorization: Bearer <generated-ingestion-secret>
```

Do not send the secret in a query parameter, screenshot, chat, note, or exported JSON. Use `Content-Type: application/json` (the app sets it for JSON exports).
5. Start with a small date range and conservative cadence. Large payloads can exceed iOS background time/memory or receiver limits; prefer bounded incremental exports over repeated full history.
6. Configure notifications/activity logs so failures are visible.

### Bearer secret storage

- **Native receiver:** store the expected bearer value in macOS Keychain. The phone holds it only in Health Auto Export's protected automation configuration.
- **Docker/server:** store it in the deployment secret manager or owner-only host file (`0600`) mounted read-only under `/run/secrets/`; never in Compose, `.env`, image layers, repository, database rows, or logs. Compare secrets in constant time and support rotation with a brief two-key overlap.

Do not expose the generic ingestion endpoint as an MCP tool. Agents read only normalized, approved rows.

## 3B. Configure iCloud JSON

1. In Health Auto Export create an iCloud Drive automation/export, choose **JSON**, and select the same privacy-minimized metrics.
2. Select a dedicated folder conceptually like:

```text
iCloud Drive/Health Auto Export/Heavenly/
```

Use the actual folder selected in onboarding; do not put secrets in filenames or JSON.
3. Configure the local Mac importer to watch/read that folder. Keep importer state (file hash, size, modification/import time) in a local SQLite/state database outside iCloud.
4. Do not give an LLM direct access to the entire iCloud folder. Import restricted raw JSON, normalize permitted metrics, then expose a read-only analysis view.

No bearer secret is required for a local private iCloud file path. iCloud account security and Mac filesystem permissions protect transport/storage; FileVault should protect the Mac at rest. If the Mac is offline, files can wait in iCloud and import later.

## 4. Test the export

### REST test

1. Unlock the iPhone and open Health Auto Export.
2. Run the automation manually for a narrow period containing a known, non-sensitive selected metric.
3. Inspect the app's activity log for the HTTP result; do not copy the payload into chat.
4. In Heavenly, inspect a redacted receipt/normalization summary.

Expected result:

```text
Apple Health export accepted
Authentication: valid (secret redacted)
Raw batch: stored once with immutable batch/source identities
Normalized: selected metric types only
Window: source timestamps recorded; no health values printed in setup output
```

### iCloud test

1. Run a narrow manual JSON export while the phone is unlocked.
2. Wait for the file to appear/download on the Mac.
3. Run/observe the importer and confirm one restricted raw file record plus normalized approved events.
4. Re-run the same import. Expected: `0 new` (or updates only), not duplicate events.

Verify that excluded categories and routes are absent. A successful HTTP status or file appearance alone is not enough; verify authentication, schema, identities, timestamps/units, normalization allowlist, and deduplication.

## 5. Immutable raw data, retries, and deduplication

- Preserve accepted payload/file content as an immutable restricted raw batch; do not let an agent rewrite source history.
- For samples, prefer the HealthKit sample UUID when exported. Otherwise derive a deterministic ID from provider, device/account pseudonym, canonical metric, original start/end/sample time, normalized value, and unit.
- Give each normalized metric a derived ID such as `{raw_source_record_id}:metric:{metric_type}`. Upsert on that ID.
- Deduplicate REST retries by payload/batch hash plus stable sample identities. Deduplicate iCloud files by content hash, not filename alone.
- Acknowledge REST success only after durable raw intake. On timeout/`5xx`, the phone may retry or the user may rerun; stable upserts make that safe. Reject `401/403` without parsing/storing and alert for secret mismatch. Use `413` to signal a payload that must be split.
- Never generate fresh random event IDs for repeated samples. Keep source timestamps separate from import time.

## 6. Locked phone, delayed sync, and offline behavior

Apple does not allow apps to read protected Health data while the iPhone is locked. Health Auto Export automations run when the phone is unlocked and iOS grants background execution; Low Power Mode, disabled Background App Refresh, inactivity, contention, memory, and payload size can delay or terminate work. Schedules are targets, not guarantees.

- **Phone offline:** REST waits until connectivity/next run; iCloud upload waits. Do not mark missing metrics as zero.
- **Phone locked:** unlock and manually run for urgent freshness. The automation can catch up on a bounded overlap window later.
- **Receiver offline:** Health Auto Export may report timeout/failure. Rerun after recovery; immutable raw storage and idempotent identities prevent duplication.
- **Mac offline (iCloud route):** iCloud can retain/sync the file later. The importer resumes from file hashes/checkpoints after the file is locally available.
- **Delayed Apple Watch sync:** Health can receive Watch samples after an export. Use overlap windows so later runs collect them.
- Include `data_through` and last successful phone export/import times in every analysis; label stale data clearly.

## 7. Troubleshooting

| Symptom | Action |
| --- | --- |
| Automation does not run | Unlock iPhone, open app, enable Background App Refresh, disable Low Power Mode temporarily, reduce payload/window, and run manually |
| Empty export/header only | Confirm Health contains the metric, Read permission is enabled, metric is selected in the automation, and date range/timezone is correct |
| REST `401/403` | Verify `Authorization` header spelling and bearer value locally; rotate if exposed; never log it |
| REST timeout/`5xx` | Check HTTPS receiver health and body limits; shorten range, batch payload, then rerun safely |
| REST `413` | Reduce date range/selected metrics or configure bounded batching; do not raise limits without review |
| File not on Mac | Confirm same iCloud account, iCloud Drive enabled, file downloaded, and Mac online; wait for sync |
| Duplicate events | Use HealthKit UUID/deterministic sample ID and file/payload hash; do not key only on run time or filename |
| Missing late Watch data | Allow Watch → iPhone Health sync, then rerun an overlapping window |
| Excluded data appears | Pause automation, tighten both HealthKit permission and export selections, quarantine/delete affected rows under policy, then retest |

## 8. Revoke, rotate, and delete

1. Disable/delete the Health Auto Export automation so no new exports occur.
2. Revoke HealthKit access in iPhone Health/Settings for Health Auto Export; remove only the data-type permissions intended for Heavenly or all access.
3. **REST:** rotate/delete the receiver bearer secret, remove it from Keychain/server secret storage and the phone automation, and verify the old secret receives `401/403`. Remove the endpoint/tunnel/route only after preview and approval if no other approved sender uses it.
4. **iCloud:** stop the importer, delete its watch configuration/checkpoint, and optionally delete exported files from iCloud Drive and Recently Deleted after reviewing scope.
5. Delete normalized events, restricted raw batches/files, analyses, queues, checkpoints, and backups according to the user's selection and published retention policy. Keep only a non-sensitive deletion audit marker.
6. Verify a repeated old REST request cannot authenticate, the importer no longer watches the folder, and no schedule recreates data. Re-enabling requires a new permission/export setup; deleted history is not reimported unless explicitly selected.

## Official references

- <https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/>
- <https://help.healthyapps.dev/en/health-auto-export/faq/>
- <https://support.apple.com/guide/iphone/share-health-and-fitness-data-iph5ede58c3d/ios>
