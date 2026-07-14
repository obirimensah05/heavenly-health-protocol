# Provider onboarding

These are paste-ready **manual operator walkthroughs and implementation
specifications**. The current `heavenly setup` command is preview-only and does
not yet automate provider connection, credential import, synchronization, or
external resource creation. Follow provider dashboard steps manually and treat
described Heavenly automation as the target onboarding contract until the
corresponding connector exists.

## Choose a route

| Source | Choose it when | Authorization model | Good default data | Important constraint |
| --- | --- | --- | --- | --- |
| [WHOOP](whoop.md) | WHOOP is the authoritative source for strain, recovery, sleep, and workouts | WHOOP OAuth 2.0 | Cycles, recovery, sleep, workouts | Register an exact callback and test with a development user; add webhooks only after a receiver exists |
| [Oura](oura.md) | Oura is the authoritative source for ring-derived daily scores, heart rate, sleep, and workouts | Oura OAuth 2.0 | Daily summaries, heart rate, workouts | Personal Access Tokens are not a supported onboarding route; refresh tokens rotate on every use |
| [Google Health API v4](google-health.md) | Fitbit or another supported Google Health source is linked to the user's Google account | Google OAuth 2.0 | Activity/fitness, sleep, health metrics | This is not Google Fit, legacy Fitbit Web API, Health Connect, or Cloud Healthcare API |
| [Apple Health via Health Auto Export](apple-health-auto-export.md) | Apple Health on an iPhone is the source of truth | iPhone HealthKit permissions; no Apple OAuth | Explicitly selected activity, sleep, vitals, and workouts | The phone cannot read protected Health data while locked; background delivery can be delayed |

Use native local storage for a local-only agent. Choose a protected HTTPS receiver and restricted database when several devices or a cloud agent need the data. Choose iCloud JSON only when a Mac can relay the files; a cloud agent cannot directly read a private iCloud Drive.

## Safety behavior shared by every walkthrough

Heavenly onboarding starts with source, destination, outputs, schedule/timezone, and selected metrics. It then requests only the permissions needed for those metrics. Clinical records, medications, reproductive/cycle data, ECG, irregular-rhythm data, mental-health/state-of-mind data, nutrition, and location/routes remain off unless the user explicitly opts in and the destination is approved for them.

Future onboarding is designed to auto-detect the Supabase CLI, `cloudflared`, a
compatible MCP client, and supported provider/cloud APIs. Detection must be
read-only. Before any future automation creates or changes an external resource,
it must show a preview and receive explicit user approval. This behavior is a
contract for implementation, not a claim that the current CLI performs it.

Secrets never belong in Git, Markdown, shell history, logs, chat, an LLM prompt/memory, Obsidian, Drive/iCloud exports, or normalized health rows. Examples in these guides use placeholders only; replace them locally, never in chat.

## Common ingestion contract

1. Preserve provider payloads as immutable, access-restricted raw records.
2. Normalize only approved metrics into the agent-readable event surface.
3. Prefer the provider record ID, namespaced by provider/resource. If none exists, derive a stable SHA-256 ID from canonical source fields.
4. Upsert on that stable identity. A retry updates delivery/checkpoint metadata; it does not create another health event.
5. Record source timestamp, ingestion timestamp, timezone/offset, source, and whether data is live, backfill, manual, or synthetic.
6. Treat “connected but no records in this window” differently from authentication or sync failure.

## Official references

Provider dashboards and policies can change. Recheck the official pages before a public launch:

- WHOOP: <https://developer.whoop.com/docs/developing/getting-started/> and <https://developer.whoop.com/docs/developing/oauth/>
- Oura: <https://cloud.ouraring.com/docs/authentication> and <https://cloud.ouraring.com/v2/docs>
- Google Health API: <https://developers.google.com/health/setup> and <https://developers.google.com/health/scopes>
- Health Auto Export: <https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/>
