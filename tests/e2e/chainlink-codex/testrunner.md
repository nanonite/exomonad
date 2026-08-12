# E2E Chainlink Codex Validator

This test validates the root Codex and direct Codex dev leaf Chainlink MCP flow:

`Codex root` creates a Chainlink issue and spawns a dev leaf -> the leaf checks/starts/marks/ends a session and notifies the root -> root closes the issue without Chainlink locks.

Run it through:

```bash
just e2e-chainlink-codex
```

For harness-only validation:

```bash
just check-e2e-chainlink-codex
```
