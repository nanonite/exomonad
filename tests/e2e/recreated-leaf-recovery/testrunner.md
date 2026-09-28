# Test plan: recreated-leaf recovery acceptance (#1111)

This acceptance is **non-interactive**. It runs the shipped `exomonad serve`
binary against a disposable project, drives the shipped controller tool surface
itself, and prints one `PASS`/`FAIL` line per T-item. There is no testrunner
companion and nothing to observe by hand; `run.sh` is the whole entry point and
its exit status is the verdict.

## What makes a run safe

| Concern | How it is handled |
|---------|-------------------|
| The operator's Forgejo | Never contacted. The run brings up its own, on its own compose project, on an ephemeral port. |
| The operator's Chainlink database | Never read or written. The run creates its own inside its own temporary directory and points `CHAINLINK_DB` at it. |
| The operator's tmux sessions | Never touched. Every session is named `exo-e2e-1111-<run id>-*`, and teardown kills only names with this run's prefix. |
| The operator's installed binary and WASM | Never replaced. The run invokes `target/debug/exomonad` and `.exo/wasm/wasm-guest-devswarm.wasm` from this worktree by absolute path. |
| The operator's checkout | The project is a fresh clone inside the run's `mktemp -d` directory. Nothing outside it is written. |

## The nine items

| Item | What it proves | Evidence it prints |
|------|----------------|--------------------|
| T1 | The ordered child is provisioned by the server's own route, and the leaf holds a unique commit with exactly one open pull request | child branch and worktree, leaf branch, leaf head, pull request number and state |
| T2 | Recreating the session preserves the leaf branch: head, worktree set, and identity are identical across it | session name, preserved head, worktrees |
| T3 | Starting the same plan reattaches to the preserved branch, in both shapes: a live worktree is reused, and a lost worktree is reattached by a recorded `attach` decision | the attach action, `branch_exists`, the reattached worktree's head |
| T4 | The head is unchanged, the cwd is a registered git worktree, and the identity's branch is the declared one | head, cwd, branch |
| T5 | Exactly one identity, one worktree, one branch, one open pull request, and exactly one spawn per plan start | the counts |
| T6 | A second recreate changes nothing | recreates, head, worktrees, worktree creations |
| T7 | Five fail-closed shapes, each with its machine code, plus a resume that fails closed without forking the leaf | the code and the message per shape |
| T8 | A sink write creates no planned directory, no worktree, and no identity for an agent that owns none | the planned path and the three "not created" facts |
| T9 | A retryable refusal created nothing and the re-drive produced exactly one spawn; a terminal conflict is never re-driven and creates nothing | the code, the spawn count, the attach completion count |

## Reading a run

```
PASS T1 {...}
...
PASS recreated-leaf-recovery: 9/9 items passed, 0 leaks, 0 cleanup problems
```

The final line is the verdict. `0 leaks` means that after teardown nothing the
run created was still visible: no session with its prefix, no process running
in its directory, no compose project or volume with its name. A non-zero leak
count fails the run even when every T-item passed.

## When an item fails

The run stops at the first failing item and still tears everything down. The
`FAIL` line names the exact assertion and the state it saw. Re-run with
`--keep` to leave the forge, the session, and the directory in place, and clean
up afterwards with:

```bash
docker compose -p <the compose project the run printed> \
  -f tests/e2e/lib/forgejo/docker-compose.yml down -v --remove-orphans
tmux kill-session -t <the session the run printed>
rm -rf <the run directory the run printed>
```
