# Public-release sanitization TDD evidence

## Source

The journeys were derived from the requirement to publish a reusable protocol
without owner identity, deployment details, credentials, local state, or private
Git history.

## User journeys

- As a maintainer, I can export only an explicit tracked-file manifest into a
  new directory so ignored and local files cannot leak into a public release.
- As an operator, I receive redacted path/reason findings without secret values
  being echoed.
- As a public user, I retain safe `.env.example` and container-home templates.

## RED and GREEN checkpoints

| Guarantee | Test target | RED evidence | GREEN evidence |
| --- | --- | --- | --- |
| Private markers, credential paths, home paths, secrets, and traversal fail closed | `tests/test_public_release.py` | `0b420c8`: module absent during collection | `f3fa60a`: 14 focused tests passed |
| Safe environment and container templates are retained | `tests/test_public_release.py` | `82a943d`: two intended false-positive failures | `7587079`: 16 focused tests passed |

The audit reports only the affected relative path and reason. It never includes
the matched marker or secret-shaped value.

## Coverage and gaps

The release helper has unit coverage for allow and deny paths. Live repository
export, GitHub security configuration, secret/history scanning, packaging,
Docker verification, and CI are release gates recorded separately in the final
release report; they are not mocked into this unit report.
