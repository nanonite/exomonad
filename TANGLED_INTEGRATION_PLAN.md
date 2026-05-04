# Tangled CI Integration Plan

## Context

Parts A (chainlink `--db`) and B (local PR workflow) are substantially implemented:
- `file_pr_local.rs`, `merge_pr_local.rs`, `worktree_event_watcher.rs` all exist
- `ReviewerRole.hs` and `complexity_classifier.rs` exist
- `.tangled/workflows/ci.yml` already exists with the full Haskell+Rust+proto pipeline
- GitHub Actions `ci.yml` and `copilot-review.yml` are already deleted
- `.exo/review-policy.toml` exists but is **empty** — needs content
- `.exo/prs.json` is **missing** — initial empty registry needed
- `query_local_ci()` is **not implemented** in `worktree_event_watcher.rs`

Tangled research confirms:
- Spindle is production-ready; Docker is the simplest knot path
- CI is event-driven via AT Protocol Jetstream — push triggers pipeline automatically
- XRPC namespace for pipeline status: `sh.tangled.pipeline.getStatus`
- **Local Tangled knots do NOT validate DIDs externally** — placeholder `did:plc:localdev` works for local dev; upgrade to real DID by editing `docker-compose.yml` and restarting
- No native bot review in Tangled — the `ReviewerRole.hs` sibling-agent is the review layer

### Architecture: exomonad binary vs. workspace

The **exomonad binary is built once** from the exomonad repo. Tangled integration work is:
1. **Machine-level infra** — knot+spindle Docker setup
2. **User-level config** — DID, SSH entry, git remote (not committed to any repo)
3. **Exomonad code** — `query_local_ci()` so all workspaces can read Tangled CI status
4. **Workspace config** — `.exo/config.toml` `tangled_knot_url` field per project

A **workspace** is any project directory running `exomonad init`. That workspace's git remote points to the local knot; `exomonad init` wires the rest.

---

## DID Identity Discipline

| Layer | What | Value |
|-------|------|-------|
| **Knot owner DID** | Who owns the local Tangled knot | Placeholder `did:plc:localdev` locally; real DID when registered at tangled.org |
| **Worker git identity** | `user.name`/`user.email` in leaf worktrees | `exomonad-leaf-{slug}@exomonad.local` — set by `spawn_leaf_subtree()` at spawn time |
| **Reviewer git identity** | `user.name`/`user.email` in reviewer worktrees | `exomonad-reviewer-{slug}@exomonad.local` — set by `spawn_reviewer_subtree()` |

