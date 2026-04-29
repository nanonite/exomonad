# Plan: Local Tangled CI Migration (Investigation-First)

## Context

You want to move exomonad's CI off GitHub Actions and onto self-hosted Tangled (knot + spindle), running on your local machine. The eventual goal is to **replace the entire GH Actions workflow** — both [`.github/workflows/ci.yml`](.github/workflows/ci.yml) (build/lint/test) and [`.github/workflows/copilot-review.yml`](.github/workflows/copilot-review.yml) (PR review for the TL→leaf→Copilot convergence loop).

Per your direction, this plan is **local-first**: start with a strictly-local knot, defer the reachability decision (tunnel vs localhost) until we know whether the rest of the workflow holds up. The Copilot-review path is the riskiest piece — exomonad's leaf agents depend on automatic PR review to surface `[FIXES PUSHED]` / `[PR READY]` to the parent TL — so this plan also scopes an explicit investigation step before that workflow is dismantled.

The user's input on the proposal had several factual errors against actual upstream sources; corrections are noted inline. The plan reflects what the source code actually does.

---

## Proposal corrections (verified against upstream)

| Proposal claim | Reality |
|---|---|
| `nix run --impure 'github:tangled.org/core#vm'` | Tangled hosts itself, not on GitHub. Correct flake URL: `git+https://tangled.org/tangled.org/core` (note the doubled path). The `vm` app exists ([flake.nix](https://tangled.org/tangled.org/core/blob/master/flake.nix)). |
| Run `nix run #vm` from inside exomonad | The `vm` script uses `jj root \|\| git rev-parse --show-toplevel` to locate `nix/vm-data/`. It must be run from a **tangled-core checkout**, not exomonad — otherwise it litters exomonad's tree with `nix/vm-data/{knot,repos,spindle,spindle-logs}`. |
| `${{ secrets.SOME_API_KEY }}` template syntax | Doesn't exist. Per [`spindle/engines/nixery/engine.go`](https://tangled.org/tangled.org/core/blob/master/spindle/engines/nixery/engine.go) `RunStep`, secrets stored in the spindle vault are injected as plain env vars — reference them as `$VAR_NAME` in step `command:`. |
| `treefmt --fail-on-change` step | exomonad has no `treefmt.toml`/`treefmt.nix`. Format checks are `ormolu --mode check` and `cargo fmt --all -- --check`, wired through `just check-fmt` ([justfile](justfile)). |
| `just proto-codegen` | Real recipe is `just proto-gen` (regenerate) or `just proto-check` (CI drift gate, exits 1 on diff). |
| `ghc98` | exomonad uses GHC `9.12.2` (pinned in `flake.nix` and `cabal.project`). |
| `git commit -s` (DCO) | exomonad doesn't require DCO. No sign-off needed. |
| `dependencies.nixpkgs: [bash, git, ...]` baseline | Per [`workflowImage` in `nixery/engine.go`](https://tangled.org/tangled.org/core/blob/master/spindle/engines/nixery/engine.go), `bash`, `git`, `coreutils`, `nix` are auto-appended — don't list them. The list is path-joined into a Nixery image URL: `https://nixery.dev/$pkg1/$pkg2/...`. |
| Tangled webhooks "only support push events" | Misleading. [`workflow/def.go`](https://tangled.org/tangled.org/core/blob/master/workflow/def.go) confirms `pull_request`, `push`, `manual`, `tag` events all parse. PR-event triggering depends on the spindle being subscribed to knot events — not a webhook limitation. |

**Verified workflow YAML schema** (from [`workflow/def.go`](https://tangled.org/tangled.org/core/blob/master/workflow/def.go) + [`spindle/engines/nixery/engine.go`](https://tangled.org/tangled.org/core/blob/master/spindle/engines/nixery/engine.go) `InitWorkflow`):

```yaml
engine: nixery
when:
  - event: [push, pull_request, manual, tag]
    branch: [main, ...]      # required for pull_request
    tag: [...]               # optional, push only
clone:
  depth: 1                   # int
  skip: false                # bool
  submodules: false          # bool
dependencies:
  nixpkgs: [pkg1, pkg2, ...] # path-joined into Nixery URL
  # other registries become 'nix profile add registry#pkg' in a setup step
environment:                 # workflow-level env
  KEY: value
steps:
  - name: "Step name"
    command: "shell command (runs under bash -c)"
    environment:             # per-step env, merged on top of workflow env
      KEY: value
```

Workspace is `/tangled/workspace`, persisted across steps within a workflow. Container image is built on demand by Nixery from the dependency path.

---

## Phase A — Stand up local knot+spindle (verify the toolchain works on your hardware)

**Goal:** confirm the VM boots, knot+spindle come up, and you can authenticate as your tangled.org identity from the appview against the **VM-internal** knot. No exomonad code touched yet.

1. Clone tangled core somewhere outside the exomonad tree:
   ```bash
   git clone https://tangled.org/tangled.org/core ~/src/tangled-core
   cd ~/src/tangled-core
   ```
2. Get your DID from tangled.org → Settings.
3. Launch the VM:
   ```bash
   TANGLED_VM_KNOT_OWNER="did:plc:<your-did>" \
   TANGLED_VM_SPINDLE_OWNER="did:plc:<your-did>" \
   nix run --impure '.#vm'
   ```
   The launcher creates `~/src/tangled-core/nix/vm-data/{knot,repos,spindle,spindle-logs}` for state.
4. Read the actual VM module (`nix/vm.nix`, `nix/modules/{knot,spindle}.nix`) **after** you launch so you can pin down the *real* port mapping — the proposal's 2222/6444/6555 numbers are unverified by me, and `vm.nix` is authoritative. Update step 5 below with whatever you find.
5. Verify the knot and spindle from the host: `ssh -p <knot-ssh-port> git@localhost`, `curl http://localhost:<knot-api-port>/health`, `curl http://localhost:<spindle-port>/health`.
6. **Reachability decision: deferred.** If the appview at tangled.org can't reach `localhost`, the registration challenge in `Settings → Knots → Verify` will fail. That's expected and fine for now — Phase B doesn't require appview registration; Phase C will tell us whether we hit a wall.

**Done when:** VM is running, knot/spindle ports respond locally, you've read `nix/vm.nix` and pinned the actual ports.

---

## Phase B — Add a Tangled pipeline that mirrors current CI, on a branch

**Goal:** a working `.tangled/workflows/ci.yml` in exomonad on a feature branch, mirroring what `.github/workflows/ci.yml` does today, executable by spindle.

Critical files in exomonad to consult/modify:

| Path | Why |
|---|---|
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | Source of truth for what CI must do today (3 jobs: Haskell, Rust, Integration). |
| [`justfile`](justfile) | All recipes (`fmt`, `check-fmt`, `lint`, `test`, `wasm-all`, `proto-check`, `pre-push`). Pipeline steps should call `just <recipe>`, not duplicate logic. |
| [`flake.nix`](flake.nix) | What `nix develop` provides — same packages we'll declare in `dependencies.nixpkgs`. |

1. Create exomonad branch: `git checkout -b ci/tangled-pipeline`.
2. Write `.tangled/workflows/ci.yml`:

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
       # mirror flake.nix devShell — let nix profile install pull versions
       - cabal-install
       - ghc           # default nixpkgs ghc; if 9.12.x mismatch with cabal.project, pin via attribute path
       - rustup
       - protobuf
       - just
       - pkg-config
       - zlib
   environment:
     PROTOC: "/run/current-system/sw/bin/protoc"
   steps:
     - name: "Install hlint"
       command: |
         curl -sSL https://github.com/ndmitchell/hlint/releases/download/v3.8/hlint-3.8-x86_64-linux.tar.gz \
           | tar xz -C /tmp
         install -m 0755 /tmp/hlint-3.8/hlint /usr/local/bin/hlint
     - name: "Format check"
       command: "just check-fmt"
     - name: "Haskell lint"
       command: 'hlint haskell --ignore-glob="haskell/vendor/**" || true'
     - name: "Haskell build"
       command: "cabal update && cabal build all -j"
     - name: "Haskell tests"
       command: |
         cabal test graph-validation-tests
         cabal test worktree-interpreter-test
         cabal test exomonad-wire-types-test
     - name: "Rust toolchain setup"
       command: |
         rustup default stable
         rustup component add clippy rustfmt
         rustup target add wasm32-wasip1
     - name: "Rust clippy"
       command: "cd rust && cargo clippy --workspace --all-targets"
     - name: "Rust fmt check"
       command: "cd rust && cargo fmt --all -- --check"
     - name: "Rust build"
       command: "cd rust && cargo build --workspace --all-targets"
     - name: "Rust tests"
       command: "cd rust && cargo test --workspace -- --skip test_cli_hook --skip test_cli_invalid_json --skip test_cli_other_hook"
     - name: "Integration: protocol golden"
       command: |
         cabal test exomonad-control-server:protocol-golden-test
         cd rust && cargo test -p exomonad-shared --test protocol_golden
     - name: "Proto drift check"
       command: "just proto-check"
   ```

3. Three known fragility points (call out in the commit message so future-you knows):
   - **GHC version pin.** The Nixery `nixpkgs` channel is whatever `nixpkgs-unstable` resolves to at image-build time; if it's not 9.12.2 our `cabal.project` may reject. If this fails: switch the `ghc` line to a pinned attribute (`ghc912`) or build under `nix develop` inside the step.
   - **WASM toolchain.** [`flake.nix`](flake.nix) has a separate `#wasm` devShell with `ghc-wasm-meta`. Nixery + flat `dependencies.nixpkgs` won't easily reproduce that — `wasm-all` is intentionally excluded from this first pipeline. We can add it later either by (a) running `nix develop .#wasm -c ...` as a step (requires `nix` in image, plus IOG cache config), or (b) building exomonad's own Tangled-friendly Nix shell.
   - **`hlint` version.** Pinned to 3.8 here as in CI, fetched fresh each run. If we want to cache, that's a job for the spindle's blob storage later.

4. **Don't commit** `.github/workflows/*.yml` deletions yet.

**Done when:** YAML committed to feature branch.

---

## Phase C — Run the pipeline locally and iterate until green

**Goal:** push the branch to the local knot, watch spindle execute, fix step failures.

1. Add the local-knot remote (use whatever ports `nix/vm.nix` exposed):
   ```bash
   # ~/.ssh/config
   Host local-tangled
     Hostname localhost
     Port <knot-ssh-port>
     User git
     IdentityFile ~/.ssh/<your-tangled-key>

   # in exomonad
   git remote add local-knot git@local-tangled:<your-handle>/exomonad
   git push local-knot main
   git push local-knot ci/tangled-pipeline
   ```
2. Trigger the pipeline. **Defer this until Phase A step 4 surfaces the trigger mechanism** — manual via spindle CLI, or PR creation if the appview can see this knot. If neither works locally, you've discovered the reachability constraint and we replan.
3. Inspect logs in `~/src/tangled-core/nix/vm-data/spindle-logs/` (path is auto-created by the VM launcher, per [flake.nix](https://tangled.org/tangled.org/core/blob/master/flake.nix) line `mkdir -p nix/vm-data/{...}`).
4. Iterate: fix YAML, push, re-trigger. The big unknowns will surface here — Nixery image build time, GHC version mismatches, `cargo` proxy/network behavior, write permissions to `/usr/local/bin`.

**Done when:** the pipeline runs to completion green at least once on `ci/tangled-pipeline`.

---

## Phase D — Investigate Copilot-review replacement (gating step before disabling GH Actions)

**This is the research step you flagged**, and it's the gate for Phase E. Don't delete `.github/workflows/copilot-review.yml` until this has a concrete answer.

The exomonad architecture relies on three things from the GitHub side that aren't currently in Tangled:

1. **Automatic PR review by Copilot.** Triggered on PR creation, posts review comments, sets PR review state. Drives leaf agents' iteration loop.
2. **PR review state polling** ([`rust/exomonad-core/src/services/copilot_review.rs`](rust/exomonad-core/src/services/copilot_review.rs) and [`github_poller.rs`](rust/exomonad-core/src/services/github_poller.rs)). Detects `ChangesRequested` → `ReviewReceived`, transitions to `FixesPushed` after the leaf pushes, fires WASM event handlers that notify the parent TL.
3. **PR merge state.** `gh pr merge` with auto-rebase from `merge_pr` MCP tool.

For each, document one of: **(a)** Tangled has a native equivalent, **(b)** can be wired via a Tangled-resident agent, or **(c)** must remain on GitHub.

Likely findings (informed but unverified guesses — confirm by reading source/docs, not by trusting these):

- **(1) Auto-review:** Tangled has stacked PRs and rounds-based review but no LLM reviewer baked in. We'd need either a self-hosted reviewer agent (Claude/Gemini calling `gh`-equivalent against Tangled's XRPC API) or to keep Copilot review on GH. Sketch the agent: it subscribes to spindle/knot events for new PRs on its repo, runs review, and posts comments via Tangled's lexicon (`/lexicons/`).
- **(2) Polling:** [`github_poller.rs`](rust/exomonad-core/src/services/github_poller.rs) hits `gh api`. A Tangled equivalent would hit Tangled's XRPC — same shape, different transport. The `EventAction` (`InjectMessage`/`NotifyParent`) wiring on the WASM side is unchanged.
- **(3) Merge:** `gh pr merge` → equivalent Tangled XRPC call, or have the TL push the merge directly to the local knot (since Tangled's merge semantics are git-native).

Output of this phase: a short ADR at [`docs/decisions/tangled-migration.md`](docs/decisions/) recording which of (a)/(b)/(c) we're choosing for each of the three pieces, plus a follow-up issue for the implementation work.

**Done when:** ADR written and committed; explicit go/no-go decision on whether Phase E can proceed without breaking exomonad's leaf-loop.

---

## Phase E — Disable GH Actions (only if Phase D unblocked)

1. Delete or comment-out `.github/workflows/ci.yml` (replaced by Tangled pipeline).
2. Delete or comment-out `.github/workflows/copilot-review.yml` only if Phase D landed a working replacement (or you're explicitly accepting the loss).
3. Keep `.github/workflows/build-container.yml` if that's still serving a separate purpose; reconsider per its own job spec.
4. Update [`CLAUDE.md`](CLAUDE.md) and [`.claude/rules/exomonad.md`](.claude/rules/exomonad.md) — the leaf-loop section in Tech Lead Praxis references "Copilot review" extensively; whatever replaces it must be reflected in the docs the agents read at startup.

---

## Phase F — Reachability decision (reserved)

If Phase D concludes that we need PR UI / event-driven pipelines from the appview side, revisit knot reachability: Tailscale Funnel and Cloudflare Tunnel are the two low-friction options. Defer the actual choice — write it then.

---

## Verification (end-to-end)

1. **Phase A:** `pgrep -af qemu` shows the VM; `ssh git@localhost -p <port>` returns Tangled's banner; `nix/vm-data/` populated.
2. **Phase B:** YAML parses with `yq` or `yamllint`. Lint by hand against [`spindle/engines/nixery/engine.go`](https://tangled.org/tangled.org/core/blob/master/spindle/engines/nixery/engine.go) `InitWorkflow` to confirm field names.
3. **Phase C:** Pipeline run completes; logs show all steps ran; final exit code 0. Compare runtime against current GH Actions ci.yml runtime as a baseline.
4. **Phase D:** ADR exists, names the path forward for each of (auto-review, polling, merge), and is reviewed (by you, not me) before Phase E starts.
5. **Phase E:** GH Actions runs no longer fire on push to `main` (check `gh run list --limit 5`); next push to local knot triggers Tangled pipeline only.

---

## Files this plan would create/modify

- `.tangled/workflows/ci.yml` — new
- `~/.ssh/config` — append `Host local-tangled` block (outside repo)
- `docs/decisions/tangled-migration.md` — new (Phase D output)
- `.github/workflows/ci.yml` — delete (Phase E)
- `.github/workflows/copilot-review.yml` — delete (Phase E, gated on D)
- [`CLAUDE.md`](CLAUDE.md) — update CI/PR-loop sections (Phase E)
- [`.claude/rules/exomonad.md`](.claude/rules/exomonad.md) — update Convergence Protocol section (Phase E)

No exomonad source code changes in Phases A–C. Source-level changes (Rust pollers, MCP tools) only land if Phase D's ADR concludes (b) — building a Tangled-resident reviewer/poller — and that work is its own follow-on plan, not this one.
