"""Durable PortfolioScheduler queue and selection evidence store.

This module implements only the evidence boundary frozen by TC-13.24a.  It
does not select work, acquire WorkerSlotLease, write canonical events, start
Workers, or call a provider or network.
"""

from __future__ import annotations

import enum
import hashlib
import os
import re
import secrets
import stat
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from dispatch_supervisor_evidence import (
    _atomic_write_bytes,
    _fsync_parent_directory,
    _is_symlink_or_reparse,
)
from core_types import WorkerKind

__all__ = [
    "BusinessPriority",
    "QueuePhase",
    "ReceiptPhase",
    "ConflictKeyClass",
    "ConflictKey",
    "QueueEntry",
    "ScheduleReceipt",
    "PortfolioSchedulerStore",
    "PortfolioSchedulerError",
    "PortfolioSchedulerInputError",
    "PortfolioSchedulerSchemaError",
    "PortfolioSchedulerSecurityError",
    "PortfolioSchedulerConflictError",
    "PortfolioSchedulerSequenceError",
    "PortfolioSchedulerLockConflictError",
    "PortfolioSchedulerTransitionError",
    "PortfolioSchedulerNotFoundError",
    "PortfolioSchedulerPermissionError",
    "encode_queue_entry",
    "decode_queue_entry",
    "encode_schedule_receipt",
    "decode_schedule_receipt",
]


SCHEMA_VERSION = "agentdesk.portfolio-scheduler/v1"
STORE_RELATIVE = Path("docs") / "pm" / "portfolio-scheduler"
SEQUENCE_FILENAME = "sequence.yaml"
QUEUE_FILENAME = "queue.yaml"
RECEIPTS_DIRECTORY = "receipts"
TOMBSTONES_DIRECTORY = "tombstones"
RESERVATIONS_DIRECTORY = "reservations"
STORE_LOCK_FILENAME = ".portfolio-scheduler-store.lock"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)

_QUEUE_FIELDS = (
    "schema_version",
    "queue_id",
    "task_id",
    "revision",
    "enqueue_sequence",
    "enqueued_at",
    "business_priority",
    "aging_basis_at",
    "worker_kind_request",
    "assessment_id",
    "state",
    "conflict_keys",
    "retry_budget_used",
    "content_digest",
    "selection_generation",
)
_RECEIPT_FIELDS = (
    "schema_version",
    "receipt_id",
    "queue_id",
    "task_id",
    "revision",
    "enqueue_sequence",
    "selected_at",
    "order_key",
    "selection_reason",
    "worker_kind",
    "dispatch_event_id",
    "phase",
    "content_digest",
    "selection_generation",
)


class PortfolioSchedulerError(ValueError):
    """Base class for stable, non-leaking store failures."""


class PortfolioSchedulerInputError(PortfolioSchedulerError):
    """Invalid typed input or operation argument."""


class PortfolioSchedulerSchemaError(PortfolioSchedulerError):
    """Malformed, noncanonical, or schema-incompatible evidence."""


class PortfolioSchedulerSecurityError(PortfolioSchedulerError):
    """Unsafe path, identity, or fail-closed filesystem boundary."""


class PortfolioSchedulerConflictError(PortfolioSchedulerError):
    """Divergent replay, duplicate identity, or fencing conflict."""


class PortfolioSchedulerSequenceError(PortfolioSchedulerError):
    """Counter corruption, rollback, discontinuity, or missing authority."""


class PortfolioSchedulerLockConflictError(PortfolioSchedulerError):
    """The independent scheduler-store lock is already held."""


class PortfolioSchedulerTransitionError(PortfolioSchedulerError):
    """A phase, receipt, recovery, or finalization transition is invalid."""


class PortfolioSchedulerNotFoundError(PortfolioSchedulerError):
    """Required durable evidence is absent."""


class PortfolioSchedulerPermissionError(PortfolioSchedulerError):
    """Filesystem state or permission could not be confirmed."""


def _raise(error_type: type[PortfolioSchedulerError], code: str) -> None:
    raise error_type(f"portfolio_scheduler:{code}")


def _require_exact_string(value: object, code: str) -> str:
    if type(value) is not str:
        _raise(PortfolioSchedulerInputError, f"{code}_type")
    if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _raise(PortfolioSchedulerInputError, f"{code}_unsafe")
    return value


def _validate_id(value: object, code: str, prefix: str | None = None) -> str:
    text = _require_exact_string(value, code)
    if text != unicodedata.normalize("NFC", text):
        _raise(PortfolioSchedulerSecurityError, f"{code}_normalization")
    if "/" in text or "\\" in text or text in (".", ".."):
        _raise(PortfolioSchedulerSecurityError, f"{code}_path")
    if _SAFE_ID_RE.fullmatch(text) is None:
        _raise(PortfolioSchedulerInputError, f"{code}_format")
    if prefix is not None and not text.startswith(prefix):
        _raise(PortfolioSchedulerInputError, f"{code}_prefix")
    if text.casefold().split(".", 1)[0] in _RESERVED_WINDOWS_NAMES:
        _raise(PortfolioSchedulerSecurityError, f"{code}_reserved")
    return text


def _validate_key(value: object, code: str) -> str:
    text = _require_exact_string(value, code)
    if text != unicodedata.normalize("NFC", text):
        _raise(PortfolioSchedulerSecurityError, f"{code}_normalization")
    if _SAFE_KEY_RE.fullmatch(text) is None:
        _raise(PortfolioSchedulerInputError, f"{code}_format")
    return text


def _validate_timestamp(value: object, code: str) -> str:
    text = _require_exact_string(value, code)
    if _TIMESTAMP_RE.fullmatch(text) is None:
        _raise(PortfolioSchedulerInputError, f"{code}_format")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _raise(PortfolioSchedulerInputError, f"{code}_value")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _raise(PortfolioSchedulerInputError, f"{code}_timezone")
    return text


def _validate_non_bool_int(value: object, code: str, minimum: int) -> int:
    if type(value) is not int:
        _raise(PortfolioSchedulerInputError, f"{code}_type")
    if value < minimum:
        _raise(PortfolioSchedulerInputError, f"{code}_range")
    return value


def _validate_digest(value: object, code: str) -> str:
    text = _require_exact_string(value, code)
    if _SHA256_RE.fullmatch(text) is None:
        _raise(PortfolioSchedulerInputError, f"{code}_format")
    return text


def _validate_selection_reason(value: object) -> str:
    text = _require_exact_string(value, "selection_reason")
    if text != unicodedata.normalize("NFC", text):
        _raise(PortfolioSchedulerSecurityError, "selection_reason_normalization")
    return text


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(value: object, code: str) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return _quote(value)
    _raise(PortfolioSchedulerInputError, f"{code}_scalar_type")


class BusinessPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class QueuePhase(str, enum.Enum):
    QUEUED = "queued"
    SELECTED = "selected"
    DISPATCHED = "dispatched"
    RETIRED = "retired"


class ReceiptPhase(str, enum.Enum):
    SELECTED = "selected"
    DISPATCHED = "dispatched"


class ConflictKeyClass(str, enum.Enum):
    HARD_EXCLUSIVE = "hard_exclusive"
    ADVISORY = "advisory"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConflictKey:
    value: str
    classification: ConflictKeyClass
    canonical: bool

    def __post_init__(self) -> None:
        _validate_key(self.value, "conflict_key")
        if type(self.classification) is not ConflictKeyClass:
            _raise(PortfolioSchedulerInputError, "conflict_classification_type")
        if type(self.canonical) is not bool:
            _raise(PortfolioSchedulerInputError, "conflict_canonical_type")
        if self.classification is ConflictKeyClass.UNKNOWN and self.canonical:
            _raise(PortfolioSchedulerInputError, "unknown_conflict_canonical")


@dataclass(frozen=True, slots=True)
class QueueEntry:
    schema_version: str
    queue_id: str
    task_id: str
    revision: int
    enqueue_sequence: int
    enqueued_at: str
    business_priority: BusinessPriority
    aging_basis_at: str
    worker_kind_request: WorkerKind
    assessment_id: str
    state: QueuePhase
    conflict_keys: tuple[ConflictKey, ...]
    retry_budget_used: int
    content_digest: str
    selection_generation: int

    def __post_init__(self) -> None:
        _validate_queue_entry_fields(self)


