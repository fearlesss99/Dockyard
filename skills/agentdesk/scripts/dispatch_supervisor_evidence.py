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
import hashlib
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
    "RETRY_RECEIPT_SCHEMA_VERSION",
    "DispatchReceiptPhase",
    "OwnerLossRetryPhase",
    "ProcessLiveness",
    "DispatchProcessReceipt",
    "DispatchFinalizerTombstone",
    "OwnerLossRetryReceipt",
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
    "probe_dispatch_process_tree",
    "read_retry_receipt",
    "read_retry_tombstone",
    "reserve_retry_receipt",
    "advance_retry_to_started",
    "advance_retry_to_finalizing",
    "advance_retry_to_finalized",
    "compute_retry_content_digest",
]

# ── constants ───────────────────────────────────────────────────────────────

SCHEMA_VERSION = "agentdesk.dispatch-supervisor-evidence/v1"
RETRY_RECEIPT_SCHEMA_VERSION = "agentdesk.owner-loss-retry-receipt/v1"

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
_EVENT_ID_RE = re.compile(r"^EVT-.+")
_PAYLOAD_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
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


class OwnerLossRetryPhase(str, Enum):
    """Exact six durable owner-loss retry receipt phases, forward-only.

    Frozen from TC-13.18d.12c.1 §18.2.
    """

    RECOVERY_TRANSITION_PENDING = "RECOVERY_TRANSITION_PENDING"
    RECOVERY_TRANSITION_COMMITTED = "RECOVERY_TRANSITION_COMMITTED"
    RETRY_RESERVED = "RETRY_RESERVED"
    RETRY_STARTED = "RETRY_STARTED"
    RETRY_FINALIZING = "RETRY_FINALIZING"
    RETRY_FINALIZED = "RETRY_FINALIZED"

    @classmethod
    def order(cls) -> tuple[OwnerLossRetryPhase, ...]:
        return (
            cls.RECOVERY_TRANSITION_PENDING,
            cls.RECOVERY_TRANSITION_COMMITTED,
            cls.RETRY_RESERVED,
            cls.RETRY_STARTED,
            cls.RETRY_FINALIZING,
            cls.RETRY_FINALIZED,
        )


_RETRY_ALLOWED_TRANSITIONS: dict[OwnerLossRetryPhase, OwnerLossRetryPhase] = {
    OwnerLossRetryPhase.RECOVERY_TRANSITION_PENDING: OwnerLossRetryPhase.RECOVERY_TRANSITION_COMMITTED,
    OwnerLossRetryPhase.RECOVERY_TRANSITION_COMMITTED: OwnerLossRetryPhase.RETRY_RESERVED,
    OwnerLossRetryPhase.RETRY_RESERVED: OwnerLossRetryPhase.RETRY_STARTED,
    OwnerLossRetryPhase.RETRY_STARTED: OwnerLossRetryPhase.RETRY_FINALIZING,
    OwnerLossRetryPhase.RETRY_FINALIZING: OwnerLossRetryPhase.RETRY_FINALIZED,
}


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


# ── OwnerLossRetryReceipt — frozen 24-field retry reservation evidence ──────
#
# Frozen from TC-13.18d.12c.1 §18.3.
# Immutable, slotted, no Any/dict/Mapping/mutable collections.

_RETRY_RECEIPT_FIELD_NAMES: tuple[str, ...] = (
    "schema_version",
    "task_id",
    "revision",
    "failed_attempt",
    "failed_dispatch_id",
    "recovery_event_id",
    "recovery_generation_id",
    "next_attempt",
    "next_dispatch_id",
    "next_dispatch_event_id",
    "phase",
    "creator_pid",
    "creator_creation_time",
    "creator_boot_id",
    "supervisor_pid",
    "supervisor_creation_time",
    "supervisor_boot_id",
    "worker_pid",
    "worker_creation_time",
    "worker_boot_id",
    "reserved_at",
    "started_at",
    "finalized_at",
    "content_digest",
)
_RETRY_RECEIPT_FIELD_SET: frozenset[str] = frozenset(_RETRY_RECEIPT_FIELD_NAMES)

# Fields that must match for byte-exact replay validation.
_RETRY_REPLAY_FIELDS: tuple[str, ...] = (
    "task_id",
    "revision",
    "failed_attempt",
    "failed_dispatch_id",
    "recovery_event_id",
    "recovery_generation_id",
    "next_attempt",
    "next_dispatch_id",
    "next_dispatch_event_id",
    "content_digest",
)


