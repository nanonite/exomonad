# Typed tool-unavailable recovery

The controller treats a tool_unavailable result as deployment skew, not as a
domain failure. The Haskell dispatch fallthrough emits the typed envelope
field while retaining its human-readable error. Rust adds the loaded role,
WASM path, and modification time, so Python recovery can explain which
artifact is stale without inspecting the message text.

The TL parks the affected slice with ParkCause.TOOL_UNAVAILABLE, emits
tl.tool_unavailable, and transitions the run to TLFailed. Parking is outside
normal dispatch retry handling: a missing tool cannot become available during
the current run, so no attempt, retry ceiling, or budget charge is consumed.
Operators can rebuild with just install-all-dev or reload the server with
exomonad reload.