@dataclass(frozen=True, slots=True)
class ScheduleReceipt:
    schema_version: str
    receipt_id: str
    queue_id: str
    task_id: str
    revision: int
    enqueue_sequence: int
    selected_at: str
    order_key: tuple[int, int, int, str]
    selection_reason: str
    worker_kind: WorkerKind
    dispatch_event_id: str | None
    phase: ReceiptPhase
    content_digest: str
    selection_generation: int

    def __post_init__(self) -> None:
        _validate_schedule_receipt_fields(self)


def _validate_worker_kind(value: object, code: str) -> None:
    if type(value) is not WorkerKind:
        _raise(PortfolioSchedulerInputError, f"{code}_type")


def _validate_queue_entry_fields(entry: QueueEntry) -> None:
    if entry.schema_version != SCHEMA_VERSION:
        _raise(PortfolioSchedulerInputError, "queue_schema_version")
    _validate_id(entry.queue_id, "queue_id", "Q-")
    _validate_id(entry.task_id, "task_id")
    _validate_non_bool_int(entry.revision, "revision", 1)
    _validate_non_bool_int(entry.enqueue_sequence, "enqueue_sequence", 1)
    _validate_timestamp(entry.enqueued_at, "enqueued_at")
    if type(entry.business_priority) is not BusinessPriority:
        _raise(PortfolioSchedulerInputError, "business_priority_type")
    _validate_timestamp(entry.aging_basis_at, "aging_basis_at")
    _validate_worker_kind(entry.worker_kind_request, "worker_kind_request")
    _validate_id(entry.assessment_id, "assessment_id", "ASM-")
    if type(entry.state) is not QueuePhase:
        _raise(PortfolioSchedulerInputError, "queue_phase_type")
    if type(entry.conflict_keys) is not tuple:
        _raise(PortfolioSchedulerInputError, "conflict_keys_type")
    seen: set[str] = set()
    for conflict_key in entry.conflict_keys:
        if type(conflict_key) is not ConflictKey:
            _raise(PortfolioSchedulerInputError, "conflict_key_type")
        folded = conflict_key.value.casefold()
        if folded in seen:
            _raise(PortfolioSchedulerInputError, "duplicate_conflict_key")
        seen.add(folded)
    _validate_non_bool_int(entry.retry_budget_used, "retry_budget_used", 0)
    _validate_digest(entry.content_digest, "content_digest")
    _validate_non_bool_int(entry.selection_generation, "selection_generation", 1)


def _validate_schedule_receipt_fields(receipt: ScheduleReceipt) -> None:
    if receipt.schema_version != SCHEMA_VERSION:
        _raise(PortfolioSchedulerInputError, "receipt_schema_version")
    _validate_id(receipt.receipt_id, "receipt_id", "SR-")
    _validate_id(receipt.queue_id, "queue_id", "Q-")
    _validate_id(receipt.task_id, "task_id")
    _validate_non_bool_int(receipt.revision, "revision", 1)
    _validate_non_bool_int(receipt.enqueue_sequence, "enqueue_sequence", 1)
    _validate_timestamp(receipt.selected_at, "selected_at")
    if type(receipt.order_key) is not tuple or len(receipt.order_key) != 4:
        _raise(PortfolioSchedulerInputError, "order_key_shape")
    for index in range(3):
        _validate_non_bool_int(receipt.order_key[index], "order_key", 0)
    _validate_id(receipt.order_key[3], "order_key_queue_id", "Q-")
    _validate_selection_reason(receipt.selection_reason)
    _validate_worker_kind(receipt.worker_kind, "worker_kind")
    if receipt.dispatch_event_id is not None:
        _validate_id(receipt.dispatch_event_id, "dispatch_event_id", "EVT-")
    if type(receipt.phase) is not ReceiptPhase:
        _raise(PortfolioSchedulerInputError, "receipt_phase_type")
    if receipt.phase is ReceiptPhase.SELECTED and receipt.dispatch_event_id is not None:
        _raise(PortfolioSchedulerInputError, "selected_dispatch_event")
    if receipt.phase is ReceiptPhase.DISPATCHED and receipt.dispatch_event_id is None:
        _raise(PortfolioSchedulerInputError, "dispatched_event_missing")
    _validate_digest(receipt.content_digest, "content_digest")
    _validate_non_bool_int(receipt.selection_generation, "selection_generation", 1)


def _entry_lines(entry: QueueEntry, include_digest: bool, indent: str = "") -> list[str]:
    lines = [
        f"{indent}schema_version: {_scalar(entry.schema_version, 'schema_version')}",
        f"{indent}queue_id: {_scalar(entry.queue_id, 'queue_id')}",
        f"{indent}task_id: {_scalar(entry.task_id, 'task_id')}",
        f"{indent}revision: {_scalar(entry.revision, 'revision')}",
        f"{indent}enqueue_sequence: {_scalar(entry.enqueue_sequence, 'enqueue_sequence')}",
        f"{indent}enqueued_at: {_scalar(entry.enqueued_at, 'enqueued_at')}",
        f"{indent}business_priority: {_scalar(entry.business_priority.value, 'business_priority')}",
        f"{indent}aging_basis_at: {_scalar(entry.aging_basis_at, 'aging_basis_at')}",
        f"{indent}worker_kind_request: {_scalar(entry.worker_kind_request.value, 'worker_kind_request')}",
        f"{indent}assessment_id: {_scalar(entry.assessment_id, 'assessment_id')}",
        f"{indent}state: {_scalar(entry.state.value, 'state')}",
        f"{indent}conflict_keys:",
    ]
    for conflict_key in entry.conflict_keys:
        lines.extend(
            (
                f"{indent}  - value: {_scalar(conflict_key.value, 'conflict_key')}",
                f"{indent}    classification: {_scalar(conflict_key.classification.value, 'classification')}",
                f"{indent}    canonical: {_scalar(conflict_key.canonical, 'canonical')}",
            )
        )
    lines.extend(
        (
            f"{indent}retry_budget_used: {_scalar(entry.retry_budget_used, 'retry_budget_used')}",
        )
    )
    if include_digest:
        lines.append(f"{indent}content_digest: {_scalar(entry.content_digest, 'content_digest')}")
    lines.append(
        f"{indent}selection_generation: {_scalar(entry.selection_generation, 'selection_generation')}"
    )
    return lines


def _receipt_lines(receipt: ScheduleReceipt, include_digest: bool, indent: str = "") -> list[str]:
    lines = [
        f"{indent}schema_version: {_scalar(receipt.schema_version, 'schema_version')}",
        f"{indent}receipt_id: {_scalar(receipt.receipt_id, 'receipt_id')}",
        f"{indent}queue_id: {_scalar(receipt.queue_id, 'queue_id')}",
        f"{indent}task_id: {_scalar(receipt.task_id, 'task_id')}",
        f"{indent}revision: {_scalar(receipt.revision, 'revision')}",
        f"{indent}enqueue_sequence: {_scalar(receipt.enqueue_sequence, 'enqueue_sequence')}",
        f"{indent}selected_at: {_scalar(receipt.selected_at, 'selected_at')}",
        f"{indent}order_key:",
        f"{indent}  - {_scalar(receipt.order_key[0], 'order_key')}",
        f"{indent}  - {_scalar(receipt.order_key[1], 'order_key')}",
        f"{indent}  - {_scalar(receipt.order_key[2], 'order_key')}",
        f"{indent}  - {_scalar(receipt.order_key[3], 'order_key')}",
        f"{indent}selection_reason: {_scalar(receipt.selection_reason, 'selection_reason')}",
        f"{indent}worker_kind: {_scalar(receipt.worker_kind.value, 'worker_kind')}",
        f"{indent}dispatch_event_id: {_scalar(receipt.dispatch_event_id, 'dispatch_event_id')}",
        f"{indent}phase: {_scalar(receipt.phase.value, 'phase')}",
    ]
    if include_digest:
        lines.append(f"{indent}content_digest: {_scalar(receipt.content_digest, 'content_digest')}")
    lines.append(
        f"{indent}selection_generation: {_scalar(receipt.selection_generation, 'selection_generation')}"
    )
    return lines


