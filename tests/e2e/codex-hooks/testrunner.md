# Codex Hooks E2E Validator Notes

This test validates the production Codex hook path for root/TL/dev/reviewer roles. The active validator is a process companion running `validate.sh`; this file records the expected checks for humans and future agent validators.

## Expected Coverage

- Root Codex config exists in `.codex/config.toml` with `hooks = true` and ExoMonad MCP args for role `root`.
- Shared Codex user config exists at `$CODEX_HOME/config.toml` with the ExoMonad hook commands and trusted hook state for PreToolUse, PostToolUse, and Stop.
- Codex root produces live hook trace logs with `runtime=Codex` and `agent=root` after it calls an ExoMonad MCP tool.
- A Codex TL worktree is spawned and receives `.codex/config.toml`, role `tl`, and TL instructions, without a per-worktree `.codex/hooks.json`.
- A Codex dev leaf worktree is spawned and receives `.codex/config.toml`, role `dev`, and dev instructions, without a per-worktree `.codex/hooks.json`.
- The dev leaf calls `notify_parent`, producing `[CODEX-HOOKS-DEV-DONE]` through normal messaging.
- Filing the dev PR uses the local `.exo/prs.json` flow; this test must not require GitHub auth or external PR APIs.
- Filing the local dev PR causes a normal Codex reviewer-role agent to spawn.
- The reviewer gets role `reviewer`, reviewer instructions, shared Codex hook config, and live hook trace logs. Reviewer v1 may allow/no-op hook decisions; the proof is correct scoped config/context plus server hook receipt.

## Observation Commands

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
tmux list-windows -t "$EXOMONAD_TMUX_SESSION"
grep -R "\\[hook\\] received" "$REPO_ROOT/.exo/logs" 2>/dev/null
find "$REPO_ROOT/.exo/worktrees" -path '*/.codex/config.toml' -print
find "$REPO_ROOT/.exo/worktrees" -name codex-hooks-dev-output.txt -print
cat "$REPO_ROOT/.exo/prs.json" 2>/dev/null
```

Do not mutate the repo while validating. The root, TL, dev leaf, and reviewer must create all tested artifacts through normal ExoMonad runtime paths.
