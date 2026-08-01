"""PortfolioScheduler admission reservation core.

This module implements the small boundary between a durable selected
ScheduleReceipt and a canonical ``TASK_DISPATCHED`` event.  It does not
start a Worker, execute ACK or delivery transitions, call
``run_dispatch_cycle()``, or re-run selection.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from control_plane_transition import (
    ControlPlaneTransitionService,
    DispatchPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from core_types import WorkerKind
from dispatcher_gateway import ModelSelectionSnapshot
from portfolio_scheduler_store import (
    RECEIPTS_DIRECTORY,
    SCHEMA_VERSION,
    STORE_RELATIVE,
    PortfolioSchedulerConflictError,
    PortfolioSchedulerNotFoundError,
    PortfolioSchedulerStore,
    PortfolioSchedulerTransitionError,
    QueueEntry,
    QueuePhase,
    ReceiptPhase,
    ScheduleReceipt,
    decode_schedule_receipt,
)
from state_provider import (
    EventEntry,
    StateProvider,
    StateSnapshot,
    TaskEntry,
)
from worker_slot_lease import (
    acquire_worker_slot,
    read_worker_slot_leases,
    release_worker_slot,
)

__all__ = [
    "PortfolioAdmissionInputError",
    "PortfolioAdmissionConflictError",
    "PortfolioAdmissionFencingError",
    "PortfolioAdmissionRequest",
    "PortfolioAdmissionResult",
    "PortfolioSchedulerAdmission",
]


_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVENT_ID_RE = re.compile(r"EVT-.+\Z")
_MESSAGE_ID_RE = re.compile(r"MSG-.+\Z")
_UNSAFE_CHARS = frozenset({"\0", "\r", "\n"})


class PortfolioAdmissionError(ValueError):
    """Base class for typed admission failures."""


class PortfolioAdmissionInputError(PortfolioAdmissionError):
    """Malformed or unsupported admission request."""


class PortfolioAdmissionConflictError(PortfolioAdmissionError):
    """Stale, divergent, or conflicting admission evidence."""


class PortfolioAdmissionFencingError(PortfolioAdmissionError):
    """Lease or canonical fencing evidence is missing or mismatched."""


def _require_exact_str(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise PortfolioAdmissionInputError(f"portfolio_admission:{code}_type")
    if value != unicodedata.normalize("NFC", value):
        raise PortfolioAdmissionInputError(
            f"portfolio_admission:{code}_normalization"
        )
    if any(character in _UNSAFE_CHARS for character in value):
        raise PortfolioAdmissionInputError(f"portfolio_admission:{code}_unsafe")
    return value


def _require_prefix(value: object, code: str, prefix: str) -> str:
    text = _require_exact_str(value, code)
    if not text.startswith(prefix):
        raise PortfolioAdmissionInputError(f"portfolio_admission:{code}_prefix")
    return text


def _require_sha(value: object, code: str) -> str:
    text = _require_exact_str(value, code)
    if _SHA40_RE.fullmatch(text) is None:
        raise PortfolioAdmissionInputError(f"portfolio_admission:{code}_sha")
    return text


def _require_digest(value: object, code: str) -> str:
    text = _require_exact_str(value, code)
    if _DIGEST_RE.fullmatch(text) is None:
        raise PortfolioAdmissionInputError(f"portfolio_admission:{code}_digest")
    return text


def _require_int(value: object, code: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise PortfolioAdmissionInputError(f"portfolio_admission:{code}_int")
    return value


def _require_utc_datetime(value: object) -> datetime:
    if type(value) is not datetime:
        raise PortfolioAdmissionInputError("portfolio_admission:now_type")
    if value.tzinfo is None:
        raise PortfolioAdmissionInputError("portfolio_admission:now_timezone")
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise PortfolioAdmissionInputError("portfolio_admission:now_timezone")
    return value


def _require_absolute_path(value: object) -> str:
    text = _require_exact_str(value, "canonical_worktree")
    if not Path(text).is_absolute():
        raise PortfolioAdmissionInputError(
            "portfolio_admission:canonical_worktree_absolute"
        )
    return text


@dataclass(frozen=True, slots=True)
class PortfolioAdmissionRequest:
    """Frozen identity and CAS input for one scheduler admission."""

    schema_version: str
    queue_id: str
    receipt_id: str
    task_id: str
    revision: int
    enqueue_sequence: int
    selection_generation: int
    worker_kind: WorkerKind
    assessment_id: str
    dispatch_id: str
    event_id: str
    outbox_message_id: str
    role_id: str
    task_card_path: str
    task_card_commit: str
    base_commit: str
    branch: str
    report_path: str
    model_selection: ModelSelectionSnapshot
    expected_task_state: str
    expected_task_attempt: int
    new_attempt: int
    expected_snapshot_commit: str
    expected_queue_phase: QueuePhase
    expected_receipt_phase: ReceiptPhase
    expected_queue_digest: str
    expected_receipt_digest: str
    policy_version: str
    now: datetime
    holder_instance_id: str
    canonical_worktree: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:schema_version"
            )
        _require_prefix(self.queue_id, "queue_id", "Q-")
        _require_prefix(self.receipt_id, "receipt_id", "SR-")
        _require_exact_str(self.task_id, "task_id")
        _require_int(self.revision, "revision", 1)
        _require_int(self.enqueue_sequence, "enqueue_sequence", 1)
        _require_int(self.selection_generation, "selection_generation", 1)
        if type(self.worker_kind) is not WorkerKind:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:worker_kind_type"
            )
        _require_prefix(self.assessment_id, "assessment_id", "ASM-")
        _require_exact_str(self.dispatch_id, "dispatch_id")
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:event_id_format"
            )
        if _MESSAGE_ID_RE.fullmatch(self.outbox_message_id) is None:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:outbox_message_id_format"
            )
        _require_exact_str(self.role_id, "role_id")
        _require_exact_str(self.task_card_path, "task_card_path")
        _require_sha(self.task_card_commit, "task_card_commit")
        _require_sha(self.base_commit, "base_commit")
        _require_exact_str(self.branch, "branch")
        _require_exact_str(self.report_path, "report_path")
        if type(self.model_selection) is not ModelSelectionSnapshot:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:model_selection_type"
            )
        if self.expected_task_state != "ready":
            raise PortfolioAdmissionInputError(
                "portfolio_admission:expected_task_state"
            )
        _require_int(self.expected_task_attempt, "expected_task_attempt", 0)
        _require_int(self.new_attempt, "new_attempt", 1)
        if self.new_attempt > 3:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:attempt_limit"
            )
        if self.expected_task_attempt >= 3:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:attempt_limit"
            )
        if self.new_attempt != self.expected_task_attempt + 1:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:attempt_sequence"
            )
        _require_sha(self.expected_snapshot_commit, "expected_snapshot_commit")
        if type(self.expected_queue_phase) is not QueuePhase:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:expected_queue_phase_type"
            )
        if type(self.expected_receipt_phase) is not ReceiptPhase:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:expected_receipt_phase_type"
            )
        _require_digest(self.expected_queue_digest, "expected_queue_digest")
        _require_digest(self.expected_receipt_digest, "expected_receipt_digest")
        if self.policy_version != SCHEMA_VERSION:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:policy_version"
            )
        _require_utc_datetime(self.now)
        _require_exact_str(self.holder_instance_id, "holder_instance_id")
        _require_absolute_path(self.canonical_worktree)


@dataclass(frozen=True, slots=True)
class PortfolioAdmissionResult:
    """Frozen result for one canonical dispatch admission."""

    queue_id: str
    receipt_id: str
    dispatch_id: str
    event_id: str
    lease_id: str
    queue_phase: QueuePhase
    receipt_phase: ReceiptPhase
    transition: TransitionResult

    def __post_init__(self) -> None:
        _require_exact_str(self.queue_id, "queue_id")
        _require_exact_str(self.receipt_id, "receipt_id")
        _require_exact_str(self.dispatch_id, "dispatch_id")
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:event_id_format"
            )
        _require_exact_str(self.lease_id, "lease_id")
        if type(self.queue_phase) is not QueuePhase:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:queue_phase_type"
            )
        if type(self.receipt_phase) is not ReceiptPhase:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:receipt_phase_type"
            )
        if type(self.transition) is not TransitionResult:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:transition_type"
            )


class PortfolioSchedulerAdmission:
    """Existing public entry points wired in frozen lock order."""

    __slots__ = ("_project_root", "_store", "_transitions")

    def __init__(
        self,
        project_root: Path,
        store: PortfolioSchedulerStore | None = None,
        transitions: ControlPlaneTransitionService | None = None,
    ) -> None:
        if not isinstance(project_root, Path):
            raise PortfolioAdmissionInputError(
                "portfolio_admission:project_root_type"
            )
        if not project_root.is_absolute():
            raise PortfolioAdmissionInputError(
                "portfolio_admission:project_root_absolute"
            )
        self._project_root = project_root
        self._store = (
            PortfolioSchedulerStore(project_root)
            if store is None
            else store
        )
        self._transitions = (
            ControlPlaneTransitionService(project_root)
            if transitions is None
            else transitions
        )

    def admit(self, request: PortfolioAdmissionRequest) -> PortfolioAdmissionResult:
        """Reserve one selected scheduler entry through TASK_DISPATCHED."""
        if type(request) is not PortfolioAdmissionRequest:
            raise PortfolioAdmissionInputError(
                "portfolio_admission:request_type"
            )

        entry, receipt = self._load_scheduler_evidence(request)
        event, snapshot, task = self._load_canonical_dispatch(request)

        if event is not None:
            return self._replay_and_align(
                request, entry, receipt, event, snapshot
            )

        self._validate_fresh_scheduler_evidence(request, entry, receipt)
        self._validate_fresh_canonical(request, task)

        lease = acquire_worker_slot(
            self._project_root,
            request.worker_kind,
            request.dispatch_id,
            request.holder_instance_id,
            Path(request.canonical_worktree),
            request.now,
        )
        try:
            transition = self._transitions.apply_transition(
                self._build_transition_request(request),
                lease,
                request.now,
            )
        except BaseException:
            if not self._event_file_exists(request.event_id):
                release_worker_slot(self._project_root, lease, request.now)
            raise

        final_entry, final_receipt = self._align_scheduler_evidence(
            request, entry, receipt, transition.event_id
        )
        return PortfolioAdmissionResult(
            queue_id=final_entry.queue_id,
            receipt_id=final_receipt.receipt_id,
            dispatch_id=request.dispatch_id,
            event_id=transition.event_id,
            lease_id=lease.lease_id,
            queue_phase=final_entry.state,
            receipt_phase=final_receipt.phase,
            transition=transition,
        )

    def _load_scheduler_evidence(
        self, request: PortfolioAdmissionRequest
    ) -> tuple[QueueEntry, ScheduleReceipt]:
        entry = self._store.read_queue_entry(
            request.queue_id, self._project_root
        )
        try:
            receipt = self._store.read_schedule_receipt(
                request.receipt_id, self._project_root
            )
        except PortfolioSchedulerTransitionError as exc:
            if str(exc) not in {
                "portfolio_scheduler:dispatched_receipt_state",
                "portfolio_scheduler:selected_receipt_stale",
            }:
                raise
            receipt = self._read_raw_receipt(request.receipt_id)

        if entry.queue_id != request.queue_id:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:queue_identity_mismatch"
            )
        if (
            receipt.queue_id != request.queue_id
            or receipt.receipt_id != request.receipt_id
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:receipt_identity_mismatch"
            )
        if (
            entry.task_id != request.task_id
            or entry.revision != request.revision
            or entry.enqueue_sequence != request.enqueue_sequence
            or entry.selection_generation != request.selection_generation
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:queue_identity_mismatch"
            )
        if (
            receipt.task_id != request.task_id
            or receipt.revision != request.revision
            or receipt.enqueue_sequence != request.enqueue_sequence
            or receipt.selection_generation != request.selection_generation
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:receipt_identity_mismatch"
            )
        return entry, receipt

    def _read_raw_receipt(self, receipt_id: str) -> ScheduleReceipt:
        path = (
            self._project_root
            / STORE_RELATIVE
            / RECEIPTS_DIRECTORY
            / f"{receipt_id}.yaml"
        )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise PortfolioSchedulerNotFoundError(
                "portfolio_scheduler:receipt_missing"
            ) from exc
        return decode_schedule_receipt(raw)

    def _load_canonical_dispatch(
        self, request: PortfolioAdmissionRequest
    ) -> tuple[EventEntry | None, StateSnapshot, TaskEntry]:
        snapshot = StateProvider(self._project_root).snapshot()
        if request.expected_snapshot_commit != self._git_head():
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:snapshot_commit_stale"
            )
        task = next(
            (item for item in snapshot.tasks if item.task_id == request.task_id),
            None,
        )
        if task is None:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:task_missing"
            )
        if task.revision != request.revision:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:task_revision_stale"
            )
        events = tuple(
            event
            for event in snapshot.events
            if event.event_type == "TASK_DISPATCHED"
            and event.task_id == request.task_id
            and event.revision == request.revision
        )
        if len(events) > 1:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:duplicate_dispatch_event"
            )
        if not events:
            return None, snapshot, task

        event = events[0]
        if (
            event.event_id != request.event_id
            or event.dispatch_id != request.dispatch_id
            or event.attempt != request.new_attempt
            or event.from_state != "ready"
            or event.to_state != "dispatched"
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:canonical_dispatch_conflict"
            )
        if (
            task.current_dispatch is None
            or task.current_dispatch.dispatch_id != request.dispatch_id
            or self._task_attempt(task) != request.new_attempt
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:canonical_dispatch_conflict"
            )
        return event, snapshot, task

    def _validate_fresh_scheduler_evidence(
        self,
        request: PortfolioAdmissionRequest,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
    ) -> None:
        if entry.state is not request.expected_queue_phase:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:queue_phase_stale"
            )
        if receipt.phase is not request.expected_receipt_phase:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:receipt_phase_stale"
            )
        if (
            entry.content_digest != request.expected_queue_digest
            or receipt.content_digest != request.expected_receipt_digest
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:content_digest_mismatch"
            )
        if request.policy_version != SCHEMA_VERSION:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:policy_version_stale"
            )
        if (
            request.worker_kind is not receipt.worker_kind
            or request.worker_kind is not entry.worker_kind_request
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:worker_kind_mismatch"
            )
        if request.assessment_id != entry.assessment_id:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:assessment_id_mismatch"
            )

    def _validate_fresh_canonical(
        self, request: PortfolioAdmissionRequest, task: TaskEntry
    ) -> None:
        if task.state != request.expected_task_state:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:task_state_stale"
            )
        if self._task_attempt(task) != request.expected_task_attempt:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:task_attempt_stale"
            )
        if (
            task.task_card_path != request.task_card_path
            or task.task_card_commit != request.task_card_commit
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:task_card_identity_mismatch"
            )
        if task.current_dispatch is not None:
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:active_dispatch_conflict"
            )

    def _build_transition_request(
        self, request: PortfolioAdmissionRequest
    ) -> TransitionRequest:
        return TransitionRequest(
            cas=TransitionCAS(
                task_id=request.task_id,
                expected_revision=request.revision,
                expected_state=request.expected_task_state,
                expected_snapshot_commit=request.expected_snapshot_commit,
            ),
            dispatch_cas=None,
            event_id=request.event_id,
            event_type="TASK_DISPATCHED",
            payload=DispatchPayload(
                dispatch_id=request.dispatch_id,
                role_id=request.role_id,
                model_selection=request.model_selection,
                task_card_path=request.task_card_path,
                task_card_commit=request.task_card_commit,
                base_commit=request.base_commit,
                branch=request.branch,
                report_path=request.report_path,
                outbox_message_id=request.outbox_message_id,
                new_attempt=request.new_attempt,
            ),
            event_context=TransitionEventContext(
                source_message_id=None,
                evidence_refs=(),
                guard_results=(),
            ),
        )

    def _align_scheduler_evidence(
        self,
        request: PortfolioAdmissionRequest,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
        event_id: str,
    ) -> tuple[QueueEntry, ScheduleReceipt]:
        final_receipt = receipt
        final_entry = entry
        if receipt.phase is ReceiptPhase.SELECTED:
            if receipt.dispatch_event_id is not None:
                raise PortfolioAdmissionConflictError(
                    "portfolio_admission:receipt_event_replay"
                )
            final_receipt = self._store.advance_schedule_receipt(
                receipt.receipt_id,
                receipt.selection_generation,
                ReceiptPhase.DISPATCHED,
                event_id,
                project_root=self._project_root,
            )
        if entry.state is QueuePhase.SELECTED:
            final_entry = self._store.advance_queue_phase(
                entry.queue_id,
                entry.selection_generation,
                QueuePhase.DISPATCHED,
                event_id,
                project_root=self._project_root,
            )
        if (
            final_receipt.phase is not ReceiptPhase.DISPATCHED
            or final_receipt.dispatch_event_id != event_id
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:receipt_alignment"
            )
        if final_entry.state not in (
            QueuePhase.DISPATCHED,
            QueuePhase.RETIRED,
        ):
            raise PortfolioAdmissionConflictError(
                "portfolio_admission:queue_alignment"
            )
        return final_entry, final_receipt

    def _replay_and_align(
        self,
        request: PortfolioAdmissionRequest,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
        event: EventEntry,
        snapshot: StateSnapshot,
    ) -> PortfolioAdmissionResult:
        final_entry, final_receipt = self._align_scheduler_evidence(
            request, entry, receipt, event.event_id
        )
        outbox = next(
            (
                item
                for item in snapshot.outbox
                if item.event_id == event.event_id
            ),
            None,
        )
        transition = TransitionResult(
            task_id=event.task_id,
            event_id=event.event_id,
            from_state=event.from_state,
            to_state=event.to_state,
            occurred_at=event.occurred_at,
            outbox_message_id=outbox.message_id if outbox is not None else None,
        )
        return PortfolioAdmissionResult(
            queue_id=final_entry.queue_id,
            receipt_id=final_receipt.receipt_id,
            dispatch_id=request.dispatch_id,
            event_id=event.event_id,
            lease_id=self._existing_lease_id(request),
            queue_phase=final_entry.state,
            receipt_phase=final_receipt.phase,
            transition=transition,
        )

    def _existing_lease_id(self, request: PortfolioAdmissionRequest) -> str:
        data = read_worker_slot_leases(self._project_root)
        leases = data.get("leases")
        if not isinstance(leases, dict):
            raise PortfolioAdmissionFencingError(
                "portfolio_admission:lease_store_invalid"
            )
        found: list[str] = []
        for raw in leases.values():
            if not isinstance(raw, dict):
                continue
            if (
                raw.get("holder_dispatch_id") == request.dispatch_id
                and raw.get("holder_instance_id")
                == request.holder_instance_id
                and raw.get("worker_kind") == request.worker_kind.value
            ):
                lease_id = raw.get("lease_id")
                if type(lease_id) is str and lease_id:
                    found.append(lease_id)
        if len(found) != 1:
            raise PortfolioAdmissionFencingError(
                "portfolio_admission:lease_identity_missing"
            )
        return found[0]

    def _event_file_exists(self, event_id: str) -> bool:
        return (
            self._project_root
            / "docs"
            / "pm"
            / "events"
            / f"{event_id}.yaml"
        ).is_file()

    def _git_head(self) -> str:
        result = subprocess.run(
            ["git", "-C", str(self._project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise PortfolioAdmissionFencingError(
                "portfolio_admission:git_head_unavailable"
            )
        head = result.stdout.strip()
        if _SHA40_RE.fullmatch(head) is None:
            raise PortfolioAdmissionFencingError(
                "portfolio_admission:git_head_invalid"
            )
        return head

    @staticmethod
    def _task_attempt(task: TaskEntry) -> int:
        attempt = task.attempt
        if attempt is None:
            return 0
        if type(attempt) is not int or attempt < 0:
            raise PortfolioAdmissionFencingError(
                "portfolio_admission:task_attempt_invalid"
            )
        return attempt
