# TL-to-Worker Messaging E2E Validator

This test validates the pane-based tmux delivery path from a Codex TL to an OpenCode worker spawned with `spawn_worker`.

The automated validator in `validate.sh` checks:

1. Codex root config exists.
2. Codex TL worktree config exists with role `tl`.
3. OpenCode worker agent config exists with role `worker`.
4. Worker `routing.json` contains a pane id.
5. The worker pane capture contains `[TL2WORKER-INJECTED]`.
6. Logs record successful `send_tmux_message` delivery to the OpenCode worker.
7. Logs record the worker `notify_parent` acknowledgement back to the Codex TL.
8. Logs record the Codex TL completion notification back to root.

Run manually with:

```bash
just e2e-tl-to-worker-messaging
```

Cheap syntax check:

```bash
just check-e2e-tl-to-worker-messaging
```
