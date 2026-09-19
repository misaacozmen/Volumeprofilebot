# Broker identity history decision

Status: `HISTORY_RETAINED_KNOWN_INACTIVE_IDENTIFIERS`

The broker account identifiers found in reachable Git history are retained as
historical findings. The account credentials are inactive according to the
task instruction, so this change applies a forward fix only. It does not
rewrite history, force-push, or delete branches or tags.

This status is an engineering decision, not user approval and not a claim that
history is clean. The scanner continues to publish raw historical findings;
the immutable baseline records only Git object identity, object type, opaque
denylist rule ID, and occurrence count.

Starting `origin/main` SHA: `023e39b288863ac93df9e7cfa1b7f218029f6458`.

The current-tree scan remains independent from historical baseline acceptance:
known historical objects do not exempt a new current-tree finding.

The acceptance scopes are separate. `identity-structure` scans the candidate
PR tree without a secret. `identity-full` scans the exact candidate SHA with the
protected denylist and historical baseline. `repository-audit` scans every
remote branch/tag tip tree plus reachable history; an old dirty main tree must
remain visible there and must not block candidate acceptance.

On 2026-09-19, repository Actions metadata was checked without reading secret
values: the repository has no `BROKER_IDENTITY_DENYLIST_JSON` secret and no
configured environment. The local GitHub session was not authenticated and the
`gh` CLI is unavailable, so owner secret configuration could not be performed.
The protected full scan remains
`BLOCKED_OWNER_SECRET_CONFIGURATION`; merge and post-merge audit closure are
intentionally not claimed.

The acceptance audit records the current `origin/main` tree separately: it has
43 denylist matches and 10 structural violations. Those findings remain visible
in repository audit output. `origin/HEAD` is the symbolic alias for that same
main commit and is excluded from independent ref-tip counting.

Authoritative final-SHA reports are emitted outside the repository at
`C:\Users\ISAAC\Documents\broker-identity-evidence-20260919-final` and are
uploaded by CI as artifacts.
