# Migrating an existing ExoMonad project

The TL-as-loop epic replaced the interactive tech-lead session with the
`tl_loop` controller. For an existing project this is a smaller migration than
it sounds: almost nothing on disk needs converting.

Two things actually move:

1. **Forgejo credentials.** Carry them forward verbatim. They are the one piece
   of old project state that is genuinely irreplaceable, and a project without
   working CI status can never reach `merge_ready`.
2. **Historical logs.** Import them into the local analysis store so the old
   project's history shows up under the same Failure Atlas mechanism as new
   sessions. This is for visibility, not for orchestration — the controller
   never reads them.

Everything else — worktrees, in-flight agents, the old TL prompt — is not
migrated. Land or abandon in-flight PRs first, then start the new controller
from a clean plan.

---

## 1. Carry the Forgejo credentials forward

These live in `.exo/config.toml` and are the only values you must not lose:

```toml
forgejo_url = "http://localhost:3000"
forgejo_token = "forgejo_pat"
forgejo_webhook_secret = "shared-secret"
```

If the old project pinned a git remote — common when a GitHub `origin` sits
alongside a local Forgejo remote — carry that too. It is stored in git config,
not `.exo/`:

```bash
git config --local --get exomonad.remote        # in the old checkout
exomonad init --set-git-remote <name>           # in the migrated one
```

Git worktrees share the main repo's `.git/config`, so this applies to every
spawned agent automatically.

Verify before going further. CI status is what gates every merge:

```bash
curl -s -H "Authorization: token $FORGEJO_TOKEN" "$FORGEJO_URL/api/v1/version"
```

### Config keys that no longer do what they did

These remain accepted for compatibility but no longer control the coordinator.
Leaving them in place is harmless; relying on them is not.

| Key | Status |
|-----|--------|
| `root_agent_type` | Ignored. `init` always starts the Python controller |
| `model` | Legacy root-model compatibility; ignored by the controller |
| `tl_effort_level` | Ignored |
| `spawn_agent_type` | **Still live.** Default harness for workers, leaves, companions |
| `worker_effort_level` | **Still live.** Inherited by leaves, sub-TLs, companions |
| `agent_type = "gemini"` | **Fails closed.** Use `codex` (model `gpt-luna`) |

### New files the project now requires

`.exo/harness_policy.toml` did not exist before and is now a **startup error**
if missing or invalid. So is a missing `.exo/tl-loop/plan.json` (unless you
start with `--wait-for-plan`). See
[Programming the TL](programming-the-tl.md) for both schemas.

`.exo/review-policy.toml` is optional and unchanged in format — if the old
project had one, it carries forward as-is.

### Ordered sub-TL plans

Existing list-ordered plans do not need a rewrite. A direct sibling without an
`order` field is normalized to numeric order 1, preserving the legacy behavior.
When adopting ordered stages, add an explicit positive order to every direct
sibling, use contiguous values starting at 1, and do not mix explicit and
missing values. Preflight rejects a mixed or non-contiguous sibling set before
the controller starts.

For example, this is rejected because `legacy` omits `order` while `new`
declares one:

```json
{
  "plan": {
    "sub_tls": [
      {"name": "legacy", "plan": {}},
      {"name": "new", "order": 1, "plan": {}}
    ]
  }
}
```

Either omit `order` from every direct sibling to retain one legacy order-1
stage, or write `order: 1` on every sibling in that stage. Do not use an
explicit order to express a dependency between leaves; put the dependent work
in a later direct sub-TL stage. The order namespace resets inside each nested
sub-TL plan.

Same-order children are dispatched concurrently within the configured bound;
their aggregate PRs are then integrated one at a time in stable sub-TL ID
order. The next numeric order waits for all children and integrations in the
current order. A child failure blocks higher orders rather than silently
skipping the failed stage. Nested plans apply the same rules independently at
each recursive boundary.

