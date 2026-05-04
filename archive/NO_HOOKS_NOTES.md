# `chainlink init --no-hooks` Behavior

When `--no-hooks` is passed to `chainlink init`:

- `.claude/` directory is **not created** — no `settings.json`, no hook scripts installed
- `prompt-guard.py` is never installed, so it never runs on `UserPromptSubmit`
- The `.chainlink/rules/*.md` files **are** still written to disk (rules dir is always created), but nothing injects them into the agent's context at runtime

## Result

With `--no-hooks`, you get the database and rules files on disk, but zero runtime enforcement:
- No rule injection into Claude's context
- No `work-check.py` blocking (no issue required before coding)
- No `session-start.py` context on session open

It's chainlink as a plain CLI tool — issue tracking and session management still work, but without any Claude Code hook integration.
