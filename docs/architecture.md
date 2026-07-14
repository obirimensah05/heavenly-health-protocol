# Architecture

> **Status:** partially implemented data architecture. The repository includes
> schema migrations, native/Docker MCP runtimes, a Supabase adapter, Health Auto
> Export normalization, native Google Health and Garmin provider adapters,
> bounded health/context tools, owner-approved mutations, Cloudflare Managed
> OAuth, and the generic agent sandbox. Additional provider adapters,
> scheduling, summaries, and delivery remain target architecture.

```text
Selected device/provider
  → source adapter
  → normalized Heavenly health format
  → chosen storage adapter
  → local or remote MCP adapter
  → any compatible LLM/agent
```

## Source adapters

Source adapters own provider OAuth, polling, refresh, revocation, and source
capability declarations. The implemented Google Health and Garmin adapters use
an exact loopback callback, PKCE/state, bounded overlap-safe pull windows,
provider-native identities, and the operating-system credential vault. Garmin
endpoint and resource paths are partner-issued configuration because its full
reference is available only after program approval.

## Normalized data

The protocol separates raw source records, normalized analysis events, and generated summaries. Sources provide timestamps, provenance, and `data_through` freshness metadata.

For Supabase, the default tables are:

```text
heavenly_health_raw_events
→ restricted provider payload/source-record audit store
→ not the standard LLM/agent read surface

heavenly_health_events
→ normalized metric rows exposed only through bounded allowlisted MCP tools
```

A raw provider record keeps its immutable provider identity. Each normalized metric derived from it receives a deterministic suffix such as `:metric:hrv_rmssd`, so a single sleep/cycle/activity record can safely produce many analysis rows without duplicate collisions. Synthetic rows are explicit (`is_synthetic=true`, `ingest_mode=synthetic_test`); live and backfilled records default to `is_synthetic=false`.

The migrations enable RLS but intentionally define no public grants or policies,
so database access is default-deny until the owner applies reviewed grants and
configures Heavenly's service-side adapter.

## Storage adapters

- Obsidian/local: raw exports, daily Markdown, local SQLite import state.
- Supabase: implemented fixed-relation REST adapter with bounded reads,
  raw provider provenance, Health Auto Export/Google Health/Garmin normalization,
  context search, and approved writes.
- iCloud/Google Drive: user-controlled file destination plus local import state.

## MCP access

Local clients use loopback Streamable HTTP MCP with no tunnel. Remote clients use
an explicit named Cloudflare Tunnel and Access Managed OAuth. Cloudflare injects
a signed assertion that the Heavenly origin verifies by issuer, audience,
signature, time claims, token type, and exact identity. Raw local ports are never
public by default.

## Scheduling

Sync and analysis are separate. A delivery at T uses source-specific freshness preparation, generally T−30 collect, T−15 validate/retry, T−10 generate, T deliver.
