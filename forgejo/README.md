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
