# Forgejo Actions with a nested dockerd: diagnosis (spike #1337)

**Date:** 2026-11-13
**Chainlink:** #1337 (split out of #1332)
**Status:** diagnosis and reproduction, no harness change

## TL;DR

The runner creates job containers in a **nested dockerd** (docker-in-docker),
so the job's image store is the dind store, not the host's. An image that is
present on the host is invisible to the job. The runner then tries a registry
pull; on the offline internal network used here the pull fails in milliseconds
and the job fails. Copying the image in with
`docker save <image> | docker exec -i <dind> docker load` makes the same job
run offline.

Two independent traps caused the earlier false leads:

1. **Docker 29 dind cannot copy the runner's `/var/run/act` payload** into an
   image whose `/var/run` is a symlink to `/run` (Ubuntu, Debian, node, and
   catthehacker images all are). It fails with
   `mkdirat var/run/act: path escapes from parent`. Docker 28 dind does the same
   copy correctly. The fix is to use `docker:28-dind`, not a `/var/run` or
   `--tmpfs /var/run/act` workaround: the tmpfs makes the copy return 200 but
   the files are hidden, and the job then fails with
   `bash: /var/run/act/workflow/0: No such file or directory`.
2. **`actions/checkout` is not available offline by default.** With
   `DEFAULT_ACTIONS_URL=http://forgejo:3000` the runner clones
   `http://forgejo:3000/actions/checkout`; without that repository the job fails
   with `remote: Not found`. Mirroring the action into the instance makes
   checkout work inside the offline job network.

The minimal template change is a new Actions variant of the disposable Forgejo
template (`tests/e2e/lib/forgejo/docker-compose.actions.yml` plus
`runner-config.yml` and `runner-entrypoint.sh`), keeping no `container_name`
and no fixed host port.

## Environment and method

Host Docker server: **29.9.2** (`docker version --format '{{.Server.Version}}'`,
evidence `01-stack.txt`). Nested daemons used in the spike:

| image | server version | evidence |
|-------|----------------|----------|
| `docker:dind` | 29.5.1 | `17-dind-versions.log` |
| `docker:28-dind` | 28.5.2 | `17-dind-versions.log` |

Forgejo `15.0.2+gitea-1.22.0`, runner `v12.10.1`, from the host image cache.
No image was pulled during the spike; every image was checked with
`docker image inspect` first.

The disposable stack used one compose project per run (all names begin with
`spike1337-`):

- `forgejo` on the compose default network (for the ephemeral host port) and on
  an internal `ci` network;
- `dind` (`docker:28-dind` for the successful runs, `docker:dind` only for the
  Docker 29 reproduction) on `ci`, privileged;
- `runner` on `ci`, registered with
  `--labels ubuntu-latest:docker://ubuntu:24.04` (or a second label for the
  probes) through the template entrypoint;
- job containers created by the runner inside dind, with
  `--add-host forgejo:<forgejo-ci-ip>` written by the template entrypoint.

The repeatable artifact is `scripts/spike-forgejo-nested-docker.sh`. It is a
single code path: the script brings up the checked-in template itself, with

```text
$ compose up -d --wait forgejo dind
```

where `compose` is `docker compose -p <project> -f tests/e2e/lib/forgejo/docker-compose.actions.yml`.
There is no inline copy of the compose file, the runner config or the host
mapping: the registration and the `--add-host` patch are produced by
`tests/e2e/lib/forgejo/runner-entrypoint.sh`, and the job runs in the template's
runner. The script prints the template's checksums and the checksums of the
files inside the runner container, so the evidence names the bytes that ran
(`19-repro-script.log`):

```text
template file: 1db7af3f87826cca719d8c9301cfe621e00248d725021d1650312e1ef7b6b1c8  /home/goya/agent-workspace/exomonad/tests/e2e/lib/forgejo/docker-compose.actions.yml
template file: 8a7256e4062a20086f37a2e294e8f993e502dbcd5071b4e8d0071e50e598ee12  /home/goya/agent-workspace/exomonad/tests/e2e/lib/forgejo/runner-config.yml
template file: 959249283516c705dceb50f4d2028d1e2a594077510107cb4939e7496bcfd528  /home/goya/agent-workspace/exomonad/tests/e2e/lib/forgejo/runner-entrypoint.sh
template check: no container_name
```

