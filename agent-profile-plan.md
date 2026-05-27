# agent profile propagation plan

## problem summary

when `exomonad init` starts an opencode tl, spawned workers default to gemini because:
1. the role context template (`tl.md`) is static — no instruction about which agent type to use
2. `spawn_worker` proto default is `UNSPECIFIED` → gemini
3. no session-level "profile" flows down the spawn tree

sub-tl worktrees also lose root workspace context (chainlink tasks, agent.md) because
their cwd is the worktree, not the project root.

---

## step 1 — add `spawn_agent_type` to config

**file:** `rust/exomonad/src/config.rs`

add field to `RawConfig` and `Config`:
```toml
spawn_agent_type = "opencode"  # agent type for spawn_worker and fork_wave children
```

resolution order: `local.spawn_agent_type > global.spawn_agent_type > AgentType::Gemini`

propagate the value into `AgentControlService` alongside the existing `root_agent_type`.

---

## step 2 — add `--opencode` and `--claude-code` init flags

**file:** `rust/exomonad/src/init.rs`

| flag | `root_agent_type` | `spawn_agent_type` |
|---|---|---|
| `--opencode` | opencode | opencode |
| `--claude-code` | claude | claude |
| `--tl=X --worker=Y` | x | y |
| _(default)_ | claude | gemini (unchanged) |

`--opencode` sets both fields. `--claude-code` sets both. `--tl`/`--worker` set each independently.

---

## step 3 — inject `EXOMONAD_SPAWN_AGENT_TYPE` in common_spawn_env

**file:** `rust/exomonad-core/src/services/agent_control/internal.rs`

add to `common_spawn_env`:
```rust
env_vars.insert(
    "EXOMONAD_SPAWN_AGENT_TYPE".to_string(),
    self.spawn_agent_type.suffix().to_string(),
);
```

`spawn_agent_type` lives on `AgentControlService` config, already accessible in `internal.rs`.
propagates automatically to all dynamically spawned agents (subtrees, leaves, workers).

---

## step 4 — interpolate role context template at spawn time

**file:** `rust/exomonad-core/src/services/agent_control/spawn.rs`

in the existing `resolve_role_context` → copy path, read the template file content and
replace `{{spawn_agent_type}}` before writing to `{worktree}/.claude/rules/exomonad_role.md`.

simple string substitution — no template engine needed:
```rust
let content = fs::read_to_string(&src).await?;
let interpolated = content.replace("{{spawn_agent_type}}", self.spawn_agent_type.suffix());
fs::write(&dest, interpolated).await?;
```

applies to both claude and opencode spawn paths (both write to `.claude/rules/exomonad_role.md`).

---

## step 5 — update tl.md template

**file:** `.exo/roles/devswarm/context/tl.md`

add to the worker spawning section:

```markdown
## spawning workers

when calling `spawn_worker`, always include `agent_type: {{spawn_agent_type}}`.
this session is configured to use **{{spawn_agent_type}}** for all spawned agents.
when calling `fork_wave`, set `agent_type` on each child to `{{spawn_agent_type}}` unless
the task explicitly requires a different agent type.
```

---

## step 6 — workspace context propagation

two sub-tasks:

### 6a — chainlink db path env var

inject `CHAINLINK_DB_PATH=<project_root>/.chainlink` (or whatever chainlink uses) in
`common_spawn_env`. sub-agents running `chainlink` commands will resolve to the root db
regardless of their cwd.

**research required** — see "chainlink mcp research note" below before implementing.

### 6b — agent.md injection into initial prompt

**file:** `rust/exomonad-core/src/services/agent_control/spawn.rs`

in `spawn_subtree`, after building `task_with_context`, check if `agent.md` exists at
`project_dir`. if so, append:

```
\n\n## workspace context\n{agent.md contents}
```

keeps sub-tl agents aware of workspace-level goals and conventions without requiring
file access outside the worktree.

---

## chainlink mcp research note

**do not implement until researched.**

before implementing step 6a manually, check whether chainlink exposes an mcp server
that could serve workspace context directly to agents. if chainlink has mcp support:
- agents could call a `chainlink::list_tasks` mcp tool instead of reading a db file
- no env var hack needed — the mcp server handles db resolution itself
- context injection into the initial prompt becomes less critical

research steps:
1. run `chainlink --help` and check for a `serve`, `mcp`, or `server` subcommand
2. check `/home/goya/agent-workspace/chainlink/` source for mcp-related code
3. check if chainlink has a config file that registers it as an mcp server
4. if mcp support exists, evaluate: add chainlink to `extra_mcp_servers` in `config.toml`
   and write a role context instruction to use it instead of bash `chainlink` commands

if chainlink has no mcp support, fall back to the env var approach in step 6a and
consider filing an issue in the chainlink repo to add it.

---

## sequencing

```
step 1 (config fields)
    │
    ├── step 2 (init flags)    — depends on step 1
    ├── step 3 (env var)       — depends on step 1
    └── step 4 (interpolation) — depends on step 1
            │
            └── step 5 (tl.md template) — can be done before step 4

step 6a (chainlink env var)    — blocked on research note
step 6b (agent.md injection)   — independent, can be done anytime
```

steps 2, 3, 4 can be done in parallel once step 1 lands.
step 5 (template edit) can be drafted now and merged with step 4.