Review evidence remains bound to the child head and patch. Integration evidence
is bound to the current base, integrated tree, head, and CI. If the base moves,
the diagnosis is `NEEDS_BASE_REVALIDATION`: let the controller refresh the
integration evidence. It is not a reason to spawn a worker or rebase a PR. A
real merge conflict is `INTEGRATION_CONFLICT` and routes repair through
same-owner `resume_pr`, preserving the existing branch, worktree, and PR.

After migration, verify normalization and inspect recovery state with:

~~~bash
python3 ~/.exo/tl_loop.pyz preflight --project-root .
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root
~~~

The status read model shows `current_order`, `ordered_stages`, integration
evidence, and `next_transition`. During a restart, use that transition and the
persisted checkpoint; do not manually mark a stage merged or create a duplicate
aggregate PR.

---

## 2. Import the old logs for Failure Atlas visibility

`exomonad logs import` reads historical log sources and normalizes them into
the local analysis database at `.exo/analysis/atlas.db`. New sessions write
structured telemetry automatically; import is how an old project's history
joins the same view.

The import is explicit, idempotent, and **read-only with respect to the source
files** — nothing is rewritten in place, and normalized records are never
appended back into legacy `.exo/logs/*.jsonl`.

### Where the old logs are

| Path | What it holds |
|------|---------------|
| `.exo/ledger/segments/*.jsonl` | The canonical append-only ledger |
| `.exo/events/` | Per-agent JSONL views (compatibility input) |
| `.exo/logs/` | Per-agent JSONL views (compatibility input) |
| `.exo/sink-health.json` | Accepted/rejected counts, last successful sequence |
| `.exo/run_id`, `.exo/session.json` | Swarm continuity and session boundary |

### Dry run first

```bash
exomonad logs import \
  --source /path/to/old-project/.exo/ledger/segments \
  --source /path/to/old-project/.exo/logs \
  --format auto \
  --dry-run
```

`--dry-run` inspects the sources and reports counts without writing
`atlas.db`. `--source` is a file or a directory and may be repeated.
`--format` accepts `auto`, `jsonl`, `json`, `sqlite`, or `text`; leave it on
`auto` unless a source is misdetected.

### Then import for real

```bash
exomonad logs import \
  --source /path/to/old-project/.exo/ledger/segments \
  --source /path/to/old-project/.exo/logs \
  --format auto
```

Re-running is safe. Each source is fingerprinted by content hash and parser
version; unchanged sources are skipped rather than duplicated. Use `--rebuild`
only when you want every derived row recomputed from the selected sources —
it clears the store first.

### What you get

`atlas.db` is a local, rebuildable SQLite store: `sources`, `segments`,
`events`, `supersessions`, `sessions`, and a `resolved_events` view that
excludes superseded observations while retaining every correction in the raw
evidence. Events carry identity (`session_id`, `run_id`, `agent_id`,
`parent_agent_id`, `invocation_id`), harness axes (`provider`, `runtime`,
`harness`), and outcome fields (`outcome`, `duration_ms`, `attempt`,
`issue_number`, `pr_number`, `head_sha_hash`).

Historical rows from the retired harness stay compilable but aggregate as
`other` under `provider`, `runtime`, and `harness`, effective 2026-08-10.
Existing ledger segments and atlas databases are unchanged by that.

### The share boundary

L1–L3 evidence is local and may be sensitive. **L4 compile is the only
shareable output**, and it emits allowlisted dimensions — never raw payloads,
transcripts, reasoning, paths, secrets, or stable source identifiers.

```bash
exomonad logs export --mode aggregate --output .exo/analysis/export
```

Do not pool raw `atlas.db` files across machines. Share the aggregate artifact
and its manifest.

### Retention

```bash
exomonad logs drop-segments --older-than-seconds 2592000 --dry-run
```

Retention is whole-segment only. `--dry-run` reports fingerprints without
deleting.

---

## 3. Stand up the controller