@dataclass(frozen=True, slots=True)
class OwnerLossRetryReceipt:
    """Immutable 24-field durable owner-loss retry receipt.

    Frozen from TC-13.18d.12c.1 §18.3.  All fields are typed; no Any,
    dict, Mapping, or mutable collection.  ``frozen=True, slots=True``.

    Identity tuple: (task_id, revision, failed_attempt, failed_dispatch_id,
    recovery_event_id, recovery_generation_id, next_attempt, next_dispatch_id,
    next_dispatch_event_id, content_digest).
    """

    schema_version: str
    task_id: str
    revision: int
    failed_attempt: int
    failed_dispatch_id: str
    recovery_event_id: str
    recovery_generation_id: str
    next_attempt: int
    next_dispatch_id: str
    next_dispatch_event_id: str
    phase: str
    creator_pid: int
    creator_creation_time: str
    creator_boot_id: str
    supervisor_pid: int | None
    supervisor_creation_time: str | None
    supervisor_boot_id: str | None
    worker_pid: int | None
    worker_creation_time: str | None
    worker_boot_id: str | None
    reserved_at: str
    started_at: str | None
    finalized_at: str | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != RETRY_RECEIPT_SCHEMA_VERSION:
            raise DispatchSupervisorValidationError(
                "schema_version must be " + RETRY_RECEIPT_SCHEMA_VERSION
            )
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise DispatchSupervisorValidationError("task_id must match TC-NNN")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise DispatchSupervisorValidationError("revision must be a non-bool int")
        if self.revision < 1:
            raise DispatchSupervisorValidationError("revision must be >= 1")
        if isinstance(self.failed_attempt, bool) or not isinstance(self.failed_attempt, int):
            raise DispatchSupervisorValidationError("failed_attempt must be a non-bool int")
        if self.failed_attempt < 1:
            raise DispatchSupervisorValidationError("failed_attempt must be >= 1")
        _validate_safe_id(self.failed_dispatch_id, "failed_dispatch_id")
        if not isinstance(self.recovery_event_id, str) or not self.recovery_event_id:
            raise DispatchSupervisorValidationError("recovery_event_id must be non-empty str")
        if _EVENT_ID_RE.fullmatch(self.recovery_event_id) is None:
            raise DispatchSupervisorValidationError("recovery_event_id must match EVT-*")
        _validate_safe_id(self.recovery_generation_id, "recovery_generation_id")
        if isinstance(self.next_attempt, bool) or not isinstance(self.next_attempt, int):
            raise DispatchSupervisorValidationError("next_attempt must be a non-bool int")
        if not 1 <= self.next_attempt <= 3:
            raise DispatchSupervisorValidationError("next_attempt must be 1..3")
        if self.next_attempt != self.failed_attempt + 1:
            raise DispatchSupervisorValidationError(
                "next_attempt must equal failed_attempt + 1"
            )
        _validate_safe_id(self.next_dispatch_id, "next_dispatch_id")
        if self.next_dispatch_id == self.failed_dispatch_id:
            raise DispatchSupervisorValidationError(
                "next_dispatch_id must differ from failed_dispatch_id"
            )
        if not isinstance(self.next_dispatch_event_id, str) or not self.next_dispatch_event_id:
            raise DispatchSupervisorValidationError("next_dispatch_event_id must be non-empty str")
        if _EVENT_ID_RE.fullmatch(self.next_dispatch_event_id) is None:
            raise DispatchSupervisorValidationError("next_dispatch_event_id must match EVT-*")
        try:
            OwnerLossRetryPhase(self.phase)
        except ValueError as exc:
            raise DispatchSupervisorValidationError(
                "phase must be a frozen retry phase"
            ) from exc
        # Creator identity — required at reservation.
        _validate_pid_required(self.creator_pid, "creator_pid")
        if not isinstance(self.creator_creation_time, str) or not self.creator_creation_time:
            raise DispatchSupervisorValidationError(
                "creator_creation_time must be a non-empty str"
            )
        if not isinstance(self.creator_boot_id, str) or not self.creator_boot_id:
            raise DispatchSupervisorValidationError("creator_boot_id must be a non-empty str")
        # Supervisor identity — all-present or all-absent.
        _validate_identity_triple_optional(
            self.supervisor_pid, self.supervisor_creation_time, self.supervisor_boot_id,
            "supervisor",
        )
        # Worker identity — all-present or all-absent.
        _validate_identity_triple_optional(
            self.worker_pid, self.worker_creation_time, self.worker_boot_id,
            "worker",
        )
        # reserved_at must be RFC 3339 UTC.
        if not isinstance(self.reserved_at, str) or not self.reserved_at:
            raise DispatchSupervisorValidationError("reserved_at must be non-empty str")
        if _RFC3339_RE.fullmatch(self.reserved_at) is None:
            raise DispatchSupervisorValidationError("reserved_at must be RFC 3339 UTC")
        # started_at — None or RFC 3339 UTC.
        if self.started_at is not None:
            if not isinstance(self.started_at, str) or not self.started_at:
                raise DispatchSupervisorValidationError("started_at must be non-empty str or None")
            if _RFC3339_RE.fullmatch(self.started_at) is None:
                raise DispatchSupervisorValidationError("started_at must be RFC 3339 UTC")
        # finalized_at — None or RFC 3339 UTC.
        if self.finalized_at is not None:
            if not isinstance(self.finalized_at, str) or not self.finalized_at:
                raise DispatchSupervisorValidationError("finalized_at must be non-empty str or None")
            if _RFC3339_RE.fullmatch(self.finalized_at) is None:
                raise DispatchSupervisorValidationError("finalized_at must be RFC 3339 UTC")
        # content_digest must be sha256:hex.
        if not isinstance(self.content_digest, str) or not self.content_digest:
            raise DispatchSupervisorValidationError("content_digest must be non-empty str")
        if _PAYLOAD_DIGEST_RE.fullmatch(self.content_digest) is None:
            raise DispatchSupervisorValidationError("content_digest must match sha256:hex")
        # Phase-field presence validation.
        _validate_retry_phase_field_presence(self)


