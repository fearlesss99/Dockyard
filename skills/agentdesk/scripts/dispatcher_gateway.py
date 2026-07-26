"""AgentDesk DispatcherAgentGateway — TC-13.7.

Execution‑only, single‑shot agent CLI subprocess dispatch boundary.
Provider‑agnostic: ships only the ``AgentCliProvider`` Protocol;
zero real provider implementations, zero file writes, zero
canonical‑state persistence.

Public API
----------
``DispatchIdentity``           — frozen 4‑field audit identity
``ModelSelectionSnapshot``     — frozen 10‑field selector snapshot
``DispatchRequest``            — frozen 5‑field dispatch input
``AgentCliInvocation``         — frozen 4‑field resolved invocation
``DispatchResult``             — frozen 8‑field success result (exit 0 only)
``AgentCliProvider``           — runtime‑checkable Protocol
``run_dispatch``               — async entry point

Error hierarchy
---------------
``DispatchGatewayError``
  ├── ``DispatchInputError``
  ├── ``DispatchSnapshotError``
  ├── ``ProviderNotSupportedError``
  ├── ``ExecutableNotFoundError``
  ├── ``DispatchInvocationError``
  ├── ``DispatchLaunchError``
  ├── ``DispatchTimeoutError``
  ├── ``DispatchCancelledError``
  └── ``DispatchNonZeroExitError``

Non‑goals
---------
* File writes, canonical state, events, outbox, receipts, reports.
* Retry, escalation, approval, rate‑limit, re‑queue.
* WorkerKind, TaskDifficulty, ContextBudgetPolicy, BudgetResult.
* Slot lease, fencing, WorkerAdapter lifecycle, WorkflowOrchestrator.
* Claude CLI specifics, ``--permission-mode``, tool allowlists.
* MAD Gateway, ``mad`` refs, MAD internal Python modules.
* Real model, API, or network calls.
* parse_result, register_provider, unregister_provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import sys
import time as _time_module
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

__all__ = [
    "AgentCliInvocation",
    "AgentCliProvider",
    "DispatchCancelledError",
    "DispatchGatewayError",
    "DispatchIdentity",
    "DispatchInputError",
    "DispatchInvocationError",
    "DispatchLaunchError",
    "DispatchNonZeroExitError",
    "DispatchRequest",
    "DispatchResult",
    "DispatchSnapshotError",
    "DispatchTimeoutError",
    "ExecutableNotFoundError",
    "ModelSelectionSnapshot",
    "ProviderNotSupportedError",
    "run_dispatch",
]

# ── constants ─────────────────────────────────────────────────────────────

_SNAPSHOT_KEYS: frozenset[str] = frozenset({
    "required_model_tier",
    "required_model_capabilities",
    "model_binding_id",
    "selected_model_provider",
    "selected_model_id",
    "selected_model_tier",
    "selected_deliberation_tier",
    "selected_context_window_tokens",
    "selected_model_capabilities",
    "model_degradation_approval_id",
})

_STDERR_PREVIEW_MAX = 500

# Conservative secret‑redaction patterns applied to exception messages.
_SECRET_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(?:api[_-]?key|apikey|token|secret|password|auth)\s*[:=]\s*\S+",
                re.IGNORECASE),
     "***REDACTED***"),
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer ***"),
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{20,})\b"), "***"),
    (re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9_]{20,})\b"), "***"),
)

# ── frozen data types ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DispatchIdentity:
    """Immutable four‑field audit identity — Gateway never generates these."""

    task_id: str
    revision: int
    attempt: int
    dispatch_id: str


def _validate_identity(identity: DispatchIdentity) -> None:
    if not isinstance(identity, DispatchIdentity):
        raise DispatchInputError(
            f"identity must be DispatchIdentity, got {type(identity).__name__}"
        )
    if not isinstance(identity.task_id, str) or not identity.task_id:
        raise DispatchInputError("identity.task_id must be a non-empty string")
    if not isinstance(identity.revision, int) or isinstance(identity.revision, bool):
        raise DispatchInputError(
            f"identity.revision must be a non-bool int ≥ 1, got {identity.revision!r}"
        )
    if identity.revision < 1:
        raise DispatchInputError(
            f"identity.revision must be ≥ 1, got {identity.revision}"
        )
    if not isinstance(identity.attempt, int) or isinstance(identity.attempt, bool):
        raise DispatchInputError(
            f"identity.attempt must be a non-bool int ≥ 1, got {identity.attempt!r}"
        )
    if identity.attempt < 1:
        raise DispatchInputError(
            f"identity.attempt must be ≥ 1, got {identity.attempt}"
        )
    if not isinstance(identity.dispatch_id, str) or not identity.dispatch_id:
        raise DispatchInputError("identity.dispatch_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ModelSelectionSnapshot:
    """Immutable ten‑field selector snapshot.  Construct via ``from_mapping()``."""

    required_model_tier: str
    required_model_capabilities: tuple[str, ...]
    model_binding_id: str
    selected_model_provider: str
    selected_model_id: str
    selected_model_tier: str
    selected_deliberation_tier: str
    selected_context_window_tokens: int
    selected_model_capabilities: tuple[str, ...]
    model_degradation_approval_id: str | None

    @staticmethod
    def from_mapping(raw: Mapping[str, object]) -> ModelSelectionSnapshot:
        """Construct from a raw mapping with exact ten‑field validation.

        Raises :exc:`DispatchSnapshotError` for any violation.
        """
        if not isinstance(raw, Mapping):
            raise DispatchSnapshotError(
                f"snapshot input must be a Mapping, got {type(raw).__name__}"
            )

        actual = set(raw.keys())
        missing = _SNAPSHOT_KEYS - actual
        extra = actual - _SNAPSHOT_KEYS
        errors: list[str] = []
        if missing:
            errors.append(f"missing key(s): {', '.join(sorted(missing))}")
        if extra:
            errors.append(f"forbidden key(s): {', '.join(sorted(extra))}")
        if errors:
            raise DispatchSnapshotError("; ".join(errors))

        # Parse each field
        kwargs: dict[str, object] = {}

        # String fields (non‑capability)
        str_fields = [
            "required_model_tier",
            "model_binding_id",
            "selected_model_provider",
            "selected_model_id",
            "selected_model_tier",
            "selected_deliberation_tier",
        ]
        for key in str_fields:
            val = raw[key]
            if not isinstance(val, str) or not val:
                raise DispatchSnapshotError(
                    f"'{key}' must be a non-empty string, got {val!r}"
                )
            kwargs[key] = val

        # selected_context_window_tokens
        tokens_val = raw["selected_context_window_tokens"]
        if isinstance(tokens_val, bool) or not isinstance(tokens_val, int):
            raise DispatchSnapshotError(
                f"'selected_context_window_tokens' must be a non‑bool int ≥ 1, "
                f"got {tokens_val!r}"
            )
        if tokens_val < 1:
            raise DispatchSnapshotError(
                f"'selected_context_window_tokens' must be ≥ 1, got {tokens_val}"
            )
        kwargs["selected_context_window_tokens"] = tokens_val

        # required_model_capabilities
        kwargs["required_model_capabilities"] = _validate_and_copy_capabilities(
            raw["required_model_capabilities"],
            "required_model_capabilities",
        )

        # selected_model_capabilities
        kwargs["selected_model_capabilities"] = _validate_and_copy_capabilities(
            raw["selected_model_capabilities"],
            "selected_model_capabilities",
        )

        # model_degradation_approval_id — str | None
        approval_val = raw["model_degradation_approval_id"]
        if approval_val is None:
            kwargs["model_degradation_approval_id"] = None
        elif isinstance(approval_val, str) and approval_val:
            kwargs["model_degradation_approval_id"] = approval_val
        else:
            raise DispatchSnapshotError(
                f"'model_degradation_approval_id' must be a non‑empty str or None, "
                f"got {approval_val!r}"
            )

        return ModelSelectionSnapshot(**kwargs)  # type: ignore[arg-type]


def _validate_and_copy_capabilities(
    raw_value: object,
    field_name: str,
) -> tuple[str, ...]:
    """Validate a capability field and return a deeply immutable tuple copy."""
    if not isinstance(raw_value, (list, tuple)):
        raise DispatchSnapshotError(
            f"'{field_name}' must be a list or tuple, got {type(raw_value).__name__}"
        )
    result: list[str] = []
    for i, item in enumerate(raw_value):
        if not isinstance(item, str) or not item:
            raise DispatchSnapshotError(
                f"'{field_name}[{i}]' must be a non‑empty string, got {item!r}"
            )
        result.append(item)
    # Check duplicates preserving order
    seen: set[str] = set()
    for item in result:
        if item in seen:
            raise DispatchSnapshotError(
                f"'{field_name}' must not contain duplicates: {item!r}"
            )
        seen.add(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """Immutable five‑field dispatch input — frozen before Gateway invocation."""

    identity: DispatchIdentity
    workspace: Path
    prompt: str
    model_selection: ModelSelectionSnapshot
    timeout_seconds: int


def _validate_request(request: DispatchRequest) -> None:
    if not isinstance(request, DispatchRequest):
        raise DispatchInputError(
            f"request must be DispatchRequest, got {type(request).__name__}"
        )
    _validate_identity(request.identity)
    _validate_workspace(request.workspace)
    if not isinstance(request.prompt, str) or not request.prompt:
        raise DispatchInputError("request.prompt must be a non-empty string")
    if not isinstance(request.model_selection, ModelSelectionSnapshot):
        raise DispatchInputError(
            "request.model_selection must be ModelSelectionSnapshot, "
            f"got {type(request.model_selection).__name__}"
        )
    if (
        isinstance(request.timeout_seconds, bool)
        or not isinstance(request.timeout_seconds, int)
        or request.timeout_seconds < 1
    ):
        raise DispatchInputError(
            f"request.timeout_seconds must be a non‑bool int ≥ 1, "
            f"got {request.timeout_seconds!r}"
        )


def _validate_workspace(workspace: Path) -> None:
    if not isinstance(workspace, Path):
        raise DispatchInputError(
            f"workspace must be a Path, got {type(workspace).__name__}"
        )
    if not workspace.is_absolute():
        raise DispatchInputError(
            f"workspace must be an absolute path, got {workspace}"
        )
    if not workspace.is_dir():
        raise DispatchInputError(
            f"workspace does not exist or is not a directory: {workspace}"
        )


@dataclass(frozen=True, slots=True)
class AgentCliInvocation:
    """Fully‑resolved, safe subprocess invocation from a provider adapter."""

    executable: str
    argv: tuple[str, ...]
    stdin: bytes | None
    env_overrides: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Immutable success result — returned **only** when exit code is 0."""

    identity: DispatchIdentity
    provider: str
    model_id: str
    duration_seconds: float
    stdout: bytes
    stderr: bytes
    stdout_sha256: str
    stderr_sha256: str


