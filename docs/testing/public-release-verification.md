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
| Tests | 184 passed |
| Coverage | 81.06%, above the 80% gate |
| Ruff | Passed |
| Pyright | Zero errors and warnings |
| Python compilation | Passed |
| Source/wheel build | Passed |
| Compose validation | Passed |
| Locked dependency audit | No known vulnerabilities |
| Bandit | Zero medium/high findings; eight reviewed low subprocess notices |
| Secret heuristic scan | Twelve reviewed, unverified test-fixture matches; no real credentials |

The subprocess notices correspond to fixed-argument lifecycle/Docker commands
and the explicitly constrained agent launcher. Those boundaries use argument
lists rather than a shell and are covered by security-focused tests.

## Real container acceptance

The public image was built from the exported tree and tested on an alternate
loopback port so an existing native service was not disturbed:

- MCP initialized successfully three consecutive times.
- The unconfigured image exposed only `protocol_status`.
- The tool call completed without an MCP error.
- Runtime user was non-root.
- Root filesystem was read-only.
- Privileged mode was false.
- All Linux capabilities were dropped.
- `no-new-privileges` was active.
- Docker Scout found zero critical and zero high vulnerabilities across 125
  indexed packages.

## Known product boundary

The release is ready as a technical protocol/runtime. Supabase and bounded
Health Auto Export normalization are implemented. Provider-specific OAuth and
sync for WHOOP, Oura, Google Health/Fitbit, Garmin, and Health Connect remain
documented contracts and are not represented as shipped adapters.
