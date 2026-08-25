# ExoMonad Development Justfile

# Development-only Python interpreter for tl_loop tooling (pytest and ruff).
#
# This exception defaults to the repo-local uv venv declared by tl_loop/pyproject.toml, which
# carries pytest and ruff. A bare `python3` is not sufficient — the system
# interpreter has no pytest, which silently fails `just test` at tl-loop-test
# and tl-loop-replay.
#
# Override for a conda env or any other interpreter:
#   EXOMONAD_PY=/home/you/anaconda3/envs/foo/bin/python just test
py := env_var_or_default("EXOMONAD_PY", justfile_directory() / "tl_loop/.venv/bin/python")
controller_policy := justfile_directory() / "tl_loop/interpreter_policy.toml"

# Default recipe
default:
    @just --list

# Verify the configured Python interpreter has the tl_loop test dependencies
py-check:
    @{{py}} -c "import pytest, sys; print(f'python {sys.version.split()[0]} pytest {pytest.__version__} -> {sys.executable}')" \
        || (echo "ERROR: {{py}} lacks pytest. Create the venv (uv sync in tl_loop/) or set EXOMONAD_PY." && exit 1)

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

# Check that fixture activity has not damaged the ExoMonad repository.
test-repo-integrity:
    bash scripts/check-repo-integrity.sh

# Run Rust tests; include integration targets when requested.
rust-test all_targets="false": test-repo-integrity
    #!/usr/bin/env bash
    set -uo pipefail
    nextest_args=(--workspace --no-fail-fast)
    if [ "{{all_targets}}" != "true" ]; then
        nextest_args+=(--lib)
    fi
    junit_path="target/nextest/default/junit.xml"
    if nix develop --command cargo nextest run "${nextest_args[@]}"; then
        exit 0
    fi
    failure_dir="target/nextest/failures"
    mkdir -p "$failure_dir"
    failure_report=$(mktemp "$failure_dir/junit-XXXXXX.xml")
    if [ -f "$junit_path" ]; then
        cp "$junit_path" "$failure_report"
        echo "Rust test failure JUnit report preserved at $failure_report" >&2
    else
        rm -f "$failure_report"
        echo "Rust tests failed; JUnit report was not found at $junit_path" >&2
    fi
    exit 1

# Run native Haskell tests
haskell-test:
    nix develop --command cabal test all

# Run the programmatic TL controller smoke tests
tl-loop-test:
    {{py}} -m pytest -q tl_loop/tests

# Validate relative documentation links and executable ordered-plan examples.
docs-check:
    python3 scripts/check-doc-links.py
    {{py}} -m pytest -q tl_loop/tests/test_documented_ordered_plans.py

# Replay committed TL event streams against the real loop
tl-loop-replay:
    {{py}} -m pytest -q tl_loop/tests/test_replay.py

# Run the hermetic ordered recursive integration acceptance suite
tl-loop-ordered-e2e:
    {{py}} -m pytest -q tl_loop/tests/test_driver.py -k 'ordered or aggregate or merging'

# Run the real-git/tmux recursive ordered integration harness. Set
# EXOMONAD_FORGEJO_E2E_MOCK=1 for the local API fixture; otherwise provide a
# dedicated Forgejo repository through EXOMONAD_FORGEJO_E2E_* variables.
tl-loop-ordered-forgejo:
    ./tests/e2e/ordered-recursive/run.sh

# Run the real Rust server + WASM + TransportClient ordered recursion check
# against disposable Git and Forgejo-shaped fixtures.
tl-loop-ordered-server-e2e:
    nix develop --command cargo build -p exomonad
    {{py}} tests/e2e/ordered-recursive/real_server_transport.py

# Run only the bounded merge-restart acceptance probes against the real server.
tl-loop-merge-convergence-e2e:
    nix develop --command cargo build -p exomonad
    EXOMONAD_MERGE_CONVERGENCE_ONLY=1 {{py}} tests/e2e/ordered-recursive/real_server_transport.py