# ── provider adapter Protocol ──────────────────────────────────────────────


@runtime_checkable
class AgentCliProvider(Protocol):
    """Provider adapter Protocol — runtime‑checkable, no default implementation.

    TC-13.7 ships only this Protocol.  Real implementations belong to
    TC-13.8 (Claude‑specific) and later contracts.
    """

    @property
    def provider_id(self) -> str:
        """Must equal ``selected_model_provider`` from the snapshot."""
        ...

    def build_invocation(self, request: DispatchRequest) -> AgentCliInvocation:
        """Build a fully‑resolved ``AgentCliInvocation`` from a validated request."""
        ...


# ── error hierarchy ───────────────────────────────────────────────────────


class DispatchGatewayError(Exception):
    """Base for all DispatcherAgentGateway errors."""


class DispatchInputError(DispatchGatewayError):
    """Structural input validation failure (identity, request, workspace)."""


class DispatchSnapshotError(DispatchGatewayError):
    """ModelSelectionSnapshot validation failure."""


class ProviderNotSupportedError(DispatchGatewayError):
    """Requested provider not in the call‑level ``providers`` mapping."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"provider not supported: {provider_id!r}")
        self.provider_id = provider_id


class ExecutableNotFoundError(DispatchGatewayError):
    """Executable not found or not an executable file."""

    def __init__(self, executable: str) -> None:
        super().__init__(f"executable not found or not executable: {executable!r}")
        self.executable = executable


class DispatchInvocationError(DispatchGatewayError):
    """Adapter‑side validation failure on the constructed AgentCliInvocation."""


class DispatchLaunchError(DispatchGatewayError):
    """Subprocess failed to start (OSError)."""


class DispatchTimeoutError(DispatchGatewayError):
    """Subprocess exceeded ``timeout_seconds``."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(f"dispatch timed out after {timeout_seconds}s")
        self.timeout_seconds = timeout_seconds


