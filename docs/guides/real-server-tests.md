# Real-server integration tests: per-test Forgejo and run-scoped cleanup

Moved verbatim from the root `CLAUDE.md`.

A test that runs the shipped server needs a real forge and leaves processes
behind. Both are run-scoped, and both are owned by the test, never shared. This
is the standard for real-server integration tests;
`tests/e2e/recreated-leaf-recovery/` is the reference implementation and
`tests/e2e/recursive-crash-convergence/` is the second harness built on it.

**A test owns its Forgejo.** The shared template is
`tests/e2e/lib/forgejo/docker-compose.yml` — a template, not a running instance.
Two properties of that file are load-bearing and must not change:

- **No `container_name`.** The container is named by its compose project, which
  is what makes a run's teardown able to find and remove it. A fixed name makes
  concurrent runs collide and lets a stale container outlive its project.
- **No fixed host port.** `127.0.0.1::3000` asks Docker for an ephemeral port,
  which the harness reads back with `docker compose -p <project> port`. A fixed
  port makes concurrent runs collide and lets a previous run's database answer
  a later run's API calls.

Each run then does all of this, with a project name derived from a per-run id:

```bash
docker compose -p "$PROJECT" -f tests/e2e/lib/forgejo/docker-compose.yml up -d --wait
HOST=$(docker compose -p "$PROJECT" -f ... port forgejo 3000)   # discovered
docker compose -p "$PROJECT" -f ... exec -u git forgejo forgejo admin user create --admin ...
# ...provision the run's own author, reviewer, tokens, repository, collaborators
docker compose -p "$PROJECT" -f ... down -v --remove-orphans
```

The instance is installed-locked with registration disabled, so the only way in
is the container CLI, and the run's accounts and repository are created from
code. Nothing is copied from a live forge and no account pre-exists.

**A run that needs Forgejo Actions uses the Actions variant.**
`tests/e2e/lib/forgejo/docker-compose.actions.yml` keeps both load-bearing
properties and adds a privileged `docker:28-dind` service, a runner, and
`runner-config.yml` / `runner-entrypoint.sh`. The runner creates job containers
in the nested daemon, whose image store starts empty, so the host's image cache
is invisible to a job: copy each job image in with
`docker save <image> | docker exec -i <dind> docker -H tcp://localhost:2375 load`
and keep `force_pull: false`. Docker 29 dind cannot copy the runner's
`/var/run/act` payload into images whose `/var/run` is a symlink, so the dind
stays on 28. The job container resolves `forgejo` only through the entrypoint's
`--add-host`, and `uses: actions/checkout` needs the action mirrored into the
instance. The diagnosis and its captured evidence are in
`docs/decisions/forgejo-actions-nested-docker.md`;
`just check-forgejo-actions-template` runs the template's contract and
`just spike-forgejo-nested-docker` reproduces the failure and the fix.

**The CI typecheck job runs in that disposable stack.**
`tests/e2e/typecheck-ci/` (wired as `just e2e-typecheck-ci`) owns a fresh
Forgejo, runner and `docker:28-dind` under its own compose project with an
ephemeral port. It runs the *real* `typecheck` job, extracted from
`.forgejo/workflows/ci.yml`, against a scratch repository holding a generic copy
of `tl_loop`, the extracted `tl-loop-typecheck` recipe, and the ratchet script.
The job image is `exomonad-typecheck-ci:local`, built once from
`tests/e2e/lib/forgejo/typecheck-ci.Dockerfile` (node plus a working venv,
`just`, an offline wheelhouse at `/opt/wheelhouse`, and a bare
`actions/checkout` mirror at `/opt/mirror/actions-checkout.git`).

Phase 0 inspects the Forgejo, runner, dind and job images with
`docker image inspect` before anything starts. A missing image fails the run by
name and prints the one-time seed command; the harness never pulls and never
builds. Per run it copies the job image into the nested store with
`docker save | docker exec -i <dind> docker load`, then pushes the image's
checkout mirror into the instance as `actions/checkout`, so no step needs the
internet. The run prints one PASS/FAIL line per check: green on the current
tree, red after one injected strict error, the missing nested-image failure, the
Phase 0 image precondition, and a project-agnostic contract. Teardown is
run-scoped and the run fails if any container, network, or volume of its
compose project survives. `just check-e2e-typecheck-ci` runs the Docker-free
contract tests.