# Require three consecutive clean real-server convergence runs. Each run uses
# fresh disposable Git, Forgejo-shaped, tmux, and controller state.
tl-loop-ordered-server-e2e-3x:
    #!/usr/bin/env bash
    set -euo pipefail
    for attempt in 1 2 3; do
        echo ">>> ordered server merge convergence run ${attempt}/3"
        just tl-loop-ordered-server-e2e
    done

tl-loop-merge-convergence-e2e-3x:
    #!/usr/bin/env bash
    set -euo pipefail
    for attempt in 1 2 3; do
        echo ">>> merge convergence server run ${attempt}/3"
        just tl-loop-merge-convergence-e2e
    done

# Lint the programmatic TL controller
tl-loop-lint:
    {{py}} -m ruff check tl_loop --exclude tl_loop/tests scripts/compile_failure_atlas.py scripts/failure_atlas_measure.py

# Verify every declared tool is role-registered and controller-callable tools
# are exposed by the TL role.
tool-surface-check:
    python3 scripts/check_tool_surface.py --project-root .

# Cross-check every statically discoverable controller payload against the
# shared event field contract used by Rust.
controller-event-contract-check:
    python3 scripts/check_controller_event_contract.py --project-root .

# Type-check the programmatic TL controller
tl-loop-typecheck:
    mypy tl_loop

# Regenerate the Haskell-sourced TL FSM parity fixture
tl-loop-golden:
    #!/usr/bin/env bash
    set -euo pipefail
    nix develop .#wasm --command bash -c '
        set -euo pipefail
        export PATH="$PWD/.codex/tmp/bin:$PATH"
        wasm32-wasi-cabal --project-file=cabal.project.wasm build role-hook-tests
        wasm_path=$(find dist-newstyle -name role-hook-tests.wasm -type f -print -quit)
        test -n "$wasm_path"
        source_hash=$(git hash-object .exo/roles/devswarm/TLPhase.hs)
        fixture_tmp=$(mktemp tl_loop/tests/.tl-phase-golden.XXXXXX)
        trap '\''rm -f "$fixture_tmp"'\'' EXIT
        wasmtime "$wasm_path" --tl-phase-golden "$source_hash" | jq -S . > "$fixture_tmp"
        chmod 644 "$fixture_tmp"
        mv "$fixture_tmp" tl_loop/tests/fixtures/tl_phase_golden.json
        trap - EXIT
    '

# Run fast tests only (Rust unit tests)
test-fast: rust-test

# Run every Rust test target through the diagnostic-preserving runner
rust-test-all:
    just rust-test true

# Run every Rust test target through the dev shell
test-cargo-all: rust-test-all

# Build WASM, then run the Rust host ↔ Haskell WASM integration tests
test-wasm-integration:
    just wasm-all
    nix develop --command cargo nextest run -p exomonad-core --test wasm_integration

# Build and run the devswarm role-hook-tests WASM test suite
role-hook-tests:
    @nix develop .#wasm --command bash -c 'export PATH=$PWD/.codex/tmp/bin:$PATH; wasm32-wasi-cabal --project-file=cabal.project.wasm build role-hook-tests'
    @nix develop .#wasm --command bash -c 'set -euo pipefail; WASM=$(find dist-newstyle -name role-hook-tests.wasm -type f -print -quit); test -n "$WASM"; wasmtime "$WASM"'

# Build and run the wasm-guest test suite under wasmtime
wasm-guest-test:
    @nix develop .#wasm --command bash -c 'export PATH=$PWD/.codex/tmp/bin:$PATH; wasm32-wasi-cabal --project-file=cabal.project.wasm build wasm-guest:wasm-guest-tests'
    @nix develop .#wasm --command bash -c 'set -euo pipefail; WASM=$(find dist-newstyle -name wasm-guest-tests.wasm -type f -print -quit); test -n "$WASM"; wasmtime "$WASM"'