def _finish(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _entry_digest(entry: QueueEntry) -> str:
    return "sha256:" + hashlib.sha256(_finish(_entry_lines(entry, False))).hexdigest()


def _receipt_digest(receipt: ScheduleReceipt) -> str:
    return "sha256:" + hashlib.sha256(_finish(_receipt_lines(receipt, False))).hexdigest()


def _ensure_entry_digest(entry: QueueEntry) -> None:
    if entry.content_digest != _entry_digest(entry):
        _raise(PortfolioSchedulerSchemaError, "queue_digest_mismatch")


def _ensure_receipt_digest(receipt: ScheduleReceipt) -> None:
    if receipt.content_digest != _receipt_digest(receipt):
        _raise(PortfolioSchedulerSchemaError, "receipt_digest_mismatch")


def encode_queue_entry(entry: QueueEntry) -> bytes:
    if type(entry) is not QueueEntry:
        _raise(PortfolioSchedulerInputError, "queue_entry_type")
    _validate_queue_entry_fields(entry)
    _ensure_entry_digest(entry)
    return _finish(_entry_lines(entry, True))


def encode_schedule_receipt(receipt: ScheduleReceipt) -> bytes:
    if type(receipt) is not ScheduleReceipt:
        _raise(PortfolioSchedulerInputError, "schedule_receipt_type")
    _validate_schedule_receipt_fields(receipt)
    _ensure_receipt_digest(receipt)
    return _finish(_receipt_lines(receipt, True))


def _decode_document(data: object, code: str) -> list[str]:
    if type(data) is not bytes:
        _raise(PortfolioSchedulerInputError, f"{code}_bytes_type")
    if not data or data.startswith(b"\xef\xbb\xbf"):
        _raise(PortfolioSchedulerSchemaError, f"{code}_document")
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        _raise(PortfolioSchedulerSchemaError, f"{code}_newline")
    if any(byte < 0x20 and byte != 0x0A for byte in data):
        _raise(PortfolioSchedulerSchemaError, f"{code}_control")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _raise(PortfolioSchedulerSchemaError, f"{code}_utf8")
    lines = text[:-1].split("\n")
    if any(not line for line in lines):
        _raise(PortfolioSchedulerSchemaError, f"{code}_blank_line")
    return lines


def _parse_quoted_scalar(token: str, code: str) -> str:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        _raise(PortfolioSchedulerSchemaError, f"{code}_scalar")
    result: list[str] = []
    index = 1
    end = len(token) - 1
    while index < end:
        character = token[index]
        if character == "\\":
            index += 1
            if index >= end or token[index] not in ('"', "\\"):
                _raise(PortfolioSchedulerSchemaError, f"{code}_escape")
            result.append(token[index])
        elif character == '"':
            _raise(PortfolioSchedulerSchemaError, f"{code}_quote")
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            _raise(PortfolioSchedulerSchemaError, f"{code}_control")
        else:
            result.append(character)
        index += 1
    value = "".join(result)
    if _quote(value) != token:
        _raise(PortfolioSchedulerSchemaError, f"{code}_noncanonical")
    return value


def _parse_scalar(token: str, code: str) -> object:
    if token == "null":
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    if token.startswith('"'):
        return _parse_quoted_scalar(token, code)
    if re.fullmatch(r"0|[1-9][0-9]*", token):
        return int(token)
    _raise(PortfolioSchedulerSchemaError, f"{code}_scalar")


def _parse_line_scalar(lines: list[str], cursor: int, indent: str, key: str) -> tuple[object, int]:
    if cursor >= len(lines):
        _raise(PortfolioSchedulerSchemaError, f"missing_{key}")
    prefix = f"{indent}{key}: "
    line = lines[cursor]
    if not line.startswith(prefix):
        _raise(PortfolioSchedulerSchemaError, f"field_order_{key}")
    token = line[len(prefix):]
    if not token:
        _raise(PortfolioSchedulerSchemaError, f"missing_value_{key}")
    return _parse_scalar(token, key), cursor + 1


def _parse_enum(value: object, enum_type: type[enum.Enum], code: str) -> enum.Enum:
    if type(value) is not str:
        _raise(PortfolioSchedulerSchemaError, f"{code}_enum")
    try:
        return enum_type(value)
    except ValueError:
        _raise(PortfolioSchedulerSchemaError, f"{code}_enum")


def _parse_entry_at(lines: list[str], cursor: int, indent: str) -> tuple[QueueEntry, int]:
    values: dict[str, object] = {}
    for key in _QUEUE_FIELDS:
        if key == "conflict_keys":
            if cursor >= len(lines) or lines[cursor] != f"{indent}conflict_keys:":
                _raise(PortfolioSchedulerSchemaError, "field_order_conflict_keys")
            cursor += 1
            conflicts: list[ConflictKey] = []
            item_prefix = f"{indent}  - value: "
            while cursor < len(lines) and lines[cursor].startswith(item_prefix):
                value = _parse_scalar(lines[cursor][len(item_prefix):], "conflict_key")
                cursor += 1
                classification, cursor = _parse_line_scalar(
                    lines, cursor, indent + "    ", "classification"
                )
                canonical, cursor = _parse_line_scalar(
                    lines, cursor, indent + "    ", "canonical"
                )
                if type(value) is not str:
                    _raise(PortfolioSchedulerSchemaError, "conflict_key_value")
                conflicts.append(
                    ConflictKey(
                        value=value,
                        classification=_parse_enum(
                            classification, ConflictKeyClass, "classification"
                        ),
                        canonical=canonical,
                    )
                )
            values[key] = tuple(conflicts)
            continue
        values[key], cursor = _parse_line_scalar(lines, cursor, indent, key)
    if type(values["business_priority"]) is not str:
        _raise(PortfolioSchedulerSchemaError, "business_priority_enum")
    if type(values["worker_kind_request"]) is not str:
        _raise(PortfolioSchedulerSchemaError, "worker_kind_enum")
    if type(values["state"]) is not str:
        _raise(PortfolioSchedulerSchemaError, "state_enum")
    values["business_priority"] = _parse_enum(
        values["business_priority"], BusinessPriority, "business_priority"
    )
    values["worker_kind_request"] = _parse_enum(
        values["worker_kind_request"], WorkerKind, "worker_kind_request"
    )
    values["state"] = _parse_enum(values["state"], QueuePhase, "state")
    try:
        entry = QueueEntry(**values)
    except PortfolioSchedulerSchemaError:
        raise
    except PortfolioSchedulerError:
        _raise(PortfolioSchedulerSchemaError, "queue_entry_fields")
    except (TypeError, ValueError):
        _raise(PortfolioSchedulerSchemaError, "queue_entry_fields")
    return entry, cursor


def decode_queue_entry(data: bytes) -> QueueEntry:
    lines = _decode_document(data, "queue")
    entry, cursor = _parse_entry_at(lines, 0, "")
    if cursor != len(lines):
        _raise(PortfolioSchedulerSchemaError, "queue_unknown_or_extra")
    canonical = encode_queue_entry(entry)
    if canonical != data:
        _raise(PortfolioSchedulerSchemaError, "queue_noncanonical")
    return entry


def _parse_receipt_at(lines: list[str], cursor: int, indent: str) -> tuple[ScheduleReceipt, int]:
    values: dict[str, object] = {}
    for key in _RECEIPT_FIELDS:
        if key == "order_key":
            if cursor >= len(lines) or lines[cursor] != f"{indent}order_key:":
                _raise(PortfolioSchedulerSchemaError, "field_order_order_key")
            cursor += 1
            order_key: list[object] = []
            for index in range(4):
                if cursor >= len(lines):
                    _raise(PortfolioSchedulerSchemaError, "order_key_missing")
                prefix = f"{indent}  - "
                if not lines[cursor].startswith(prefix):
                    _raise(PortfolioSchedulerSchemaError, "order_key_item")
                order_key.append(_parse_scalar(lines[cursor][len(prefix):], "order_key"))
                cursor += 1
            values[key] = tuple(order_key)
            continue
        values[key], cursor = _parse_line_scalar(lines, cursor, indent, key)
    if type(values["worker_kind"]) is not str:
        _raise(PortfolioSchedulerSchemaError, "worker_kind_enum")
    if type(values["phase"]) is not str:
        _raise(PortfolioSchedulerSchemaError, "phase_enum")
    values["worker_kind"] = _parse_enum(values["worker_kind"], WorkerKind, "worker_kind")
    values["phase"] = _parse_enum(values["phase"], ReceiptPhase, "phase")
    try:
        receipt = ScheduleReceipt(**values)
    except PortfolioSchedulerSchemaError:
        raise
    except PortfolioSchedulerError:
        _raise(PortfolioSchedulerSchemaError, "schedule_receipt_fields")
    except (TypeError, ValueError):
        _raise(PortfolioSchedulerSchemaError, "schedule_receipt_fields")
    return receipt, cursor


def decode_schedule_receipt(data: bytes) -> ScheduleReceipt:
    lines = _decode_document(data, "receipt")
    receipt, cursor = _parse_receipt_at(lines, 0, "")
    if cursor != len(lines):
        _raise(PortfolioSchedulerSchemaError, "receipt_unknown_or_extra")
    canonical = encode_schedule_receipt(receipt)
    if canonical != data:
        _raise(PortfolioSchedulerSchemaError, "receipt_noncanonical")
    return receipt


def _validate_project_root(project_root: object) -> Path:
    if not isinstance(project_root, Path):
        _raise(PortfolioSchedulerInputError, "project_root_type")
    if not project_root.is_absolute():
        _raise(PortfolioSchedulerSecurityError, "project_root_relative")
    try:
        if _is_symlink_or_reparse(project_root):
            _raise(PortfolioSchedulerSecurityError, "project_root_link")
        mode = os.lstat(str(project_root)).st_mode
    except FileNotFoundError:
        _raise(PortfolioSchedulerSecurityError, "project_root_missing")
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "project_root_unknown")
    if not stat.S_ISDIR(mode):
        _raise(PortfolioSchedulerSecurityError, "project_root_directory")
    return project_root


