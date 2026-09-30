# Codex Home Isolation — Test Runner

The contract tests are the whole acceptance here. Run them with:

```bash
just check-e2e-codex-home-isolation
```

That recipe does the static syntax checks (`bash -n`) on the shared helper and
every changed harness, compiles the Python helper and the contract test, runs the
contract tests, and shellchecks the changed harnesses when `shellcheck` is on
`PATH`.

To run the tests alone:

```bash
python3 -m pytest -q tests/e2e/codex-home-isolation/test_contract.py
```

There is no `e2e-codex-home-isolation` recipe and there should not be one. This
harness has no live scenario: the thing it protects is the *absence* of a host
side effect, which a real Codex run cannot demonstrate any better than a real
Codex run can demonstrate that a `sleep` returned.

## What is covered

| Group | What it checks |
|---|---|
| Shell helper | creates and exports a run-scoped `CODEX_HOME`, copies only `auth.json` and `installation_id`, never copies/moves/restores the host config, honours `KEEP_E2E_WORKDIR`, is safe to source twice |
| Sentinel | a run that appends to the isolated config leaves the host config's sha256 unchanged; a tampered host config fails |
| Python helper | `codex_home.isolate` is run-scoped, copies credentials only on request, refuses when the host has none; `assert_untouched` detects a modified host config |
| Wiring | every Codex-generating shell harness reaches the shared helper; library-based harnesses inherit it rather than duplicating it; the Python acceptances isolate; only live-Codex harnesses copy credentials |

## Related

- `docs/decisions/e2e-harness-library.md` — where the shared harness boundary
  is drawn, and why Codex isolation belongs in the library.
- `rust/exomonad-core/src/codex_config.rs` — `codex_user_config_path()`, the
  resolution this contract exists to bound.
- `rust/exomonad/src/init.rs` — propagates `CODEX_HOME` into the tmux session
  env; `services/agent_control/internal.rs` propagates it into every spawned
  agent's env. A harness that exports it after those processes start does not
  have isolated anything, which is why isolation happens at work-dir creation.
