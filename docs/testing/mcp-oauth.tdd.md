# MCP OAuth TDD and live evidence

## Implemented boundaries

Cloudflare Managed OAuth is the recommended remote mode. The origin requires one
signed `Cf-Access-Jwt-Assertion` for public/Cloudflare-forwarded traffic and
validates RS256 signature, team issuer, application audience, required temporal
and subject claims, application token type, and exact owner identity. Loopback
traffic remains available. Managed OAuth and FastMCP's alternative OIDC proxy are
mutually exclusive.

Provisioning is repeatable through `heavenly access oauth plan/apply`. It
validates the exact self-hosted application/domain and exact-email policies,
preserves the fetched application, and reconciles only Managed OAuth settings.
An existing Access JWT can bootstrap the four protected origin settings only
after signature verification.

## RED to GREEN checkpoints

The implementation was built through committed compile-time and behavioral RED
tests followed by focused GREEN runs:

1. Missing Managed OAuth module and origin middleware.
2. Missing safe, idempotent Cloudflare provisioning client.
3. Missing nested `access oauth plan/apply` commands and Keychain token fallback.
4. Missing verified-assertion runtime bootstrap.
5. Docker startup regression when a standalone public host was mistaken for a
   partial Managed OAuth trust configuration.

Focused final run:

```text
uv run pytest tests/test_cloudflare_managed_oauth.py tests/test_mcp_server.py
28 passed
```

The Docker regression was reproduced by the real container traceback, locked into
`test_managed_oauth_settings_are_all_or_nothing_and_reject_unsafe_origins`, fixed,
and rechecked against `docker compose up --wait`.

## Maintainer acceptance evidence

A self-hosted test application was updated to Managed OAuth only after the
native origin verifier and protected settings were active.

Observed without exposing identities, tokens, records, or credentials:

```text
unauthenticated POST /mcp: 401
Location header: absent
WWW-Authenticate resource metadata: present
protected-resource metadata: 200 JSON
authorization-server metadata: 200 JSON

fresh FastMCP OAuth client:
browser authorization: successful
tools/list: 10 tools
protocol_status: successful

public Docker MCP rebuild:
container health: healthy
tools/list: 1 status-only tool (no private storage injected)
protocol_status: successful
container user: non-root
read-only root: true
privileged: false
capabilities dropped: ALL

restored private native MCP:
tools/list: 10 tools
```

Cloudflare resolves the opaque client access token at the edge and injects the
signed assertion; the successful remote tool call therefore also proves the
origin verifier accepted the real Cloudflare assertion.

One known upstream warning remains in ASGI tests: Starlette recommends its future
`httpx2` test client. The warning does not suppress or alter test results.