**A run that walks many cases owns a Forgejo per batch, not one for the whole
run.** A matrix case pushes, reviews, and merges; one container asked to carry
all of them is a single point of failure whose death turns every case after it
into an identical provisioning refusal, so the report reads as dozens of
boundary verdicts when there was one incident. `fj.provision(..., batch=)` puts
the batch label in the compose project name (`forgejo-p2`), which keeps every
instance separately attributable to its run and its batch and separately
reclaimable by the prefix sweep, and `fj.release(scope, instance)` gives one back
mid-run with the same `down -v` teardown, so a run can finish a batch without
waiting for its end to hand the container back. Between cases the run
re-checks the instance (`fj.assert_answering`) and reports a lost one against
its compose project — naming the resource that died and how many cases were not
attempted — instead of letting each later case blame its own provisioning.

**A test owns everything it creates.** A run scope registers each resource
*before* it is started, and one trap on `EXIT`/`INT`/`TERM` tears the run down
whatever happens — including a failed assertion. Teardown is idempotent, because
it runs on every exit path. It removes:

| Resource | How it is found again after teardown |
|----------|------------------------------------------|
| tmux sessions | named from the run id, so a session is attributable to one run |
| processes | tracked by pid, signalled by process group, checked for zombie state |
| compose projects | by name, including the project-scoped volume |
| the run's directory | `mktemp -d`, never a fixed path, so two runs cannot collide |

Four rules follow from the leaks this replaced:

- **A child process must start in its own process group.** A child that inherits
  the harness's group makes a group signal a self-inflicted kill, so teardown
  takes itself down with the thing it was cleaning up. `start_new_session=True`
  on every `Popen`, and a kill path that falls back to the single pid when the
  target shares the caller's group.
- **Cleanup is verified, not assumed.** After teardown the scope reports what it
  can still see — a session with the run's prefix, any process whose working
  directory is under the run's directory, any compose project or volume with the
  run's name — and the run **fails** if any of it remains. A leaked process is
  detected through `/proc`, not through a name prefix, so a process that
  outlives its run cannot hide by being renamed.
- **A removed directory does not mean the process is gone.** The kernel reports a
  deleted working directory with `(deleted)` appended, which is exactly the state
  a leaked server is left in once teardown has removed the directory. That suffix
  is stripped before the path is compared, or the check reports clean while the
  process is still running.

- **Every tmux call names its own server.** tmux resolves the server from the
  inherited `TMUX` variable *before* it reads `TMUX_TMPDIR` or a `-S` flag, so a
  bare `tmux` run from inside a pane talks to whichever server owns that pane —
  which is how a harness whose docstring claimed the operator's sessions were
  unreachable ended up killing them. Every call goes through
  `e2e_harness/tmuxio.py`, which passes `-S` with an absolute socket inside the
  run's directory and strips `TMUX`/`TMUX_PANE`; `child_env()` gives every
  child the same stripped environment with `TMUX_TMPDIR` on the run's
  directory, which is where the shipped binary's own bare calls land. A
  contract test fails the build on a call that bypasses it.

**Teardown cannot be the only reclamation path.** A SIGKILL, a host reboot, or a
Docker restart takes the trap with it, so a run that was killed from outside
never tears itself down. Every run therefore begins with a prefix-driven sweep
that reclaims what such a run left — its processes, sessions, compose projects,
volumes, and directories — and fails the run if it cannot. The sweep is
prefix-driven rather than record-driven precisely because the thing being
reclaimed is a run this harness has no record of.

**Counts are read after a terminal boundary, never after a quiet period.** A
count that has not moved for a few seconds is not evidence that nothing further
is coming, so a duplicate landing after that window would pass silently. Every
count is taken once the work that could change it has recorded that it finished:
the deterministic agent writes a terminal record per invocation, and the count of
authoritative spawns is only read once it equals the count of those records. A
second spawn makes the two disagree, which fails the item instead of hiding.
There is deliberately no "wait for quiescence" helper to misuse.

**Waits are on durable boundaries.** An integration test polls a ledger event, a
`run.json`, git's worktree registry, or the forge's own record, with a bounded
deadline and a diagnostic naming the last state it saw. A duration being long
enough is never the thing being asserted. Where an actor has to stay alive for
the system to observe it — an agent holding its tmux window — it blocks on
standard input rather than sleeping, so it ends when teardown kills the session
and never depends on a duration.
