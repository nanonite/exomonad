# E2E Chainlink Codex Validator

This test validates the Codex TL and Codex worker Chainlink MCP flow:

`Codex root` spawns `Codex TL` -> TL creates a Chainlink issue and checks session status -> TL spawns `Codex worker` -> worker starts/marks/ends a session and notifies the TL -> TL closes the issue without Chainlink locks.

Run it through:

```bash
just e2e-chainlink-codex
```

For harness-only validation:

```bash
just check-e2e-chainlink-codex
```