def _ensure_directory(path: Path, create: bool) -> None:
    try:
        if _is_symlink_or_reparse(path):
            _raise(PortfolioSchedulerSecurityError, "directory_link")
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        if not create:
            _raise(PortfolioSchedulerNotFoundError, "directory_missing")
        try:
            path.mkdir()
            mode = os.lstat(str(path)).st_mode
        except FileExistsError:
            return _ensure_directory(path, False)
        except OSError:
            _raise(PortfolioSchedulerPermissionError, "directory_create")
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "directory_unknown")
    if not stat.S_ISDIR(mode):
        _raise(PortfolioSchedulerSecurityError, "directory_not_directory")


def _prepare_store(project_root: Path, create: bool) -> Path:
    root = _validate_project_root(project_root)
    current = root
    for component in STORE_RELATIVE.parts:
        current = current / component
        _ensure_directory(current, create)
    store_root = current
    for component in (RECEIPTS_DIRECTORY, TOMBSTONES_DIRECTORY, RESERVATIONS_DIRECTORY):
        _ensure_directory(store_root / component, create)
    return store_root


def _safe_file(path: Path, missing_ok: bool) -> bytes | None:
    try:
        if _is_symlink_or_reparse(path):
            _raise(PortfolioSchedulerSecurityError, "file_link")
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        if missing_ok:
            return None
        _raise(PortfolioSchedulerNotFoundError, "evidence_missing")
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "file_unknown")
    if not stat.S_ISREG(mode):
        _raise(PortfolioSchedulerSecurityError, "file_not_regular")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        _raise(PortfolioSchedulerSecurityError, "file_raced")
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "file_read")


def _atomic_store_bytes(path: Path, content: bytes) -> None:
    try:
        if _is_symlink_or_reparse(path):
            _raise(PortfolioSchedulerSecurityError, "write_file_link")
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "write_file_unknown")
    try:
        _atomic_write_bytes(path, content)
    except PortfolioSchedulerError:
        raise
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "atomic_write")


_WIN32_ERROR_FILE_EXISTS = 80
_WIN32_ERROR_ALREADY_EXISTS = 183
_WIN32_ERROR_ACCESS_DENIED = 5
_WIN32_ERROR_SHARING_VIOLATION = 32
_WIN32_ERROR_LOCK_VIOLATION = 33
_WIN32_ERROR_PERMISSION_CODES = frozenset(
    {
        1,    # ERROR_INVALID_FUNCTION
        3,    # ERROR_PATH_NOT_FOUND
        5,    # ERROR_ACCESS_DENIED
        6,    # ERROR_INVALID_HANDLE
        8,    # ERROR_NOT_ENOUGH_MEMORY
        14,   # ERROR_OUTOFMEMORY
        32,   # ERROR_SHARING_VIOLATION
        33,   # ERROR_LOCK_VIOLATION
        50,   # ERROR_NOT_SUPPORTED
        87,   # ERROR_INVALID_PARAMETER
        111,  # ERROR_BUFFER_OVERFLOW
        120,  # ERROR_CALL_NOT_IMPLEMENTED
        122,  # ERROR_INSUFFICIENT_BUFFER
        123,  # ERROR_INVALID_NAME
        145,  # ERROR_DIR_NOT_EMPTY
        206,  # ERROR_FILENAME_EXCED_RANGE
        267,  # ERROR_DIRECTORY
        995,  # ERROR_OPERATION_ABORTED
    }
)


def _windows_create_lock_handle(lock_path: Path) -> tuple[object, object]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=False)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(lock_path),
        0x40000000,  # GENERIC_WRITE
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        1,            # CREATE_NEW
        0x00000080,   # FILE_ATTRIBUTE_NORMAL
        None,
    )
    error_code = int(kernel32.GetLastError())
    invalid_handle = ctypes.c_void_p(-1).value
    handle_value = handle.value if hasattr(handle, "value") else handle
    if handle_value in (None, invalid_handle):
        if error_code in (_WIN32_ERROR_FILE_EXISTS, _WIN32_ERROR_ALREADY_EXISTS):
            _raise(PortfolioSchedulerLockConflictError, "lock_contention")
        if error_code in _WIN32_ERROR_PERMISSION_CODES:
            _raise(PortfolioSchedulerPermissionError, "lock_create")
        _raise(PortfolioSchedulerError, "win32_lock_create_unknown")
    return kernel32, handle


def _windows_write_and_flush(kernel32: object, handle: object, token: bytes) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(token)
    written = wintypes.DWORD(0)
    if not kernel32.WriteFile(handle, buffer, len(token), ctypes.byref(written), None):
        return False
    if int(written.value) != len(token):
        return False
    return bool(kernel32.FlushFileBuffers(handle))


def _windows_close_handle(kernel32: object, handle: object) -> bool:
    try:
        return bool(kernel32.CloseHandle(handle))
    except OSError:
        return False


def _prove_and_remove_owned_lock(lock_path: Path, token: bytes) -> bool:
    try:
        if _is_symlink_or_reparse(lock_path):
            return False
        mode = os.lstat(str(lock_path)).st_mode
        if not stat.S_ISREG(mode):
            return False
        if lock_path.read_bytes() != token:
            return False
        lock_path.unlink()
        _fsync_parent_directory(lock_path)
        return True
    except OSError:
        return False


@contextmanager
def _store_lock(project_root: Path) -> Iterator[None]:
    _prepare_store(project_root, True)
    lock_path = project_root / STORE_LOCK_FILENAME
    try:
        if _is_symlink_or_reparse(lock_path):
            _raise(PortfolioSchedulerSecurityError, "lock_link")
    except OSError:
        _raise(PortfolioSchedulerPermissionError, "lock_unknown")
    token = secrets.token_hex(16)
    created = False
    win_kernel32: object | None = None
    win_handle: object | None = None
    if os.name == "nt":
        win_kernel32, win_handle = _windows_create_lock_handle(lock_path)
        created = True
        if not _windows_write_and_flush(win_kernel32, win_handle, token.encode("ascii")):
            closed = _windows_close_handle(win_kernel32, win_handle)
            if closed and _prove_and_remove_owned_lock(lock_path, token.encode("ascii")):
                pass
            raise PortfolioSchedulerPermissionError("portfolio_scheduler:lock_initialize")
    else:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
        except FileExistsError:
            _raise(PortfolioSchedulerLockConflictError, "lock_contention")
        except OSError:
            _raise(PortfolioSchedulerPermissionError, "lock_create")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token.encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_parent_directory(lock_path)
        except BaseException:
            if created:
                try:
                    if not _is_symlink_or_reparse(lock_path):
                        lock_path.unlink()
                        _fsync_parent_directory(lock_path)
                except OSError:
                    pass
            raise PortfolioSchedulerPermissionError("portfolio_scheduler:lock_initialize")
    body_exception: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_exception = exc
    release_error: PortfolioSchedulerError | None = None
    try:
        if os.name == "nt":
            if not _windows_close_handle(win_kernel32, win_handle):
                release_error = PortfolioSchedulerPermissionError(
                    "portfolio_scheduler:lock_release"
                )
            elif _is_symlink_or_reparse(lock_path):
                release_error = PortfolioSchedulerSecurityError("portfolio_scheduler:lock_replaced")
            else:
                stored = _safe_file(lock_path, False)
                if stored != token.encode("ascii"):
                    release_error = PortfolioSchedulerLockConflictError("portfolio_scheduler:lock_token")
                else:
                    try:
                        lock_path.unlink()
                        _fsync_parent_directory(lock_path)
                    except OSError:
                        release_error = PortfolioSchedulerPermissionError(
                            "portfolio_scheduler:lock_release"
                        )
        elif _is_symlink_or_reparse(lock_path):
            release_error = PortfolioSchedulerSecurityError("portfolio_scheduler:lock_replaced")
        else:
            stored = _safe_file(lock_path, False)
            if stored != token.encode("ascii"):
                release_error = PortfolioSchedulerLockConflictError("portfolio_scheduler:lock_token")
            else:
                try:
                    lock_path.unlink()
                    _fsync_parent_directory(lock_path)
                except OSError:
                    release_error = PortfolioSchedulerPermissionError(
                        "portfolio_scheduler:lock_release"
                    )
    except PortfolioSchedulerError as exc:
        release_error = exc
    if body_exception is not None:
        raise body_exception
    if release_error is not None:
        raise release_error


