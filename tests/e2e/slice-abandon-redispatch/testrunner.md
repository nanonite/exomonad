# Real-server slice recovery test plan

1. Build the development WASM and ExoMonad binary.
2. For each of three consecutive runs, create a bare remote and disposable
   repository, register root and nested identities (`abandonable-leaf-opencode`
   and `nested-abandonable-leaf-opencode`), and start the real server.
3. File a PR through the real `file_pr` effect, close it unmerged, delete the
   remote head, remove the local worktree and branch, and prune the commit
   object. Query the real `watcher_pr_state` effect and assert that Forgejo
   still reports the closed PR while the head is unreachable and no registry-
   domain error is emitted. Then seed the local checkpoint with the live
   attempt.
4. Invoke `abandon_slice` through the real effect transport. Record the
   `tl.slice_abandoned` payload, checkpoint cause, ledger cursor, filesystem,
   Git branch/worktree, and byte-identical publication registry.
5. Invoke abandonment again and assert `already_abandoned`, one event, one
   cleanup journal key, and unchanged attempts.
6. Redispatch from the plan and assert a new runtime identity with cleared PR,
   review, branch, worktree, and park fields. Dispose the fresh identity.
7. Repeat steps 3–6 for the nested `nested-sub-tl` run with
   `parent_run_id=root`, using the same server and the nested identity. This
   must exercise the real checkpoint, disposal, event, and redispatch paths;
   constructing a nested `WorkPlan` in memory is not sufficient.
8. Print machine-readable evidence including both watcher results and each
   lifecycle checkpoint, and fail on any cleanup residue.

The static NC1 mutation removes the actual
`resolveLivePrForSlice = mkHandler @ResolveLivePrForSlice` registration and
must make `scripts/check_tool_surface.py` fail.