```bash
cd your-project

# 1. Author the required policy (no default is synthesized)
$EDITOR .exo/harness_policy.toml

# 2. Author the plan (no natural-language TL prompt is accepted)
$EDITOR .exo/tl-loop/plan.json

# 3. Rebuild the session against the new binary and WASM
exomonad init --recreate
```

`--recreate` tears down and rebuilds the tmux session. Companion worktrees under
`.exo/companions/` persist across it — only the session is torn down.

The TL window now runs `python3 ~/.exo/tl_loop.pyz`, not a harness session. Do not type
`claude` into it. Init applies a bounded five-second startup liveness gate to
the controller; this confirms that its process appears and survives startup,
but is not a semantic ready signal or heartbeat protocol. Later hangs and
failures remain visible through the Watcher, `status`, and the durable
`controller-exit.json` record.

---

## 4. Verify the migration

```bash
# Contracts still validate
just validate-observability-contracts

# Controller loaded the plan and reached a real phase
python3 ~/.exo/tl_loop.pyz status --project-root . --run-id root

# Documentation examples and relative links remain valid
just docs-check

# Historical logs landed
sqlite3 .exo/analysis/atlas.db "SELECT count(*), min(event_time), max(event_time) FROM events;"
```

A healthy `status` prints a phase, the slice map with statuses, and
`last_consumed_offset`. Any `pending` gate is printed with the exact command to
answer it.

For ordered runs, also inspect `current_order`, each `ordered_stages[*].sub_tls`
row, the per-candidate owner/head/base evidence under the ordered-stage rows,
and `next_transition`. `NEEDS_BASE_REVALIDATION` means the reviewed head is
still the same and only the parent base moved. `INTEGRATION_CONFLICT` means the
aggregate PR needs same-owner `resume_pr`; neither state calls for a worker or
an unnecessary rebase.

---

## What is deliberately not migrated

| Thing | Why |
|-------|-----|
| In-flight worktrees and agents | The triad is born and torn down together. Land or abandon the PRs, then re-plan |
| The old TL prompt | `.exo/roles/devswarm/context/tl.md` survives as decompose-prompt *vocabulary*. It is not an agent protocol and nothing executes it |
| The TL coordination stop hook | Removed. Terminal decisions are the controller's phase predicates in `tl_loop/fsm/terminal.py`. Worker and reviewer lifecycle hooks are untouched |
| The merge-reviewer teammate plan | Subsumed — the controller is the merge queue |
| Old run state | There was no durable controller checkpoint to convert. `.exo/tl-loop/<run_id>/run.json` starts fresh |

## Anti-patterns

- **Do not** let `exomonad init` scan and rewrite old logs implicitly. Import is
  explicit, resumable, and idempotent by design.
- **Do not** append normalized records back into `.exo/logs/*.jsonl`. Preserve
  the original stream.
- **Do not** treat a file count as a session count. A human session, a swarm
  run, an agent invocation, and a provider transcript are four different things
  and the schema distinguishes them.
- **Do not** copy an old `config.toml` wholesale and assume the TL keys still
  apply. Check the table above.
- **Do not** start the controller without the four required files: `config.toml`,
  `harness_policy.toml`, `review-policy.toml`, and `harness_capability.toml`.
  Preflight fails closed, on purpose.

Before starting a migrated project, add `.exo/harness_capability.toml` with a `[capabilities]` entry for every harness allowed by `.exo/harness_policy.toml`. `exomonad init` backfills the canonical map for the standard policy entries and otherwise fails naming the file rather than widening the allowlist.

## See also

- [Programming the TL](programming-the-tl.md) — the four required controller files and their schemas
- [`docs/decisions/tl-as-loop.md`](../decisions/tl-as-loop.md) — why the boundary moved
- [`docs/observability/README.md`](../observability/README.md) — contracts and layers
- [`docs/exomonad-failure-atlas-sync-plan.md`](../exomonad-failure-atlas-sync-plan.md) — the import/export design constraints
