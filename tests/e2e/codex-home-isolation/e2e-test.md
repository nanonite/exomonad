# Codex Home Isolation (contract)

The acceptance for this harness is `test_contract.py`, run by
`just check-e2e-codex-home-isolation`. It needs no server, no tmux session, and
no `codex` binary, which is the point: this is the fast gate that runs before
anyone spends a live Codex run.

## What it proves

ExoMonad seeds Codex hook trust by rewriting the Codex *user* config that
`codex_config::codex_user_config_path()` resolves: `$CODEX_HOME/config.toml`
when that variable is set, `~/.codex/config.toml` when it is not. Every E2E
that starts a real ExoMonad process with a Codex agent type therefore reaches
that write, and a harness that does not set `CODEX_HOME` makes the write land
on the operator's own configuration.

Three properties are checked.

**The host config survives.** A run seeds hook trust exactly the way
`install_codex_hook_trust` does — append to the Codex user config — and the host
`config.toml` must come out byte-for-byte identical. The helper records a
sha256 of the host file before any ExoMonad process starts and re-checks it at
teardown. A modified host file fails the sentinel; the tests prove the sentinel
actually fails rather than merely existing.

**The home is run-scoped.** `CODEX_HOME` is created under the run's own work dir
and exported before any ExoMonad process starts. A home outside the run is
rejected: two runs sharing one Codex home race on the trust lock, and one run's
teardown deletes the other's state.

**No harness arranges its own.** Harnesses built on `lib/harness.sh` inherit
isolation from `e2e_create_work_dir` and the sentinel from `e2e_cleanup`, so
there is nothing for a scenario to forget. Bespoke harnesses call the shared
helper directly. Neither group keeps a private `CODEX_HOME_DIR`, names a
`codex-home` path itself, or reads `$HOME/.codex/config.toml`.

## Anti-patterns this replaces

- **Copy the host `config.toml` into the run and restore it afterwards.**
  ExoMonad rewrites the file in place, so a restore that does not run — because
  the run died, or because teardown was interrupted — leaves the operator's
  config corrupted. Isolation makes the restore unnecessary.
- **Rely on each scenario remembering to export `CODEX_HOME`.** The shared
  harness does it for every harness that reaches `e2e_create_work_dir`.
- **Copying more than the documented auth artifacts.** Only `auth.json` and
  `installation_id` are credentials a live `codex` process needs, and only the
  live-Codex harnesses copy them. A harness with a fixture `codex` binary pulls
  no credentials into its work dir at all.

## KEEP_E2E_WORKDIR

`KEEP_E2E_WORKDIR=1` keeps the isolated home along with the rest of the work
dir, so a preserved run can still be inspected. The sentinel is *not* skipped
when a work dir is kept: the host config lives outside the work dir, so keeping
state must not also keep the check.
