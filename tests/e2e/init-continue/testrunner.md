# Running the --continue acceptance

Use just check-e2e-init-continue for shell, Python, and documentation checks
without starting tmux or a server. Use just e2e-init-continue for one
real-server run, or repeat it three times for the acceptance gate.

The result JSON is written inside the disposable E2E work directory and is
derived only from invocation files, published-heads.json, the checkpoint, and
the authoritative ledger. Set KEEP_E2E_WORKDIR=1 when investigating a failure.
