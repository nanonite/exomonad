# Recursive crash recovery and exactly-once convergence (Chainlink #1057)

This is the final real-server acceptance before #1058. It uses a disposable
checkout, a dedicated Forgejo repository, the production Rust server, the
generated WASM tools, the Unix-socket TransportClient, and process death at
the external-effect boundary. The runner refuses the Forgejo-shaped mock.

Each logical operation is exercised immediately before and immediately after
the operation. The restart invokes run_tl_loop with plan=None, so the
persisted recursive manifest and checkpoint—not external plan input—select the
next action. The action journal, ledger cursor, state version, PR ancestry,
lane identity, and final state are checked after every restart. A response lost
after an effect is intentionally treated as an unknown outcome; reconciliation
must observe the authoritative server state and must not dispatch a second
effect for the same durable identity.

The matrix covers spawn, publication, review, repair, merge intent, remote
merge, merged adoption, parent synchronization, issue closure, changelog,
bookkeeping push, stage release, aggregate publication, and root finalization.
Same-order sub-TLs run together, later orders remain barriers, and nested
children publish only to their direct parent branches.

command to the captured checkpoint three times. It requires monotonic versions
and cursors, one merge intent, terminal journal entries, and no more than one
The separately configured Beast runner applies the supplied continuation command
to the captured checkpoint three times. It requires monotonic versions and
cursors, one merge intent, terminal journal entries, and no more than one new
merge reconciliation overall. If the captured checkpoint already contains the
confirmed merge, the runner baselines it and requires adoption/bookkeeping
without redispatching that merge.
