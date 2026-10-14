"""Canonical repository addresses used as durable run identity.

The canonical form intentionally identifies the configured Git remote rather
than trying to infer repository equivalence across transports or aliases.
Supported forms are HTTP(S), ``ssh://`` URLs, and relative scp-like SSH
remotes. Host and scheme are case-folded, transport users and recognized
credential query/fragment parameters are removed, and repository path spelling
and explicit ports are preserved.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, unquote, urlsplit

_SUPPORTED_SCHEMES = frozenset({"http", "https", "ssh"})
_CREDENTIAL_PARAMETERS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "oauth_token",
        "password",
        "passwd",
        "private_token",
        "sig",
        "signature",
        "token",
    }
)
_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_SCP_REMOTE = re.compile(
    r"^(?:(?P<user>[^/@:\s]+)@)?(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):(?P<path>[^?#]+)$"
)


def canonical_repository_url(remote_url: str) -> str:
    """Return a stable, credential-free URL or reject ambiguous input.

    Error text is deliberately independent of the supplied URL so a malformed
    credential-bearing remote cannot leak through logs or parked state.
    """
    if not isinstance(remote_url, str) or not remote_url:
        raise ValueError("repository remote URL is missing")
    value = remote_url.strip()
    if not value or _has_control_or_space(value):
        raise ValueError("repository remote URL is malformed")

    if "://" not in value:
        return _canonical_scp_remote(value)

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError("repository remote URL uses an unsupported transport")
    if not parts.netloc or not parts.path.startswith("/"):
        raise ValueError("repository remote URL is malformed")
    if "?" in value and not parts.query:
        raise ValueError("repository remote URL has an ambiguous empty query")
    if "#" in value and not parts.fragment:
        raise ValueError("repository remote URL has an ambiguous empty fragment")
    _validate_removable_parameters(parts.query, "query")
    _validate_removable_parameters(parts.fragment, "fragment")

    authority = _canonical_authority(parts.netloc)
    path = _canonical_path(parts.path)
    return f"{scheme}://{authority}{path}"


def repository_api_coordinates(remote_url: str) -> tuple[str, str]:
    """Derive the Forge API owner/repository address from a canonical URL."""
    canonical = canonical_repository_url(remote_url)
    path = urlsplit(canonical).path.rstrip("/")
    segments = path.split("/")
    if len(segments) < 3 or not segments[-2] or not segments[-1]:
        raise ValueError("repository URL does not contain an API owner and repository path")
    owner = unquote(segments[-2])
    repo = unquote(segments[-1]).removesuffix(".git")
    if not owner or not repo or "/" in owner or "/" in repo:
        raise ValueError("repository URL does not contain an API owner and repository path")
    return owner, repo


def _canonical_scp_remote(value: str) -> str:
    match = _SCP_REMOTE.fullmatch(value)
    if match is None:
        raise ValueError("repository remote URL is malformed or unsupported")
    host = _canonical_host(match.group("host"))
    path = match.group("path")
    if path.startswith("/"):
        raise ValueError("absolute scp-like repository paths are unsupported")
    path = _canonical_path(f"/{path}")
    return f"ssh://{host}{path}"


def _canonical_authority(netloc: str) -> str:
    authority = netloc.rsplit("@", 1)[-1]
    if not authority:
        raise ValueError("repository remote URL is malformed")
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            raise ValueError("repository remote URL is malformed")
        host = authority[: close + 1]
        suffix = authority[close + 1 :]
        if suffix and not suffix.startswith(":"):
            raise ValueError("repository remote URL is malformed")
        port = suffix[1:] if suffix else None
        canonical_host = _canonical_host(host)
    else:
        if authority.count(":") > 1:
            raise ValueError("IPv6 repository hosts must use brackets")
        host, separator, port_text = authority.partition(":")
        port = port_text if separator else None
        canonical_host = _canonical_host(host)
    if port is not None:
        if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("repository remote URL has an invalid port")
        return f"{canonical_host}:{port}"
    return canonical_host


def _canonical_host(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        try:
            address = ipaddress.IPv6Address(host[1:-1])
        except ipaddress.AddressValueError as error:
            raise ValueError("repository remote URL has an invalid host") from error
        return f"[{address.compressed.lower()}]"
    if not host or not host.isascii() or host.endswith("."):
        raise ValueError("repository remote URL has an invalid host")
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[A-Za-z0-9-]+", label)
        for label in labels
    ):
        raise ValueError("repository remote URL has an invalid host")
    return host.lower()


def _canonical_path(path: str) -> str:
    if not path.startswith("/") or len(path) < 2:
        raise ValueError("repository remote URL has no repository path")
    if (
        not path.isascii()
        or _has_control_or_space(path)
        or "\\" in path
        or _INVALID_PERCENT_ESCAPE.search(path)
    ):
        raise ValueError("repository remote URL has a malformed path")
    try:
        decoded_segments = [
            unquote(segment, errors="strict") for segment in path.split("/")
        ]
    except UnicodeDecodeError as error:
        raise ValueError("repository remote URL has an invalid encoding") from error
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise ValueError("repository remote URL has an ambiguous path")
    if any(segment.casefold() in _CREDENTIAL_PARAMETERS for segment in decoded_segments):
        raise ValueError("repository remote URL path contains a credential marker")
    return _PERCENT_ESCAPE.sub(lambda match: f"%{match.group(1).upper()}", path)


def _validate_removable_parameters(value: str, component: str) -> None:
    if not value:
        return
    try:
        parameters = parse_qsl(value, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError(f"repository remote URL has a malformed {component}") from error
    if not parameters or any(
        key.casefold() not in _CREDENTIAL_PARAMETERS for key, _ in parameters
    ):
        raise ValueError(f"repository remote URL has an ambiguous {component}")


def _has_control_or_space(value: str) -> bool:
    return any(character.isspace() or ord(character) < 0x20 for character in value)
