# Reviewer Hardening and Authorship Preservation E2E

This harness verifies the composite provenance contract from Chainlink #301 without using an LLM testrunner.

Phases:

1. Create a temp repository and a distinct dev-leaf commit authored by `authorship-worker@example.com`.
2. Start a real ExoMonad server with the devswarm WASM guest.
3. Send reviewer-role PreToolUse payloads for `Write` and `Edit`; both must be denied.
4. Send reviewer-role PreToolUse payloads for `git commit -am 'fix'`; it must be denied.
5. Send reviewer-role PreToolUse payloads for `git status` and `git rev-parse HEAD`; both must be allowed, then the commands must succeed in the repo.
6. Merge the dev branch into `main` without rewriting it and assert `git log -1 main --format=%ae` is the dev-leaf email, not the TL email.
