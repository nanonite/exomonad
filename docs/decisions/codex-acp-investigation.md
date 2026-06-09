# Codex ACP Investigation

Status: superseded

Date: 2026-06-09

Chainlink: #321, #493

## Context

Issue #321 asked whether Codex agents could use ACP as a structured delivery
path between native Teams inbox delivery and tmux STDIN fallback. That path was
low priority because tmux delivery already worked for Codex.

Issue #493 supersedes that implementation target. ACP was only the Gemini
delivery leg in the Teams -> ACP -> UDS -> tmux chain, and the InboxStore-based
mailbox from #479 replaces it as the cross-runtime delivery mechanism.

## Finding

Codex ACP support is no longer an ExoMonad implementation target.

The current Codex CLI and official Codex manual do not expose a documented
`acp` command, ACP server mode, or Agent Client Protocol interface. The
documented programmatic surfaces are:

- `codex mcp-server`, which exposes Codex as an MCP server over stdio.
- `codex app-server`, an experimental app server over stdio, WebSocket, or Unix
  socket.
- `codex remote-control`, which manages the app-server daemon with remote
  control support.
- `codex exec-server`, an experimental standalone exec-server service listed by
  local CLI help.

None of those surfaces are the ACP `new_session` / `prompt` /
`session_notification` protocol targeted by #321.

## Checked

- Official Codex manual fetched on 2026-06-09 from
  `https://developers.openai.com/codex/codex-manual.md`.
- Manual search terms: `ACP`, `acp`, and `Agent Client Protocol`; all returned
  zero matches.
- Official manual CLI reference:
  `https://developers.openai.com/codex/cli/reference`.
- Official manual "Use Codex with the Agents SDK" guide:
  `https://developers.openai.com/codex/guides/agents-sdk`.
- Local `codex --help` and `codex acp --help`; both showed the same documented
  command list and no `acp` subcommand.
- `CLAUDE.md` runtime support sections describing Codex hooks/config and
  non-Teams delivery fallback.
- `docs/decisions/cross-runtime-message-inbox.md`, which treats Codex delivery
  as ExoMonad inbox plus tmux consumer, not ACP.
- Chainlink #493, which removes ACP dead code and updates active delivery docs.

## Decision

Do not add Codex ACP support. After #493 lands, close #321 as superseded by ACP
removal and the InboxStore mailbox direction.

Keep Codex runtime work focused on the documented Codex surfaces already used by
ExoMonad: config, MCP, hooks, noninteractive execution, and tmux or future
InboxStore-backed delivery.
