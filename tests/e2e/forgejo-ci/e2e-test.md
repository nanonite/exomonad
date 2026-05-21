# Forgejo CI Pipeline E2E

This test validates the Forgejo CI migration path:

1. Brings up local Forgejo/runner stack from `forgejo/docker-compose.yml`.
2. Bootstraps a fresh workspace with `exomonad new`.
3. Verifies `.github/workflows/ci.yml` is generated.
4. Verifies Forgejo remote registration in the workspace git config.
5. Confirms logs indicate webhook registration path or graceful skip behavior.
