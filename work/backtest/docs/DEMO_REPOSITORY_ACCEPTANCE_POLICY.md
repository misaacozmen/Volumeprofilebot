# Demo repository acceptance policy

This policy applies only to a demo repository source publication. It does not authorize a broker connection, a live order, a production release, or a history rewrite of the source repository.

## Replaced requirements

Demo publication does not require or consume:

- an RSA owner signature;
- a signed replay receipt or replay ledger;
- a signed push lease.

The demo acceptance validator rejects those legacy fields instead of treating old signed artifacts as current acceptance.

## Required replacement evidence

The validator requires four external, branch/commit-bound inputs:

1. An owner declaration stating the account rotation date, that the old credential is no longer used, and that no credential value was shared. This declaration does not stand in for Git history cleanup or push approval.
2. A history sanitization report showing the verified Git history/ref scan and an external sanitized mirror with zero matches.
3. A sensitive-data scan showing zero working-tree, reachable-history, ref-name and quarantine matches. The private denylist remains external and is referenced only by SHA-256.
4. An explicit, unsigned push approval bound to the exact branch, source commit, source tree and destination ref. This is the only replacement evidence that authorizes a branch/commit-specific publication decision.

Missing or invalid evidence produces `BLOCKED_EXTERNAL_ACCEPTANCE`; it never produces `DEMO_REPOSITORY_READY`.

## Safety boundaries

The live risk/approval path, broker write prohibition, CAS/ledger rules and data-manifest hash bindings remain unchanged. Items 12–19 may be distributed as source, but missing provider data or an unverified manifest must remain non-production-ready.

The current branch/HEAD/tree and all tracked plus non-ignored current files are recorded in the demo ZIP manifest. Generated binaries, runtime caches, credentials, private keys and other excluded paths are listed without exposing their contents. A package with no owner declaration, history report, sensitive scan and push approval is a source handoff, not a publication acceptance.
