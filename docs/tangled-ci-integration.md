# Tangled CI Integration — Architecture & Verification

> Session documentation for issue #104 (chainlink: Tangled CI → exomonad).
> Covers the full investigation, what was built, and how AT Protocol fits in.

---

## Overview

This documents the end-to-end CI pipeline that connects three systems:

```
git push
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Tangled Knot (git server)                                          │
│  localhost:5555  ·  did:web:localhost:5555                          │
│                                                                     │
│  - Stores bare git repos on disk                                    │
│  - On push: compiles .tangled/workflows/ci.yml → pipeline record    │
│  - Emits sh.tangled.pipeline events on /events WebSocket            │
└────────────────────────────┬────────────────────────────────────────┘
                             │ ws://localhost:5555/events
                             │ (sh.tangled.pipeline events)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Tangled Spindle (CI runner)                                        │
│  localhost:6555  ·  did:web:localhost                               │
│                                                                     │
│  - Subscribes to knot /events via eventconsumer                     │
│  - On pipeline event: pulls nixery image, creates Docker container  │
│  - Container clones repo via HTTP from knot                         │
│  - Runs CI steps inside container                                   │
│  - Emits sh.tangled.pipeline.status on /events WebSocket            │
└────────────────────────────┬────────────────────────────────────────┘
                             │ ws://localhost:6555/events
                             │ (sh.tangled.pipeline.status events)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ExoMonad (orchestration server)                                    │
│  .exo/server.sock  ·  worktree_event_watcher                        │
│                                                                     │
│  - Subscribes to spindle /events (run_spindle_subscriber)           │
│  - Maintains CIStatus per branch (Arc<RwLock<HashMap>>)             │
│  - Gates merge_pr on CIStatus::Success                              │
│  - Notifies TL agent via Teams inbox                                │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Deep-Dive

### The Knot (tangled-core/knotserver)

The knot is a git server with an identity layer. Every repo owner is an AT Protocol DID.

**Event emission flow (happy path):**
```
SSH push arrives
  │
  ├─ authorized_keys: keys-wrapper queries /keys internal API
  │   → for each registered DID: outputs "command=knot guard -user <did> ..." <pubkey>
  │
  ├─ knot guard: RBAC check via internal API (/guard)
  │   → verifies push permission for the DID
  │   → sets GIT_USER_DID env var
  │
  ├─ git-receive-pack runs, then post-receive hook fires
  │   → hook/40-notify.sh: calls `knot hook post-receive --user-did $GIT_USER_DID`
  │   → POSTs to internal API (/hooks/post-receive)
  │
  └─ InternalHandle.triggerPipeline:
      → reads .tangled/workflows/*.yml from the pushed commit
      → compiles into tangled.Pipeline record (triggerMetadata + workflows)
      → inserts into events table (rkey, nsid=sh.tangled.pipeline, event JSON)
      → notifies WebSocket broadcaster
```

**WebSocket event format** (from `/events`):
```json
{
  "rkey": "3ml764icfy722",
  "nsid": "sh.tangled.pipeline",
  "event": {
    "$type": "sh.tangled.pipeline",
    "triggerMetadata": {
      "kind": "push",
      "push": { "ref": "refs/heads/main", "oldSha": "...", "newSha": "..." },
      "repo": {
        "did": "did:plc:localdev",
        "knot": "localhost:5555",
        "repo": "ci-test"
      }
    },
    "workflows": [{
      "engine": "nixery",
      "name": "ci.yml",
      "raw": "<full yaml content>",
      "clone": { "depth": 1, "skip": false, "submodules": false }
    }]
  },
  "created": 1778086477
}
```

**HTTP clone routing** (`router.go`):
```
GET /{did}/{name}/info/refs?service=git-upload-pack
       │
       ├─ middleware: resolveDidRedirect
       │   → if {did} starts with "did:" → pass through
       │   → otherwise: resolve handle → redirect to /did:.../name
       │
       └─ resolveRepoPath:
           1. GetRepoDid(did, name) from repo_keys table → if found: ResolveRepoDIDOnDisk
           2. Legacy fallback: securejoin(scanPath, did+"/"+name)
              → serves the directory directly (follows relative symlinks)
```

> **Key discovery**: `securejoin` re-roots absolute symlink targets under `scanPath`.
> Symlinks must be **relative** (e.g., `../owner/ci-test.git`) not absolute paths.

---

### The Spindle (tangled-core/spindle)

The spindle is a CI runner. It subscribes to known knots and executes `.tangled/workflows/ci.yml`.

**Event consumer loop:**
```
spindle startup
  │
  ├─ reads repos table: SELECT knot FROM repos
  │   → for each knot: eventconsumer.AddSource(KnotSource{knot})
  │
  └─ KnotSource.Url(cursor, dev=true):
      → ws://localhost:5555/events?cursor=<last_seen>
      → backfills from cursor=0 on first start
      → cursor stored in SQLite cursors table (persists across restarts)

on sh.tangled.pipeline event received:
  │
  ├─ processPipeline checks:
  │   1. src.Key() == event.triggerMetadata.repo.knot  (knot identity match)
  │   2. GetRepo(knot, did, repoName)                  (repo must be in repos table)
  │
  ├─ nixery engine: InitWorkflow
  │   → parses raw YAML: steps, dependencies, environment
  │   → builds image URL: nixery.tangled.sh/<dep1>/<dep2>/.../bash/git/coreutils/nix
  │   → prepends: Clone step (git init + fetch from knot HTTP) + nix config step
  │
  ├─ Docker: pull image → create network → create container → start
  │
  ├─ container runs steps sequentially, logs to /tmp/spindle-logs/<wid>.log
  │
  └─ spindle emits sh.tangled.pipeline.status events on its own /events WS:
      pending → running → success | failed
```

**Clone URL construction** (`models/clone.go`):
```go
// dev mode: localhost → host.docker.internal for Docker networking
host = "host.docker.internal:5555"
url  = "http://host.docker.internal:5555/{repo.Did}/{*repo.Repo}"
    = "http://host.docker.internal:5555/did:plc:localdev/ci-test"
```

**PipelineStatus event format** (on spindle's `/events`):
```json
{
  "rkey": "3ml765m5dmk22",
  "nsid": "sh.tangled.pipeline.status",
  "event": {
    "pipeline": "at://did:web:localhost:5555/sh.tangled.pipeline/ci-test-1778086477",
    "status": "success",
    "workflow": "ci.yml",
    "createdAt": "2026-05-06T11:55:15-05:00"
  },
  "created": 1778086515418705860
}
```

---

### ExoMonad (worktree_event_watcher)

Two background subscribers in `run_ci_subscriber`:

```
run_knot_subscriber(knot_url, pipeline_map)
  │
  └─ ws://localhost:5555/events
      → on sh.tangled.pipeline: rkey → branch_name (from triggerMetadata.push.ref)
      → stores in pipeline_map: HashMap<rkey, BranchName>

run_spindle_subscriber(spindle_url, pipeline_map, ci_status_map)
  │
  └─ ws://localhost:6555/events
      → on sh.tangled.pipeline.status:
          rkey → lookup branch in pipeline_map
          → update ci_status_map: HashMap<BranchName, CIStatus>

WorktreeEventWatcher.poll()
  └─ for each open PR:
      → reads ci_status_map[pr.head_branch]
      → merge_pr gates: CIStatus::Success required
```

**Config** (`.exo/config.toml`):
```toml
tangled_knot_url    = "ws://localhost:5555"  # knot subscriber: pipeline→branch mapping
tangled_spindle_url = "ws://localhost:6555"  # spindle subscriber: CI status updates
```

---

## AT Protocol Connection

Tangled uses AT Protocol identity (DIDs) throughout. Here's how it maps:

```
AT Protocol concept          Tangled realization
─────────────────────────────────────────────────────────────────────
DID                          Knot owner:  did:plc:localdev
                             Knot server: did:web:localhost:5555
                             Spindle:     did:web:localhost

PDS (Personal Data Server)   → The knot stores repo records (sh.tangled.repo)
                               as AT-URI addressable data

Lexicon (NSID)               sh.tangled.pipeline         — CI trigger record
                             sh.tangled.pipeline.status  — CI result record
                             sh.tangled.publicKey        — SSH key record
                             sh.tangled.repo             — Repo registration record
                             sh.tangled.spindle.member   — Spindle membership

AT-URI                       at://did:web:localhost:5555/sh.tangled.pipeline/rkey
                             → identifies a specific pipeline run

Jetstream                    The spindle subscribes to the knot's /events WS
                             (behaves like a local ATProto jetstream for repo events)
                             Spindle also watches the global ATProto jetstream
                             for SpindleMember / Repo / RepoCollaborator records

Service Auth                 did:web URLs used for service-to-service auth
                             (disabled in dev mode: SPINDLE_SERVER_DEV=true)
```

**AT Protocol data flow for membership** (production):
```
User creates sh.tangled.spindle.member record on their PDS
  → ATProto jetstream delivers to spindle
  → spindle.ingest(): adds the knot to eventconsumer sources
  → spindle now subscribes to that knot's /events

User creates sh.tangled.repo record on their PDS
  → jetstream delivers to both appview and knot
  → knot registers repo in repo_keys table
  → spindle learns about repo via repos table
```

**Local dev bypass** (what setup-dev.sh does instead):
```
setup-dev.sh (manual equivalent of AT Protocol membership flow)
  ├─ Creates test repo in /tmp/exomonad-ci-test
  ├─ Pushes to knot via SSH (git@local-tangled:repositories/owner/ci-test.git)
  ├─ Creates did:plc:localdev/ci-test symlink (HTTP clone path)
  ├─ Seeds spindle.db repos table (bypasses jetstream membership)
  └─ Injects sh.tangled.pipeline event directly into knot events table
     (bypasses the guard/hook/RBAC path — used because the repo is at
      a legacy "owner/" path, not a DID path, so the hook can't fire)
```

---

## Local Dev Setup

### Prerequisites

- Docker running (`tangled-knot-knot-1` container up)
- Spindle binary installed by `just install-all-dev` or `just spindle-dev`
- SSH key registered in container's `authorized_keys`

### What setup-dev.sh does

```
/tmp/exomonad-ci-test/
├─ .tangled/workflows/ci.yml    ← trivial workflow (python3 only)
├─ src/hello.py
└─ src/test_hello.py

Knot container (tangled-knot-knot-1):
├─ /home/git/repositories/owner/ci-test.git    ← bare repo (pushed via SSH)
└─ /home/git/repositories/did:plc:localdev/
   └─ ci-test → ../owner/ci-test.git            ← relative symlink (securejoin-safe)

spindle.db:
└─ repos: (localhost:5555, did:plc:localdev, ci-test)

tangled-knot/server/knotserver.db:
├─ public_keys: (did:plc:localdev, ssh-ed25519 AAAA...)
└─ events: (rkey=ci-test-..., nsid=sh.tangled.pipeline, ...)
```

### Running

```bash
# One-time setup — creates test repo, pushes, seeds DBs, injects event
bash tangled-knot/setup-dev.sh

# Start spindle (picks up injected event on first connect)
./tangled-knot/start-spindle.sh
```

### Verified end-to-end (session result)

```
11:54:37  spindle connects → ws://localhost:5555/events
11:54:37  pipeline enqueued: ci-test-1778086477
11:54:37  pulling image: nixery.tangled.sh/python3/bash/git/coreutils/nix
11:55:14  Status: Downloaded newer image ✓
11:55:14  creating container, starting container
11:55:14  Clone repository into workspace
          → git fetch http://host.docker.internal:5555/did:plc:localdev/ci-test
          → HEAD is now at 155ef5b test: minimal CI test workspace ✓
11:55:14  Run tests
          → python3 src/test_hello.py
          → all tests passed ✓
11:55:25  all workflows completed
          spindle events DB: status=success ✓
```

---

## CI Workflow Format (.tangled/workflows/ci.yml)

### Test workflow (minimal — used for integration testing)

```yaml
engine: nixery
when:
  - event: [push, manual]
    branch: [main]
clone:
  depth: 1
  submodules: false
dependencies:
  nixpkgs:
    - python3
steps:
  - name: "Run tests"
    command: "python3 src/test_hello.py"
```

### Production workflow (exomonad repo)

```yaml
engine: nixery
when:
  - event: [pull_request]
    branch: [main]
  - event: [push, manual]
    branch: [main]
clone:
  depth: 1
  submodules: false
dependencies:
  nixpkgs:
    - cabal-install
    - haskell.compiler.ghc912   # nested nixpkgs attr, not top-level
    - hlint
    - ormolu
    - rustup
    - protobuf                  # protoc in PATH — no PROTOC env var needed
    - pkg-config
    - zlib
steps:
  - name: "Haskell format check"
    command: |
      ormolu --mode check --ghc-opt -XImportQualifiedPost \
        $(find haskell -name "*.hs" -not -path "*/vendor/*")
  - name: "Haskell lint"
    command: 'hlint haskell --ignore-glob="haskell/vendor/**" || true'
  - name: "Haskell build"
    command: "cabal update && cabal build all -j"
  # ... (Rust steps omitted for brevity)
```

**Key nixery packaging rules:**
- Nested nixpkgs attributes use dot notation: `haskell.compiler.ghc912` not `ghc912`
- `protobuf` puts `protoc` in PATH — no `PROTOC=/run/current-system/sw/bin/protoc` (NixOS-specific)
- Don't use `nix develop` inside CI steps — the workspace flake has workspace-specific deps (WASM toolchain, HLS, etc.) not needed for CI
- `just` is not needed — inline the commands directly

---

## Known Limitations & Follow-ups

### Real git push → CI (not yet wired)

The current setup injects events directly because the test repo lives at `owner/ci-test.git` (legacy path). The knot's `triggerPipeline` needs the repo registered in `repo_keys`, but the knot's `resolveAtIdentifier` can't resolve the literal string `"owner"` as a DID.

**To wire real git push → CI**, the repo needs to be created through the knot's XRPC:
```
POST /xrpc/sh.tangled.knot.createRepo  { "name": "ci-test" }
  → creates repo_keys entry
  → creates repo on disk at did:<repodid>/
  → installs post-receive hook
  → sets RBAC push permission
```
This requires authenticated XRPC (service auth or dev mode equivalent).

### WAL isolation

The knotserver.db is held open by the live container. Writes from the host `sqlite3` process go to the WAL but aren't immediately visible to the container's Go connection. This is why `setup-dev.sh` uses the direct event injection approach rather than trying to seed `repo_keys` via the host.

### exomonad serve integration

The `tangled_spindle_url` and `tangled_knot_url` are now in `.exo/config.toml`. When `exomonad serve` starts, `WorktreeEventWatcher` launches `run_ci_subscriber` which connects to both WebSockets. The CI merge gate in `merge_pr_local.rs` checks `ci_status_map` before allowing merges.

---

## File Map

| File | Purpose |
|------|---------|
| `tangled-knot/setup-dev.sh` | One-shot dev setup: creates test repo, seeds DBs, injects pipeline event |
| `tangled-knot/start-spindle.sh` | Starts spindle with correct env vars |
| `.exo/config.toml` | Added `tangled_knot_url`, `tangled_spindle_url` |
| `.tangled/workflows/ci.yml` | Fixed nixery package attrs (`haskell.compiler.ghc912`, removed `just`) |
| `rust/exomonad-core/src/services/worktree_event_watcher.rs` | Spindle/knot subscriber already implemented (no changes needed) |
| `TANGLED_CI_REVIEWER_PLAN.md` | Architecture plan (pre-session) |
| `tangled-core/cmd/spindle/spindle` | Pre-compiled spindle binary |
| `tangled-core/spindle/models/clone.go` | Clone URL builder (localhost→host.docker.internal in dev) |
| `tangled-core/knotserver/router.go` | Knot HTTP routes + resolveDidRedirect middleware |