class DispatchCancelledError(DispatchGatewayError):
    """Caller cancelled the subprocess via asyncio cancellation."""


class DispatchNonZeroExitError(DispatchGatewayError):
    """Subprocess exited with a non‑zero exit code.

    Carries *only* exit_code, stdout_sha256, stderr_sha256, and a
    length‑capped, secret‑redacted stderr_preview.
    """

    def __init__(
        self,
        exit_code: int,
        stdout_sha256: str,
        stderr_sha256: str,
        stderr_bytes: bytes,
    ) -> None:
        preview = _safe_stderr_preview(stderr_bytes)
        self.exit_code = exit_code
        self.stdout_sha256 = stdout_sha256
        self.stderr_sha256 = stderr_sha256
        self.stderr_preview = preview
        super().__init__(
            f"dispatch exited with code {exit_code}"
            + (f": {preview}" if preview else "")
        )


# ── helpers ───────────────────────────────────────────────────────────────


def _safe_stderr_preview(stderr_bytes: bytes, max_len: int = _STDERR_PREVIEW_MAX) -> str:
    """Return a length‑capped, secret‑redacted, replace‑decoded stderr preview."""
    if not stderr_bytes:
        return ""
    text = stderr_bytes.decode("utf-8", errors="replace")
    # Apply conservative secret redaction
    for pattern, replacement in _SECRET_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) <= max_len:
        return text.strip()
    return text[:max_len].strip() + "…"