```text
$ compose exec -T runner sha256sum /etc/runner-config.yml /runner-entrypoint.sh /data/runner-config.yml
8a7256e4062a20086f37a2e294e8f993e502dbcd5071b4e8d0071e50e598ee12  /etc/runner-config.yml
959249283516c705dceb50f4d2028d1e2a594077510107cb4939e7496bcfd528  /runner-entrypoint.sh
e8cdfb08ceb934cda44fddcdcfa07b56d6c2115364ae0f8259d64507d80bbe2e  /data/runner-config.yml
```

The first two match the host files, so the runner ran the checked-in config and
entrypoint; `/data/runner-config.yml` differs because the entrypoint patched it,
and the same log prints the patched file (`options: "--add-host forgejo:172.31.0.2"`).
The script also runs both jobs through that template and verifies its own
teardown. The template variant is in `tests/e2e/lib/forgejo/` (see Q6).

All captured outputs are in
`docs/decisions/forgejo-actions-nested-docker/`.

---

## Q1. What happens when the job image is absent from the nested store?

The runner tries the image, **attempts a pull**, and the job fails. It does not
hang on the internal network used here; the DNS failure is immediate.

From `19-repro-script.log` (the script's run 1, `ubuntu:24.04` absent from a
fresh dind store, `job_level: info` as the template sets it):

```text
run 1: image absent from the nested store
2026-11-13T05:24:53.9071511Z spike1337-repro-20261113052442-3964612-runner(version:v12.10.1) received task 1 of job probe, be triggered by event: push
2026-11-13T05:24:53.9086832Z workflow prepared
2026-11-13T05:24:53.9087991Z 🚀  Start image=ubuntu:24.04
2026-11-13T05:24:53.9200069Z   🐳  docker pull image=ubuntu:24.04 platform=linux/amd64 username= forcePull=false
2026-11-13T05:24:53.9435828Z Error response from daemon: Get "https://registry-1.docker.io/v2/": dial tcp: lookup registry-1.docker.io on 127.0.0.11:53: server misbehaving
run 1 failed as expected (missing image, attempted pull)
```

With `job_level: debug` the runner also logs the check that decided to pull, so
the earlier manual capture (`13-missing-image-dind28.log`) has that line too:

```text
2026-11-13T03:27:06.0108829Z Image exists? false
2026-11-13T03:27:06.0109008Z pulling image 'docker.io/spike1337/absent:1' (linux/amd64)
2026-11-13T03:27:06.0112359Z   🐳  docker pull image=spike1337/absent:1 platform=linux/amd64 username= forcePull=false
2026-11-13T03:27:06.0282986Z Error response from daemon: Get "https://registry-1.docker.io/v2/": dial tcp: lookup registry-1.docker.io on 127.0.0.11:53: server misbehaving
```

Timing from those timestamps: task received at `03:27:06.001`, error at
`03:27:06.028`, so about **27 ms**. That is the offline DNS failure, not a
pull timeout. The script's own run 1 shows the same margin
(`05:24:53.907` received, `05:24:53.943` error, about 36 ms). A network that
resolves the registry but blackholes it would be bounded by Docker's own client
timeouts; this run did not measure that case. The operational point is
independent of the timeout: the runner does not look at the host's image cache,
so a missing nested image is a registry attempt and a failed job.

## Q2. Does `docker save | docker exec -i <dind> docker load` make the job run offline? Label syntax and pull policy?

Yes. After loading the host-cached `ubuntu:24.04` into the nested daemon, the
same workflow created the job container and ran to success.

Command and output (`02-load.txt`):

```text
$ docker save ubuntu:24.04 | docker exec -i spike1337-attempt1-dind-1 docker -H tcp://localhost:2375 load
Loaded image: ubuntu:24.04
```

The script performs the same load against the dind container of the template
project (`19-repro-script.log`):

```text
$ docker save ubuntu:24.04 | docker exec -i ef2ea1e5f8b0d2ff846e9de564ecf2571cfb703107af00306e4acc81401e1476 docker -H tcp://localhost:2375 load
Loaded image: ubuntu:24.04
```

Second run from `19-repro-script.log`:

