"""Durable, Scheduler-local execution of a frozen recovery decision.

This module consumes the typed recovery decision core.  It does not choose a
recovery action and it does not cross the canonical state or worker boundary.
All durable mutations use the existing PortfolioSchedulerStore lock and
canonical encoders.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

import portfolio_scheduler_store as _store
from portfolio_scheduler_recovery import (
    RecoveryAction,
    RecoveryDecision,
    RecoveryError,
    RecoveryReason,
    RecoveryRequest,
    decide_reconciliation,
)
from portfolio_scheduler_store import (
    AdmissionPlanReservation,
    PortfolioSchedulerConflictError,
    PortfolioSchedulerError,
    PortfolioSchedulerInputError,
    PortfolioSchedulerNotFoundError,
    PortfolioSchedulerSchemaError,
    PortfolioSchedulerSecurityError,
    PortfolioSchedulerStore,
    PortfolioSchedulerTransitionError,
    QueueEntry,
    QueuePhase,
    ReceiptPhase,
    ScheduleReceipt,
)

__all__ = [
    "RECOVERY_EXECUTION_SCHEMA_VERSION",
    "RecoveryExecutionPhase",
    "RecoveryExecutionRequest",
    "RecoveryExecutionReceipt",
    "RecoveryExecutionOutcome",
    "PortfolioSchedulerRecoveryExecutorError",
    "PortfolioSchedulerRecoveryExecutorInputError",
    "PortfolioSchedulerRecoveryExecutorConflictError",
    "PortfolioSchedulerRecoveryExecutorFailClosedError",
    "PortfolioSchedulerRecoveryExecutor",
    "execute_recovery_action",
    "encode_recovery_execution_receipt",
    "decode_recovery_execution_receipt",
]


RECOVERY_EXECUTION_SCHEMA_VERSION = (
    "agentdesk.portfolio-scheduler.recovery-execution/v1"
)
_RECOVERY_ACTION_DIRECTORY = "recovery-actions"
_SHA256_PREFIX = "sha256:"
_PHASE_ORDER = (
    "RESERVED",
    "VALIDATED",
    "APPLYING",
    "APPLIED",
    "FINALIZED",
)
_ZERO_WRITE_ACTIONS = frozenset(
    {
        RecoveryAction.NO_OP,
        RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
        RecoveryAction.FAIL_CLOSED,
        RecoveryAction.REJECT,
    }
)


class PortfolioSchedulerRecoveryExecutorError(PortfolioSchedulerError):
    """Base error for the typed executor boundary."""


class PortfolioSchedulerRecoveryExecutorInputError(
    PortfolioSchedulerRecoveryExecutorError
):
    """Malformed or internally inconsistent typed input."""


class PortfolioSchedulerRecoveryExecutorConflictError(
    PortfolioSchedulerRecoveryExecutorError
):
    """Divergent execution identity or stale scheduler evidence."""


class PortfolioSchedulerRecoveryExecutorFailClosedError(
    PortfolioSchedulerRecoveryExecutorError
):
    """Evidence is insufficient for a safe Scheduler-local mutation."""


class RecoveryExecutionPhase(str, enum.Enum):
    RESERVED = "RESERVED"
    VALIDATED = "VALIDATED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    FINALIZED = "FINALIZED"


def _raise(error_type: type[PortfolioSchedulerRecoveryExecutorError], code: str) -> None:
    raise error_type(f"portfolio_scheduler_recovery_executor:{code}")


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_type")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_unsafe")
    return value


def _optional_text(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _text(value, code)


def _positive_int(value: object, code: str) -> int:
    if type(value) is not int or value < 1:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_int")
    return value


def _digest(value: object, code: str) -> str:
    text = _text(value, code)
    if len(text) != len(_SHA256_PREFIX) + 64 or not text.startswith(_SHA256_PREFIX):
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_format")
    if any(character not in "0123456789abcdef" for character in text[len(_SHA256_PREFIX):]):
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_format")
    return text


def _timestamp(value: object, code: str) -> str:
    text = _text(value, code)
    try:
        _store._validate_timestamp(text, code)
    except PortfolioSchedulerError:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_format")
    return text


def _enum_value(value: object, enum_type: type[enum.Enum], code: str) -> enum.Enum:
    if type(value) is not enum_type:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_type")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is str:
        return _quote(value)
    if type(value) is int:
        return str(value)
    _raise(PortfolioSchedulerRecoveryExecutorInputError, "scalar_type")


def _finish(lines: tuple[str, ...]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class RecoveryExecutionRequest:
    schema_version: str
    execution_id: str
    recovery_request: RecoveryRequest
    recovery_decision: RecoveryDecision
    decision_replay_digest: str
    expected_queue_phase: QueuePhase
    expected_receipt_phase: ReceiptPhase | None
    expected_selection_generation: int
    expected_recovery_generation: int
    expected_canonical_dispatch_event_id: str | None
    requested_at: str

    def __post_init__(self) -> None:
        if self.schema_version != RECOVERY_EXECUTION_SCHEMA_VERSION:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "schema_version")
        _text(self.execution_id, "execution_id")
        if type(self.recovery_request) is not RecoveryRequest:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "recovery_request")
        if type(self.recovery_decision) is not RecoveryDecision:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "recovery_decision")
        _digest(self.decision_replay_digest, "decision_replay_digest")
        _enum_value(self.expected_queue_phase, QueuePhase, "expected_queue_phase")
        if self.expected_receipt_phase is not None:
            _enum_value(self.expected_receipt_phase, ReceiptPhase, "expected_receipt_phase")
        _positive_int(self.expected_selection_generation, "expected_selection_generation")
        _positive_int(self.expected_recovery_generation, "expected_recovery_generation")
        _optional_text(
            self.expected_canonical_dispatch_event_id,
            "expected_canonical_dispatch_event_id",
        )
        _timestamp(self.requested_at, "requested_at")

        entry = self.recovery_request.queue_entry
        decision = self.recovery_decision
        if self.expected_selection_generation != entry.selection_generation:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "selection_generation")
        if self.expected_recovery_generation != self.recovery_request.recovery_generation:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "recovery_generation")
        if self.expected_queue_phase is not entry.state:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "queue_phase")
        receipt = self.recovery_request.schedule_receipt
        observed_receipt_phase = None if receipt is None else receipt.phase
        if self.expected_receipt_phase is not observed_receipt_phase:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "receipt_phase")
        if decision.queue_id != entry.queue_id:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "queue_id")
        if decision.task_id != entry.task_id or decision.revision != entry.revision:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "task_identity")
        if decision.enqueue_sequence != entry.enqueue_sequence:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "enqueue_sequence")
        if decision.expected_generation != self.expected_selection_generation:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "decision_generation")
        if decision.receipt_id != (None if receipt is None else receipt.receipt_id):
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "decision_receipt")
        if decision.canonical_dispatch_event_id != self.expected_canonical_dispatch_event_id:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "canonical_event")
        if decision.replay_digest != self.decision_replay_digest:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "decision_digest")


@dataclass(frozen=True, slots=True)
class RecoveryExecutionReceipt:
    schema_version: str
    execution_id: str
    queue_id: str
    receipt_id: str | None
    task_id: str
    revision: int
    selection_generation: int
    recovery_generation: int
    recovery_action: RecoveryAction
    recovery_reason: RecoveryReason
    canonical_dispatch_event_id: str | None
    decision_replay_digest: str
    phase: RecoveryExecutionPhase
    reserved_at: str
    applied_at: str | None
    finalized_at: str | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != RECOVERY_EXECUTION_SCHEMA_VERSION:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_schema_version")
        _text(self.execution_id, "receipt_execution_id")
        _store._validate_id(self.queue_id, "execution_queue_id", "Q-")
        _optional_text(self.receipt_id, "receipt_id")
        _text(self.task_id, "receipt_task_id")
        _positive_int(self.revision, "receipt_revision")
        _positive_int(self.selection_generation, "receipt_selection_generation")
        _positive_int(self.recovery_generation, "receipt_recovery_generation")
        _enum_value(self.recovery_action, RecoveryAction, "recovery_action")
        _enum_value(self.recovery_reason, RecoveryReason, "recovery_reason")
        _optional_text(self.canonical_dispatch_event_id, "canonical_event_id")
        _digest(self.decision_replay_digest, "receipt_decision_digest")
        _enum_value(self.phase, RecoveryExecutionPhase, "phase")
        _timestamp(self.reserved_at, "reserved_at")
        if self.applied_at is not None:
            _timestamp(self.applied_at, "applied_at")
        if self.finalized_at is not None:
            _timestamp(self.finalized_at, "finalized_at")
        _digest(self.content_digest, "content_digest")
        if self.phase is RecoveryExecutionPhase.RESERVED:
            if self.applied_at is not None or self.finalized_at is not None:
                _raise(PortfolioSchedulerRecoveryExecutorInputError, "reserved_timestamps")
        elif self.phase in (
            RecoveryExecutionPhase.VALIDATED,
            RecoveryExecutionPhase.APPLYING,
        ):
            if self.finalized_at is not None:
                _raise(PortfolioSchedulerRecoveryExecutorInputError, "active_finalized_at")
        elif self.phase is RecoveryExecutionPhase.APPLIED:
            if self.applied_at is None or self.finalized_at is not None:
                _raise(PortfolioSchedulerRecoveryExecutorInputError, "applied_timestamps")
        else:
            if self.applied_at is None or self.finalized_at is None:
                _raise(PortfolioSchedulerRecoveryExecutorInputError, "finalized_timestamps")
        if self.content_digest != _receipt_digest(self, include_digest=False):
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "content_digest_mismatch")


@dataclass(frozen=True, slots=True)
class RecoveryExecutionOutcome:
    schema_version: str
    execution_id: str
    phase: RecoveryExecutionPhase
    result: str
    recovery_action: RecoveryAction
    receipt: RecoveryExecutionReceipt | None

    def __post_init__(self) -> None:
        if self.schema_version != RECOVERY_EXECUTION_SCHEMA_VERSION:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "outcome_schema_version")
        _text(self.execution_id, "outcome_execution_id")
        _enum_value(self.phase, RecoveryExecutionPhase, "outcome_phase")
        if self.result not in ("NO_OP", "APPLIED", "REPLAYED", "REJECTED", "FAIL_CLOSED"):
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "outcome_result")
        _enum_value(self.recovery_action, RecoveryAction, "outcome_action")
        if self.receipt is not None and type(self.receipt) is not RecoveryExecutionReceipt:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "outcome_receipt")


_RECEIPT_FIELD_NAMES = (
    "schema_version",
    "execution_id",
    "queue_id",
    "receipt_id",
    "task_id",
    "revision",
    "selection_generation",
    "recovery_generation",
    "recovery_action",
    "recovery_reason",
    "canonical_dispatch_event_id",
    "decision_replay_digest",
    "phase",
    "reserved_at",
    "applied_at",
    "finalized_at",
    "content_digest",
)


def _receipt_lines(receipt: RecoveryExecutionReceipt, include_digest: bool) -> tuple[str, ...]:
    values = (
        receipt.schema_version,
        receipt.execution_id,
        receipt.queue_id,
        receipt.receipt_id,
        receipt.task_id,
        receipt.revision,
        receipt.selection_generation,
        receipt.recovery_generation,
        receipt.recovery_action.value,
        receipt.recovery_reason.value,
        receipt.canonical_dispatch_event_id,
        receipt.decision_replay_digest,
        receipt.phase.value,
        receipt.reserved_at,
        receipt.applied_at,
        receipt.finalized_at,
        receipt.content_digest if include_digest else None,
    )
    return tuple(
        f"{field}: {_scalar(value)}"
        for field, value in zip(_RECEIPT_FIELD_NAMES, values)
        if include_digest or field != "content_digest"
    )


def _receipt_digest_from_values(values: tuple[object, ...]) -> str:
    lines = tuple(
        f"{field}: {_scalar(value)}"
        for field, value in zip(_RECEIPT_FIELD_NAMES[:-1], values)
    )
    return _SHA256_PREFIX + hashlib.sha256(_finish(lines)).hexdigest()


def _receipt_digest(receipt: RecoveryExecutionReceipt, include_digest: bool) -> str:
    del include_digest
    return _SHA256_PREFIX + hashlib.sha256(_finish(_receipt_lines(receipt, False))).hexdigest()


def encode_recovery_execution_receipt(receipt: RecoveryExecutionReceipt) -> bytes:
    if type(receipt) is not RecoveryExecutionReceipt:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_type")
    return _finish(_receipt_lines(receipt, True))


def _parse_scalar(token: str, code: str) -> object:
    if token == "null":
        return None
    if token.startswith('"'):
        if len(token) < 2 or token[-1] != '"':
            _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_scalar")
        result = []
        index = 1
        end = len(token) - 1
        while index < end:
            character = token[index]
            if character == "\\":
                index += 1
                if index >= end or token[index] not in ('"', "\\"):
                    _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_escape")
                result.append(token[index])
            elif character == '"':
                _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_quote")
            else:
                result.append(character)
            index += 1
        value = "".join(result)
        if _quote(value) != token:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_canonical")
        return value
    if token.isdigit() and (token == "0" or not token.startswith("0")):
        return int(token)
    _raise(PortfolioSchedulerRecoveryExecutorInputError, f"{code}_scalar")


def decode_recovery_execution_receipt(data: bytes) -> RecoveryExecutionReceipt:
    try:
        lines = _store._decode_document(data, "recovery_execution")
    except PortfolioSchedulerError as exc:
        raise PortfolioSchedulerRecoveryExecutorInputError(
            "portfolio_scheduler_recovery_executor:receipt_document"
        ) from exc
    if len(lines) != len(_RECEIPT_FIELD_NAMES):
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_field_count")
    values = []
    for field, line in zip(_RECEIPT_FIELD_NAMES, lines):
        prefix = f"{field}: "
        if not line.startswith(prefix):
            _raise(PortfolioSchedulerRecoveryExecutorInputError, f"receipt_field_{field}")
        values.append(_parse_scalar(line[len(prefix):], field))
    if values[0] != RECOVERY_EXECUTION_SCHEMA_VERSION:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_schema")
    if type(values[5]) is not int or type(values[6]) is not int or type(values[7]) is not int:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_integer")
    if type(values[8]) is not str or type(values[9]) is not str or type(values[12]) is not str:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_enum")
    try:
        receipt = RecoveryExecutionReceipt(
            schema_version=values[0],
            execution_id=values[1],
            queue_id=values[2],
            receipt_id=values[3],
            task_id=values[4],
            revision=values[5],
            selection_generation=values[6],
            recovery_generation=values[7],
            recovery_action=RecoveryAction(values[8]),
            recovery_reason=RecoveryReason(values[9]),
            canonical_dispatch_event_id=values[10],
            decision_replay_digest=values[11],
            phase=RecoveryExecutionPhase(values[12]),
            reserved_at=values[13],
            applied_at=values[14],
            finalized_at=values[15],
            content_digest=values[16],
        )
    except (TypeError, ValueError, PortfolioSchedulerError) as exc:
        raise PortfolioSchedulerRecoveryExecutorInputError(
            "portfolio_scheduler_recovery_executor:receipt_values"
        ) from exc
    if encode_recovery_execution_receipt(receipt) != data:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "receipt_noncanonical")
    return receipt


def _validate_event(request: RecoveryExecutionRequest) -> None:
    event_id = request.expected_canonical_dispatch_event_id
    if event_id is None:
        if request.recovery_decision.recovery_action in (
            RecoveryAction.ADOPT_CANONICAL_DISPATCH,
            RecoveryAction.RETIRE_QUEUE_ENTRY,
        ):
            _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "canonical_event_missing")
        return
    _store._validate_id(event_id, "canonical_event_id", "EVT-")
    exact = tuple(
        event for event in request.recovery_request.canonical_events if event.event_id == event_id
    )
    if len(exact) != 1:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "canonical_event_unproven")
    event = exact[0]
    if (
        event.event_type != "TASK_DISPATCHED"
        or event.task_id != request.recovery_request.queue_entry.task_id
        or event.revision != request.recovery_request.queue_entry.revision
    ):
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "canonical_event_binding")


def _validate_request(request: RecoveryExecutionRequest) -> None:
    if type(request) is not RecoveryExecutionRequest:
        _raise(PortfolioSchedulerRecoveryExecutorInputError, "request_type")
    entry = request.recovery_request.queue_entry
    if entry.retry_budget_used >= 3:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "attempt_four")
    try:
        recomputed = decide_reconciliation(request.recovery_request)
    except RecoveryError as exc:
        raise PortfolioSchedulerRecoveryExecutorFailClosedError(
            "portfolio_scheduler_recovery_executor:decision_recompute"
        ) from exc
    if recomputed != request.recovery_decision:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "decision_replay")
    if recomputed.replay_digest != request.decision_replay_digest:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "decision_digest")
    _validate_event(request)


def _execution_path(store_root: Path, queue_id: str, generation: int, create: bool) -> Path:
    _store._validate_id(queue_id, "execution_queue_id", "Q-")
    _store._validate_non_bool_int(generation, "execution_generation", 1)
    root = store_root / _RECOVERY_ACTION_DIRECTORY
    if create:
        _store._ensure_directory(root, True)
        _store._ensure_directory(root / queue_id, True)
    return root / queue_id / f"g{generation}.yaml"


def _read_execution_receipt(store_root: Path, queue_id: str, generation: int) -> RecoveryExecutionReceipt | None:
    path = _execution_path(store_root, queue_id, generation, False)
    raw = _store._safe_file(path, True)
    if raw is None:
        return None
    return decode_recovery_execution_receipt(raw)


def _write_execution_receipt(
    store_root: Path, receipt: RecoveryExecutionReceipt
) -> RecoveryExecutionReceipt:
    path = _execution_path(
        store_root, receipt.queue_id, receipt.recovery_generation, True
    )
    _store._atomic_store_bytes(path, encode_recovery_execution_receipt(receipt))
    return receipt


def _new_receipt(request: RecoveryExecutionRequest) -> RecoveryExecutionReceipt:
    decision = request.recovery_decision
    values = (
        RECOVERY_EXECUTION_SCHEMA_VERSION,
        request.execution_id,
        decision.queue_id,
        decision.receipt_id,
        decision.task_id,
        decision.revision,
        request.expected_selection_generation,
        request.expected_recovery_generation,
        decision.recovery_action.value,
        decision.reason.value,
        decision.canonical_dispatch_event_id,
        request.decision_replay_digest,
        RecoveryExecutionPhase.RESERVED.value,
        request.requested_at,
        None,
        None,
    )
    return RecoveryExecutionReceipt(
        schema_version=RECOVERY_EXECUTION_SCHEMA_VERSION,
        execution_id=request.execution_id,
        queue_id=decision.queue_id,
        receipt_id=decision.receipt_id,
        task_id=decision.task_id,
        revision=decision.revision,
        selection_generation=request.expected_selection_generation,
        recovery_generation=request.expected_recovery_generation,
        recovery_action=decision.recovery_action,
        recovery_reason=decision.reason,
        canonical_dispatch_event_id=decision.canonical_dispatch_event_id,
        decision_replay_digest=request.decision_replay_digest,
        phase=RecoveryExecutionPhase.RESERVED,
        reserved_at=request.requested_at,
        applied_at=None,
        finalized_at=None,
        content_digest=_receipt_digest_from_values(values),
    )


def _same_execution_identity(
    left: RecoveryExecutionReceipt, request: RecoveryExecutionRequest
) -> bool:
    decision = request.recovery_decision
    return (
        left.execution_id == request.execution_id
        and left.queue_id == decision.queue_id
        and left.receipt_id == decision.receipt_id
        and left.task_id == decision.task_id
        and left.revision == decision.revision
        and left.selection_generation == request.expected_selection_generation
        and left.recovery_generation == request.expected_recovery_generation
        and left.recovery_action is decision.recovery_action
        and left.recovery_reason is decision.reason
        and left.canonical_dispatch_event_id == decision.canonical_dispatch_event_id
        and left.decision_replay_digest == request.decision_replay_digest
    )


def _advance_receipt(
    store_root: Path,
    receipt: RecoveryExecutionReceipt,
    phase: RecoveryExecutionPhase,
    requested_at: str,
) -> RecoveryExecutionReceipt:
    current_index = _PHASE_ORDER.index(receipt.phase.value)
    next_index = _PHASE_ORDER.index(phase.value)
    if next_index != current_index + 1:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "phase_transition")
    applied_at = receipt.applied_at
    finalized_at = receipt.finalized_at
    if phase is RecoveryExecutionPhase.APPLIED:
        applied_at = requested_at
    if phase is RecoveryExecutionPhase.FINALIZED:
        if applied_at is None:
            applied_at = requested_at
        finalized_at = requested_at
    values = (
        receipt.schema_version,
        receipt.execution_id,
        receipt.queue_id,
        receipt.receipt_id,
        receipt.task_id,
        receipt.revision,
        receipt.selection_generation,
        receipt.recovery_generation,
        receipt.recovery_action.value,
        receipt.recovery_reason.value,
        receipt.canonical_dispatch_event_id,
        receipt.decision_replay_digest,
        phase.value,
        receipt.reserved_at,
        applied_at,
        finalized_at,
    )
    candidate = replace(
        receipt,
        phase=phase,
        applied_at=applied_at,
        finalized_at=finalized_at,
        content_digest=_receipt_digest_from_values(values),
    )
    return _write_execution_receipt(store_root, candidate)


def _read_live(
    store_root: Path, request: RecoveryExecutionRequest
) -> tuple[QueueEntry, ScheduleReceipt | None, object | None]:
    entries, _ = _store._load_state(store_root)
    entry = _store._find_queue_entry(entries, request.recovery_decision.queue_id)
    receipt_id = request.recovery_decision.receipt_id
    schedule_receipt = None if receipt_id is None else _store._read_receipt_file(store_root, receipt_id)
    tombstone = None if receipt_id is None else _store._read_tombstone(store_root, receipt_id)
    if entry.task_id != request.recovery_decision.task_id or entry.revision != request.recovery_decision.revision:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "live_task_identity")
    if entry.selection_generation not in (
        request.expected_selection_generation,
        request.expected_selection_generation + 1,
    ):
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "live_generation")
    return entry, schedule_receipt, tombstone


def _validate_initial_live(
    request: RecoveryExecutionRequest,
    entry: QueueEntry,
    schedule_receipt: ScheduleReceipt | None,
) -> None:
    expected_entry = request.recovery_request.queue_entry
    action = request.recovery_decision.recovery_action
    if action is RecoveryAction.REQUEUE_SELECTED and entry.selection_generation == expected_entry.selection_generation + 1:
        if entry.state is not QueuePhase.QUEUED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "requeue_generation_state")
    elif action is RecoveryAction.ADOPT_CANONICAL_DISPATCH and entry.state in (
        QueuePhase.DISPATCHED,
        QueuePhase.RETIRED,
    ):
        if schedule_receipt is None or schedule_receipt.phase is not ReceiptPhase.DISPATCHED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "adopt_replay_receipt")
        if schedule_receipt.dispatch_event_id != request.expected_canonical_dispatch_event_id:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "adopt_replay_event")
    elif action is RecoveryAction.RETIRE_QUEUE_ENTRY and entry.state is QueuePhase.RETIRED:
        if schedule_receipt is None or schedule_receipt.phase is not ReceiptPhase.DISPATCHED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "retire_replay_receipt")
        if schedule_receipt.dispatch_event_id != request.expected_canonical_dispatch_event_id:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "retire_replay_event")
    elif entry != expected_entry:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "live_queue_divergence")
    if schedule_receipt is None:
        if request.recovery_decision.receipt_id is not None:
            _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "schedule_receipt_missing")
    else:
        expected_receipt = request.recovery_request.schedule_receipt
        if expected_receipt is not None and schedule_receipt.receipt_id != expected_receipt.receipt_id:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "live_receipt_identity")
        if schedule_receipt.queue_id != entry.queue_id or schedule_receipt.task_id != entry.task_id:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "live_receipt_binding")


def _write_queue_phase_locked(
    store_root: Path,
    entry: QueueEntry,
    new_phase: QueuePhase,
    event_id: str | None,
) -> QueueEntry:
    if entry.state is new_phase:
        if new_phase is QueuePhase.DISPATCHED and entry.state is not QueuePhase.DISPATCHED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "queue_replay_event")
        return entry
    allowed = {
        QueuePhase.QUEUED: QueuePhase.SELECTED,
        QueuePhase.SELECTED: QueuePhase.DISPATCHED,
        QueuePhase.DISPATCHED: QueuePhase.RETIRED,
    }
    if allowed.get(entry.state) is not new_phase:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "queue_phase_transition")
    if new_phase is QueuePhase.DISPATCHED and event_id is None:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "dispatch_event_missing")
    if new_phase is not QueuePhase.DISPATCHED and event_id is not None:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "unexpected_dispatch_event")
    updated = _store._replace_entry(entry, state=new_phase)
    entries, _ = _store._load_state(store_root)
    current = _store._find_queue_entry(entries, entry.queue_id)
    if current.content_digest != entry.content_digest:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "queue_cas")
    _store._write_queue(
        store_root,
        tuple(updated if item.queue_id == entry.queue_id else item for item in entries),
    )
    return updated


def _align_canonical_dispatch_locked(
    store_root: Path,
    request: RecoveryExecutionRequest,
    entry: QueueEntry,
    schedule_receipt: ScheduleReceipt | None,
) -> None:
    event_id = request.expected_canonical_dispatch_event_id
    if event_id is None or schedule_receipt is None:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "adopt_evidence_missing")
    if schedule_receipt.phase is ReceiptPhase.SELECTED:
        reservation_path = _store._reservation_path(
            store_root,
            schedule_receipt.queue_id,
            schedule_receipt.selection_generation,
            create=False,
        )
        reservation = _store._safe_file(reservation_path, True)
        encoded_old = _store.encode_schedule_receipt(schedule_receipt)
        if reservation is not None and reservation != encoded_old:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "reservation_divergence")
        updated_receipt = _store._replace_receipt(
            schedule_receipt,
            phase=ReceiptPhase.DISPATCHED,
            dispatch_event_id=event_id,
        )
        _store._atomic_store_bytes(
            _store._receipt_path(store_root, schedule_receipt.receipt_id),
            _store.encode_schedule_receipt(updated_receipt),
        )
        schedule_receipt = updated_receipt
    elif (
        schedule_receipt.phase is not ReceiptPhase.DISPATCHED
        or schedule_receipt.dispatch_event_id != event_id
    ):
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "receipt_alignment")
    if entry.state is QueuePhase.SELECTED:
        _write_queue_phase_locked(store_root, entry, QueuePhase.DISPATCHED, event_id)
    elif entry.state not in (QueuePhase.DISPATCHED, QueuePhase.RETIRED):
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "queue_alignment")


def _requeue_selected_locked(
    store_root: Path,
    request: RecoveryExecutionRequest,
    entry: QueueEntry,
    schedule_receipt: ScheduleReceipt | None,
    tombstone: object | None,
) -> None:
    receipt_id = request.recovery_decision.receipt_id
    if receipt_id is None or schedule_receipt is None:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "requeue_evidence_missing")
    if schedule_receipt.phase is not ReceiptPhase.SELECTED:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "requeue_receipt_phase")
    expected_generation = request.expected_selection_generation
    if tombstone is not None:
        if (
            tombstone.kind != "selected_recovery"
            or tombstone.receipt_id != receipt_id
            or tombstone.queue_id != entry.queue_id
            or tombstone.selection_generation != expected_generation
            or tombstone.next_selection_generation != expected_generation + 1
        ):
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "requeue_tombstone")
        _store._validate_tombstone_for_receipt(tombstone, schedule_receipt)
    if entry.selection_generation == expected_generation + 1:
        if entry.state is not QueuePhase.QUEUED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "requeue_replay_state")
        return
    if entry.selection_generation != expected_generation or entry.state is not QueuePhase.SELECTED:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "requeue_state")
    _store._validate_receipt_binding(schedule_receipt, entry)
    if tombstone is None:
        tombstone = _store._Tombstone(
            kind="selected_recovery",
            receipt_id=receipt_id,
            queue_id=entry.queue_id,
            selection_generation=expected_generation,
            next_selection_generation=expected_generation + 1,
            receipt_digest=schedule_receipt.content_digest,
            created_at=request.requested_at,
        )
        _store._atomic_store_bytes(
            _store._tombstone_path(store_root, receipt_id),
            _store._encode_tombstone(tombstone),
        )
    updated = _store._replace_entry(
        entry,
        state=QueuePhase.QUEUED,
        selection_generation=expected_generation + 1,
    )
    entries, _ = _store._load_state(store_root)
    current = _store._find_queue_entry(entries, entry.queue_id)
    if current.content_digest != entry.content_digest:
        _raise(PortfolioSchedulerRecoveryExecutorConflictError, "requeue_queue_cas")
    _store._write_queue(
        store_root,
        tuple(updated if item.queue_id == entry.queue_id else item for item in entries),
    )


def _apply_action_locked(
    store_root: Path,
    request: RecoveryExecutionRequest,
) -> None:
    entry, schedule_receipt, tombstone = _read_live(store_root, request)
    _validate_initial_live(request, entry, schedule_receipt)
    action = request.recovery_decision.recovery_action
    if action is RecoveryAction.RESUME:
        if entry.state is not QueuePhase.QUEUED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "resume_state")
        return
    if action is RecoveryAction.REQUEUE_SELECTED:
        _requeue_selected_locked(store_root, request, entry, schedule_receipt, tombstone)
        return
    if action is RecoveryAction.ADOPT_CANONICAL_DISPATCH:
        _align_canonical_dispatch_locked(store_root, request, entry, schedule_receipt)
        return
    if action is RecoveryAction.RETIRE_QUEUE_ENTRY:
        event_id = request.expected_canonical_dispatch_event_id
        if event_id is None or schedule_receipt is None:
            _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "retire_evidence_missing")
        if schedule_receipt.phase is not ReceiptPhase.DISPATCHED or schedule_receipt.dispatch_event_id != event_id:
            _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "retire_receipt_evidence")
        if entry.state is QueuePhase.DISPATCHED:
            _write_queue_phase_locked(store_root, entry, QueuePhase.RETIRED, None)
        elif entry.state is not QueuePhase.RETIRED:
            _raise(PortfolioSchedulerRecoveryExecutorConflictError, "retire_queue_state")
        return
    if action is RecoveryAction.INVALIDATE_SELECTION:
        _raise(PortfolioSchedulerRecoveryExecutorFailClosedError, "invalidate_requires_receipt")
    _raise(PortfolioSchedulerRecoveryExecutorInputError, "unsupported_action")


def _zero_write_outcome(request: RecoveryExecutionRequest) -> RecoveryExecutionOutcome:
    action = request.recovery_decision.recovery_action
    if action is RecoveryAction.NO_OP or action is RecoveryAction.WAIT_FOR_EXECUTION_OWNER:
        result = "NO_OP"
    elif action is RecoveryAction.REJECT:
        result = "REJECTED"
    else:
        result = "FAIL_CLOSED"
    return RecoveryExecutionOutcome(
        schema_version=RECOVERY_EXECUTION_SCHEMA_VERSION,
        execution_id=request.execution_id,
        phase=RecoveryExecutionPhase.FINALIZED,
        result=result,
        recovery_action=action,
        receipt=None,
    )


class PortfolioSchedulerRecoveryExecutor:
    """Execute one frozen decision inside the Scheduler evidence boundary."""

    __slots__ = ("_project_root", "_store")

    def __init__(
        self,
        project_root: Path,
        store: PortfolioSchedulerStore | None = None,
    ) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "project_root")
        if store is not None and type(store) is not PortfolioSchedulerStore:
            _raise(PortfolioSchedulerRecoveryExecutorInputError, "store_type")
        self._project_root = project_root
        self._store = PortfolioSchedulerStore(project_root) if store is None else store

    def execute(self, request: RecoveryExecutionRequest) -> RecoveryExecutionOutcome:
        _validate_request(request)
        action = request.recovery_decision.recovery_action
        if action in _ZERO_WRITE_ACTIONS:
            return _zero_write_outcome(request)
        if action is RecoveryAction.INVALIDATE_SELECTION and request.recovery_request.schedule_receipt is None:
            return RecoveryExecutionOutcome(
                schema_version=RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id=request.execution_id,
                phase=RecoveryExecutionPhase.FINALIZED,
                result="FAIL_CLOSED",
                recovery_action=action,
                receipt=None,
            )
        root = self._store._root(self._project_root)
        with _store._store_lock(root):
            store_root = _store._prepare_store(root, True)
            existing = _read_execution_receipt(
                store_root,
                request.recovery_decision.queue_id,
                request.expected_recovery_generation,
            )
            if existing is not None:
                if not _same_execution_identity(existing, request):
                    _raise(PortfolioSchedulerRecoveryExecutorConflictError, "execution_divergence")
                if existing.phase is RecoveryExecutionPhase.FINALIZED:
                    return RecoveryExecutionOutcome(
                        schema_version=RECOVERY_EXECUTION_SCHEMA_VERSION,
                        execution_id=request.execution_id,
                        phase=existing.phase,
                        result="REPLAYED",
                        recovery_action=existing.recovery_action,
                        receipt=existing,
                    )
                receipt = existing
            else:
                receipt = _write_execution_receipt(store_root, _new_receipt(request))

            if receipt.phase is RecoveryExecutionPhase.RESERVED:
                receipt = _advance_receipt(
                    store_root, receipt, RecoveryExecutionPhase.VALIDATED, request.requested_at
                )
            if receipt.phase is RecoveryExecutionPhase.VALIDATED:
                receipt = _advance_receipt(
                    store_root, receipt, RecoveryExecutionPhase.APPLYING, request.requested_at
                )
            if receipt.phase is RecoveryExecutionPhase.APPLYING:
                _apply_action_locked(store_root, request)
                receipt = _advance_receipt(
                    store_root, receipt, RecoveryExecutionPhase.APPLIED, request.requested_at
                )
            if receipt.phase is RecoveryExecutionPhase.APPLIED:
                _apply_action_locked(store_root, request)
                receipt = _advance_receipt(
                    store_root, receipt, RecoveryExecutionPhase.FINALIZED, request.requested_at
                )
            if receipt.phase is not RecoveryExecutionPhase.FINALIZED:
                _raise(PortfolioSchedulerRecoveryExecutorConflictError, "execution_incomplete")
            return RecoveryExecutionOutcome(
                schema_version=RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id=request.execution_id,
                phase=receipt.phase,
                result="APPLIED",
                recovery_action=receipt.recovery_action,
                receipt=receipt,
            )


def execute_recovery_action(
    project_root: Path,
    request: RecoveryExecutionRequest,
) -> RecoveryExecutionOutcome:
    return PortfolioSchedulerRecoveryExecutor(project_root).execute(request)
