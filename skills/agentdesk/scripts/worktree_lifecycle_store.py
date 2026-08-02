"""Durable evidence Store and pure reconciliation for managed worktrees.

TC-13.25b deliberately contains no Git worktree side effect.  It parses a
caller-supplied porcelain inventory, stores canonical evidence, and returns
typed reconciliation decisions.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterator

from dispatch_supervisor_evidence import _fsync_parent_directory, _is_symlink_or_reparse

SCHEMA_VERSION = "agentdesk.worktree-lifecycle/v1"
STORE_RELATIVE = Path("docs") / "pm" / "worktree-lifecycle"
RESERVATIONS_DIRECTORY = "reservations"
RECORDS_DIRECTORY = "records"
LOCK_FILENAME = ".worktree-lifecycle.lock"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_TASK_RE = re.compile(r"TC-[0-9]{3,}\Z")
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)

_RESERVATION_FIELDS = (
    "schema_version", "reservation_id", "worktree_id", "repository_common_dir",
    "repository_root", "worktree_path", "branch", "base_commit", "task_id",
    "revision", "attempt", "dispatch_id", "phase", "reserved_at", "content_digest",
)
_RECORD_FIELDS = (
    "schema_version", "worktree_id", "reservation_id", "repository_common_dir",
    "repository_root", "worktree_path", "branch", "base_commit", "head_commit",
    "task_id", "revision", "attempt", "dispatch_id", "phase", "created_at",
    "release_requested_at", "released_at", "inventory_digest", "content_digest",
)
_DECISION_FIELDS = (
    "schema_version", "worktree_id", "action", "reason", "expected_phase",
    "observed_present", "observed_head", "observed_branch", "observed_clean",
    "content_digest",
)
_RECONCILIATION_REASONS = frozenset(
    {
        "reservation_missing", "resume_create", "adopt_ready", "ready_exact",
        "resume_release", "released_exact", "identity_divergent",
        "inventory_missing", "dirty_or_unknown", "phase_unsupported",
    }
)


class WorktreeLifecycleStoreError(ValueError):
    """Stable non-leaking Store error."""


class WorktreeLifecycleInputError(WorktreeLifecycleStoreError):
    pass


class WorktreeLifecycleSchemaError(WorktreeLifecycleStoreError):
    pass


class WorktreeLifecycleSecurityError(WorktreeLifecycleStoreError):
    pass


class WorktreeLifecycleConflictError(WorktreeLifecycleStoreError):
    pass


class WorktreeLifecycleTransitionError(WorktreeLifecycleStoreError):
    pass


class WorktreeLifecycleNotFoundError(WorktreeLifecycleStoreError):
    pass


class WorktreeLifecyclePermissionError(WorktreeLifecycleStoreError):
    pass


def _fail(error_type: type[WorktreeLifecycleStoreError], code: str) -> None:
    raise error_type("worktree_lifecycle:" + code)


class WorktreePhase(str, Enum):
    RESERVED = "RESERVED"
    CREATING = "CREATING"
    READY = "READY"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"

    @classmethod
    def order(cls) -> tuple[WorktreePhase, ...]:
        return tuple(cls)


class WorktreeLifecycleAction(str, Enum):
    CREATE = "CREATE"
    RELEASE = "RELEASE"
    RECONCILE = "RECONCILE"


class WorktreeLifecycleOutcome(str, Enum):
    READY = "READY"
    RELEASED = "RELEASED"
    REPLAYED = "REPLAYED"
    REFUSED = "REFUSED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class WorktreeReconciliationAction(str, Enum):
    NO_OP = "NO_OP"
    RESUME_CREATE = "RESUME_CREATE"
    ADOPT_READY = "ADOPT_READY"
    RESUME_RELEASE = "RESUME_RELEASE"
    REJECT = "REJECT"
    FAIL_CLOSED = "FAIL_CLOSED"


def _text(value: object, code: str) -> str:
    if type(value) is not str:
        _fail(WorktreeLifecycleInputError, code + "_type")
    if not value or value != value.strip() or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        _fail(WorktreeLifecycleInputError, code + "_unsafe")
    return value


def _safe_id(value: object, code: str, prefix: str | None = None) -> str:
    value = _text(value, code)
    if _SAFE_ID_RE.fullmatch(value) is None or "/" in value or "\\" in value:
        _fail(WorktreeLifecycleInputError, code + "_format")
    if prefix is not None and not value.startswith(prefix):
        _fail(WorktreeLifecycleInputError, code + "_prefix")
    if value.casefold().split(".", 1)[0] in _RESERVED_NAMES:
        _fail(WorktreeLifecycleSecurityError, code + "_reserved")
    return value


def _positive(value: object, code: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        _fail(WorktreeLifecycleInputError, code + "_range")
    return value


def _sha40(value: object, code: str) -> str:
    if type(value) is not str or _SHA40_RE.fullmatch(value) is None:
        _fail(WorktreeLifecycleInputError, code + "_format")
    return value


def _digest(value: object, code: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail(WorktreeLifecycleInputError, code + "_format")
    return value


def _timestamp(value: object, code: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        _fail(WorktreeLifecycleInputError, code + "_format")
    return value


def _path_text(value: object, code: str) -> str:
    value = _text(value, code)
    if value.startswith("\\\\") or value.startswith("//"):
        _fail(WorktreeLifecycleSecurityError, code + "_unc")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _fail(WorktreeLifecycleSecurityError, code + "_absolute")
    for index, part in enumerate(path.parts):
        if index == 0:
            continue
        if part.casefold().split(".", 1)[0] in _RESERVED_NAMES:
            _fail(WorktreeLifecycleSecurityError, code + "_reserved")
        if ":" in part:
            _fail(WorktreeLifecycleSecurityError, code + "_ads")
    if os.path.normpath(value) != value.rstrip("\\/"):
        _fail(WorktreeLifecycleSecurityError, code + "_canonical")
    return value


def _branch(value: object) -> str:
    value = _text(value, "branch")
    if not value.startswith("agentdesk/") or ".." in value.split("/") or "\\" in value:
        _fail(WorktreeLifecycleInputError, "branch_format")
    return value


def _phase(value: object, code: str = "phase") -> WorktreePhase:
    if type(value) is not WorktreePhase:
        _fail(WorktreeLifecycleInputError, code + "_type")
    return value


@dataclass(frozen=True, slots=True)
class WorktreeInventoryEntry:
    repository_common_dir: str
    worktree_path: str
    head_commit: str
    branch: str | None
    is_bare: bool
    is_detached: bool
    is_locked: bool
    is_prunable: bool

    def __post_init__(self) -> None:
        _path_text(self.repository_common_dir, "inventory_common_dir")
        _path_text(self.worktree_path, "inventory_worktree_path")
        _sha40(self.head_commit, "inventory_head")
        if self.branch is not None:
            _text(self.branch, "inventory_branch")
        for name in ("is_bare", "is_detached", "is_locked", "is_prunable"):
            if type(getattr(self, name)) is not bool:
                _fail(WorktreeLifecycleInputError, name + "_type")
        if self.is_bare and (self.branch is not None or self.is_detached):
            _fail(WorktreeLifecycleSchemaError, "inventory_bare_state")
        if not self.is_bare and self.is_detached == (self.branch is not None):
            _fail(WorktreeLifecycleSchemaError, "inventory_branch_state")


@dataclass(frozen=True, slots=True)
class WorktreeReservation:
    schema_version: str
    reservation_id: str
    worktree_id: str
    repository_common_dir: str
    repository_root: str
    worktree_path: str
    branch: str
    base_commit: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    phase: WorktreePhase
    reserved_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(WorktreeLifecycleInputError, "reservation_schema")
        _safe_id(self.reservation_id, "reservation_id", "WTR-")
        _safe_id(self.worktree_id, "worktree_id", "WT-")
        _path_text(self.repository_common_dir, "repository_common_dir")
        _path_text(self.repository_root, "repository_root")
        _path_text(self.worktree_path, "worktree_path")
        _branch(self.branch)
        _sha40(self.base_commit, "base_commit")
        if type(self.task_id) is not str or _TASK_RE.fullmatch(self.task_id) is None:
            _fail(WorktreeLifecycleInputError, "task_id")
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt", 3)
        _safe_id(self.dispatch_id, "dispatch_id", "DSP-")
        if self.phase is not WorktreePhase.RESERVED:
            _fail(WorktreeLifecycleInputError, "reservation_phase")
        _timestamp(self.reserved_at, "reserved_at")
        _digest(self.content_digest, "reservation_digest")


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    schema_version: str
    worktree_id: str
    reservation_id: str
    repository_common_dir: str
    repository_root: str
    worktree_path: str
    branch: str
    base_commit: str
    head_commit: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    phase: WorktreePhase
    created_at: str | None
    release_requested_at: str | None
    released_at: str | None
    inventory_digest: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(WorktreeLifecycleInputError, "record_schema")
        _safe_id(self.worktree_id, "record_worktree_id", "WT-")
        _safe_id(self.reservation_id, "record_reservation_id", "WTR-")
        _path_text(self.repository_common_dir, "record_common_dir")
        _path_text(self.repository_root, "record_root")
        _path_text(self.worktree_path, "record_path")
        _branch(self.branch)
        _sha40(self.base_commit, "record_base")
        _sha40(self.head_commit, "record_head")
        if type(self.task_id) is not str or _TASK_RE.fullmatch(self.task_id) is None:
            _fail(WorktreeLifecycleInputError, "record_task_id")
        _positive(self.revision, "record_revision")
        _positive(self.attempt, "record_attempt", 3)
        _safe_id(self.dispatch_id, "record_dispatch_id", "DSP-")
        _phase(self.phase)
        _timestamp(self.created_at, "created_at", True)
        _timestamp(self.release_requested_at, "release_requested_at", True)
        _timestamp(self.released_at, "released_at", True)
        _digest(self.inventory_digest, "inventory_digest")
        _digest(self.content_digest, "record_digest")
        position = self.phase.order().index(self.phase)
        if position < 2 and self.created_at is not None:
            _fail(WorktreeLifecycleInputError, "created_at_premature")
        if position >= 2 and self.created_at is None:
            _fail(WorktreeLifecycleInputError, "created_at_missing")
        if position < 3 and self.release_requested_at is not None:
            _fail(WorktreeLifecycleInputError, "release_requested_premature")
        if position >= 3 and self.release_requested_at is None:
            _fail(WorktreeLifecycleInputError, "release_requested_missing")
        if position < 4 and self.released_at is not None:
            _fail(WorktreeLifecycleInputError, "released_at_premature")
        if position >= 4 and self.released_at is None:
            _fail(WorktreeLifecycleInputError, "released_at_missing")


@dataclass(frozen=True, slots=True)
class WorktreeLifecycleRequest:
    schema_version: str
    operation_id: str
    action: WorktreeLifecycleAction
    repository_common_dir: str
    repository_root: str
    worktree_path: str
    branch: str
    base_commit: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    expected_worktree_id: str
    requested_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(WorktreeLifecycleInputError, "request_schema")
        _safe_id(self.operation_id, "operation_id", "OP-")
        if type(self.action) is not WorktreeLifecycleAction:
            _fail(WorktreeLifecycleInputError, "request_action")
        _path_text(self.repository_common_dir, "request_common_dir")
        _path_text(self.repository_root, "request_root")
        _path_text(self.worktree_path, "request_path")
        _branch(self.branch)
        _sha40(self.base_commit, "request_base")
        if type(self.task_id) is not str or _TASK_RE.fullmatch(self.task_id) is None:
            _fail(WorktreeLifecycleInputError, "request_task_id")
        _positive(self.revision, "request_revision")
        _positive(self.attempt, "request_attempt", 3)
        _safe_id(self.dispatch_id, "request_dispatch_id", "DSP-")
        _safe_id(self.expected_worktree_id, "expected_worktree_id", "WT-")
        _timestamp(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class WorktreeLifecycleResult:
    schema_version: str
    operation_id: str
    action: WorktreeLifecycleAction
    outcome: WorktreeLifecycleOutcome
    worktree_id: str
    phase: WorktreePhase
    worktree_path: str
    head_commit: str | None
    decision: WorktreeReconciliationAction

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(WorktreeLifecycleInputError, "result_schema")
        _safe_id(self.operation_id, "result_operation_id", "OP-")
        if type(self.action) is not WorktreeLifecycleAction or type(self.outcome) is not WorktreeLifecycleOutcome:
            _fail(WorktreeLifecycleInputError, "result_enum")
        _safe_id(self.worktree_id, "result_worktree_id", "WT-")
        _phase(self.phase, "result_phase")
        _path_text(self.worktree_path, "result_path")
        if self.head_commit is not None:
            _sha40(self.head_commit, "result_head")
        if type(self.decision) is not WorktreeReconciliationAction:
            _fail(WorktreeLifecycleInputError, "result_decision")


@dataclass(frozen=True, slots=True)
class WorktreeReconciliationDecision:
    schema_version: str
    worktree_id: str
    action: WorktreeReconciliationAction
    reason: str
    expected_phase: WorktreePhase
    observed_present: bool
    observed_head: str | None
    observed_branch: str | None
    observed_clean: bool | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(WorktreeLifecycleInputError, "decision_schema")
        _safe_id(self.worktree_id, "decision_worktree_id", "WT-")
        if type(self.action) is not WorktreeReconciliationAction:
            _fail(WorktreeLifecycleInputError, "decision_action")
        if type(self.reason) is not str or self.reason not in _RECONCILIATION_REASONS:
            _fail(WorktreeLifecycleInputError, "decision_reason")
        _phase(self.expected_phase, "expected_phase")
        if type(self.observed_present) is not bool:
            _fail(WorktreeLifecycleInputError, "observed_present")
        if self.observed_head is not None:
            _sha40(self.observed_head, "observed_head")
        if self.observed_branch is not None:
            _text(self.observed_branch, "observed_branch")
        if self.observed_clean is not None and type(self.observed_clean) is not bool:
            _fail(WorktreeLifecycleInputError, "observed_clean")
        _digest(self.content_digest, "decision_digest")


def _binding_parts(
    repository_common_dir: str,
    repository_root: str,
    branch: str,
    base_commit: str,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
) -> tuple[str, ...]:
    return (
        _path_text(repository_common_dir, "binding_common_dir"),
        _path_text(repository_root, "binding_root"),
        _branch(branch),
        _sha40(base_commit, "binding_base"),
        _text(task_id, "binding_task"),
        str(_positive(revision, "binding_revision")),
        str(_positive(attempt, "binding_attempt", 3)),
        _safe_id(dispatch_id, "binding_dispatch", "DSP-"),
    )


def _binding_bytes(parts: tuple[str, ...]) -> bytes:
    return ("\0".join(parts) + "\0").encode("utf-8")


def derive_worktree_id(
    repository_common_dir: str,
    repository_root: str,
    branch: str,
    base_commit: str,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
) -> str:
    parts = _binding_parts(
        repository_common_dir, repository_root, branch, base_commit,
        task_id, revision, attempt, dispatch_id,
    )
    return "WT-" + hashlib.sha256(_binding_bytes(parts)).hexdigest()


def derive_reservation_id(worktree_id: str, request: WorktreeLifecycleRequest) -> str:
    _safe_id(worktree_id, "derive_worktree_id", "WT-")
    parts = _binding_parts(
        request.repository_common_dir, request.repository_root, request.branch,
        request.base_commit, request.task_id, request.revision, request.attempt,
        request.dispatch_id,
    )
    return "WTR-" + hashlib.sha256(
        worktree_id.encode("utf-8") + b"\0" + _binding_bytes(parts)
    ).hexdigest()


def expected_branch(task_id: str, revision: int, attempt: int, dispatch_id: str) -> str:
    if type(task_id) is not str or _TASK_RE.fullmatch(task_id) is None:
        _fail(WorktreeLifecycleInputError, "expected_branch_task")
    _positive(revision, "expected_branch_revision")
    _positive(attempt, "expected_branch_attempt", 3)
    _safe_id(dispatch_id, "expected_branch_dispatch", "DSP-")
    return f"agentdesk/{task_id}/r{revision}/a{attempt}/{dispatch_id}"


def expected_worktree_path(repository_root: str, worktree_id: str) -> str:
    root = Path(_path_text(repository_root, "expected_path_root"))
    _safe_id(worktree_id, "expected_path_id", "WT-")
    return str(root.parent / f"{root.name}-agentdesk-worktrees" / worktree_id)


def validate_request_identity(request: WorktreeLifecycleRequest) -> None:
    if type(request) is not WorktreeLifecycleRequest:
        _fail(WorktreeLifecycleInputError, "request_type")
    branch = expected_branch(
        request.task_id, request.revision, request.attempt, request.dispatch_id
    )
    if request.branch != branch:
        _fail(WorktreeLifecycleConflictError, "branch_identity")
    worktree_id = derive_worktree_id(
        request.repository_common_dir, request.repository_root, request.branch,
        request.base_commit, request.task_id, request.revision, request.attempt,
        request.dispatch_id,
    )
    if request.expected_worktree_id != worktree_id:
        _fail(WorktreeLifecycleConflictError, "worktree_identity")
    if os.path.normcase(request.worktree_path) != os.path.normcase(
        expected_worktree_path(request.repository_root, worktree_id)
    ):
        _fail(WorktreeLifecycleConflictError, "worktree_path_identity")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return _quote(value)
    _fail(WorktreeLifecycleInputError, "scalar_type")


def _finish(lines: tuple[str, ...]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _lines(fields: tuple[str, ...], values: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(f"{name}: {_scalar(value)}" for name, value in zip(fields, values))


def _content_digest(fields: tuple[str, ...], values: tuple[object, ...]) -> str:
    return "sha256:" + hashlib.sha256(_finish(_lines(fields, values))).hexdigest()


def _reservation_values(value: WorktreeReservation) -> tuple[object, ...]:
    return (
        value.schema_version, value.reservation_id, value.worktree_id,
        value.repository_common_dir, value.repository_root, value.worktree_path,
        value.branch, value.base_commit, value.task_id, value.revision,
        value.attempt, value.dispatch_id, value.phase.value, value.reserved_at,
        value.content_digest,
    )


def _record_values(value: WorktreeRecord) -> tuple[object, ...]:
    return (
        value.schema_version, value.worktree_id, value.reservation_id,
        value.repository_common_dir, value.repository_root, value.worktree_path,
        value.branch, value.base_commit, value.head_commit, value.task_id,
        value.revision, value.attempt, value.dispatch_id, value.phase.value,
        value.created_at, value.release_requested_at, value.released_at,
        value.inventory_digest, value.content_digest,
    )


def _decision_values(value: WorktreeReconciliationDecision) -> tuple[object, ...]:
    return (
        value.schema_version, value.worktree_id, value.action.value, value.reason,
        value.expected_phase.value, value.observed_present, value.observed_head,
        value.observed_branch, value.observed_clean, value.content_digest,
    )


def _with_reservation_digest(value: WorktreeReservation) -> WorktreeReservation:
    value = replace(value, content_digest="sha256:" + "0" * 64)
    values = _reservation_values(value)
    return replace(value, content_digest=_content_digest(_RESERVATION_FIELDS[:-1], values[:-1]))


def _with_record_digest(value: WorktreeRecord) -> WorktreeRecord:
    value = replace(value, content_digest="sha256:" + "0" * 64)
    values = _record_values(value)
    return replace(value, content_digest=_content_digest(_RECORD_FIELDS[:-1], values[:-1]))


def _with_decision_digest(value: WorktreeReconciliationDecision) -> WorktreeReconciliationDecision:
    value = replace(value, content_digest="sha256:" + "0" * 64)
    values = _decision_values(value)
    return replace(value, content_digest=_content_digest(_DECISION_FIELDS[:-1], values[:-1]))


def encode_reservation(value: WorktreeReservation) -> bytes:
    if type(value) is not WorktreeReservation:
        _fail(WorktreeLifecycleInputError, "reservation_type")
    values = _reservation_values(value)
    if value.content_digest != _content_digest(_RESERVATION_FIELDS[:-1], values[:-1]):
        _fail(WorktreeLifecycleSchemaError, "reservation_digest")
    return _finish(_lines(_RESERVATION_FIELDS, values))


def encode_record(value: WorktreeRecord) -> bytes:
    if type(value) is not WorktreeRecord:
        _fail(WorktreeLifecycleInputError, "record_type")
    values = _record_values(value)
    if value.content_digest != _content_digest(_RECORD_FIELDS[:-1], values[:-1]):
        _fail(WorktreeLifecycleSchemaError, "record_digest")
    return _finish(_lines(_RECORD_FIELDS, values))


def encode_decision(value: WorktreeReconciliationDecision) -> bytes:
    if type(value) is not WorktreeReconciliationDecision:
        _fail(WorktreeLifecycleInputError, "decision_type")
    values = _decision_values(value)
    if value.content_digest != _content_digest(_DECISION_FIELDS[:-1], values[:-1]):
        _fail(WorktreeLifecycleSchemaError, "decision_digest")
    return _finish(_lines(_DECISION_FIELDS, values))


def _parse_scalar(token: str, code: str) -> object:
    if token == "null":
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    if re.fullmatch(r"0|[1-9][0-9]*", token):
        return int(token)
    if not token.startswith('"') or not token.endswith('"'):
        _fail(WorktreeLifecycleSchemaError, code + "_scalar")
    result: list[str] = []
    index = 1
    while index < len(token) - 1:
        char = token[index]
        if char == "\\":
            index += 1
            if index >= len(token) - 1 or token[index] not in ('"', "\\"):
                _fail(WorktreeLifecycleSchemaError, code + "_escape")
            result.append(token[index])
        elif char == '"' or ord(char) < 0x20:
            _fail(WorktreeLifecycleSchemaError, code + "_quoted")
        else:
            result.append(char)
        index += 1
    value = "".join(result)
    if _quote(value) != token:
        _fail(WorktreeLifecycleSchemaError, code + "_noncanonical")
    return value


def _document(data: object, fields: tuple[str, ...], code: str) -> tuple[object, ...]:
    if type(data) is not bytes or not data or data.startswith(b"\xef\xbb\xbf"):
        _fail(WorktreeLifecycleSchemaError, code + "_bytes")
    if not data.endswith(b"\n") or data.endswith(b"\n\n") or b"\r" in data or b"\t" in data:
        _fail(WorktreeLifecycleSchemaError, code + "_newline")
    try:
        lines = tuple(data[:-1].decode("utf-8").split("\n"))
    except UnicodeDecodeError:
        _fail(WorktreeLifecycleSchemaError, code + "_utf8")
    if len(lines) != len(fields) or any(not line for line in lines):
        _fail(WorktreeLifecycleSchemaError, code + "_field_count")
    values: list[object] = []
    for line, field in zip(lines, fields):
        prefix = field + ": "
        if not line.startswith(prefix):
            _fail(WorktreeLifecycleSchemaError, code + "_field_order")
        values.append(_parse_scalar(line[len(prefix):], field))
    return tuple(values)


def decode_reservation(data: bytes) -> WorktreeReservation:
    values = _document(data, _RESERVATION_FIELDS, "reservation")
    try:
        value = WorktreeReservation(
            values[0], values[1], values[2], values[3], values[4], values[5],
            values[6], values[7], values[8], values[9], values[10], values[11],
            WorktreePhase(values[12]), values[13], values[14],
        )
    except WorktreeLifecycleStoreError:
        raise
    except (TypeError, ValueError):
        _fail(WorktreeLifecycleSchemaError, "reservation_fields")
    if encode_reservation(value) != data:
        _fail(WorktreeLifecycleSchemaError, "reservation_noncanonical")
    return value


def decode_record(data: bytes) -> WorktreeRecord:
    values = _document(data, _RECORD_FIELDS, "record")
    try:
        value = WorktreeRecord(
            values[0], values[1], values[2], values[3], values[4], values[5],
            values[6], values[7], values[8], values[9], values[10], values[11],
            values[12], WorktreePhase(values[13]), values[14], values[15],
            values[16], values[17], values[18],
        )
    except WorktreeLifecycleStoreError:
        raise
    except (TypeError, ValueError):
        _fail(WorktreeLifecycleSchemaError, "record_fields")
    if encode_record(value) != data:
        _fail(WorktreeLifecycleSchemaError, "record_noncanonical")
    return value


def decode_decision(data: bytes) -> WorktreeReconciliationDecision:
    values = _document(data, _DECISION_FIELDS, "decision")
    try:
        value = WorktreeReconciliationDecision(
            values[0], values[1], WorktreeReconciliationAction(values[2]),
            values[3], WorktreePhase(values[4]), values[5], values[6],
            values[7], values[8], values[9],
        )
    except WorktreeLifecycleStoreError:
        raise
    except (TypeError, ValueError):
        _fail(WorktreeLifecycleSchemaError, "decision_fields")
    if encode_decision(value) != data:
        _fail(WorktreeLifecycleSchemaError, "decision_noncanonical")
    return value


def make_reservation(request: WorktreeLifecycleRequest) -> WorktreeReservation:
    validate_request_identity(request)
    worktree_id = request.expected_worktree_id
    return _with_reservation_digest(
        WorktreeReservation(
            SCHEMA_VERSION,
            derive_reservation_id(worktree_id, request),
            worktree_id,
            request.repository_common_dir,
            request.repository_root,
            request.worktree_path,
            request.branch,
            request.base_commit,
            request.task_id,
            request.revision,
            request.attempt,
            request.dispatch_id,
            WorktreePhase.RESERVED,
            request.requested_at,
            "sha256:" + "0" * 64,
        )
    )


def make_record(
    reservation: WorktreeReservation,
    phase: WorktreePhase,
    inventory_digest: str,
    head_commit: str | None = None,
    created_at: str | None = None,
    release_requested_at: str | None = None,
    released_at: str | None = None,
) -> WorktreeRecord:
    if type(reservation) is not WorktreeReservation:
        _fail(WorktreeLifecycleInputError, "make_record_reservation")
    return _with_record_digest(
        WorktreeRecord(
            SCHEMA_VERSION, reservation.worktree_id, reservation.reservation_id,
            reservation.repository_common_dir, reservation.repository_root,
            reservation.worktree_path, reservation.branch, reservation.base_commit,
            reservation.base_commit if head_commit is None else head_commit,
            reservation.task_id, reservation.revision, reservation.attempt,
            reservation.dispatch_id, phase, created_at, release_requested_at,
            released_at, inventory_digest, "sha256:" + "0" * 64,
        )
    )


def parse_worktree_inventory(
    data: bytes, repository_common_dir: str
) -> tuple[WorktreeInventoryEntry, ...]:
    if type(data) is not bytes or not data or not data.endswith(b"\0"):
        _fail(WorktreeLifecycleSchemaError, "inventory_bytes")
    common_dir = _path_text(repository_common_dir, "inventory_common_dir")
    try:
        raw_tokens = data.decode("utf-8").split("\0")
    except UnicodeDecodeError:
        _fail(WorktreeLifecycleSchemaError, "inventory_utf8")
    if raw_tokens[-1] != "":
        _fail(WorktreeLifecycleSchemaError, "inventory_terminator")
    raw_tokens.pop()
    groups: list[list[str]] = []
    current: list[str] = []
    for token in raw_tokens:
        if token == "":
            if not current:
                _fail(WorktreeLifecycleSchemaError, "inventory_empty_record")
            groups.append(current)
            current = []
        else:
            current.append(token)
    if current:
        groups.append(current)
    entries: list[WorktreeInventoryEntry] = []
    seen_paths: set[str] = set()
    for group in groups:
        worktree_path: str | None = None
        head: str | None = None
        branch: str | None = None
        flags: set[str] = set()
        seen_keys: set[str] = set()
        for token in group:
            key, separator, value = token.partition(" ")
            if key in seen_keys:
                _fail(WorktreeLifecycleSchemaError, "inventory_duplicate_field")
            seen_keys.add(key)
            if key == "worktree" and separator:
                worktree_path = value
            elif key == "HEAD" and separator:
                head = value
            elif key == "branch" and separator and value.startswith("refs/heads/"):
                branch = value[len("refs/heads/"):]
            elif key in ("bare", "detached") and not separator:
                flags.add(key)
            elif key in ("locked", "prunable"):
                flags.add(key)
            else:
                _fail(WorktreeLifecycleSchemaError, "inventory_unknown_field")
        if worktree_path is None or head is None:
            _fail(WorktreeLifecycleSchemaError, "inventory_missing_field")
        canonical_worktree_path = os.path.normpath(worktree_path)
        folded = os.path.normcase(
            _path_text(canonical_worktree_path, "inventory_path")
        )
        if folded in seen_paths:
            _fail(WorktreeLifecycleConflictError, "inventory_duplicate_path")
        seen_paths.add(folded)
        entries.append(
            WorktreeInventoryEntry(
                common_dir, canonical_worktree_path, head, branch,
                "bare" in flags, "detached" in flags,
                "locked" in flags, "prunable" in flags,
            )
        )
    return tuple(sorted(entries, key=lambda e: (os.path.normcase(e.worktree_path), e.branch or "", e.head_commit)))


def inventory_digest(entries: tuple[WorktreeInventoryEntry, ...]) -> str:
    if type(entries) is not tuple or any(type(e) is not WorktreeInventoryEntry for e in entries):
        _fail(WorktreeLifecycleInputError, "inventory_entries")
    payload = bytearray()
    for entry in sorted(entries, key=lambda e: (os.path.normcase(e.worktree_path), e.branch or "", e.head_commit)):
        values = (
            entry.repository_common_dir, entry.worktree_path, entry.head_commit,
            entry.branch or "", "1" if entry.is_bare else "0",
            "1" if entry.is_detached else "0", "1" if entry.is_locked else "0",
            "1" if entry.is_prunable else "0",
        )
        payload.extend(_binding_bytes(values))
    return "sha256:" + hashlib.sha256(bytes(payload)).hexdigest()


def _decision(
    worktree_id: str,
    action: WorktreeReconciliationAction,
    reason: str,
    phase: WorktreePhase,
    entry: WorktreeInventoryEntry | None,
    observed_clean: bool | None,
) -> WorktreeReconciliationDecision:
    return _with_decision_digest(
        WorktreeReconciliationDecision(
            SCHEMA_VERSION, worktree_id, action, reason, phase,
            entry is not None, None if entry is None else entry.head_commit,
            None if entry is None else entry.branch, observed_clean,
            "sha256:" + "0" * 64,
        )
    )


def reconcile_worktree(
    worktree_id: str,
    reservation: WorktreeReservation | None,
    record: WorktreeRecord | None,
    inventory: tuple[WorktreeInventoryEntry, ...],
    observed_clean: bool | None,
) -> WorktreeReconciliationDecision:
    _safe_id(worktree_id, "reconcile_worktree_id", "WT-")
    if type(inventory) is not tuple or any(type(e) is not WorktreeInventoryEntry for e in inventory):
        _fail(WorktreeLifecycleInputError, "reconcile_inventory")
    matching = tuple(e for e in inventory if reservation is not None and os.path.normcase(e.worktree_path) == os.path.normcase(reservation.worktree_path))
    if len(matching) > 1:
        _fail(WorktreeLifecycleConflictError, "reconcile_duplicate")
    entry = matching[0] if matching else None
    if reservation is None:
        return _decision(worktree_id, WorktreeReconciliationAction.NO_OP, "reservation_missing", WorktreePhase.RESERVED, None, observed_clean)
    if reservation.worktree_id != worktree_id:
        _fail(WorktreeLifecycleConflictError, "reconcile_reservation_identity")
    phase = WorktreePhase.RESERVED if record is None else record.phase
    if record is not None and (
        record.worktree_id != reservation.worktree_id
        or record.reservation_id != reservation.reservation_id
        or record.worktree_path != reservation.worktree_path
        or record.branch != reservation.branch
        or record.base_commit != reservation.base_commit
    ):
        return _decision(worktree_id, WorktreeReconciliationAction.FAIL_CLOSED, "identity_divergent", phase, entry, observed_clean)
    exact = entry is not None and entry.branch == reservation.branch and entry.head_commit == (reservation.base_commit if record is None else record.head_commit) and not entry.is_bare and not entry.is_detached and not entry.is_prunable
    if phase in (WorktreePhase.RESERVED, WorktreePhase.CREATING):
        if entry is None:
            return _decision(worktree_id, WorktreeReconciliationAction.RESUME_CREATE, "resume_create", phase, None, observed_clean)
        if exact:
            return _decision(worktree_id, WorktreeReconciliationAction.ADOPT_READY, "adopt_ready", phase, entry, observed_clean)
        return _decision(worktree_id, WorktreeReconciliationAction.FAIL_CLOSED, "identity_divergent", phase, entry, observed_clean)
    if phase is WorktreePhase.READY:
        if exact and observed_clean is True:
            return _decision(worktree_id, WorktreeReconciliationAction.NO_OP, "ready_exact", phase, entry, True)
        return _decision(worktree_id, WorktreeReconciliationAction.FAIL_CLOSED, "inventory_missing" if entry is None else "dirty_or_unknown", phase, entry, observed_clean)
    if phase is WorktreePhase.RELEASING:
        if entry is None:
            return _decision(worktree_id, WorktreeReconciliationAction.NO_OP, "released_exact", phase, None, observed_clean)
        if exact and observed_clean is True and not entry.is_locked:
            return _decision(worktree_id, WorktreeReconciliationAction.RESUME_RELEASE, "resume_release", phase, entry, True)
        return _decision(worktree_id, WorktreeReconciliationAction.FAIL_CLOSED, "dirty_or_unknown", phase, entry, observed_clean)
    if phase is WorktreePhase.RELEASED and entry is None:
        return _decision(worktree_id, WorktreeReconciliationAction.NO_OP, "released_exact", phase, None, observed_clean)
    return _decision(worktree_id, WorktreeReconciliationAction.FAIL_CLOSED, "phase_unsupported", phase, entry, observed_clean)


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(project_root: Path) -> threading.Lock:
    key = os.path.normcase(str(project_root))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _safe_existing_directory(path: Path, code: str) -> None:
    try:
        if _is_symlink_or_reparse(path):
            _fail(WorktreeLifecycleSecurityError, code + "_link")
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        _fail(WorktreeLifecycleNotFoundError, code + "_missing")
    except OSError:
        _fail(WorktreeLifecyclePermissionError, code + "_unknown")
    if not stat.S_ISDIR(mode):
        _fail(WorktreeLifecycleSecurityError, code + "_not_directory")


def _ensure_directory(path: Path) -> None:
    try:
        if path.exists():
            _safe_existing_directory(path, "directory")
            return
        path.mkdir()
        _fsync_parent_directory(path)
    except WorktreeLifecycleStoreError:
        raise
    except FileExistsError:
        _safe_existing_directory(path, "directory")
    except OSError:
        _fail(WorktreeLifecyclePermissionError, "directory_create")


def _store_root(project_root: Path, create: bool) -> Path:
    if not isinstance(project_root, Path) or not project_root.is_absolute():
        _fail(WorktreeLifecycleSecurityError, "project_root")
    _safe_existing_directory(project_root, "project_root")
    current = project_root
    for component in STORE_RELATIVE.parts:
        current = current / component
        if create:
            _ensure_directory(current)
        else:
            _safe_existing_directory(current, "store_directory")
    for component in (RESERVATIONS_DIRECTORY, RECORDS_DIRECTORY):
        directory = current / component
        if create:
            _ensure_directory(directory)
        else:
            _safe_existing_directory(directory, "evidence_directory")
    return current


def _safe_read(path: Path, missing_ok: bool) -> bytes | None:
    try:
        if _is_symlink_or_reparse(path):
            _fail(WorktreeLifecycleSecurityError, "evidence_link")
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail(WorktreeLifecycleNotFoundError, "evidence_missing")
    except OSError:
        _fail(WorktreeLifecyclePermissionError, "evidence_unknown")
    if not stat.S_ISREG(mode):
        _fail(WorktreeLifecycleSecurityError, "evidence_not_regular")
    try:
        return path.read_bytes()
    except OSError:
        _fail(WorktreeLifecyclePermissionError, "evidence_read")


def _atomic_write(path: Path, content: bytes) -> None:
    if type(content) is not bytes:
        _fail(WorktreeLifecycleInputError, "write_content")
    try:
        if path.exists() and _is_symlink_or_reparse(path):
            _fail(WorktreeLifecycleSecurityError, "write_link")
        fd, temp_name = tempfile.mkstemp(prefix=".worktree-lifecycle-", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if _is_symlink_or_reparse(temp_path) or not stat.S_ISREG(os.lstat(str(temp_path)).st_mode):
                _fail(WorktreeLifecycleSecurityError, "temp_type")
            os.replace(str(temp_path), str(path))
            _fsync_parent_directory(path)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
    except WorktreeLifecycleStoreError:
        raise
    except OSError:
        _fail(WorktreeLifecyclePermissionError, "atomic_write")


@contextmanager
def _store_lock(project_root: Path) -> Iterator[None]:
    lock = _thread_lock(project_root)
    with lock:
        lock_path = project_root / LOCK_FILENAME
        token = os.urandom(16).hex().encode("ascii")
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _fail(WorktreeLifecycleConflictError, "lock_contention")
        except PermissionError:
            _fail(WorktreeLifecyclePermissionError, "lock_permission")
        except OSError:
            _fail(WorktreeLifecyclePermissionError, "lock_create")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_parent_directory(lock_path)
            yield
        finally:
            try:
                if _is_symlink_or_reparse(lock_path):
                    _fail(WorktreeLifecycleSecurityError, "lock_replaced")
                if lock_path.read_bytes() != token:
                    _fail(WorktreeLifecycleConflictError, "lock_token")
                lock_path.unlink()
                _fsync_parent_directory(lock_path)
            except WorktreeLifecycleStoreError:
                raise
            except OSError:
                _fail(WorktreeLifecyclePermissionError, "lock_release")


def _evidence_path(root: Path, directory: str, worktree_id: str) -> Path:
    _safe_id(worktree_id, "evidence_worktree_id", "WT-")
    parent = root / directory
    path = parent / f"{worktree_id}.yaml"
    if path.parent != parent:
        _fail(WorktreeLifecycleSecurityError, "evidence_containment")
    return path


def _reject_extra_files(directory: Path) -> None:
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        _fail(WorktreeLifecyclePermissionError, "directory_list")
    for entry in entries:
        if _is_symlink_or_reparse(entry):
            _fail(WorktreeLifecycleSecurityError, "extra_link")
        if not re.fullmatch(r"WT-[0-9a-f]{64}\.yaml", entry.name):
            _fail(WorktreeLifecycleSecurityError, "extra_file")
        if not stat.S_ISREG(os.lstat(str(entry)).st_mode):
            _fail(WorktreeLifecycleSecurityError, "extra_type")


class WorktreeLifecycleStore:
    """Canonical reservation/record Store with no Git side effects."""

    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            _fail(WorktreeLifecycleSecurityError, "project_root")
        _safe_existing_directory(project_root, "project_root")
        self._project_root = project_root

    def reserve(self, request: WorktreeLifecycleRequest) -> WorktreeReservation:
        validate_request_identity(request)
        if request.action is not WorktreeLifecycleAction.CREATE:
            _fail(WorktreeLifecycleInputError, "reserve_action")
        if os.path.normcase(request.repository_root) != os.path.normcase(str(self._project_root)):
            _fail(WorktreeLifecycleConflictError, "repository_root")
        _safe_existing_directory(Path(request.repository_common_dir), "repository_common_dir")
        reservation = make_reservation(request)
        encoded = encode_reservation(reservation)
        root = _store_root(self._project_root, True)
        with _store_lock(self._project_root):
            _reject_extra_files(root / RESERVATIONS_DIRECTORY)
            _reject_extra_files(root / RECORDS_DIRECTORY)
            path = _evidence_path(root, RESERVATIONS_DIRECTORY, reservation.worktree_id)
            existing = _safe_read(path, True)
            if existing is not None:
                if existing != encoded:
                    _fail(WorktreeLifecycleConflictError, "reservation_replay")
                return decode_reservation(existing)
            _atomic_write(path, encoded)
        return reservation

    def read_reservation(self, worktree_id: str) -> WorktreeReservation:
        root = _store_root(self._project_root, False)
        _reject_extra_files(root / RESERVATIONS_DIRECTORY)
        data = _safe_read(_evidence_path(root, RESERVATIONS_DIRECTORY, worktree_id), False)
        return decode_reservation(data)

    def read_record(self, worktree_id: str, missing_ok: bool = False) -> WorktreeRecord | None:
        root = _store_root(self._project_root, False)
        _reject_extra_files(root / RECORDS_DIRECTORY)
        data = _safe_read(_evidence_path(root, RECORDS_DIRECTORY, worktree_id), missing_ok)
        return None if data is None else decode_record(data)

    def create_record(self, record: WorktreeRecord) -> WorktreeRecord:
        if type(record) is not WorktreeRecord or record.phase is not WorktreePhase.CREATING:
            _fail(WorktreeLifecycleInputError, "create_record")
        reservation = self.read_reservation(record.worktree_id)
        if (
            record.reservation_id != reservation.reservation_id
            or record.worktree_path != reservation.worktree_path
            or record.branch != reservation.branch
            or record.base_commit != reservation.base_commit
        ):
            _fail(WorktreeLifecycleConflictError, "record_binding")
        encoded = encode_record(record)
        root = _store_root(self._project_root, False)
        with _store_lock(self._project_root):
            path = _evidence_path(root, RECORDS_DIRECTORY, record.worktree_id)
            existing = _safe_read(path, True)
            if existing is not None:
                if existing != encoded:
                    _fail(WorktreeLifecycleConflictError, "record_replay")
                return decode_record(existing)
            _atomic_write(path, encoded)
        return record

    def advance_record(
        self,
        worktree_id: str,
        expected_phase: WorktreePhase,
        target_phase: WorktreePhase,
        inventory_digest_value: str,
        operation_at: str,
        head_commit: str | None = None,
    ) -> WorktreeRecord:
        _safe_id(worktree_id, "advance_worktree_id", "WT-")
        _phase(expected_phase, "expected_phase")
        _phase(target_phase, "target_phase")
        _digest(inventory_digest_value, "advance_inventory_digest")
        _timestamp(operation_at, "operation_at")
        root = _store_root(self._project_root, False)
        path = _evidence_path(root, RECORDS_DIRECTORY, worktree_id)
        with _store_lock(self._project_root):
            data = _safe_read(path, False)
            current = decode_record(data)
            order = current.phase.order()
            if current.phase is target_phase:
                return current
            if current.phase is not expected_phase or order.index(target_phase) != order.index(expected_phase) + 1:
                _fail(WorktreeLifecycleTransitionError, "phase_transition")
            updates: dict[str, object] = {
                "phase": target_phase,
                "inventory_digest": inventory_digest_value,
            }
            if head_commit is not None:
                updates["head_commit"] = _sha40(head_commit, "advance_head")
            if target_phase is WorktreePhase.READY:
                updates["created_at"] = operation_at
            elif target_phase is WorktreePhase.RELEASING:
                updates["release_requested_at"] = operation_at
            elif target_phase is WorktreePhase.RELEASED:
                updates["released_at"] = operation_at
            advanced = _with_record_digest(replace(current, **updates))
            _atomic_write(path, encode_record(advanced))
            return advanced
