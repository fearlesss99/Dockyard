"""Claude Code CLI Provider — TC-13.8.

Concrete ``AgentCliProvider`` adapter for the ``claude`` CLI.
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

__all__ = ["ClaudeCodeProvider"]

_CONTROL_PROMPT = (
    "Read the task instructions from stdin and execute them."
)

_BLOCKED_CHARS_EXECUTABLE = frozenset(
    {"\x00", "\r", "\n", "\t", "\v", "\f"}
)
_ALLOWED_PROVIDER_IDS = frozenset({"claude", "claudecode"})
_ALLOWED_PERMISSION_MODES = frozenset({
    "default",
    "plan",
    "acceptEdits",
    "dontAsk",
})
_SHELL_METACHARS = frozenset(
    {"&", "|", ";", "`", "$", "<", ">", "(", ")", '"', "'"}
)
# Recognised executable extensions, case-insensitive.
_EXE_EXTENSIONS = frozenset({".exe", ".cmd", ".bat", ".com"})
_SUSPICIOUS_EXE_EXT = re.compile(r"(?i)\.(?:exe|cmd|bat|com)\s")
# A whitespace followed by a second absolute path or slash‑argument.
_SECOND_PATH_OR_SLASH_ARG = re.compile(
    r"\s(?:[A-Za-z]:[\\/]|\\\\|/)"
)
# Drive‑absolute pattern — compiled once at module load.
_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:\\|/)")
_EFFORT_MAP = {
    "efficient": "low",
    "balanced": "medium",
    "deep": "high",
}


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
        raise ValueError("executable must not have leading or trailing whitespace")

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
    #   C:\…\claude.exe /help
    #   \\srv\…\claude.exe C:\payload.txt
    if _SUSPICIOUS_EXE_EXT.search(executable[:-1] if executable else ""):
        raise ValueError(
            "executable must not contain embedded arguments"
        )

    # Reject: a whitespace followed by a second absolute path
    # (drive letter, UNC, or slash) — catches:
    #   C:\…\runner D:\tmp\payload.cmd
    #   C:\…\claude C:\tmp\payload.exe
    #   \\srv\…\runner \\other\share\payload.exe
    #   C:\…\runner /help
    if _SECOND_PATH_OR_SLASH_ARG.search(executable):
        raise ValueError(
            "executable must not contain embedded arguments"
        )

    # Must end with a recognised executable extension (case‑insensitive).
    ext_lower = executable.rsplit(".", 1)[-1].lower() if "." in executable else ""
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


def _validate_provider_id(provider_id: object) -> str:
    """Validate *provider_id* — must be exactly ``"claude"`` or ``"claudecode"``."""
    if not isinstance(provider_id, str):
        raise ValueError(
            f"provider_id must be str, got {type(provider_id).__name__}"
        )
    if provider_id not in _ALLOWED_PROVIDER_IDS:
        raise ValueError(
            f"provider_id must be 'claude' or 'claudecode', "
            f"got {provider_id!r}"
        )
    return provider_id


def _validate_permission_mode(mode: object) -> str:
    """Validate *permission_mode* against the safe set."""
    if not isinstance(mode, str):
        raise ValueError(
            f"permission_mode must be str, got {type(mode).__name__}"
        )
    if mode not in _ALLOWED_PERMISSION_MODES:
        raise ValueError(
            f"permission_mode must be one of "
            f"{sorted(_ALLOWED_PERMISSION_MODES)!r}, got {mode!r}"
        )
    return mode


def _validate_and_copy_tool_tuple(value: object) -> tuple[str, ...]:
    """Validate a tool tuple field and return a deeply immutable copy.

    Accepts tuple or other non‑str sequences; copies to tuple.
    Each element must be a non‑empty str, no leading/trailing
    whitespace, no NUL/CR/LF, and no duplicates.
    """
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"tool field must be a sequence, not {type(value).__name__}"
        )
    if isinstance(value, (dict, set)):
        raise ValueError(
            f"tool field must be a sequence, not {type(value).__name__}"
        )
    if not isinstance(value, (tuple, list)):
        raise ValueError(
            f"tool field must be a tuple or list, got {type(value).__name__}"
        )

    result: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"tool[{i}] must be str, got {type(item).__name__}"
            )
        if not item:
            raise ValueError(f"tool[{i}] must not be empty")
        if item != item.strip():
            raise ValueError(
                f"tool[{i}] must not have leading or trailing whitespace"
            )
        for ch in ("\x00", "\r", "\n"):
            if ch in item:
                raise ValueError(f"tool[{i}] must not contain NUL, CR, or LF")
        if item in seen:
            raise ValueError(f"tool[{i}] is a duplicate: {item!r}")
        seen.add(item)
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ClaudeCodeProvider:
    """Claude Code CLI adapter — TC-13.8 frozen contract."""

    provider_id: str
    executable: str
    permission_mode: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        # Validate provider_id
        _validate_provider_id(self.provider_id)

        # Validate executable
        _validate_executable(self.executable)

        # Validate permission_mode
        _validate_permission_mode(self.permission_mode)

        # Validate and copy tool tuples (must use object.__setattr__ on frozen).
        allowed = _validate_and_copy_tool_tuple(self.allowed_tools)
        disallowed = _validate_and_copy_tool_tuple(self.disallowed_tools)

        # Check intersection (exact string equality — no case folding).
        if not set(allowed).isdisjoint(disallowed):
            raise ValueError(
                "allowed_tools and disallowed_tools must be disjoint"
            )

        # Write back the validated copies (the originals may have been lists).
        object.__setattr__(self, "allowed_tools", allowed)
        object.__setattr__(self, "disallowed_tools", disallowed)

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

        # Model ID — fail-closed validation.
        model_id = request.model_selection.selected_model_id
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
                raise ValueError("selected_model_id must not contain NUL, CR, or LF")

        # Effort mapping — fail-closed.
        deliberation_tier = request.model_selection.selected_deliberation_tier
        if not isinstance(deliberation_tier, str):
            raise ValueError(
                f"selected_deliberation_tier must be str, "
                f"got {type(deliberation_tier).__name__}"
            )
        mapped_effort = _EFFORT_MAP.get(deliberation_tier)
        if mapped_effort is None:
            raise ValueError(
                f"unknown selected_deliberation_tier: {deliberation_tier!r}"
            )

        # Prompt — strict UTF-8 encoding.
        try:
            stdin = request.prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "request.prompt contains characters that cannot be "
                "encoded as UTF-8"
            ) from None

        # Build argv.
        argv = [
            "-p",
            _CONTROL_PROMPT,
            "--output-format", "json",
            "--model", model_id,
            "--permission-mode", self.permission_mode,
            "--effort", mapped_effort,
            "--no-session-persistence",
        ]

        if self.allowed_tools:
            argv.extend(["--allowedTools", *self.allowed_tools])

        if self.disallowed_tools:
            argv.extend(["--disallowedTools", *self.disallowed_tools])

        return AgentCliInvocation(
            executable=self.executable,
            argv=tuple(argv),
            stdin=stdin,
            env_overrides=(),
        )
