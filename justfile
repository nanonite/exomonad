# ExoMonad Development Justfile

# Default recipe
default:
    @just --list

# Format all code
fmt: haskell-fmt rust-fmt

# Format Haskell code
haskell-fmt:
    nix develop --command bash -c 'cd haskell && ormolu --mode inplace --ghc-opt -XImportQualifiedPost $(find . -name "*.hs" -not -path "./vendor/*")'

# Format Rust code
rust-fmt:
    nix develop --command cargo fmt --all

# Check formatting (fails if unformatted — run `just fmt` to fix)
check-fmt:
    nix develop --command bash -c 'cd haskell && ormolu --mode check --ghc-opt -XImportQualifiedPost $(find . -name "*.hs" -not -path "./vendor/*")'
    nix develop --command cargo fmt --all --check

# Lint Haskell code
lint:
    nix develop --command hlint haskell

# Run fast Rust tests only
rust-test:
    nix develop --command cargo test --workspace --lib

# Run native Haskell tests
haskell-test:
    nix develop --command cabal test all

# Run fast tests only (Rust unit tests)
test-fast: rust-test

# Run every Rust test target through the dev shell
rust-test-all:
    nix develop --command cargo test --workspace

# Run every Rust test target through the dev shell
test-cargo-all: rust-test-all

# Build WASM, then run the Rust host ↔ Haskell WASM integration tests
test-wasm-integration:
    just wasm-all
    nix develop --command cargo test -p exomonad-core --test wasm_integration

# Build and run the devswarm role-hook-tests WASM test suite
role-hook-tests:
    @nix develop .#wasm --command bash -c 'export PATH=$PWD/.gemini/tmp/bin:$PATH; wasm32-wasi-cabal --project-file=cabal.project.wasm build role-hook-tests'
    @nix develop .#wasm --command bash -c 'set -euo pipefail; WASM=$(find dist-newstyle -name role-hook-tests.wasm -type f -print -quit); test -n "$WASM"; wasmtime "$WASM"'

# Run tests: Rust unit tests, cargo check, WASM build, proto freshness
test:
    #!/usr/bin/env bash
    set -euo pipefail
    echo ">>> [1/5] Rust unit tests..."
    just rust-test
    echo ">>> [2/5] Rust check (all targets)..."
    nix develop --command cargo check --workspace --all-targets
    echo ">>> [3/5] WASM build..."
    just wasm-all
    echo ">>> [4/5] Role hook tests..."
    just role-hook-tests
    echo ">>> [5/5] Proto freshness check..."
    just proto-check
    echo ">>> All checks passed."

# Verify generated proto files are up-to-date
proto-check:
    #!/usr/bin/env bash
    set -euo pipefail
    echo ">>> Regenerating proto to check for drift..."
    just proto-gen
    if ! git diff --quiet haskell/proto/src/ rust/exomonad-proto/src/; then
        echo "ERROR: Generated proto files are out of date."
        echo "Run 'just proto-gen' and commit the results."
        git diff --stat haskell/proto/src/ rust/exomonad-proto/src/
        exit 1
    fi
    echo ">>> Proto files are up to date."

# Pre-push: format check + tests
pre-push: check-fmt test

# Install git hooks (symlinks scripts/hooks/* to .git/hooks/)
install-hooks:
    @echo "Installing git hooks..."
    @ln -sf ../../scripts/hooks/pre-push .git/hooks/pre-push
    @echo "Installed: pre-push"
    @echo "Done. Use 'git push --no-verify' to bypass in emergencies."

# Build WASM role and install to .exo/wasm/
wasm role="tl":
    @nix develop .#wasm --command bash -c 'export PATH=$PWD/.gemini/tmp/bin:$PATH; if [ ! -d ~/.cabal/packages/hackage.haskell.org ]; then echo ">>> First-time WASM setup (populating cabal package index)..."; wasm32-wasi-cabal update --project-file=cabal.project.wasm; fi'
    @echo ">>> Building wasm-guest-{{role}}..."
    nix develop .#wasm --command bash -c 'export PATH=$PWD/.gemini/tmp/bin:$PATH; wasm32-wasi-cabal build --project-file=cabal.project.wasm wasm-guest-{{role}}'
    @echo ">>> Installing to .exo/wasm/..."
    mkdir -p .exo/wasm
    rm -f .exo/wasm/wasm-guest-{{role}}.wasm
    cp $(find dist-newstyle -name "wasm-guest-{{role}}.wasm" -type f -print -quit) .exo/wasm/wasm-guest-{{role}}.wasm
    @echo ">>> Done: .exo/wasm/wasm-guest-{{role}}.wasm"