def _sha256(data: bytes) -> str:
    """Return 64‑char lowercase hex SHA‑256 of *data*."""
    return hashlib.sha256(data).hexdigest()


# ── executable resolution ─────────────────────────────────────────────────


def _resolve_executable(executable: str) -> str:
    """Resolve *executable* to a runnable path.

    Raises :exc:`ExecutableNotFoundError` if not found or not executable.
    """
    # Must not contain NUL.
    if "\0" in executable:
        raise DispatchInvocationError(
            "executable must not contain NUL characters"
        )

    # Absolute path?
    if os.path.isabs(executable):
        path = Path(executable)
        if not path.is_file():
            raise ExecutableNotFoundError(executable)
        if not os.access(path, os.X_OK):
            raise ExecutableNotFoundError(executable)
        return str(path)

    # Plain name — must not contain directory separators.
    if "/" in executable or "\\" in executable:
        # Relative path — resolve it
        path = Path(executable)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise ExecutableNotFoundError(executable)

    # shutil.which() lookup.
    resolved = shutil.which(executable)
    if resolved is None:
        raise ExecutableNotFoundError(executable)
    return resolved


# ── invocation validation ─────────────────────────────────────────────────


def _validate_invocation(invocation: AgentCliInvocation) -> None:
    """Validate an ``AgentCliInvocation`` before launching a subprocess."""
    if not isinstance(invocation, AgentCliInvocation):
        raise DispatchInvocationError(
            f"invocation must be AgentCliInvocation, got {type(invocation).__name__}"
        )

    # executable
    if not isinstance(invocation.executable, str) or not invocation.executable:
        raise DispatchInvocationError("invocation.executable must be a non-empty string")
    if "\0" in invocation.executable:
        raise DispatchInvocationError(
            "invocation.executable must not contain NUL characters"
        )

    # argv
    if not isinstance(invocation.argv, tuple):
        raise DispatchInvocationError(
            f"invocation.argv must be a tuple, got {type(invocation.argv).__name__}"
        )
    for i, arg in enumerate(invocation.argv):
        if not isinstance(arg, str):
            raise DispatchInvocationError(
                f"invocation.argv[{i}] must be a string, got {type(arg).__name__}"
            )
        if "\0" in arg:
            raise DispatchInvocationError(
                f"invocation.argv[{i}] must not contain NUL characters"
            )

    # stdin
    if invocation.stdin is not None and not isinstance(invocation.stdin, bytes):
        raise DispatchInvocationError(
            f"invocation.stdin must be bytes or None, got {type(invocation.stdin).__name__}"
        )

    # env_overrides
    if not isinstance(invocation.env_overrides, tuple):
        raise DispatchInvocationError(
            "invocation.env_overrides must be a tuple of (key, value) pairs, "
            f"got {type(invocation.env_overrides).__name__}"
        )
    env_keys: set[str] = set()
    for i, pair in enumerate(invocation.env_overrides):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise DispatchInvocationError(
                f"invocation.env_overrides[{i}] must be a 2‑tuple, got {pair!r}"
            )
        key, value = pair
        if not isinstance(key, str) or not isinstance(value, str):
            raise DispatchInvocationError(
                f"invocation.env_overrides[{i}] must be (str, str), "
                f"got ({type(key).__name__}, {type(value).__name__})"
            )
        if not key:
            raise DispatchInvocationError(
                f"invocation.env_overrides[{i}] key must be non‑empty"
            )
        if "=" in key:
            raise DispatchInvocationError(
                f"invocation.env_overrides[{i}] key must not contain '=': {key!r}"
            )
        if "\0" in key or "\0" in value:
            raise DispatchInvocationError(
                f"invocation.env_overrides[{i}] must not contain NUL"
            )
        if key in env_keys:
            raise DispatchInvocationError(
                f"invocation.env_overrides has duplicate key: {key!r}"
            )
        env_keys.add(key)


