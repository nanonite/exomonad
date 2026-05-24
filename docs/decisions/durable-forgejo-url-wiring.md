# Durable Forgejo URL Wiring

**Date:** 2026-05-24

**Chainlink:** #388

## Context

The local Forgejo stack has four network perspectives that currently get conflated:

| Perspective | Process | Current fragile value |
|-------------|---------|-----------------------|
| Host | `exomonad new`, `exomonad init`, user browser, `gh` | `http://localhost:3000` or `http://127.0.0.1:3000` |
| Runner | `forgejo-runner register` and daemon inside Compose | `http://172.17.0.1:3000` during manual registration |
| Docker daemon | `docker:dind` serving job containers | implicit Compose service network |
| Job containers | Forgejo Actions checkout/cache/upload traffic | whatever act infers from the runner config |

The hardcoded Docker bridge address is the problem. `172.17.0.1` is a host implementation detail, changes across Docker Desktop/Podman/rootless setups, and does not describe which network boundary is being crossed. The durable design must name each boundary explicitly and generate runner config from those names.

## Decision

Keep the host-facing URL as the public ExoMonad/CLI URL, and introduce a separate internal Actions URL used only by the Forgejo Compose stack:

| Name | Default | Consumers |
|------|---------|-----------|
| `forgejo_url` | `http://localhost:3000` | Host CLI, browser, ExoMonad API client, git remote rewrite |
| `forgejo_actions_url` | `http://forgejo:3000` | Forgejo `DEFAULT_ACTIONS_URL`, runner registration, runner daemon |
| `forgejo_job_network` | `forgejo-ci` | Runner `container.network` for job containers |
| `forgejo_cache_host` | `forgejo-runner` | Runner cache URL advertised to job containers |

`forgejo_url` remains the only URL stored in normal project config unless the user needs to override the local stack. The Docker stack owns the internal defaults through environment variables and generated runner config.

## Compose Wiring

Use an explicit named bridge network for the Forgejo stack:

```yaml
networks:
  forgejo-ci:
    name: forgejo-ci

services:
  forgejo:
    networks:
      forgejo-ci:
        aliases: [forgejo]
    environment:
      - FORGEJO__actions__DEFAULT_ACTIONS_URL=http://forgejo:3000

  docker-in-docker:
    networks: [forgejo-ci]

  runner:
    networks:
      forgejo-ci:
        aliases: [forgejo-runner]
    environment:
      - FORGEJO_INSTANCE_URL=http://forgejo:3000
      - FORGEJO_JOB_NETWORK=forgejo-ci
      - FORGEJO_CACHE_HOST=forgejo-runner
      - DOCKER_HOST=tcp://docker-in-docker:2375
```

The runner config must set:

```yaml
container:
  network: forgejo-ci

cache:
  host: forgejo-runner
```

This makes job containers join the same named Docker network as Forgejo and the runner cache proxy. Checkout URLs, log upload URLs, and cache URLs resolve by service name instead of by a bridge IP.

## Registration Flow

The registration command should stop embedding `172.17.0.1` and use the internal URL:

```bash
forgejo-runner register \
  --no-interactive \
  --instance "${FORGEJO_INSTANCE_URL:-http://forgejo:3000}" \
  --token "$FORGEJO_RUNNER_TOKEN" \
  --name "${FORGEJO_RUNNER_NAME:-local-runner}" \
  --config /data/runner-config.yml
```

For manual host registration, users can still run the command with `--instance http://localhost:3000`. The checked-in local stack should prefer in-network registration so the generated `.runner` file is consistent with daemon runtime.

## Implementation Plan

1. Add optional config fields to `Config` only if they are needed outside the Forgejo Compose stack:
   - `forgejo_actions_url`
   - `forgejo_job_network`
   - `forgejo_cache_host`

2. Update `forgejo/docker-compose.yml`:
   - Add a named `forgejo-ci` network.
   - Set `FORGEJO__actions__DEFAULT_ACTIONS_URL=http://forgejo:3000`.
   - Put `forgejo`, `runner`, and `docker-in-docker` on `forgejo-ci`.
   - Pass `FORGEJO_INSTANCE_URL`, `FORGEJO_JOB_NETWORK`, and `FORGEJO_CACHE_HOST` to the runner.

3. Update `forgejo/runner-entrypoint.sh`:
   - Generate config if missing.
   - Patch `container.network` to `$FORGEJO_JOB_NETWORK`.
   - Patch `cache.host` to `$FORGEJO_CACHE_HOST`.
   - Keep the existing `/var/run/act` tmpfs patch.
   - Optionally auto-register when `FORGEJO_RUNNER_TOKEN` is present and `/data/.runner` is missing.

4. Update `forgejo/README.md`:
   - Document `forgejo_url` for host/ExoMonad use.
   - Document `FORGEJO_INSTANCE_URL` for runner-in-Compose use.
   - Remove the Docker bridge IP from first-time setup.

5. Update `tests/e2e/forgejo-ci/run.sh`:
   - Use `EXOMONAD_FORGEJO_URL=http://localhost:3000` for host-side API checks.
   - Assert the generated runner config contains `container.network: forgejo-ci` and `cache.host: forgejo-runner` when runner setup is exercised.

## Done Criteria

- No checked-in docs or scripts instruct users to register runners with `http://172.17.0.1:3000`.
- Host-side ExoMonad config continues to use `http://localhost:3000` by default.
- Runner and job-container traffic use stable Compose names on the named `forgejo-ci` network.
- Fresh local setup works on Docker bridge configurations where `172.17.0.1` is not the host gateway.