# Build unified WASM plugin (contains all roles)
wasm-all:
    @just wasm devswarm
    @just wasm e2e-test
    @echo ">>> Installed to .exo/wasm/:"
    @ls -lh .exo/wasm/wasm-guest-*.wasm

# Build Tangled Spindle and install it for consuming repos.
spindle-dev:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d tangled-core/cmd/spindle ]; then
        echo "ERROR: tangled-core/cmd/spindle not found."
        exit 1
    fi
    echo ">>> Building Tangled spindle..."
    mkdir -p .exo/bin ~/.exo/bin
    nix develop --command bash -c 'cd tangled-core && go build -o ../.exo/bin/spindle ./cmd/spindle'
    cp .exo/bin/spindle ~/.exo/bin/spindle
    chmod 755 .exo/bin/spindle ~/.exo/bin/spindle
    echo ">>> Installed spindle:"
    ls -lh .exo/bin/spindle ~/.exo/bin/spindle

# One-time WASM build environment setup (populates cabal package index)
wasm-setup:
    @echo ">>> Setting up WASM build environment (one-time)..."
    nix develop .#wasm --command bash -c 'export PATH=$PWD/.gemini/tmp/bin:$PATH; wasm32-wasi-cabal update --project-file=cabal.project.wasm'
    @echo ">>> Done. You can now run: just wasm-all"

