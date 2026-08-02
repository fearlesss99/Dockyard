"""Durable scheduler-to-Worker handoff evidence.

The module owns scheduler-side evidence only.  It never acquires a Worker
lease, writes canonical task/event/outbox state, starts a process, or calls a
provider, model, API, network, or Worker.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from core_types import WorkerKind
from dispatch_supervisor_evidence import DispatchProcessReceipt, read_dispatch_receipt
from portfolio_scheduler_store import (
    PortfolioSchedulerConflictError,
    PortfolioSchedulerError,
    PortfolioSchedulerInputError,
    PortfolioSchedulerLockConflictError,
    PortfolioSchedulerNotFoundError,
    PortfolioSchedulerPermissionError,
    PortfolioSchedulerSchemaError,
    PortfolioSchedulerSecurityError,
    _atomic_store_bytes,
    _ensure_directory,
    _fsync_parent_directory,
    _prepare_store,
    _safe_file,
    _store_lock,
    _validate_project_root,
)

__all__ = [
    "HANDOFF_SCHEMA_VERSION",
    "HANDOFF_RECEIPT_SCHEMA_VERSION",
    "ScheduledDispatchHandoffPhase",
    "ScheduledDispatchHandoffOutcome",
    "ScheduledDispatchHandoff",
    "ScheduledDispatchHandoffReceipt",
    "PortfolioSchedulerWorkerHandoffStoreError",
    "PortfolioSchedulerWorkerHandoffInputError",
    "PortfolioSchedulerWorkerHandoffSchemaError",
    "PortfolioSchedulerWorkerHandoffSecurityError",
    "PortfolioSchedulerWorkerHandoffConflictError",
    "PortfolioSchedulerWorkerHandoffPhaseError",
    "PortfolioSchedulerWorkerHandoffNotFoundError",
    "PortfolioSchedulerWorkerHandoffPermissionError",
    "encode_handoff",
    "decode_handoff",
    "encode_handoff_receipt",
    "decode_handoff_receipt",
    "PortfolioSchedulerWorkerHandoffStore",
]

HANDOFF_SCHEMA_VERSION = "agentdesk.portfolio-scheduler-worker-handoff/v1"
HANDOFF_RECEIPT_SCHEMA_VERSION = HANDOFF_SCHEMA_VERSION
_HANDOFF_STORE_RELATIVE = Path("docs") / "pm" / "portfolio-scheduler"
_HANDOFF_DIRECTORY = "worker-handoffs"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_TASK_ID_RE = re.compile(r"TC-[0-9]{3,}\Z")
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_HANDOFF_FIELDS = (
    "schema_version", "handoff_id", "queue_id", "plan_digest", "receipt_id",
    "task_id", "revision", "attempt", "dispatch_id", "dispatch_event_id",
    "outbox_message_id", "lease_id", "lease_epoch", "holder_instance_id",
    "worker_kind", "assessment_id", "expected_snapshot_commit",
    "provider_binding_id", "phase", "reserved_at", "supervisor_started_at",
    "worker_started_at", "acknowledged_at", "finalized_at", "content_digest",
)
_RECEIPT_FIELDS = (
    "schema_version", "handoff_id", "dispatch_id", "generation_id", "task_id",
    "revision", "attempt", "phase", "binding_digest", "written_at",
    "content_digest",
)


class PortfolioSchedulerWorkerHandoffStoreError(PortfolioSchedulerError):
    """Stable base error whose text contains no evidence or path data."""


class PortfolioSchedulerWorkerHandoffInputError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


class PortfolioSchedulerWorkerHandoffSchemaError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


class PortfolioSchedulerWorkerHandoffSecurityError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


class PortfolioSchedulerWorkerHandoffConflictError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


class PortfolioSchedulerWorkerHandoffPhaseError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


class PortfolioSchedulerWorkerHandoffNotFoundError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


class PortfolioSchedulerWorkerHandoffPermissionError(PortfolioSchedulerWorkerHandoffStoreError):
    pass


def _fail(error_type: type[PortfolioSchedulerWorkerHandoffStoreError], code: str) -> None:
    raise error_type("portfolio_scheduler_worker_handoff:" + code)


def _text(value: object, code: str) -> str:
    if type(value) is not str:
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_type")
    if not value or value != value.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_unsafe")
    return value


def _id(value: object, code: str, prefix: str | None = None) -> str:
    value = _text(value, code)
    if "/" in value or "\\" in value or value in (".", ".."):
        _fail(PortfolioSchedulerWorkerHandoffSecurityError, code + "_path")
    if _SAFE_ID_RE.fullmatch(value) is None:
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_format")
    if prefix is not None and not value.startswith(prefix):
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_prefix")
    return value


def _positive(value: object, code: str, maximum: int | None = None) -> int:
    if type(value) is not int:
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_type")
    if value < 1 or (maximum is not None and value > maximum):
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_range")
    return value


def _digest(value: object, code: str) -> str:
    value = _text(value, code)
    if _SHA256_RE.fullmatch(value) is None:
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_format")
    return value


def _timestamp(value: object, code: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    value = _text(value, code)
    if _TIMESTAMP_RE.fullmatch(value) is None:
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_format")
    return value


def _scalar(value: object, code: str) -> str:
    if value is None:
        return "null"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_scalar_type")


def _finish(lines: tuple[str, ...]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


class ScheduledDispatchHandoffPhase(str, Enum):
    ADMISSION_COMMITTED = "ADMISSION_COMMITTED"
    HANDOFF_RESERVED = "HANDOFF_RESERVED"
    SUPERVISOR_STARTED = "SUPERVISOR_STARTED"
    WORKER_STARTED = "WORKER_STARTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FINALIZING = "FINALIZING"
    FINALIZED = "FINALIZED"

    @classmethod
    def order(cls) -> tuple[ScheduledDispatchHandoffPhase, ...]:
        return tuple(cls)


class ScheduledDispatchHandoffOutcome(str, Enum):
    HANDOFF_ACCEPTED = "HANDOFF_ACCEPTED"
    HANDOFF_REJECTED = "HANDOFF_REJECTED"
    HANDOFF_DEFERRED = "HANDOFF_DEFERRED"
    HANDOFF_FAILED = "HANDOFF_FAILED"


@dataclass(frozen=True, slots=True)
class ScheduledDispatchHandoff:
    schema_version: str
    handoff_id: str
    queue_id: str
    plan_digest: str
    receipt_id: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    dispatch_event_id: str
    outbox_message_id: str
    lease_id: str
    lease_epoch: int
    holder_instance_id: str
    worker_kind: WorkerKind
    assessment_id: str
    expected_snapshot_commit: str
    provider_binding_id: str
    phase: ScheduledDispatchHandoffPhase
    reserved_at: str
    supervisor_started_at: str | None
    worker_started_at: str | None
    acknowledged_at: str | None
    finalized_at: str | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != HANDOFF_SCHEMA_VERSION:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "schema_version")
        _id(self.handoff_id, "handoff_id", "HNDF-")
        _id(self.queue_id, "queue_id")
        _digest(self.plan_digest, "plan_digest")
        _id(self.receipt_id, "receipt_id")
        if type(self.task_id) is not str or _TASK_ID_RE.fullmatch(self.task_id) is None:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "task_id")
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt", 3)
        _id(self.dispatch_id, "dispatch_id", "DSP-")
        _id(self.dispatch_event_id, "dispatch_event_id", "EVT-")
        _id(self.outbox_message_id, "outbox_message_id", "MSG-")
        _id(self.lease_id, "lease_id")
        _positive(self.lease_epoch, "lease_epoch")
        _id(self.holder_instance_id, "holder_instance_id")
        if type(self.worker_kind) is not WorkerKind:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "worker_kind")
        _id(self.assessment_id, "assessment_id", "ASM-")
        if type(self.expected_snapshot_commit) is not str or _SHA40_RE.fullmatch(self.expected_snapshot_commit) is None:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "expected_snapshot_commit")
        _id(self.provider_binding_id, "provider_binding_id")
        if type(self.phase) is not ScheduledDispatchHandoffPhase:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "phase")
        _timestamp(self.reserved_at, "reserved_at")
        position = self.phase.order().index(self.phase)
        for timestamp, name, required_position in (
            (self.supervisor_started_at, "supervisor_started_at", 2),
            (self.worker_started_at, "worker_started_at", 3),
            (self.acknowledged_at, "acknowledged_at", 4),
            (self.finalized_at, "finalized_at", 6),
        ):
            if position >= required_position:
                _timestamp(timestamp, name)
            elif timestamp is not None:
                _fail(PortfolioSchedulerWorkerHandoffInputError, name + "_premature")
        _digest(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class ScheduledDispatchHandoffReceipt:
    schema_version: str
    handoff_id: str
    dispatch_id: str
    generation_id: str
    task_id: str
    revision: int
    attempt: int
    phase: ScheduledDispatchHandoffPhase
    binding_digest: str
    written_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != HANDOFF_RECEIPT_SCHEMA_VERSION:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "receipt_schema_version")
        _id(self.handoff_id, "receipt_handoff_id", "HNDF-")
        _id(self.dispatch_id, "receipt_dispatch_id", "DSP-")
        _id(self.generation_id, "generation_id")
        if type(self.task_id) is not str or _TASK_ID_RE.fullmatch(self.task_id) is None:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "receipt_task_id")
        _positive(self.revision, "receipt_revision")
        _positive(self.attempt, "receipt_attempt", 3)
        if type(self.phase) is not ScheduledDispatchHandoffPhase:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "receipt_phase")
        _digest(self.binding_digest, "binding_digest")
        _timestamp(self.written_at, "written_at")
        _digest(self.content_digest, "receipt_content_digest")


def _handoff_values(handoff: ScheduledDispatchHandoff) -> tuple[object, ...]:
    return (
        handoff.schema_version, handoff.handoff_id, handoff.queue_id,
        handoff.plan_digest, handoff.receipt_id, handoff.task_id,
        handoff.revision, handoff.attempt, handoff.dispatch_id,
        handoff.dispatch_event_id, handoff.outbox_message_id, handoff.lease_id,
        handoff.lease_epoch, handoff.holder_instance_id, handoff.worker_kind.value,
        handoff.assessment_id, handoff.expected_snapshot_commit,
        handoff.provider_binding_id, handoff.phase.value, handoff.reserved_at,
        handoff.supervisor_started_at, handoff.worker_started_at,
        handoff.acknowledged_at, handoff.finalized_at, handoff.content_digest,
    )


def _receipt_values(receipt: ScheduledDispatchHandoffReceipt) -> tuple[object, ...]:
    return (
        receipt.schema_version, receipt.handoff_id, receipt.dispatch_id,
        receipt.generation_id, receipt.task_id, receipt.revision, receipt.attempt,
        receipt.phase.value, receipt.binding_digest, receipt.written_at,
        receipt.content_digest,
    )


def _lines(fields: tuple[str, ...], values: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(field + ": " + _scalar(value, field) for field, value in zip(fields, values))


def _content_digest(fields: tuple[str, ...], values: tuple[object, ...]) -> str:
    return "sha256:" + hashlib.sha256(_finish(_lines(fields, values))).hexdigest()


def encode_handoff(handoff: ScheduledDispatchHandoff) -> bytes:
    if type(handoff) is not ScheduledDispatchHandoff:
        _fail(PortfolioSchedulerWorkerHandoffInputError, "handoff_type")
    values = _handoff_values(handoff)
    if handoff.content_digest != _content_digest(_HANDOFF_FIELDS[:-1], values[:-1]):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "handoff_digest")
    return _finish(_lines(_HANDOFF_FIELDS, values))


def encode_handoff_receipt(receipt: ScheduledDispatchHandoffReceipt) -> bytes:
    if type(receipt) is not ScheduledDispatchHandoffReceipt:
        _fail(PortfolioSchedulerWorkerHandoffInputError, "receipt_type")
    values = _receipt_values(receipt)
    if receipt.content_digest != _content_digest(_RECEIPT_FIELDS[:-1], values[:-1]):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "receipt_digest")
    return _finish(_lines(_RECEIPT_FIELDS, values))


def _parse_scalar(token: str, code: str) -> object:
    if token == "null":
        return None
    if token.startswith('"') and token.endswith('"'):
        chars: list[str] = []
        index = 1
        end = len(token) - 1
        while index < end:
            character = token[index]
            if character == "\\":
                index += 1
                if index >= end or token[index] not in ('"', "\\"):
                    _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_escape")
                chars.append(token[index])
            elif character == '"' or ord(character) < 0x20 or ord(character) == 0x7F:
                _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_quote")
            else:
                chars.append(character)
            index += 1
        value = "".join(chars)
        if _scalar(value, code) != token:
            _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_noncanonical")
        return value
    if re.fullmatch(r"0|[1-9][0-9]*", token):
        return int(token)
    _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_scalar")


def _document(data: object, code: str) -> tuple[str, ...]:
    if type(data) is not bytes:
        _fail(PortfolioSchedulerWorkerHandoffInputError, code + "_bytes_type")
    if not data or data.startswith(b"\xef\xbb\xbf") or not data.endswith(b"\n"):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_document")
    if b"\r" in data or b"\t" in data or any(
        byte < 0x20 and byte != 0x0A for byte in data
    ):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_control")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_utf8")
    lines = tuple(text[:-1].split("\n"))
    if any(not line for line in lines):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_blank")
    return lines


def _ordered(lines: tuple[str, ...], fields: tuple[str, ...], code: str) -> tuple[object, ...]:
    if len(lines) != len(fields):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_field_count")
    values: list[object] = []
    for line, field in zip(lines, fields):
        prefix = field + ": "
        if not line.startswith(prefix):
            _fail(PortfolioSchedulerWorkerHandoffSchemaError, code + "_field_order")
        values.append(_parse_scalar(line[len(prefix):], field))
    return tuple(values)


def decode_handoff(data: bytes) -> ScheduledDispatchHandoff:
    values = _ordered(_document(data, "handoff"), _HANDOFF_FIELDS, "handoff")
    if type(values[14]) is not str or type(values[18]) is not str:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "handoff_enum")
    try:
        handoff = ScheduledDispatchHandoff(
            values[0], values[1], values[2], values[3], values[4], values[5],
            values[6], values[7], values[8], values[9], values[10], values[11],
            values[12], values[13], WorkerKind(values[14]), values[15], values[16],
            values[17], ScheduledDispatchHandoffPhase(values[18]), values[19],
            values[20], values[21], values[22], values[23], values[24],
        )
    except PortfolioSchedulerWorkerHandoffStoreError:
        raise
    except (TypeError, ValueError):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "handoff_fields")
    if encode_handoff(handoff) != data:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "handoff_noncanonical")
    return handoff


def decode_handoff_receipt(data: bytes) -> ScheduledDispatchHandoffReceipt:
    values = _ordered(_document(data, "receipt"), _RECEIPT_FIELDS, "receipt")
    if type(values[7]) is not str:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "receipt_enum")
    try:
        receipt = ScheduledDispatchHandoffReceipt(
            values[0], values[1], values[2], values[3], values[4], values[5],
            values[6], ScheduledDispatchHandoffPhase(values[7]), values[8],
            values[9], values[10],
        )
    except PortfolioSchedulerWorkerHandoffStoreError:
        raise
    except (TypeError, ValueError):
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "receipt_fields")
    if encode_handoff_receipt(receipt) != data:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "receipt_noncanonical")
    return receipt


def _binding_digest(handoff_id: str, generation_id: str) -> str:
    return "sha256:" + hashlib.sha256(
        handoff_id.encode("utf-8") + generation_id.encode("utf-8")
    ).hexdigest()


def _make_receipt(
    handoff: ScheduledDispatchHandoff,
    process_receipt: DispatchProcessReceipt,
    written_at: str,
) -> ScheduledDispatchHandoffReceipt:
    _timestamp(written_at, "receipt_written_at")
    receipt = ScheduledDispatchHandoffReceipt(
        HANDOFF_RECEIPT_SCHEMA_VERSION, handoff.handoff_id, handoff.dispatch_id,
        process_receipt.generation_id, handoff.task_id, handoff.revision,
        handoff.attempt, handoff.phase,
        _binding_digest(handoff.handoff_id, process_receipt.generation_id),
        written_at, "sha256:" + "0" * 64,
    )
    values = _receipt_values(receipt)
    return replace(
        receipt,
        content_digest=_content_digest(_RECEIPT_FIELDS[:-1], values[:-1]),
    )


def _with_handoff_digest(handoff: ScheduledDispatchHandoff) -> ScheduledDispatchHandoff:
    placeholder = replace(handoff, content_digest="sha256:" + "0" * 64)
    values = _handoff_values(placeholder)
    return replace(
        placeholder,
        content_digest=_content_digest(_HANDOFF_FIELDS[:-1], values[:-1]),
    )


def _process_receipt(
    handoff: ScheduledDispatchHandoff,
    supplied: DispatchProcessReceipt | None,
    project_root: Path,
) -> DispatchProcessReceipt:
    receipt = supplied
    if receipt is None:
        try:
            receipt = read_dispatch_receipt(project_root, handoff.dispatch_id)
        except Exception:
            _fail(PortfolioSchedulerWorkerHandoffSchemaError, "dispatch_receipt")
        if receipt is None:
            _fail(PortfolioSchedulerWorkerHandoffNotFoundError, "dispatch_receipt")
    if type(receipt) is not DispatchProcessReceipt:
        _fail(PortfolioSchedulerWorkerHandoffInputError, "dispatch_receipt_type")
    if (
        receipt.dispatch_id != handoff.dispatch_id
        or receipt.task_id != handoff.task_id
        or receipt.revision != handoff.revision
        or receipt.attempt != handoff.attempt
        or receipt.lease_epoch != handoff.lease_epoch
        or receipt.holder_instance_id != handoff.holder_instance_id
    ):
        _fail(PortfolioSchedulerWorkerHandoffConflictError, "dispatch_receipt_identity")
    return receipt


def _handoff_dir(project_root: Path, create: bool) -> Path:
    try:
        root = _validate_project_root(project_root)
        if create:
            base = _prepare_store(root, True)
        else:
            base = root / _HANDOFF_STORE_RELATIVE
            current = root
            for component in _HANDOFF_STORE_RELATIVE.parts:
                current = current / component
                _ensure_directory(current, False)
        directory = base / _HANDOFF_DIRECTORY
        _ensure_directory(directory, create)
        return directory
    except PortfolioSchedulerWorkerHandoffStoreError:
        raise
    except PortfolioSchedulerNotFoundError:
        _fail(PortfolioSchedulerWorkerHandoffNotFoundError, "store")
    except PortfolioSchedulerSecurityError:
        _fail(PortfolioSchedulerWorkerHandoffSecurityError, "store")
    except PortfolioSchedulerPermissionError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "store")
    except PortfolioSchedulerError:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "store")
    except OSError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "store")


def _path(directory: Path, dispatch_id: str, suffix: str) -> Path:
    dispatch_id = _id(dispatch_id, "path_dispatch_id", "DSP-")
    candidate = directory / (dispatch_id + suffix)
    if candidate.parent != directory or candidate.name != dispatch_id + suffix:
        _fail(PortfolioSchedulerWorkerHandoffSecurityError, "path_containment")
    return candidate


def _handoff_path(directory: Path, dispatch_id: str) -> Path:
    return _path(directory, dispatch_id, ".yaml")


def _receipt_path(directory: Path, dispatch_id: str) -> Path:
    return _path(directory, dispatch_id, ".receipt.yaml")


def _reject_extra_dispatch_files(directory: Path, dispatch_id: str) -> None:
    allowed = {dispatch_id + ".yaml", dispatch_id + ".receipt.yaml"}
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "directory_read")
    for entry in entries:
        if entry.name.startswith(dispatch_id + ".") and entry.name not in allowed:
            _fail(PortfolioSchedulerWorkerHandoffSecurityError, "extra_file")


def _read_optional(path: Path) -> bytes | None:
    try:
        return _safe_file(path, True)
    except PortfolioSchedulerNotFoundError:
        return None
    except PortfolioSchedulerSecurityError:
        _fail(PortfolioSchedulerWorkerHandoffSecurityError, "file")
    except PortfolioSchedulerPermissionError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "file")
    except PortfolioSchedulerError:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "file")


def _write(path: Path, content: bytes) -> None:
    try:
        _atomic_store_bytes(path, content)
        _fsync_parent_directory(path)
    except PortfolioSchedulerSecurityError:
        _fail(PortfolioSchedulerWorkerHandoffSecurityError, "write_file")
    except PortfolioSchedulerPermissionError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "write_file")
    except PortfolioSchedulerError:
        _fail(PortfolioSchedulerWorkerHandoffSchemaError, "write_file")
    except OSError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "write_file")


@contextmanager
def _handoff_lock(project_root: Path):
    try:
        with _store_lock(project_root):
            yield
    except PortfolioSchedulerLockConflictError:
        _fail(PortfolioSchedulerWorkerHandoffConflictError, "lock_contention")
    except PortfolioSchedulerPermissionError:
        _fail(PortfolioSchedulerWorkerHandoffPermissionError, "lock")
    except PortfolioSchedulerSecurityError:
        _fail(PortfolioSchedulerWorkerHandoffSecurityError, "lock")


def _pair(
    directory: Path, dispatch_id: str
) -> tuple[ScheduledDispatchHandoff, ScheduledDispatchHandoffReceipt]:
    handoff_raw = _read_optional(_handoff_path(directory, dispatch_id))
    receipt_raw = _read_optional(_receipt_path(directory, dispatch_id))
    if handoff_raw is None or receipt_raw is None:
        _fail(PortfolioSchedulerWorkerHandoffNotFoundError, "handoff_pair")
    handoff = decode_handoff(handoff_raw)
    receipt = decode_handoff_receipt(receipt_raw)
    if (
        handoff.dispatch_id != dispatch_id
        or receipt.dispatch_id != dispatch_id
        or receipt.handoff_id != handoff.handoff_id
        or receipt.task_id != handoff.task_id
        or receipt.revision != handoff.revision
        or receipt.attempt != handoff.attempt
        or receipt.phase != handoff.phase
    ):
        _fail(PortfolioSchedulerWorkerHandoffConflictError, "handoff_pair_identity")
    return handoff, receipt


class PortfolioSchedulerWorkerHandoffStore:
    """Atomic and replayable scheduler-side handoff evidence store."""

    def __init__(self, project_root: Path) -> None:
        try:
            self._project_root = _validate_project_root(project_root)
        except PortfolioSchedulerError:
            raise
        except Exception:
            _fail(PortfolioSchedulerWorkerHandoffSecurityError, "project_root")

    def reserve_handoff(
        self,
        handoff: ScheduledDispatchHandoff,
        dispatch_receipt: DispatchProcessReceipt | None = None,
        written_at: str | None = None,
    ) -> tuple[ScheduledDispatchHandoff, ScheduledDispatchHandoffReceipt]:
        if type(handoff) is not ScheduledDispatchHandoff:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "handoff_type")
        if handoff.phase is not ScheduledDispatchHandoffPhase.ADMISSION_COMMITTED:
            _fail(PortfolioSchedulerWorkerHandoffPhaseError, "reserve_phase")
        process = _process_receipt(handoff, dispatch_receipt, self._project_root)
        timestamp = handoff.reserved_at if written_at is None else written_at
        _timestamp(timestamp, "reservation_time")
        reserved = _with_handoff_digest(
            replace(handoff, phase=ScheduledDispatchHandoffPhase.HANDOFF_RESERVED)
        )
        receipt = _make_receipt(reserved, process, timestamp)
        handoff_bytes = encode_handoff(reserved)
        receipt_bytes = encode_handoff_receipt(receipt)
        directory = _handoff_dir(self._project_root, True)
        handoff_path = _handoff_path(directory, handoff.dispatch_id)
        receipt_path = _receipt_path(directory, handoff.dispatch_id)
        _reject_extra_dispatch_files(directory, handoff.dispatch_id)
        with _handoff_lock(self._project_root):
            existing_handoff = _read_optional(handoff_path)
            existing_receipt = _read_optional(receipt_path)
            if existing_handoff is not None or existing_receipt is not None:
                if existing_handoff is None or existing_receipt is None:
                    _fail(PortfolioSchedulerWorkerHandoffConflictError, "partial_pair")
                if existing_handoff != handoff_bytes or existing_receipt != receipt_bytes:
                    _fail(PortfolioSchedulerWorkerHandoffConflictError, "divergent_replay")
                return decode_handoff(existing_handoff), decode_handoff_receipt(existing_receipt)
            _write(handoff_path, handoff_bytes)
            _write(receipt_path, receipt_bytes)
        return reserved, receipt

    reserve = reserve_handoff
    create_handoff = reserve_handoff

    def read_handoff(self, dispatch_id: str) -> ScheduledDispatchHandoff:
        directory = _handoff_dir(self._project_root, False)
        _reject_extra_dispatch_files(directory, dispatch_id)
        return _pair(directory, dispatch_id)[0]

    read = read_handoff

    def read_handoff_receipt(self, dispatch_id: str) -> ScheduledDispatchHandoffReceipt:
        directory = _handoff_dir(self._project_root, False)
        _reject_extra_dispatch_files(directory, dispatch_id)
        return _pair(directory, dispatch_id)[1]

    def advance_phase(
        self,
        dispatch_id: str,
        target_phase: ScheduledDispatchHandoffPhase,
        written_at: str,
        dispatch_receipt: DispatchProcessReceipt | None = None,
    ) -> tuple[ScheduledDispatchHandoff, ScheduledDispatchHandoffReceipt]:
        dispatch_id = _id(dispatch_id, "dispatch_id", "DSP-")
        if type(target_phase) is not ScheduledDispatchHandoffPhase:
            _fail(PortfolioSchedulerWorkerHandoffInputError, "target_phase")
        _timestamp(written_at, "phase_written_at")
        directory = _handoff_dir(self._project_root, False)
        _reject_extra_dispatch_files(directory, dispatch_id)
        with _handoff_lock(self._project_root):
            current, current_receipt = _pair(directory, dispatch_id)
            process = _process_receipt(current, dispatch_receipt, self._project_root)
            order = current.phase.order()
            current_index = order.index(current.phase)
            target_index = order.index(target_phase)
            if target_index == current_index:
                previous_timestamp = {
                    ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED: current.supervisor_started_at,
                    ScheduledDispatchHandoffPhase.WORKER_STARTED: current.worker_started_at,
                    ScheduledDispatchHandoffPhase.ACKNOWLEDGED: current.acknowledged_at,
                    ScheduledDispatchHandoffPhase.FINALIZED: current.finalized_at,
                }.get(target_phase)
                if previous_timestamp == written_at:
                    return current, current_receipt
                _fail(PortfolioSchedulerWorkerHandoffConflictError, "phase_replay")
            if target_index != current_index + 1:
                _fail(PortfolioSchedulerWorkerHandoffPhaseError, "phase_transition")
            advanced = current
            if target_phase is ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED:
                advanced = replace(advanced, phase=target_phase, supervisor_started_at=written_at)
            elif target_phase is ScheduledDispatchHandoffPhase.WORKER_STARTED:
                advanced = replace(advanced, phase=target_phase, worker_started_at=written_at)
            elif target_phase is ScheduledDispatchHandoffPhase.ACKNOWLEDGED:
                advanced = replace(advanced, phase=target_phase, acknowledged_at=written_at)
            elif target_phase is ScheduledDispatchHandoffPhase.FINALIZING:
                advanced = replace(advanced, phase=target_phase)
            elif target_phase is ScheduledDispatchHandoffPhase.FINALIZED:
                advanced = replace(advanced, phase=target_phase, finalized_at=written_at)
            advanced = _with_handoff_digest(advanced)
            advanced_receipt = _make_receipt(advanced, process, written_at)
            _write(_handoff_path(directory, dispatch_id), encode_handoff(advanced))
            _write(_receipt_path(directory, dispatch_id), encode_handoff_receipt(advanced_receipt))
            return advanced, advanced_receipt

    advance = advance_phase
    advance_handoff = advance_phase
