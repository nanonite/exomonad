# Forgejo CI Migration

**Date:** 2026-05-21

**Chainlink:** #343 (parent) + #344–#351 (subissues)

## Context

The Tangled/Spindle CI integration is broken for local development. The Tangled knot requires signed AT Protocol events using a secp256k1 key tied to a real PLC directory — `localhost` cannot satisfy this. Extensive debugging confirmed this is a fundamental limitation, not a configuration issue. The `sh.tangled.git.refUpdate` event fires correctly on push (repo registration works), but `sh.tangled.pipeline` event creation fails with `did="" err="directory not found"` because the knot cannot sign the event.

**Replacement:** Forgejo (lightweight self-hosted Git forge) + Forgejo Actions (GitHub Actions-compatible CI via `act_runner`). Forgejo has a GitHub-compatible REST API, so the existing `gh` CLI usage in the codebase works with `GH_HOST` pointing at the local instance. The merge-gate logic is already abstracted over a `CiStatusMap` — only the subscriber feeding it changes.

## Scope

### Remove

| Component | File(s) |
|-----------|---------|
| `tangled_knot_url`, `tangled_spindle_url`, `tangled_appview_url`, `tangled_owner_did`, `tangled_knot_container`, `tangled_spindle_db` config fields | `rust/exomonad/src/config.rs` |
| `run_ci_subscriber()`, `run_knot_subscriber()`, `run_spindle_subscriber()`, `PipelineContext`, `TangledStreamEvent`, `CiSubscriberKind`, `with_knot_url()`, `with_spindle_url()` | `rust/exomonad-core/src/services/worktree_event_watcher.rs` |
| Spindle companion auto-spawn, `.with_knot_url()` / `.with_spindle_url()` watcher config | `rust/exomonad/src/serve.rs` |
| `tangled_pr.rs` service | `rust/exomonad-core/src/services/tangled_pr.rs` (delete) |
| Tangled module export | `rust/exomonad-core/src/services/mod.rs` |
| Nixery-based CI workflow template | `rust/exomonad/src/new.rs` |
| Knot docker-compose | `tangled-knot/` (delete) |
| Tangled references | `CLAUDE.md`, `docs/`, `.exo/config.toml` template |

### Add

| Component | File(s) |
|-----------|---------|
| `forgejo_url`, `forgejo_token`, `forgejo_webhook_secret` config fields | `rust/exomonad/src/config.rs` |
| Forgejo CI webhook handler — receives `workflow_run`/`check_run` events, feeds `ci_status_map` | `rust/exomonad-core/src/services/forgejo_ci.rs` (new) |
| `POST /ci` route | `rust/exomonad/src/serve.rs` |
| `GH_HOST` + `GH_TOKEN` injection into agent env | `rust/exomonad/src/init.rs` |
| Forgejo repo creation + webhook registration in `exomonad new` | `rust/exomonad/src/new.rs` |
| GitHub Actions YAML template (replaces Nixery) | `rust/exomonad/src/new.rs` |
| Forgejo + act_runner docker-compose | `forgejo/docker-compose.yml` (new) |

## CI Status Flow (After Migration)

```
git push → Forgejo → triggers .github/workflows/ci.yml → act_runner executes
                   → sends workflow_run webhook → POST /ci on exomonad server
                   → forgejo_webhook_handler → CIStatus::parse() → ci_status_map
                   → merge gate reads ci_status_map → [MERGE READY] fires
```

## Forgejo API Compatibility

Forgejo has a GitHub-compatible REST API. Configure `gh` CLI per agent:
```bash
GH_HOST=localhost:3000 GH_TOKEN=<forgejo_token>
```
All existing `gh pr create`, `gh pr merge`, `gh pr list` calls work unchanged.

## Migration Path for Consumer Repos (nemotron-port, etc.)

1. `docker compose -f forgejo/docker-compose.yml up -d`
2. Create user + token in Forgejo UI
3. Set `forgejo_url`, `forgejo_token` in `.exo/config.toml`
4. `exomonad new` → auto-creates Forgejo repo + registers webhook
5. `exomonad init --recreate` → agents get `GH_HOST`/`GH_TOKEN` in env
6. Push branches → CI runs → merge gate works

## Verification

1. `just build` — compiles without Tangled deps
2. `docker compose -f forgejo/docker-compose.yml up -d` — Forgejo at localhost:3000
3. `exomonad new` in test repo — creates Forgejo repo, registers `/ci` webhook
4. `git push` — triggers Forgejo Actions, act_runner runs `ci.yml`
5. Webhook received at `/ci` — `RUST_LOG=debug` shows ci_status_map update
6. `merge_pr` — merge gate passes when `CIStatus::Success` present
7. `gh --hostname localhost:3000 pr list` — gh CLI works against Forgejo
