# Runtime lifecycle TDD evidence

## Scope and journeys

This review covers only the local native and Docker Compose MCP lifecycle.

1. A native readiness timeout keeps ownership state until the owned process instance has changed and the listener is released.
2. A failure to write ownership state terminates only the still-matching launched process and confirms cleanup when possible.
3. Native ownership is recorded only when the captured process arguments plausibly include `heavenly-mcp`.
4. Stop recognizes a changed process identity (including PID reuse) as exit of the owned instance, while still requiring listener release.
5. Docker Compose startup has its own bounded timeout and pull/status guidance; Compose `ps` accepts JSON and NDJSON but reports malformed output as unavailable.
6. Compose project discovery works from a checkout working directory even when the CLI module is installed elsewhere.
7. Runtime overrides are validated by Typer and native logs retain one deterministic bounded backup.
8. A Compose container left in `created` state after a failed bind is recovered with `docker compose up -d` rather than treated as already running.
9. Native cold start has a dedicated bounded 12-second readiness window while shutdown confirmation remains bounded at 2 seconds.
10. Docker start waits for the Compose health check before reporting the MCP service as running.

## RED

Tests were added before implementation. The first focused run was a valid compile-time RED gate:

```text
UV_CACHE_DIR=/private/tmp/heavenly-uv-cache uv run pytest tests/test_runtime.py -q
ImportError: cannot import name 'DOCKER_START_TIMEOUT_SECONDS'
```

The new test module referenced the required separate Docker startup timeout and Compose discovery APIs before they existed, so collection stopped before any production implementation was edited. This is compile-time RED evidence for the newly specified lifecycle contract; it is not represented as an execution failure of every added scenario.

After live-state inspection exposed a previously failed Compose container in `created` state, a focused regression test produced this additional genuine RED:

```text
uv run pytest tests/test_runtime.py::test_docker_start_recovers_a_created_container_by_running_compose_up -q
AssertionError: assert 'created' == 'running'
```

The coordinator was then changed so only `running` and `restarting` are active/idempotent Docker states; `created` is recoverable and proceeds through `compose up -d`.

The native cold-start regression test also produced this genuine RED before the native implementation was changed:

```text
UV_CACHE_DIR=/private/tmp/heavenly-uv-cache uv run pytest tests/test_runtime.py::test_native_start_allows_cold_listener_more_than_twenty_readiness_checks -q
FAILED tests/test_runtime.py::test_native_start_allows_cold_listener_more_than_twenty_readiness_checks
RuntimeError: Native MCP process did not become ready on 127.0.0.1:8791 and cleanup could not be confirmed; ownership state was retained.
```

The listener intentionally became ready on check 26. The old startup loop reused the 20-attempt shutdown confirmation budget, so it incorrectly entered cleanup before observing readiness.

Live Docker verification then exposed a startup race: Compose returned after the
container port opened but before the MCP application accepted requests. The first
initialize request was disconnected. Tightening the existing Docker-start test to
require Compose health waiting produced this genuine RED:

```text
uv run --extra dev pytest tests/test_runtime.py::test_start_uses_selected_runtime_by_default -q
FAILED tests/test_runtime.py::test_start_uses_selected_runtime_by_default
AssertionError: expected docker compose up -d --wait --wait-timeout 300 heavenly-mcp
```

## GREEN

```text
UV_CACHE_DIR=/private/tmp/heavenly-uv-cache uv run pytest tests/test_runtime.py --cov=heavenly_health.runtime --cov-report=term-missing -q
35 passed
TOTAL 449 statements, 79 missed, 82% coverage
```

For the native cold-start fix, the exact regression and focused runtime module then passed:

```text
UV_CACHE_DIR=/private/tmp/heavenly-uv-cache uv run pytest tests/test_runtime.py::test_native_start_allows_cold_listener_more_than_twenty_readiness_checks -q
1 passed

UV_CACHE_DIR=/private/tmp/heavenly-uv-cache uv run pytest tests/test_runtime.py -q
36 passed
```

After Docker start was changed to use Compose's bounded health wait, the focused
runtime suite remained green:

```text
uv run --extra dev pytest tests/test_runtime.py -q
36 passed
```

The parent macOS environment permitted safe process inspection, so the process-identity test passed. The implementation still fails closed when process-instance identity cannot be captured on a supported platform.

## Guarantees

| Guarantee | Test |
| --- | --- |
| A native readiness timeout retains state if the owned instance/listener cannot be confirmed released. | `test_native_readiness_timeout_retains_state_until_owned_process_exits_and_listener_releases` |
| A write failure cleans up a matching child and reports whether cleanup was confirmed. | `test_native_write_failure_terminates_owned_child_and_confirms_cleanup` |
| A changed identity during stop, including PID reuse, is treated as the owned process exit once the listener is free. | `test_native_stop_treats_pid_reuse_as_owned_exit_once_listener_releases` |
| Captured process arguments must plausibly identify `heavenly-mcp`. | `test_native_start_rejects_identity_that_is_not_plausibly_heavenly` |
| Compose status parses NDJSON and rejects malformed output rather than reporting stopped. | `test_docker_status_parses_compose_ndjson_and_surfaces_malformed_output` |
| Compose startup has a longer dedicated timeout and does not claim Docker Desktop is down. | `test_docker_start_uses_separate_longer_timeout_and_pull_progress_guidance` |
| Installed-layout discovery uses the working checkout instead of CLI parent depth. | `test_compose_discovery_uses_working_project_when_cli_is_installed_elsewhere` |
| Invalid runtime overrides are Typer bad parameters. | `test_cli_invalid_runtime_override_is_a_bad_parameter` |
| Native logs rotate one 1 MiB backup deterministically. | `test_native_log_rotates_at_bounded_size` |
| A failed container left in `created` state is started again instead of reported as active. | `test_docker_start_recovers_a_created_container_by_running_compose_up` |
| Native startup waits through a cold listener requiring more than 20 readiness checks without terminating it, using a separate bounded readiness budget. | `test_native_start_allows_cold_listener_more_than_twenty_readiness_checks` |
| Docker start waits for the configured health check, bounded by the existing 300-second startup timeout, before reporting success. | `test_start_uses_selected_runtime_by_default` |
| Native launch forwards only `HEAVENLY_*` settings and a narrow process/network allowlist, preventing unrelated parent credentials from entering the MCP child. | `test_native_start_records_owned_pid_waits_for_listener_and_stop_only_kills_matching_identity` |

## Residual limitation

User space cannot atomically compare an arbitrary process identity and signal its PID. The runtime revalidates immediately before `SIGTERM`, then requires a changed identity and listener release before removing ownership state. If either confirmation is unavailable, the record remains and the error tells the operator to inspect status/retry stop.

## Final checks

```text
uv run pytest -q
# exited 0; 102 tests collected, one documented upstream warning

UV_CACHE_DIR=/private/tmp/heavenly-uv-cache uv run python -m compileall -q src tests
# exited 0

docker compose -f compose.yaml config
# exited 0; configuration rendered without starting a service

git diff --check -- src/heavenly_health/runtime src/heavenly_health/cli.py tests/test_runtime.py docs/testing/runtime-lifecycle.tdd.md
# exited 0
```

`uv build` also completed successfully in the parent environment and produced the source distribution and wheel under `dist/`. The earlier Codex sandbox could not resolve Hatchling because that isolated sandbox lacked dependency/network access; that sandbox-only failure is not the final build result.
