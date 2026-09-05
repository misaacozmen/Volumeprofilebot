# SUPER1 Local Runtime Readiness V11 — 2026-09-04

Contract ID: `SUPER1_LOCAL_RUNTIME_READINESS_V11_20260904`  
Authority: `PROJECT_ARCHITECT`

## Corrected architectural decisions

The architect aggregate-hash constant is corrected and the superseded value is
cancelled:

```text
architect_expected_hash_corrected=true
canonical_aggregate_engine_hash=a7456f84c7d07c09a728d0c8c83f8b7e491ed61bbce124c06bcf412beb31530f
```

The canonical algorithm is the production `backtest.engine_pipeline.source_code_hash()`
algorithm: initialize SHA-256, iterate the lexicographically sorted files matching
`backtest/*.py`, and for each file update the digest with its UTF-8 filename bytes
followed by its raw file bytes. The independently reproduced calculation, the
baseline HEAD, and `super1_signal_contract.json` all resolve to the canonical hash
above. No `backtest/*.py`, product engine, or engine hash implementation was changed.

## Semantic classification

V08/V09/V10 semantic packages are historical research evidence, not the live-release gate. Their `1,179 unresolved`, `1,054 resource`, and `17 schema` results must not be rewritten as PASS. The existing files and `proposal-005` remain unchanged. No fabricated mapping, formula, or expected value may be produced.

The authoritative gate for this version is direct runtime testing through the real production paths. `semantic_acceptance` is not PASS. The legacy classification is:

`legacy_semantic_evidence=INCOMPLETE_HISTORICAL_NON_GATE`

This V11 decision preserves the historical semantic results and prevents a non-working meta-evidence producer from substituting for live runtime behavior.

## V11 runtime gate

The V11 gate is the logical AND of:

- aggregate engine hash closure;
- Super1 runtime/config/contract/manifest hash closure;
- correct RTH validation;
- targeted suite;
- full test suite;
- `PRE_SEND_DEFERRED` and `CHECK_RETRYABLE` stress tests;
- credential fail-closed test;
- runtime safety matrix;
- independent evidence verification.

All test and stress counters must be zero for failed, error, skipped, xfailed, xpassed, deselected, missing, duplicate, or unexpected test nodes. Duplicate broker sends and concurrency exceptions are also forbidden.

For the corrected run, every Phase 2 gate is PASS:

```text
phase2_status=PASS
local_runtime_readiness=READY_FOR_ARCHITECT_PHASE3_DECISION
phase2_required=false
runtime_safety_matrix=PASS
remaining_blockers=[]
overall=NO_GO
```

The final working bytes were independently re-run on CPython 3.11 on 2026-09-04:

```text
targeted_tests=144 passed
full_suite=532 passed
failed=0
errors=0
skipped=0
```

## Phase boundary

Fresh install and V16 transfer orchestration are deferred to Phase 3 by the architect. This phase does not connect to a server, deploy, use AWS, send broker orders, sign a release, or commit/push changes.

These are Phase 3 prerequisites, not Phase 2 blockers:

```text
phase3_prerequisites:
fresh_install_status=DEFERRED_TO_PHASE3_BY_ARCHITECT
transfer_orchestration_status=DEFERRED_TO_PHASE3_BY_ARCHITECT
```

The corrected Phase 2 result fields are:

```text
deployment_status=NOT_RUN
successor_release_status=NOT_BUILT
phase3_execution_allowed=false
```

Even if every local runtime gate passes, Phase 3 must not be started by this phase.

## Phase 3 operator decision addendum — 2026-09-04

After the Phase 2 PASS result, the operator explicitly authorized continuing the
work needed to enter Phase 3. This authorization does not rewrite the Phase 2
boundary above and does not authorize a broker order or unattended execution.

The approved Phase 3 preparation scope is:

```text
target=LOCAL_WINDOWS_PC
installation=FRESH_INSTALL
broker_account=NEW_XM_DEMO_ACCOUNT
campaign=NEW_DEMO_CAMPAIGN
source_server_migration=NONE
second_candidate=OUT_OF_SCOPE
phase3_execution_allowed=true
phase3_status=APPROVED_FOR_PREPARATION
broker_order_authorized=false
unattended_execution_authorized=false
```

Phase 3 must create and verify the signed release, install only the Super1
dependencies on the local PC, bind credentials locally without placing secrets in
the repository, and provide explicit manual start/stop controls. The operating
window is America/New_York 09:20 through 13:00; DST must be resolved from the
America/New_York time zone rather than fixed conversion to the PC's local clock.
A separate operator confirmation remains mandatory before any demo smoke order or
continuous unattended run.

## Phase 3 preparation validation — 2026-09-05

The local target was verified against the new XM demo account without sending an
order. The terminal reported `trade_mode=0`, `connected=true`,
`trade_allowed=true`, and `tradeapi_disabled=false`. The verified broker binding
is stored in the protected runtime configuration.

```text
terminal_build=6180
MetaTrader5_package=5.0.6162
targeted_tests=154 passed
full_suite=533 passed
failed=0
errors=0
skipped=0
manual_session_control=PASS
new_york_start_guard=09:20
new_york_stop_boundary=13:00
broker_order_authorized=false
```