# ── provider mapping validation ───────────────────────────────────────────


def _validate_providers(
    providers: Mapping[str, AgentCliProvider],
    selected_provider: str,
) -> AgentCliProvider:
    """Validate the providers mapping and return the matching adapter.

    Raises :exc:`ProviderNotSupportedError` if the provider is missing.
    """
    if not isinstance(providers, Mapping):
        raise DispatchInputError(
            f"providers must be a Mapping, got {type(providers).__name__}"
        )
    if len(providers) == 0:
        raise DispatchInputError("providers must not be empty")

    adapter = providers.get(selected_provider)
    if adapter is None:
        raise ProviderNotSupportedError(selected_provider)

    # adapter.provider_id must match its key
    try:
        pid = adapter.provider_id
    except Exception as exc:
        raise DispatchInvocationError(
            f"failed to read provider_id from adapter: {exc}"
        ) from exc

    if not isinstance(pid, str) or not pid:
        raise DispatchInvocationError(
            f"adapter.provider_id must be a non‑empty string, got {pid!r}"
        )
    if pid != selected_provider:
        raise DispatchInvocationError(
            f"adapter.provider_id {pid!r} does not match mapping key "
            f"{selected_provider!r}"
        )

    return adapter


# ── environment construction ──────────────────────────────────────────────


