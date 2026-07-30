"""AgentDesk Durable Dispatch Supervisor Evidence — TC-13.18d.12a-pre2.

Frozen data types, a strict receipt/tombstone store, monotonic phase
progression, and three-state (ALIVE/DEAD/UNKNOWN) process liveness
probing for creator, supervisor, and Worker.

This module is the durable evidence foundation that a future owner-loss
recovery path must consult before it is ever allowed to write
``DISPATCH_FAILED`` or start the next attempt.  It does **not** implement
owner-loss recovery itself.

Non-goals:
* Owner-loss auto ``DISPATCH_FAILED`` and next-attempt start.
* Spawning the supervisor or the Worker (see ``dispatch_supervisor_runner``).
* Changing the existing ``StateSnapshot`` public shape.
* Persisting prompt, argv, env, stdout, or stderr.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, fields as dc_fields
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "SCHEMA_VERSION",
    "DispatchReceiptPhase",
    "ProcessLiveness",
    "DispatchProcessReceipt",
    "DispatchFinalizerTombstone",
    "DispatchSupervisorEvidenceError",
    "DispatchSupervisorValidationError",
    "DispatchSupervisorStoreError",
    "DispatchSupervisorPhaseError",
    "DispatchSupervisorFencingError",
    "DispatchSupervisorContentionError",
    "validate_dispatch_supervisor_receipt",
    "read_dispatch_receipt",
    "read_dispatch_tombstone",
    "reserve_receipt",
    "advance_to_supervisor_ready",
    "advance_to_worker_started",
    "advance_to_finalizing",
    "write_finalizer_tombstone",
    "get_boot_id",
    "get_process_creation_time",
    "get_current_process_identity",
    "probe_process",
]

# ── constants ───────────────────────────────────────────────────────────────

SCHEMA_VERSION = "agentdesk.dispatch-supervisor-evidence/v1"

_RUNTIME_RELATIVE = Path(".agentdesk") / "runtime" / "dispatch-supervisor"
_LOCK_NAME = ".dispatch-supervisor.lock"

_LEASE_TTL_NOT_USED = 0  # receipts have no TTL; liveness decides death

# Exact frozen field sets (order is contractual).
_RECEIPT_FIELD_NAMES: tuple[str, ...] = (
    "schema_version",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "lease_epoch",
    "holder_instance_id",
    "generation_id",
    "platform",
    "boot_id",
    "phase",
    "creator_pid",
    "creator_creation_time",
    "supervisor_pid",
    "supervisor_creation_time",
    "worker_pid",
    "worker_creation_time",
    "worker_process_group",
    "written_at",
)
_RECEIPT_FIELD_SET: frozenset[str] = frozenset(_RECEIPT_FIELD_NAMES)

_TOMBSTONE_FIELD_NAMES: tuple[str, ...] = (
    "schema_version",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "generation_id",
    "winner",
    "worker_done",
    "heartbeat_done",
    "release_completed",
    "failure_kind",
    "finalized_at",
)
_TOMBSTONE_FIELD_SET: frozenset[str] = frozenset(_TOMBSTONE_FIELD_NAMES)

# Fields that must match between caller, receipt, and an existing tombstone
# during finalization (generation_id is checked separately because its
# mismatch is a fencing error).
_TOMBSTONE_REPLAY_FIELDS: tuple[str, ...] = (
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "winner",
    "worker_done",
    "heartbeat_done",
    "release_completed",
    "failure_kind",
)

_DISPATCH_FAILURE_KINDS: frozenset[str] = frozenset({
    "dispatch_start_failed",
    "worker_failed",
    "worker_output_failed",
    "delivery_transition_failed",
})

# Safe identifier patterns.  dispatch_id / generation_id / holder_instance_id
# must be safe single-segment filenames so they never escape the store dir.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TASK_ID_RE = re.compile(r"^TC-[0-9]{3,}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Windows directory-fsync errno allowlist (mirrors worker_slot_lease).
_WIN_DIR_FSYNC_ALLOWLIST: frozenset[int] = frozenset({
    errno.EACCES,
    errno.EBADF,
    errno.EINVAL,
})


def _is_dir_fsync_allowed_error(exc: OSError) -> bool:
    return (
        os.name == "nt"
        and exc.errno is not None
        and exc.errno in _WIN_DIR_FSYNC_ALLOWLIST
    )


def _safe_type_name(value: object) -> str:
    return type(value).__name__


# ── frozen enums ─────────────────────────────────────────────────────────────


class DispatchReceiptPhase(str, Enum):
    """Exact five durable receipt phases, forward-only."""

    RESERVED = "RESERVED"
    SUPERVISOR_READY = "SUPERVISOR_READY"
    WORKER_STARTED = "WORKER_STARTED"
    FINALIZING = "FINALIZING"
    FINALIZED = "FINALIZED"

    @classmethod
    def order(cls) -> tuple[DispatchReceiptPhase, ...]:
        return (
            cls.RESERVED,
            cls.SUPERVISOR_READY,
            cls.WORKER_STARTED,
            cls.FINALIZING,
            cls.FINALIZED,
        )


_ALLOWED_TRANSITIONS: dict[DispatchReceiptPhase, DispatchReceiptPhase] = {
    DispatchReceiptPhase.RESERVED: DispatchReceiptPhase.SUPERVISOR_READY,
    DispatchReceiptPhase.SUPERVISOR_READY: DispatchReceiptPhase.WORKER_STARTED,
    DispatchReceiptPhase.WORKER_STARTED: DispatchReceiptPhase.FINALIZING,
    DispatchReceiptPhase.FINALIZING: DispatchReceiptPhase.FINALIZED,
}


class ProcessLiveness(str, Enum):
    """Three-state process liveness.  UNKNOWN must never become DEAD."""

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


# ── frozen data types ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DispatchProcessReceipt:
    """Immutable 19-field durable dispatch process receipt.

    Fields belonging to a phase not yet reached must be exactly ``None``;
    no forged or placeholder values are permitted.
    """

    schema_version: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    lease_epoch: int
    holder_instance_id: str
    generation_id: str
    platform: str
    boot_id: str
    phase: str
    creator_pid: int | None
    creator_creation_time: str | None
    supervisor_pid: int | None
    supervisor_creation_time: str | None
    worker_pid: int | None
    worker_creation_time: str | None
    worker_process_group: int | None
    written_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DispatchSupervisorValidationError(
                "schema_version must be " + SCHEMA_VERSION
            )
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise DispatchSupervisorValidationError("task_id must match TC-NNN")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise DispatchSupervisorValidationError("revision must be a non-bool int")
        if self.revision < 1:
            raise DispatchSupervisorValidationError("revision must be >= 1")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise DispatchSupervisorValidationError("attempt must be a non-bool int")
        if self.attempt < 1:
            raise DispatchSupervisorValidationError("attempt must be >= 1")
        _validate_safe_id(self.dispatch_id, "dispatch_id")
        if isinstance(self.lease_epoch, bool) or not isinstance(self.lease_epoch, int):
            raise DispatchSupervisorValidationError("lease_epoch must be a non-bool int")
        if self.lease_epoch < 1:
            raise DispatchSupervisorValidationError("lease_epoch must be >= 1")
        _validate_safe_id(self.holder_instance_id, "holder_instance_id")
        _validate_safe_id(self.generation_id, "generation_id")
        if self.platform not in ("posix", "win32"):
            raise DispatchSupervisorValidationError("platform must be posix or win32")
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise DispatchSupervisorValidationError("boot_id must be a non-empty str")
        try:
            DispatchReceiptPhase(self.phase)
        except ValueError as exc:
            raise DispatchSupervisorValidationError(
                "phase must be a frozen receipt phase"
            ) from exc
        _validate_pid_optional(self.creator_pid, "creator_pid")
        _validate_pid_optional(self.supervisor_pid, "supervisor_pid")
        _validate_pid_optional(self.worker_pid, "worker_pid")
        _validate_creation_optional(self.creator_creation_time, "creator_creation_time")
        _validate_creation_optional(
            self.supervisor_creation_time, "supervisor_creation_time"
        )
        _validate_creation_optional(
            self.worker_creation_time, "worker_creation_time"
        )
        if isinstance(self.worker_process_group, bool) or not isinstance(
            self.worker_process_group, int
        ):
            if self.worker_process_group is not None:
                raise DispatchSupervisorValidationError(
                    "worker_process_group must be a non-bool int or None"
                )
        if self.worker_process_group is not None and self.worker_process_group < 0:
            raise DispatchSupervisorValidationError(
                "worker_process_group must be >= 0"
            )
        if not isinstance(self.written_at, str) or not self.written_at:
            raise DispatchSupervisorValidationError("written_at must be non-empty str")
        if _RFC3339_RE.fullmatch(self.written_at) is None:
            raise DispatchSupervisorValidationError(
                "written_at must be RFC 3339 UTC"
            )
        _validate_phase_field_presence(self)


def _validate_phase_field_presence(receipt: DispatchProcessReceipt) -> None:
    """A field for a phase not yet reached must be exactly None."""
    phase = DispatchReceiptPhase(receipt.phase)
    order = DispatchReceiptPhase.order()
    reached = order.index(phase)
    supervisor_expected = reached >= order.index(DispatchReceiptPhase.SUPERVISOR_READY)
    worker_expected = reached >= order.index(DispatchReceiptPhase.WORKER_STARTED)
    if not supervisor_expected:
        if receipt.supervisor_pid is not None:
            raise DispatchSupervisorValidationError(
                "supervisor_pid must be None before SUPERVISOR_READY"
            )
        if receipt.supervisor_creation_time is not None:
            raise DispatchSupervisorValidationError(
                "supervisor_creation_time must be None before SUPERVISOR_READY"
            )
    else:
        if receipt.supervisor_pid is None:
            raise DispatchSupervisorValidationError(
                "supervisor_pid must be set at SUPERVISOR_READY"
            )
        if receipt.supervisor_creation_time is None:
            raise DispatchSupervisorValidationError(
                "supervisor_creation_time must be set at SUPERVISOR_READY"
            )
    if not worker_expected:
        if receipt.worker_pid is not None:
            raise DispatchSupervisorValidationError(
                "worker_pid must be None before WORKER_STARTED"
            )
        if receipt.worker_creation_time is not None:
            raise DispatchSupervisorValidationError(
                "worker_creation_time must be None before WORKER_STARTED"
            )
        if receipt.worker_process_group is not None:
            raise DispatchSupervisorValidationError(
                "worker_process_group must be None before WORKER_STARTED"
            )
    else:
        if receipt.worker_pid is None:
            raise DispatchSupervisorValidationError(
                "worker_pid must be set at WORKER_STARTED"
            )
        if receipt.worker_creation_time is None:
            raise DispatchSupervisorValidationError(
                "worker_creation_time must be set at WORKER_STARTED"
            )
    if receipt.creator_pid is None or receipt.creator_creation_time is None:
        raise DispatchSupervisorValidationError(
            "creator_pid and creator_creation_time must always be set"
        )


def _validate_pid_optional(value: object, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise DispatchSupervisorValidationError(
            name + " must be a non-bool int or None"
        )
    if value < 0:
        raise DispatchSupervisorValidationError(name + " must be >= 0")


def _validate_creation_optional(value: object, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise DispatchSupervisorValidationError(
            name + " must be a non-empty str or None"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise DispatchSupervisorValidationError(name + " must not contain NUL/CR/LF")


def _validate_safe_id(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise DispatchSupervisorValidationError(
            name + " must be a non-empty str"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise DispatchSupervisorValidationError(name + " must not contain NUL/CR/LF")
    if value != value.strip():
        raise DispatchSupervisorValidationError(
            name + " must not have leading or trailing whitespace"
        )
    if _SAFE_ID_RE.fullmatch(value) is None:
        raise DispatchSupervisorValidationError(
            name + " must be a safe single-segment identifier"
        )


@dataclass(frozen=True, slots=True)
class DispatchFinalizerTombstone:
    """Immutable 12-field durable finalizer tombstone.

    A ``FINALIZED`` tombstone may only be written after the Worker has
    ended, the heartbeat has ended, and the release has completed.
    Tombstone absence does not prove death.
    """

    schema_version: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    generation_id: str
    winner: str
    worker_done: bool
    heartbeat_done: bool
    release_completed: bool
    failure_kind: str | None
    finalized_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DispatchSupervisorValidationError(
                "schema_version must be " + SCHEMA_VERSION
            )
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise DispatchSupervisorValidationError("task_id must match TC-NNN")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise DispatchSupervisorValidationError("revision must be a non-bool int")
        if self.revision < 1:
            raise DispatchSupervisorValidationError("revision must be >= 1")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise DispatchSupervisorValidationError("attempt must be a non-bool int")
        if self.attempt < 1:
            raise DispatchSupervisorValidationError("attempt must be >= 1")
        _validate_safe_id(self.dispatch_id, "dispatch_id")
        _validate_safe_id(self.generation_id, "generation_id")
        if not isinstance(self.winner, str) or not self.winner:
            raise DispatchSupervisorValidationError("winner must be a non-empty str")
        for name in ("worker_done", "heartbeat_done", "release_completed"):
            if not isinstance(getattr(self, name), bool):
                raise DispatchSupervisorValidationError(name + " must be bool")
        if self.failure_kind is not None:
            if not isinstance(self.failure_kind, str) or not self.failure_kind:
                raise DispatchSupervisorValidationError(
                    "failure_kind must be a non-empty str or None"
                )
            if self.failure_kind not in _DISPATCH_FAILURE_KINDS:
                raise DispatchSupervisorValidationError(
                    "failure_kind must be a frozen dispatch failure kind"
                )
        if not isinstance(self.finalized_at, str) or not self.finalized_at:
            raise DispatchSupervisorValidationError(
                "finalized_at must be non-empty str"
            )
        if _RFC3339_RE.fullmatch(self.finalized_at) is None:
            raise DispatchSupervisorValidationError(
                "finalized_at must be RFC 3339 UTC"
            )


# ── exception hierarchy ─────────────────────────────────────────────────────


class DispatchSupervisorEvidenceError(Exception):
    """Base for all durable dispatch supervisor evidence errors."""


class DispatchSupervisorValidationError(DispatchSupervisorEvidenceError):
    """Schema or field validation failure."""


class DispatchSupervisorStoreError(DispatchSupervisorEvidenceError):
    """Filesystem / atomic-write / lock failure."""


class DispatchSupervisorPhaseError(DispatchSupervisorEvidenceError):
    """Illegal phase transition (jump, backward, or cross-generation)."""


class DispatchSupervisorFencingError(DispatchSupervisorEvidenceError):
    """Generation mismatch or stale receipt on a guarded write."""


class DispatchSupervisorContentionError(DispatchSupervisorEvidenceError):
    """Another writer holds the exclusive store lock."""


# ── serialization ────────────────────────────────────────────────────────────


def _receipt_to_dict(receipt: DispatchProcessReceipt) -> dict[str, object]:
    return {name: getattr(receipt, name) for name in _RECEIPT_FIELD_NAMES}


def _tombstone_to_dict(tombstone: DispatchFinalizerTombstone) -> dict[str, object]:
    return {name: getattr(tombstone, name) for name in _TOMBSTONE_FIELD_NAMES}


def _receipt_from_dict(data: dict[str, object]) -> DispatchProcessReceipt:
    if not isinstance(data, dict):
        raise DispatchSupervisorValidationError("receipt must be a dict")
    missing = _RECEIPT_FIELD_SET - set(data.keys())
    extra = set(data.keys()) - _RECEIPT_FIELD_SET
    if missing or extra:
        raise DispatchSupervisorValidationError(
            "receipt field set mismatch (missing/extra)"
        )
    kwargs: dict[str, object] = dict(data)
    return DispatchProcessReceipt(**kwargs)  # type: ignore[arg-type]


def _tombstone_from_dict(data: dict[str, object]) -> DispatchFinalizerTombstone:
    if not isinstance(data, dict):
        raise DispatchSupervisorValidationError("tombstone must be a dict")
    missing = _TOMBSTONE_FIELD_SET - set(data.keys())
    extra = set(data.keys()) - _TOMBSTONE_FIELD_SET
    if missing or extra:
        raise DispatchSupervisorValidationError(
            "tombstone field set mismatch (missing/extra)"
        )
    return DispatchFinalizerTombstone(**data)  # type: ignore[arg-type]


def validate_dispatch_supervisor_receipt(data: object) -> None:
    """Pure re-validation of a receipt dict.  Raises on any violation."""
    if not isinstance(data, dict):
        raise DispatchSupervisorValidationError("receipt must be a dict")
    _receipt_from_dict(data)


# ── path helpers ─────────────────────────────────────────────────────────────


def _validate_project_root(project_root: Path) -> None:
    if not isinstance(project_root, Path):
        raise DispatchSupervisorValidationError("project_root must be a Path")
    if not project_root.is_absolute():
        raise DispatchSupervisorValidationError("project_root must be absolute")
    if not project_root.is_dir():
        raise DispatchSupervisorValidationError("project_root must be an existing dir")


def _store_dir(project_root: Path) -> Path:
    return project_root / _RUNTIME_RELATIVE


def _receipt_path(project_root: Path, dispatch_id: str) -> Path:
    _validate_safe_id(dispatch_id, "dispatch_id")
    return _store_dir(project_root) / (dispatch_id + ".receipt.yaml")


def _tombstone_path(project_root: Path, dispatch_id: str) -> Path:
    _validate_safe_id(dispatch_id, "dispatch_id")
    return _store_dir(project_root) / (dispatch_id + ".tombstone.yaml")


def _lock_path(project_root: Path) -> Path:
    return _store_dir(project_root) / _LOCK_NAME


# ── symlink / reparse rejection ──────────────────────────────────────────────


def _is_symlink_or_reparse(entry: Path) -> bool:
    if entry.is_symlink():
        return True
    try:
        st = os.lstat(str(entry))
        import stat as _stat
        if hasattr(st, "st_file_attributes") and hasattr(
            _stat, "FILE_ATTRIBUTE_REPARSE_POINT"
        ):
            if st.st_file_attributes & _stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    except (AttributeError, OSError):
        pass
    return False


# ── exclusive store lock (token-checked, O_CREAT|O_EXCL) ─────────────────────


@contextmanager
def _exclusive_store_lock(project_root: Path) -> Iterator[None]:
    store_dir = _store_dir(project_root)
    store_dir.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(project_root)
    token = secrets.token_hex(16)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise DispatchSupervisorContentionError(
            "another writer holds the dispatch-supervisor lock"
        ) from None
    except OSError as exc:
        raise DispatchSupervisorStoreError(
            "cannot create dispatch-supervisor lock"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            lock.unlink()
        except OSError:
            pass
        raise
    body_exception: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_exception = exc
    finally:
        release_error: str | None = None
        try:
            stored = lock.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            release_error = "lock file disappeared while held"
        except OSError:
            release_error = "lock file unreadable while held"
        else:
            if stored == token:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    release_error = "lock file could not be removed"
            else:
                release_error = "lock token mismatch — taken over"
        if body_exception is not None:
            raise body_exception
        if release_error is not None:
            raise DispatchSupervisorStoreError(release_error)


# ── atomic write ─────────────────────────────────────────────────────────────


def _fsync_parent_directory(path: Path) -> None:
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError as exc:
        if not _is_dir_fsync_allowed_error(exc):
            raise
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if not _is_dir_fsync_allowed_error(exc):
            raise
    finally:
        os.close(dir_fd)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode: int | None = None
    try:
        previous_mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".yaml", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, previous_mode if previous_mode is not None else 0o644)
        os.replace(tmp, path)
        _fsync_parent_directory(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _serialize(data: dict[str, object]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


# ── read API ─────────────────────────────────────────────────────────────────


def _read_bytes_reject_symlink(path: Path) -> bytes | None:
    try:
        if _is_symlink_or_reparse(path):
            raise DispatchSupervisorValidationError(
                "symlink or reparse point forbidden"
            )
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DispatchSupervisorStoreError("cannot read dispatch-supervisor file") from exc


def read_dispatch_receipt(project_root: Path, dispatch_id: str) -> DispatchProcessReceipt | None:
    _validate_project_root(project_root)
    raw = _read_bytes_reject_symlink(_receipt_path(project_root, dispatch_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchSupervisorValidationError(
            "receipt is not valid JSON"
        ) from exc
    return _receipt_from_dict(data)


def read_dispatch_tombstone(
    project_root: Path, dispatch_id: str
) -> DispatchFinalizerTombstone | None:
    _validate_project_root(project_root)
    raw = _read_bytes_reject_symlink(_tombstone_path(project_root, dispatch_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchSupervisorValidationError(
            "tombstone is not valid JSON"
        ) from exc
    return _tombstone_from_dict(data)


# ── monotonic phase progression ──────────────────────────────────────────────


def _now_utc_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_receipt(project_root: Path, receipt: DispatchProcessReceipt) -> None:
    data = _receipt_to_dict(receipt)
    _atomic_write_bytes(_receipt_path(project_root, receipt.dispatch_id), _serialize(data))


def _enforce_transition(
    current: DispatchProcessReceipt,
    target_phase: DispatchReceiptPhase,
    generation_id: str,
) -> None:
    if current.generation_id != generation_id:
        raise DispatchSupervisorFencingError("generation_id mismatch")
    if DispatchReceiptPhase(current.phase) == target_phase:
        raise DispatchSupervisorPhaseError("phase already reached")
    expected = _ALLOWED_TRANSITIONS.get(DispatchReceiptPhase(current.phase))
    if expected is None or expected is not target_phase:
        raise DispatchSupervisorPhaseError("illegal phase transition")


def reserve_receipt(
    project_root: Path,
    *,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    lease_epoch: int,
    holder_instance_id: str,
    generation_id: str,
    creator_pid: int,
    creator_creation_time: str,
    boot_id: str,
) -> DispatchProcessReceipt:
    """Write the RESERVED receipt.  Must be the first write for this dispatch."""
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        existing = read_dispatch_receipt(project_root, dispatch_id)
        if existing is not None:
            raise DispatchSupervisorPhaseError("receipt already exists for dispatch")
        receipt = DispatchProcessReceipt(
            schema_version=SCHEMA_VERSION,
            task_id=task_id,
            revision=revision,
            attempt=attempt,
            dispatch_id=dispatch_id,
            lease_epoch=lease_epoch,
            holder_instance_id=holder_instance_id,
            generation_id=generation_id,
            platform=("win32" if os.name == "nt" else "posix"),
            boot_id=boot_id,
            phase=DispatchReceiptPhase.RESERVED.value,
            creator_pid=creator_pid,
            creator_creation_time=creator_creation_time,
            supervisor_pid=None,
            supervisor_creation_time=None,
            worker_pid=None,
            worker_creation_time=None,
            worker_process_group=None,
            written_at=_now_utc_str(),
        )
        _write_receipt(project_root, receipt)
        return receipt


def advance_to_supervisor_ready(
    project_root: Path,
    *,
    dispatch_id: str,
    generation_id: str,
    supervisor_pid: int,
    supervisor_creation_time: str,
) -> DispatchProcessReceipt:
    """Atomic RESERVED → SUPERVISOR_READY under generation exact-match."""
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        current = read_dispatch_receipt(project_root, dispatch_id)
        if current is None:
            raise DispatchSupervisorFencingError("receipt not found")
        _enforce_transition(current, DispatchReceiptPhase.SUPERVISOR_READY, generation_id)
        updated = _replace_receipt(
            current,
            phase=DispatchReceiptPhase.SUPERVISOR_READY.value,
            supervisor_pid=supervisor_pid,
            supervisor_creation_time=supervisor_creation_time,
            written_at=_now_utc_str(),
        )
        _write_receipt(project_root, updated)
        return updated


def advance_to_worker_started(
    project_root: Path,
    *,
    dispatch_id: str,
    generation_id: str,
    worker_pid: int,
    worker_creation_time: str,
    worker_process_group: int | None,
) -> DispatchProcessReceipt:
    """Atomic SUPERVISOR_READY → WORKER_STARTED under generation exact-match."""
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        current = read_dispatch_receipt(project_root, dispatch_id)
        if current is None:
            raise DispatchSupervisorFencingError("receipt not found")
        _enforce_transition(current, DispatchReceiptPhase.WORKER_STARTED, generation_id)
        updated = _replace_receipt(
            current,
            phase=DispatchReceiptPhase.WORKER_STARTED.value,
            worker_pid=worker_pid,
            worker_creation_time=worker_creation_time,
            worker_process_group=worker_process_group,
            written_at=_now_utc_str(),
        )
        _write_receipt(project_root, updated)
        return updated


def advance_to_finalizing(
    project_root: Path,
    *,
    dispatch_id: str,
    generation_id: str,
) -> DispatchProcessReceipt:
    """Atomic WORKER_STARTED → FINALIZING under generation exact-match."""
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        current = read_dispatch_receipt(project_root, dispatch_id)
        if current is None:
            raise DispatchSupervisorFencingError("receipt not found")
        _enforce_transition(current, DispatchReceiptPhase.FINALIZING, generation_id)
        updated = _replace_receipt(
            current,
            phase=DispatchReceiptPhase.FINALIZING.value,
            written_at=_now_utc_str(),
        )
        _write_receipt(project_root, updated)
        return updated


def _replace_receipt(current: DispatchProcessReceipt, **changes: object) -> DispatchProcessReceipt:
    base = _receipt_to_dict(current)
    base.update(changes)
    return _receipt_from_dict(base)


def write_finalizer_tombstone(
    project_root: Path,
    *,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    generation_id: str,
    winner: str,
    worker_done: bool,
    heartbeat_done: bool,
    release_completed: bool,
    failure_kind: str | None,
) -> DispatchFinalizerTombstone:
    """Write the FINALIZED tombstone and advance the receipt to FINALIZED.

    Requires a durable receipt in the ``FINALIZING`` phase with matching
    ``generation_id``, ``task_id``, ``revision``, ``attempt``, and
    ``dispatch_id``.  The tombstone is written first; the receipt is then
    advanced from ``FINALIZING`` to ``FINALIZED`` under the same store
    lock.  If both writes already succeeded, byte-exact replay returns
    the existing tombstone.  If the tombstone write succeeded but the
    process crashed before the receipt advance, replay finishes the
    advance and returns the existing tombstone.

    Requires ``worker_done``, ``heartbeat_done``, and ``release_completed``
    all be ``True``.  Cross-generation writes and content-divergent
    writes are rejected; a receipt already ``FINALIZED`` but missing its
    tombstone is fail-closed.
    """
    _validate_project_root(project_root)
    if not (worker_done and heartbeat_done and release_completed):
        raise DispatchSupervisorPhaseError(
            "FINALIZED requires worker_done, heartbeat_done, and release_completed"
        )
    tombstone = DispatchFinalizerTombstone(
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        revision=revision,
        attempt=attempt,
        dispatch_id=dispatch_id,
        generation_id=generation_id,
        winner=winner,
        worker_done=worker_done,
        heartbeat_done=heartbeat_done,
        release_completed=release_completed,
        failure_kind=failure_kind,
        finalized_at=_now_utc_str(),
    )
    path = _tombstone_path(project_root, dispatch_id)
    with _exclusive_store_lock(project_root):
        existing = read_dispatch_tombstone(project_root, dispatch_id)
        receipt = read_dispatch_receipt(project_root, dispatch_id)

        if receipt is None:
            raise DispatchSupervisorPhaseError("receipt not found")

        # Caller and receipt must agree on generation and identity.
        if receipt.generation_id != generation_id:
            raise DispatchSupervisorFencingError(
                "receipt generation mismatch — refusing finalization"
            )
        for field in ("task_id", "revision", "attempt", "dispatch_id"):
            if getattr(receipt, field) != locals()[field]:
                raise DispatchSupervisorPhaseError(
                    "receipt identity mismatch — refusing finalization"
                )

        if existing is not None:
            if existing.generation_id != generation_id:
                raise DispatchSupervisorFencingError(
                    "tombstone generation mismatch — refusing overwrite"
                )
            # Byte-exact replay: same generation + same content fields.
            for field in _TOMBSTONE_REPLAY_FIELDS:
                if getattr(existing, field) != getattr(tombstone, field):
                    raise DispatchSupervisorPhaseError(
                        "tombstone already exists with different content"
                    )
            if receipt.phase == DispatchReceiptPhase.FINALIZED.value:
                return existing
            if receipt.phase == DispatchReceiptPhase.FINALIZING.value:
                _write_receipt(
                    project_root,
                    _replace_receipt(
                        receipt,
                        phase=DispatchReceiptPhase.FINALIZED.value,
                        written_at=_now_utc_str(),
                    ),
                )
                return existing
            raise DispatchSupervisorPhaseError(
                "tombstone exists but receipt is not FINALIZING or FINALIZED"
            )

        # No tombstone yet.
        if receipt.phase == DispatchReceiptPhase.FINALIZED.value:
            raise DispatchSupervisorPhaseError(
                "receipt FINALIZED but tombstone missing"
            )
        if receipt.phase != DispatchReceiptPhase.FINALIZING.value:
            raise DispatchSupervisorPhaseError(
                "receipt must be in FINALIZING phase"
            )

        # Write tombstone first, then advance receipt.  A crash between the
        # two leaves the receipt at FINALIZING and the tombstone present;
        # the replay path above finishes the advance.
        _atomic_write_bytes(path, _serialize(_tombstone_to_dict(tombstone)))
        _write_receipt(
            project_root,
            _replace_receipt(
                receipt,
                phase=DispatchReceiptPhase.FINALIZED.value,
                written_at=_now_utc_str(),
            ),
        )
        return tombstone


# ── process identity & liveness ─────────────────────────────────────────────

_BOOT_ID_CACHE: str | None = None


def get_boot_id() -> str:
    """Return a stable per-boot identity string.

    POSIX: ``/proc/sys/kernel/random/boot_id``.
    Windows: boot-start = ``now_utc - GetTickCount64()`` (stable within a boot;
    a reboot changes it, which is exactly the reuse-detection signal).
    """
    global _BOOT_ID_CACHE
    if _BOOT_ID_CACHE is not None:
        return _BOOT_ID_CACHE
    if os.name != "nt":
        try:
            with open("/proc/sys/kernel/random/boot_id", "r", encoding="utf-8") as fh:
                bid = fh.read().strip()
            if bid:
                _BOOT_ID_CACHE = bid
                return _BOOT_ID_CACHE
        except OSError:
            pass
        # Fallback: derive a per-boot id from uptime + machine.
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as fh:
                uptime = fh.read().split()[0]
            _BOOT_ID_CACHE = "uptime:" + uptime
            return _BOOT_ID_CACHE
        except OSError:
            _BOOT_ID_CACHE = "unknown-posix-boot"
            return _BOOT_ID_CACHE
    # Windows
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        get_tick = kernel32.GetTickCount64
        get_tick.restype = ctypes.c_uint64
        uptime_ms = get_tick()
        boot_start = datetime.now(UTC) - timedelta(milliseconds=uptime_ms)
        _BOOT_ID_CACHE = "win-boot:" + boot_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return _BOOT_ID_CACHE
    except Exception:
        _BOOT_ID_CACHE = "unknown-win32-boot"
        return _BOOT_ID_CACHE


def get_process_creation_time(pid: int) -> str | None:
    """Return a stable per-process incarnation string, or None if unavailable.

    POSIX: ``/proc/<pid>/stat`` field 22 (start time in jiffies since boot).
    Windows: ``GetProcessTimes`` CreationTime as a comparable string.
    """
    if pid is None or pid < 0:
        return None
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
                stat = fh.read()
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError:
            return None
        # comm is in parens and may contain spaces; split after the last ')'.
        idx = stat.rfind(")")
        if idx < 0:
            return None
        rest = stat[idx + 2:].split()
        # start_time is the 22nd field (1-indexed); after comm it is index 19.
        if len(rest) <= 19:
            return None
        return "posix-starttime:" + rest[19]
    # Windows
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None
        try:
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]
            creation = FILETIME()
            exit_time = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return "win-creation:" + str(value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def get_current_process_identity() -> tuple[int, str]:
    """Return (pid, creation_time) for the current process."""
    return os.getpid(), get_process_creation_time(os.getpid()) or ""


def _probe_pid_state(pid: int) -> str:
    """Return ``"absent"`` | ``"present"`` | ``"inaccessible"``.

    ``inaccessible`` means the PID exists but cannot be inspected
    (permission denied or platform limitation) — callers must map this to
    UNKNOWN, never DEAD.
    """
    if pid <= 0:
        return "absent"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "absent"
        except PermissionError:
            return "inaccessible"
        except OSError:
            return "inaccessible"
        return "present"
    # Windows
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ERROR_INVALID_PARAMETER = 87
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            err = kernel32.GetLastError()
            if err == ERROR_INVALID_PARAMETER:
                return "absent"
            return "inaccessible"
        kernel32.CloseHandle(handle)
        return "present"
    except Exception:
        return "inaccessible"


def _worker_tree_dead(recorded_pgid: int | None) -> bool:
    """POSIX: confirm no living process shares the recorded process group.

    On Windows there is no process group; the tree cannot be confirmed and
    this returns False (caller maps to UNKNOWN).
    """
    if os.name == "nt" or recorded_pgid is None:
        return False
    proc_dir = Path("/proc")
    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        # /proc/<pid>/stat parsed for pgid (field 5) and start_time (field 22).
        try:
            with open(entry / "stat", "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            continue
        idx = content.rfind(")")
        if idx < 0:
            continue
        rest = content[idx + 2:].split()
        if len(rest) <= 4:
            continue
        try:
            pgid = int(rest[2])  # field 5 (1-indexed) → index 2 after comm
        except (ValueError, IndexError):
            continue
        if pgid == recorded_pgid:
            return False  # a living member exists
    return True


def probe_process(
    pid: int | None,
    creation_time: str | None,
    boot_id: str | None,
    *,
    recorded_process_group: int | None = None,
    require_tree: bool = False,
) -> ProcessLiveness:
    """Three-state liveness probe for a single subject.

    Compares PID + creation time/incarnation + boot_id.  When
    ``require_tree`` is True (Worker), the whole process tree must also be
    confirmed dead before returning DEAD; an unconfirmable tree is UNKNOWN.

    UNKNOWN is never downgraded to DEAD.
    """
    if pid is None or creation_time is None or boot_id is None:
        return ProcessLiveness.UNKNOWN
    state = _probe_pid_state(pid)
    if state == "absent":
        # PID absent.  For the Worker, also require the tree to be gone.
        if require_tree and not _worker_tree_dead(recorded_process_group):
            return ProcessLiveness.UNKNOWN
        return ProcessLiveness.DEAD
    if state == "inaccessible":
        # PID exists but cannot be inspected — never DEAD.
        return ProcessLiveness.UNKNOWN
    current_creation = get_process_creation_time(pid)
    if current_creation is None:
        # PID present but incarnation unreadable (permissions/platform).
        return ProcessLiveness.UNKNOWN
    if current_creation != creation_time:
        # PID reused.
        return ProcessLiveness.UNKNOWN
    current_boot = get_boot_id()
    if current_boot != boot_id:
        # System rebooted since the receipt was written.
        return ProcessLiveness.UNKNOWN
    return ProcessLiveness.ALIVE