def _validate_pid_required(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DispatchSupervisorValidationError(
            name + " must be a non-bool int"
        )
    if value < 0:
        raise DispatchSupervisorValidationError(name + " must be >= 0")


def _validate_identity_triple_optional(
    pid: object,
    creation_time: object,
    boot_id: object,
    prefix: str,
) -> None:
    """Validate a supervisor/Worker identity triple: all-None or all-present."""
    if pid is None and creation_time is None and boot_id is None:
        return
    if pid is None or creation_time is None or boot_id is None:
        raise DispatchSupervisorValidationError(
            prefix + " identity fields must be all-None or all-present"
        )
    _validate_pid_required(pid, prefix + "_pid")
    if not isinstance(creation_time, str) or not creation_time:
        raise DispatchSupervisorValidationError(
            prefix + "_creation_time must be a non-empty str"
        )
    if not isinstance(boot_id, str) or not boot_id:
        raise DispatchSupervisorValidationError(
            prefix + "_boot_id must be a non-empty str"
        )


def _validate_retry_phase_field_presence(receipt: OwnerLossRetryReceipt) -> None:
    """A field for a phase not yet reached must be exactly None."""
    phase = OwnerLossRetryPhase(receipt.phase)
    order = OwnerLossRetryPhase.order()
    reached = order.index(phase)
    started_reached = reached >= order.index(OwnerLossRetryPhase.RETRY_STARTED)
    finalized_reached = reached >= order.index(OwnerLossRetryPhase.RETRY_FINALIZED)

    if not started_reached:
        if receipt.supervisor_pid is not None:
            raise DispatchSupervisorValidationError(
                "supervisor_pid must be None before RETRY_STARTED"
            )
        if receipt.supervisor_creation_time is not None:
            raise DispatchSupervisorValidationError(
                "supervisor_creation_time must be None before RETRY_STARTED"
            )
        if receipt.supervisor_boot_id is not None:
            raise DispatchSupervisorValidationError(
                "supervisor_boot_id must be None before RETRY_STARTED"
            )
        if receipt.worker_pid is not None:
            raise DispatchSupervisorValidationError(
                "worker_pid must be None before RETRY_STARTED"
            )
        if receipt.worker_creation_time is not None:
            raise DispatchSupervisorValidationError(
                "worker_creation_time must be None before RETRY_STARTED"
            )
        if receipt.worker_boot_id is not None:
            raise DispatchSupervisorValidationError(
                "worker_boot_id must be None before RETRY_STARTED"
            )
        if receipt.started_at is not None:
            raise DispatchSupervisorValidationError(
                "started_at must be None before RETRY_STARTED"
            )

    if not finalized_reached:
        if receipt.finalized_at is not None:
            raise DispatchSupervisorValidationError(
                "finalized_at must be None before RETRY_FINALIZED"
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


# ── Windows Job Object private API (TC-13.18d.12a-pre2.2) ──────────────────
#
# All handles, structures, constants, and Windows error conversion live inside
# this private boundary.  Handles are never exposed to public API, dataclass,
# receipt, JSONL, or exception messages.  Non-Windows platforms use the
# existing POSIX process-tree logic and never import/execute Windows APIs.

_JOB_NAME_PREFIX = "Local\\AgentDesk-Dispatch-"


def _derive_job_name(generation_id: str) -> str:
    """Return a deterministic named Job Object identifier from *generation_id*.

    SHA-256 of the generation_id is used to produce a non-reversible,
    collision-resistant handle name.  The raw generation_id is never
    embedded in the name or any error message.
    """
    _validate_safe_id(generation_id, "generation_id")
    digest = hashlib.sha256(generation_id.encode("utf-8")).hexdigest()
    return _JOB_NAME_PREFIX + digest


def _derive_job_name_public(generation_id: str) -> str:
    """Public determinstic Job name derivation — pure, no Windows API.

    Kept for test compatibility.  Prefer ``_derive_job_name()`` for new code.
    """
    return _derive_job_name(generation_id)


# ── Windows API ctypes wrapper (win32 only) ────────────────────────────────

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    # Windows constants
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_LIMIT_VALID_FLAGS = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    _JobObjectBasicLimitInformation = 2
    _JobObjectExtendedLimitInformation = 9
    _JobObjectBasicProcessIdList = 3

    # ctypes structures
    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_uint64),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", wintypes.ULONG * 1),  # variable-length
        ]

    # Kernel32 function prototypes
    _kernel32.CreateJobObjectW.argtypes = (
        ctypes.c_void_p,  # lpJobAttributes (NULL = default security)
        wintypes.LPCWSTR,  # lpName
    )
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    _kernel32.OpenJobObjectW.argtypes = (
        wintypes.DWORD,     # dwDesiredAccess
        wintypes.BOOL,      # bInheritHandle
        wintypes.LPCWSTR,   # lpName
    )
    _kernel32.OpenJobObjectW.restype = wintypes.HANDLE

    _kernel32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE,  # hJob
        wintypes.HANDLE,  # hProcess
    )
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,   # hJob
        ctypes.c_int,      # JobObjectInfoClass
        ctypes.c_void_p,   # lpJobObjectInfo
        wintypes.DWORD,    # cbJobObjectInfoLength
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,   # hJob
        ctypes.c_int,      # JobObjectInfoClass
        ctypes.c_void_p,   # lpJobObjectInfo
        wintypes.DWORD,    # cbJobObjectInfoLength
        wintypes.LPDWORD,  # lpReturnLength
    )
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL

    _kernel32.IsProcessInJob.argtypes = (
        wintypes.HANDLE,   # hProcess
        wintypes.HANDLE,   # hJob (NULL to test if in any job)
        wintypes.LPBOOL,   # pbResult
    )
    _kernel32.IsProcessInJob.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    # Windows constants and functions remain private to this boundary.

    _JOB_OBJECT_QUERY = 0x0004
    _JOB_OBJECT_ASSIGN_PROCESS = 0x0001 | _JOB_OBJECT_QUERY
    _JOB_OBJECT_SET_ATTRIBUTES = 0x0002 | _JOB_OBJECT_QUERY

    _MAXIMUM_ALLOWED = 0x02000000

    _ERROR_ACCESS_DENIED = 5
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_INVALID_HANDLE = 6
    _ERROR_NOT_ENOUGH_MEMORY = 8
    _ERROR_INVALID_PARAMETER = 87

    def _get_last_error_message() -> str:
        """Return a safe Win32 error code — never path, pid, or handle."""
        err = int(_kernel32.GetLastError())
        return "win32-error-" + str(err)


def _create_dispatch_job(job_name: str) -> int:
    """Create or open a named Windows Job Object.  Returns a raw handle.

    The handle must be closed by the caller via ``_close_handle()`` or
    ``close_dispatch_job_handle()``.  Raises ``OSError`` on failure.
    """
    if os.name != "nt":
        raise OSError("Job Objects are only available on Windows")
    handle = _kernel32.CreateJobObjectW(None, job_name)
    if not handle:
        raise OSError(
            "CreateJobObjectW failed: " + _get_last_error_message()
        )
    return handle


