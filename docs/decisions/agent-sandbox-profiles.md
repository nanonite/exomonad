# Agent Sandbox Profiles

**Status:** Accepted (schema migrated 2026-09-15, see Update below)

**Date:** 2026-05-19

**Chainlink:** #309

## Context

Codex agents are spawned with per-agent `.codex/config.toml` files, but before #309 the files only described hooks, MCP servers, and instructions. Runtime hook policy from #308 blocks known edit tools, and lifecycle hardening from #298/#304 prevents reviewer and worker provenance failures, but filesystem confinement belongs in the runtime sandbox when the runtime provides one.

Codex provides native filesystem sandboxing through named permission profiles selected by `default_permissions`. ExoMonad now renders one profile per role into every generated Codex config and selects the active profile from the agent role.

## Investigation

The write-path inventory below was produced with `strace -f -e trace=%file` against representative commands and cross-checked against ExoMonad role responsibilities. Commands that require network credentials (`gh`, `git fetch`) were classified by their local write surfaces rather than their remote side effects.

| Workflow | Observed or expected writes | Role implication |
|----------|-----------------------------|------------------|
| `git status`, `git diff`, `git log`, `git show` | May refresh `.git/index`; otherwise read-only | TL/root need `.git` write for safe read-only git inspection; reviewer gets no commit path because #308/#298 deny mutating git commands. |
| `git fetch` | `.git/FETCH_HEAD`, remote refs, pack files under `.git/objects` | TL/root need `.git` write for branch discovery and merge orchestration. Reviewers may need read-only diff context; fetch should stay coordinator-owned unless a reviewer test workflow proves otherwise. |
| `cargo test`, `cargo check`, `cargo metadata` | `target/`, sometimes `Cargo.lock` if dependency graph changes | Reviewers need build output writes only if they run tests themselves. `Cargo.lock` updates are not allowed in reviewer profile. |
| `just <test target>` | Delegates to build tools; writes `target/`, `dist-newstyle/`, generated logs under project temp/cache directories | Reviewer profile includes common build artifact roots but not arbitrary source writes. |
| `nix develop --command ...` | Nix store is external and managed by the host; project-local writes come from the nested command | No extra project write root beyond the nested workflow. |
| `gh pr view`, `gh pr diff`, `gh pr checks` | User cache/config under `$HOME`, not project source | Agents should prefer ExoMonad MCP tools over raw `gh`; #308 still blocks gh from hook shell commands. |
| ExoMonad MCP calls | `.exo/` state (events, session metadata), `.git` only for coordinator merge/write tools | TL/root need `.exo` and `.git`; reviewers need event/session surfaces and build output roots; dev/worker need full workspace write. |

## Decision

Codex config rendering writes:

```toml
default_permissions = "<role-profile>"

[permissions.root]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = [".exo", ".git"]

[permissions.tl]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = [".exo", ".git"]

[permissions.reviewer]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = [".exo/events", ".exo/tmp", "target", "rust/target", "dist-newstyle", "haskell/dist-newstyle", ".stack-work", ".cache"]

[permissions.dev]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = ["."]

[permissions.worker]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = ["."]
```

Role mapping is conservative:

| ExoMonad role | Codex profile |
|---------------|---------------|
| `root` | `root` |
| `tl` | `tl` |
| `reviewer` | `reviewer` |
| `worker` | `worker` |
| `dev` and custom roles | `dev` |

The custom-role fallback keeps unknown implementation roles functional while preserving strict profiles for the coordination and review roles.

## Reviewer Cargo Tradeoff

Reviewer test runs are the tension point. A reviewer that can run `cargo test` needs broad build-artifact writes, but a reviewer that can write arbitrary source files undermines #298. The current profile chooses the middle path: reviewers may write common build/cache output roots and ExoMonad event/session state, but not source paths or `Cargo.lock`. Review verdicts are submitted through Forgejo rather than written to workspace files.

If this proves too narrow, prefer moving reviewer test execution into an ExoMonad MCP tool that runs outside the Codex sandbox and returns structured results. Broadening reviewer workspace writes should be the fallback, not the default, because it weakens the reviewer authorship invariant.

## Relationship To Hook Policy

