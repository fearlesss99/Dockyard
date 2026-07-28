"""AgentDesk MAD Audit Gateway — TC-13.16b.

Subprocess invocation of ``mad audit --format json`` with full
stdout‑validation pipeline, SHA‑256 hashing, fail‑closed error handling,
and gitignored ``agentdesk.mad-refs/v1`` recording.

Public API
----------
``MadAuditGatewayInput``      – frozen :class:`dataclass` with the 12
                                required invocation parameters.
``MadAuditIssueLocation``     – 3‑field immutable audit issue location.
``MadAuditIssue``             – 7‑field immutable audit finding.
``MadAuditEvidence``           – 5‑field immutable evidence record.
``MadAuditPlan``               – 1‑field immutable plan (depth only).
``MadAuditGatewayResult``      – 12‑field fully‑validated result.
``run_audit_gateway``          – main entry point.

Reuses :class:`MadGatewayConfig`, ``MadRefEntry``, and the full Gateway
exception hierarchy from ``mad_gateway`` — no parallel exception classes.

Non‑goals
---------
* Worktree creation, removal, or pruning.
* Writing tasks, events, outbox, acceptance, or Git‑tracked audit events.
* Importing MAD internal Python modules.
* Reading files inside the MAD archive.
* Git fetch / pull / push.
* Setting ``MAD_PARTICIPANT``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core_types import MadDeliberationDepth
import mad_refs
from mad_gateway import (
    GatewayError,
    GatewayExit1Error,
    GatewayExit2Error,
    GatewayExit3Error,
    GatewayExit130Error,
    GatewayExecutableNotFoundError,
    GatewayInputError,
    GatewayLaunchError,
    GatewayNonZeroExitError,
    GatewayOutputValidationError,
    GatewaySchemaVersionError,
    GatewayStdoutNotJsonError,
    GatewayStdoutNotUtf8Error,
    GatewayStdoutRootNotObjectError,
    GatewayTimeoutError,
    GatewayUnknownExitError,
    MadGatewayConfig,
    _terminate_process,
)

MadRefEntry = mad_refs.MadRefEntry


# ── data types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MadAuditGatewayInput:
    """Immutable input for the MAD Audit Gateway — exactly 12 fields."""

    project_root: Path
    task_id: str
    dispatch_id: str
    question: str
    workspace: Path
    task_card_commit: str
    task_card_path: str
    delivery_report_path: str
    report_commit: str
    base_commit: str
    implementation_commit: str
    depth: MadDeliberationDepth


@dataclass(frozen=True, slots=True)
class MadAuditIssueLocation:
    """Immutable location of an audit finding — exactly 3 fields."""

    file: str
    line: str | None
    commit: str


@dataclass(frozen=True, slots=True)
class MadAuditIssue:
    """Immutable audit finding — exactly 7 fields."""

    id: str
    severity: str
    category: str
    title: str
    description: str
    location: MadAuditIssueLocation
    recommendation: str


@dataclass(frozen=True, slots=True)
class MadAuditEvidence:
    """Immutable evidence record — exactly 5 fields."""

    ref: str
    type: str
    source: str
    summary: str
    verified: bool


@dataclass(frozen=True, slots=True)
class MadAuditPlan:
    """Immutable audit plan — exactly 1 field (matches MAD Current output)."""

    depth: MadDeliberationDepth


@dataclass(frozen=True, slots=True)
class MadAuditGatewayResult:
    """Immutable, fully‑validated audit result — exactly 12 fields."""

    deliberation_id: str
    status: str
    verdict: str
    issues: tuple[MadAuditIssue, ...]
    evidence: tuple[MadAuditEvidence, ...]
    warnings: tuple[str, ...]
    report: str
    archive_path: str
    participants: tuple[str, ...]
    plan: MadAuditPlan
    stdout_sha256: str
    report_sha256: str


__all__ = [
    "MadAuditGatewayInput",
    "MadAuditIssueLocation",
    "MadAuditIssue",
    "MadAuditEvidence",
    "MadAuditPlan",
    "MadAuditGatewayResult",
    "run_audit_gateway",
]

# ── constants ─────────────────────────────────────────────────────────────

MAD_AUDIT_RESULT_SCHEMA = "mad.audit-result/v1"

_AUDIT_ROOT_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "deliberation_id",
    "status",
    "verdict",
    "issues",
    "evidence",
    "warnings",
    "report",
    "archive_path",
    "participants",
    "plan",
})

_ISSUE_KEYS: frozenset[str] = frozenset({
    "id", "severity", "category", "title",
    "description", "location", "recommendation",
})

_LOCATION_KEYS: frozenset[str] = frozenset({"file", "line", "commit"})

_EVIDENCE_KEYS: frozenset[str] = frozenset({
    "ref", "type", "source", "summary", "verified",
})

_ALLOWED_VERDICTS: frozenset[str] = frozenset({"pass", "fail", "blocked"})

_ALLOWED_SEVERITIES: frozenset[str] = frozenset({
    "critical", "high", "medium", "low", "info",
})

_ALLOWED_CATEGORIES: frozenset[str] = frozenset({
    "security", "correctness", "completeness",
    "consistency", "evidence", "process",
})

_ALLOWED_EVIDENCE_TYPES: frozenset[str] = frozenset({
    "git-ancestry", "git-diff", "file-content",
    "commit-message", "check-output", "model-output",
})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Safe repo-relative path: no absolute, no backslash, no . or .. segments,
# no NUL / CR / LF.
_SAFE_PATH_RE = re.compile(
    r"^(?![/\\]|[.][.]?(?:[/\\]|$))"
    r"[^\x00-\x1f\\]+"
    r"(?:[/][^\x00-\x1f\\]+)*$"
)


# ── helpers ───────────────────────────────────────────────────────────────


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_stderr_preview(stderr_bytes: bytes, max_len: int = 500) -> str:
    """Length‑capped, decoded preview — never leaks full stderr."""
    if not stderr_bytes:
        return ""
    try:
        text = stderr_bytes.decode("utf-8", errors="replace")
    except Exception:
        return f"[{len(stderr_bytes)} bytes, non-UTF-8]"
    if len(text) <= max_len:
        return text.strip()
    return text[:max_len].strip() + "…"


# ── validation helpers ────────────────────────────────────────────────────


def _validate_sha(value: Any, field_name: str) -> str:
    """Validate *value* is a 40-char lowercase hex SHA.  Returns the string."""
    if not isinstance(value, str):
        raise GatewayInputError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    if not _SHA256_RE.fullmatch(value):
        raise GatewayInputError(
            f"{field_name} must be 64 lowercase hex characters"
        )
    return value


def _validate_safe_path(value: Any, field_name: str) -> str:
    """Validate *value* is a safe repo-relative POSIX path.

    Rejects absolute paths, backslashes, ``.`` / ``..`` segments,
    NUL, CR, LF.
    """
    if not _is_non_empty_str(value):
        raise GatewayInputError(
            f"{field_name} must be a non-empty string"
        )
    if not _SAFE_PATH_RE.match(value):
        raise GatewayInputError(
            f"{field_name} is not a safe repo-relative path"
        )
    return value


# ── input validation ──────────────────────────────────────────────────────


def _validate_audit_input(
    config: MadGatewayConfig,
    inp: MadAuditGatewayInput,
) -> None:
    """Validate *inp* and cross‑reference with *config*.

    Raises :exc:`GatewayInputError` for any violation.
    Exception messages must not contain user‑supplied values.
    """
    # ── config ─────────────────────────────────────────────────────────
    if not isinstance(config, MadGatewayConfig):
        raise GatewayInputError(
            f"config must be MadGatewayConfig, "
            f"got {type(config).__name__}"
        )

    # ── project_root ───────────────────────────────────────────────────
    if not isinstance(inp.project_root, Path) or not inp.project_root.is_dir():
        raise GatewayInputError("project_root must be an absolute directory")
    if not inp.project_root.is_absolute():
        raise GatewayInputError("project_root must be absolute")

    # ── task_id / dispatch_id ──────────────────────────────────────────
    if not _is_non_empty_str(inp.task_id):
        raise GatewayInputError("task_id must be a non-empty string")
    if not _is_non_empty_str(inp.dispatch_id):
        raise GatewayInputError("dispatch_id must be a non-empty string")

    # ── question ───────────────────────────────────────────────────────
    if not isinstance(inp.question, str) or not inp.question.strip():
        raise GatewayInputError("question must be a non-empty string")

    # ── workspace ──────────────────────────────────────────────────────
    if not isinstance(inp.workspace, Path) or not inp.workspace.is_dir():
        raise GatewayInputError("workspace must be an absolute directory")
    if not inp.workspace.is_absolute():
        raise GatewayInputError("workspace must be absolute")

    # ── depth ──────────────────────────────────────────────────────────
    if not isinstance(inp.depth, MadDeliberationDepth):
        raise GatewayInputError(
            "depth must be a MadDeliberationDepth, "
            f"got {type(inp.depth).__name__}"
        )

    # ── four commit SHA fields ─────────────────────────────────────────
    _validate_sha(inp.task_card_commit, "task_card_commit")
    _validate_sha(inp.report_commit, "report_commit")
    _validate_sha(inp.base_commit, "base_commit")
    _validate_sha(inp.implementation_commit, "implementation_commit")

    # ── safe repo-relative paths ───────────────────────────────────────
    _validate_safe_path(inp.task_card_path, "task_card_path")
    _validate_safe_path(inp.delivery_report_path, "delivery_report_path")

    # ── agent list from config only ────────────────────────────────────
    if not config.audit_agent_ids:
        raise GatewayInputError("config.audit_agent_ids must be non-empty")
    if not _is_non_empty_str(config.audit_report_agent_id):
        raise GatewayInputError(
            "config.audit_report_agent_id must be non-empty"
        )
    if config.audit_report_agent_id not in config.audit_agent_ids:
        raise GatewayInputError(
            "audit_report_agent_id must be in audit_agent_ids"
        )


# ── command construction ──────────────────────────────────────────────────


def _build_audit_command(
    config: MadGatewayConfig,
    inp: MadAuditGatewayInput,
) -> tuple[str, ...]:
    """Build the exact ``mad audit --format json`` command array.

    Returns an immutable tuple of strings — never a shell string.
    """
    return (
        config.mad_executable,
        "audit",
        inp.question,
        "--workspace",
        str(inp.workspace),
        "--task-card-commit",
        inp.task_card_commit,
        "--task-card-path",
        inp.task_card_path,
        "--delivery-report-path",
        inp.delivery_report_path,
        "--report-commit",
        inp.report_commit,
        "--base-commit",
        inp.base_commit,
        "--implementation-commit",
        inp.implementation_commit,
        "--agents",
        ",".join(config.audit_agent_ids),
        "--report-agent",
        config.audit_report_agent_id,
        "--depth",
        inp.depth.value,
        "--convergence",
        "auto",
        "--confirm-plan",
        "--format",
        "json",
    )


# ── subprocess environment ────────────────────────────────────────────────


def _build_env(mad_home: str) -> dict[str, str]:
    """Copy parent environment and set ONLY ``MAD_HOME``.

    Does NOT set ``MAD_PARTICIPANT`` — MAD handles that internally.
    """
    env = os.environ.copy()
    env["MAD_HOME"] = mad_home
    return env


# ── non‑zero exit dispatch ────────────────────────────────────────────────


def _raise_non_zero_exit(
    exit_code: int,
    stdout_sha256: str,
) -> None:
    """Raise the appropriate exception for a non‑zero exit.

    Constructs the exception WITHOUT raw stderr — per TC‑13.16b the
    non‑zero exit exception must NOT leak stderr content.
    """
    cls_map: dict[int, type[GatewayNonZeroExitError]] = {
        1: GatewayExit1Error,
        2: GatewayExit2Error,
        3: GatewayExit3Error,
        130: GatewayExit130Error,
    }
    cls = cls_map.get(exit_code, GatewayUnknownExitError)
    # Construct without stderr bytes — leak‑free.
    raise cls(exit_code, b"", stdout_sha256)


# ── output validation ─────────────────────────────────────────────────────


def _validate_issues(issues_raw: list[dict[str, Any]]) -> tuple[MadAuditIssue, ...]:
    """Parse and validate the ``issues`` array.

    Each issue must have exactly the 7 frozen keys, correct types,
    and a 3‑field location.  Fail‑closed on any mismatch.
    """
    result: list[MadAuditIssue] = []
    for i, raw in enumerate(issues_raw):
        if not isinstance(raw, dict):
            raise GatewayOutputValidationError(
                f"issues[{i}]", "", "must be an object"
            )
        actual = set(raw.keys())
        missing = _ISSUE_KEYS - actual
        extra = actual - _ISSUE_KEYS
        if missing or extra:
            raise GatewayOutputValidationError(
                f"issues[{i}]", "",
                f"must have exactly 7 keys; "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

        loc_raw = raw["location"]
        if not isinstance(loc_raw, dict):
            raise GatewayOutputValidationError(
                f"issues[{i}].location", "", "must be an object"
            )
        loc_actual = set(loc_raw.keys())
        loc_missing = _LOCATION_KEYS - loc_actual
        loc_extra = loc_actual - _LOCATION_KEYS
        if loc_missing or loc_extra:
            raise GatewayOutputValidationError(
                f"issues[{i}].location", "",
                f"must have exactly 3 keys; "
                f"missing={sorted(loc_missing)} extra={sorted(loc_extra)}"
            )

        # Validate severity enum
        sev = raw.get("severity")
        if sev not in _ALLOWED_SEVERITIES:
            raise GatewayOutputValidationError(
                f"issues[{i}].severity", "",
                f"unknown severity value"
            )

        # Validate category enum
        cat = raw.get("category")
        if cat not in _ALLOWED_CATEGORIES:
            raise GatewayOutputValidationError(
                f"issues[{i}].category", "",
                f"unknown category value"
            )

        loc = MadAuditIssueLocation(
            file=loc_raw["file"],
            line=loc_raw.get("line"),
            commit=loc_raw["commit"],
        )
        result.append(MadAuditIssue(
            id=raw["id"],
            severity=sev,
            category=cat,
            title=raw["title"],
            description=raw["description"],
            location=loc,
            recommendation=raw["recommendation"],
        ))
    return tuple(result)


def _validate_evidence(
    evidence_raw: list[dict[str, Any]],
) -> tuple[MadAuditEvidence, ...]:
    """Parse and validate the ``evidence`` array.

    Each item must have exactly 5 keys and ``verified`` must be a strict
    ``bool``.
    """
    result: list[MadAuditEvidence] = []
    for i, raw in enumerate(evidence_raw):
        if not isinstance(raw, dict):
            raise GatewayOutputValidationError(
                f"evidence[{i}]", "", "must be an object"
            )
        actual = set(raw.keys())
        missing = _EVIDENCE_KEYS - actual
        extra = actual - _EVIDENCE_KEYS
        if missing or extra:
            raise GatewayOutputValidationError(
                f"evidence[{i}]", "",
                f"must have exactly 5 keys; "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

        # verified must be strict bool
        verified = raw["verified"]
        if verified is not True and verified is not False:
            raise GatewayOutputValidationError(
                f"evidence[{i}].verified", "",
                f"must be bool, got {type(verified).__name__}"
            )

        ev_type = raw.get("type")
        if ev_type not in _ALLOWED_EVIDENCE_TYPES:
            raise GatewayOutputValidationError(
                f"evidence[{i}].type", "",
                f"unknown evidence type"
            )

        result.append(MadAuditEvidence(
            ref=raw["ref"],
            type=ev_type,
            source=raw["source"],
            summary=raw["summary"],
            verified=verified,
        ))
    return tuple(result)


def _validate_audit_output(
    root: dict[str, Any],
    stdout_sha256: str,
    config: MadGatewayConfig,
    inp: MadAuditGatewayInput,
) -> MadAuditGatewayResult:
    """Validate consumed fields from ``mad.audit-result/v1`` output.

    Fail‑closed: unknown keys, wrong types, unknown enum values all raise.
    Exception messages never contain report text, issue descriptions,
    raw stdout, workspace, or secrets.
    """
    # ── exact 11 root keys ────────────────────────────────────────────
    actual_keys = set(root.keys())
    missing = _AUDIT_ROOT_KEYS - actual_keys
    extra = actual_keys - _AUDIT_ROOT_KEYS
    if missing or extra:
        raise GatewayOutputValidationError(
            "root", stdout_sha256,
            f"must have exactly 11 keys; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    # ── schema_version ─────────────────────────────────────────────────
    sv = root["schema_version"]
    if sv != MAD_AUDIT_RESULT_SCHEMA:
        raise GatewaySchemaVersionError(
            stdout_sha256, sv, MAD_AUDIT_RESULT_SCHEMA
        )

    # ── deliberation_id ────────────────────────────────────────────────
    did = root["deliberation_id"]
    if not _is_non_empty_str(did):
        raise GatewayOutputValidationError(
            "deliberation_id", stdout_sha256,
            "must be non-empty string"
        )

    # ── status must be exactly "completed" ─────────────────────────────
    status = root["status"]
    if status != "completed":
        raise GatewayOutputValidationError(
            "status", stdout_sha256,
            "must be the string 'completed'"
        )

    # ── verdict ────────────────────────────────────────────────────────
    verdict = root["verdict"]
    if verdict not in _ALLOWED_VERDICTS:
        raise GatewayOutputValidationError(
            "verdict", stdout_sha256,
            f"must be pass/fail/blocked"
        )

    # ── report ─────────────────────────────────────────────────────────
    report = root["report"]
    if not isinstance(report, str):
        raise GatewayOutputValidationError(
            "report", stdout_sha256,
            "must be string"
        )
    report_sha256 = hashlib.sha256(report.encode("utf-8")).hexdigest()

    # ── archive_path must be absolute ──────────────────────────────────
    archive = root["archive_path"]
    if not isinstance(archive, str) or not Path(archive).is_absolute():
        raise GatewayOutputValidationError(
            "archive_path", stdout_sha256,
            "must be an absolute path"
        )

    # ── warnings ───────────────────────────────────────────────────────
    warnings_val = root["warnings"]
    if not isinstance(warnings_val, list) or \
       not all(isinstance(w, str) for w in warnings_val):
        raise GatewayOutputValidationError(
            "warnings", stdout_sha256, "must be list[str]"
        )

    # ── participants — must be non-empty, no duplicates, match
    #     config.audit_agent_ids order and values ────────────────────────
    participants_raw = root["participants"]
    if not isinstance(participants_raw, list) or \
       not all(isinstance(p, str) for p in participants_raw):
        raise GatewayOutputValidationError(
            "participants", stdout_sha256, "must be list[str]"
        )
    if not participants_raw:
        raise GatewayOutputValidationError(
            "participants", stdout_sha256, "must not be empty"
        )
    if len(set(participants_raw)) != len(participants_raw):
        raise GatewayOutputValidationError(
            "participants", stdout_sha256, "must not contain duplicates"
        )
    participants = tuple(participants_raw)

    # ── plan must be exactly {"depth": "<value>"} ──────────────────────
    plan_raw = root["plan"]
    if not isinstance(plan_raw, dict):
        raise GatewayOutputValidationError(
            "plan", stdout_sha256, "must be an object"
        )
    plan_keys = set(plan_raw.keys())
    if plan_keys != {"depth"}:
        raise GatewayOutputValidationError(
            "plan", stdout_sha256,
            "must have exactly the key 'depth'"
        )
    plan_depth_str = plan_raw["depth"]
    if plan_depth_str != inp.depth.value:
        raise GatewayOutputValidationError(
            "plan.depth", stdout_sha256,
            f"must equal input depth value"
        )
    try:
        plan_depth = MadDeliberationDepth(plan_depth_str)
    except ValueError:
        raise GatewayOutputValidationError(
            "plan.depth", stdout_sha256,
            f"unknown depth value"
        )

    # ── issues ─────────────────────────────────────────────────────────
    issues_raw = root["issues"]
    if not isinstance(issues_raw, list):
        raise GatewayOutputValidationError(
            "issues", stdout_sha256, "must be an array"
        )
    issues = _validate_issues(issues_raw)

    # ── evidence ───────────────────────────────────────────────────────
    evidence_raw = root["evidence"]
    if not isinstance(evidence_raw, list):
        raise GatewayOutputValidationError(
            "evidence", stdout_sha256, "must be an array"
        )
    evidence = _validate_evidence(evidence_raw)

    plan = MadAuditPlan(depth=plan_depth)

    return MadAuditGatewayResult(
        deliberation_id=did,
        status=status,
        verdict=verdict,
        issues=issues,
        evidence=evidence,
        warnings=tuple(warnings_val),
        report=report,
        archive_path=archive,
        participants=participants,
        plan=plan,
        stdout_sha256=stdout_sha256,
        report_sha256=report_sha256,
    )


# ── main entry point ──────────────────────────────────────────────────────


async def run_audit_gateway(
    config: MadGatewayConfig,
    inp: MadAuditGatewayInput,
) -> MadAuditGatewayResult:
    """Run the MAD Audit Gateway end‑to‑end.

    1. Validate input + cross‑reference config.
    2. Build command & environment.
    3. Launch subprocess with ``cwd=str(inp.workspace)``.
    4. Capture stdout / stderr as raw bytes.
    5. Validate exit code.
    6. Compute SHA‑256 of raw stdout bytes.
    7. UTF‑8 decode → JSON parse → schema validate.
    8. Extract & validate all 11 fields + nested types.
    9. Compute SHA‑256 of report.
    10. Append ``MadRefEntry`` (``purpose="audit"``) to ``mad-refs.yaml``.
    11. Return ``MadAuditGatewayResult``.

    Any failure is fail‑closed — no mad‑ref is written and no partial
    data is returned.
    """
    import time as _time_module

    _validate_audit_input(config, inp)

    command = _build_audit_command(config, inp)
    env = _build_env(config.mad_home)
    started = _time_module.monotonic()

    # ── launch subprocess ──────────────────────────────────────────────
    try:
        if sys.platform == "win32":
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(inp.workspace),
                env=env,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(inp.workspace),
                env=env,
                start_new_session=True,
            )
    except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
        raise GatewayLaunchError(
            f"failed to launch mad audit subprocess: {exc}"
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=config.timeout_seconds
        )
    except (TimeoutError, asyncio.TimeoutError):
        await _terminate_process(process)
        raise GatewayTimeoutError(
            f"mad audit subprocess timed out after {config.timeout_seconds}s"
        ) from None

    exit_code = process.returncode if process.returncode is not None else -1

    # ── compute stdout SHA‑256 BEFORE any processing ──────────────────
    stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()

    # ── exit code validation ──────────────────────────────────────────
    if exit_code != 0:
        _raise_non_zero_exit(exit_code, stdout_sha256)

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
    result = _validate_audit_output(root, stdout_sha256, config, inp)

    # ── append mad‑ref (purpose="audit") ──────────────────────────────
    ref_entry = MadRefEntry(
        task_id=inp.task_id,
        dispatch_id=inp.dispatch_id,
        purpose="audit",
        deliberation_id=result.deliberation_id,
        depth=inp.depth.value,
        stdout_sha256=stdout_sha256,
        report_sha256=result.report_sha256,
        status=result.status,
        archive_path=result.archive_path,
        created_at=_utc_now(),
    )
    mad_refs.append_mad_ref(inp.project_root, ref_entry)

    return result
