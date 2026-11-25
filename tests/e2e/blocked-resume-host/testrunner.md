# Test runner notes

Run the full compiled-host acceptance:

```bash
nix develop --command cargo build -p exomonad
just wasm devswarm
just e2e-blocked-resume-host
```

Run one case while iterating (the harness prints the case output as JSON):

```bash
./tests/e2e/blocked-resume-host/run.sh slow-adoption
./tests/e2e/blocked-resume-host/run.sh terminal-failure-hold
./tests/e2e/blocked-resume-host/run.sh skip-held-stage
```

`skip-held-stage` builds a run-scoped TL controller archive under the run's
`controller-home/.exo/tl_loop.pyz`, so the `exomonad init --continue` restart
validates the working tree under test rather than an older installed archive.

`--keep` retains a failed run directory under `/tmp/opencode` for inspection.
Do not add sleeps as assertions: each case waits on a marker file, a ledger
row, a host journal phase, or an owner window/routing record with a bounded
deadline.