# Run tests: Python checks, formatting, Rust check, WASM build/tests, Rust tests, proto freshness
test: tl-loop-replay tl-loop-test tl-loop-lint tl-loop-archive-test tool-surface-check controller-event-contract-check
    #!/usr/bin/env bash
    set -euo pipefail
    echo ">>> [1/8] Observability contract checks..."
    just validate-observability-contracts
    echo ">>> [2/8] Formatting checks..."
    just check-fmt
    echo ">>> [3/8] Rust check (all targets)..."
    nix develop --command cargo check --workspace --all-targets
    echo ">>> [4/8] WASM build..."
    just wasm-all
    echo ">>> [5/8] Rust tests (all targets)..."
    just rust-test-all
    echo ">>> [6/8] Role hook tests..."
    just role-hook-tests
    echo ">>> [7/8] WASM guest tests..."
    just wasm-guest-test
    echo ">>> [8/8] Proto freshness check..."
    just proto-check
    echo ">>> All checks passed."

# Verify generated proto files are up-to-date
proto-check:
    #!/usr/bin/env bash
    set -euo pipefail
    temp_root=$(mktemp -d)
    trap 'rm -rf "$temp_root"' EXIT
    cp -a haskell/proto/src/. "$temp_root/"
    echo ">>> Regenerating proto into a temporary tree to check for drift..."
    PROTO_OUTPUT_ROOT="$temp_root" nix develop --command ./proto-codegen/generate.sh
    nix develop --command cargo build -p exomonad-proto
    if ! diff -ru haskell/proto/src "$temp_root"; then
        echo "ERROR: Generated proto files are out of date."
        echo "Run 'just proto-gen' and commit the results."
        exit 1
    fi
    echo ">>> Proto files are up to date."

# Pre-push: format check + tests
pre-push: check-fmt test

# Fail closed if the retired provider name returns outside the canonical
# deprecation message. The recipe declaration is excluded because it names
# this gate; every other non-ignored source path is scanned.
check-no-gemini:
    #!/usr/bin/env bash
    set -euo pipefail
    needle="$(printf 'ge%s' mini)"
    canonical="$(rg -o '\"agent_type [^\"]+\"' rust/exomonad-core/src/services/agent_control/mod.rs | sed 's/^\"//; s/\"$//')"
    test -n "$canonical"
    hits="$(rg -n -i --hidden "$needle" . \
        --glob '!target/**' \
        --glob '!dist-newstyle/**' \
        --glob '!vendor/**' \
        --glob '!archive/**' \
        --glob '!.chainlink/issues.json' \
        --glob '!.git/**' \
        --glob '!justfile' || true)"
    unexpected="$(printf '%s\n' "$hits" | grep -vF "$canonical" || true)"
    if test -n "$unexpected"; then
        printf '%s\n' "$unexpected" >&2
        exit 1
    fi

# Pre-commit: deprecation gate plus the full repository checks
# `test` includes the TL-loop smoke test and lint checks.
pre-commit:
    just check-no-$(printf 'ge%s' mini)
    just test

# Install git hooks (symlinks scripts/hooks/* to .git/hooks/)
install-hooks:
    @echo "Installing git hooks..."
    @ln -sf ../../scripts/hooks/pre-push .git/hooks/pre-push
    @echo "Installed: pre-push"
    @echo "Done. Use 'git push --no-verify' to bypass in emergencies."

# Build WASM role and install to .exo/wasm/
wasm role="tl":
    @nix develop .#wasm --command bash -c 'export PATH=$PWD/.codex/tmp/bin:$PATH; cache_dir="$(wasm32-wasi-cabal path --project-file=cabal.project.wasm | sed -n "s/^remote-repo-cache: //p")"; test -n "$cache_dir"; if [ ! -d "$cache_dir/hackage.haskell.org" ] || [ ! -d "$cache_dir/head.hackage" ]; then echo ">>> First-time WASM setup (populating cabal package index in $cache_dir)..."; wasm32-wasi-cabal update --project-file=cabal.project.wasm; fi'
    @echo ">>> Building wasm-guest-{{role}}..."
    nix develop .#wasm --command bash -c 'export PATH=$PWD/.codex/tmp/bin:$PATH; wasm32-wasi-cabal build --project-file=cabal.project.wasm wasm-guest-{{role}}'
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