def _open_dispatch_job(job_name: str, *, desire_access: int | None = None) -> int:
    """Open an existing named Windows Job Object for query.  Returns a raw handle.

    The handle must be closed by the caller.
    Raises ``OSError`` on failure (including when the job doesn't exist).
    """
    if os.name != "nt":
        raise OSError("Job Objects are only available on Windows")
    access = desire_access if desire_access is not None else _JOB_OBJECT_QUERY
    handle = _kernel32.OpenJobObjectW(access, False, job_name)
    if not handle:
        error_code = int(_kernel32.GetLastError())
        raise OSError(
            error_code,
            "OpenJobObjectW failed: " + _get_last_error_message(),
        )
    return handle


def _set_kill_on_job_close(handle: int) -> None:
    """Configure ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` on *handle*.

    Only ``KILL_ON_JOB_CLOSE`` is set; no CPU/memory/time/UI/active-process
    limits are applied.  Raises ``OSError`` on failure.
    """
    if os.name != "nt":
        raise OSError("Job Objects are only available on Windows")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        handle,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        raise OSError(
            "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed: "
            + _get_last_error_message()
        )
    _verify_kill_on_job_close(handle)


def _verify_kill_on_job_close(handle: int) -> None:
    """Verify that ``KILL_ON_JOB_CLOSE`` is actually set on *handle*.

    Raises ``OSError`` if the flag cannot be confirmed.
    """
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    ret_len = wintypes.DWORD(0)
    ok = _kernel32.QueryInformationJobObject(
        handle,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(ret_len),
    )
    if not ok:
        raise OSError(
            "QueryInformationJobObject(verify) failed: "
            + _get_last_error_message()
        )
    if not (info.BasicLimitInformation.LimitFlags & _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
        raise OSError(
            "KILL_ON_JOB_CLOSE was not confirmed after SetInformationJobObject"
        )


def _assign_process_to_job(handle: int, pid: int) -> None:
    """Assign the process identified by *pid* to *handle*'s Job.

    Opens the target process with ``PROCESS_SET_QUOTA`` | ``PROCESS_TERMINATE``
    access, assigns it to the Job, then closes the process handle.

    Raises ``OSError`` if any step fails.
    """
    if os.name != "nt":
        raise OSError("Job Objects are only available on Windows")

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    access = PROCESS_SET_QUOTA | PROCESS_TERMINATE

    proc_handle = _kernel32.OpenProcess(access, False, pid)
    if not proc_handle:
        raise OSError(
            "OpenProcess(assign) failed: " + _get_last_error_message()
        )
    try:
        ok = _kernel32.AssignProcessToJobObject(handle, proc_handle)
        if not ok:
            raise OSError(
                "AssignProcessToJobObject failed: "
                + _get_last_error_message()
            )
    finally:
        _kernel32.CloseHandle(proc_handle)


def _is_process_in_any_job(pid: int) -> bool | None:
    """Return True if *pid* is already in a Job, False if not, None on error."""
    if os.name != "nt":
        return None
    PROCESS_QUERY_LIMITED_INFO = 0x1000
    proc_handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFO, False, pid)
    if not proc_handle:
        return None
    try:
        result = wintypes.BOOL(False)
        ok = _kernel32.IsProcessInJob(proc_handle, None, ctypes.byref(result))
        if not ok:
            return None
        return bool(result.value)
    finally:
        _kernel32.CloseHandle(proc_handle)


def _is_process_in_specific_job(pid: int, job_handle: int) -> bool | None:
    """Return True if *pid* is in the Job named by *job_handle*.

    Returns None on any Windows API failure.
    """
    if os.name != "nt":
        return None
    PROCESS_QUERY_LIMITED_INFO = 0x1000
    proc_handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFO, False, pid)
    if not proc_handle:
        return None
    try:
        result = wintypes.BOOL(False)
        ok = _kernel32.IsProcessInJob(proc_handle, job_handle, ctypes.byref(result))
        if not ok:
            return None
        return bool(result.value)
    finally:
        _kernel32.CloseHandle(proc_handle)


def _query_job_active_process_count(handle: int) -> int | None:
    """Return the number of active processes in the Job, or None on failure."""
    if os.name != "nt":
        return None
    # Use BasicProcessIdList with a generous initial buffer.
    # The structure starts with space for 1 process; we overallocate.
    class _ProcessIdListBuffer(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", wintypes.ULONG * 4096),
        ]

    buf = _ProcessIdListBuffer()
    ret_len = wintypes.DWORD(0)
    ok = _kernel32.QueryInformationJobObject(
        handle,
        _JobObjectBasicProcessIdList,
        ctypes.byref(buf),
        ctypes.sizeof(buf),
        ctypes.byref(ret_len),
    )
    if not ok:
        return None
    return int(buf.NumberOfAssignedProcesses)


def _close_handle(handle: int) -> None:
    """Close a Windows handle.  No-op on non-Windows or invalid handle."""
    if os.name != "nt":
        return
    if handle == 0 or handle is None:
        return
    _kernel32.CloseHandle(handle)


# ── _DispatchJobOwner — private typed supervisor-side Job handle owner ──────
#
# This class is the ONLY place that creates, holds, or closes a raw Job handle.
# It is private to this module.  The supervisor runner and tests reach it
# through module-internal access (``dse._DispatchJobOwner``), never through the
# public ``__all__``.  The handle is NEVER exposed to dataclass, receipt,
# tombstone, JSONL, argv, env, or exception message.

