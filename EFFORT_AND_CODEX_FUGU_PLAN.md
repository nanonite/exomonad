# Role-Specific Effort Levels

## Summary

Add independent effort-level defaults for root TLs, spawned workers, and reviewers, with agent companions inheriting the worker setting. Apply effort through each capable harness's native interface, validate model-specific support before side effects whenever a model catalog is available, and give actionable feedback when a harness or model cannot honor the requested level.

Note: this plan originally also added `codex-fugu` as a first-class ExoMonad harness. That support was removed after diagnosis showed Sakana's Fugu models declare no MCP tool support in Codex's model catalog, making the harness unusable for ExoMonad's orchestration roles — see `docs/decisions/codex-integration.md` § Codex-Fugu (removed). The Codex-Fugu-specific sections below are retained as historical design context only; they are no longer implementation requirements. The effort-level machinery they depended on (typed `EffortLevel`, per-role resolution, Claude/OpenCode/Codex propagation) remains shipped and current.

## Fixed Product Decisions

- Public effort values are `low`, `medium`, `high`, `xhigh`, and `max`.
- Startup flags are:
  - `--tl-effort-level <LEVEL>`
  - `--worker-effort-level <LEVEL>`
  - `--reviewer-effort-level <LEVEL>`
- Configuration fields are:

  ```toml
  tl_effort_level = "medium"
  worker_effort_level = "medium"

  [reviewer]
  effort_level = "medium"
  ```

- CLI values override the corresponding config values.
- Each omitted role setting defaults to `medium`, except a role using `codex-fugu` defaults to `high` because Fugu rejects `medium`.
- Agent companions inherit `worker_effort_level`. Process companions have no model and do not receive an effort setting.
- Spawn tools do not expose per-call effort overrides in this version. Every spawned TL, leaf, and ephemeral worker uses the resolved worker effort.
- An explicitly requested level that is unsupported by an effort-capable selected model is rejected before tmux session recreation, worktree creation, pane creation, or reviewer launch.
- Harnesses without a stable generic effort interface continue without an effort override and produce a concise role-specific notice. This initially applies to Gemini and Shoal.

## Anti-Patterns

- Do not reintroduce one global effort value; TL, worker, and reviewer settings are independent.
- Do not silently clamp unsupported levels or silently replace explicit user input with a model default.
- Do not send `medium` to Fugu in unit, integration, or end-to-end tests.
- Do not treat `codex-fugu` as a string alias for Codex. It needs a distinct typed agent identity, command, suffix, configuration value, and model list.
- Do not duplicate Codex hooks or introduce a new Fugu hook protocol. Fugu runs through Codex and must reuse `HookRuntime::Codex`.
- Do not assume OpenCode ignores effort. It supports model variants and provider-specific `reasoningEffort` configuration.
- Do not mutate user-level Codex/Fugu profile files. ExoMonad only writes its existing project/agent-local configuration and invokes the installed wrapper.
- Do not begin implementation until the Chainlink epic and subissues created from this plan are assigned for an implementation session.

## Public Interfaces and Resolution

### Typed effort configuration

Introduce a shared `EffortLevel` enum with parsing, display, serde, and Clap value-enum support. Preserve the source of every resolved value (`CLI`, `config`, or `default`) so the Fugu-specific omitted-value default can be distinguished from an explicit `medium`.

Resolve three independent policies:

| Policy | Applies to |
|---|---|
| TL effort | Root TL command and root runtime config |
| Worker effort | Forked TLs, dev leaves, ephemeral workers, and agent companions |
| Reviewer effort | Automatically spawned reviewers |

Startup validation must finish before any existing session is killed for `--recreate`. Spawn-time validation must run before creating a worktree or pane when a spawn supplies or resolves a model not known during `init`.

### Harness-specific application

#### Claude

- Pass `--effort <LEVEL>` to root, forked, worker, companion, and reviewer commands.
- Treat the five public levels as harness-supported because they are accepted by the installed Claude CLI.
- Keep existing model validation and include role/model/effort context if Claude rejects a model-specific combination.

#### OpenCode

