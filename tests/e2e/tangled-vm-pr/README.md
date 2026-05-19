# Tangled VM PR E2E

This harness drives the Codex dev-leaf PR path against a pre-provisioned Tangled
VM. It is separate from `tests/e2e/tangled-pr-codex`, which owns the local
container/knot relay path.

Required environment:

- `TANGLED_VM_GIT_REMOTE`: SSH Git remote for the VM repo.
- `TANGLED_VM_KNOT_WS_URL`: knot event stream URL, usually `ws://host:port/events`.
- `TANGLED_VM_SPINDLE_WS_URL`: spindle event stream URL, usually `ws://host:port/events`.
- `TANGLED_VM_OWNER_DID`: owner DID used by the VM Tangled deployment.

Optional environment:

- `TANGLED_VM_APPVIEW_URL`: appview base URL checked for reachability.
- `TANGLED_VM_REPO_NAME`: human-readable fixture name.
- `TANGLED_VM_CLEANUP_REMOTE=0`: keep pushed branches after the run.
- `TANGLED_VM_PR_E2E_TIMEOUT_SECONDS`: validator timeout, default 900.

Run with `just e2e-tangled-vm-pr` after the VM repo/auth is provisioned. The
validator asserts local PR creation, reviewer approval, `approved_at_sha`, spindle
success for that SHA, and `[MERGE READY]` delivery.