```text
run 2: same workflow after the image is loaded
2026-11-13T05:24:55.8851923Z spike1337-repro-20261113052442-3964612-runner(version:v12.10.1) received task 2 of job probe, be triggered by event: push
2026-11-13T05:24:55.8866736Z workflow prepared
2026-11-13T05:24:55.8867384Z 🚀  Start image=ubuntu:24.04
2026-11-13T05:24:55.9427712Z   🐳  docker create image=ubuntu:24.04 platform=linux/amd64 entrypoint=["tail" "-f" "/dev/null"] cmd=[] network="WORKFLOW-496349f3b1dfda09419d585e7e35cab9"
2026-11-13T05:24:56.0708855Z   🐳  docker run image=ubuntu:24.04 platform=linux/amd64 entrypoint=["tail" "-f" "/dev/null"] cmd=[] network="WORKFLOW-496349f3b1dfda09419d585e7e35cab9"
2026-11-13T05:24:56.3407176Z ⭐ Run Main Show job identity
2026-11-13T05:24:56.4283310Z job-image-probe-ok
...
2026-11-13T05:24:56.5412211Z 172.31.0.2      forgejo
2026-11-13T05:24:56.5503315Z 🏁  Job succeeded
PASS: missing image failed; after docker save | docker load the job succeeded
PASS: the job container resolved forgejo through the template's host mapping
```

The load makes the difference between a failed job and a successful one: run 1
(above) never created a container because the pull failed, run 2 created the
container from the nested `ubuntu:24.04` and executed the step. The
`getent hosts forgejo` step (`172.31.0.2      forgejo`) is the mapping the
template entrypoint wrote into `container.options`; it only resolves inside the
job container because of that patch.

**Label syntax.** The runner's `.runner` file shows the mapping
(`03-registration.txt`; the probe labels were added to the same file for the
extra jobs):

```json
"labels": [
  "ubuntu-latest:docker://ubuntu:24.04",
  "tools:docker://node:lts",
  "absent:docker://spike1337/absent:1"
]
```

The workflow asks for `runs-on: ubuntu-latest`; the runner resolves the label to
`docker://ubuntu:24.04`. The registration command used is the template
entrypoint's (`tests/e2e/lib/forgejo/runner-entrypoint.sh`; the manual run above
used the same arguments):

```text
forgejo-runner register --no-interactive \
  --instance http://forgejo:3000 --token "$FORGEJO_RUNNER_TOKEN" \
  --name "$FORGEJO_RUNNER_NAME" \
  --labels "$FORGEJO_RUNNER_LABELS" \
  --config /data/runner-config.yml
```

The script passes `FORGEJO_RUNNER_LABELS=ubuntu-latest:docker://ubuntu:24.04`,
and the registration that came out of the template confirms it
(`19-repro-script.log`):

```text
runner-1  | level=info msg="Runner registered successfully."
runner-1  | time="2026-11-13T05:24:49Z" level=info msg="runner: spike1337-repro-20261113052442-3964612-runner, with version: v12.10.1, with labels: [ubuntu-latest], ephemeral: false, declared successfully"
```

**Pull policy.** `container.force_pull: false` is required to avoid registry
access when the image is already loaded. With `force_pull: true`, the runner
still makes the request, but if the local image exists it logs a warning and
continues (`09-force-pull-true.log`):

```text
2026-11-13T03:26:00.3324884Z Image exists? true
2026-11-13T03:26:00.3325082Z pulling image 'docker.io/library/ubuntu:24.04' (linux/amd64)
2026-11-13T03:26:00.3328567Z   🐳  docker pull image=ubuntu:24.04 platform=linux/amd64 username= forcePull=true
2026-11-13T03:26:00.3413758Z   🐳  Could not update image ubuntu:24.04, continuing with outdated local copy; error: Error response from daemon: Get "https://registry-1.docker.io/v2/": dial tcp: lookup registry-1.docker.io on 127.0.0.11:53: server misbehaving
2026-11-13T03:26:00.8202634Z 🏁  Job succeeded
```

So `force_pull: true` is not fatal when the image is already present, but it is
not offline-clean: it still contacts the registry. Use `force_pull: false` and
load the image explicitly.

The host and dind image IDs can differ after save/load (host
`69f919497cad`, dind `d401e511e11e` in this run); the loaded image is the same
platform (`amd64/linux`) and runs.

## Q3. Does the job container reach Forgejo, and does `actions/checkout` work offline?

**Reachability.** Yes, when the job container gets an explicit host mapping.
The job container lives in dind, so it has no compose DNS. The template
entrypoint resolves `forgejo` on the runner's network and writes
`--add-host forgejo:<forgejo-ci-ip>` into `container.options`. The repro
script's job proves it end to end: its second step is
`getent hosts forgejo`, and the job log shows the mapped address
(`19-repro-script.log`):

```text
2026-11-13T05:24:56.3407176Z ⭐ Run Main Show job identity
...
2026-11-13T05:24:56.5412211Z 172.31.0.2      forgejo
2026-11-13T05:24:56.5503315Z 🏁  Job succeeded
```

