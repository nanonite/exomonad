# Compiled-host blocked-resume recovery acceptance

`just e2e-blocked-resume-host` runs the shipped `exomonad serve` against a
disposable repository, a mock forge, a run-scoped tmux server (every call uses
the harness `-S` socket), and the production controller action journal and
launch reconciler. It prints one PASS line per case:

- `slow-adoption` — the host handler is held past the old 10s client timeout,
  the client times out, the journal stays `unknown`, the production
  reconciliation hook holds on absent evidence and then adopts exactly one
  `agent.spawned` row by `dispatch_intent_id` on a later tick. No second
  request, no second window.
- `init-continue-before-window` — the host is SIGKILLed after the intent is
  recorded but before a window exists. A real `exomonad init --continue`
  starts the replacement server and controller; the pending host journal
  launches exactly one successor, the controller adopts it, and a repeat
  `init --continue` adds nothing.
- `sigkill-after-window` — the host is SIGKILLed after the worker window
  and routing record exist but before `agent.spawned`. The restarted host
  re-drives the pending journal, adopts the already-live successor and writes
  exactly one launch row; the controller adopts it and exactly one owner
  window survives. (This boundary uses a compiled server restart in the
  preserved session because the fixture has no prior managed controller
  target for `init --continue` to recover.)
- `terminal-failure-hold` — a SIGKILLed host restarts into a dirty-worktree
  preflight refusal, so the ledger carries one terminal `agent.spawn_failed`
  and no launch row. The controller keeps the journal `unknown` with a hold
  diagnostic naming the intent and the failure; nothing is relaunched. The
  operator release path is documented in
  `docs/launch-capable-effect-contract.md`.

A missing binary, WASM, or tmux is an explicit SKIP with its reason. The run
owns its temporary repository, mock forge, Chainlink database, tmux server and
processes.