def _sequence_lines(last_issued: int, reserved: tuple[int, ...]) -> list[str]:
    lines = [
        f"schema_version: {_quote(SCHEMA_VERSION)}",
        f"last_issued_enqueue_sequence: {last_issued}",
        "reserved_sequences:",
    ]
    lines.extend(f"  - {sequence}" for sequence in reserved)
    return lines


def _encode_sequence(last_issued: int, reserved: tuple[int, ...]) -> bytes:
    _validate_non_bool_int(last_issued, "last_issued_enqueue_sequence", 0)
    if reserved != tuple(range(1, last_issued + 1)):
        _raise(PortfolioSchedulerSequenceError, "sequence_not_contiguous")
    return _finish(_sequence_lines(last_issued, reserved))


def _decode_sequence(data: bytes) -> tuple[int, tuple[int, ...]]:
    lines = _decode_document(data, "sequence")
    if len(lines) < 3:
        _raise(PortfolioSchedulerSchemaError, "sequence_fields")
    expected_schema = f"schema_version: {_quote(SCHEMA_VERSION)}"
    if lines[0] != expected_schema:
        _raise(PortfolioSchedulerSchemaError, "sequence_schema")
    prefix = "last_issued_enqueue_sequence: "
    if not lines[1].startswith(prefix):
        _raise(PortfolioSchedulerSchemaError, "sequence_counter")
    value = _parse_scalar(lines[1][len(prefix):], "last_issued_enqueue_sequence")
    if type(value) is not int:
        _raise(PortfolioSchedulerSequenceError, "counter_type")
    if value < 0:
        _raise(PortfolioSchedulerSequenceError, "counter_negative")
    if lines[2] != "reserved_sequences:":
        _raise(PortfolioSchedulerSchemaError, "sequence_reserved")
    reserved: list[int] = []
    for line in lines[3:]:
        prefix = "  - "
        if not line.startswith(prefix):
            _raise(PortfolioSchedulerSchemaError, "sequence_extra")
        parsed = _parse_scalar(line[len(prefix):], "reserved_sequence")
        if type(parsed) is not int or parsed < 1:
            _raise(PortfolioSchedulerSequenceError, "reserved_sequence_value")
        reserved.append(parsed)
    result = (value, tuple(reserved))
    if _encode_sequence(*result) != data:
        _raise(PortfolioSchedulerSequenceError, "sequence_noncanonical")
    return result


def _validate_queue_snapshot(entries: tuple[QueueEntry, ...]) -> None:
    sequences: set[int] = set()
    queue_ids: set[str] = set()
    task_revisions: set[tuple[str, int]] = set()
    previous = 0
    for entry in entries:
        _ensure_entry_digest(entry)
        if entry.enqueue_sequence <= previous:
            _raise(PortfolioSchedulerSequenceError, "queue_sequence_order")
        previous = entry.enqueue_sequence
        if entry.enqueue_sequence in sequences:
            _raise(PortfolioSchedulerSequenceError, "queue_sequence_duplicate")
        if entry.queue_id in queue_ids:
            _raise(PortfolioSchedulerConflictError, "queue_id_duplicate")
        identity = (entry.task_id, entry.revision)
        if identity in task_revisions:
            _raise(PortfolioSchedulerConflictError, "task_revision_duplicate")
        sequences.add(entry.enqueue_sequence)
        queue_ids.add(entry.queue_id)
        task_revisions.add(identity)


def _encode_queue_snapshot(entries: tuple[QueueEntry, ...]) -> bytes:
    _validate_queue_snapshot(entries)
    lines = [f"schema_version: {_quote(SCHEMA_VERSION)}", "entries:"]
    for entry in entries:
        lines.append("  - entry:")
        lines.extend(_entry_lines(entry, True, "    "))
    return _finish(lines)


def _decode_queue_snapshot(data: bytes) -> tuple[QueueEntry, ...]:
    lines = _decode_document(data, "queue_snapshot")
    if len(lines) < 2 or lines[0] != f"schema_version: {_quote(SCHEMA_VERSION)}":
        _raise(PortfolioSchedulerSchemaError, "queue_snapshot_schema")
    if lines[1] != "entries:":
        _raise(PortfolioSchedulerSchemaError, "queue_snapshot_entries")
    entries: list[QueueEntry] = []
    cursor = 2
    while cursor < len(lines):
        if lines[cursor] != "  - entry:":
            _raise(PortfolioSchedulerSchemaError, "queue_snapshot_item")
        entry, cursor = _parse_entry_at(lines, cursor + 1, "    ")
        entries.append(entry)
    result = tuple(entries)
    _validate_queue_snapshot(result)
    if _encode_queue_snapshot(result) != data:
        _raise(PortfolioSchedulerSchemaError, "queue_snapshot_noncanonical")
    return result


def _tombstone_lines(tombstone: "_Tombstone") -> list[str]:
    return [
        f"schema_version: {_quote(SCHEMA_VERSION)}",
        f"kind: {_quote(tombstone.kind)}",
        f"receipt_id: {_quote(tombstone.receipt_id)}",
        f"queue_id: {_quote(tombstone.queue_id)}",
        f"selection_generation: {tombstone.selection_generation}",
        f"next_selection_generation: {_scalar(tombstone.next_selection_generation, 'next_selection_generation')}",
        f"receipt_digest: {_quote(tombstone.receipt_digest)}",
        f"created_at: {_quote(tombstone.created_at)}",
    ]


@dataclass(frozen=True, slots=True)
class _Tombstone:
    kind: str
    receipt_id: str
    queue_id: str
    selection_generation: int
    next_selection_generation: int | None
    receipt_digest: str
    created_at: str


def _validate_tombstone(tombstone: _Tombstone) -> None:
    if tombstone.kind not in ("finalized", "selected_recovery"):
        _raise(PortfolioSchedulerSchemaError, "tombstone_kind")
    _validate_id(tombstone.receipt_id, "tombstone_receipt", "SR-")
    _validate_id(tombstone.queue_id, "tombstone_queue", "Q-")
    _validate_non_bool_int(tombstone.selection_generation, "tombstone_generation", 1)
    if tombstone.kind == "finalized":
        if tombstone.next_selection_generation is not None:
            _raise(PortfolioSchedulerSchemaError, "finalized_next_generation")
    else:
        if tombstone.next_selection_generation != tombstone.selection_generation + 1:
            _raise(PortfolioSchedulerSchemaError, "recovery_next_generation")
    if tombstone.next_selection_generation is not None:
        _validate_non_bool_int(tombstone.next_selection_generation, "next_generation", 1)
    _validate_digest(tombstone.receipt_digest, "tombstone_digest")
    _validate_timestamp(tombstone.created_at, "tombstone_created_at")


def _encode_tombstone(tombstone: _Tombstone) -> bytes:
    _validate_tombstone(tombstone)
    return _finish(_tombstone_lines(tombstone))