# One-time WASM build environment setup (populates cabal package index)
wasm-setup:
    @echo ">>> Setting up WASM build environment (one-time)..."
    nix develop .#wasm --command bash -c 'export PATH=$PWD/.codex/tmp/bin:$PATH; wasm32-wasi-cabal update --project-file=cabal.project.wasm'
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

    echo ">>> [1/3] Building Haskell WASM plugins (cabal cached if unchanged)..."
    just wasm-all

    echo ">>> [2/4] Checking the TL controller Python version..."
    just tl-loop-python-check

    echo ">>> [3/4] Building the embedded TL controller archive..."
    just tl-loop-archive
    test -s tl_loop.pyz

    echo ">>> [4/4] Building Rust binary ()..."
    nix develop --command cargo build ${CARGO_FLAGS} -p exomonad

    echo ">>> [5/5] Installing binaries..."
    mkdir -p ~/.cargo/bin
    mkdir -p ~/.exo/wasm
    # Atomic rename so install works even when the binary is in use (e.g. mcp-stdio running)
    cp "target/${TARGET_DIR}/exomonad" ~/.cargo/bin/exomonad.new
    mv ~/.cargo/bin/exomonad.new ~/.cargo/bin/exomonad
    rm -rf ~/.exo/tl_loop
    cp tl_loop.pyz ~/.exo/tl_loop.pyz.new
    mv ~/.exo/tl_loop.pyz.new ~/.exo/tl_loop.pyz
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
# Build Rust binary only (no WASM, no install) — fast iteration
build:
    nix develop --command cargo build -p exomonad

# Install everything: Rust binaries + WASM plugins (release build)
install-all: (_install "release")

# Install everything (fast dev build)
install-all-dev: (_install "dev")

# Build the stdlib-only TL controller as a package-preserving zipapp.
tl-loop-archive:
    #!/usr/bin/env bash
    set -euo pipefail
    controller=$(python3 scripts/resolve_tl_loop_python.py --policy "{{controller_policy}}")
    echo "TL controller archive interpreter: $controller"
    "$controller" scripts/build_tl_loop_archive.py --source tl_loop --output tl_loop.pyz

tl-loop-python-check:
    #!/usr/bin/env bash
    set -euo pipefail
    controller=$(python3 scripts/resolve_tl_loop_python.py --policy "{{controller_policy}}")
    echo "TL controller build interpreter: $controller"
    "$controller" scripts/check_tl_loop_python.py

tl-loop-archive-test: tl-loop-archive
    #!/usr/bin/env bash
    set -euo pipefail
    python3 scripts/check_tl_loop_archive.py "$(pwd)/tl_loop.pyz" --source "$(pwd)/tl_loop"
    installed="${EXOMONAD_TL_LOOP_ARCHIVE:-$HOME/.exo/tl_loop.pyz}"
    if test -f "$installed"; then
        python3 scripts/check_tl_loop_archive.py "$installed" --source "$(pwd)/tl_loop"
    else
        echo "TL controller installed archive not found at $installed; local archive check passed"
    fi

# Compatibility entry point for profile-based install commands
install profile:
    #!/usr/bin/env bash
    set -euo pipefail

    case "{{profile}}" in
        all)
            just install-all
            ;;
        all-dev)
            just install-all-dev
            ;;
        *)
            echo "Usage: just install all | all-dev" >&2
            exit 2
            ;;
    esac

# Regenerate Haskell proto types
# Generated files are checked in - only run when protos change
proto-gen-haskell:
    nix develop --command ./proto-codegen/generate.sh

# Regenerate Rust proto types (part of normal cargo build)
proto-gen-rust:
    nix develop --command cargo build -p exomonad-proto

# Full proto regeneration (the generator formats its Haskell output)
proto-gen: proto-gen-haskell proto-gen-rust
    @echo "Proto generation complete. Don't forget to commit haskell/proto/src/"

# Verify proto changes don't break wire format
proto-test:
    #!/usr/bin/env bash
    set -euo pipefail
    echo ">>> Running Rust proto wire format tests..."
    nix develop --command cargo nextest run -p exomonad-proto
    echo ">>> Running Haskell proto tests..."
    nix develop --command cabal test exomonad-proto || echo "No tests defined yet"
    echo ">>> Running proto wire format compatibility test..."
    nix develop --command cabal run proto-test || echo "Wire format test not yet implemented"
    echo ">>> Done"

