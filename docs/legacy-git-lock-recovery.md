# Legacy Git index-lock recovery

Run this procedure only for an unowned legacy `index.lock` that `exomonad init`
has left untouched. It does not require the ExoMonad server. The inspect step is
read-only and reports the canonical worktree/Git/index/lock paths, the current
index and lock fingerprints, recorded ExoMonad process activity, and a recovery
fingerprint.

From the affected worktree, inspect first:

```sh
exomonad git-lock inspect --project-root "$PWD"
```

Proceed only if inspection reports no active or uncertain managed process and
does not report a matching managed lock proof. Stop every Git writer for this
repository, including external tools. Copy the exact SHA-256 recovery
fingerprint printed by inspect, then authorize recovery explicitly:

```sh
exomonad git-lock apply \
  --project-root "$PWD" \
  --expected-fingerprint 'PASTE_THE_64_CHARACTER_FINGERPRINT' \
  --writers-stopped
```

Apply rechecks the canonical paths, current index bytes/inode, lock bytes/inode,
recorded process scopes, and fingerprint under ExoMonad's index mutation lease.
It atomically moves the exact lock into `.git/exomonad-mutation/quarantine/` and
records an operator-assisted recovery intent and receipt in `.exo/ledger`. The
lock contents are preserved as evidence; the index and worktree files are not
rewritten. Nonempty locks up to the supported evidence limit are quarantined
byte-for-byte and are never interpreted or installed as an index.

If the apply command succeeds, resume the existing run:

```sh
exomonad init --continue
```

If the fingerprint is stale, inspect again and review the changed evidence. If
inspection reports an active owner, inaccessible managed evidence, a managed
proof, or an unsupported path, stop and resolve that condition first. This
procedure does not scan every host process; `--writers-stopped` is the
operator's explicit authorization for writers outside ExoMonad's durable
process records. Git's native lock protocol and ExoMonad's cooperative lease
remain in force.