Author identity ≠ reviewer identity. `merge_pr` verifies this at merge time. `user.useConfigOnly = true` prevents ambient auth fallthrough (mergepath #157 pattern).

DID upgrade path: replace `KNOT_SERVER_OWNER` in `docker-compose.yml` + restart knot. No code changes.

---

## Phase C.1 — Machine-Level Knot + Spindle Setup

### C.1.1 — Stand up knot via Docker

```bash
git clone https://tangled.org/tangled.org/knot-docker ~/src/tangled-knot
cd ~/src/tangled-knot
# Edit docker-compose.yml:
#   KNOT_SERVER_OWNER=did:plc:localdev
#   KNOT_SERVER_HOSTNAME=localhost
#   KNOT_SERVER_PORT=<pick available port, e.g. 7000>
docker compose up -d
curl http://localhost:<knot-api-port>/health   # → 200
ssh -p <knot-ssh-port> git@localhost           # → Tangled banner
```

### C.1.2 — Build and run spindle

```bash
git clone https://tangled.org/tangled.org/core ~/src/tangled-core
cd ~/src/tangled-core
go mod download
go build -o spindle cmd/spindle/main.go
export SPINDLE_SERVER_HOSTNAME="localhost"
export SPINDLE_SERVER_OWNER="did:plc:localdev"
./spindle   # connects to local knot Jetstream automatically
```

### C.1.3 — SSH config (machine-level, not committed to any repo)

Append to `~/.ssh/config`:
```
Host local-tangled
  Hostname localhost
  Port <knot-ssh-port>
  User git
  IdentityFile ~/.ssh/<your-tangled-key>
```

**Done when:** Health endpoint responds; spindle shows Jetstream subscription; `ssh git@local-tangled` returns Tangled banner.

---

## Phase C.2 — Test Workspace Setup

Verify end-to-end push flow using a throw-away workspace (not the exomonad repo):

```bash
mkdir ~/tangled-test-workspace && cd ~/tangled-test-workspace
git init
git remote add origin git@local-tangled:localdev/test-workspace
exomonad new
# In .exo/config.toml add: tangled_knot_url = "http://localhost:<knot-api-port>"
exomonad init
git push origin main   # spindle should pick this up via Jetstream
```

**Done when:** Push lands on local knot; spindle logs show push event received.

---

## Phase C.3 — Verify ci.yml Schema and Iterate to Green

`.tangled/workflows/ci.yml` already exists. Cross-check against Tangled's `workflow/def.go`:
- Engine: `nixery`
- `when.event`: values from `[push, pull_request, manual, tag]`
- `dependencies.nixpkgs`: flat list (not dict)
- Secrets: plain env vars (not `${{ secrets.X }}`)

Push exomonad to local knot and iterate until green:
```bash
cd /home/goya/agent-workspace/exomonad
git remote add local-knot git@local-tangled:localdev/exomonad
git push local-knot main
# Watch spindle logs; fix failures
```

Known fragility points:
- **GHC version**: Pin `ghc912` in `dependencies.nixpkgs` if Nixery version mismatches `cabal.project`
- **hlint install**: Change `/usr/local/bin/hlint` to `$HOME/.local/bin/hlint` if container perms block it
- **WASM toolchain**: `wasm-all` excluded from first pipeline (requires IOG Nix cache) — leave it out for now

**File:** [.tangled/workflows/ci.yml](.tangled/workflows/ci.yml) — schema fixes only if validation shows gaps.

**Done when:** Spindle runs pipeline to green at least once.

---

## Phase C.4 — Implement `query_local_ci()` in exomonad

**File:** `rust/exomonad-core/src/services/worktree_event_watcher.rs`

Replace the placeholder `query_local_ci()` call with a real XRPC HTTP call to the local knot:

```rust
async fn query_local_ci(branch: &str, knot_url: &str) -> Result<CiStatus> {
    let url = format!(
        "{}/xrpc/sh.tangled.pipeline.getStatus?ref={}",
        knot_url, branch
    );
    let resp = reqwest::get(&url).await?.json::<serde_json::Value>().await?;
    match resp["status"].as_str() {
        Some("success") => Ok(CiStatus::Success),
        Some("failure") => Ok(CiStatus::Failure),
        Some("running") => Ok(CiStatus::Running),
        _ => Ok(CiStatus::Pending),
    }
}
```

Add `tangled_knot_url: Option<String>` to the exomonad config struct. CI status querying is a no-op when `None` (backward compatible — workspaces without Tangled still work).

**Done when:** `cargo test -p exomonad-core worktree_event_watcher` passes; CI status populated in `WatchState` when `tangled_knot_url` is set.

---

## Phase C.5 — Populate Config Files

### `.exo/review-policy.toml` (currently empty)

```toml
min_review_rounds = 1
reviewer_max_rounds = 2
reviewer_max_wait_seconds = 1200
reviewer_max_rate_limit_retries = 2
review_freshness_window_secs = 1200
external_review_threshold = 300
external_review_paths = ["proto/**", "rust/exomonad-core/src/handlers/**"]
require_second_reviewer_complexity = false
complexity_line_threshold = 500

[review_routing]
second_reviewer_timeout_seconds = 1800
```

### `.exo/prs.json` (missing — create)

```json
{
  "prs": {},
  "next_number": 1
}
```

Add creation of `prs.json` to `exomonad new` template so new workspaces get it automatically.

---

## Phase C.6 — Doc Updates

Update `CLAUDE.md` and `.claude/rules/exomonad.md` to reference `tangled_knot_url` config field and document the DID upgrade path. Verify:

```bash
grep -ri "copilot" CLAUDE.md .claude/rules/exomonad.md   # → 0 results
```

`build-container.yml` stays (Docker CI container build — separate purpose from CI pipelines).

---

## Execution Order

```
C.1 (knot+spindle on machine)
  → C.2 (test workspace push)
  → C.3 (green pipeline)
  → C.4 (query_local_ci code) + C.5 (config files) [parallel]
  → C.6 (docs)
```

C.4 can be unit-tested without a live knot (mock HTTP); integration-tested after C.1.

---

## Verification

```bash
curl http://localhost:<knot-port>/health               # 200 OK
ssh git@local-tangled                                  # Tangled banner
git push local-knot main                               # spindle shows push + pipeline runs
cargo test -p exomonad-core worktree_event_watcher     # CI status tests pass
python3 -c "import tomllib; tomllib.load(open('.exo/review-policy.toml','rb'))"
cat .exo/prs.json | jq .
grep -ri "copilot" CLAUDE.md .claude/rules/exomonad.md # 0 results
```
