# Merge comparison is a non-atomic precondition

Date: 2026-08-24

Status: Accepted

Builds on: [watcher-as-sensor.md](watcher-as-sensor.md), [ci-approved-sha-gating.md](ci-approved-sha-gating.md)

## Context

The TL records merge evidence from the watcher snapshot and passes the
expected base SHA, head SHA, patch digest, and merge-tree SHA to the
`merge_pr` effect. The Rust merge service fetches the current pull request,
recomputes that evidence, and rejects a mismatch before calling
`ForgejoClient::merge_pull_request`. The Forgejo adapter currently accepts
only a pull-request number and merge method; it exposes no expected-head (or
expected-base) compare-and-swap parameter. Observation and the remote merge
request are therefore separate operations.

## Decision

Evidence comparison is a fail-closed precondition, not an atomic server-side
CAS. A push, base-branch update, or other concurrent Forgejo action can occur
between the successful comparison and the merge request, so ExoMonad must not
claim that this boundary is race-free. A comparison failure prevents the
request and enters the normal reconciliation path. A successful request is
accepted only after authoritative Forgejo observation emits `pr.merged` (or a
subsequent merged snapshot is adopted); an unexpected or conflicting outcome
is treated as reconciliation/integrity evidence rather than inferred from the
effect response alone. Any future Forgejo endpoint that provides an atomic
expected-head guard must be wired through the effect and update this decision
and its tests before atomicity is claimed.

The contract is exercised by
`tl_loop/tests/test_effects.py::test_merge_pr_emits_compare_and_swap_evidence`,
`tl_loop/tests/test_review.py::test_missing_direct_compare_evidence_opens_integrity_gate`,
and the merge reconciliation tests in `tl_loop/tests/test_reconcile.py`.
