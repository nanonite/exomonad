# Forgejo Local CI

Forgejo + Forgejo Actions runner for local development CI.

## Start

```bash
docker compose up -d forgejo
# Wait for Forgejo to be healthy, then complete setup (see below)
```

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

4. Start the runner:
   ```bash
   docker compose up -d runner
   ```

   The compose entrypoint patches the generated runner config to mount `/var/run/act` as tmpfs in job containers. Docker 29 rejects Forgejo Runner archive uploads through the `/var/run` symlink unless that path exists as a real mount.

4. Set config in `.exo/config.toml`:
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
