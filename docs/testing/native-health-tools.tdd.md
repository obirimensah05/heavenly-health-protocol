# Native health tools — TDD evidence

## User journeys

- As an owner, I can run native Heavenly as a persistent macOS user service.
- As an approved MCP client, I can discover real health tools and issue bounded,
  allowlisted reads without receiving raw provider payloads or credentials.
- As an owner, I can normalize configured Health Auto Export deliveries
  idempotently while preserving restricted raw provenance.
- As an owner, I can search explicitly configured personal context through bounded
  previews without coupling the protocol to a named private second brain.
- As an owner, I must approve every proposed health mutation in a separate local
  CLI channel before MCP can execute that exact mutation once.

## RED → GREEN evidence

| Behavior | RED evidence | GREEN evidence |
| --- | --- | --- |
| Protected runtime file loading | `pytest tests/test_secret_loader.py` failed collection because `heavenly_health.secret_loader` did not exist. | Seven loader/security tests passed after implementation. |
| Supabase reads, normalization, context, and approvals | `pytest tests/test_health_storage.py tests/test_approvals.py` failed collection because both modules did not exist. | Twelve storage/approval tests passed; focused Ruff passed. |
| MCP tool surface and CLI-only approval | Three focused tests failed because `create_mcp_server` had no storage arguments and CLI had no approval store. | The same three tests passed with ten tools and no MCP approval action. |
| Persistent native service | `pytest tests/test_launchd_runtime.py` failed collection because the backend did not exist. | Three LaunchAgent install/start/status/stop/readiness tests passed. |
| Freshness and storage SSRF boundary | Two focused tests failed because connector status had no clock/data query and private IP origins were accepted. | Both tests passed after the bounded status query and public-origin validation. |

Checkpoint commits preserve each failing test separately from its minimal passing
implementation. No credential, owner endpoint, private relation name, personal
record, or raw payload is present in these fixtures or this report.

## Test guarantees

| Guarantee | Test file | Type |
| --- | --- | --- |
| Secret values load only from protected files and never leak through errors. | `tests/test_secret_loader.py` | unit/security |
| Health queries enforce metric, date, source, and result bounds. | `tests/test_health_storage.py` | unit/integration |
| Provider normalization is deterministic, allowlisted, and removes device names. | `tests/test_health_storage.py` | unit |
| Raw provenance and normalized rows use fixed idempotent upserts. | `tests/test_health_storage.py` | integration |
| Context search is limited to one configured relation and bounded previews. | `tests/test_health_storage.py` | integration |
| Approval records are signed, owner-only, expiring, reject tampering, and execute once. | `tests/test_approvals.py` | unit/security |
| MCP exposes ten implemented tools but no self-approval action. | `tests/test_mcp_server.py` | integration |
| A persistent LaunchAgent owns only the expected label and fails closed on readiness. | `tests/test_launchd_runtime.py` | unit/integration |

## Private acceptance boundary

The private deployment was exercised through both loopback and its authenticated
Cloudflare route using a real FastMCP client. Verification output was limited to
tool names, metric names, booleans, and counts; no measurements, timestamps,
context text, credentials, tokens, cookies, or raw payloads were printed or
committed. Standards-based hosted-client OAuth remains a separate deployment
acceptance gate and must not be inferred from an ordinary Access session.