The earlier connectivity job (`10-connectivity.log`) proves DNS, TCP, and HTTP:

```text
2026-11-13T03:27:02.5018383Z server-url=http://forgejo:3000
2026-11-13T03:27:02.5018723Z 172.30.0.3      forgejo
2026-11-13T03:27:02.5018769Z tcp-ok
2026-11-13T03:27:02.5047558Z {
2026-11-13T03:27:02.5047728Z   "status": "pass",
...
2026-11-13T03:27:02.5052769Z }http-ok
2026-11-13T03:27:02.5155856Z 🏁  Job succeeded
```

`GITHUB_SERVER_URL` is `http://forgejo:3000` (the instance's `ROOT_URL`), and
the job container resolves that name only because of the `--add-host` mapping.
Do not rely on compose service names (`<project>_ci`, `forgejo-runner`, etc.)
inside a job container; only names added to its hosts file resolve.

**Checkout without an action source.** It fails. With
`FORGEJO__actions__DEFAULT_ACTIONS_URL=http://forgejo:3000`, the runner tries to
clone `actions/checkout` from the instance (`11-checkout-no-mirror.log`):

```text
2026-11-13T03:27:04.4111869Z   ☁️  git clone 'http://forgejo:3000/actions/checkout' # ref=v4
2026-11-13T03:27:04.4288706Z ⚙️ [runner]: unable to clone 'http://forgejo:3000/actions/checkout' ...:
Cloning into bare repository '...'...
remote: Not found.
fatal: repository 'http://forgejo:3000/actions/checkout/' not found: exit status 128
2026-11-13T03:27:04.4330154Z 🏁  Job failed
```

**Checkout with a mirrored action.** It works. The action source was mirrored
into the disposable instance once, on the host, with:

```text
git clone --bare --branch v4 --single-branch https://code.forgejo.org/actions/checkout.git checkout-mirror.git
git -C checkout-mirror.git push --mirror http://127.0.0.1:<port>/actions/checkout.git
```

The job container itself has no external network (dind is on the internal `ci`
network); the runner clones the action from the instance over that internal
network. `12-checkout-mirrored.log`:

```text
2026-11-13T03:31:10.1508867Z   ☁️  git clone 'http://forgejo:3000/actions/checkout' # ref=v4
2026-11-13T03:31:10.3945398Z Cloned http://forgejo:3000/actions/checkout to /data/.cache/act/...
2026-11-13T03:31:10.5489293Z Syncing repository: spike-admin/spike-repo
2026-11-13T03:31:10.6574559Z [command]/usr/bin/git checkout --progress --force -B main refs/remotes/origin/main
2026-11-13T03:31:10.9695300Z   ✅  Success - Post actions/checkout@v4
```

The job image was `node:lts` (node 24, git, python3, venv). Checkout needs a
node runtime in the job image and the action source in the instance. If the
workflow can avoid `uses: actions/checkout@v4`, a `run: git clone` with
`$GITHUB_TOKEN` (or a pre-cloned workspace) is the simpler offline path; if the
workflow keeps the standard action, mirror it.

## Q4. Which of these matter?

### `/var/run/act` tmpfs: not a fix; the dind version is

The runner mounts a volume at `/var/run/act` and copies its workflow files
there. On Ubuntu-family images `/var/run` is a symlink to `/run`, and Docker 29
rejects the copy:

- `04-docker29-default-path-escapes.log` (Docker 29, default volume):
  `copyContent: failed to copy content to container: Error response from daemon: mkdirat var/run/act: path escapes from parent`.
- `05-docker29-var-run-volume.log` (Docker 29, volume at `/var/run`):
  `[spike1337-act-var-run] is not a valid volume, will be ignored` and then the
  same `path escapes from parent`. (Volumes in `container.options` must also be
  allow-listed in `container.valid_volumes`; the message is silent-ish and easy
  to miss.)
- `06-docker29-var-run-file-exists.log` (Docker 29, allow-listed volume at
  `/var/run`): `mkdirat var/run: file exists`.
- `07-docker29-tmpfs-hidden.log` (Docker 29, `--tmpfs /var/run/act`): the copy
  succeeds, but the job fails with
  `bash: /var/run/act/workflow/0: No such file or directory`; the tmpfs hides
  the files that were copied to the lower mount.
- `16-put-archive-matrix.log` reproduces the same behavior directly against the
  Docker API (`PUT /containers/<id>/archive?path=/var/run/act/`):

```text
=== dind spike1337-m29a (--mount type=volume,...,dst=/var/run/act)
{"message":"mkdirat var/run/act: path escapes from parent"}
HTTP=500
=== dind spike1337-m29b (... dst=/var/run/act ... dst=/var/run)
{"message":"mkdirat var/run: file exists"}
HTTP=500
=== dind spike1337-m29c (... dst=/var/run/act --tmpfs /var/run/act)
HTTP=200
-- visible?
/var/run/act
=== spike1337-dind28 spike1337-m28a (... dst=/var/run/act)
HTTP=200
-- visible?
/var/run/act
/var/run/act/workflow
/var/run/act/workflow/event.json
/var/run/act/workflow/envs.txt
```

On **Docker 28.5.2** the default volume works and the files are visible; the
full job succeeds (`08-dind28-load-then-run-success.log`):

```text
2026-11-13T03:25:27.7400563Z job-image-probe-ok
2026-11-13T03:25:27.7400756Z Linux 1405d16576d3 6.8.0-147-generic #147-Ubuntu SMP PREEMPT_DYNAMIC Thu Sep 24 11:33:16 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
2026-11-13T03:25:27.7501285Z 🏁  Job succeeded
```

The persistent stack's `runner-entrypoint.sh` defines the option but never
appends it:

```text
ACT_TMPFS_OPTION="--tmpfs /var/run/act"
...
options="$ACT_VAR_RUN_OPTION $ACT_NIX_STORE_OPTION"
```

That is the right outcome: enabling it would produce the hidden-file failure.
The durable fix is to pin **`docker:28-dind`** (the operator's persistent stack
already uses it) and not add a `/var/run` or `/var/run/act` mount.

### Cache host: optional; needs an egress network and a reachable address

On an internal-only network the runner cannot determine an outbound IP, so the
cache server is disabled (`14a-cache-disabled-internal-network.log`):

```text
time="2026-11-13T03:43:14Z" level=error msg="Could not start the cache server, cache will be disabled: unable to determine outbound IP address"
```

With the runner also attached to an egress network and `cache.host` set to the
runner's **internal-network** IP, the cache URL is set and reachable from the
job container (`14-cache-reachable.log`):

```text
2026-11-13T03:30:12.2192042Z cache-url=http://172.30.0.4:46715/ad0c14a0809dde8c4670837e178761d4/
2026-11-13T03:30:12.2192200Z cache-host=172.30.0.4
2026-11-13T03:30:12.2216523Z cache-http=301
```

The job container has no compose DNS (Q3), so a `cache.host` of
`forgejo-runner` (the name used in
`docs/decisions/durable-forgejo-url-wiring.md`) is not resolvable from the job
container unless the runner also adds `--add-host forgejo-runner:<ip>`. For the
typecheck job, which does not use `actions/cache`, **disable the cache** and
avoid the extra network and host mapping.

### Privileged dind: required

A non-privileged `docker:dind` does not start (`15-unprivileged-dind.log`):

```text
failed to start daemon: Error initializing network controller: error obtaining controller instance:
failed to register "bridge" driver: failed to create NAT chain DOCKER:
iptables failed: iptables --wait -t nat -N DOCKER: iptables v1.8.11 (nf_tables):
Could not fetch rule set generation id: Permission denied (you must be root)
 (exit status 4)
```

### Registration token flow: works via the Forgejo container CLI

On a locked instance with registration disabled, the only way in is the
container CLI. The flow used here:

```text
docker compose -p <project> exec -T -u git forgejo forgejo admin user create \
  --admin --username spike-admin --password <generated> \
  --email spike-admin@example.invalid --must-change-password=false
docker compose -p <project> exec -T -u git forgejo forgejo actions generate-runner-token
```

The token output was 43 characters (`22-token-reuse.log`) and the runner
registered successfully (`03-registration.txt` shows the same CLI flow with the
token redacted). The token was also accepted a second time
for a second runner (`22-token-reuse.log`):

```text
$ docker run ... forgejo-runner register ... --token $TOKEN --name spike1337-reuse-a ...
Runner registered successfully.
$ docker run ... forgejo-runner register ... --token $TOKEN --name spike1337-reuse-b ...
Runner registered successfully.
```

So in Forgejo 15.0.2 the registration token was reusable; treat it as a secret
anyway and do not assume single use. The runner's `/data/.runner` stores a
runner token, so the `runner-data` volume is a credential.

## Q5. Which cached image can run the typecheck job's steps?

The typecheck job (`.forgejo/workflows/ci.yml`) needs `git`, `node`,
`python3 -m venv`, and `just` (and `pip` for `pip install './tl_loop[dev]'`).
The cached images were probed with
`docker run --rm --entrypoint sh <image> -c '...'` (`18-tool-matrix.txt`):

| image | git | node | python3 | venv | just | pip3 |
|-------|-----|------|---------|------|------|------|
| `ubuntu:24.04` | no | no | no | no | no | no |
| `node:lts` | yes | yes (24) | yes | yes | **no** | no |
| `docker:dind` | yes | no | no | no | no | no |
| `ghcr.io/catthehacker/ubuntu:rust-24.04` | yes | yes (24, toolcache) | yes | yes | **no** | yes |
| `nixos/nix:latest` | yes | no | no | no | no | no |

**No cached image has `just`.** `node:lts` is the closest: it has git, node,
python3 and venv, so it needs only `just`. `catthehacker/ubuntu:rust-24.04` has
pip as well but is 3.05 GB and still lacks `just`; it is not the smallest.

Recommendation:

- **Smallest change:** build one purpose image from `node:lts` and add the
  `just` binary. The host already has `just 1.49.0`
  (`/home/goya/.cargo/bin/just`, `18b-host-just.txt`), so a build step can
  `COPY` it in, or the image can `cargo install just` once on a networked host.
  This gives all four tools and keeps the already-cached node runtime.
- **Smaller final image:** base on `ubuntu:24.04` and install `git`,
  `python3`, `python3-venv`, a Node 20+ runtime, and `just`. This is smaller
  than `node:lts` but needs a build with network access; build it once on a
  networked host, then `docker save`/`docker load` it into dind.

Caveat for a fully offline typecheck: the job also runs
`python3 -m venv .venv-typecheck` and
`pip install './tl_loop[dev]'`. A venv is local, but `pip install` needs a
package source. For offline runs, either pre-build the venv into the purpose
image, mount a wheel cache, or keep the typecheck job's dependency step
networked. This is separate from the nested-image problem.

## Q6. Minimal template change for the disposable Forgejo

The new variant is checked in as:

- `tests/e2e/lib/forgejo/docker-compose.actions.yml` — full disposable
  template with Actions enabled;
- `tests/e2e/lib/forgejo/runner-config.yml` — base runner config
  (`force_pull: false`, cache disabled);
- `tests/e2e/lib/forgejo/runner-entrypoint.sh` — resolves `forgejo`, patches
  `container.options` with `--add-host forgejo:<ip>`, registers on first start
  with `FORGEJO_RUNNER_TOKEN`, then starts the daemon.

The two load-bearing properties are preserved:

- **No `container_name`.** Containers are named by the compose project
  (`<project>-forgejo-1`, `<project>-dind-1`, `<project>-runner-1`), so
  `docker compose -p <project> down -v --remove-orphans` finds them and
  concurrent runs cannot collide.
- **No fixed host port.** `127.0.0.1::3000` asks Docker for an ephemeral port,
  read back with `docker compose -p <project> port forgejo 3000`.

The additions:

```yaml
services:
  forgejo:
    environment:
      - FORGEJO__actions__ENABLED=true
      - FORGEJO__actions__DEFAULT_ACTIONS_URL=http://forgejo:3000
    networks: [default, ci]

  dind:
    image: docker:28-dind          # Docker 29 breaks /var/run/act copy
    privileged: true
    command: ["dockerd", "-H", "tcp://0.0.0.0:2375", "--tls=false"]
    networks: [ci]

  runner:
    image: data.forgejo.org/forgejo/runner:12
    user: "0:0"
    environment:
      - DOCKER_HOST=tcp://dind:2375
      - FORGEJO_RUNNER_TOKEN=${FORGEJO_RUNNER_TOKEN:-}
      - FORGEJO_RUNNER_LABELS=${FORGEJO_RUNNER_LABELS:-ubuntu-latest:docker://ubuntu:24.04}
    volumes:
      - runner-data:/data
      - ./runner-config.yml:/etc/runner-config.yml:ro
      - ./runner-entrypoint.sh:/runner-entrypoint.sh:ro
    command: ["/bin/sh", "/runner-entrypoint.sh"]
    networks: [ci]

networks:
  ci:
    internal: true
```

The harness must do two things the compose file cannot:

1. Generate the runner token with
   `docker compose -p <project> exec -T -u git forgejo forgejo actions generate-runner-token`
   and pass it as `FORGEJO_RUNNER_TOKEN` when starting the runner.
2. Copy each job image into the nested store before the job:
   `docker save <image> | docker exec -i "$(docker compose -p <project> ps -q dind)" docker -H tcp://localhost:2375 load`.

The template smoke test is now part of the reproduction script itself: the same
`19-repro-script.log` that runs the two jobs first starts the stack from this
compose file, registers through this entrypoint, and prints the config the
entrypoint produced:

```text
runner-1  | level=info msg="Runner registered successfully."
...
runner config after the entrypoint patch:
...
container:
  network: ""
  privileged: false
  options: "--add-host forgejo:172.31.0.2"
  valid_volumes: []
  force_pull: false
```

`docker compose config --no-interpolate` (same log) shows the dind pin, the
privileged flag, Actions enabled, the ephemeral port and the internal network:

```text
resolved compose model (docker compose config --no-interpolate)
5:    internal: true
26:    image: docker:28-dind
29:    privileged: true
43:      - FORGEJO__actions__ENABLED=true
56:    image: codeberg.org/forgejo/forgejo:15
64:        target: 3000
87:    image: data.forgejo.org/forgejo/runner:12
```

`21-template-config.txt` captured the same model with interpolation before the
script used the template; the `--no-interpolate` form above is used in the
script so the runner token cannot appear in a log.

### Template contract test

`tests/e2e/lib/forgejo/test_contract.py` checks the template without Docker
(`27-template-contract-test.txt`):

```text
$ tl_loop/.venv/bin/python -m pytest -q tests/e2e/lib/forgejo/test_contract.py
7 passed in 0.02s
```

`ruff check` also passes on the module
(`27-template-contract-test.txt` records both commands).

It asserts the seven properties this diagnosis depends on: no
`container_name`, the ephemeral `127.0.0.1::3000` port, `docker:28-dind` with
`privileged: true`, the internal `ci` network, the runner registering through
the entrypoint, `force_pull: false` with the cache disabled, and the
entrypoint's `container.options` patch, its refusal to run on a config it
cannot patch, and its failure when `forgejo` does not resolve. The entrypoint
tests execute the checked-in awk program and the checked-in resolution guard,
so they fail if that behaviour changes.

The justfile is outside this task's edit scope, and its
`check-e2e-recreated-leaf-recovery` recipe names
`tests/e2e/recreated-leaf-recovery/test_contract.py` explicitly, so a module
under `tests/e2e/lib/` is not collected by `just` today. The run above used the
same interpreter and pytest the recipes use
(`tl_loop/.venv/bin/python -m pytest`). Adding
`tests/e2e/lib/forgejo/test_contract.py` to a `check-e2e-*` recipe is the
follow-up that puts it in the normal invocation.

## Recommendation for #1332

1. **Pin `docker:28-dind`.** Do not use `docker:dind` (29.x) for the nested
   daemon; it cannot copy the runner's `/var/run/act` payload into these images.
   Keep the dind privileged.
2. **Use the Actions template variant** (`docker-compose.actions.yml`) with
   no `container_name` and no fixed host port. Generate the runner token with
   `forgejo actions generate-runner-token`; pass it to the runner.
3. **Job image:** build one purpose image once and cache it. The smallest
   change from the cache is `node:lts` + `just`; a smaller final image is
   `ubuntu:24.04` + git + python3-venv + Node 20+ + just. Load it into dind per
   run with `docker save | docker exec -i <dind> docker load`. Set the runner
   label to `ubuntu-latest:docker://<purpose-image>`.
4. **Pull policy:** `container.force_pull: false`. Do not rely on
   `force_pull: true` for offline runs; it still contacts the registry.
5. **Actions source:** keep `DEFAULT_ACTIONS_URL=http://forgejo:3000` and mirror
   `actions/checkout@v4` into the instance (or remove the `uses:` step and use
   a `run: git clone` with `$GITHUB_TOKEN`). Without the mirror, checkout fails
   with `remote: Not found`.
6. **Cache:** disable it for the typecheck job. If a later job needs
   `actions/cache`, attach the runner to an egress network to determine its
   outbound IP and set `cache.host` to an address the job containers can reach
   (the runner's internal-network IP, or a name added with `--add-host`).
7. **Offline Python dependencies:** the typecheck job's `pip install` needs a
   package source. Pre-build the venv into the purpose image, mount a wheel
   cache, or keep that step networked; the nested-image fix alone does not make
   the typecheck job fully offline.

## Teardown

Every run used a unique compose project starting with `spike1337-` and was
removed with `docker compose -p <project> down -v --remove-orphans`. The repro
script verifies its own project, and also compares the host's volume list with
the baseline it took before starting (`19-repro-script.log`):

```text
teardown verified: no container, volume or network for project spike1337-repro-20261113052442-3964612
teardown verified: no volume created by this run remains
```

The script creates only the template's compose project, so there are no
standalone containers whose anonymous volumes `down -v` could miss: the dind
container belongs to the project, and its anonymous `/var/lib/docker` volume is
removed with it.

The manual stack's before/after snapshots are in `23-teardown.txt`:

- before: the spike's `forgejo`, `dind`, `runner` containers, its
  `forgejo-data` / `runner-data` volumes, its `ci` / `edge` / `default`
  networks, and the compose dind's anonymous `/var/lib/docker` volume;
- after: no `spike1337-*` container, volume or network:

```text
$ docker ps -a | grep spike1337
none
$ docker volume ls | grep spike1337
none
$ docker network ls | grep spike1337
none
```

The project-scoped teardown in `23-teardown.txt` removed the manual stack's
containers, named volumes, networks, and the compose dind's anonymous
`/var/lib/docker` volume (`1057d798...`). The non-spike baseline was unchanged
for containers and networks.

The first teardown missed two more anonymous volumes because they belonged to
standalone `docker run` dind containers, not to a compose project:
`3409d474...` (created 22:23:55) and `fc31a001...` (created 22:31:44).
`docker rm -f` removed those containers but left their anonymous
`/var/lib/docker` volumes, and the volumes had no compose-project label for
`down -v` to find. The reviewer flagged them; `25-review-cleanup.txt` records
the `docker volume inspect`, the `docker volume rm`, and the after state. Both
are gone. `26-anonymous-volume-check.txt` lists every dangling volume with
its creation time and confirms that none was created during the spike window.
The remaining dangling volumes are older than this spike (2024 through
2026-07) and were not touched.

The final snapshot (`24-final-resources.txt`) repeats `docker ps -a`,
`docker volume ls` and `docker network ls`: no `spike1337-*` container, volume
or network remains, and no anonymous volume created during the spike remains.
The operator's `forgejo`, `forgejo-dind` and `forgejo-runner` containers are
still up. They were never stopped, removed, restarted or exec'd into.

The snapshot after the rewritten script's run is `28-final-resources.txt`. It
gives the same answers for the host's resources, and its volume list is
identical to the baseline recorded in `24-final-resources.txt` (35 volumes
before, 35 after, empty `comm -3` diff), so the run left nothing behind and
removed nothing that was not its own:

```text
$ docker ps -a | grep spike1337
(none)
$ docker volume ls | grep spike1337
(none)
$ docker network ls | grep spike1337
(none)
$ comm -3 /tmp/spike1337-baseline.txt /tmp/spike1337-after.txt   # empty means identical
```

## Artifacts

- `scripts/spike-forgejo-nested-docker.sh` — run-scoped reproduction: brings up
  the checked-in template with `-f`, registers through
  `runner-entrypoint.sh`, shows the missing-image failure, `docker save | docker
  load`, success, and teardown verification against a volume baseline.
- `tests/e2e/lib/forgejo/docker-compose.actions.yml` — disposable Actions
  template variant.
- `tests/e2e/lib/forgejo/runner-config.yml` — base runner config.
- `tests/e2e/lib/forgejo/runner-entrypoint.sh` — registration and host mapping.
- `tests/e2e/lib/forgejo/test_contract.py` — Docker-free contract test for the
  template (7 tests).
- `docs/decisions/forgejo-actions-nested-docker/` — captured logs:
  `01-stack.txt`, `02-load.txt`, `03-registration.txt`,
  `04`–`09` (Docker 29/28 job logs and force_pull),
  `10`–`12` (connectivity and checkout),
  `13` (missing image), `14`/`14a` (cache),
  `15` (unprivileged dind), `16` (raw archive matrix),
  `17` (dind versions), `18`/`18b` (tool matrix and host just),
  `19` (repro script, template-driven, both jobs and teardown),
  `21` (pre-script template config capture),
  `22` (token reuse), `23` (project teardown), `24` (final resource snapshot),
  `25` (review cleanup of the two anonymous dind volumes), `26`
  (dangling-volume creation check), `27` (template contract test),
  `28` (final resource snapshot and volume baseline after the rewritten run).