- Treat OpenCode as effort-capable.
- For `opencode run`, forked sessions, workers, and non-interactive launches, pass `--variant <LEVEL>`.
- For interactive root or companion launches, add an ExoMonad-managed agent profile to the generated `opencode.json` with `reasoningEffort: <LEVEL>` and launch it with `--agent <generated-name>`, because the interactive top-level command does not expose `--variant`.
- Parse `opencode models --verbose`; its model records contain a `variants` map. When the role's model is configured, reject a requested level that is absent from that map and list the available variants.
- When OpenCode selects its own model because no model was configured, pass the requested variant and clearly report that model-specific validation is deferred to OpenCode. Preserve OpenCode's unsupported-variant error verbatim with role context instead of substituting a level.
- Ensure generated profiles retain the existing MCP, instructions, plugin, and permission fields.

#### Codex

- Pass `-c model_reasoning_effort="<LEVEL>"` in generated root, fork, worker, companion, and reviewer commands.
- Render the same `model_reasoning_effort` into agent-local `.codex/config.toml` so manual restarts retain the resolved value.
- Parse `codex debug models` and validate explicitly configured models against `supported_reasoning_levels`.
- If no model is explicitly configured, allow Codex to select its model and retain its native error if the selected model rejects the level.

#### Codex-Fugu

- Use Codex command/config rendering with the `codex-fugu` executable.
- Support explicit models `fugu` and `fugu-ultra` only.
- Support effort levels `high`, `xhigh`, and `max`; `max` is accepted as the provider-compatible alias of `xhigh`.
- Resolve an omitted effort to `high` independently for each role using Fugu.
- Reject explicit `low` or `medium` before side effects and recommend `high`, `xhigh`, or `max`.
- Fail early with installation guidance when `codex-fugu` is not executable on `PATH`.

#### Gemini and Shoal

- Do not inject an undocumented effort flag or provider option.
- Continue startup/spawn and emit one concise notice identifying the affected role and harness.
- Process companions do not produce this notice because effort is inapplicable rather than unsupported.

## Codex-Fugu Agent-Type Integration

Add `CodexFugu` to the Rust service agent type and the protobuf agent type, with explicit wire/string spelling `codex-fugu`. Update every exhaustive conversion and routing match, including:

- CLI/config/environment parsing and valid-value diagnostics.
- Agent metadata with command `codex-fugu`, suffix `codex-fugu`, and an unambiguous display label.
- Directory and birth-branch inference for names ending in `-codex-fugu`.
- Spawn request/result conversions, lifecycle records, agent filters, cleanup, delivery, watcher role selection, and reviewer tracking.
- Haskell tool descriptions and any accepted agent-type enumerations exposed through WASM.

Reuse the Codex path for:

- `.codex/config.toml` rendering and project trust.
- ExoMonad MCP registration and developer instructions.
- PreToolUse, PostToolUse, and Stop hooks using `HookRuntime::Codex`.
- Role sandbox profiles, durable inbox/tmux delivery, fork/resume behavior, reviewer instructions, and Forgejo review submission.

Extend `exomonad models codex-fugu` to list `fugu` and `fugu-ultra`, and include `codex-fugu` in all root, worker, reviewer, companion, and model-command help text.

## User Feedback and Help

Expand `exomonad init --help` to explain:

- The three role-specific flags and accepted values.
- CLI-over-config precedence.
- Worker inheritance by forked TLs, leaves, ephemeral workers, and agent companions.
- The general `medium` default and per-role Fugu `high` exception.
- Strict rejection of explicit unsupported model/effort combinations.
- OpenCode variant handling and notices for harnesses without effort support.

Before launching, log/print a compact resolved summary containing each role's harness, model when known, requested/source effort, and effective effort or ignored status. Error messages must provide the exact corrective flag, for example `--reviewer-effort-level high`.

## Test Plan

### Unit and integration tests

