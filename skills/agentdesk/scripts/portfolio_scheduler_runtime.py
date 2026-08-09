"""PortfolioScheduler selection-to-admission runtime tick boundary.

This module wires one scheduler tick from a validated queue snapshot through
deterministic selection, durable reservation, queue phase advance, and the
existing PortfolioSchedulerAdmission boundary.  It never starts a Worker,
never calls a provider/model/API/network, and never executes recovery actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

from core_types import WorkerKind
from dispatcher_gateway import ModelSelectionSnapshot
from portfolio_scheduler_admission import (
    PortfolioAdmissionRequest,
    PortfolioAdmissionResult,
    PortfolioSchedulerAdmission,
)
from portfolio_scheduler_policy import AdmissionContext, select_next
from portfolio_scheduler_recovery import (
    LivenessEvidence,
    RecoveryAction,
    RecoveryDecision,
    RecoveryRequest,
    decide_reconciliation,
)
from portfolio_scheduler_store import (
    AdmissionPlanReservation,
    SCHEMA_VERSION,
    PortfolioSchedulerNotFoundError,
    PortfolioSchedulerStore,
    QueueEntry,
    QueuePhase,
    ReceiptPhase,
    ScheduleReceipt,
    _plan_digest,
)
from state_provider import StateProvider

__all__ = [
    "PortfolioAdmissionPlan",
    "PortfolioSchedulerRuntime",
    "PortfolioSchedulerRuntimeConflictError",
    "PortfolioSchedulerRuntimeError",
    "PortfolioSchedulerRuntimeInputError",
    "PortfolioSchedulerRuntimeTransitionError",
    "PortfolioSchedulerTickOutcome",
    "PortfolioSchedulerTickOutcomeKind",
    "PortfolioSchedulerTickRequest",
    "PortfolioSchedulerTickResult",
]


_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_EVENT_ID_RE = re.compile(r"EVT-.+\Z")
_MESSAGE_ID_RE = re.compile(r"MSG-.+\Z")
_UNSAFE_CHARS = frozenset({"\0", "\r", "\n"})
_RECOVERY_ACTIONS_NOT_EXECUTED = frozenset({
    RecoveryAction.INVALIDATE_SELECTION,
    RecoveryAction.REQUEUE_SELECTED,
    RecoveryAction.ADOPT_CANONICAL_DISPATCH,
    RecoveryAction.RETIRE_QUEUE_ENTRY,
    RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
    RecoveryAction.FAIL_CLOSED,
})


class PortfolioSchedulerRuntimeError(ValueError):
    """Base typed runtime error."""


class PortfolioSchedulerRuntimeInputError(PortfolioSchedulerRuntimeError):
    """Malformed or unsupported runtime input."""


class PortfolioSchedulerRuntimeConflictError(PortfolioSchedulerRuntimeError):
    """Stale, divergent, or duplicate runtime evidence."""


class PortfolioSchedulerRuntimeTransitionError(
    PortfolioSchedulerRuntimeError
):
    """Queue or receipt phase cannot be advanced by this tick."""


def _require_exact_type(value: object, expected_type: type, code: str) -> None:
    if type(value) is not expected_type:
        raise PortfolioSchedulerRuntimeInputError(
            f"portfolio_scheduler_runtime:{code}_type"
        )


def _require_str(value: object, code: str) -> str:
    _require_exact_type(value, str, code)
    if not value or any(ch in _UNSAFE_CHARS for ch in value):
        raise PortfolioSchedulerRuntimeInputError(
            f"portfolio_scheduler_runtime:{code}_unsafe"
        )
    if value != value.strip():
        raise PortfolioSchedulerRuntimeInputError(
            f"portfolio_scheduler_runtime:{code}_whitespace"
        )
    return value


def _require_prefix(value: object, code: str, prefix: str) -> str:
    text = _require_str(value, code)
    if not text.startswith(prefix):
        raise PortfolioSchedulerRuntimeInputError(
            f"portfolio_scheduler_runtime:{code}_prefix"
        )
    return text


def _require_sha(value: object, code: str) -> str:
    text = _require_str(value, code)
    if _SHA40_RE.fullmatch(text) is None:
        raise PortfolioSchedulerRuntimeInputError(
            f"portfolio_scheduler_runtime:{code}_sha"
        )
    return text


def _require_int(value: object, code: str, minimum: int) -> int:
    _require_exact_type(value, int, code)
    if value < minimum:
        raise PortfolioSchedulerRuntimeInputError(
            f"portfolio_scheduler_runtime:{code}_range"
        )
    return value


def _require_utc_datetime(value: object) -> datetime:
    _require_exact_type(value, datetime, "now")
    if value.tzinfo is None:
        raise PortfolioSchedulerRuntimeInputError(
            "portfolio_scheduler_runtime:now_timezone"
        )
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise PortfolioSchedulerRuntimeInputError(
            "portfolio_scheduler_runtime:now_timezone"
        )
    return value


def _require_absolute_path(value: object) -> Path:
    if not isinstance(value, Path):
        raise PortfolioSchedulerRuntimeInputError(
            "portfolio_scheduler_runtime:project_root_type"
        )
    if not value.is_absolute():
        raise PortfolioSchedulerRuntimeInputError(
            "portfolio_scheduler_runtime:project_root_absolute"
        )
    return value


@dataclass(frozen=True, slots=True)
class PortfolioAdmissionPlan:
    """Frozen durable dispatch identity and admission inputs."""

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
    policy_version: str
    now: datetime
    holder_instance_id: str
    canonical_worktree: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:schema_version"
            )
        _require_prefix(self.queue_id, "queue_id", "Q-")
        _require_prefix(self.receipt_id, "receipt_id", "SR-")
        _require_str(self.task_id, "task_id")
        _require_int(self.revision, "revision", 1)
        _require_int(self.enqueue_sequence, "enqueue_sequence", 1)
        _require_int(self.selection_generation, "selection_generation", 1)
        _require_exact_type(self.worker_kind, WorkerKind, "worker_kind")
        _require_prefix(self.assessment_id, "assessment_id", "ASM-")
        _require_str(self.dispatch_id, "dispatch_id")
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:event_id_format"
            )
        if _MESSAGE_ID_RE.fullmatch(self.outbox_message_id) is None:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:outbox_message_id_format"
            )
        _require_str(self.role_id, "role_id")
        _require_str(self.task_card_path, "task_card_path")
        _require_sha(self.task_card_commit, "task_card_commit")
        _require_sha(self.base_commit, "base_commit")
        _require_str(self.branch, "branch")
        _require_str(self.report_path, "report_path")
        _require_exact_type(
            self.model_selection, ModelSelectionSnapshot, "model_selection"
        )
        if self.expected_task_state != "ready":
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:expected_task_state"
            )
        _require_int(self.expected_task_attempt, "expected_task_attempt", 0)
        _require_int(self.new_attempt, "new_attempt", 1)
        if self.new_attempt > 3:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:attempt_limit"
            )
        if self.expected_task_attempt >= 3:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:attempt_limit"
            )
        if self.new_attempt != self.expected_task_attempt + 1:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:attempt_sequence"
            )
        _require_sha(self.expected_snapshot_commit, "expected_snapshot_commit")
        if self.policy_version != SCHEMA_VERSION:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:policy_version"
            )
        _require_utc_datetime(self.now)
        _require_str(self.holder_instance_id, "holder_instance_id")
        _require_str(self.canonical_worktree, "canonical_worktree")
        _require_absolute_path(Path(self.canonical_worktree))


@dataclass(frozen=True, slots=True)
class PortfolioSchedulerTickRequest:
    """One frozen scheduler tick."""

    project_root: Path
    plan: PortfolioAdmissionPlan
    admission_context: AdmissionContext
    recovery_generation: int = 1

    def __post_init__(self) -> None:
        _require_absolute_path(self.project_root)
        _require_exact_type(self.plan, PortfolioAdmissionPlan, "plan")
        _require_exact_type(
            self.admission_context, AdmissionContext, "admission_context"
        )
        _require_int(self.recovery_generation, "recovery_generation", 1)


class PortfolioSchedulerTickOutcomeKind(str, Enum):
    IDLE = "idle"
    ADMITTED = "admitted"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class PortfolioSchedulerTickOutcome:
    """Typed outcome for one tick."""

    kind: PortfolioSchedulerTickOutcomeKind
    plan: PortfolioAdmissionPlan
    entry: QueueEntry | None = None
    receipt: ScheduleReceipt | None = None
    recovery_decision: RecoveryDecision | None = None

    def __post_init__(self) -> None:
        _require_exact_type(
            self.kind, PortfolioSchedulerTickOutcomeKind, "kind"
        )
        _require_exact_type(self.plan, PortfolioAdmissionPlan, "plan")
        if self.entry is not None and type(self.entry) is not QueueEntry:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:outcome_entry_type"
            )
        if self.receipt is not None and type(self.receipt) is not ScheduleReceipt:
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:outcome_receipt_type"
            )
        if (
            self.recovery_decision is not None
            and type(self.recovery_decision) is not RecoveryDecision
        ):
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:outcome_recovery_type"
            )


@dataclass(frozen=True, slots=True)
class PortfolioSchedulerTickResult:
    """Frozen result of one tick."""

    outcome: PortfolioSchedulerTickOutcome
    admission_result: PortfolioAdmissionResult | None = None

    def __post_init__(self) -> None:
        _require_exact_type(
            self.outcome, PortfolioSchedulerTickOutcome, "outcome"
        )
        if (
            self.admission_result is not None
            and type(self.admission_result) is not PortfolioAdmissionResult
        ):
            raise PortfolioSchedulerRuntimeInputError(
                "portfolio_scheduler_runtime:result_admission_type"
            )


class PortfolioSchedulerRuntime:
    """One-tick selection-to-admission runtime boundary."""

    __slots__ = ("_project_root", "_store", "_admission")

    def __init__(
        self,
        project_root: Path,
        store: PortfolioSchedulerStore | None = None,
        admission: PortfolioSchedulerAdmission | None = None,
    ) -> None:
        _require_absolute_path(project_root)
        self._project_root = project_root
        self._store = (
            PortfolioSchedulerStore(project_root)
            if store is None
            else store
        )
        self._admission = (
            PortfolioSchedulerAdmission(project_root, store=self._store)
            if admission is None
            else admission
        )

    def tick(
        self, request: PortfolioSchedulerTickRequest
    ) -> PortfolioSchedulerTickResult:
        _require_exact_type(
            request, PortfolioSchedulerTickRequest, "request"
        )
        root = request.project_root
        plan = request.plan
        snapshot = self._store.enumerate_queue_snapshot(root)
        if not snapshot:
            return self._idle(request)

        # A recovered queued entry may retain an older admission-plan file
        # after its selection generation was fenced forward.  Such an entry
        # is not a current reservation, but the pure policy layer cannot see
        # the Git snapshot binding and would otherwise let stale history block
        # every newer plan.  Exclude only entries whose historical plans are
        # provably bound to a different snapshot; leave all durable evidence
        # untouched so a later recovery pass can inspect it explicitly.
        snapshot = self._without_stale_historical_plans(
            snapshot, root, plan.expected_snapshot_commit
        )
        if not snapshot:
            return self._idle(request)

        pending = self._pending_selection_ids(snapshot, root)
        if len(pending) > 1:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:multiple_selected_generations"
            )

        entry = self._find_entry(snapshot, plan.queue_id)
        if entry is None:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:queue_missing"
            )

        if pending:
            if pending[0] != plan.queue_id:
                raise PortfolioSchedulerRuntimeConflictError(
                    "portfolio_scheduler_runtime:plan_queue_mismatch"
                )
            durable_plan = self._read_plan(root, entry)
            receipt = self._read_reservation(root, entry)
            if durable_plan is None and entry.state is QueuePhase.SELECTED:
                return self._recovery_required(root, request, entry, None)
            if durable_plan is not None:
                self._validate_durable_plan(plan, durable_plan)
            if receipt is None:
                if entry.state is not QueuePhase.QUEUED:
                    return self._recovery_required(root, request, entry, None)
                policy_receipt = select_next(snapshot, request.admission_context)
                if policy_receipt is None:
                    return self._idle(request)
                self._validate_policy_receipt(plan, entry, policy_receipt)
                durable_plan, receipt, entry = self._reserve_selection(
                    request, entry, policy_receipt
                )
            elif entry.state is not QueuePhase.SELECTED:
                durable_plan, receipt, entry = self._reserve_selection(
                    request, entry, receipt
                )
            self._validate_reservation(plan, entry, receipt)
            return self._admit(request, entry, receipt)

        if entry.state in (QueuePhase.DISPATCHED, QueuePhase.RETIRED):
            return self._replay_or_recovery(root, request, entry)

        if entry.state is QueuePhase.SELECTED:
            return self._recovery_required(root, request, entry, None)

        if entry.state is not QueuePhase.QUEUED:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:unknown_phase"
            )

        policy_receipt = select_next(snapshot, request.admission_context)
        if policy_receipt is None:
            return self._idle(request)
        self._validate_policy_receipt(plan, entry, policy_receipt)
        _, receipt, entry = self._reserve_selection(
            request, entry, policy_receipt
        )
        return self._admit(request, entry, receipt)

    def _idle(
        self, request: PortfolioSchedulerTickRequest
    ) -> PortfolioSchedulerTickResult:
        outcome = PortfolioSchedulerTickOutcome(
            kind=PortfolioSchedulerTickOutcomeKind.IDLE,
            plan=request.plan,
        )
        return PortfolioSchedulerTickResult(outcome=outcome)

    def _admit(
        self,
        request: PortfolioSchedulerTickRequest,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
    ) -> PortfolioSchedulerTickResult:
        durable_plan = self._read_plan(request.project_root, entry)
        if durable_plan is None:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:admission_plan_missing"
            )
        self._validate_durable_plan(request.plan, durable_plan)
        self._validate_reservation(request.plan, entry, receipt)
        admission_request = self._build_admission_request(
            request.plan, entry, receipt
        )
        admission_result = self._admission.admit(admission_request)
        final_entry = self._store.read_queue_entry(entry.queue_id, request.project_root)
        final_receipt = self._store.read_schedule_receipt(
            receipt.receipt_id, request.project_root
        )
        outcome = PortfolioSchedulerTickOutcome(
            kind=PortfolioSchedulerTickOutcomeKind.ADMITTED,
            plan=request.plan,
            entry=final_entry,
            receipt=final_receipt,
        )
        return PortfolioSchedulerTickResult(
            outcome=outcome, admission_result=admission_result
        )

    def _without_stale_historical_plans(
        self,
        snapshot: tuple[QueueEntry, ...],
        root: Path,
        expected_snapshot_commit: str,
    ) -> tuple[QueueEntry, ...]:
        try:
            historical = self._store.enumerate_validated_admission_plans(root)
        except PortfolioSchedulerNotFoundError:
            historical = ()
        by_queue: dict[str, list[AdmissionPlanReservation]] = {}
        for plan in historical:
            by_queue.setdefault(plan.queue_id, []).append(plan)
        result: list[QueueEntry] = []
        for entry in snapshot:
            if entry.state is not QueuePhase.QUEUED:
                result.append(entry)
                continue
            current_plan = self._read_plan(root, entry)
            if current_plan is not None:
                result.append(entry)
                continue
            prior = by_queue.get(entry.queue_id, ())
            if prior and all(
                item.expected_snapshot_commit != expected_snapshot_commit
                for item in prior
            ):
                continue
            result.append(entry)
        return tuple(result)

    def _replay_or_recovery(
        self,
        root: Path,
        request: PortfolioSchedulerTickRequest,
        entry: QueueEntry,
    ) -> PortfolioSchedulerTickResult:
        try:
            receipt = self._store.read_schedule_receipt(
                request.plan.receipt_id, root
            )
        except PortfolioSchedulerNotFoundError:
            return self._recovery_required(root, request, entry, None)
        if receipt.phase is not ReceiptPhase.DISPATCHED:
            return self._recovery_required(root, request, entry, receipt)
        durable_plan = self._read_plan(root, entry)
        if durable_plan is None:
            return self._recovery_required(root, request, entry, receipt)
        self._validate_durable_plan(request.plan, durable_plan)
        self._validate_reservation(request.plan, entry, receipt)
        return self._admit(request, entry, receipt)

    def _recovery_required(
        self,
        root: Path,
        request: PortfolioSchedulerTickRequest,
        entry: QueueEntry | None,
        receipt: ScheduleReceipt | None,
    ) -> PortfolioSchedulerTickResult:
        if entry is None:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:recovery_entry_missing"
            )
        decision = self._recovery_decision(root, entry, receipt)
        if decision.recovery_action in _RECOVERY_ACTIONS_NOT_EXECUTED:
            outcome = PortfolioSchedulerTickOutcome(
                kind=PortfolioSchedulerTickOutcomeKind.RECOVERY_REQUIRED,
                plan=request.plan,
                entry=entry,
                receipt=receipt,
                recovery_decision=decision,
            )
            return PortfolioSchedulerTickResult(outcome=outcome)
        raise PortfolioSchedulerRuntimeConflictError(
            "portfolio_scheduler_runtime:recovery_not_required"
        )

    def _recovery_decision(
        self,
        root: Path,
        entry: QueueEntry,
        receipt: ScheduleReceipt | None,
    ) -> RecoveryDecision:
        snapshot = StateProvider(root).snapshot()
        task = next(
            (item for item in snapshot.tasks if item.task_id == entry.task_id),
            None,
        )
        if task is None:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:recovery_task_missing"
            )
        request = RecoveryRequest(
            queue_entry=entry,
            canonical_task=task,
            canonical_events=tuple(
                event
                for event in snapshot.events
                if event.task_id == entry.task_id
            ),
            schedule_receipt=receipt,
            dispatch_receipt=None,
            dispatch_tombstone=None,
            lease_snapshot=None,
            recovery_time="",
            policy_version=SCHEMA_VERSION,
            recovery_generation=entry.selection_generation,
            liveness=LivenessEvidence.DEAD,
        )
        return decide_reconciliation(request)

    @staticmethod
    def _find_entry(
        snapshot: tuple[QueueEntry, ...], queue_id: str
    ) -> QueueEntry | None:
        for entry in snapshot:
            if entry.queue_id == queue_id:
                return entry
        return None

    def _pending_selection_ids(
        self,
        snapshot: tuple[QueueEntry, ...],
        root: Path,
    ) -> tuple[str, ...]:
        pending: list[str] = []
        for entry in snapshot:
            if entry.state is QueuePhase.SELECTED:
                pending.append(entry.queue_id)
            elif entry.state is QueuePhase.QUEUED:
                if (
                    self._read_plan(root, entry) is not None
                    or self._read_reservation(root, entry) is not None
                ):
                    pending.append(entry.queue_id)
        return tuple(pending)

    def _read_plan(
        self, root: Path, entry: QueueEntry
    ) -> AdmissionPlanReservation | None:
        try:
            return self._store.read_admission_plan(
                entry.queue_id, entry.selection_generation, root
            )
        except PortfolioSchedulerNotFoundError:
            return None

    def _read_reservation(
        self, root: Path, entry: QueueEntry
    ) -> ScheduleReceipt | None:
        try:
            return self._store.read_reservation(
                entry.queue_id, entry.selection_generation, root
            )
        except PortfolioSchedulerNotFoundError:
            return None

    @staticmethod
    def _durable_plan(
        plan: PortfolioAdmissionPlan,
    ) -> AdmissionPlanReservation:
        reserved_at = plan.now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        provisional = AdmissionPlanReservation(
            schema_version=plan.schema_version,
            queue_id=plan.queue_id,
            receipt_id=plan.receipt_id,
            task_id=plan.task_id,
            revision=plan.revision,
            enqueue_sequence=plan.enqueue_sequence,
            selection_generation=plan.selection_generation,
            worker_kind=plan.worker_kind,
            assessment_id=plan.assessment_id,
            dispatch_id=plan.dispatch_id,
            event_id=plan.event_id,
            outbox_message_id=plan.outbox_message_id,
            role_id=plan.role_id,
            task_card_path=plan.task_card_path,
            task_card_commit=plan.task_card_commit,
            base_commit=plan.base_commit,
            branch=plan.branch,
            report_path=plan.report_path,
            model_selection=plan.model_selection,
            expected_task_state=plan.expected_task_state,
            expected_task_attempt=plan.expected_task_attempt,
            new_attempt=plan.new_attempt,
            expected_snapshot_commit=plan.expected_snapshot_commit,
            policy_version=plan.policy_version,
            holder_instance_id=plan.holder_instance_id,
            canonical_worktree=plan.canonical_worktree,
            reserved_at=reserved_at,
            content_digest="sha256:" + "0" * 64,
        )
        return replace(provisional, content_digest=_plan_digest(provisional))

    @staticmethod
    def _validate_durable_plan(
        plan: PortfolioAdmissionPlan,
        durable: AdmissionPlanReservation,
    ) -> None:
        expected = {
            "schema_version": plan.schema_version,
            "queue_id": plan.queue_id,
            "receipt_id": plan.receipt_id,
            "task_id": plan.task_id,
            "revision": plan.revision,
            "enqueue_sequence": plan.enqueue_sequence,
            "selection_generation": plan.selection_generation,
            "worker_kind": plan.worker_kind,
            "assessment_id": plan.assessment_id,
            "dispatch_id": plan.dispatch_id,
            "event_id": plan.event_id,
            "outbox_message_id": plan.outbox_message_id,
            "role_id": plan.role_id,
            "task_card_path": plan.task_card_path,
            "task_card_commit": plan.task_card_commit,
            "base_commit": plan.base_commit,
            "branch": plan.branch,
            "report_path": plan.report_path,
            "model_selection": plan.model_selection,
            "expected_task_state": plan.expected_task_state,
            "expected_task_attempt": plan.expected_task_attempt,
            "new_attempt": plan.new_attempt,
            "expected_snapshot_commit": plan.expected_snapshot_commit,
            "policy_version": plan.policy_version,
            "holder_instance_id": plan.holder_instance_id,
            "canonical_worktree": plan.canonical_worktree,
        }
        for field_name, expected_value in expected.items():
            if getattr(durable, field_name) != expected_value:
                raise PortfolioSchedulerRuntimeConflictError(
                    "portfolio_scheduler_runtime:durable_plan_identity"
                )

    def _reserve_selection(
        self,
        request: PortfolioSchedulerTickRequest,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
    ) -> tuple[AdmissionPlanReservation, ScheduleReceipt, QueueEntry]:
        durable_plan, durable_receipt, selected_entry = (
            self._store.reserve_admission_selection(
                self._durable_plan(request.plan),
                receipt,
                request.project_root,
            )
        )
        reread_plan = self._store.read_admission_plan(
            selected_entry.queue_id,
            selected_entry.selection_generation,
            request.project_root,
        )
        self._validate_durable_plan(request.plan, reread_plan)
        return reread_plan, durable_receipt, selected_entry

    def _validate_reservation(
        self,
        plan: PortfolioAdmissionPlan,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
    ) -> None:
        if (
            receipt.queue_id != plan.queue_id
            or receipt.queue_id != entry.queue_id
            or receipt.task_id != plan.task_id
            or receipt.task_id != entry.task_id
            or receipt.revision != plan.revision
            or receipt.revision != entry.revision
            or receipt.enqueue_sequence != plan.enqueue_sequence
            or receipt.enqueue_sequence != entry.enqueue_sequence
            or receipt.selection_generation != plan.selection_generation
            or receipt.selection_generation != entry.selection_generation
            or receipt.worker_kind is not plan.worker_kind
            or receipt.worker_kind is not entry.worker_kind_request
            or receipt.receipt_id != plan.receipt_id
        ):
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:reservation_identity"
            )
        if plan.assessment_id != entry.assessment_id:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:assessment_mismatch"
            )
        if plan.policy_version != SCHEMA_VERSION:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:policy_version"
            )
        if receipt.phase not in (ReceiptPhase.SELECTED, ReceiptPhase.DISPATCHED):
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:receipt_phase"
            )
        if receipt.phase is ReceiptPhase.SELECTED:
            if entry.state not in (QueuePhase.QUEUED, QueuePhase.SELECTED):
                raise PortfolioSchedulerRuntimeConflictError(
                    "portfolio_scheduler_runtime:receipt_queue_phase"
                )
        elif entry.state not in (QueuePhase.DISPATCHED, QueuePhase.RETIRED):
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:receipt_queue_phase"
            )

    def _validate_policy_receipt(
        self,
        plan: PortfolioAdmissionPlan,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
    ) -> None:
        if (
            receipt.queue_id != plan.queue_id
            or receipt.task_id != plan.task_id
            or receipt.revision != plan.revision
            or receipt.enqueue_sequence != plan.enqueue_sequence
            or receipt.selection_generation != plan.selection_generation
            or receipt.worker_kind is not plan.worker_kind
            or receipt.receipt_id != plan.receipt_id
            or receipt.queue_id != entry.queue_id
            or receipt.task_id != entry.task_id
            or receipt.revision != entry.revision
            or receipt.enqueue_sequence != entry.enqueue_sequence
            or receipt.selection_generation != entry.selection_generation
            or receipt.worker_kind is not entry.worker_kind_request
            or receipt.phase is not ReceiptPhase.SELECTED
        ):
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:policy_plan_mismatch"
            )
        if plan.assessment_id != entry.assessment_id:
            raise PortfolioSchedulerRuntimeConflictError(
                "portfolio_scheduler_runtime:assessment_mismatch"
            )

    def _build_admission_request(
        self,
        plan: PortfolioAdmissionPlan,
        entry: QueueEntry,
        receipt: ScheduleReceipt,
    ) -> PortfolioAdmissionRequest:
        expected_queue_phase = (
            entry.state
            if entry.state in (QueuePhase.DISPATCHED, QueuePhase.RETIRED)
            else QueuePhase.SELECTED
        )
        return PortfolioAdmissionRequest(
            schema_version=SCHEMA_VERSION,
            queue_id=entry.queue_id,
            receipt_id=receipt.receipt_id,
            task_id=entry.task_id,
            revision=entry.revision,
            enqueue_sequence=entry.enqueue_sequence,
            selection_generation=entry.selection_generation,
            worker_kind=entry.worker_kind_request,
            assessment_id=entry.assessment_id,
            dispatch_id=plan.dispatch_id,
            event_id=plan.event_id,
            outbox_message_id=plan.outbox_message_id,
            role_id=plan.role_id,
            task_card_path=plan.task_card_path,
            task_card_commit=plan.task_card_commit,
            base_commit=plan.base_commit,
            branch=plan.branch,
            report_path=plan.report_path,
            model_selection=plan.model_selection,
            expected_task_state=plan.expected_task_state,
            expected_task_attempt=plan.expected_task_attempt,
            new_attempt=plan.new_attempt,
            expected_snapshot_commit=plan.expected_snapshot_commit,
            expected_queue_phase=expected_queue_phase,
            expected_receipt_phase=receipt.phase,
            expected_queue_digest=entry.content_digest,
            expected_receipt_digest=receipt.content_digest,
            policy_version=plan.policy_version,
            now=plan.now,
            holder_instance_id=plan.holder_instance_id,
            canonical_worktree=plan.canonical_worktree,
        )
