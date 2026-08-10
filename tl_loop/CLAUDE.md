# Programmatic TL Controller

`tl_loop/` is the programmatic tech-lead controller that replaces the
interactive TL orchestration loop. It owns the controller finite state machine
and calls the Rust ExoMonad runtime over its Unix-domain socket (UDS) boundary.

This package is intentionally a scaffold at M1.1. Runtime dependencies remain
empty; development tools are declared in `pyproject.toml`.

All I/O stays in Rust. Python owns controller decisions and pure state/event
transitions, while Rust remains responsible for sockets, processes, files,
ledger access, agent lifecycle, and every other external effect.

The runtime creates per-run state under `.exo/tl-loop/<run_id>/`. That directory
is runtime state, not Python source, and must never be used as the package code
location.