- Parse and serialize all five effort levels and reject unknown values through Clap and TOML.
- Verify independent CLI/config/default precedence for TL, worker, and reviewer policies.
- Verify companions receive worker effort and process companions receive none.
- Verify spawned TLs, leaves, and ephemeral workers use worker effort while reviewers use reviewer effort.
- Verify omitted Fugu effort becomes `high` separately for TL, worker/companion, and reviewer roles.
- Verify explicit Fugu `low` and `medium` fail before mocked session/worktree/pane side effects.
- Verify Claude command rendering includes the correct role effort.
- Verify OpenCode run/fork commands use `--variant`, interactive configs use `reasoningEffort`, and verbose catalog fixtures reject absent variants with supported-value feedback.
- Verify Codex command and config rendering use matching `model_reasoning_effort` values and catalog fixtures enforce supported levels.
- Verify mixed harness configurations apply, validate, or report ignored effort independently per role.
- Verify `CodexFugu` serde/protobuf round trips, suffix parsing, command selection, config reuse, delivery, lifecycle, cleanup, and watcher behavior.
- Verify CLI help and `exomonad models` output cover role effort and `codex-fugu`.

### End-to-end tests

- Add a focused Codex-Fugu E2E that starts with explicit `--tl-effort-level high`, `--worker-effort-level high`, and `--reviewer-effort-level high` for every live Fugu role involved.
- Never run a live Fugu E2E with `medium`. Cover `medium` only as a pre-launch rejection test using fake commands or unit/integration fixtures.
- Assert generated Fugu commands and `.codex/config.toml` contain `high` and contain no accidental `medium`.
- Exercise at least root-to-leaf spawning and completion delivery with `codex-fugu`; cover reviewer command/config construction deterministically if a full live review cycle would make the E2E non-hermetic.
- Add deterministic fake-CLI coverage for Claude, OpenCode, Codex, and Fugu validation/error paths so CI does not depend on credentials.

### Verification commands

```bash
just fmt
just build
just test-cargo-all
just test-wasm-integration
just check-e2e-codex-hooks
just check-e2e-codex-messaging
just check-e2e-codex-fugu
just e2e-codex-fugu
```

The live Fugu command is credential-gated; all deterministic checks must pass without external credentials.

## Documentation Updates During Implementation

- Update root and Rust `CLAUDE.md` documentation for role effort resolution, OpenCode variants, and Codex-Fugu.
- Update the Codex integration decision record or add a focused Codex-Fugu decision record explaining why it is a distinct agent type but shares the Codex runtime protocol.
- Update the runtime-role E2E matrix and user-facing configuration examples.
- Keep help text, config examples, and model listings synchronized with the typed accepted values.

## Chainlink Decomposition

Parent epic: **Add role-specific effort levels and Codex Fugu harness support**

Planned subissues:

1. **Add typed role-specific effort configuration and validation**
   - Own `EffortLevel`, config/CLI precedence, explicit/default tracking, resolved summaries, and side-effect-free preflight APIs.
2. **Propagate effort levels across agent roles and companions**
   - Own Claude, OpenCode, and Codex command/config propagation; worker inheritance; reviewer separation; and OpenCode/Codex catalog validation.
3. **Add Codex Fugu as a supported agent harness**
   - Own typed agent integration, command/config reuse, Fugu defaults and validation, model listing, and all-role availability.
4. **Improve effort and harness CLI guidance**
   - Own help text, actionable errors, configuration examples, architecture/decision documentation, and runtime-role matrix updates.
5. **Add effort and Codex Fugu integration coverage**
   - Own cross-role fixtures, command/config tests, fake CLI validation, and the high-effort-only live Fugu E2E.

Dependencies:

- Propagation depends on typed effort configuration and validation.
- Codex-Fugu depends on the typed effort interface but can otherwise proceed independently of generic harness propagation.
- CLI/documentation depends on the typed public interface and Codex-Fugu naming being stable.
- Integration/E2E coverage depends on both implementation tracks and the final user-facing interface.

## Done Criteria

- All three role-specific effort settings resolve independently with documented precedence.
- Agent companions inherit worker effort; process companions remain unaffected.
- Claude, OpenCode, Codex, and Codex-Fugu receive effort through their supported interfaces.
- Unsupported explicit effort/model combinations fail with corrective feedback before destructive or spawn side effects whenever the selected model is known.
- Codex-Fugu works as a distinct root, worker, companion, and reviewer harness while sharing Codex runtime integration.
- No live Fugu test sends `medium`.
- Unit, integration, deterministic E2E checks, documentation, and credential-gated Fugu E2E satisfy the verification section.