class _DispatchJobOwner:
    """Private typed owner of a Windows Job Object for a single dispatch
    generation.

    Encapsulates the lifecycle:  create → configure → assign supervisor →
    hold until process exit.  On error paths the caller may explicitly
    ``close()`` to terminate the Job tree; on normal paths the handle
    survives until the process exits and the OS closes it.

    On non-Windows platforms this is a no-op stub — ``create()`` raises
    ``OSError`` and the runner must guard with ``os.name == "nt"``.
    """

    __slots__ = ("_handle", "_generation_id")

    def __init__(self, generation_id: str) -> None:
        self._handle: int = 0
        self._generation_id = generation_id
        if os.name == "nt":
            _validate_safe_id(generation_id, "generation_id")
            job_name = _derive_job_name(generation_id)
            self._handle = _create_dispatch_job(job_name)
            try:
                _set_kill_on_job_close(self._handle)
            except Exception:
                _close_handle(self._handle)
                self._handle = 0
                raise

    @property
    def handle(self) -> int:
        """The raw handle (int, 0 when not initialised).  Internal-only."""
        return self._handle

    def assign_supervisor(self, pid: int) -> None:
        """Assign the process identified by *pid* to this Job.

        Must only be called after construction succeeded and before
        ``SUPERVISOR_READY`` is persisted.

        Raises ``OSError`` on failure.
        """
        if os.name != "nt":
            raise OSError("Job Objects are only available on Windows")
        if self._handle == 0:
            raise OSError("_DispatchJobOwner has no live handle")
        _assign_process_to_job(self._handle, pid)

    def query_active_process_count(self) -> int | None:
        """Return the number of active processes in this Job, or None."""
        if os.name != "nt" or self._handle == 0:
            return None
        return _query_job_active_process_count(self._handle)

    def is_process_in_job(self, pid: int) -> bool | None:
        """Return True if *pid* is in this Job, False if not, None on error."""
        if os.name != "nt" or self._handle == 0:
            return None
        return _is_process_in_specific_job(pid, self._handle)

    def close(self) -> None:
        """Close the Job handle, triggering KILL_ON_JOB_CLOSE.

        Idempotent — safe to call multiple times.  After this call the
        owner is exhausted.
        """
        if self._handle:
            _close_handle(self._handle)
            self._handle = 0

    @staticmethod
    def probe_job_by_name(generation_id: str) -> int | None:
        """Open and query the named Job for *generation_id*.

        Opens by name, queries active process count, then closes.
        Returns None on any failure.  This is a probe-only door — it
        opens, reads, and closes; it never holds a handle.

        Public consumers call ``probe_dispatch_process_tree()`` instead;
        this is only for the internal Windows probe path.
        """
        if os.name != "nt":
            return None
        job_name = _derive_job_name(generation_id)
        try:
            h = _open_dispatch_job(job_name)
        except OSError as exc:
            error_code = getattr(exc, "winerror", None)
            if error_code is None:
                error_code = getattr(exc, "errno", None)
            if error_code == 2:
                return -1
            return None
        try:
            return _query_job_active_process_count(h)
        finally:
            _close_handle(h)

    @staticmethod
    def probe_process_in_job_by_name(generation_id: str, pid: int) -> bool | None:
        """Open the named Job and test *pid* membership.

        Opens by name, queries membership, then closes.
        Returns None on any failure.
        """
        if os.name != "nt":
            return None
        job_name = _derive_job_name(generation_id)
        try:
            h = _open_dispatch_job(job_name)
        except OSError:
            return None
        try:
            return _is_process_in_specific_job(pid, h)
        finally:
            _close_handle(h)


# ── legacy internal aliases — keep tests & runner working ───────────────────
#
# These module-internal names are retained so that existing code using
# ``dse._assign_process_to_job``, ``dse._is_process_in_any_job``, etc.
# continues to compile.  They are NOT in __all__ and are NOT public API.
#
# New callers inside this module should go through _DispatchJobOwner;
# external callers (tests) may use these only for Windows API verification
# tests, never for handle ownership.

_create_dispatch_job_handle = _create_dispatch_job  # (unused; tests call _DispatchJobOwner)
_close_dispatch_job_handle = _close_handle  # (unused; tests call owner.close())
_query_active_count_by_name = _DispatchJobOwner.probe_job_by_name
_is_process_in_job_by_name = _DispatchJobOwner.probe_process_in_job_by_name


# ── three-state process-tree probe (Windows Job Object path) ───────────────


def probe_dispatch_process_tree(
    receipt: DispatchProcessReceipt,
) -> ProcessLiveness:
    """Three-state liveness probe for a complete dispatch process tree.

    Windows path (``os.name == "nt"``):
        Uses the named Job Object derived from ``receipt.generation_id``
        together with exact supervisor identity to produce a strict
        ``ALIVE / DEAD / UNKNOWN`` result.

    POSIX path (``os.name != "nt"``):
        Delegates to ``probe_process()`` with tree-aware logic using
        the recorded process group.  This path is unchanged.

    The probe never downgrades ``UNKNOWN`` to ``DEAD``.  Any evidence gap,
    permission failure, or API error returns ``UNKNOWN``.
    """
    # Precondition: receipt must be validated and in a phase >= SUPERVISOR_READY.
    try:
        phase = DispatchReceiptPhase(receipt.phase)
    except ValueError:
        return ProcessLiveness.UNKNOWN

    order = DispatchReceiptPhase.order()
    if phase.value < DispatchReceiptPhase.SUPERVISOR_READY.value:
        # No durable evidence that the Job was ever initialized.
        return ProcessLiveness.UNKNOWN

    # 1. Exact supervisor identity probe.
    supervisor_liveness = probe_process(
        receipt.supervisor_pid,
        receipt.supervisor_creation_time,
        receipt.boot_id,
    )
    if supervisor_liveness == ProcessLiveness.ALIVE:
        return ProcessLiveness.ALIVE

    # 2. Job Object probe (Windows only).
    if os.name == "nt":
        return _probe_dispatch_job_windows(receipt)

    # 3. POSIX: delegate to existing process-group tree logic.
    if receipt.worker_pid is not None:
        worker_liveness = probe_process(
            receipt.worker_pid,
            receipt.worker_creation_time,
            receipt.boot_id,
            recorded_process_group=receipt.worker_process_group,
            require_tree=True,
        )
        if worker_liveness == ProcessLiveness.ALIVE:
            return ProcessLiveness.ALIVE
        # On POSIX, if the supervisor is DEAD and the Worker tree is confirmed
        # DEAD, we can return DEAD.  Otherwise UNKNOWN.
        if supervisor_liveness == ProcessLiveness.DEAD and worker_liveness == ProcessLiveness.DEAD:
            return ProcessLiveness.DEAD
        return ProcessLiveness.UNKNOWN

    # Worker not yet started — can only rely on supervisor state.
    if supervisor_liveness == ProcessLiveness.DEAD:
        return ProcessLiveness.DEAD
    return ProcessLiveness.UNKNOWN


