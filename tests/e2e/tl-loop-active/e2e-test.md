# Programmatic active TL rules

This is an active controller test plus a smoke of the command used in the default TL window; it is not an interactive-agent test.

- Do not run `exomonad init`, `exomonad serve`, Claude, Codex, OpenCode, or any other agent process. The launcher smoke invokes `python3 -m tl_loop` directly so this test never creates a session.
- Do not create, attach to, or kill a tmux session. The controller must run in the current process.
- Use exactly two leaves: `active-slice-a` and `active-slice-b`.
- Keep their source boundaries disjoint and execute the declared unittest plan in each real worktree.
- Treat the ledger queue as the only event source. Do not synthesize events in the driver or bypass the durable run store.
- Review approval, adjudication, and PR transport are deterministic test doubles at the effect boundary.
- Stop on any ambiguity, mutation-blocked error, sequence gap, unmerged PR, or cleanup failure.