def _build_env(env_overrides: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Build the subprocess environment from ``os.environ`` plus overrides.

    Does NOT set ``MAD_HOME`` or ``MAD_PARTICIPANT``.
    Returns a **new** dict — ``os.environ`` is never modified.
    """
    env = os.environ.copy()
    for key, value in env_overrides:
        env[key] = value
    return env


# ── process termination ───────────────────────────────────────────────────


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate the entire process tree.

    On Windows: ``taskkill /T /F /PID <pid>``.
    On POSIX: ``SIGTERM`` → 5 s grace → ``SIGKILL``.
    """
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await _terminate_windows(process)
    else:
        await _terminate_posix(process)


async def _terminate_windows(process: asyncio.subprocess.Process) -> None:
    """Windows process‑tree termination via ``taskkill /T /F``."""
    kill = await asyncio.create_subprocess_exec(
        "taskkill", "/PID", str(process.pid), "/T", "/F",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await kill.wait()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass


async def _terminate_posix(process: asyncio.subprocess.Process) -> None:
    """POSIX process‑group termination: SIGTERM → 5 s → SIGKILL."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


# ── main entry point ──────────────────────────────────────────────────────


async def run_dispatch(
    request: DispatchRequest,
    providers: Mapping[str, AgentCliProvider],
) -> DispatchResult:
    """Execute a single agent CLI subprocess dispatch.

    1. Validate ``DispatchRequest`` and its nested types.
    2. Validate ``providers`` mapping and resolve the adapter.
    3. Call ``adapter.build_invocation(request)``.
    4. Validate the returned ``AgentCliInvocation``.
    5. Resolve executable, build environment.
    6. Launch subprocess via ``asyncio.create_subprocess_exec``.
    7. Capture stdout/stderr as raw ``bytes``.
    8. Compute SHA‑256 of captured bytes.
    9. On exit 0 → return ``DispatchResult``.
    10. On non‑zero exit → raise ``DispatchNonZeroExitError``.
    11. On timeout → terminate process tree, raise ``DispatchTimeoutError``.
    12. On cancellation → terminate process tree, raise ``DispatchCancelledError``.

    Every failure path raises a precise exception — ``DispatchResult`` is
    **never** returned for a non‑zero exit, timeout, or cancellation.
    """
    # ── 1. Validate request ───────────────────────────────────────────
    _validate_request(request)
    snapshot = request.model_selection

    # ── 2. Validate providers mapping ─────────────────────────────────
    selected_provider = snapshot.selected_model_provider
    adapter = _validate_providers(providers, selected_provider)

    # ── 3. Build invocation via adapter ───────────────────────────────
    try:
        invocation = adapter.build_invocation(request)
    except Exception as exc:
        raise DispatchInvocationError(
            f"adapter.build_invocation failed: {exc}"
        ) from exc

    # ── 4. Validate invocation ────────────────────────────────────────
    _validate_invocation(invocation)

    # ── 5. Resolve executable ─────────────────────────────────────────
    resolved_executable = _resolve_executable(invocation.executable)

    # ── 6. Build environment ──────────────────────────────────────────
    env = _build_env(invocation.env_overrides)

    # ── 7. Launch subprocess ──────────────────────────────────────────
    started = _time_module.monotonic()

    try:
        if sys.platform == "win32":
            process = await asyncio.create_subprocess_exec(
                resolved_executable,
                *invocation.argv,
                stdin=asyncio.subprocess.PIPE if invocation.stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                resolved_executable,
                *invocation.argv,
                stdin=asyncio.subprocess.PIPE if invocation.stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
    except OSError as exc:
        raise DispatchLaunchError(
            f"failed to launch dispatch subprocess: {exc}"
        ) from exc

    # ── 8. Communicate (with timeout) ─────────────────────────────────
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=invocation.stdin),
            timeout=request.timeout_seconds,
        )
    except (TimeoutError, asyncio.TimeoutError):
        await _terminate_process(process)
        raise DispatchTimeoutError(request.timeout_seconds) from None
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise DispatchCancelledError() from None

    duration = _time_module.monotonic() - started

    # stdout/stderr are bytes from communicate()
    out = stdout_bytes if stdout_bytes is not None else b""
    err = stderr_bytes if stderr_bytes is not None else b""

    exit_code = process.returncode if process.returncode is not None else -1

    # ── 9. Compute SHA‑256 on raw bytes ───────────────────────────────
    stdout_sha256 = _sha256(out)
    stderr_sha256 = _sha256(err)

    # ── 10. Non‑zero exit → exception ─────────────────────────────────
    if exit_code != 0:
        raise DispatchNonZeroExitError(
            exit_code=exit_code,
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            stderr_bytes=err,
        )

    # ── 11. Exit 0 → DispatchResult ───────────────────────────────────
    return DispatchResult(
        identity=request.identity,
        provider=snapshot.selected_model_provider,
        model_id=snapshot.selected_model_id,
        duration_seconds=duration,
        stdout=out,
        stderr=err,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
    )
