# E2E Harness Library

**Status:** Accepted

**Date:** 2026-05-30

**Chainlink:** #281

## Context

The E2E suite grew as standalone `tests/e2e/*/run.sh` scripts. Each script repeated the same setup: choose a safe temp root, initialize git, run `exomonad new`, install WASM and roles, write `.exo/config.toml`, start `exomonad serve` or `exomonad init`, wait for readiness, and clean up logs and tmux/server state.

That duplication made infrastructure bugs expensive. Path length, workspace contamination, `/tmp` behavior, cleanup drift, and preflight drift were all fixed test by test instead of once.

## Decision

Introduce `tests/e2e/lib/harness.sh` as the shared shell harness for current E2E tests. New and migrated shell E2Es should source it and use its helpers for:

- preflight checks for `exomonad`, required commands, and WASM guests;
- short, isolated work dirs under `${E2E_CACHE_ROOT:-$HOME/.cache/exomonad-e2e}`;
- cleanup with `KEEP_E2E_WORKDIR=1` support;
- temp git repository creation;
- `exomonad new` plus project-local WASM and role installation;
- basic `.exo/config.toml` generation;
- local `exomonad serve` launch and socket readiness waiting.

The library is intentionally shell-first. Most existing harnesses are shell scripts, and the high-risk bugs are around process environment and filesystem setup. A Rust harness can still wrap these concepts later, but it should not block removing duplicated shell bootstrap now.

## Boundaries

The shared harness owns fixture infrastructure. Test-specific prompts, payloads, validators, fake binaries, and expected assertions stay in each test directory.

Work dirs must stay outside the repository and outside `/tmp` by default. This keeps Unix socket paths short enough for Linux and avoids parent-walking into the live ExoMonad workspace.

Headless `exomonad serve` tests should prefer process validators. Interactive `exomonad init` tests may continue to attach tmux until they are migrated to a virtual TTY or a direct-server flow.

## First Migration

`tests/e2e/chainlink-sqlite-block/run.sh` is the first migrated test. It now uses `tests/e2e/lib/harness.sh` for preflight, temp repo setup, Chainlink initialization, WASM/role installation, server startup, socket readiness, and cleanup. Its runtime hook probes and sqlite fake remain local to the test.

## Migration Plan

Migrate tests in this order:

1. Local `exomonad serve` tests with process validators: Chainlink env failsafe, authorship, lifecycle, timer role scope.
2. Codex-only local tmux tests: Codex messaging, Chainlink Codex, sub-TL worker notify, recursive fork wave Codex.
3. OpenCode tests after observer/path assertions are stable.
4. Claude-only or mixed-runtime tests last, preserving explicit operator approval for token-burning runs.

Each migration must keep the old `just e2e-*` target name and add the library itself to the matching `check-e2e-*` syntax check.

## Consequences

E2E setup bugs now have one primary place to fix. Individual tests become smaller and easier to review, while still allowing special cases for fake binaries, companion validators, isolated `CODEX_HOME`, or live tmux sessions.
