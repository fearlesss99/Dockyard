"""Claude Code CLI Provider — TC-13.8.

Concrete ``AgentCliProvider`` adapter for the ``claude`` CLI.
Translates a ``DispatchRequest`` into an ``AgentCliInvocation``.
Zero I/O, zero subprocess, zero network.
"""

from __future__ import annotations

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

_BLOCKED_CHARS_EXECUTABLE = frozenset({"\x00", "\r", "\n"})
_ALLOWED_PROVIDER_IDS = frozenset({"claude", "claudecode"})
_ALLOWED_PERMISSION_MODES = frozenset({
    "default",
    "plan",
    "acceptEdits",
    "dontAsk",
})
_EFFORT_MAP = {
    "efficient": "low",
    "balanced": "medium",
    "deep": "high",
}


def _validate_executable(executable: object) -> str:
    """Validate and return *executable* as a non-empty, safe ``str``.

    Must be ``str``, non-empty, non-pure-whitespace, no leading/trailing
    whitespace, no NUL/CR/LF, and must not be an obvious shell command
    with embedded arguments or metacharacters.
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

    for ch in _BLOCKED_CHARS_EXECUTABLE:
        if ch in executable:
            if ch == "\x00":
                raise ValueError("executable must not contain NUL")
            if ch == "\r":
                raise ValueError("executable must not contain CR")
            if ch == "\n":
                raise ValueError("executable must not contain LF")

    # Reject embedded arguments / shell command characters.
    # A path with spaces is legal (e.g. "C:\\Program Files\\Claude\\claude.exe").
    # But the executable must not contain things like &&, |, ;, backticks, or
    # look like "exe --flag" (a space followed by a leading dash or similar).
    # The heuristic: if there is a space followed by a dash- or slash-prefixed
    # token, that's an embedded argument.
    parts = executable.split()
    if len(parts) > 1:
        for part in parts[1:]:
            if part.startswith("-") or part.startswith("/"):
                raise ValueError(
                    "executable must not contain embedded arguments"
                )
    # Shell metacharacters — reject outright (not just after spaces).
    for mc in ("&&", "|", ";", "`"):
        if mc in executable:
            raise ValueError(
                "executable must not contain shell metacharacters"
            )

    return executable


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