def _decode_tombstone(data: bytes) -> _Tombstone:
    lines = _decode_document(data, "tombstone")
    keys = (
        "schema_version",
        "kind",
        "receipt_id",
        "queue_id",
        "selection_generation",
        "next_selection_generation",
        "receipt_digest",
        "created_at",
    )
    values: list[object] = []
    cursor = 0
    for key in keys:
        value, cursor = _parse_line_scalar(lines, cursor, "", key)
        values.append(value)
    if cursor != len(lines) or values[0] != SCHEMA_VERSION:
        _raise(PortfolioSchedulerSchemaError, "tombstone_schema")
    if type(values[1]) is not str or type(values[2]) is not str or type(values[3]) is not str:
        _raise(PortfolioSchedulerSchemaError, "tombstone_identity")
    if type(values[4]) is not int:
        _raise(PortfolioSchedulerSchemaError, "tombstone_generation")
    if values[5] is not None and type(values[5]) is not int:
        _raise(PortfolioSchedulerSchemaError, "next_generation_type")
    if type(values[6]) is not str or type(values[7]) is not str:
        _raise(PortfolioSchedulerSchemaError, "tombstone_value")
    tombstone = _Tombstone(
        kind=values[1],
        receipt_id=values[2],
        queue_id=values[3],
        selection_generation=values[4],
        next_selection_generation=values[5],
        receipt_digest=values[6],
        created_at=values[7],
    )
    _validate_tombstone(tombstone)
    if _encode_tombstone(tombstone) != data:
        _raise(PortfolioSchedulerSchemaError, "tombstone_noncanonical")
    return tombstone


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _entry_path(store_root: Path) -> Path:
    return store_root / QUEUE_FILENAME


def _sequence_path(store_root: Path) -> Path:
    return store_root / SEQUENCE_FILENAME


def _receipt_path(store_root: Path, receipt_id: str) -> Path:
    _validate_id(receipt_id, "receipt_id", "SR-")
    return store_root / RECEIPTS_DIRECTORY / f"{receipt_id}.yaml"


def _tombstone_path(store_root: Path, receipt_id: str) -> Path:
    _validate_id(receipt_id, "receipt_id", "SR-")
    return store_root / TOMBSTONES_DIRECTORY / f"{receipt_id}.yaml"


def _reservation_path(
    store_root: Path, queue_id: str, selection_generation: int, create: bool = True
) -> Path:
    _validate_id(queue_id, "queue_id", "Q-")
    _validate_non_bool_int(selection_generation, "selection_generation", 1)
    directory = store_root / RESERVATIONS_DIRECTORY / queue_id
    _ensure_directory(directory, create)
    return directory / f"g{selection_generation}.yaml"


def _load_queue(store_root: Path) -> tuple[QueueEntry, ...]:
    raw = _safe_file(_entry_path(store_root), True)
    if raw is None:
        return ()
    return _decode_queue_snapshot(raw)


def _load_sequence(store_root: Path, entries: tuple[QueueEntry, ...]) -> tuple[int, tuple[int, ...]]:
    raw = _safe_file(_sequence_path(store_root), True)
    if raw is None:
        if entries:
            _raise(PortfolioSchedulerSequenceError, "counter_missing_with_queue")
        return 0, ()
    last_issued, reserved = _decode_sequence(raw)
    entry_sequences = {entry.enqueue_sequence for entry in entries}
    if any(sequence > last_issued for sequence in entry_sequences):
        _raise(PortfolioSchedulerSequenceError, "queue_sequence_above_counter")
    if not entry_sequences.issubset(set(reserved)):
        _raise(PortfolioSchedulerSequenceError, "queue_sequence_unreserved")
    return last_issued, reserved


def _load_state(store_root: Path) -> tuple[tuple[QueueEntry, ...], tuple[int, tuple[int, ...]]]:
    entries = _load_queue(store_root)
    return entries, _load_sequence(store_root, entries)


def _write_queue(store_root: Path, entries: tuple[QueueEntry, ...]) -> None:
    _atomic_store_bytes(_entry_path(store_root), _encode_queue_snapshot(entries))


def _write_sequence(store_root: Path, last_issued: int, reserved: tuple[int, ...]) -> None:
    _atomic_store_bytes(_sequence_path(store_root), _encode_sequence(last_issued, reserved))


def _replace_entry(entry: QueueEntry, **changes: object) -> QueueEntry:
    candidate = replace(entry, **changes)
    return replace(candidate, content_digest=_entry_digest(candidate))


def _replace_receipt(receipt: ScheduleReceipt, **changes: object) -> ScheduleReceipt:
    candidate = replace(receipt, **changes)
    return replace(candidate, content_digest=_receipt_digest(candidate))


def _validate_expected_generation(value: object) -> int:
    return _validate_non_bool_int(value, "expected_generation", 1)


def _validate_event_id(value: object) -> str:
    return _validate_id(value, "dispatch_event_id", "EVT-")


def _find_queue_entry(entries: tuple[QueueEntry, ...], queue_id: str) -> QueueEntry:
    for entry in entries:
        if entry.queue_id == queue_id:
            return entry
    _raise(PortfolioSchedulerNotFoundError, "queue_entry_missing")


def _validate_receipt_binding(receipt: ScheduleReceipt, entry: QueueEntry) -> None:
    if (
        receipt.queue_id != entry.queue_id
        or receipt.task_id != entry.task_id
        or receipt.revision != entry.revision
        or receipt.enqueue_sequence != entry.enqueue_sequence
        or receipt.selection_generation != entry.selection_generation
        or receipt.order_key[2] != entry.enqueue_sequence
        or receipt.order_key[3] != entry.queue_id
        or receipt.worker_kind is not entry.worker_kind_request
    ):
        _raise(PortfolioSchedulerConflictError, "receipt_queue_binding")
    if receipt.phase is ReceiptPhase.SELECTED and entry.state is not QueuePhase.SELECTED:
        _raise(PortfolioSchedulerTransitionError, "selected_receipt_stale")
    if receipt.phase is ReceiptPhase.DISPATCHED and entry.state not in (
        QueuePhase.DISPATCHED,
        QueuePhase.RETIRED,
    ):
        _raise(PortfolioSchedulerTransitionError, "dispatched_receipt_state")


def _read_tombstone(store_root: Path, receipt_id: str) -> _Tombstone | None:
    raw = _safe_file(_tombstone_path(store_root, receipt_id), True)
    return None if raw is None else _decode_tombstone(raw)


def _read_receipt_file(store_root: Path, receipt_id: str) -> ScheduleReceipt | None:
    raw = _safe_file(_receipt_path(store_root, receipt_id), True)
    return None if raw is None else decode_schedule_receipt(raw)


def _validate_tombstone_for_receipt(tombstone: _Tombstone, receipt: ScheduleReceipt) -> None:
    if (
        tombstone.receipt_id != receipt.receipt_id
        or tombstone.queue_id != receipt.queue_id
        or tombstone.selection_generation != receipt.selection_generation
        or tombstone.receipt_digest != receipt.content_digest
    ):
        _raise(PortfolioSchedulerConflictError, "tombstone_receipt_mismatch")


