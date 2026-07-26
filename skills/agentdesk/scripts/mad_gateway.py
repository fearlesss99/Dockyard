"""AgentDesk MAD Decision Gateway — TC-13.6 (Commit 2).

Config‑driven subprocess invocation of ``mad deliberate --format json``
with full stdout‑validation pipeline, SHA‑256 hashing, fail‑closed
error handling, and gitignored ``agentdesk.mad-refs/v1`` recording.

Public API
----------
``MadGatewayInput``          – frozen :class:`dataclass` with all
                              required invocation parameters.
``MadGatewayResult``         – frozen, fully‑validated result.
``MadGatewayConfig``         – validated ``agentdesk.gateway-config/v1``.
``run_gateway``              – main entry point: config + input →
                              subprocess → validate → mad‑ref → result.

Error hierarchy
---------------
``GatewayError``
  ├─ ``GatewayInputError``            (invalid MadGatewayInput)
  ├─ ``GatewayConfigError``           (invalid config)
  ├─ ``GatewayExecutableNotFoundError`` (mad executable not found)
  ├─ ``GatewayLaunchError``           (subprocess failed to start)
  ├─ ``GatewayTimeoutError``          (subprocess timed out)
  ├─ ``GatewayNonZeroExitError``      (MAD exited non‑zero)
  │    ├─ ``GatewayExit1Error``        (exit 1: workflow / report failure)
  │    ├─ ``GatewayExit2Error``        (exit 2: param / plan / config error)
  │    ├─ ``GatewayExit3Error``        (exit 3: insufficient participants)
  │    ├─ ``GatewayExit130Error``      (exit 130: user cancellation)
  │    └─ ``GatewayUnknownExitError``  (any other non‑zero exit)
  ├─ ``GatewayStdoutNotUtf8Error``    (stdout not valid UTF‑8)
  ├─ ``GatewayStdoutNotJsonError``    (stdout not valid JSON)
  ├─ ``GatewayStdoutRootNotObjectError`` (JSON root not a dict)
  ├─ ``GatewaySchemaVersionError``    (schema_version missing / wrong)
  └─ ``GatewayOutputValidationError`` (required field missing / wrong type)

Non‑goals
---------
* ``mad agents`` pre‑check, ``mad resume``, ``mad audit``.
* WorkerAdapter, WorkerKind mapping, ContextBudgetPolicy.
* Slot, lease, scheduling, retry, escalation, rate‑limit logic.
* Event, outbox, or Git‑tracked state writing.
* Importing MAD internal Python modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core_types import MadDeliberationDepth
import mad_refs

SCHEMA_VERSION_MAD_REFS = mad_refs.SCHEMA_VERSION
MadRefEntry = mad_refs.MadRefEntry

__all__ = [
    "GatewayConfigError",
    "GatewayError",
    "GatewayExecutableNotFoundError",
    "GatewayExit130Error",
    "GatewayExit1Error",
    "GatewayExit2Error",
    "GatewayExit3Error",
    "GatewayInputError",
    "GatewayLaunchError",
    "GatewayNonZeroExitError",
    "GatewayOutputValidationError",
    "GatewaySchemaVersionError",
    "GatewayStdoutNotJsonError",
    "GatewayStdoutNotUtf8Error",
    "GatewayStdoutRootNotObjectError",
    "GatewayTimeoutError",
    "GatewayUnknownExitError",
    "MadGatewayConfig",
    "MadGatewayInput",
    "MadGatewayResult",
    "run_gateway",
]

# ── constants ─────────────────────────────────────────────────────────────

GATEWAY_CONFIG_SCHEMA = "agentdesk.gateway-config/v1"
MAD_RUN_RESULT_SCHEMA = "mad.run-result/v1"

_GATEWAY_CONFIG_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "mad_executable",
    "mad_home",
    "timeout_seconds",
    "planning_agent_ids",
    "planning_report_agent_id",
    "audit_agent_ids",
    "audit_report_agent_id",
})


# ── data types ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MadGatewayInput:
    """Immutable input for the MAD Decision Gateway."""

    project_root: Path
    task_id: str
    dispatch_id: str
    question: str
    workspace: Path
    depth: MadDeliberationDepth
    agent_ids: tuple[str, ...]
    report_agent_id: str


@dataclass(slots=True, frozen=True)
class MadGatewayResult:
    """Immutable, fully‑validated result from a successful MAD deliberation."""

    deliberation_id: str
    status: str
    report: str
    archive_path: str
    participants: tuple[str, ...]
    warnings: tuple[str, ...]
    convergence: dict[str, Any]
    plan: dict[str, Any]
    stdout_sha256: str
    report_sha256: str
    exit_code: int
    duration_seconds: float
    mad_command: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class MadGatewayConfig:
    """Validated ``agentdesk.gateway-config/v1``."""

    mad_executable: str
    mad_home: str                          # absolute path
    timeout_seconds: int
    planning_agent_ids: tuple[str, ...]
    planning_report_agent_id: str
    audit_agent_ids: tuple[str, ...]
    audit_report_agent_id: str


# ── error hierarchy ───────────────────────────────────────────────────────


class GatewayError(Exception):
    """Base for all Gateway errors."""


class GatewayInputError(GatewayError):
    """Invalid ``MadGatewayInput``."""


class GatewayConfigError(GatewayError):
    """Invalid ``agentdesk.gateway-config/v1``."""


class GatewayExecutableNotFoundError(GatewayError):
    """The configured ``mad_executable`` does not exist or is not a file."""


class GatewayLaunchError(GatewayError):
    """Subprocess failed to start (e.g. OSError)."""


class GatewayTimeoutError(GatewayError):
    """Subprocess exceeded ``timeout_seconds``."""


class GatewayNonZeroExitError(GatewayError):
    """MAD exited non‑zero.  ``stderr_preview`` is length‑capped."""

    def __init__(
        self,
        exit_code: int,
        stderr_bytes: bytes = b"",
        stdout_sha256: str = "",
    ) -> None:
        preview = _safe_stderr_preview(stderr_bytes)
        super().__init__(
            f"mad exited with code {exit_code}"
            + (f": {preview}" if preview else "")
        )
        self.exit_code = exit_code
        self.stderr_preview = preview
        self.stdout_sha256 = stdout_sha256


class GatewayExit1Error(GatewayNonZeroExitError):
    """Exit 1 — workflow, recovery, or report failure."""


class GatewayExit2Error(GatewayNonZeroExitError):
    """Exit 2 — parameter, plan, or configuration error."""


class GatewayExit3Error(GatewayNonZeroExitError):
    """Exit 3 — insufficient available participants."""


class GatewayExit130Error(GatewayNonZeroExitError):
    """Exit 130 — user cancellation or SIGINT."""


class GatewayUnknownExitError(GatewayNonZeroExitError):
    """Any non‑zero exit code not in ``{1, 2, 3, 130}``."""


class GatewayStdoutNotUtf8Error(GatewayError):
    """stdout bytes are not valid UTF‑8."""

    def __init__(self, stdout_sha256: str) -> None:
        super().__init__("mad stdout is not valid UTF-8")
        self.stdout_sha256 = stdout_sha256


class GatewayStdoutNotJsonError(GatewayError):
    """stdout is valid UTF‑8 but not valid JSON."""

    def __init__(self, stdout_sha256: str, stdout_preview: str = "") -> None:
        super().__init__("mad stdout is not valid JSON")
        self.stdout_sha256 = stdout_sha256
        self.stdout_preview = stdout_preview[:200]


class GatewayStdoutRootNotObjectError(GatewayError):
    """stdout JSON root is not a dict."""

    def __init__(self, stdout_sha256: str) -> None:
        super().__init__("mad stdout JSON root is not an object")
        self.stdout_sha256 = stdout_sha256


class GatewaySchemaVersionError(GatewayError):
    """``schema_version`` missing, wrong type, or unknown."""

    def __init__(
        self,
        stdout_sha256: str,
        schema_version: Any = None,
        expected: str = MAD_RUN_RESULT_SCHEMA,
    ) -> None:
        super().__init__(
            f"expected schema_version {expected!r}, got {schema_version!r}"
        )
        self.stdout_sha256 = stdout_sha256
        self.schema_version = schema_version


class GatewayOutputValidationError(GatewayError):
    """Required field in MAD output is missing or has wrong type."""

    def __init__(
        self,
        field: str,
        stdout_sha256: str,
        detail: str = "",
    ) -> None:
        super().__init__(
            f"field {field!r} validation failed"
            + (f": {detail}" if detail else "")
        )
        self.field = field
        self.stdout_sha256 = stdout_sha256


# ── helpers ───────────────────────────────────────────────────────────────


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_abs_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and Path(value).is_absolute()


def _safe_stderr_preview(stderr_bytes: bytes, max_len: int = 500) -> str:
    """Return a length‑capped, decoded preview of stderr bytes."""
    if not stderr_bytes:
        return ""
    try:
        text = stderr_bytes.decode("utf-8", errors="replace")
    except Exception:
        return f"[{len(stderr_bytes)} bytes, non-UTF-8]"
    if len(text) <= max_len:
        return text.strip()
    return text[:max_len].strip() + "…"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── config validation ─────────────────────────────────────────────────────


def validate_gateway_config(raw: dict[str, Any]) -> MadGatewayConfig:
    """Validate *raw* against ``agentdesk.gateway-config/v1``.

    Returns a validated :class:`MadGatewayConfig`.  Raises
    :exc:`GatewayConfigError` for any schema violation.
    """
    if not isinstance(raw, dict):
        raise GatewayConfigError("gateway config must be a JSON object")

    # ── exact root keys ───────────────────────────────────────────────
    actual = set(raw.keys())
    missing = _GATEWAY_CONFIG_KEYS - actual
    extra = actual - _GATEWAY_CONFIG_KEYS
    errors: list[str] = []
    if missing:
        errors.append(f"missing key(s): {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"forbidden key(s): {', '.join(sorted(extra))}")
    if errors:
        raise GatewayConfigError("; ".join(errors))

    # ── schema_version ────────────────────────────────────────────────
    sv = raw.get("schema_version")
    if sv != GATEWAY_CONFIG_SCHEMA:
        raise GatewayConfigError(
            f"schema_version must be {GATEWAY_CONFIG_SCHEMA!r}, got {sv!r}"
        )

    # ── mad_executable ────────────────────────────────────────────────
    mad_exe = raw.get("mad_executable")
    if not _is_non_empty_str(mad_exe):
        raise GatewayConfigError("mad_executable must be a non-empty string")
    # Resolve now so we fail fast if not found.
    resolved = _resolve_executable(mad_exe)  # type: ignore[arg-type]
    if resolved is None:
        raise GatewayConfigError(
            f"mad_executable not found or not a file: {mad_exe!r}"
        )

    # ── mad_home ──────────────────────────────────────────────────────
    mad_home = raw.get("mad_home")
    if not _is_abs_path(mad_home):
        raise GatewayConfigError(
            f"mad_home must be an absolute path, got {mad_home!r}"
        )
    home_path = Path(mad_home)  # type: ignore[arg-type]
    agents_toml = home_path / "config" / "agents.toml"
    if not agents_toml.is_file():
        raise GatewayConfigError(
            f"mad_home must contain config/agents.toml: {agents_toml}"
        )

    # ── timeout_seconds ───────────────────────────────────────────────
    timeout = raw.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise GatewayConfigError(
            f"timeout_seconds must be a positive integer, got {timeout!r}"
        )

    # ── planning_agent_ids ────────────────────────────────────────────
    planning_ids = _validate_agent_ids(
        raw.get("planning_agent_ids"), "planning_agent_ids"
    )

    # ── planning_report_agent_id ──────────────────────────────────────
    planning_report = raw.get("planning_report_agent_id")
    if not _is_non_empty_str(planning_report):
        raise GatewayConfigError(
            "planning_report_agent_id must be a non-empty string"
        )
    if planning_report not in planning_ids:
        raise GatewayConfigError(
            f"planning_report_agent_id {planning_report!r} "
            f"must be in planning_agent_ids"
        )

    # ── audit_agent_ids ───────────────────────────────────────────────
    audit_ids = _validate_agent_ids(raw.get("audit_agent_ids"), "audit_agent_ids")

    # ── audit_report_agent_id ─────────────────────────────────────────
    audit_report = raw.get("audit_report_agent_id")
    if not _is_non_empty_str(audit_report):
        raise GatewayConfigError(
            "audit_report_agent_id must be a non-empty string"
        )
    if audit_report not in audit_ids:
        raise GatewayConfigError(
            f"audit_report_agent_id {audit_report!r} "
            f"must be in audit_agent_ids"
        )

    return MadGatewayConfig(
        mad_executable=resolved,  # type: ignore[arg-type]
        mad_home=mad_home,  # type: ignore[arg-type]
        timeout_seconds=timeout,  # type: ignore[arg-type]
        planning_agent_ids=planning_ids,
        planning_report_agent_id=planning_report,  # type: ignore[arg-type]
        audit_agent_ids=audit_ids,
        audit_report_agent_id=audit_report,  # type: ignore[arg-type]
    )


def _validate_agent_ids(
    value: Any,
    field_name: str,
) -> tuple[str, ...]:
    """Validate an agent‑ID list field.  Returns a tuple of unique IDs."""
    if not isinstance(value, list) or not value:
        raise GatewayConfigError(
            f"{field_name} must be a non-empty list, got {value!r}"
        )
    ids: list[str] = []
    for i, item in enumerate(value):
        if not _is_non_empty_str(item):
            raise GatewayConfigError(
                f"{field_name}[{i}] must be a non-empty string, got {item!r}"
            )
        ids.append(item)  # type: ignore[arg-type]
    if len(set(ids)) != len(ids):
        dups = sorted({v for v in ids if ids.count(v) > 1})
        raise GatewayConfigError(
            f"{field_name} must not contain duplicates: {', '.join(dups)}"
        )
    return tuple(ids)


# ── executable resolution ─────────────────────────────────────────────────


def _resolve_executable(candidate: str) -> str | None:
    """Resolve *candidate* to an absolute executable path.

    Mirrors MAD's own ``CliAdapter.executable`` property.
    """
    if "/" in candidate or "\\" in candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which(candidate)


# ── input validation ──────────────────────────────────────────────────────


def validate_gateway_input(inp: MadGatewayInput) -> None:
    """Validate a :class:`MadGatewayInput` is structurally sound.

    Raises :exc:`GatewayInputError` for any violation.
    """
    if not isinstance(inp.project_root, Path) or not inp.project_root.is_dir():
        raise GatewayInputError(f"project_root is not a directory: {inp.project_root!r}")
    if not _is_non_empty_str(inp.task_id):
        raise GatewayInputError("task_id must be a non-empty string")
    if not _is_non_empty_str(inp.dispatch_id):
        raise GatewayInputError("dispatch_id must be a non-empty string")
    if not isinstance(inp.question, str) or not inp.question.strip():
        raise GatewayInputError("question must be a non-empty string")
    if not isinstance(inp.workspace, Path) or not inp.workspace.is_dir():
        raise GatewayInputError(f"workspace is not a directory: {inp.workspace!r}")
    if not isinstance(inp.depth, MadDeliberationDepth):
        raise GatewayInputError(
            f"depth must be a MadDeliberationDepth, "
            f"got {type(inp.depth).__name__}: {inp.depth!r}"
        )
    if not isinstance(inp.agent_ids, tuple) or not inp.agent_ids or \
       not all(_is_non_empty_str(a) for a in inp.agent_ids):
        raise GatewayInputError("agent_ids must be a non-empty tuple of non-empty strings")
    if len(set(inp.agent_ids)) != len(inp.agent_ids):
        raise GatewayInputError("agent_ids must not contain duplicates")
    if not _is_non_empty_str(inp.report_agent_id):
        raise GatewayInputError("report_agent_id must be a non-empty string")
    if inp.report_agent_id not in inp.agent_ids:
        raise GatewayInputError(
            f"report_agent_id {inp.report_agent_id!r} must be in agent_ids"
        )


# ── command construction ──────────────────────────────────────────────────


def _build_command(
    exe: str,
    inp: MadGatewayInput,
) -> tuple[str, ...]:
    """Build the exact ``mad deliberate --format json`` command array.

    Returns an immutable tuple of strings — never a shell string.
    """
    return (
        exe,
        "deliberate",
        inp.question,
        "--workspace",
        str(inp.workspace),
        "--agents",
        ",".join(inp.agent_ids),
        "--report-agent",
        inp.report_agent_id,
        "--depth",
        inp.depth.value,
        "--confirm-plan",
        "--format",
        "json",
    )


# ── subprocess environment ────────────────────────────────────────────────


def _build_env(mad_home: str) -> dict[str, str]:
    """Build the subprocess environment.

    Copies the current environment and sets ONLY ``MAD_HOME``.
    Does NOT set ``MAD_PARTICIPANT`` — MAD sets that internally.
    """
    env = os.environ.copy()
    env["MAD_HOME"] = mad_home
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


# ── stdout validation pipeline ────────────────────────────────────────────


def _validate_mad_output(
    root: dict[str, Any],
    stdout_sha256: str,
) -> MadGatewayResult:
    """Validate consumed fields from ``mad.run-result/v1`` output.

    Forward‑compatible: allows extra top‑level keys, strictly validates
    consumed fields.
    """
    # ── schema_version ────────────────────────────────────────────────
    sv = root.get("schema_version")
    if sv != MAD_RUN_RESULT_SCHEMA:
        raise GatewaySchemaVersionError(stdout_sha256, sv)

    # ── deliberation_id ───────────────────────────────────────────────
    did = root.get("deliberation_id")
    if not _is_non_empty_str(did):
        raise GatewayOutputValidationError(
            "deliberation_id", stdout_sha256,
            f"must be non-empty string, got {did!r}"
        )

    # ── status ────────────────────────────────────────────────────────
    status = root.get("status")
    if not _is_non_empty_str(status):
        raise GatewayOutputValidationError(
            "status", stdout_sha256,
            f"must be non-empty string, got {status!r}"
        )

    # ── report ────────────────────────────────────────────────────────
    report = root.get("report")
    if not isinstance(report, str):
        raise GatewayOutputValidationError(
            "report", stdout_sha256,
            f"must be string, got {type(report).__name__}"
        )

    # ── archive_path must be absolute ─────────────────────────────────
    archive = root.get("archive_path")
    if not _is_abs_path(archive):
        raise GatewayOutputValidationError(
            "archive_path", stdout_sha256,
            f"must be absolute path, got {archive!r}"
        )

    # ── warnings ──────────────────────────────────────────────────────
    warnings_val = root.get("warnings")
    if not isinstance(warnings_val, list) or \
       not all(isinstance(w, str) for w in warnings_val):
        raise GatewayOutputValidationError(
            "warnings", stdout_sha256,
            "must be list[str]"
        )

    # ── participants ──────────────────────────────────────────────────
    participants_val = root.get("participants")
    if not isinstance(participants_val, list) or \
       not all(isinstance(p, str) for p in participants_val):
        raise GatewayOutputValidationError(
            "participants", stdout_sha256,
            "must be list[str]"
        )

    # ── convergence ───────────────────────────────────────────────────
    conv = root.get("convergence")
    if not isinstance(conv, dict):
        raise GatewayOutputValidationError(
            "convergence", stdout_sha256,
            "must be an object"
        )

    # ── plan ──────────────────────────────────────────────────────────
    plan = root.get("plan")
    if not isinstance(plan, dict):
        raise GatewayOutputValidationError(
            "plan", stdout_sha256,
            "must be an object"
        )

    report_sha256 = hashlib.sha256(report.encode("utf-8")).hexdigest()

    return MadGatewayResult(
        deliberation_id=did,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        report=report,
        archive_path=archive,  # type: ignore[arg-type]
        participants=tuple(participants_val),  # type: ignore[arg-type]
        warnings=tuple(warnings_val),  # type: ignore[arg-type]
        convergence=conv,
        plan=plan,
        stdout_sha256=stdout_sha256,
        report_sha256=report_sha256,
        exit_code=0,
        duration_seconds=0.0,   # filled by caller
        mad_command=(),          # filled by caller
    )


# ── main entry point ──────────────────────────────────────────────────────


async def run_gateway(
    config: MadGatewayConfig,
    inp: MadGatewayInput,
) -> MadGatewayResult:
    """Run the MAD Decision Gateway end‑to‑end.

    1. Validate input.
    2. Build command & environment.
    3. Launch subprocess.
    4. Capture stdout / stderr as raw bytes.
    5. Validate exit code.
    6. Compute SHA‑256 of raw stdout bytes.
    7. UTF‑8 decode → JSON parse → schema validate.
    8. Extract & validate consumed fields.
    9. Compute SHA‑256 of report.
    10. Append ``MadRefEntry`` to ``mad-refs.yaml``.
    11. Return ``MadGatewayResult``.

    Any failure is fail‑closed — no mad‑ref is written and no partial
    data is returned.
    """
    import time as _time_module

    validate_gateway_input(inp)

    command = _build_command(config.mad_executable, inp)
    env = _build_env(config.mad_home)
    started = _time_module.monotonic()

    try:
        if sys.platform == "win32":
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(inp.project_root),
                env=env,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(inp.project_root),
                env=env,
                start_new_session=True,
            )
    except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
        raise GatewayLaunchError(
            f"failed to launch mad subprocess: {exc}"
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=config.timeout_seconds
        )
    except (TimeoutError, asyncio.TimeoutError):
        await _terminate_process(process)
        raise GatewayTimeoutError(
            f"mad subprocess timed out after {config.timeout_seconds}s"
        ) from None

    duration = _time_module.monotonic() - started
    exit_code = process.returncode if process.returncode is not None else -1

    # ── compute stdout SHA‑256 BEFORE any processing ──────────────────
    stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()

    # ── exit code validation ──────────────────────────────────────────
    if exit_code != 0:
        _raise_non_zero_exit(exit_code, stderr_bytes, stdout_sha256)

    # ── empty stdout? ─────────────────────────────────────────────────
    if len(stdout_bytes) == 0:
        raise GatewayStdoutNotJsonError(stdout_sha256, "(empty stdout)")

    # ── UTF‑8 decode ──────────────────────────────────────────────────
    try:
        stdout_text = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise GatewayStdoutNotUtf8Error(stdout_sha256) from None

    # ── JSON parse ────────────────────────────────────────────────────
    try:
        root = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise GatewayStdoutNotJsonError(
            stdout_sha256, stdout_text[:200]
        ) from exc

    # ── root must be object ───────────────────────────────────────────
    if not isinstance(root, dict):
        raise GatewayStdoutRootNotObjectError(stdout_sha256)

    # ── validate consumed fields ──────────────────────────────────────
    result = _validate_mad_output(root, stdout_sha256)
    duration_seconds = duration

    # ── append mad‑ref ────────────────────────────────────────────────
    ref_entry = MadRefEntry(
        task_id=inp.task_id,
        dispatch_id=inp.dispatch_id,
        purpose="planning",
        deliberation_id=result.deliberation_id,
        depth=inp.depth.value,
        stdout_sha256=stdout_sha256,
        report_sha256=result.report_sha256,
        status=result.status,
        archive_path=result.archive_path,
        created_at=_utc_now(),
    )
    mad_refs.append_mad_ref(inp.project_root, ref_entry)

    # Return with timing info filled in.
    return MadGatewayResult(
        deliberation_id=result.deliberation_id,
        status=result.status,
        report=result.report,
        archive_path=result.archive_path,
        participants=result.participants,
        warnings=result.warnings,
        convergence=result.convergence,
        plan=result.plan,
        stdout_sha256=stdout_sha256,
        report_sha256=result.report_sha256,
        exit_code=0,
        duration_seconds=duration_seconds,
        mad_command=command,
    )


def _raise_non_zero_exit(
    exit_code: int,
    stderr_bytes: bytes,
    stdout_sha256: str,
) -> None:
    """Raise the appropriate :exc:`GatewayNonZeroExitError` subclass."""
    cls_map: dict[int, type[GatewayNonZeroExitError]] = {
        1: GatewayExit1Error,
        2: GatewayExit2Error,
        3: GatewayExit3Error,
        130: GatewayExit130Error,
    }
    cls = cls_map.get(exit_code, GatewayUnknownExitError)
    raise cls(exit_code, stderr_bytes, stdout_sha256)