# Run MCP integration tests (starts server, runs tests, cleans up)
test-mcp *args:
    ./scripts/test-mcp-integration.sh {{args}}

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

# Capture a live multi-slice TL trajectory beside the read-only shadow loop.
e2e-tl-loop-shadow:
    ./tests/e2e/tl-loop-shadow/run.sh

check-e2e-tl-loop-shadow:
    bash -n tests/e2e/tl-loop-shadow/run.sh
    {{py}} -m py_compile tests/e2e/tl-loop-shadow/shadow_companion.py

# Run the bounded active TL loop and the default TL-window command against a scratch repository; no session is created by this test.
e2e-tl-loop-active:
    ./tests/e2e/tl-loop-active/run.sh

# Run the real-server closed-PR abandonment and redispatch acceptance scenario three times.
e2e-slice-abandon-redispatch:
    ./tests/e2e/slice-abandon-redispatch/run.sh

check-e2e-slice-abandon-redispatch:
    bash -n tests/e2e/slice-abandon-redispatch/run.sh
    {{py}} -m py_compile tests/e2e/slice-abandon-redispatch/run.py

# Run the real-server base-CI blocked handoff, durable gate, and same-owner resume scenario three times.
e2e-task-blocked-human-gate:
    nix develop --command cargo build -p exomonad
    just wasm devswarm
    ./tests/e2e/task-blocked-human-gate/run.sh

check-e2e-task-blocked-human-gate:
    bash -n tests/e2e/task-blocked-human-gate/run.sh
    {{py}} -m py_compile tests/e2e/task-blocked-human-gate/run.py

e2e-pre-pr-recovery-fsm:
    nix develop --command cargo build -p exomonad
    just wasm devswarm
    ./tests/e2e/pre-pr-recovery-fsm/run.sh

check-e2e-pre-pr-recovery-fsm:
    bash -n tests/e2e/pre-pr-recovery-fsm/run.sh
    {{py}} -m py_compile tests/e2e/pre-pr-recovery-fsm/run.py
    test -s tests/e2e/pre-pr-recovery-fsm/e2e-test.md
    test -s tests/e2e/pre-pr-recovery-fsm/testrunner.md

check-e2e-tl-loop-active:
    bash -n tests/e2e/tl-loop-active/run.sh
    {{py}} -m py_compile tests/e2e/tl-loop-active/active_run.py

# Run bounded Claude-only smoke test (root SessionStart + TeamCreate, no children)
e2e-claude-only:
    ./tests/e2e/claude-only/run.sh

# Check bounded Claude-only smoke harness without launching Claude/tmux
check-e2e-claude-only:
    bash -n tests/e2e/claude-only/run.sh

# Run E2E Claude Teams inbox review chain (Claude TL -> Claude dev leaf -> Claude reviewer)
e2e-claude-teams-inbox:
    ./tests/e2e/claude-teams-inbox/run.sh

# Check Claude Teams inbox review-chain harness scripts without launching Claude/tmux
check-e2e-claude-teams-inbox:
    bash -n tests/e2e/claude-teams-inbox/run.sh
    bash -n tests/e2e/claude-teams-inbox/validate.sh

# Run E2E OpenCode hook rewrite test (BeforeModel/AfterModel PII term rewriting)
e2e-oc-rewrite:
    ./tests/e2e/hook-rewrite/run.sh

# Run E2E OpenCode TL test (OpenCode serve/attach delivery chain: serve → port capture → run --attach → MCP → notify_parent)
e2e-opencode-tl:
    ./tests/e2e/opencode-tl/run.sh

# Run E2E OpenCode leaf test (spawn_leaf agent_type=opencode, model forwarding, notify_parent)
e2e-opencode-worker:
    ./tests/e2e/opencode-worker/run.sh

# Compare ExoMonad's trusted hook hash with the installed Codex CLI hash
e2e-codex-hook-parity:
    nix develop --command cargo nextest run -p exomonad-core --lib codex_hook_hash_matches_installed_codex_cli --run-ignored ignored-only --no-capture

# Run E2E Codex messaging test (send_tmux_message + notify_parent delivery)
e2e-codex-messaging:
    ./tests/e2e/codex-messaging/run.sh