class PortfolioSchedulerStore:
    """Independent, short-critical-section durable evidence store."""

    def __init__(self, project_root: Path | None = None) -> None:
        if project_root is not None:
            _validate_project_root(project_root)
        self._project_root = project_root

    def _root(self, project_root: Path | None) -> Path:
        root = self._project_root if project_root is None else project_root
        if root is None:
            _raise(PortfolioSchedulerInputError, "project_root_missing")
        return _validate_project_root(root)

    @contextmanager
    def _locked(self, project_root: Path | None = None) -> Iterator[Path]:
        root = self._root(project_root)
        with _store_lock(root):
            yield _prepare_store(root, True)

    def reserve_enqueue_sequence(self, project_root: Path | None = None) -> int:
        with self._locked(project_root) as store_root:
            entries = _load_queue(store_root)
            last_issued, reserved = _load_sequence(store_root, entries)
            next_sequence = last_issued + 1
            _write_sequence(store_root, next_sequence, reserved + (next_sequence,))
            return next_sequence

    def reserve_next_enqueue_sequence(self, project_root: Path | None = None) -> int:
        return self.reserve_enqueue_sequence(project_root)

    def create_queue_entry(
        self, entry: QueueEntry, project_root: Path | None = None
    ) -> QueueEntry:
        if type(entry) is not QueueEntry:
            _raise(PortfolioSchedulerInputError, "queue_entry_type")
        _validate_queue_entry_fields(entry)
        _ensure_entry_digest(entry)
        if entry.state is not QueuePhase.QUEUED:
            _raise(PortfolioSchedulerTransitionError, "queue_create_phase")
        with self._locked(project_root) as store_root:
            entries, (last_issued, reserved) = _load_state(store_root)
            if entry.enqueue_sequence > last_issued or entry.enqueue_sequence not in reserved:
                _raise(PortfolioSchedulerSequenceError, "enqueue_sequence_unreserved")
            encoded = encode_queue_entry(entry)
            for existing in entries:
                if existing.queue_id == entry.queue_id:
                    if encode_queue_entry(existing) == encoded:
                        return existing
                    _raise(PortfolioSchedulerConflictError, "queue_divergent_replay")
                if (existing.task_id, existing.revision) == (entry.task_id, entry.revision):
                    _raise(PortfolioSchedulerConflictError, "task_revision_conflict")
                if existing.enqueue_sequence == entry.enqueue_sequence:
                    _raise(PortfolioSchedulerConflictError, "sequence_conflict")
            updated = tuple(sorted(entries + (entry,), key=lambda item: item.enqueue_sequence))
            _write_queue(store_root, updated)
            return entry

    def read_queue_entry(
        self, queue_id: str, project_root: Path | None = None
    ) -> QueueEntry:
        _validate_id(queue_id, "queue_id", "Q-")
        root = self._root(project_root)
        store_root = _prepare_store(root, False)
        entries, _ = _load_state(store_root)
        return _find_queue_entry(entries, queue_id)

    def enumerate_queue_snapshot(self, project_root: Path | None = None) -> tuple[QueueEntry, ...]:
        root = self._root(project_root)
        store_root = _prepare_store(root, False)
        entries, _ = _load_state(store_root)
        return entries

    def advance_queue_phase(
        self,
        queue_id: str,
        expected_generation: int,
        new_state: QueuePhase,
        dispatch_event_id: str | None = None,
        project_root: Path | None = None,
    ) -> QueueEntry:
        _validate_id(queue_id, "queue_id", "Q-")
        generation = _validate_expected_generation(expected_generation)
        if type(new_state) is not QueuePhase:
            _raise(PortfolioSchedulerInputError, "queue_phase_type")
        if dispatch_event_id is not None:
            _validate_event_id(dispatch_event_id)
        with self._locked(project_root) as store_root:
            entries, _ = _load_state(store_root)
            current = _find_queue_entry(entries, queue_id)
            if current.selection_generation != generation:
                _raise(PortfolioSchedulerConflictError, "queue_generation_stale")
            if current.state is new_state:
                if new_state is QueuePhase.DISPATCHED and dispatch_event_id is None:
                    _raise(PortfolioSchedulerTransitionError, "dispatch_event_missing")
                if new_state is not QueuePhase.DISPATCHED and dispatch_event_id is not None:
                    _raise(PortfolioSchedulerTransitionError, "unexpected_dispatch_event")
                return current
            allowed = {
                QueuePhase.QUEUED: QueuePhase.SELECTED,
                QueuePhase.SELECTED: QueuePhase.DISPATCHED,
                QueuePhase.DISPATCHED: QueuePhase.RETIRED,
            }
            if allowed.get(current.state) is not new_state:
                _raise(PortfolioSchedulerTransitionError, "queue_phase_transition")
            if new_state is QueuePhase.DISPATCHED:
                if dispatch_event_id is None:
                    _raise(PortfolioSchedulerTransitionError, "dispatch_event_missing")
            elif dispatch_event_id is not None:
                _raise(PortfolioSchedulerTransitionError, "unexpected_dispatch_event")
            updated_entry = _replace_entry(current, state=new_state)
            updated = tuple(updated_entry if item.queue_id == queue_id else item for item in entries)
            _write_queue(store_root, updated)
            return updated_entry

    def retire_queue_entry(
        self, queue_id: str, expected_generation: int, project_root: Path | None = None
    ) -> QueueEntry:
        return self.advance_queue_phase(
            queue_id, expected_generation, QueuePhase.RETIRED, project_root=project_root
        )

    def create_schedule_receipt(
        self, receipt: ScheduleReceipt, project_root: Path | None = None
    ) -> ScheduleReceipt:
        if type(receipt) is not ScheduleReceipt:
            _raise(PortfolioSchedulerInputError, "schedule_receipt_type")
        _validate_schedule_receipt_fields(receipt)
        _ensure_receipt_digest(receipt)
        if receipt.phase is not ReceiptPhase.SELECTED:
            _raise(PortfolioSchedulerTransitionError, "receipt_create_phase")
        with self._locked(project_root) as store_root:
            entries, _ = _load_state(store_root)
            entry = _find_queue_entry(entries, receipt.queue_id)
            _validate_receipt_binding(receipt, entry)
            tombstone = _read_tombstone(store_root, receipt.receipt_id)
            if tombstone is not None:
                _raise(PortfolioSchedulerConflictError, "receipt_tombstoned")
            encoded = encode_schedule_receipt(receipt)
            existing = _read_receipt_file(store_root, receipt.receipt_id)
            if existing is not None:
                if encode_schedule_receipt(existing) != encoded:
                    _raise(PortfolioSchedulerConflictError, "receipt_divergent_replay")
                reservation = _safe_file(
                    _reservation_path(store_root, receipt.queue_id, receipt.selection_generation),
                    True,
                )
                if reservation is not None and reservation != encoded:
                    _raise(PortfolioSchedulerConflictError, "reservation_divergent")
                if reservation is None:
                    _atomic_store_bytes(
                        _reservation_path(store_root, receipt.queue_id, receipt.selection_generation),
                        encoded,
                    )
                return existing
            reservation_path = _reservation_path(
                store_root, receipt.queue_id, receipt.selection_generation
            )
            reservation = _safe_file(reservation_path, True)
            if reservation is not None and reservation != encoded:
                _raise(PortfolioSchedulerConflictError, "reservation_divergent")
            _atomic_store_bytes(_receipt_path(store_root, receipt.receipt_id), encoded)
            if reservation is None:
                _atomic_store_bytes(reservation_path, encoded)
            return receipt

    def reserve_schedule_receipt(
        self, receipt: ScheduleReceipt, project_root: Path | None = None
    ) -> ScheduleReceipt:
        return self.create_schedule_receipt(receipt, project_root)

    def read_schedule_receipt(
        self, receipt_id: str, project_root: Path | None = None
    ) -> ScheduleReceipt:
        _validate_id(receipt_id, "receipt_id", "SR-")
        root = self._root(project_root)
        store_root = _prepare_store(root, False)
        receipt = _read_receipt_file(store_root, receipt_id)
        if receipt is None:
            _raise(PortfolioSchedulerNotFoundError, "receipt_missing")
        tombstone = _read_tombstone(store_root, receipt_id)
        if tombstone is not None:
            _validate_tombstone_for_receipt(tombstone, receipt)
            if tombstone.kind == "selected_recovery":
                _raise(PortfolioSchedulerTransitionError, "receipt_recovered")
        entries, _ = _load_state(store_root)
        entry = _find_queue_entry(entries, receipt.queue_id)
        _validate_receipt_binding(receipt, entry)
        return receipt

    def advance_schedule_receipt(
        self,
        receipt_id: str,
        expected_generation: int,
        new_phase: ReceiptPhase,
        dispatch_event_id: str | None = None,
        project_root: Path | None = None,
    ) -> ScheduleReceipt:
        _validate_id(receipt_id, "receipt_id", "SR-")
        generation = _validate_expected_generation(expected_generation)
        if type(new_phase) is not ReceiptPhase:
            _raise(PortfolioSchedulerInputError, "receipt_phase_type")
        if dispatch_event_id is not None:
            _validate_event_id(dispatch_event_id)
        with self._locked(project_root) as store_root:
            receipt = _read_receipt_file(store_root, receipt_id)
            if receipt is None:
                _raise(PortfolioSchedulerNotFoundError, "receipt_missing")
            tombstone = _read_tombstone(store_root, receipt_id)
            if tombstone is not None:
                _validate_tombstone_for_receipt(tombstone, receipt)
                _raise(PortfolioSchedulerTransitionError, "receipt_finalized_or_recovered")
            if receipt.selection_generation != generation:
                _raise(PortfolioSchedulerConflictError, "receipt_generation_stale")
            entries, _ = _load_state(store_root)
            entry = _find_queue_entry(entries, receipt.queue_id)
            _validate_receipt_binding(receipt, entry)
            if receipt.phase is new_phase:
                if new_phase is ReceiptPhase.DISPATCHED and receipt.dispatch_event_id != dispatch_event_id:
                    _raise(PortfolioSchedulerConflictError, "receipt_event_replay")
                if new_phase is ReceiptPhase.SELECTED and dispatch_event_id is not None:
                    _raise(PortfolioSchedulerConflictError, "receipt_event_replay")
                return receipt
            if receipt.phase is not ReceiptPhase.SELECTED or new_phase is not ReceiptPhase.DISPATCHED:
                _raise(PortfolioSchedulerTransitionError, "receipt_phase_transition")
            if dispatch_event_id is None:
                _raise(PortfolioSchedulerTransitionError, "dispatch_event_missing")
            updated = _replace_receipt(
                receipt, phase=ReceiptPhase.DISPATCHED, dispatch_event_id=dispatch_event_id
            )
            _atomic_store_bytes(_receipt_path(store_root, receipt_id), encode_schedule_receipt(updated))
            reservation_path = _reservation_path(store_root, receipt.queue_id, receipt.selection_generation)
            reservation = _safe_file(reservation_path, True)
            if reservation is not None and reservation != encode_schedule_receipt(receipt):
                _raise(PortfolioSchedulerConflictError, "reservation_divergent")
            return updated

    def finalize_schedule_receipt(
        self, receipt_id: str, project_root: Path | None = None
    ) -> ScheduleReceipt:
        _validate_id(receipt_id, "receipt_id", "SR-")
        with self._locked(project_root) as store_root:
            receipt = _read_receipt_file(store_root, receipt_id)
            if receipt is None:
                _raise(PortfolioSchedulerNotFoundError, "receipt_missing")
            entries, _ = _load_state(store_root)
            entry = _find_queue_entry(entries, receipt.queue_id)
            _validate_receipt_binding(receipt, entry)
            if receipt.phase is not ReceiptPhase.DISPATCHED:
                _raise(PortfolioSchedulerTransitionError, "receipt_not_dispatched")
            tombstone = _read_tombstone(store_root, receipt_id)
            if tombstone is not None:
                _validate_tombstone_for_receipt(tombstone, receipt)
                if tombstone.kind == "finalized":
                    return receipt
                _raise(PortfolioSchedulerTransitionError, "receipt_recovered")
            tombstone = _Tombstone(
                kind="finalized",
                receipt_id=receipt.receipt_id,
                queue_id=receipt.queue_id,
                selection_generation=receipt.selection_generation,
                next_selection_generation=None,
                receipt_digest=receipt.content_digest,
                created_at=_utc_now(),
            )
            _atomic_store_bytes(_tombstone_path(store_root, receipt_id), _encode_tombstone(tombstone))
            return receipt

    def enumerate_validated_receipts(
        self, project_root: Path | None = None
    ) -> tuple[ScheduleReceipt, ...]:
        root = self._root(project_root)
        store_root = _prepare_store(root, False)
        entries, _ = _load_state(store_root)
        receipts_dir = store_root / RECEIPTS_DIRECTORY
        try:
            paths = sorted(receipts_dir.iterdir(), key=lambda path: path.name)
        except OSError:
            _raise(PortfolioSchedulerPermissionError, "receipts_enumerate")
        result: list[ScheduleReceipt] = []
        names: set[str] = set()
        for path in paths:
            if path.suffix != ".yaml":
                _raise(PortfolioSchedulerSchemaError, "receipt_unknown_file")
            receipt_id = path.stem
            _validate_id(receipt_id, "receipt_id", "SR-")
            tombstone = _read_tombstone(store_root, receipt_id)
            if tombstone is not None and tombstone.kind == "selected_recovery":
                receipt = _read_receipt_file(store_root, receipt_id)
                if receipt is None:
                    _raise(PortfolioSchedulerNotFoundError, "receipt_missing")
                _validate_tombstone_for_receipt(tombstone, receipt)
                if receipt.phase is not ReceiptPhase.SELECTED:
                    _raise(PortfolioSchedulerSchemaError, "recovery_receipt_phase")
                names.add(receipt_id)
                continue
            else:
                receipt = self.read_schedule_receipt(receipt_id, root)
                entry = _find_queue_entry(entries, receipt.queue_id)
                _validate_receipt_binding(receipt, entry)
            if receipt_id in names:
                _raise(PortfolioSchedulerConflictError, "receipt_duplicate")
            names.add(receipt_id)
            result.append(receipt)
        tombstones_dir = store_root / TOMBSTONES_DIRECTORY
        try:
            tombstone_paths = sorted(tombstones_dir.iterdir(), key=lambda path: path.name)
        except OSError:
            _raise(PortfolioSchedulerPermissionError, "tombstones_enumerate")
        for path in tombstone_paths:
            if path.suffix != ".yaml":
                _raise(PortfolioSchedulerSchemaError, "tombstone_unknown_file")
            tombstone_id = path.stem
            _validate_id(tombstone_id, "receipt_id", "SR-")
            if tombstone_id not in names:
                _raise(PortfolioSchedulerConflictError, "orphan_tombstone")
            tombstone = _read_tombstone(store_root, tombstone_id)
            if tombstone is None:
                _raise(PortfolioSchedulerNotFoundError, "tombstone_missing")
        return tuple(result)

    def recover_selected_without_dispatch(
        self,
        queue_id: str,
        expected_generation: int,
        receipt_id: str,
        project_root: Path | None = None,
    ) -> QueueEntry:
        _validate_id(queue_id, "queue_id", "Q-")
        generation = _validate_expected_generation(expected_generation)
        _validate_id(receipt_id, "receipt_id", "SR-")
        with self._locked(project_root) as store_root:
            entries, _ = _load_state(store_root)
            current = _find_queue_entry(entries, queue_id)
            tombstone = _read_tombstone(store_root, receipt_id)
            if tombstone is not None:
                if (
                    tombstone.kind != "selected_recovery"
                    or tombstone.queue_id != queue_id
                    or tombstone.selection_generation != generation
                ):
                    _raise(PortfolioSchedulerConflictError, "recovery_tombstone_conflict")
                receipt = _read_receipt_file(store_root, receipt_id)
                if receipt is None or receipt.phase is not ReceiptPhase.SELECTED:
                    _raise(PortfolioSchedulerTransitionError, "recovery_receipt")
                _validate_tombstone_for_receipt(tombstone, receipt)
                if receipt.queue_id != queue_id or receipt.selection_generation != generation:
                    _raise(PortfolioSchedulerConflictError, "recovery_receipt_binding")
                if current.selection_generation == tombstone.next_selection_generation and current.state is QueuePhase.QUEUED:
                    return current
                if current.selection_generation != generation or current.state is not QueuePhase.SELECTED:
                    _raise(PortfolioSchedulerTransitionError, "recovery_incomplete_state")
                _validate_receipt_binding(receipt, current)
            else:
                if current.selection_generation != generation or current.state is not QueuePhase.SELECTED:
                    _raise(PortfolioSchedulerTransitionError, "recovery_state")
                receipt = _read_receipt_file(store_root, receipt_id)
                if receipt is None or receipt.phase is not ReceiptPhase.SELECTED:
                    _raise(PortfolioSchedulerTransitionError, "recovery_receipt")
                _validate_receipt_binding(receipt, current)
                tombstone = _Tombstone(
                    kind="selected_recovery",
                    receipt_id=receipt_id,
                    queue_id=queue_id,
                    selection_generation=generation,
                    next_selection_generation=generation + 1,
                    receipt_digest=receipt.content_digest,
                    created_at=_utc_now(),
                )
                _atomic_store_bytes(_tombstone_path(store_root, receipt_id), _encode_tombstone(tombstone))
            updated_entry = _replace_entry(
                current, state=QueuePhase.QUEUED, selection_generation=generation + 1
            )
            updated = tuple(updated_entry if item.queue_id == queue_id else item for item in entries)
            _write_queue(store_root, updated)
            return updated_entry

    def read_reservation(
        self, queue_id: str, selection_generation: int, project_root: Path | None = None
    ) -> ScheduleReceipt:
        _validate_id(queue_id, "queue_id", "Q-")
        _validate_expected_generation(selection_generation)
        root = self._root(project_root)
        store_root = _prepare_store(root, False)
        raw = _safe_file(
            _reservation_path(store_root, queue_id, selection_generation, create=False), False
        )
        if raw is None:
            _raise(PortfolioSchedulerNotFoundError, "reservation_missing")
        receipt = decode_schedule_receipt(raw)
        if receipt.queue_id != queue_id or receipt.selection_generation != selection_generation:
            _raise(PortfolioSchedulerConflictError, "reservation_binding")
        return receipt
