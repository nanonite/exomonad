"""The one shared definition of a host-recorded prelaunch role-route refusal.

The controller and the standalone host both implement this closed set. The
controller uses it to recognize a durable ``tool.called`` row that proves no
launch occurred; the host uses it to refuse a superseded declaration. Contract:
``docs/dispatch-prelaunch-refusal-contract.md``.
"""

from __future__ import annotations

from collections.abc import Mapping

ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED = (
    "Invalid input: role route configuration is stale or was not accepted by this server"
)
ROLE_ROUTE_DISPATCH_AUTHORIZATION_MISSING_OR_AMBIGUOUS = (
    "Invalid input: role route dispatch authorization is missing or ambiguous"
)

# Contract: a modern role-route refusal proves the host rejected the request
# before any launch only when it is one of these exact envelopes. Both sides
# implement this one set: see docs/dispatch-prelaunch-refusal-contract.md and
# the matching `PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS` constant in
# rust/exomonad-core/src/handlers/dispatch_authorization.rs.
PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS = frozenset(
    {
        ROLE_ROUTE_CONFIGURATION_NOT_ACCEPTED,
        ROLE_ROUTE_DISPATCH_AUTHORIZATION_MISSING_OR_AMBIGUOUS,
    }
)


def is_role_route_configuration_fingerprint(value: object) -> bool:
    """Accept a versioned configuration identity.

    ``v1`` values remain valid historical evidence and decode unchanged. New
    values use ``v2``, the effective-value identity; equality, never the version
    tag alone, decides whether one is the accepted snapshot.
    """
    if not isinstance(value, str):
        return False
    for prefix in ("sha256:v1:", "sha256:v2:"):
        if value.startswith(prefix):
            return _is_sha256_hex(value[len(prefix) :])
    return False


def is_proven_prelaunch_role_route_refusal(entry: Mapping[str, object]) -> bool:
    """Recognize only a host refusal that proves no effect was launched.

    This is the controller side of the shared prelaunch refusal contract. The
    historical action already carried a complete versioned route/configuration
    receipt, so it is not the older missing-route refusal. The result must be
    the exact rejected envelope: a false success, no result payload, one of the
    shared prelaunch errors, and the same error at the entry and result level.
    A matching error string alone is never enough, so successful, launched,
    unresolved, or contradictory entries stay held for their own outcome.
    """
    arguments = entry.get("arguments")
    result = entry.get("result")
    error = entry.get("error")
    return (
        entry.get("operation") == "resume_blocked_leaf"
        and entry.get("status") == "rejected"
        and isinstance(arguments, Mapping)
        and isinstance(arguments.get("route_configuration_fingerprint"), str)
        and is_role_route_configuration_fingerprint(
            arguments.get("route_configuration_fingerprint")
        )
        and isinstance(result, Mapping)
        and result.get("success") is False
        and result.get("result") is None
        and isinstance(error, str)
        and error in PRELAUNCH_ROLE_ROUTE_REFUSAL_ERRORS
        and result.get("error") == error
    )


def _is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )
