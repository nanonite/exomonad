# Chainlink DB Session-Start Failsafe

Chainlink DB safety has two layers:

- Spawn propagation sets `CHAINLINK_DB` to the project root `.chainlink` directory for every root, TL, dev, reviewer, and worker process.
- SessionStart hooks receive that agent-side value and refuse startup when it is unset, points at a missing path, or points at a directory without `issues.db`.

The hook does not auto-repair the environment. Repairing inside the hook would hide a broken spawn contract and allow phantom Chainlink trackers to diverge.

The E2E guard is `just e2e-chainlink-env-failsafe`; the cheap syntax preflight is `just check-e2e-chainlink-env-failsafe`.