This is the structural fix for Codex. The runtime hook parity from #308 remains belt-and-braces for runtimes that do not sandbox, and for defense in depth when a Codex profile is missing or disabled. The reviewer edit deny from #298 and worker clean-tree/provenance invariant from #304 remain authoritative behavioral policy; sandbox profiles only reduce the filesystem blast radius.

## Consequences

- Root and TL Codex agents can mutate orchestration state and git metadata, but not source files through ordinary file writes.
- Reviewers can submit review records through Forgejo and write build artifacts, but source modification attempts must fail at the sandbox layer and at the PreToolUse hook layer.
- Dev leaves and workers keep full workspace write because implementation is their job.
- Network remains disabled in all generated profiles; networked operations should route through approved tools or explicit operator policy.

## Update 2026-09-15: Codex Schema Migration

Codex's April 2026 config refactor (`codex-rs` commit `1f24116` / PR #16962,
2026-04-07) replaced the flat `sandbox_mode` / `network_access` /
`writable_roots` keys inside named `[permissions.<name>]` profiles with a
nested `filesystem` / `network` sub-table shape. `codex_config.rs` was never
updated to follow that change, so `default_permissions = "<role>"` and every
`[permissions.<role>]` block above stopped being recognized. Rather than
erroring, current Codex silently falls back to its own unconfigured default
and prints a startup warning ("Permissions profile `<role>` does not define
any recognized filesystem entries for this version of Codex"). Every Codex
agent in this project was hitting that fallback.

`codex_config.rs` now renders the still-current legacy fields directly instead
of a named profile:

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
writable_roots = ["<abs>/.exo", "<abs>/.git"]   # role-scoped, see table below; absolute paths required
network_access = false
```

The per-role root mapping is unchanged from the Decision above (`root`/`tl` →
`.exo`, `.git`; `reviewer` → build/event roots; `dev`/`worker`/custom → the
whole worktree). `writable_roots` entries are rendered as absolute paths
because Codex's config now types them as `AbsolutePathBuf`; a relative path
like `.exo` is silently invalid.

**Host caveat, not a config regression:** `network_access = false` still makes
Codex's `bwrap` sandbox helper unshare the network namespace and configure a
loopback-only interface, both when entering `workspace-write` for ordinary
tool calls and when the interactive TUI/app-server's `exec-server` helper
preloads `AGENTS.md` at session bootstrap. A host whose AppArmor
`unprivileged_userns` transition profile does not grant `capability net_admin`
to unprivileged user namespaces (Ubuntu 24.04+ default hardening, tracked by
`kernel.apparmor_restrict_unprivileged_userns`) will fail that step with
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. That failure
is independent of this config — it reproduces with a bare `bwrap --unshare-net
... /bin/true` — and must be fixed at the host level (grant
`capability net_admin`/`capability net_raw` in
`/etc/apparmor.d/local/unprivileged_userns`, or disable
`kernel.apparmor_restrict_unprivileged_userns`), not by widening this profile.

**Verified against:** `codex-cli 0.154.0` (`@openai/codex` npm package),
cross-checked against vendored `codex-rs` commit
`5e3ee5eddfa5333f2e0b011880abf0cbf92bd295` (2026-05-12). `tests/e2e/codex-reviewer-sandbox`
passed against this same `codex-cli 0.154.0` on 2026-09-15 (host: Ubuntu
24.04.7, `apparmor` 4.0.1). Re-verify this note (and the e2e run) whenever the
installed Codex version changes — Codex's config schema has already drifted
out from under this ADR once.

## Update 2026-09-15 (cont.): Review Adjudication Is Implemented; Root/TL Codex Profile Scope

The Context and Investigation sections above were written when TL/root
orchestration was still expected to run as an interactive LLM session
deciding its own merges — the same mental model documented in the now-legacy
`codex_root_instructions()` PLAN/FORK/IDLE/MERGE/REPEAT text in
`rust/exomonad/src/init.rs`. Two things have since changed and are worth
recording here so this ADR doesn't keep citing a stale model:

**`adjudicate_review` is implemented, not aspirational.** CLAUDE.md's Tech
Lead Praxis section lists `decompose`, `adjudicate_review`, and
`compose_repair` as narrow model calls; at the time this ADR was written that
read as forward-looking. It is not — `adjudicate_review`
(`tl_loop/rlm/adjudicate.py:58-87`) is a real call through the shared RLM
boundary (`tl_loop/rlm/call.py::rlm`), invoked from the live ledger-event
handler in `tl_loop/loop/driver.py:10096-10103`, validated against a closed
schema, and gated by Python-side policy checks
(`_apply_policy_gates`, `adjudicate.py:210-232`) before a verdict is trusted.
The repair loop is real too: a `NO_GO` verdict flows through
`compose_repair(..., dispatch=dispatch_resume)`
(`driver.py:10449-10459`) into the same-owner `resume_pr` path
(`driver.py:10437-10447`) — never a new branch or owner — and reviewed-head
SHA binding (`driver.py:10040-10057`) drops stale verdicts on a head change,
matching the reviewed-head invariant elsewhere in CLAUDE.md. Exhausting
`reviewer_max_rounds` parks the slice with `ParkCause.REVIEW_STUCK`
(`driver.py:10460-10489`) rather than looping — this is a sound, bounded
state machine as implemented, not just as designed.

**The TL/root Codex sandbox profile in this ADR no longer covers the primary
orchestrator.** `tl_loop`'s own effect-client calls authenticate as
`role="tl"`/`role="root"` directly against the Rust runtime
(`tl_loop/client/effects.py:172`, `tl_loop/loop/recovery_control.py:160`) —
the Python controller process itself, unsandboxed by Codex, not a spawned
Codex/Claude agent running its own `git fetch`/merge commands. A "sub-TL" is
a nested `tl_run` (another Python controller instance,
`tl_loop/loop/driver.py:11303`), not another LLM session either. So the
Investigation table's `git fetch`/merge-orchestration row, and the `root`/`tl`
writable-roots profile in the Decision section, now apply only to
**Codex-based root/tl companions** (`[[companions]]` with `role = "root"` or
`"tl"` in `config.toml`) and any remaining manual Codex root/tl spawn path —
not to the controller's own git/merge operations, which never go through a
Codex sandbox at all. The `reviewer`, `dev`, and `worker` profiles are
unaffected by this and remain the primary Codex sandbox surface in practice.

## Update 2026-09-16: network_access Flipped to true — Denial Is Unfixable on This Host Class

`sandbox_workspace_write.network_access` is now `true` for every generated
Codex profile (`rust/exomonad-core/src/codex_config.rs`,
`sandbox_workspace_write_toml`). This reverses the network-deny half of the
original Decision; the filesystem `writable_roots` restrictions are
unchanged.

**Why.** Denying network makes Codex's bwrap sandbox unshare the network
namespace and configure a loopback-only interface inside it, which needs
`capability net_admin` granted to unprivileged user namespaces. Chainlink
#1085 attempted the documented host-level fix — adding
`/etc/apparmor.d/local/unprivileged_userns` with `capability net_admin,` and
`capability net_raw,`, then reloading — on a host whose AppArmor
`unprivileged_userns` transition profile denies that capability by default
(Ubuntu 24.04+ hardening, `kernel.apparmor_restrict_unprivileged_userns=1`).
The attempt was exhausted, not merely inconclusive: the override's syntax and
include resolution were verified correct
(`apparmor_parser -p`, no root needed), a cache-bypassed reload
(`apparmor_parser --skip-read-cache -r`) reported success, and a genuine cold
reboot (confirmed via `systemd-detect-virt: none` and a changed `boot_id`,
ruling out a container/VM restart artifact) still left the kernel's loaded
policy hash
(`/sys/kernel/security/apparmor/policy/profiles/unprivileged_userns.*/raw_sha256`)
byte-for-byte identical throughout. The bare repro
(`bwrap --unshare-net --dev /dev --proc /proc --ro-bind / / -- /bin/true`)
failed identically at every checkpoint. Working theory (unconfirmed): this
transition-target profile is established earlier in boot than the normal
`/etc/apparmor.d/` directory scan apparmor.service performs, based on an
"already loaded with profiles" skip path found in
`/lib/apparmor/rc.apparmor.functions`; pinning that down would need
kernel/initrd-level tracing beyond what's reachable from userspace tooling.
Full diagnostic trail is in chainlink #1085; the flip itself is chainlink
#1086.

**What this means going forward.** ExoMonad's Codex harness currently has no
way to reliably enforce network denial on a host in this class — the
harness's own sandbox config can request it, but whether it actually holds
depends on host AppArmor policy exomonad cannot control or verify from
userspace. Real, robust per-role network isolation would need each role
instance running inside its own container (Docker or similar) with a network
namespace under a runtime that already holds the privileges AppArmor is
denying to unprivileged bwrap on this host class. That is a real
architecture direction, not a rejected one — it is explicitly tabled rather
than attempted here, and any future work on it should start from this
paragraph rather than rediscovering why plain host-level AppArmor overrides
were not sufficient.

**What did not change.** The reviewer-authorship invariant — verdicts go
through `approve_pr`/`request_changes`, never a raw shell call to Forgejo —
still holds; it was never actually enforced by `network_access = false`
alone; it also had to be an instruction/policy discipline; and it still is.
`tests/e2e/codex-reviewer-sandbox` checks this directly regardless of the
sandbox's own network setting.

## Update 2026-09-16 (cont.): Real Host Fix Found — network_access Reverted to false

The `network_access = true` workaround above was temporary. Chainlink #1087
found the actual fix, and `network_access` is back to `false`
(`rust/exomonad-core/src/codex_config.rs`, `sandbox_workspace_write_toml`) —
the original Decision stands with no compromise.

**The fix.** Rather than editing `/etc/apparmor.d/local/unprivileged_userns`
(the transition profile `unconfined` processes go through when calling
`unshare(CLONE_NEWUSER)` — the approach #1085 exhausted without effect),
attach a dedicated profile directly to the `bwrap` binary itself:

```
# /etc/apparmor.d/bwrap-userns
abi <abi/4.0>,
include <tunables/global>

profile bwrap_userns /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
```

Loaded with `apparmor_parser -r /etc/apparmor.d/bwrap-userns` (no sysctl
change, no edit to the shipped `unprivileged_userns` profile). Verified
immediately: bare `bwrap --unshare-net --dev /dev --proc /proc --ro-bind / /
-- /bin/true` and the same command without `--unshare-net` both now exit 0
(previously `bwrap: loopback: Failed RTM_NEWADDR` and
`bwrap: setting up uid map: Permission denied` respectively). Verified end
to end: a real `codex exec` run with `approval_policy = "never"`,
`sandbox_mode = "workspace-write"`, `network_access = false`, in a trusted
project — the exact shape exomonad generates — executed a real shell command
cleanly with no sandbox errors.

**Why this worked where #1085 didn't.** The `unprivileged_userns`
restriction, per its own docstring, only applies to processes AppArmor
considers **unconfined** transitioning into a new user namespace. `bwrap`
running under a named profile — even one with `flags=(unconfined)`, which
does not itself restrict any operation — is no longer in the literal
unconfined domain, so the separate unconfined-specific transition rule
doesn't apply to it. This sidesteps the restriction instead of trying to
carve an exception into a profile (`unprivileged_userns`) that, per #1085,
could not be reloaded on this host through any means tried (correct syntax,
cache-bypassed reload, full cold reboot). Scoped to exactly `/usr/bin/bwrap`
by path attachment — it does not touch the system-wide
`kernel.apparmor_restrict_unprivileged_userns` sysctl or weaken AppArmor for
any other binary. What runs *inside* the sandbox remains constrained by
bwrap's own mount/namespace mechanisms regardless, unrelated to this
AppArmor layer.

**Consequence for #1085's follow-on.** Implemented (chainlink #1089):
`rust/exomonad-core/src/services/codex_sandbox_probe.rs` runs this exact
`bwrap --unshare-net ... /bin/true` reproduction as a non-blocking preflight
in `exomonad new` and `exomonad init`, gated on `Config::uses_codex_anywhere`
so a Claude/OpenCode-only project never runs it. On
`UserNamespaceRestricted`, it prints this exact remediation profile as a
`warn!` — never a hard error, never a nonzero exit. `BwrapNotFound` and
`Inconclusive` get their own advisory messages. Classification logic is
unit-tested against synthetic stderr; the real subprocess call has an
`#[ignore]`d test for manual host verification.

**Consequence for #1087.** The full-sandbox-bypass-vs-containerization
decision that issue posed is moot for this host — the underlying userns
restriction is fixed, filesystem scoping and network denial both hold with
no compromise, and no sandbox bypass or containerized-per-role execution is
needed here. Left open only as a decision record in case a future host
exhausts *this* fix too.
