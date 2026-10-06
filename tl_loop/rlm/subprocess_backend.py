"""Tool-free RLM backend that reaches a model through the harness CLI.

One ``complete`` call is one fresh, stateless CLI invocation in an empty
scratch directory, so the model sees only the prompt and never a repository,
a session, or an ExoMonad MCP server. The harnesses a policy may name for the
TL role are:

* ``claude``: ``claude -p --output-format json --model <id> --tools ""``; the
  JSON envelope carries authoritative token usage.
* ``codex``: ``codex exec --model <id> --sandbox read-only --ephemeral
  --ignore-user-config``; the final message is read from ``--output-last-message``.
* ``opencode``: ``opencode run --standalone -m <id>``; opencode has no flag
  that disables tools, so isolation is the empty working directory and the
  absence of ``--auto`` (no permission is ever auto-approved).

Codex and opencode report no usage on this path, so their token charge is the
deterministic ``ApproximateTokenCounter`` estimate over the prompt and the
response; the charge is never zero and the method is the same one every RLM
event already records.

Every call logs the command before it runs and the exit code, response size,
and (on failure) stderr after it returns. A missing binary, an unsupported
harness, a non-zero exit, a timeout, and an unparseable response each raise
an explicit ``RlmCallError``/``RlmConfigurationError``; there is no fallback.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from tl_loop.client.transport import JsonObject

from .budget import ApproximateTokenCounter
from .call import RlmCallError, RlmConfigurationError
from .store import RlmRequest, RlmResponse

LOGGER = logging.getLogger(__name__)

SUPPORTED_BACKEND_HARNESSES = ("claude", "codex", "opencode")
DEFAULT_CALL_TIMEOUT_SECONDS = 600.0
_STDERR_LOG_LIMIT = 2000

CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def require_backend_binary(harness: str) -> str:
    """Return the harness binary path, refusing an unsupported or absent one."""
    if harness not in SUPPORTED_BACKEND_HARNESSES:
        raise RlmConfigurationError(
            f"harness {harness!r} has no tool-free RLM backend; supported: "
            + ", ".join(SUPPORTED_BACKEND_HARNESSES)
        )
    path = shutil.which(harness)
    if path is None:
        raise RlmConfigurationError(
            f"harness binary {harness!r} was not found on PATH; install it or change "
            "the roles.tl allow list in .exo/harness_policy.toml"
        )
    return path


def render_prompt(request: RlmRequest) -> str:
    """Render one request as a self-contained prompt that demands bare JSON."""
    parts = [
        (
            "You are a stateless, tool-free judgment function. Do not use tools, read "
            "files, or run commands. Answer from the input below only."
        ),
        "INPUT (JSON):",
        json.dumps(request.inputs, ensure_ascii=False, sort_keys=True, indent=2),
        "OUTPUT SCHEMA (JSON Schema; additional keys are rejected):",
        json.dumps(request.output_schema, ensure_ascii=False, sort_keys=True, indent=2),
    ]
    if request.retry_error:
        parts.append(
            "YOUR PREVIOUS ANSWER WAS REJECTED: "
            + request.retry_error
            + "\nReturn a corrected answer."
        )
    parts.append(
        "Reply with exactly one JSON object that satisfies the schema. No prose, "
        "no Markdown fences."
    )
    return "\n\n".join(parts)


def parse_json_object(text: str, *, source: str) -> JsonObject:
    """Parse a model reply that is one JSON object, optionally fenced."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise RlmCallError(f"{source} reply is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise RlmCallError(f"{source} reply must be a JSON object")
    return cast(JsonObject, value)


