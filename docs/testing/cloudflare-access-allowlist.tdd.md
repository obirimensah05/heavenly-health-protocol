# Cloudflare Access allowlist automation — TDD evidence

## Source

No external implementation plan was used. The user journey was derived in-session:

> As a Heavenly owner, I can preview and then explicitly apply an exact email addition to a pre-existing Cloudflare Access allowlist, so that approved users can be added without manual dashboard edits.

## RED evidence

1. `uv run --extra dev pytest tests/test_cloudflare_access.py`
   - Result: collection failed with `ModuleNotFoundError: No module named 'heavenly_health.cloudflare_access'`.
2. `uv run --extra dev pytest tests/test_cli.py`
   - Result: the new `heavenly access allow` preview test failed with exit code `2` because the command did not exist.

## GREEN evidence

```text
uv run --extra dev pytest
11 passed in 0.57s
```

```text
uv run --extra dev pytest tests/test_cloudflare_access.py --cov=heavenly_health.cloudflare_access --cov-report=term-missing
2 passed
cloudflare_access.py coverage: 80%
```

```text
uv build
Successfully built source distribution and wheel
```

```text
git diff --check
passed
```

## Guarantees

| # | Guarantee | Test | Result |
|---|---|---|---|
| 1 | Existing exact-email and non-email include rules are preserved when a new exact email is added. | `tests/test_cloudflare_access.py::test_add_email_to_include_rules_preserves_existing_rules_and_deduplicates` | PASS |
| 2 | The Cloudflare client reads and updates only the configured account/application/policy endpoint and sends bearer auth without printing it. | `tests/test_cloudflare_access.py::test_allow_email_updates_only_the_target_access_policy` | PASS |
| 3 | `heavenly access allow <email>` is preview-only unless `--apply` is explicit. | `tests/test_cli.py::test_access_allow_defaults_to_a_non_mutating_preview` | PASS |
| 4 | A configured target must resolve to the exact policy ID with decision `allow`; other policy types fail closed before PUT. | `tests/test_cloudflare_access.py::test_allow_email_fails_closed_for_a_non_allow_policy`; `test_policy_summary_returns_only_validated_target_identity` | PASS |
| 5 | A configured preview shows the account/application/policy IDs, policy name, and `allow` decision without exposing the API token. | `tests/test_cli.py::test_access_allow_preview_shows_the_validated_policy_target` | PASS |

The policy-type hardening was added after a security review. Before implementation,
the focused tests failed because a `bypass` policy was accepted,
`policy_summary()` did not exist, and preview omitted target identity. After the
fail-closed validation and read-only preview were implemented:

```text
uv run pytest tests/test_cloudflare_access.py tests/test_cli.py -q
8 passed
```

## Known gap

A live Cloudflare policy mutation was intentionally not performed in this test
run. The tests use `httpx.MockTransport` to verify the request contract and
fail-closed policy checks. Operators must still run preview against the selected
live policy, review its displayed identity/decision, apply explicitly, and verify
the resulting policy through Cloudflare before considering the live change complete.
