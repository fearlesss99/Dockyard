"""Reasonix CLI Provider — TC-13.28a.2 Phase A.

Concrete ``AgentCliProvider`` adapter for the ``reasonix`` CLI.
Translates a ``DispatchRequest`` into an ``AgentCliInvocation``.
Zero I/O, zero subprocess, zero network during build_invocation.

Frozen boundary:
  * provider_id == "reasonix"
  * selected_model_id == "deepseek-v4-flash"
  * profile == "economy"
  * max_steps == 12
  * one-shot ``reasonix run`` via stdin
  * --output-format json
  * Permission mode: evidence-dependent from TC-13.28a.2 Phase A
  * No API key, no auto-upgrade, no ACP/Desktop/TUI/MCP/web/subagent
"""

from __future__ import annotations

from dataclasses import dataclass

from dispatcher_gateway import (
    AgentCliInvocation,
    DispatchRequest,
)

__all__ = ["ReasonixCliProvider"]

# ── frozen constants ────────────────────────────────────────────────────────

_FROZEN_PROVIDER_ID: str = "reasonix"
_FROZEN_MODEL_ID: str = "deepseek-v4-flash"
_FROZEN_REASONIX_ROUTE: str = "deepseek-flash"
_FROZEN_PROFILE: str = "economy"
_FROZEN_MAX_STEPS: int = 12

# Allowed characters in selected_model_id as a sanity gate.
_MODEL_ID_ALLOWED_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-_./"
)


# ── validation helpers ───────────────────────────────────────────────────────


def _validate_provider_id(provider_id: object) -> str:
    if not isinstance(provider_id, str):
        raise ValueError(
            f"provider_id must be str, got {type(provider_id).__name__}"
        )
    if provider_id != _FROZEN_PROVIDER_ID:
        raise ValueError(
            f"provider_id must be {_FROZEN_PROVIDER_ID!r}, got {provider_id!r}"
        )
    return provider_id


def _validate_model_id(model_id: object) -> str:
    """Validate *model_id* — must be exactly ``"deepseek-v4-flash"``."""
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
    lower = model_id.lower()
    if lower != _FROZEN_MODEL_ID:
        raise ValueError(
            f"selected_model_id must be {_FROZEN_MODEL_ID!r}, "
            f"got {model_id!r}"
        )
    # Also reject any auto-upgrade patterns in the raw model_id.
    if "v4-pro" in lower or "pro" in lower.split("-"):
        raise ValueError(
            "Reasonix Basic must not auto-upgrade to deepseek-v4-pro"
        )
    return model_id


def _validate_permission_flags(
    permission_mode: str,
    allowed_tools: tuple[str, ...],
) -> None:
    """Validate permission flags against frozen safety rules.

    *permission_mode* must be a non-empty str, no whitespace edges, no
    NUL/CR/LF, and must not be ``manual``, ``auto``, or
    ``bypassPermissions``.

    *allowed_tools* must be a tuple of non-empty str, each no whitespace
    edges, no NUL/CR/LF, no duplicates.
    """
    if not isinstance(permission_mode, str) or not permission_mode:
        raise ValueError("permission_mode must be a non-empty str")
    if permission_mode != permission_mode.strip():
        raise ValueError(
            "permission_mode must not have leading or trailing whitespace"
        )
    for ch in ("\x00", "\r", "\n"):
        if ch in permission_mode:
            raise ValueError(
                "permission_mode must not contain NUL, CR, or LF"
            )

    forbidden: frozenset[str] = frozenset({
        "manual", "auto", "bypassPermissions",
    })
    if permission_mode in forbidden:
        raise ValueError(
            f"permission_mode must not be one of "
            f"{sorted(forbidden)!r}, got {permission_mode!r}"
        )

    if not isinstance(allowed_tools, tuple):
        raise ValueError(
            f"allowed_tools must be tuple, got {type(allowed_tools).__name__}"
        )
    seen: set[str] = set()
    for i, tool in enumerate(allowed_tools):
        if not isinstance(tool, str):
            raise ValueError(
                f"allowed_tools[{i}] must be str, got {type(tool).__name__}"
            )
        if not tool:
            raise ValueError(f"allowed_tools[{i}] must not be empty")
        if tool != tool.strip():
            raise ValueError(
                f"allowed_tools[{i}] must not have leading or trailing "
                f"whitespace"
            )
        for ch in ("\x00", "\r", "\n"):
            if ch in tool:
                raise ValueError(
                    f"allowed_tools[{i}] must not contain NUL, CR, or LF"
                )
        if tool in seen:
            raise ValueError(f"allowed_tools[{i}] is a duplicate: {tool!r}")
        seen.add(tool)


