# Mixed Agent Chain E2E Validator

This test validates the pane-based tmux delivery path from a Claude TL to an OpenCode worker spawned with `spawn_worker`, with Codex configured as the reviewer runtime.

The automated validator in `validate.sh` checks:

1. Fixture config sets `root_agent_type = "claude"`.
2. Fixture config sets `spawn_agent_type = "opencode"`.
3. Fixture config sets `reviewer.agent_type = "codex"`.
4. OpenCode worker agent config exists with role `worker`.
5. Worker `routing.json` contains a pane id.
6. The worker pane capture contains `[TL2WORKER-INJECTED]`.
7. Logs record successful `send_tmux_message` delivery to the OpenCode worker.
8. Logs record the worker `notify_parent` acknowledgement back to the Claude TL.
9. Logs record the Claude TL completion notification.

Run manually with:

```bash
just e2e-tl-to-worker-messaging
```

Cheap syntax check:

```bash
just check-e2e-tl-to-worker-messaging
```