# Check E2E Codex messaging harness scripts without launching Codex/tmux
check-e2e-codex-messaging:
    bash -n tests/e2e/codex-messaging/run.sh
    bash -n tests/e2e/codex-messaging/validate.sh


# Run E2E mixed agent chain test (Claude TL -> OpenCode worker, Codex reviewer config)
e2e-tl-to-worker-messaging:
    ./tests/e2e/tl-to-worker-messaging/run.sh

# Check E2E mixed agent chain scripts without launching Claude/OpenCode/Codex/tmux
check-e2e-tl-to-worker-messaging:
    bash -n tests/e2e/tl-to-worker-messaging/run.sh
    bash -n tests/e2e/tl-to-worker-messaging/validate.sh

# Run E2E worker notify_parent pane-pinning test
e2e-subtl-worker-notify:
    ./tests/e2e/subtl-worker-notify/run.sh

# Check E2E worker notify harness scripts without launching Codex/tmux
check-e2e-subtl-worker-notify:
    bash -n tests/e2e/subtl-worker-notify/run.sh
    bash -n tests/e2e/subtl-worker-notify/validate.sh


# Run E2E chainlink issue create test (chainlink_issue_create MCP tool via ProcessRun)
e2e-chainlink:
    ./tests/e2e/chainlink/run.sh

# Run E2E Chainlink Codex flow test (root Codex + direct dev leaf Chainlink MCP flow)
e2e-chainlink-codex:
    ./tests/e2e/chainlink-codex/run.sh

# Check E2E Chainlink Codex harness scripts without launching Codex/tmux
check-e2e-chainlink-codex:
    bash -n tests/e2e/chainlink-codex/run.sh
    bash -n tests/e2e/chainlink-codex/validate.sh

# Run the opt-in Codex-TL orphan-PR resume smoke test
e2e-orphan-pr-guard-codex:
    ./tests/e2e/orphan-pr-guard/run.sh

# Check the Codex-TL orphan-PR smoke harness without launching Codex/tmux
check-e2e-orphan-pr-guard-codex:
    bash -n tests/e2e/orphan-pr-guard/run.sh

# Run E2E Chainlink sqlite direct DB access block test
e2e-chainlink-sqlite-block:
    ./tests/e2e/chainlink-sqlite-block/run.sh

# Check E2E Chainlink sqlite block harness script without launching the server
check-e2e-chainlink-sqlite-block:
    bash -n tests/e2e/lib/harness.sh
    bash -n tests/e2e/chainlink-sqlite-block/run.sh

# Run E2E cross-harness SQLite inbox test
e2e-cross-harness-inbox:
    ./tests/e2e/cross-harness-inbox/run.sh

# Check E2E cross-harness inbox harness script without launching the server
check-e2e-cross-harness-inbox:
    bash -n tests/e2e/lib/harness.sh
    bash -n tests/e2e/cross-harness-inbox/run.sh

# Run E2E reviewer hardening and authorship preservation test
e2e-authorship:
    ./tests/e2e/authorship/run.sh

# Check E2E reviewer hardening and authorship scripts without launching the server
check-e2e-authorship:
    bash -n tests/e2e/authorship/run.sh
    bash -n tests/e2e/authorship/validate.sh


# Run E2E agent lifecycle invariants test
e2e-lifecycle:
    ./tests/e2e/lifecycle/run.sh

# Run the cross-provider one-shot lifecycle E2E with a deterministic Codex fixture
e2e-one-shot-lifecycle:
    ./tests/e2e/one-shot-lifecycle/run.sh

# Check the one-shot lifecycle E2E harness without launching server or tmux
check-e2e-one-shot-lifecycle:
    bash -n tests/e2e/one-shot-lifecycle/run.sh
    bash -n tests/e2e/one-shot-lifecycle/fake-codex.sh
    {{py}} -m py_compile tests/e2e/one-shot-lifecycle/validate.py tests/e2e/one-shot-lifecycle/mock_forgejo.py