# ── public provider ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReasonixCliProvider:
    """Reasonix CLI adapter — TC-13.28a.2 Phase A frozen contract.

    Fields:
        provider_id: Must be exactly ``"reasonix"``.
        executable: The Reasonix CLI executable name or path.
        permission_mode: Evidence-dependent policy (not yet frozen).
            Leading candidate: ``"acceptEdits"``.
        allowed_tools: Precise tool allowlist scoped by permission evidence.
        max_steps: Hard ceiling per dispatch (default 12).
    """

    provider_id: str
    executable: str
    permission_mode: str
    allowed_tools: tuple[str, ...]
    max_steps: int = _FROZEN_MAX_STEPS

    def __post_init__(self) -> None:
        _validate_provider_id(self.provider_id)

        # executable: non-empty str, no leading/trailing whitespace,
        # no NUL/CR/LF, no shell metacharacters.  We reuse the same
        # validation as the Codex and Claude providers without
        # copy-pasting their helper functions (they are module-private).
        if not isinstance(self.executable, str) or not self.executable:
            raise ValueError("executable must be a non-empty str")
        if self.executable != self.executable.strip():
            raise ValueError(
                "executable must not have leading or trailing whitespace"
            )
        blocked = frozenset({"\x00", "\r", "\n"})
        for ch in blocked:
            if ch in self.executable:
                raise ValueError(
                    f"executable must not contain NUL, CR, or LF"
                )
        shell_mc = frozenset({"&", "|", ";", "`", "$", "<", ">", "(", ")",
                               '"', "'"})
        for mc in shell_mc:
            if mc in self.executable:
                raise ValueError(
                    "executable must not contain shell metacharacters"
                )

        _validate_permission_flags(self.permission_mode, self.allowed_tools)

        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool):
            raise ValueError(
                f"max_steps must be int, got {type(self.max_steps).__name__}"
            )
        if self.max_steps < 1 or self.max_steps > 12:
            raise ValueError(
                f"max_steps must be 1-12, got {self.max_steps}"
            )

    def build_invocation(
        self,
        request: DispatchRequest,
    ) -> AgentCliInvocation:
        """Build a fully-resolved ``AgentCliInvocation`` for ``reasonix run``.

        Raises :exc:`ValueError` for any unmappable field.
        """
        if not isinstance(request, DispatchRequest):
            raise ValueError(
                f"request must be DispatchRequest, "
                f"got {type(request).__name__}"
            )

        # Validate model_id — must be deepseek-v4-flash, never upgraded.
        model_id = request.model_selection.selected_model_id
        _validate_model_id(model_id)

        # Prompt — strict UTF-8 encoding for stdin.
        try:
            stdin = request.prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "request.prompt contains characters that cannot be "
                "encoded as UTF-8"
            ) from None

        # Build argv — frozen order, no shell string injection.
        argv: list[str] = [
            "run",
            # Reasonix selects a configured provider route here; the
            # immutable API model identity remains on DispatchRequest.
            "--model", _FROZEN_REASONIX_ROUTE,
            "--profile", _FROZEN_PROFILE,
            "--max-steps", str(self.max_steps),
            "--output-format", "json",
            "--permission-mode", self.permission_mode,
            "--print",
        ]

        if self.allowed_tools:
            argv.append("--allowed-tools")
            argv.extend(self.allowed_tools)

        return AgentCliInvocation(
            executable=self.executable,
            argv=tuple(argv),
            stdin=stdin,
            env_overrides=(),
        )