def _probe_dispatch_job_windows(receipt: DispatchProcessReceipt) -> ProcessLiveness:
    """Windows Job Object based process-tree liveness probe.

    Returns:
      - ``ALIVE``: active process count > 0 in the named Job, OR the
        exact supervisor identity is confirmed ALIVE.
      - ``DEAD``: the named Job *was successfully queried and is empty*
        (count == 0) AND supervisor is DEAD AND Worker (if durable) is
        also not ALIVE.  An absent Job is NOT enough for DEAD — a query
        failure could be caused by permissions.
      - ``UNKNOWN``: any evidence gap, permission error, API failure,
        job-not-found, or inconsistent evidence.
    """
    # Probe supervisor exact-identity first.
    supervisor_liveness = probe_process(
        receipt.supervisor_pid,
        receipt.supervisor_creation_time,
        receipt.boot_id,
    )
    if supervisor_liveness == ProcessLiveness.ALIVE:
        return ProcessLiveness.ALIVE

    # Probe the named Job.
    try:
        count = _DispatchJobOwner.probe_job_by_name(receipt.generation_id)
    except Exception:
        return ProcessLiveness.UNKNOWN

    if count is not None and count > 0:
        # Job exists and contains active processes.
        return ProcessLiveness.ALIVE

    if count is not None and count == 0:
        # Job exists but is empty.
        # If supervisor is confirmed DEAD and the Job is empty, we have
        # consistent evidence.  But we also need to check Worker identity.
        if supervisor_liveness != ProcessLiveness.DEAD:
            return ProcessLiveness.UNKNOWN

        if receipt.worker_pid is not None:
            worker_liveness = probe_process(
                receipt.worker_pid,
                receipt.worker_creation_time,
                receipt.boot_id,
            )
            if worker_liveness == ProcessLiveness.ALIVE:
                # Worker is alive but not in the Job — evidence inconsistency.
                return ProcessLiveness.UNKNOWN
            if worker_liveness == ProcessLiveness.UNKNOWN:
                return ProcessLiveness.UNKNOWN

        # Supervisor DEAD, Job empty, Worker not-alive → DEAD.
        return ProcessLiveness.DEAD

    # count is None — Job query failed (Job doesn't exist, permissions
    # denied, or API error).  We cannot distinguish between these cases.
    # Without a confirmed Job query, we must NOT return DEAD.
    if count == -1:
        # SUPERVISOR_READY proves this named Job was created with
        # KILL_ON_JOB_CLOSE. ERROR_FILE_NOT_FOUND proves its last handle is
        # gone. Exact supervisor and Worker identities must independently be
        # DEAD; PID reuse and every other evidence gap remain UNKNOWN.
        if supervisor_liveness != ProcessLiveness.DEAD:
            return ProcessLiveness.UNKNOWN
        if receipt.worker_pid is not None:
            worker_liveness = probe_process(
                receipt.worker_pid,
                receipt.worker_creation_time,
                receipt.boot_id,
            )
            if worker_liveness != ProcessLiveness.DEAD:
                return ProcessLiveness.UNKNOWN
        return ProcessLiveness.DEAD
    return ProcessLiveness.UNKNOWN


def _worker_tree_dead(
    recorded_pgid: int | None, *, _retry_after_observation: bool = True
) -> bool:
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
        process_state = rest[0]
        # Explicit kernel terminal states cannot execute or retain live
        # descendants.  They may remain visible until their parent reaps
        # them, so they must not keep an otherwise-dead Worker tree alive.
        if pgid == recorded_pgid and process_state not in ("Z", "X", "x"):
            # A process can leave its group between the PID probe and this
            # scan (especially while a supervisor reaps a killed Worker).
            # Take one immediate second observation; a persistent member is
            # still UNKNOWN, while a disappearing member is terminal.
            if _retry_after_observation:
                return _worker_tree_dead(
                    recorded_pgid, _retry_after_observation=False
                )
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


# ── retry receipt store ─────────────────────────────────────────────────────
#
# Owner-loss retry receipt and tombstone read/write/advance functions.
# These use the same atomic-write, lock, and symlink-rejection infrastructure
# as the existing dispatch supervisor evidence store.
#
# Storage: .agentdesk/runtime/dispatch-supervisor/<next_dispatch_id>.retry-receipt.yaml
#          .agentdesk/runtime/dispatch-supervisor/<next_dispatch_id>.retry-tombstone.yaml

_RETRY_RECEIPT_SUFFIX = ".retry-receipt.yaml"
_RETRY_TOMBSTONE_SUFFIX = ".retry-tombstone.yaml"


def _retry_receipt_path(project_root: Path, next_dispatch_id: str) -> Path:
    _validate_safe_id(next_dispatch_id, "next_dispatch_id")
    return _store_dir(project_root) / (next_dispatch_id + _RETRY_RECEIPT_SUFFIX)


def _retry_tombstone_path(project_root: Path, next_dispatch_id: str) -> Path:
    _validate_safe_id(next_dispatch_id, "next_dispatch_id")
    return _store_dir(project_root) / (next_dispatch_id + _RETRY_TOMBSTONE_SUFFIX)


def _retry_receipt_to_dict(receipt: OwnerLossRetryReceipt) -> dict[str, object]:
    return {name: getattr(receipt, name) for name in _RETRY_RECEIPT_FIELD_NAMES}


