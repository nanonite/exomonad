# E2E acceptance modes and test pattern details

Moved verbatim from the root `CLAUDE.md`.

`just e2e-role-config` runs `tests/e2e/init-recovery/run.sh` with
`E2E_ROLE_CONFIG_ONLY=1`. That mode reuses the same Phase 0 build, fresh WASM,
isolated Codex home and run-scoped tmux server as the composed acceptance, then
runs only `tests/e2e/init-recovery/role-config-acceptance.sh`: a standalone
`exomonad serve` loaded from project configuration, an `init` repeating the
same harness/model/effort values, repeat-init duplicate checks, genuine
model/effort/harness overrides that must hold before dispatch, and a
no-identity (old-binary) server that must hold with
`role_configuration_unavailable`. It prints `ROLECONFIG-1..5` and
`ROLECONFIG-ALL PASS`. The R1 regression is
`tests/e2e/init-recovery/check_role_config_preflight.py` plus the
`init::tests::*preflight*` unit tests; the recorded red-on-old-code logs live in
the run's evidence directory.

`just e2e-stored-proof` runs `tests/e2e/init-recovery/run.sh` with
`E2E_STORED_PROOF_ONLY=1`. It reuses the same Phase 0 build, fresh explicitly
selected WASM, isolated Codex home and run-scoped tmux server, then runs
`tests/e2e/init-recovery/stored-proof-acceptance.sh`: the retained
`sha256:v1:` terminal-owner authorization, the rejected prelaunch configuration
action, the approved #814 recovery gate and the historical f446/f285 receipts
are seeded against a real standalone `serve`, and `init --continue` with
identical explicit flags must derive one linked `sha256:v2:` authorization and
launch exactly one generation-3 OpenCode DeepSeek/high successor. Repeat init
before and after a supported server restart adds no duplicate launch, and
`check-stored-proof-negatives.py` runs the shipped controller archive against
every negative (forged/malformed digest, wrong owner/scope/manifest/predecessor,
unaccepted identity, unknown prior effect, live worker, independent gate
rejection, genuine model/effort change). It also re-runs the role-configuration
acceptance and prints `STOREDPROOF-1..6` then `STOREDPROOF-ALL PASS`.

`just e2e-stored-proof-literal` runs the same Phase 0 build with
`E2E_STORED_PROOF_ONLY=1 E2E_STORED_PROOF_LITERAL=1`, then runs
`tests/e2e/init-recovery/stored-proof-literal-acceptance.sh`. Instead of
regenerating a v1 proof under a scratch policy, it copies the ACTUAL retained
controlled-plan backup read-only and installs the retained historical
`harness_policy.toml`, `harness_capability.toml` and child manifest
(`4be5ddad...`). The shipped controller must reproduce the literal v1 digest
`a8b9524b...` under that context before continuation, then link exactly one
generation-3 OpenCode DeepSeek/high successor to `f285`, preserve every
historical byte, and add no duplicate after repeat init or a server-only
restart. It prints `LITERAL-1..5` then `LITERAL-ALL PASS`. The digest excludes
the assignment coordinates (ledger sequence, dispatch intent id, terminal
invocation digest, worktree fingerprint), so those alone are rebound to the
disposable ledger and worktree; the retained values name the read-only Beast
filesystem. Set `E2E_STORED_PROOF_RETAINED` to point at another backup copy.

`just e2e-dispatch-collision` runs the same Phase 0 build, fresh explicit WASM,
isolated Codex home, run-scoped tmux server and standalone `serve` as the
stored-proof modes. It seeds the production-shaped history the literal fixture
used to omit (both old `tl.dispatch_intended`/`tl.spawn_requested` rows, the
exact rejected prelaunch journal, the literal `a8b9524b...` proof and the
approved gate), replays the old v1 request through the production host path so
the exact prelaunch refusal is durable, then proves the reauthorized v2 retry
selects one declaration, launches one real generation-3 OpenCode DeepSeek/high
successor, adds no duplicate across repeat init, server restart, adoption and
concurrent requests, holds every host negative control, and leaves historical
bytes unchanged. Before that retry it pauses the real compiled server after
authorization persistence, after request persistence, after launch and after
durable adoption, kills the controller and host at that exact window, then
restarts and re-runs compiled `init --continue` at each boundary. Each marker is
checked for its exact boundary, dispatch scope, paused-host pid and live
`exomonad serve` command; the adoption window also requires the durable
`.exo/agents/<owner>/adoption.json` receipt, exactly one launch row older than
the marker, and a controller journal action still `intended` with no result. See
the interruption table in
`docs/guides/changing-routes-mid-run.md`. Set
`E2E_DISPATCH_RETAIN_ARTIFACTS` to retain, per boundary, and independently
rehash the exact binary, WASM guests and controller archive consumed by the
passing run. It
prints `DISPATCH-1..6` then `DISPATCH-ALL PASS`. `just
e2e-dispatch-interruptions` (`E2E_DISPATCH_INTERRUPT_ONLY=1`) runs the four
interruption boundaries in separate fixtures and prints
`DISPATCH-INTERRUPT-<boundary> PASS` then `DISPATCH-INTERRUPT-ALL PASS`. The
resolver and its backward-compatibility contract are described in
`docs/guides/changing-routes-mid-run.md`; the Docker-free syntax/compile checks
are part of `just check-e2e-init-recovery`.

`just e2e-recreated-leaf-recovery` is non-interactive like
`just e2e-tl-loop-active`: it builds the binary and WASM from this worktree,
brings up its own disposable Forgejo, and prints one PASS/FAIL line per T-item.
It owns everything it creates and fails if anything outlives the run — see
§ Real-server integration tests below.

`just e2e-tl-loop-active` is intentionally non-interactive: it uses a bounded
Python controller, a bare scratch remote, deterministic effect/review stubs,
and the exact default TL-window command without creating an ExoMonad session.

`just test` builds the WASM plugins before running the full Rust workspace test
set, so the WASM integration target has its plugin available. New Rust test
targets must be reachable from `just test`. `just rust-test` is the fast
library-only Rust loop. `just rust-test-all` runs the full Rust workspace test
set, including native integration targets. New Rust tests must also remain
reachable from `just rust-test-all`. `just test` also runs
`just tl-loop-typecheck`, which applies the strict mypy configuration in
`tl_loop/pyproject.toml` through `scripts/check_typecheck_ratchet.py`. Its
checked-in `tl_loop/typecheck-baseline.json` records per-file, per-error-code
counts and the mypy version that produced them, and may only shrink; run
`tl_loop/.venv/bin/python scripts/check_typecheck_ratchet.py --update` after
fixing errors, and pass `--allow-increase` only when deliberately accepting new
type debt. CI runs the same recipe in the `typecheck` job of
`.forgejo/workflows/ci.yml`: it creates a throwaway virtualenv and installs the
`dev` extra's tools directly (`pytest`, the pinned `mypy`, and `ruff`) instead
of installing the `tl-loop` package, whose wheel has no runtime dependencies
and is never imported by the job. `EXOMONAD_PY` points at that virtualenv's
python. `just e2e-typecheck-ci` runs the real job, extracted from the real
workflow, against a disposable Forgejo (see § Real-server integration tests).
Run the tmux first-window environment integration tests with
`just tmux-session-environment-test [inside_nix] [tmpdir]`; the default is
`true /tmp`. Pass `false` to use the host Rust toolchain and tmux binary.
