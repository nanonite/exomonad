# Plan — make `exomonad init` show a live TL

## Context

`exomonad init` starts a server and a Watcher window, and the TL window looks
dead. The controller is the whole point of the system, and an operator has no
way to see it working without knowing to run a separate command.

The root cause is a chain of four things, none individually obviously wrong:

1. **A `TL` window *is* created** — `init.rs:1523` runs
   `tl_loop run … --wait-for-plan`. It is not missing; it is blocked.
2. **`write_tl_loop_plan` (`init.rs:790`) only writes `plan.json` when
   `initial_prompt` is configured.** With no `initial_prompt`, no plan file is
   ever created.
3. **`--wait-for-plan` then blocks forever**, printing one line —
   `"[TL loop] waiting for plan at …"` (`__main__.py:185`) — and nothing more.
   The window looks hung because it *is* hung, by design, on a file that
   nothing created.
4. **`status` is one-shot** (`__main__.py:298`). It prints JSON and exits.
   There is no live view anywhere, and no `--watch`.

Two further defects sit behind that:

- **`init` never validates `harness_policy.toml`** — zero references in
  `init.rs`. The file is a hard startup error for the controller, so a project
  missing it (like `beast-workspace/workspace`) waits on a plan, and *then*
  dies on policy. Two sequential invisible failures.
- **`tl_loop_command` (`init.rs:837`) uses bare `python3`** — the same bug
  class just fixed in the justfile, where the system interpreter is not the one
  carrying the package.

Intended outcome: `exomonad init` in a project root either brings up a
visibly-live TL, or tells the operator exactly which file is missing and how to
write it. No silent hang.

## Design note: two windows, not one

You asked for `init` to run `python3 -m tl_loop status`. Doing that *in place
of* the current TL window would remove the process that does the orchestrating
— `run` is the controller, `status` is a read of its state.

So: the **TL** window keeps running the controller, and a new **Status** window
carries the live view. Two windows, two jobs. If you'd rather have one window
that runs the controller and renders status inline, that's a different and
larger change to the controller's own output, and worth deciding before P2.

## Phases

### P1 — Preflight: fail loudly instead of hanging

In `init.rs`, before any window is created, validate the project root the
command was invoked from:

- `.exo/harness_policy.toml` exists and parses, with all three role tables.
  Reuse the existing validator rather than reimplementing — `tl_loop`'s
  `select/policy.py::load_policy` is authoritative, so shell out to a small
  `python3 -m tl_loop preflight` rather than duplicating the rules in Rust.
- `.exo/tl-loop/plan.json` exists and is a valid closed-key `WorkPlan`, **or**
  `initial_prompt` is configured so one will be written.

On failure, print the missing path, what it is for, and a minimal valid
example, then exit non-zero. Do not create a session that cannot work.

Add `--skip-preflight` for the deliberate "start it and I'll write the plan
into the waiting window" workflow, which is the only case `--wait-for-plan`
actually serves.

### P2 — `tl_loop status --watch`

Add `--watch` (and `--interval`, default ~2s) to the `status` subcommand in
`tl_loop/__main__.py`. It re-reads the run checkpoint on a timer and redraws.

It must degrade well, because the common case at startup is *no state yet*:

- No `run.json` → "no run yet; controller is waiting for
  `.exo/tl-loop/plan.json`" plus the path.
- Run present → phase, slice table with status and PR number, pending gates
  called out prominently, park causes with their cause string, consumed event
  offset.
- Never crash on a partially-written checkpoint; the writer is atomic but a
  reader can still race a rename.

Keep the existing one-shot behavior as the default so scripts and
`control_read_model` are unaffected.

### P3 — The Status window

Add a `Status` window in `init.rs` beside `Watcher`, following
`ensure_watcher_dashboard_window` (`init.rs:112`) exactly — same idempotent
"does it already exist" check, same non-fatal failure handling, so a tmux
problem degrades to a missing dashboard rather than a failed init.

It runs `tl_loop status --watch --project-root <cwd> --run-id root`, with the
same `cwd` the TL window uses — the directory `init` was invoked from.

### P4 — Resolve the interpreter

`tl_loop_command` and the new status command must not hardcode `python3`.
Resolve in this order: `EXOMONAD_TL_LOOP_PYTHON` (already honored by
`control_gate.rs:76`), then a repo-local `tl_loop/.venv/bin/python`, then
`python3`. Reuse the existing env var rather than inventing a second one.

### P5 — Make the waiting state legible

If `--wait-for-plan` is retained after P1, its log line should state the exact
path, a one-line example of the smallest valid plan, and that the controller
will pick it up automatically on write. One clear line beats a silent block.

## Verification

```bash
just rust-test            # init preflight unit tests
just tl-loop-test         # status --watch rendering and degradation
just test                 # full gate
```

End to end, in a scratch project:

1. `exomonad init` with **no** `harness_policy.toml` → fails with the path and
   an example, creates no session.
2. Add the policy, `init` with no plan → fails naming `plan.json`, or with
   `--skip-preflight` starts and the Status window says "waiting for plan".
3. Add a one-leaf plan → TL window shows the controller running, Status window
   shows the slice moving `pending → spawned → in_review`.
4. Force a gate → Status window shows it pending; answering it via
   `tl_loop gate` updates the view within one interval.
5. `beast-workspace/workspace` → `init` names the two missing files rather than
   hanging.

## Out of scope

- Rendering status *inside* the controller's own output (see the design note).
- Any change to what the controller does. This is purely visibility: no new
  authority, no new mutation path, and the Status window stays read-only.