def _retry_receipt_from_dict(data: dict[str, object]) -> OwnerLossRetryReceipt:
    if not isinstance(data, dict):
        raise DispatchSupervisorValidationError("retry receipt must be a dict")
    missing = _RETRY_RECEIPT_FIELD_SET - set(data.keys())
    extra = set(data.keys()) - _RETRY_RECEIPT_FIELD_SET
    if missing or extra:
        raise DispatchSupervisorValidationError(
            "retry receipt field set mismatch (missing/extra)"
        )
    kwargs: dict[str, object] = dict(data)
    return OwnerLossRetryReceipt(**kwargs)  # type: ignore[arg-type]


# ── content digest computation ──────────────────────────────────────────────


def compute_retry_content_digest(
    *,
    task_id: str,
    revision: int,
    failed_attempt: int,
    failed_dispatch_id: str,
    recovery_event_id: str,
    recovery_generation_id: str,
    next_attempt: int,
    next_dispatch_id: str,
    next_dispatch_event_id: str,
) -> str:
    """Compute the frozen ``content_digest`` for a retry receipt.

    SHA-256 over canonical UTF-8 bytes of every identity field except
    ``content_digest``.  Canonical serialization uses frozen field order,
    enum values as strings, no insignificant whitespace, and no
    platform-dependent formatting.

    The digest binds the exact retry plan and identity; it is NOT derived
    from exception text or process output.
    """
    canonical = json.dumps(
        {
            "task_id": task_id,
            "revision": revision,
            "failed_attempt": failed_attempt,
            "failed_dispatch_id": failed_dispatch_id,
            "recovery_event_id": recovery_event_id,
            "recovery_generation_id": recovery_generation_id,
            "next_attempt": next_attempt,
            "next_dispatch_id": next_dispatch_id,
            "next_dispatch_event_id": next_dispatch_event_id,
        },
        ensure_ascii=False,
        indent=None,
        separators=(",", ":"),
        sort_keys=True,
    )
    raw = canonical.encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ── read API ────────────────────────────────────────────────────────────────


