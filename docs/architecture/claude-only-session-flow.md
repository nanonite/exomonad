# Claude-Only Session Flow Verification

Chainlink: #421

This note records the bounded Claude-only ExoMonad flow that is safe to run when Claude credits are scarce, plus the release-only expansion that should run when credits are available.

## Bounded Flow

Target: `just e2e-claude-only`

The bounded harness launches an isolated temp repository, runs `exomonad init` with `root_agent_type = "claude"`, and gives the root TL a role-safe prompt that only calls TeamCreate. It validates:

- the server starts with `port = 0` and accepts connections;
- Claude SessionStart registration reaches the server;
- TeamCreate registers a team for the session;
- a new Claude Teams metadata directory appears;
- the root TL does not attempt direct writes or child spawns.

The last recorded live run passed on 2026-05-27. `E2E_STATUS.md` carries the observed evidence.

## No-Token Preflight

Target: `just check-e2e-claude-only`

This target only performs a shell syntax check of `tests/e2e/claude-only/run.sh`. Use it during normal development to keep the harness parseable without launching Claude Code or consuming credits.

## Credit-Gated Release Expansion

The bounded flow does not prove the full Claude runtime-role matrix. Before a release that changes Claude runtime launch, role hooks, message delivery, or reviewer behavior, run the Claude rows in `docs/architecture/runtime-role-e2e-matrix.md` with explicit operator approval for token use:

- `just e2e-claude-only`
- `just e2e-subtl-recursive-fork-wave runtime=claude`
- reviewer hardening/authorship E2E once #301 provides the harness
- lifecycle invariants E2E once #305 provides the harness

Do not replace a failed full-matrix run with the bounded smoke. The bounded smoke only verifies Claude root startup and Teams registration.
