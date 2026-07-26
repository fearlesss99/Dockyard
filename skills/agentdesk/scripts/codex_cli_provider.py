"""Codex CLI Provider — TC-13.8.4.

Concrete ``AgentCliProvider`` adapter for the ``codex`` CLI.
Translates a ``DispatchRequest`` into an ``AgentCliInvocation``.
Zero I/O, zero subprocess, zero network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dispatcher_gateway import (
    AgentCliInvocation,
    AgentCliProvider,
    DispatchRequest,
)

__all__ = ["CodexCliProvider"]

# ── constants ─────────────────────────────────────────────────────────────

_ALLOWED_PROVIDER_IDS: frozenset[str] = frozenset({"codex"})
_ALLOWED_SANDBOX_MODES: frozenset[str] = frozenset({
    "read-only",
    "workspace-write",
})
_EFFORT_MAP: dict[str, str] = {
    "efficient": "low",
    "balanced": "medium",
    "deep": "high",
}

# Model ID strict character allowlist (§2.12.4 frozen contract).
_MODEL_ID_ALLOWED_CHARS: frozenset[str] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-_./:"
)

# Control characters rejected in executable.
_BLOCKED_CHARS_EXECUTABLE: frozenset[str] = frozenset(
    {"\x00", "\r", "\n", "\t", "\v", "\f"}
)

# Shell metacharacters rejected in executable.
_SHELL_METACHARS: frozenset[str] = frozenset(
    {"&", "|", ";", "`", "$", "<", ">", "(", ")", '"', "'"}
)

# Recognised executable extensions, case-insensitive.
_EXE_EXTENSIONS: frozenset[str] = frozenset({".exe", ".cmd", ".bat", ".com"})

# Compiled regex patterns — module-level for per-construct cost.
_SUSPICIOUS_EXE_EXT = re.compile(r"(?i)\.(?:exe|cmd|bat|com)\s")
_SECOND_PATH_OR_SLASH_ARG = re.compile(
    r"\s(?:[A-Za-z]:[\\/]|\\\\|/)"
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:\\|/)")


# ── validation helpers ────────────────────────────────────────────────────


def _validate_provider_id(provider_id: object) -> str:
    """Validate *provider_id* — must be exactly ``"codex"``."""
    if not isinstance(provider_id, str):
        raise ValueError(
            f"provider_id must be str, got {type(provider_id).__name__}"
        )
    if provider_id not in _ALLOWED_PROVIDER_IDS:
        raise ValueError(
            f"provider_id must be 'codex', got {provider_id!r}"
        )
    return provider_id


def _validate_executable(executable: object) -> str:
    """Validate and return *executable* as a non-empty, safe ``str``.

    Must be ``str``, non-empty, non-pure-whitespace, no leading/trailing
    whitespace, no NUL/CR/LF/TAB/VT/FF, no shell metacharacters, and
    must not be an executable name with embedded arguments.

    Paths with ordinary spaces are only allowed when the string is a
    drive-absolute or UNC Windows path ending with a recognised
    executable extension (``.exe`` / ``.cmd`` / ``.bat`` / ``.com``).
    No other space-containing inputs are permitted — POSIX paths with
    spaces are fail-closed because arguments cannot be reliably
    distinguished from path components without filesystem access.
    """
    if not isinstance(executable, str):
        raise ValueError(
            f"executable must be str, got {type(executable).__name__}"
        )
    if not executable:
        raise ValueError("executable must not be empty")
    if executable.strip() == "":
        raise ValueError("executable must not be pure whitespace")
    if executable != executable.strip():
        raise ValueError(
            "executable must not have leading or trailing whitespace"
        )

    # Reject control characters — NUL, CR, LF, TAB, VT, FF.
    for ch in _BLOCKED_CHARS_EXECUTABLE:
        if ch in executable:
            label = {
                "\x00": "NUL", "\r": "CR", "\n": "LF",
                "\t": "TAB", "\v": "VT", "\f": "FF",
            }[ch]
            raise ValueError(f"executable must not contain {label}")

    # Reject shell metacharacters outright.
    for mc in _SHELL_METACHARS:
        if mc in executable:
            raise ValueError(
                "executable must not contain shell metacharacters"
            )

    # ── paths without spaces: no embedded‑argument risk ──────────────────
    if " " not in executable:
        return executable

    # ── paths with ordinary spaces ───────────────────────────────────────
    # Only Windows drive‑absolute or UNC paths with a recognised executable
    # extension are permitted.  Everything else is fail‑closed because we
    # cannot reliably tell a path component from an argument without
    # filesystem access.

    # Reject: an executable‑extension followed by whitespace anywhere
    # before the very end — this catches patterns like
    #   C:\…\codex.exe /help
    #   \\srv\…\codex.exe C:\payload.txt
    if _SUSPICIOUS_EXE_EXT.search(executable[:-1] if executable else ""):
        raise ValueError(
            "executable must not contain embedded arguments"
        )

    # Reject: a whitespace followed by a second absolute path
    # (drive letter, UNC, or slash) — catches:
    #   C:\…\runner D:\tmp\payload.cmd
    #   \\srv\…\runner \\other\share\payload.exe
    #   C:\…\runner /help
    if _SECOND_PATH_OR_SLASH_ARG.search(executable):
        raise ValueError(
            "executable must not contain embedded arguments"
        )

    # Must end with a recognised executable extension (case‑insensitive).
    ext_lower = (
        executable.rsplit(".", 1)[-1].lower() if "." in executable else ""
    )
    if f".{ext_lower}" not in _EXE_EXTENSIONS:
        raise ValueError(
            "executable with spaces must end with .exe, .cmd, .bat, or .com"
        )

    # Drive‑absolute:  X:\...    or  X:/...
    if _DRIVE_RE.match(executable):
        # Must contain exactly one drive‑colon, at index 1.
        if executable.count(":") != 1 or executable.find(":") != 1:
            raise ValueError(
                "executable must not contain embedded arguments"
            )
        return executable

    # UNC:  \\server\share\...
    if executable.startswith("\\\\"):
        # Must have at least one backslash after the initial \\, i.e.
        # \\server\share\...
        rest = executable[2:]
        first_sep = rest.find("\\")
        if first_sep < 1:
            raise ValueError(
                "executable must not contain embedded arguments"
            )
        share_start = first_sep + 1
        second_sep = rest.find("\\", share_start)
        if second_sep < share_start + 1:
            raise ValueError(
                "executable must not contain embedded arguments"
            )
        return executable

    # Anything else with spaces is ambiguous — fail closed.
    raise ValueError(
        "executable must not contain embedded arguments"
    )


def _validate_sandbox_mode(mode: object) -> str:
    """Validate *sandbox_mode* against the safe set."""
    if not isinstance(mode, str):
        raise ValueError(
            f"sandbox_mode must be str, got {type(mode).__name__}"
        )
    if mode not in _ALLOWED_SANDBOX_MODES:
        raise ValueError(
            f"sandbox_mode must be one of "
            f"{sorted(_ALLOWED_SANDBOX_MODES)!r}, got {mode!r}"
        )
    return mode


def _validate_model_id(model_id: object) -> str:
    """Validate *model_id* for strict character allowlist.

    Must be a non-empty str, no leading/trailing whitespace, no NUL/CR/LF,
    and all characters must be in the strict allowlist.
    """
    if not isinstance(model_id, str) or not model_id:
        raise ValueError(
            f"selected_model_id must be a non-empty str, got {model_id!r}"
        )
    if model_id != model_id.strip():
        raise ValueError(
            "selected_model_id must not have leading or trailing whitespace"
        )
    for ch in ("\x00", "\r", "\n"):
        if ch in model_id:
            raise ValueError(
                "selected_model_id must not contain NUL, CR, or LF"
            )
    for i, ch in enumerate(model_id):
        if ch not in _MODEL_ID_ALLOWED_CHARS:
            raise ValueError(
                f"selected_model_id[{i}] contains forbidden "
                f"character {ch!r}"
            )
    return model_id


# ── public provider ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CodexCliProvider:
    """Codex CLI adapter — TC-13.8.4 frozen contract."""

    provider_id: str
    executable: str
    sandbox_mode: str

    def __post_init__(self) -> None:
        # Validate provider_id
        _validate_provider_id(self.provider_id)
        # Validate executable
        _validate_executable(self.executable)
        # Validate sandbox_mode
        _validate_sandbox_mode(self.sandbox_mode)

    def build_invocation(
        self,
        request: DispatchRequest,
    ) -> AgentCliInvocation:
        """Build a fully-resolved ``AgentCliInvocation`` from a validated request.

        Raises :exc:`ValueError` for any unmappable field.
        """
        if not isinstance(request, DispatchRequest):
            raise ValueError(
                f"request must be DispatchRequest, "
                f"got {type(request).__name__}"
            )

        # Model ID — fail-closed validation with strict character allowlist.
        model_id = request.model_selection.selected_model_id
        _validate_model_id(model_id)

        # Effort mapping — fail-closed.
        deliberation_tier = (
            request.model_selection.selected_deliberation_tier
        )
        if not isinstance(deliberation_tier, str):
            raise ValueError(
                f"selected_deliberation_tier must be str, "
                f"got {type(deliberation_tier).__name__}"
            )
        mapped_effort = _EFFORT_MAP.get(deliberation_tier)
        if mapped_effort is None:
            raise ValueError(
                f"unknown selected_deliberation_tier: "
                f"{deliberation_tier!r}"
            )

        # Prompt — strict UTF-8 encoding.
        try:
            stdin = request.prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "request.prompt contains characters that cannot be "
                "encoded as UTF-8"
            ) from None

        # Build argv — frozen order from §2.12.4.
        argv = (
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--model",
            model_id,
            "--sandbox",
            self.sandbox_mode,
            "-c",
            f'model_reasoning_effort="{mapped_effort}"',
            "-",
        )

        return AgentCliInvocation(
            executable=self.executable,
            argv=argv,
            stdin=stdin,
            env_overrides=(),
        )
