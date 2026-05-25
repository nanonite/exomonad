# Forgejo Local CI

Forgejo + Forgejo Actions runner for local development CI.

## Start

```bash
docker compose up -d forgejo
# Wait for Forgejo to be healthy, then complete setup (see below)
```

After first-time setup, `docker compose up -d` starts Forgejo, the Docker-in-Docker daemon, and the runner. The dind service is health-gated so the runner waits for a reachable Docker daemon instead of crash-looping.

## First-Time Setup

1. Open http://localhost:3000 and complete the installation wizard:
   - Database: SQLite (default)
   - Admin username/password of your choice
   - Leave defaults for everything else

2. Generate a runner registration token:
   - Go to http://localhost:3000/-/admin/runners
   - Click "Create new runner" → copy the token

3. Register the runner (Linux — uses Docker bridge IP, not `host.docker.internal`):
   ```bash
   docker run --rm -v "$(pwd)/runner-data:/data" \
     data.forgejo.org/forgejo/runner:12 \
     forgejo-runner register \
       --no-interactive \
       --instance http://172.17.0.1:3000 \
       --token <token> \
       --name local-runner \
       --config /data/runner-config.yml
   ```

4. Start the CI stack:
   ```bash
   docker compose up -d
   ```

   The compose entrypoint patches the generated runner config to mount `/var/run/act` as tmpfs in job containers. Docker 29 rejects Forgejo Runner archive uploads through the `/var/run` symlink unless that path exists as a real mount. Runner registration persists in `./runner-data/.runner`, and the generated config persists in `./runner-data/runner-config.yml`, both through the bind-mounted `/data` volume.

5. Set config in `.exo/config.toml`:
   ```toml
   forgejo_url = "http://localhost:3000"
   forgejo_token = "<personal-access-token>"
   forgejo_webhook_secret = "<random-secret>"
   ```

## Ports

| Port | Service |
|------|---------|
| 3000 | Forgejo web UI + API |
| 2222 | Git SSH |


## Git Remote Token Auth

Agent worktrees inherit the parent repo's Git config, so use an HTTP origin with the local Forgejo personal access token when agents must push or fetch without SSH prompts:

```bash
git remote set-url origin http://forgejo_pat:<personal-access-token>@localhost:3000/<owner>/<repo>.git
git fetch origin
git push origin HEAD
```

`exomonad init` applies the same rewrite automatically when `forgejo_url` and a non-empty `forgejo_token` are set in `.exo/config.toml` and the current origin already points at the configured Forgejo host. The token is stored in local Git config and appears in `git remote -v`; keep this pattern to local Forgejo instances.

## gh CLI

Configure `gh` to use local Forgejo:
```bash
gh auth login --hostname localhost:3000 --git-protocol http
# or set per-command:
GH_HOST=localhost:3000 GH_TOKEN=<token> gh pr list
```

## Stop

```bash
docker compose down        # keep data
docker compose down -v     # wipe data
```

## Troubleshooting

**Runner waits for Docker-in-Docker health.**
The runner executes jobs inside the `docker-in-docker` (dind) service via `DOCKER_HOST=tcp://docker-in-docker:2375`. Compose pins dind to `docker:28-dind`, disables TLS for the local TCP daemon, and gates runner startup on a dind healthcheck. If the host reboots, all three services use `restart: unless-stopped`; after `docker compose up -d`, the runner should wait until dind is healthy instead of crash-looping.

To verify recovery from a clean recreate:

```bash
docker compose down
docker compose up -d
docker compose ps
```

`forgejo`, `forgejo-dind`, and `forgejo-runner` should all be running, with dind healthy and the runner idle. Runner registration should survive because `./runner-data/.runner` and `./runner-data/runner-config.yml` are bind-mounted into `/data`.
