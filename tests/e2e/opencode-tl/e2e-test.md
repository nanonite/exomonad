# E2E OpenCode TL Test Mode

This test validates OpenCode as root TL. The OpenCode agent receives its task via `initial_prompt` in `config.toml`, delivered through the ACP chain (`opencode serve` → port capture → `opencode run --attach`). OpenCode does not read `.claude/rules/` — its instructions come entirely from `initial_prompt`.

The testrunner companion (Claude haiku) validates:
1. `opencode-tl-test.txt` was created in the OpenCode worktree
2. `[OC-TL-DONE]` arrived via Teams inbox through `send_message` (root agents have no parent, so they use `send_message` with an explicit peer target instead of `notify_parent`)

This file is loaded by the testrunner companion only (via `.claude/rules/`).
