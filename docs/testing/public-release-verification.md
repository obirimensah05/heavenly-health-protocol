# Public release verification

This report records the checks run against the fresh-history public export. It
contains no deployment identifiers, private endpoints, credentials, or health
data.

## Release surface

- Export source: explicit Git-tracked manifest only.
- Public history: initialized independently; private Git history is absent.
- Owner/deployment marker scan: passed.
- Local environment, state, handover, log, credential, and agent-auth files:
  absent and rejected by the release guard.

## Quality gates

| Gate | Result |
| --- | --- |
| Tests | 244 passed |
| Coverage | 81.16%, above the 80% gate |
| Ruff | Passed |
| Pyright | Zero errors and warnings |
| Python compilation | Passed |
| Source/wheel build | Passed |
| Compose validation | Passed |
| Locked dependency audit | No known vulnerabilities |
| Bandit | Zero medium/high findings |
| Secret heuristic scan | Seventeen reviewed test-fixture candidates; no real credentials |
| Public release guard | 88 tracked files validated with owner/deployment markers forbidden |

The secret candidates are deliberate fake values used to prove that provider,
storage, OAuth, and sandbox credentials are redacted or isolated. No candidate
is a live token, key, owner identity, or deployment value.

## Real container acceptance

The public image was built from the exported tree and tested on an alternate
loopback port so an existing native service was not disturbed:

- Native MCP and the built Docker image both initialized successfully on
  isolated loopback ports.
- The unconfigured image exposed only `protocol_status`.
- The tool call completed without an MCP error.
- Runtime user was non-root.
- Root filesystem was read-only.
- Privileged mode was false.
- All Linux capabilities were dropped.
- `no-new-privileges` was active.
- Docker Scout found zero critical and zero high vulnerabilities.

## Known product boundary

The release is ready as a technical protocol/runtime. Supabase, bounded Health
Auto Export normalization, Google Health API v4 OAuth/pull synchronization, and
the Garmin partner-configurable OAuth/pull adapter are implemented. Google live
acceptance is performed during owner onboarding with the owner's OAuth client
and data. Garmin live acceptance additionally requires Developer Program
approval and partner-issued endpoint details. Health Connect remains a
documented contract and is not represented as a shipped adapter.

## 2026-07-14 release update

Shipped since the record above: the interactive onboarding wizard
(`heavenly setup`) and native WHOOP and Oura connectors (owner-only env-file
client import, browser OAuth with a pasted redirect URL, token refresh, bounded
pull synchronization, per-resource scope skips). Both connectors passed live
owner acceptance: authorization, token refresh, and bounded synchronization
completed against real provider accounts; WHOOP data reads additionally
require an active membership on the authorizing account.

Gates rerun for this update: tests (269 passed), Ruff, Pyright, Python
compilation, lock check, source/wheel build, Compose validation, and the
public release guard with owner/deployment markers forbidden. The container
acceptance results above are from the prior record and were not rerun.