def read_retry_receipt(
    project_root: Path,
    next_dispatch_id: str,
) -> OwnerLossRetryReceipt | None:
    """Read an owner-loss retry receipt, or None if absent."""
    _validate_project_root(project_root)
    raw = _read_bytes_reject_symlink(
        _retry_receipt_path(project_root, next_dispatch_id)
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchSupervisorValidationError(
            "retry receipt is not valid JSON"
        ) from exc
    return _retry_receipt_from_dict(data)


def read_retry_tombstone(
    project_root: Path,
    next_dispatch_id: str,
) -> OwnerLossRetryReceipt | None:
    """Read an owner-loss retry tombstone (FINALIZED receipt copy), or None."""
    _validate_project_root(project_root)
    raw = _read_bytes_reject_symlink(
        _retry_tombstone_path(project_root, next_dispatch_id)
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatchSupervisorValidationError(
            "retry tombstone is not valid JSON"
        ) from exc
    return _retry_receipt_from_dict(data)


# ── phase enforcement ───────────────────────────────────────────────────────


def _enforce_retry_transition(
    current: OwnerLossRetryReceipt,
    target_phase: OwnerLossRetryPhase,
    recovery_generation_id: str,
) -> None:
    if current.recovery_generation_id != recovery_generation_id:
        raise DispatchSupervisorFencingError("retry receipt generation_id mismatch")
    if OwnerLossRetryPhase(current.phase) == target_phase:
        raise DispatchSupervisorPhaseError("retry phase already reached")
    expected = _RETRY_ALLOWED_TRANSITIONS.get(OwnerLossRetryPhase(current.phase))
    if expected is None or expected is not target_phase:
        raise DispatchSupervisorPhaseError("illegal retry phase transition")


def _replace_retry_receipt(
    current: OwnerLossRetryReceipt,
    **changes: object,
) -> OwnerLossRetryReceipt:
    base = _retry_receipt_to_dict(current)
    base.update(changes)
    return _retry_receipt_from_dict(base)


def _write_retry_receipt_bytes(project_root: Path, receipt: OwnerLossRetryReceipt) -> None:
    data = _retry_receipt_to_dict(receipt)
    _atomic_write_bytes(
        _retry_receipt_path(project_root, receipt.next_dispatch_id),
        _serialize(data),
    )


# ── atomic reservation (RETRY_RESERVED) ─────────────────────────────────────


def reserve_retry_receipt(
    project_root: Path,
    *,
    task_id: str,
    revision: int,
    failed_attempt: int,
    failed_dispatch_id: str,
    recovery_event_id: str,
    recovery_generation_id: str,
    next_attempt: int,
    next_dispatch_id: str,
    next_dispatch_event_id: str,
    content_digest: str,
    creator_pid: int,
    creator_creation_time: str,
    creator_boot_id: str,
) -> OwnerLossRetryReceipt:
    """Atomically write a ``RETRY_RESERVED`` owner-loss retry receipt.

    Must be called under the existing canonical state lock.  Only one
    reservation may exist for a given ``(task_id, failed_attempt,
    recovery_generation_id)`` identity.  Byte-exact replay returns the
    existing receipt; divergent content rejects without a write.
    """
    _validate_project_root(project_root)

    # Build the proposed receipt.
    proposed = OwnerLossRetryReceipt(
        schema_version=RETRY_RECEIPT_SCHEMA_VERSION,
        task_id=task_id,
        revision=revision,
        failed_attempt=failed_attempt,
        failed_dispatch_id=failed_dispatch_id,
        recovery_event_id=recovery_event_id,
        recovery_generation_id=recovery_generation_id,
        next_attempt=next_attempt,
        next_dispatch_id=next_dispatch_id,
        next_dispatch_event_id=next_dispatch_event_id,
        phase=OwnerLossRetryPhase.RETRY_RESERVED.value,
        creator_pid=creator_pid,
        creator_creation_time=creator_creation_time,
        creator_boot_id=creator_boot_id,
        supervisor_pid=None,
        supervisor_creation_time=None,
        supervisor_boot_id=None,
        worker_pid=None,
        worker_creation_time=None,
        worker_boot_id=None,
        reserved_at=_now_utc_str(),
        started_at=None,
        finalized_at=None,
        content_digest=content_digest,
    )

    with _exclusive_store_lock(project_root):
        existing = read_retry_receipt(project_root, next_dispatch_id)
        if existing is not None:
            # Byte-exact replay check.
            if existing.content_digest != proposed.content_digest:
                raise DispatchSupervisorPhaseError(
                    "retry receipt already exists with different content"
                )
            if existing.recovery_generation_id != proposed.recovery_generation_id:
                raise DispatchSupervisorFencingError(
                    "retry receipt generation mismatch"
                )
            if existing.phase != proposed.phase:
                raise DispatchSupervisorPhaseError(
                    "retry receipt already exists with different phase"
                )
            return existing

        _write_retry_receipt_bytes(project_root, proposed)
        return proposed


# ── phase advance: RETRY_RESERVED → RETRY_STARTED ───────────────────────────


def advance_retry_to_started(
    project_root: Path,
    *,
    next_dispatch_id: str,
    recovery_generation_id: str,
    supervisor_pid: int,
    supervisor_creation_time: str,
    supervisor_boot_id: str,
    worker_pid: int,
    worker_creation_time: str,
    worker_boot_id: str,
) -> OwnerLossRetryReceipt:
    """Atomically advance from RETRY_RESERVED to RETRY_STARTED.

    Requires durable supervisor and Worker evidence proving both have
    started.  Advances under the exclusive store lock with generation
    exact-match.
    """
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        current = read_retry_receipt(project_root, next_dispatch_id)
        if current is None:
            raise DispatchSupervisorFencingError("retry receipt not found")
        _enforce_retry_transition(
            current, OwnerLossRetryPhase.RETRY_STARTED, recovery_generation_id
        )
        updated = _replace_retry_receipt(
            current,
            phase=OwnerLossRetryPhase.RETRY_STARTED.value,
            supervisor_pid=supervisor_pid,
            supervisor_creation_time=supervisor_creation_time,
            supervisor_boot_id=supervisor_boot_id,
            worker_pid=worker_pid,
            worker_creation_time=worker_creation_time,
            worker_boot_id=worker_boot_id,
            started_at=_now_utc_str(),
        )
        _write_retry_receipt_bytes(project_root, updated)
        return updated


# ── phase advance: RETRY_STARTED → RETRY_FINALIZING ─────────────────────────


def advance_retry_to_finalizing(
    project_root: Path,
    *,
    next_dispatch_id: str,
    recovery_generation_id: str,
) -> OwnerLossRetryReceipt:
    """Atomically advance from RETRY_STARTED to RETRY_FINALIZING.

    Only the completion winner, after Worker done, heartbeat done, and
    release completed, may advance.
    """
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        current = read_retry_receipt(project_root, next_dispatch_id)
        if current is None:
            raise DispatchSupervisorFencingError("retry receipt not found")
        _enforce_retry_transition(
            current, OwnerLossRetryPhase.RETRY_FINALIZING, recovery_generation_id
        )
        updated = _replace_retry_receipt(
            current,
            phase=OwnerLossRetryPhase.RETRY_FINALIZING.value,
        )
        _write_retry_receipt_bytes(project_root, updated)
        return updated


# ── phase advance: RETRY_FINALIZING → RETRY_FINALIZED ───────────────────────


def advance_retry_to_finalized(
    project_root: Path,
    *,
    next_dispatch_id: str,
    recovery_generation_id: str,
) -> OwnerLossRetryReceipt:
    """Atomically advance from RETRY_FINALIZING to RETRY_FINALIZED.

    Writes the final tombstone first (a copy of the receipt at FINALIZED),
    then advances the receipt.  Follows the existing tombstone-first crash
    replay rules.
    """
    _validate_project_root(project_root)
    with _exclusive_store_lock(project_root):
        current = read_retry_receipt(project_root, next_dispatch_id)
        if current is None:
            raise DispatchSupervisorFencingError("retry receipt not found")
        _enforce_retry_transition(
            current, OwnerLossRetryPhase.RETRY_FINALIZED, recovery_generation_id
        )
        tombstone_path = _retry_tombstone_path(project_root, next_dispatch_id)
        existing_tombstone = read_retry_tombstone(project_root, next_dispatch_id)

        finalized_receipt = _replace_retry_receipt(
            current,
            phase=OwnerLossRetryPhase.RETRY_FINALIZED.value,
            finalized_at=_now_utc_str(),
        )

        if existing_tombstone is not None:
            # Byte-exact replay: same generation + same content.
            if existing_tombstone.content_digest != finalized_receipt.content_digest:
                raise DispatchSupervisorPhaseError(
                    "retry tombstone already exists with different content"
                )
            if existing_tombstone.recovery_generation_id != recovery_generation_id:
                raise DispatchSupervisorFencingError(
                    "retry tombstone generation mismatch"
                )
            if current.phase == OwnerLossRetryPhase.RETRY_FINALIZED.value:
                return existing_tombstone
            # Tombstone exists but receipt not yet advanced — finish the advance.
            _write_retry_receipt_bytes(project_root, finalized_receipt)
            return existing_tombstone

        # No tombstone yet.
        if current.phase == OwnerLossRetryPhase.RETRY_FINALIZED.value:
            raise DispatchSupervisorPhaseError(
                "retry receipt FINALIZED but tombstone missing"
            )

        # Write tombstone first, then advance receipt.
        _atomic_write_bytes(
            tombstone_path,
            _serialize(_retry_receipt_to_dict(finalized_receipt)),
        )
        _write_retry_receipt_bytes(project_root, finalized_receipt)
        return finalized_receipt