# Drive the real exomonad init binary + tmux through Server/Watcher/TL crash
# recovery, concurrent-init locking, and nonterminal-checkpoint continuation
# through the embedded controller, watcher, and ledger (chainlink #907)
e2e-init-recovery:
    ./tests/e2e/init-recovery/run.sh

# Check the init-recovery E2E harness without launching tmux
check-e2e-init-recovery:
    bash -n tests/e2e/init-recovery/run.sh
    {{py}} -m py_compile tests/e2e/init-recovery/seed_checkpoint.py tests/e2e/init-recovery/seed_publication.py

# Run the real-server --continue identity-preservation and corruption
# classification acceptance scenario (chainlink #1019).
e2e-init-continue:
    nix develop --command cargo build -p exomonad
    just wasm devswarm
    ./tests/e2e/init-continue/run.sh

# Check the --continue acceptance harness without launching tmux or a server.
check-e2e-init-continue:
    bash -n tests/e2e/init-continue/run.sh
    {{py}} -m py_compile tests/e2e/init-continue/run.py
    test -s tests/e2e/init-continue/e2e-test.md
    test -s tests/e2e/init-continue/testrunner.md

# Check E2E agent lifecycle scripts without launching the server
check-e2e-lifecycle:
    bash -n tests/e2e/lifecycle/run.sh
    bash -n tests/e2e/lifecycle/validate.sh

# Run E2E root idle/shutdown convergence test
e2e-idle-shutdown:
    ./tests/e2e/idle-shutdown/run.sh

# Check E2E root idle/shutdown scripts without launching the server
check-e2e-idle-shutdown:
    bash -n tests/e2e/idle-shutdown/run.sh
    bash -n tests/e2e/idle-shutdown/validate.sh


# Run E2E Chainlink SessionStart env failsafe test
e2e-chainlink-env-failsafe:
    ./tests/e2e/chainlink-env-failsafe/run.sh

# Check E2E Chainlink SessionStart env failsafe harness script without launching the server
check-e2e-chainlink-env-failsafe:
    bash -n tests/e2e/chainlink-env-failsafe/run.sh

# Run live continuation-brief root SessionStart E2E
e2e-continuation-brief:
    ./tests/e2e/continuation-brief/run.sh

# Check continuation-brief E2E harness without launching the server
check-e2e-continuation-brief:
    bash -n tests/e2e/continuation-brief/run.sh

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


# Run live E2E Teams messaging test (requires active CC team "teams-e2e")
live-teams-e2e:
    nix develop --command cargo nextest run -p claude-teams-bridge --test integration live_teams_e2e --run-ignored ignored-only --no-capture

# Validate the Phase 0 observability registries and synthetic fixtures
validate-observability-contracts:
    {{py}} scripts/validate_observability_contracts.py

# Smoke-test the allowlist-first, deterministic Failure Atlas compiler
test-observability-export:
    {{py}} scripts/test_compile_failure_atlas.py

# Smoke-test detector, incident, adjudication, and controlled-contrast gates
test-observability-measurement:
    {{py}} scripts/test_failure_atlas_measure.py


# Run E2E review-loop stuck human escalation test
e2e-review-loop-stuck:
    ./tests/e2e/review-loop-stuck/run.sh

# Check E2E review-loop stuck harness without launching the server
check-e2e-review-loop-stuck:
    bash -n tests/e2e/review-loop-stuck/run.sh
    {{py}} -m py_compile tests/e2e/mock_github.py

# Run E2E Codex reviewer sandbox/instructions consistency test
e2e-codex-reviewer-sandbox:
    ./tests/e2e/codex-reviewer-sandbox/run.sh

# Check E2E Codex reviewer sandbox harness without launching the server
check-e2e-codex-reviewer-sandbox:
    bash -n tests/e2e/codex-reviewer-sandbox/run.sh
    {{py}} -m py_compile tests/e2e/mock_github.py

# Forgejo CI migration E2E
# Requires: forgejo/docker-compose.yml stack and EXOMONAD_FORGEJO_TOKEN
# export EXOMONAD_FORGEJO_TOKEN=...
e2e-forgejo-ci:
    ./tests/e2e/forgejo-ci/run.sh

check-e2e-forgejo-ci:
    bash -n tests/e2e/forgejo-ci/run.sh