class SubprocessRlmBackend:
    """Run one stateless harness CLI call per ``complete`` request."""

    def __init__(
        self,
        harness: str,
        model_id: str,
        *,
        timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        runner: CommandRunner | None = None,
    ) -> None:
        self._binary = require_backend_binary(harness)
        if not model_id:
            raise RlmConfigurationError("model_id must be non-empty")
        if timeout_seconds <= 0:
            raise RlmConfigurationError("timeout_seconds must be positive")
        self.harness = harness
        self.model_id = model_id
        self._timeout = timeout_seconds
        self._runner: CommandRunner = runner if runner is not None else subprocess.run
        self._counter = ApproximateTokenCounter()

    def complete(self, request: RlmRequest) -> RlmResponse:
        """Return one parsed response; every failure is an explicit RlmCallError."""
        prompt = render_prompt(request)
        with tempfile.TemporaryDirectory(prefix="tl-rlm-") as scratch:
            workdir = Path(scratch)
            command, stdin_text, output_file = self._command(prompt, workdir)
            LOGGER.info(
                "[RlmBackend] harness=%s model=%s judgment=%s attempt=%d "
                "command=%s prompt_bytes=%d",
                self.harness,
                self.model_id,
                request.name,
                request.attempt,
                " ".join(command),
                len(prompt.encode("utf-8")),
            )
            started = time.monotonic()
            try:
                completed = self._runner(
                    command,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    cwd=str(workdir),
                    timeout=self._timeout,
                    check=False,
                    env=dict(os.environ),
                )
            except subprocess.TimeoutExpired as error:
                LOGGER.error(
                    "[RlmBackend] harness=%s judgment=%s timed out after %.1fs",
                    self.harness,
                    request.name,
                    self._timeout,
                )
                raise RlmCallError(
                    f"{self.harness} call for {request.name!r} timed out after "
                    f"{self._timeout:.0f}s"
                ) from error
            except OSError as error:
                LOGGER.error(
                    "[RlmBackend] harness=%s judgment=%s could not start: %s",
                    self.harness,
                    request.name,
                    error,
                )
                raise RlmCallError(
                    f"{self.harness} could not be started: {error}"
                ) from error
            latency_ms = int((time.monotonic() - started) * 1000)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            LOGGER.info(
                "[RlmBackend] harness=%s judgment=%s exit_code=%d stdout_bytes=%d "
                "latency_ms=%d",
                self.harness,
                request.name,
                completed.returncode,
                len(stdout.encode("utf-8")),
                latency_ms,
            )
            if completed.returncode != 0:
                LOGGER.error(
                    "[RlmBackend] harness=%s judgment=%s FAILED stderr=%s",
                    self.harness,
                    request.name,
                    stderr[:_STDERR_LOG_LIMIT],
                )
                raise RlmCallError(
                    f"{self.harness} exited with code {completed.returncode} for "
                    f"{request.name!r}: {stderr.strip()[:300]}"
                )
            reply, input_tokens, output_tokens = self._reply(stdout, output_file, prompt)
        output = parse_json_object(reply, source=self.harness)
        return RlmResponse(
            output,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    def _command(
        self, prompt: str, workdir: Path
    ) -> tuple[list[str], str | None, Path | None]:
        if self.harness == "claude":
            return (
                [
                    self._binary,
                    "-p",
                    "--output-format",
                    "json",
                    "--model",
                    self.model_id,
                    "--no-session-persistence",
                    "--tools",
                    "",
                ],
                prompt,
                None,
            )
        if self.harness == "codex":
            output_file = workdir / "last-message.txt"
            return (
                [
                    self._binary,
                    "exec",
                    "--model",
                    self.model_id,
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--output-last-message",
                    str(output_file),
                    "-",
                ],
                prompt,
                output_file,
            )
        return (
            [self._binary, "run", "--standalone", "-m", self.model_id, prompt],
            None,
            None,
        )

    def _reply(
        self, stdout: str, output_file: Path | None, prompt: str
    ) -> tuple[str, int, int]:
        if self.harness == "claude":
            return _claude_reply(stdout)
        if output_file is not None:
            try:
                reply = output_file.read_text(encoding="utf-8")
            except OSError as error:
                raise RlmCallError(
                    f"codex wrote no final message to {output_file.name}: {error}"
                ) from error
        else:
            reply = stdout
        return reply, self._estimate(prompt), self._estimate(reply)

    def _estimate(self, text: str) -> int:
        return self._counter.count({"text": text})


def _claude_reply(stdout: str) -> tuple[str, int, int]:
    envelope = parse_json_object(stdout, source="claude")
    if envelope.get("is_error") is True:
        raise RlmCallError(f"claude reported an error: {str(envelope.get('result'))[:300]}")
    result = envelope.get("result")
    if not isinstance(result, str):
        raise RlmCallError("claude envelope has no string result")
    usage = envelope.get("usage")
    if not isinstance(usage, Mapping):
        raise RlmCallError("claude envelope has no usage")
    return result, _usage_tokens(usage, "input"), _usage_tokens(usage, "output")


def _usage_tokens(usage: Mapping[str, object], direction: str) -> int:
    """Sum the usage counters that belong to one direction of a claude call."""
    keys: Sequence[str]
    if direction == "input":
        keys = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    else:
        keys = ("output_tokens",)
    total = 0
    for key in keys:
        value = usage.get(key, 0)
        if type(value) is not int or value < 0:
            raise RlmCallError(f"claude usage {key} must be a non-negative integer")
        total += value
    return total


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "SUPPORTED_BACKEND_HARNESSES",
    "SubprocessRlmBackend",
    "parse_json_object",
    "render_prompt",
    "require_backend_binary",
]