# Internal: shared install logic for release/dev builds.
_install profile:
    #!/usr/bin/env bash
    set -euo pipefail

    if [ "{{profile}}" = "release" ]; then
        CARGO_FLAGS="--release"
        TARGET_DIR="release"
        LABEL="release"
    else
        CARGO_FLAGS=""
        TARGET_DIR="debug"
        LABEL="debug"
    fi

    echo ">>> [1/4] Building Haskell WASM plugins (cabal cached if unchanged)..."
    just wasm-all

    echo ">>> [2/4] Building Rust binary (${LABEL})..."
    nix develop --command cargo build ${CARGO_FLAGS} -p exomonad

    echo ">>> [3/4] Building and installing Tangled spindle..."
    just spindle-dev

    echo ">>> [4/4] Installing binaries..."
    mkdir -p ~/.cargo/bin
    mkdir -p ~/.exo/wasm
    # Atomic rename so install works even when the binary is in use (e.g. mcp-stdio running)
    cp "target/${TARGET_DIR}/exomonad" ~/.cargo/bin/exomonad.new
    mv ~/.cargo/bin/exomonad.new ~/.cargo/bin/exomonad
    cp .exo/wasm/wasm-guest-devswarm.wasm ~/.exo/wasm/
    [ -f .exo/wasm/wasm-guest-e2e-test.wasm ] && cp .exo/wasm/wasm-guest-e2e-test.wasm ~/.exo/wasm/ || true

    # Install role context files for consuming repos
    mkdir -p ~/.exo/roles/devswarm/context
    cp .exo/roles/devswarm/context/*.md ~/.exo/roles/devswarm/context/

    # macOS: remove quarantine and ad-hoc sign to avoid sandbox/Gatekeeper issues
    if [ "$(uname)" = "Darwin" ]; then
        xattr -d com.apple.quarantine ~/.cargo/bin/exomonad 2>/dev/null || true
        codesign -s - -f ~/.cargo/bin/exomonad 2>/dev/null || true
    fi

    echo ">>> Done!"
    echo ""
    echo "Installed:"
    ls -lh ~/.cargo/bin/exomonad
    ls -lh .exo/wasm/wasm-guest-devswarm.wasm
    ls -lh ~/.exo/bin/spindle

# Build Rust binary only (no WASM, no install) — fast iteration
build:
    nix develop --command cargo build -p exomonad

# Install everything: Rust binaries + WASM plugins (release build)
install-all: (_install "release")

# Install everything (fast dev build)
install-all-dev: (_install "dev")

# Regenerate Haskell proto types
# Generated files are checked in - only run when protos change
proto-gen-haskell:
    nix develop --command ./proto-codegen/generate.sh

# Regenerate Rust proto types (part of normal cargo build)
proto-gen-rust:
    nix develop --command cargo build -p exomonad-proto

# Full proto regeneration (includes formatting so proto-check passes)
proto-gen: proto-gen-haskell proto-gen-rust
    nix develop --command bash -c 'cd haskell && ormolu --mode inplace --ghc-opt -XImportQualifiedPost $(find proto/src -name "*.hs")'
    @echo "Proto generation complete. Don't forget to commit haskell/proto/src/"

# Verify proto changes don't break wire format
proto-test:
    #!/usr/bin/env bash
    set -euo pipefail
    echo ">>> Running Rust proto wire format tests..."
    nix develop --command cargo test -p exomonad-proto
    echo ">>> Running Haskell proto tests..."
    nix develop --command cabal test exomonad-proto || echo "No tests defined yet"
    echo ">>> Running proto wire format compatibility test..."
    nix develop --command cabal run proto-test || echo "Wire format test not yet implemented"
    echo ">>> Done"

# Run MCP integration tests (starts server, runs tests, cleans up)
test-mcp *args:
    ./scripts/test-mcp-integration.sh {{args}}

# Run interactive E2E test (starts mocks, drops you into exomonad init session)
test-e2e:
    ./tests/e2e/run.sh

# ============================================================================
# E2E test recipes
#
# Naming convention across this section:
#   e2e-<name>        — runs the test interactively (sets up temp repo, launches
#                       tmux, attaches you so you can observe codex/claude
#                       sessions). This is the one you want when you actually
#                       want to run the test.
#   check-e2e-<name>  — `bash -n` static syntax check of the harness scripts.
#                       Silent on success — produces no useful output for the
#                       operator. Used by CI/pre-commit to ensure the scripts
#                       parse, NOT to verify the test passes.
#
# If you ran a `check-e2e-*` and got "two bash scripts" echoed at you, you
# wanted the matching `e2e-<name>` recipe.
# ============================================================================

# Run E2E messaging test (Teams inbox delivery, no spawn/merge)
e2e-messaging:
    ./tests/e2e/messaging/run.sh

# Run E2E OpenCode hook rewrite test (BeforeModel/AfterModel PII term rewriting)
e2e-oc-rewrite:
    ./tests/e2e/hook-rewrite/run.sh

# Run E2E OpenCode TL test (ACP delivery chain: serve → port capture → run --attach → MCP → notify_parent)
e2e-opencode-tl:
    ./tests/e2e/opencode-tl/run.sh

# Run E2E OpenCode worker test (fork_wave agent_type=opencode, model forwarding, notify_parent)
e2e-opencode-worker:
    ./tests/e2e/opencode-worker/run.sh

# Run E2E Codex hooks test (root/TL/dev/reviewer hook config and dispatch)
e2e-codex-hooks:
    ./tests/e2e/codex-hooks/run.sh

# Check E2E Codex hooks harness scripts without launching Codex/tmux
check-e2e-codex-hooks:
    bash -n tests/e2e/codex-hooks/run.sh
    bash -n tests/e2e/codex-hooks/validate.sh

# Compare ExoMonad's trusted hook hash with the installed Codex CLI hash
e2e-codex-hook-parity:
    nix develop --command cargo test -p exomonad-core --lib codex_hook_hash_matches_installed_codex_cli -- --ignored --nocapture

# Run E2E Codex messaging test (send_message + notify_parent tmux delivery)
e2e-codex-messaging:
    ./tests/e2e/codex-messaging/run.sh

# Check E2E Codex messaging harness scripts without launching Codex/tmux
check-e2e-codex-messaging:
    bash -n tests/e2e/codex-messaging/run.sh
    bash -n tests/e2e/codex-messaging/validate.sh

# Run E2E chainlink issue create test (chainlink_issue_create MCP tool via ProcessRun)
e2e-chainlink:
    ./tests/e2e/chainlink/run.sh

# Run E2E Chainlink Codex flow test (Codex TL + Codex worker Chainlink MCP flow)
e2e-chainlink-codex:
    ./tests/e2e/chainlink-codex/run.sh

# Check E2E Chainlink Codex harness scripts without launching Codex/tmux
check-e2e-chainlink-codex:
    bash -n tests/e2e/chainlink-codex/run.sh
    bash -n tests/e2e/chainlink-codex/validate.sh

# Run E2E reviewer convergence loop test (fixes_pushed fan-out to reviewer per chainlink #247)
e2e-reviewer-convergence:
    ./tests/e2e/reviewer-convergence-loop/run.sh

# Check E2E reviewer convergence harness scripts without launching Codex/tmux
check-e2e-reviewer-convergence:
    bash -n tests/e2e/reviewer-convergence-loop/run.sh
    bash -n tests/e2e/reviewer-convergence-loop/validate.sh

# Run E2E reviewer ephemerality regression test.
e2e-reviewer-ephemerality:
    ./tests/e2e/reviewer-ephemerality/run.sh

# Check E2E reviewer ephemerality harness scripts without launching agents.
check-e2e-reviewer-ephemerality:
    bash -n tests/e2e/reviewer-ephemerality/run.sh
    bash -n tests/e2e/reviewer-ephemerality/validate.sh

# Run E2E Chainlink sqlite direct DB access block test
e2e-chainlink-sqlite-block:
    ./tests/e2e/chainlink-sqlite-block/run.sh

# Check E2E Chainlink sqlite block harness script without launching the server
check-e2e-chainlink-sqlite-block:
    bash -n tests/e2e/chainlink-sqlite-block/run.sh

# Check Chainlink timer/session role scoping without launching agents
check-e2e-chainlink-timer-role-scope:
    bash -n tests/e2e/chainlink-timer-role-scope/validate.sh
    bash tests/e2e/chainlink-timer-role-scope/validate.sh

# Assert live MCP tool visibility matches docs/architecture/agent-system.md
e2e-mcp-tool-visibility:
    ./tests/e2e/mcp-tool-visibility/run.sh

# Check MCP tool visibility harness without running the WASM assertion
check-e2e-mcp-tool-visibility:
    bash -n tests/e2e/mcp-tool-visibility/run.sh

# Run Tangled PR integration test through Codex root/TL/worker/dev/reviewer roles.
# Requires the local knot container: docker compose up -d  (in tests/e2e/tangled-ci/)
e2e-tangled-pr-codex:
    ./tests/e2e/tangled-pr-codex/run.sh

# Check Tangled PR Codex E2E scripts without launching agents or containers.
check-e2e-tangled-pr-codex:
    bash -n tests/e2e/tangled-pr-codex/run.sh
    bash -n tests/e2e/tangled-pr-codex/validate.sh
    bash -n tests/e2e/tangled-pr-codex/deferred-spindle.sh
    python3 -m py_compile tests/e2e/tangled-pr-codex/knot-event-relay.py


# Run Tangled VM PR integration test against a pre-provisioned Tangled VM.
e2e-tangled-vm-pr:
    ./tests/e2e/tangled-vm-pr/run.sh

# Check Tangled VM PR E2E scripts without launching agents or contacting the VM.
check-e2e-tangled-vm-pr:
    bash -n tests/e2e/tangled-vm-pr/run.sh
    bash -n tests/e2e/tangled-vm-pr/validate.sh
    python3 -m py_compile tests/e2e/tangled-vm-pr/pr-field.py

# Run Tangled CI integration test.
# Requires only the knot container: docker compose up -d  (in tests/e2e/tangled-ci/)
# Manages spindle lifecycle entirely: kills any existing instance, starts fresh, cleans up on exit.
e2e-tangled-ci:
    #!/usr/bin/env bash
    set -euo pipefail
    PROJECT_ROOT="$(pwd)"
    SPINDLE_LOG="/tmp/spindle-e2e-$$.log"
    SPINDLE_PID=""

    cleanup() {
        echo ""
        echo "=== Cleanup ==="
        [[ -n "$SPINDLE_PID" ]] && kill "$SPINDLE_PID" 2>/dev/null || true
        rm -f "$SPINDLE_LOG"
        rm -f "$PROJECT_ROOT/spindle.db"
        rm -f /tmp/tangled-ci-e2e-rkey
        rm -rf /tmp/spindle-logs
        echo "Done."
    }
    trap cleanup EXIT

    echo "=== Stopping any existing spindle on :6555 ==="
    pkill -f 'tangled-core/cmd/spindle/spindle' 2>/dev/null || true
    sleep 1

    ./tests/e2e/tangled-ci/setup.sh

    echo ""
    echo "=== Starting spindle ==="
    ./tests/e2e/tangled-ci/start-spindle.sh > "$SPINDLE_LOG" 2>&1 &
    SPINDLE_PID=$!
    echo "Spindle PID: $SPINDLE_PID  Log: $SPINDLE_LOG"

    echo "Waiting for spindle to be ready..."
    for i in $(seq 1 20); do
        if curl -sf http://localhost:6555/ > /dev/null 2>&1; then break; fi
        sleep 1
    done
    if ! curl -sf http://localhost:6555/ > /dev/null 2>&1; then
        echo "ERROR: spindle did not come up within 20s. Logs:"
        cat "$SPINDLE_LOG"
        exit 1
    fi
    echo "Spindle ready."

    RKEY="$(cat /tmp/tangled-ci-e2e-rkey 2>/dev/null)"
    if [[ -z "$RKEY" ]]; then
        echo "ERROR: could not read rkey from /tmp/tangled-ci-e2e-rkey"
        exit 1
    fi
    # Spindle writes per-workflow logs to /tmp/spindle-logs/{rkey}-{name}.log
    WORKFLOW_LOG="/tmp/spindle-logs/localhost-5555-${RKEY}-ci.yml.log"
    echo ""
    echo "=== Waiting for pipeline result (rkey=${RKEY}, timeout: 5m) ==="
    DEADLINE=$((SECONDS + 300))
    RESULT=""
    ENQUEUED=0
    while [[ $SECONDS -lt $DEADLINE ]]; do
        if [[ $ENQUEUED -eq 0 ]] && grep -q "pipeline enqueued successfully.*${RKEY}" "$SPINDLE_LOG" 2>/dev/null; then
            echo "  pipeline enqueued"
            ENQUEUED=1
        fi
        if [[ -f "$WORKFLOW_LOG" ]]; then
            # Last control line with step_status tells us if the final step completed
            if grep -q '"step_status":"end"' "$WORKFLOW_LOG" && grep -q '"all tests passed"\|"step_status":"end"' "$WORKFLOW_LOG" 2>/dev/null; then
                if ! grep -q '"step_status":"failed"\|"exit_code":[^0]' "$WORKFLOW_LOG" 2>/dev/null; then
                    RESULT="pass"
                    break
                fi
            fi
            if grep -q '"step_status":"failed"' "$WORKFLOW_LOG" 2>/dev/null; then
                RESULT="fail"
                break
            fi
        fi
        sleep 2
    done

    echo ""
    echo "=== Spindle daemon log ==="
    cat "$SPINDLE_LOG"
    echo ""
    if [[ -f "$WORKFLOW_LOG" ]]; then
        echo "=== Workflow log ($RKEY) ==="
        cat "$WORKFLOW_LOG" | python3 tests/e2e/tangled-ci/format-log.py
    fi
    echo ""

    if [[ "$RESULT" == "pass" ]]; then
        echo "PASS: pipeline completed successfully"
    elif [[ "$RESULT" == "fail" ]]; then
        echo "FAIL: pipeline step failed (see workflow log above)"
        exit 1
    else
        echo "FAIL: timed out waiting for pipeline result"
        exit 1
    fi

# Run live E2E Teams messaging test (requires active CC team "teams-e2e")
live-teams-e2e:
    nix develop --command cargo test -p claude-teams-bridge --test integration -- live_teams_e2e --ignored --nocapture

# Validate Gemini settings against schema
validate-settings:
    nix-shell -p python3Packages.jsonschema --run "python3 scripts/validate_json.py .gemini/settings.json schema/gemini-cli/settings.schema.json"
