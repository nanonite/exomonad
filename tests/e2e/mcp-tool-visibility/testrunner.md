# MCP Tool Visibility E2E Test Runner

This E2E test is automated by `run.sh`. It loads the production devswarm WASM plugin and asserts that `handle_list_tools` for each role matches the canonical matrix in `docs/architecture/agent-system.md`.

No agent should edit files during this test. On failure, read the markdown diff table printed by the Rust test and report whether the documentation or tool registration is wrong.
