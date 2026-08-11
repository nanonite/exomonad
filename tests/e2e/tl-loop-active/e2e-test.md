# Programmatic active TL rules

This is an M5.6 controller test, not an interactive-agent test.

- Do not run `exomonad init`, `exomonad serve`, `exomonad new`, Claude, Codex, OpenCode, or any other agent process.
- Do not create, attach to, or kill a tmux session. The controller must run in the current process.
- Use exactly two leaves: `active-slice-a` and `active-slice-b`.
- Keep their source boundaries disjoint and execute the declared unittest plan in each real worktree.
- Treat the ledger queue as the only event source. Do not synthesize events in the driver or bypass the durable run store.
- Review approval, adjudication, and PR transport are deterministic test doubles at the effect boundary.
- Stop on any ambiguity, mutation-blocked error, sequence gap, unmerged PR, or cleanup failure.
