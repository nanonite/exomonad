"""Run-scoped helpers shared by the real-server E2E acceptances.

Every real-server acceptance owns its forge, its Chainlink database, its tmux
sessions, its processes, and its temporary directory, and every one of those
must be reclaimed however the run ends. The code that does that lives here
once, so two acceptances cannot drift into two different answers about what a
leak is or how a disposable instance is provisioned.

Modules
-------
``cleanup``
    Run-scope ownership: register a resource before it starts, tear the whole
    run down from one trap, and report what survived. The run's name prefixes
    are a parameter, so no harness's names are baked in.
``forgejo_stack``
    The run's own disposable Forgejo, brought up from the shared compose
    template under this run's project name and removed with its volume.
``chainlink_db``
    A fresh Chainlink database created inside the run's own directory and
    seeded with exactly the issues the scenario needs. No existing database is
    ever copied or read.
``waiter``
    Bounded waits on durable boundaries only: a committed record, a registry
    entry, or a refusal. There is deliberately no "wait for the count to stop
    changing" helper.

Importing this package requires ``tests/e2e/lib`` on ``sys.path``::

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import e2e_harness.cleanup as cl
"""

from __future__ import annotations

__all__ = [
    "chainlink_db",
    "cleanup",
    "forgejo_stack",
    "waiter",
]
