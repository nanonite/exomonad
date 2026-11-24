# Dispatch prelaunch refusal contract

Status: authoritative for the controller and the standalone host.

## Purpose

A blocked-leaf resume may be retried after a durable host refusal **only when
the refusal proves that no launch occurred**. Model, harness, runtime and
effort changes between `exomonad init --continue` runs are a supported continue
path: the prior route the host refused is replaced by the newly accepted route,
while the refused proof and journal entry stay immutable history.

This contract is implemented once on each side and enforced by a cross-language
test:

- controller: `PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS` and
  `is_proven_prelaunch_role_route_refusal` in
  `tl_loop/loop/prelaunch_refusal.py`. The driver imports those names and does
  not keep a copy; `tl_loop/loop/launch_reconciliation.py` uses the same set to
  reject a timed-out resume from the host's durable `tool.called` row.
- host: `PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS` and
  `is_prelaunch_role_route_refusal_error` in
  `rust/exomonad-core/src/handlers/dispatch_authorization.rs`

`tl_loop/tests/test_driver.py::test_prelaunch_refusal_contract_matches_the_compiled_host_definition`
fails if the two sets drift.

## The closed error set

Exactly these two host errors prove a modern prelaunch refusal:

| Constant | Error text |
| --- | --- |
| `ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED` | `Invalid input: role route configuration is stale or was not accepted by this server` |
| `ROLE_ROUTE_DISPATCH_AUTHORIZATION_MISSING_OR_AMBIGUOUS` | `Invalid input: role route dispatch authorization is missing or ambiguous` |

The second error is what the deployed host recorded for the live #814 attempt
(`action-journal.json` entry #36). Both errors are emitted before any launch.
The older missing-route refusal
(`Invalid input: blocked-leaf controller recovery requires its accepted role route`)
remains a separate, narrower historical contract.

The set is closed. An unrelated error string, a generic failed request, a
successful request, an unresolved (`intended`/`unknown`) outcome or a row with a
result payload is never proof.

## Envelope requirements

Controller (action journal entry):

1. `operation == "resume_blocked_leaf"` and `status == "rejected"`.
2. `arguments.route_configuration_fingerprint` is a versioned `sha256:v1:`/`sha256:v2:` identity.
3. `result.success is False` and `result.result is None`.
4. `entry.error` and `result.error` are the **same** member of the closed set.

Host (`tool.called` ledger row):

1. `tool_name == "resume_blocked_leaf"`, `success == false`, no `result` payload.
2. `error` is a member of the closed set.
3. `arguments.role_route` reproduces the superseded declaration's full route
   tuple and the exact owner/predecessor/branch/worktree binding.
4. The row is positioned inside the superseded observation's window, follows
   its declaration and precedes the selected declaration.
5. Exactly one current per-slice recovery gate is `approved`, owned by the run
   and controller epoch that emitted the selected declaration.
6. The owner invocation proves no launch: it is still the named predecessor with
   no launch row, or the single linked successor with exactly one launch row.

A controller release additionally requires that the refused action was
authorized by the saved proof (`route_authorization_digest`, configuration
identity, desired route and assignment all match) before a changed route may
replace it. The fresh route proof links over the saved digest with transition
`prelaunch_refusal_route_reauthorized_for_accepted_configuration`; the saved
proof is preserved in `configured_route_authorization_history`.

## Diagnostics

A hold under this contract must name the unclassified entry: its index, status,
error text, route/config fingerprint and dispatch intent id. Task text and
credential material are never included.
