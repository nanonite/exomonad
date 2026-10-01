"""Direct-leaf dependency edges declared by one plan scope.

A dependency edge is a declared, durable ordering between two **direct leaves
of the same plan scope**. Nothing here infers an edge: the only source of truth
is the explicit ``depends_on`` array a leaf declares in ``plan.json``. Path
overlap, sibling order, and list position are never read as ordering, because
two leaves touching disjoint paths may still carry a real ordering requirement
and two leaves sharing a glob may not.

An edge names the other leaf's *name*. Names are unique across a scope by the
closed-key plan contract, so resolution never has to guess between a worker, a
leaf, and a sub-TL that happen to share a label.

## What an edge means

``b depends_on a`` asserts "``b`` must not be dispatched until ``a`` is
integrated into the base branch ``b`` will be dispatched against". It is a
dispatch precondition, not a scheduling preference: the scheduler refuses the
dispatch rather than reordering it. ``tl_loop.loop.schedule`` enforces it and
propagates the consequences.

## Why only leaves

Only leaves merge, so only a merged leaf can satisfy the "integrated into its
declared base" half of the precondition. A worker is ephemeral and a sub-TL
carries its own staged ``order`` contract, so a leaf edge may not name either.
Naming one is rejected rather than silently dropped, because a dropped edge
would dispatch a dependent slice before its prerequisite.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: The only direct child kind that may declare and satisfy a dependency edge.
#: A leaf is the only kind that produces a merge, and a merge is the only thing
#: that can integrate a prerequisite into a dependent's declared base.
DEPENDABLE_CHILD_KIND = "leaf"


class DependencyValidationError(ValueError):
    """A declared dependency edge is not a valid same-scope DAG."""


def validate_leaf_dependencies(
    edges: Mapping[str, Sequence[object]],
    *,
    leaf_names: frozenset[str] | set[str],
    other_names: frozenset[str] | set[str],
    path: str = "plan",
) -> dict[str, tuple[str, ...]]:
    """Validate one scope's leaf dependency edges and return them normalized.

    ``edges`` maps a leaf name to its declared ``depends_on`` value.
    ``leaf_names`` is every direct leaf name in the scope and ``other_names``
    every direct worker and sub-TL name, so an edge naming a sibling of an
    incompatible kind is reported as such rather than as a missing target.

    Normalization is deterministic: each returned tuple holds the declared
    targets in declaration order with duplicates rejected, so one DAG declared
    in a different list order produces identical normalized content.
    """
    normalized = {
        name: _declared_targets(value, name=name, path=path) for name, value in edges.items()
    }
    _require_same_scope(normalized, leaf_names=leaf_names, other_names=other_names, path=path)
    _require_acyclic(normalized, path=path)
    return normalized


def _declared_targets(value: Sequence[object], *, name: str, path: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DependencyValidationError(f"{path}.{name}.depends_on must be an array of names")
    targets: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise DependencyValidationError(
                f"{path}.{name}.depends_on[{index}] must be a non-empty name"
            )
        if item in targets:
            raise DependencyValidationError(f"{path}.{name}.depends_on repeats {item!r}")
        targets.append(item)
    return tuple(targets)


def _require_same_scope(
    edges: Mapping[str, tuple[str, ...]],
    *,
    leaf_names: frozenset[str] | set[str],
    other_names: frozenset[str] | set[str],
    path: str,
) -> None:
    for name, targets in edges.items():
        if name not in leaf_names:
            raise DependencyValidationError(
                f"{path}.{name} declares depends_on but is not a direct leaf"
            )
        for target in targets:
            _require_resolvable(
                target, name=name, leaf_names=leaf_names, other_names=other_names, path=path
            )


def _require_resolvable(
    target: str,
    *,
    name: str,
    leaf_names: frozenset[str] | set[str],
    other_names: frozenset[str] | set[str],
    path: str,
) -> None:
    if target == name:
        raise DependencyValidationError(f"{path}.{name} depends_on itself")
    if target in leaf_names:
        return
    if target in other_names:
        raise DependencyValidationError(
            f"{path}.{name} depends_on {target!r}, which is not a direct leaf; only a "
            f"{DEPENDABLE_CHILD_KIND} merges and can satisfy a dependency"
        )
    raise DependencyValidationError(
        f"{path}.{name} depends_on unknown sibling {target!r}; a dependency must name "
        "a direct leaf in the same plan scope"
    )


def _require_acyclic(edges: Mapping[str, tuple[str, ...]], *, path: str) -> None:
    """Reject any cycle, naming the trail that closes it.

    Targets are already proven to be leaves in the same scope, so every target
    is a key of ``edges`` and no edge leaves this subgraph.
    """
    settled: set[str] = set()
    for start in edges:
        if start in settled:
            continue
        _visit(start, edges, settled, path=path)


def _visit(
    start: str, edges: Mapping[str, tuple[str, ...]], settled: set[str], *, path: str
) -> None:
    frames: list[tuple[str, int]] = [(start, 0)]
    trail: list[str] = [start]
    settled.add(start)
    # Only leaves that declare an edge are keys of ``edges``; a target without a
    # key is a DAG leaf and has no outgoing edge to expand.
    while frames:
        node, cursor = frames[-1]
        targets = edges.get(node, ())
        if cursor >= len(targets):
            frames.pop()
            trail.pop()
            continue
        frames[-1] = (node, cursor + 1)
        target = targets[cursor]
        if target in trail:
            raise DependencyValidationError(
                f"{path} depends_on cycle: {' -> '.join((*trail[trail.index(target) :], target))}"
            )
        if target in settled:
            continue
        settled.add(target)
        trail.append(target)
        frames.append((target, 0))


__all__ = [
    "DEPENDABLE_CHILD_KIND",
    "DependencyValidationError",
    "validate_leaf_dependencies",
]
