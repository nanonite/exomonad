---
paths:
  - "**"
---

# Orientation — ExoMonad Workspace Agent

## What — the work

Building and extending ExoMonad itself: the TL-to-worker orchestration framework. Typical sessions involve:

- Implementing new PR workflow methods or ideology (e.g., local PR registry, merge gates, reviewer agent lifecycle)
- Integrating new CI infrastructure — Tangled knot + spindle as a fully local development environment, wired into exomonad's test suite
- Extending or onboarding new agent types (OpenCode) so they fit within the exomonad effects and messaging workflow
- Closing gaps in the messaging protocol mismatch: Claude Code has native Teams inbox; Gemini and OpenCode do not — bridging that is active work
- Using chainlink as the coordination layer: chainlink issues delegate work to worker agents, Claude Code reviews commits and writes markdown feedback, that feedback routes back through chainlink to the workers
- Writing and maintaining documentation of architecture decisions — many of which currently live only in the operator's head

The issue tracker is chainlink (`.chainlink/issues.db`).

## Where — the context

- Claude Code instance running inside the exomonad workspace itself (`/home/goya/agent-workspace/exomonad`)
- Primary tools: chainlink CLI (issue tracking and delegation), `just` + nix for builds and tests, `cargo` / `cabal` for the Rust and Haskell toolchains, `gh` for GitHub operations, Tangled + spindle for local CI
- Haskell WASM is the tool/hook/event DSL; Rust is the I/O runtime — all tool definitions live in Haskell, all effects executed in Rust
- `just build` for Rust-only, `just install-all-dev` for full install (WASM + binary), `just wasm-all` for WASM only — never raw `cargo build` or `cp ~/.cargo/bin/exomonad` (nix shell required; atomic rename required)
- Tangled+spindle integration is complete: `just e2e-tangled-ci` runs the full local CI pipeline end-to-end

## Who — the user

Roger. More experienced in Rust than Haskell — has not had dedicated time to learn Haskell, but finds the patterns legible enough to extend. Strongest on orchestration and architecture; weakest on the Haskell WASM internals and the messaging protocol mismatches between agent runtimes.

The architecture is well-formed in his head but poorly externalized — design decisions and reasoning are not yet captured in documentation, which blocks new users and new agents from extending the system confidently. Closing that gap is a standing priority alongside feature work.

## How — the interaction

- **Push back directly** when something conflicts with the architecture. If the agent is pushing back, the architecture is in context — no need to re-explain it; the pushback is the signal.
- **Always ask before acting.** No autonomous decisions.
- **Hard no on incomplete features.** Every feature needs associated tests. Shipping something without a test is not a done state.
- The agent should flag missing documentation, missing tests, or design decisions that aren't externalized — these are bugs, not cosmetic issues.
