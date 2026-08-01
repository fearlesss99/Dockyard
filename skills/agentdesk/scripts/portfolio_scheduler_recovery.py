"""Pure, read-only, deterministic PortfolioScheduler crash-reconciliation decision core.

Answers "what should be done" after a scheduler crash.  Never writes to
Store, never calls OS liveness probes, never starts a Worker, never calls
a model/provider/API, never reads filesystem/environment/clock.

Implements the frozen crash-recovery matrix from the PortfolioScheduler
durable admission contract (TC-13.24a §8).

All evidence must be explicitly passed via frozen typed RecoveryRequest.
Same input → exact same output (pure, deterministic).

All public types are frozen/slotted with no Any, dict, Mapping, or
mutable collection annotations.  Three external evidence classes are
re-presented as frozen local projections — pure data, no I/O — so the
decision core stays decoupled from dispatch_supervisor_evidence and
worker_slot_lease imports while still consuming exact typed fields.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass

from portfolio_scheduler_store import (
    SCHEMA_VERSION,
    QueuePhase,
    ReceiptPhase,
    QueueEntry,
    ScheduleReceipt,
    _receipt_digest,
)
from state_provider import EventEntry, TaskEntry

__all__ = [
    "RecoveryAction",
    "RecoveryReason",
    "LivenessEvidence",
    "DispatchReceiptEvidence",
    "DispatchTombstoneEvidence",
    "LeaseEvidence",
    "RecoveryRequest",
    "RecoveryDecision",
    "RecoveryError",
    "RecoveryInputError",
    "RecoveryDefenseError",
    "RecoveryDivergenceError",
    "decide_reconciliation",
]

# ═══════════════════════════════════════════════════════════════════════════
# Frozen enums — names from the contract crash-recovery matrix (TC-13.24a §8)
# ═══════════════════════════════════════════════════════════════════════════


class RecoveryAction(str, enum.Enum):
    """Actions ONLY from the contract crash-recovery matrix."""

    RESUME = "resume"
    NO_OP = "no_op"
    INVALIDATE_SELECTION = "invalidate_selection"
    REQUEUE_SELECTED = "requeue_selected"
    ADOPT_CANONICAL_DISPATCH = "adopt_canonical_dispatch"
    RETIRE_QUEUE_ENTRY = "retire_queue_entry"
    WAIT_FOR_EXECUTION_OWNER = "wait_for_execution_owner"
    REJECT = "reject"
    FAIL_CLOSED = "fail_closed"


class RecoveryReason(str, enum.Enum):
    """Frozen reason codes — never free text, never exception classification."""

    QUEUED_NOT_SELECTED = "queued_not_selected"
    WORKER_RUNNING = "worker_running"
    SELECTED_NO_EVENT = "selected_no_event"
    PARTIAL_RECEIPT = "partial_receipt"
    RECEIPT_DIVERGENT = "receipt_divergent"
    CANONICAL_DISPATCHED = "canonical_dispatched"
    CANONICAL_ADVANCED = "canonical_advanced"
    SCHEDULER_ORPHAN = "scheduler_orphan"
    PRE_ACK_CRASH = "pre_ack_crash"
    TOMBSTONE_REPLAY_OK = "tombstone_replay_ok"
    TOMBSTONE_DIVERGENT = "tombstone_divergent"
    LEASE_RESERVED_NO_EVENT = "lease_reserved_no_event"
    LEASE_IDENTITY_DIVERGENCE = "lease_identity_divergence"
    RECEIPT_TOMBSTONE_IDENTITY_DIVERGENCE = "receipt_tombstone_identity_divergence"
    EVIDENCE_IDENTITY_DIVERGENCE = "evidence_identity_divergence"
    ALIVE = "alive"
    UNKNOWN_LIVENESS = "unknown_liveness"
    DEAD = "dead"
    STALE_GENERATION = "stale_generation"
    ATTEMPT_FOUR = "attempt_four"
    DUPLICATE_IDENTITY = "duplicate_identity"
    IMPOSSIBLE_PHASE = "impossible_phase"
    CANONICAL_PRIORITY = "canonical_priority"
    CONCURRENT_REJECT = "concurrent_reject"
    IDENTITY_MISMATCH = "identity_mismatch"
    MULTIPLE_DISPATCH = "multiple_dispatch"
    RETIRED_NO_OP = "retired_no_op"
    FINALIZED_TOMBSTONE_REPLAY = "finalized_tombstone_replay"


class LivenessEvidence(str, enum.Enum):
    """Three-state liveness provided by caller — NOT probed by this module."""

    ALIVE = "alive"
    UNKNOWN = "unknown"
    DEAD = "dead"


# ═══════════════════════════════════════════════════════════════════════════
# Typed exceptions — never free-text classification
# ═══════════════════════════════════════════════════════════════════════════


class RecoveryError(ValueError):
    """Base typed recovery error."""


class RecoveryInputError(RecoveryError):
    """Invalid or malformed typed input."""


class RecoveryDefenseError(RecoveryError):
    """Defense rule violation — duplicate, corrupt, attempt 4, etc."""


class RecoveryDivergenceError(RecoveryError):
    """Evidence divergence — fail-closed."""


# ═══════════════════════════════════════════════════════════════════════════
# Frozen evidence projections — pure data, no I/O, no external imports
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DispatchReceiptEvidence:
    """Frozen projection of dispatch process receipt — identity + phase only.

    Mirrors DispatchProcessReceipt without the I/O-layer fields.
    Every field is typed; no Any, dict, mutable collections, or
    process-handle exposure.
    """

    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    generation_id: str
    phase: str  # DispatchReceiptPhase value string

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_task_id")
        if type(self.revision) is not int or isinstance(self.revision, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_revision")
        if self.revision < 1:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_revision_range")
        if type(self.attempt) is not int or isinstance(self.attempt, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_attempt")
        if self.attempt < 1:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_attempt_range")
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_dispatch_id")
        if not isinstance(self.generation_id, str) or not self.generation_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_generation_id")
        if not isinstance(self.phase, str) or not self.phase:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dre_phase")
        # Validate known phase values
        valid_phases = frozenset({
            "RESERVED", "SUPERVISOR_READY", "WORKER_STARTED",
            "FINALIZING", "FINALIZED",
        })
        if self.phase not in valid_phases:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_phase_unknown")


@dataclass(frozen=True, slots=True)
class DispatchTombstoneEvidence:
    """Frozen projection of dispatch finalizer tombstone — outcome only.

    Mirrors DispatchFinalizerTombstone without I/O-layer fields.
    No handles, no timestamps used as safety conclusions.
    """

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

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_task_id")
        if type(self.revision) is not int or isinstance(self.revision, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_revision")
        if self.revision < 1:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_revision_range")
        if type(self.attempt) is not int or isinstance(self.attempt, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_attempt")
        if self.attempt < 1:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_attempt_range")
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_dispatch_id")
        if not isinstance(self.generation_id, str) or not self.generation_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_generation_id")
        if not isinstance(self.winner, str) or not self.winner:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_winner")
        if not isinstance(self.worker_done, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_worker_done")
        if not isinstance(self.heartbeat_done, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_heartbeat_done")
        if not isinstance(self.release_completed, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:dte_release_completed")
        if self.failure_kind is not None:
            if not isinstance(self.failure_kind, str) or not self.failure_kind:
                raise RecoveryInputError(
                    "portfolio_scheduler_recovery:dte_failure_kind")


@dataclass(frozen=True, slots=True)
class LeaseEvidence:
    """Frozen projection of WorkerSlot lease — identity + binding only.

    No TTL, no expiry-as-safety, no slot-capacity inference.
    """

    lease_id: str
    slot_id: str
    holder_dispatch_id: str
    holder_instance_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or not self.lease_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:le_lease_id")
        if not isinstance(self.slot_id, str) or not self.slot_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:le_slot_id")
        if not isinstance(self.holder_dispatch_id, str) or not self.holder_dispatch_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:le_holder_dispatch_id")
        if not isinstance(self.holder_instance_id, str) or not self.holder_instance_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:le_holder_instance_id")


# ═══════════════════════════════════════════════════════════════════════════
# Frozen request — all evidence explicitly passed in, zero Any
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """All evidence explicitly passed in — pure, no I/O, no probes.

    Every input is typed, frozen, and slotted.  No Any, dict, Mapping,
    or mutable collections.
    """

    # Required scheduler evidence
    queue_entry: QueueEntry
    # Required canonical evidence
    canonical_task: TaskEntry
    canonical_events: tuple[EventEntry, ...]

    # Optional: schedule receipt snapshot
    schedule_receipt: ScheduleReceipt | None = None

    # Optional: durable dispatch supervisor evidence (typed projections)
    dispatch_receipt: DispatchReceiptEvidence | None = None
    dispatch_tombstone: DispatchTombstoneEvidence | None = None

    # Optional: WorkerSlotLease snapshot (typed projection)
    lease_snapshot: LeaseEvidence | None = None

    # Recovery metadata
    recovery_time: str = ""
    policy_version: str = SCHEMA_VERSION
    recovery_generation: int = 1

    # Liveness — provided by caller, NOT probed here
    liveness: LivenessEvidence | None = None

    def __post_init__(self) -> None:
        if type(self.queue_entry) is not QueueEntry:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:queue_entry_type"
            )
        if self.schedule_receipt is not None and type(self.schedule_receipt) is not ScheduleReceipt:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:receipt_type"
            )
        if type(self.canonical_task) is not TaskEntry:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:task_type"
            )
        if type(self.canonical_events) is not tuple:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:events_type"
            )
        for _i, evt in enumerate(self.canonical_events):
            if type(evt) is not EventEntry:
                raise RecoveryInputError(
                    "portfolio_scheduler_recovery:event_entry_type"
                )
        if type(self.policy_version) is not str or not self.policy_version:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:policy_version_type"
            )
        if type(self.recovery_generation) is not int or isinstance(self.recovery_generation, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:generation_type"
            )
        if self.recovery_generation < 1:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:generation_range"
            )
        if type(self.recovery_time) is not str:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:recovery_time_type"
            )
        if self.liveness is not None and type(self.liveness) is not LivenessEvidence:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:liveness_type"
            )

        # Evidence type guards — wrong concrete type → typed reject
        if self.dispatch_receipt is not None:
            if type(self.dispatch_receipt) is not DispatchReceiptEvidence:
                raise RecoveryInputError(
                    "portfolio_scheduler_recovery:dispatch_receipt_type"
                )
        if self.dispatch_tombstone is not None:
            if type(self.dispatch_tombstone) is not DispatchTombstoneEvidence:
                raise RecoveryInputError(
                    "portfolio_scheduler_recovery:dispatch_tombstone_type"
                )
        if self.lease_snapshot is not None:
            if type(self.lease_snapshot) is not LeaseEvidence:
                raise RecoveryInputError(
                    "portfolio_scheduler_recovery:lease_snapshot_type"
                )

        # Policy version must match
        if self.policy_version != SCHEMA_VERSION:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:stale_policy_version"
            )

        # Defense: bool-as-int on queue entry integer fields
        for _field_name in ("revision", "enqueue_sequence", "retry_budget_used",
                            "selection_generation"):
            val = getattr(self.queue_entry, _field_name)
            if isinstance(val, bool):
                raise RecoveryDefenseError(
                    "portfolio_scheduler_recovery:bool_as_int"
                )

        # Defense: attempt 4+ — contract says maximum attempt is 3
        if self.queue_entry.retry_budget_used >= 3:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:attempt_four"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Frozen decision — typed, no free text
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Frozen typed recovery decision.

    Binds the complete evidence context to a single recovery action with
    CAS generation and replay digest.  Every field is typed — no free
    text classification.
    """

    queue_id: str
    receipt_id: str | None
    task_id: str
    revision: int
    enqueue_sequence: int
    observed_queue_phase: str
    observed_receipt_phase: str | None
    canonical_task_state: str
    canonical_dispatch_event_id: str | None
    recovery_action: RecoveryAction
    reason: RecoveryReason
    expected_generation: int
    replay_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.queue_id, str) or not self.queue_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_queue_id"
            )
        if self.receipt_id is not None:
            if not isinstance(self.receipt_id, str) or not self.receipt_id:
                raise RecoveryInputError(
                    "portfolio_scheduler_recovery:decision_receipt_id"
                )
        if not isinstance(self.task_id, str) or not self.task_id:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_task_id"
            )
        if type(self.revision) is not int or isinstance(self.revision, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_revision"
            )
        if type(self.enqueue_sequence) is not int or isinstance(self.enqueue_sequence, bool):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_sequence"
            )
        if not isinstance(self.observed_queue_phase, str):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_queue_phase"
            )
        if self.observed_receipt_phase is not None and not isinstance(
            self.observed_receipt_phase, str
        ):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_receipt_phase"
            )
        if not isinstance(self.canonical_task_state, str):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_task_state"
            )
        if self.canonical_dispatch_event_id is not None and not isinstance(
            self.canonical_dispatch_event_id, str
        ):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_event_id"
            )
        if type(self.recovery_action) is not RecoveryAction:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_action"
            )
        if type(self.reason) is not RecoveryReason:
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_reason"
            )
        if type(self.expected_generation) is not int or isinstance(
            self.expected_generation, bool
        ):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_generation"
            )
        if not isinstance(self.replay_digest, str):
            raise RecoveryInputError(
                "portfolio_scheduler_recovery:decision_digest"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers — pure, deterministic
# ═══════════════════════════════════════════════════════════════════════════


def _find_dispatched_for_attempt(
    events: tuple[EventEntry, ...],
    task_id: str,
    revision: int,
    attempt: int | None,
) -> EventEntry | None:
    """Find the TASK_DISPATCHED event for exact task/revision/attempt."""
    if attempt is None:
        return None
    for evt in events:
        if (
            evt.event_type == "TASK_DISPATCHED"
            and evt.task_id == task_id
            and evt.revision == revision
            and evt.attempt == attempt
        ):
            return evt
    return None


def _has_any_dispatched_event(
    events: tuple[EventEntry, ...],
    task_id: str,
    revision: int,
) -> bool:
    """True if any TASK_DISPATCHED exists for this task/revision."""
    for evt in events:
        if (
            evt.event_type == "TASK_DISPATCHED"
            and evt.task_id == task_id
            and evt.revision == revision
        ):
            return True
    return False


def _compute_replay_digest(
    entry: QueueEntry,
    receipt: ScheduleReceipt | None,
    task: TaskEntry,
    events: tuple[EventEntry, ...],
    recovery_generation: int,
    liveness: LivenessEvidence | None,
    dre: DispatchReceiptEvidence | None,
    dte: DispatchTombstoneEvidence | None,
    lease: LeaseEvidence | None,
    canonical_event_id: str | None = None,
) -> str:
    """Deterministic SHA-256 digest over all input evidence.

    Binds every typed evidence field so any single evidence change
    produces a different digest.  Also binds the canonical dispatch
    event_id when known so that swapping event_id ↔ dispatch_id
    in the decision triggers a digest change.
    """
    h = hashlib.sha256()
    # Queue entry identity
    h.update(b"entry:")
    h.update(entry.queue_id.encode("utf-8"))
    h.update(entry.state.value.encode("utf-8"))
    h.update(str(entry.enqueue_sequence).encode("utf-8"))
    h.update(str(entry.selection_generation).encode("utf-8"))
    h.update(str(entry.retry_budget_used).encode("utf-8"))
    h.update(entry.content_digest.encode("utf-8"))
    # Receipt identity if present
    h.update(b"|receipt:")
    if receipt is not None:
        h.update(receipt.receipt_id.encode("utf-8"))
        h.update(receipt.phase.value.encode("utf-8"))
        h.update(str(receipt.selection_generation).encode("utf-8"))
        h.update(receipt.content_digest.encode("utf-8"))
    else:
        h.update(b"none")
    # Dispatch receipt evidence
    h.update(b"|dre:")
    if dre is not None:
        h.update(dre.task_id.encode("utf-8"))
        h.update(str(dre.revision).encode("utf-8"))
        h.update(str(dre.attempt).encode("utf-8"))
        h.update(dre.dispatch_id.encode("utf-8"))
        h.update(dre.generation_id.encode("utf-8"))
        h.update(dre.phase.encode("utf-8"))
    else:
        h.update(b"none")
    # Dispatch tombstone evidence
    h.update(b"|dte:")
    if dte is not None:
        h.update(dte.task_id.encode("utf-8"))
        h.update(str(dte.revision).encode("utf-8"))
        h.update(str(dte.attempt).encode("utf-8"))
        h.update(dte.dispatch_id.encode("utf-8"))
        h.update(dte.generation_id.encode("utf-8"))
        h.update(dte.winner.encode("utf-8"))
        h.update(b"1" if dte.worker_done else b"0")
        h.update(b"1" if dte.heartbeat_done else b"0")
        h.update(b"1" if dte.release_completed else b"0")
        if dte.failure_kind:
            h.update(dte.failure_kind.encode("utf-8"))
    else:
        h.update(b"none")
    # Lease evidence
    h.update(b"|lease:")
    if lease is not None:
        h.update(lease.lease_id.encode("utf-8"))
        h.update(lease.slot_id.encode("utf-8"))
        h.update(lease.holder_dispatch_id.encode("utf-8"))
        h.update(lease.holder_instance_id.encode("utf-8"))
    else:
        h.update(b"none")
    # Canonical task
    h.update(b"|task:")
    h.update(task.task_id.encode("utf-8"))
    h.update(str(task.revision).encode("utf-8"))
    h.update(task.state.encode("utf-8"))
    h.update(str(task.attempt or 0).encode("utf-8"))
    # Canonical events (sorted by event_id for determinism)
    h.update(b"|events:")
    sorted_evts = sorted(events, key=lambda e: e.event_id)
    for evt in sorted_evts:
        h.update(evt.event_id.encode("utf-8"))
        h.update(evt.event_type.encode("utf-8"))
        h.update(evt.task_id.encode("utf-8"))
        h.update(str(evt.revision).encode("utf-8"))
        h.update(str(evt.attempt or 0).encode("utf-8"))
        if evt.dispatch_id:
            h.update(evt.dispatch_id.encode("utf-8"))
    # Canonical dispatch event_id binding — binds which event is canonical
    h.update(b"|canonical_event:")
    if canonical_event_id is not None:
        h.update(canonical_event_id.encode("utf-8"))
    else:
        h.update(b"none")
    # Recovery metadata
    h.update(b"|recovery:")
    h.update(str(recovery_generation).encode("utf-8"))
    if liveness is not None:
        h.update(b"|liveness:")
        h.update(liveness.value.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def _build_decision(
    entry: QueueEntry,
    receipt: ScheduleReceipt | None,
    task: TaskEntry,
    action: RecoveryAction,
    reason: RecoveryReason,
    expected_gen: int,
    replay_digest: str,
    dispatch_event_id: str | None = None,
    receipt_id_override: str | None = None,
) -> RecoveryDecision:
    """Construct a fully-typed RecoveryDecision."""
    rid = (
        receipt_id_override
        if receipt_id_override is not None
        else (receipt.receipt_id if receipt else None)
    )
    return RecoveryDecision(
        queue_id=entry.queue_id,
        receipt_id=rid,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        observed_queue_phase=entry.state.value,
        observed_receipt_phase=(receipt.phase.value if receipt else None),
        canonical_task_state=task.state,
        canonical_dispatch_event_id=dispatch_event_id,
        recovery_action=action,
        reason=reason,
        expected_generation=expected_gen,
        replay_digest=replay_digest,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Defense validators — fail-fast on corrupt / malicious evidence
# ═══════════════════════════════════════════════════════════════════════════


def _run_defense(request: RecoveryRequest) -> None:
    """Run all defense rules before any recovery decision."""
    entry = request.queue_entry
    receipt = request.schedule_receipt
    task = request.canonical_task
    events = request.canonical_events
    dre = request.dispatch_receipt
    dte = request.dispatch_tombstone
    lease = request.lease_snapshot

    # Bool-as-int on canonical task integer fields
    if isinstance(task.revision, bool):
        raise RecoveryDefenseError(
            "portfolio_scheduler_recovery:task_revision_bool"
        )
    if task.attempt is not None and isinstance(task.attempt, bool):
        raise RecoveryDefenseError(
            "portfolio_scheduler_recovery:task_attempt_bool"
        )

    # task_id / revision must match between entry and canonical task
    if entry.task_id != task.task_id or entry.revision != task.revision:
        raise RecoveryDefenseError(
            "portfolio_scheduler_recovery:identity_mismatch"
        )

    # Unknown queue phase
    if entry.state not in (
        QueuePhase.QUEUED,
        QueuePhase.SELECTED,
        QueuePhase.DISPATCHED,
        QueuePhase.RETIRED,
    ):
        raise RecoveryDefenseError(
            "portfolio_scheduler_recovery:unknown_queue_phase"
        )

    # ── Optional evidence binding to canonical (task_id, revision) ──────

    if dre is not None:
        if dre.task_id != task.task_id or dre.revision != task.revision:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_identity_divergence"
            )
        if task.attempt is not None and dre.attempt != task.attempt:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_attempt_mismatch"
            )

    if dte is not None:
        if dte.task_id != task.task_id or dte.revision != task.revision:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dte_identity_divergence"
            )
        if task.attempt is not None and dte.attempt != task.attempt:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dte_attempt_mismatch"
            )

    # receipt/tombstone mutual consistency
    if dre is not None and dte is not None:
        if dre.task_id != dte.task_id or dre.revision != dte.revision:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_dte_identity_divergence"
            )
        if dre.attempt != dte.attempt:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_dte_attempt_divergence"
            )
        if dre.dispatch_id != dte.dispatch_id:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_dte_dispatch_divergence"
            )
        if dre.generation_id != dte.generation_id:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:dre_dte_generation_divergence"
            )
        # Impossible phase combinations: tombstone exists but receipt not FINALIZED
        # The tombstone signals completion; receipt must be FINALIZED or FINALIZING
        if dre.phase not in ("WORKER_STARTED", "FINALIZING", "FINALIZED"):
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:impossible_dre_dte_phase"
            )

    # lease identity binds to task — validated in decision layer against
    # canonical event where available; defense only checks structural form

    # Impossible phase combinations — receipt vs queue entry
    if receipt is not None:
        if (
            receipt.phase is ReceiptPhase.SELECTED
            and entry.state not in (QueuePhase.QUEUED, QueuePhase.SELECTED)
        ):
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:impossible_phase"
            )
        if (
            receipt.phase is ReceiptPhase.DISPATCHED
            and entry.state not in (QueuePhase.DISPATCHED, QueuePhase.RETIRED)
        ):
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:impossible_phase"
            )
        # receipt must bind to the same queue entry
        if receipt.queue_id != entry.queue_id:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:receipt_queue_mismatch"
            )
        if receipt.task_id != entry.task_id:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:receipt_task_mismatch"
            )
        if receipt.enqueue_sequence != entry.enqueue_sequence:
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:receipt_sequence_mismatch"
            )

    # Multiple TASK_DISPATCHED for one attempt
    seen: set[tuple[str, int, int]] = set()
    for evt in events:
        if evt.event_type == "TASK_DISPATCHED":
            key = (evt.task_id, evt.revision, evt.attempt or 0)
            if key in seen:
                raise RecoveryDefenseError(
                    "portfolio_scheduler_recovery:multiple_dispatch"
                )
            seen.add(key)

    # Mutable collections — conflict_keys must be tuple
    if type(entry.conflict_keys) is not tuple:
        raise RecoveryDefenseError(
            "portfolio_scheduler_recovery:mutable_collection"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Core entry point — pure, read-only, deterministic
# ═══════════════════════════════════════════════════════════════════════════


def decide_reconciliation(request: RecoveryRequest) -> RecoveryDecision:
    """Pure, read-only, deterministic crash-reconciliation decision.

    Returns a typed ``RecoveryDecision`` indicating exactly what should be
    done.  Never writes to Store, never probes processes, never calls
    network/model, never reads filesystem/environment/clock.

    Same input → exact same output (pure deterministic function).
    All error paths use typed exceptions — never free-text classification.
    """

    # ── 0. Defense checks ──────────────────────────────────────────────────
    _run_defense(request)

    entry = request.queue_entry
    receipt = request.schedule_receipt
    task = request.canonical_task
    events = request.canonical_events
    generation = request.recovery_generation
    dre = request.dispatch_receipt
    dte = request.dispatch_tombstone
    lease = request.lease_snapshot

    replay_digest = _compute_replay_digest(
        entry, receipt, task, events, generation, request.liveness,
        dre, dte, lease,
    )

    def decide(
        action: RecoveryAction,
        reason: RecoveryReason,
        expected_gen: int | None = None,
        dispatch_event_id: str | None = None,
        receipt_id_override: str | None = None,
    ) -> RecoveryDecision:
        gen = (
            expected_gen
            if expected_gen is not None
            else entry.selection_generation
        )
        return _build_decision(
            entry,
            receipt,
            task,
            action,
            reason,
            gen,
            replay_digest,
            dispatch_event_id,
            receipt_id_override,
        )

    # ── 1. Liveness evidence (precedes all recovery) ──────────────────────
    if request.liveness is not None:
        live = request.liveness
        if live == LivenessEvidence.ALIVE:
            return decide(RecoveryAction.NO_OP, RecoveryReason.ALIVE)
        if live == LivenessEvidence.UNKNOWN:
            return decide(RecoveryAction.FAIL_CLOSED,
                          RecoveryReason.UNKNOWN_LIVENESS)
        # DEAD — fall through to recovery logic below

    # ── 2. Tombstone-finalized → byte-exact replay check ────────────────
    if dte is not None:
        finished = dte.worker_done and dte.heartbeat_done and dte.release_completed
        if finished:
            # Tombstone says dispatch completed; receipt must align.
            if dre is not None and dre.phase == "FINALIZED":
                # Byte-exact replay: decision is no-op; Orchestrator owns lifecycle.
                return decide(
                    RecoveryAction.RETIRE_QUEUE_ENTRY,
                    RecoveryReason.FINALIZED_TOMBSTONE_REPLAY,
                )
            # Tombstone without matching FINALIZED receipt → divergence
            if dre is not None and dre.phase != "FINALIZED":
                return decide(
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.RECEIPT_TOMBSTONE_IDENTITY_DIVERGENCE,
                )
            # Tombstone present but receipt absent — fail-closed
            return decide(
                RecoveryAction.FAIL_CLOSED,
                RecoveryReason.TOMBSTONE_DIVERGENT,
            )

    # ── 3. Canonical events — highest priority (§9) ──────────────────────

    # CASE: canonical task state shows Worker running (precedes dispatch)
    if task.state == "in_progress":
        # Pre-ACK: dispatch receipt may be WORKER_STARTED or FINALIZING
        return decide(RecoveryAction.NO_OP, RecoveryReason.WORKER_RUNNING)

    canonical_attempt = task.attempt
    dispatched_evt = _find_dispatched_for_attempt(
        events, entry.task_id, entry.revision, canonical_attempt
    )

    # CASE: canonical TASK_DISPATCHED event exists for this attempt
    if dispatched_evt is not None:
        canonical_event_id = dispatched_evt.event_id
        canonical_event_dispatch_id = dispatched_evt.dispatch_id or ""

        # Recompute digest with canonical event_id bound
        digest = _compute_replay_digest(
            entry, receipt, task, events, generation, request.liveness,
            dre, dte, lease, canonical_event_id=canonical_event_id,
        )

        # ── Divergence check: event dispatch_id must match canonical current_dispatch ──
        if task.current_dispatch is not None:
            if task.current_dispatch.dispatch_id != canonical_event_dispatch_id:
                return _build_decision(
                    entry, receipt, task,
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE,
                    entry.selection_generation,
                    digest,
                    dispatch_event_id=None,
                )
            if task.attempt is not None and dispatched_evt.attempt != task.attempt:
                return _build_decision(
                    entry, receipt, task,
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE,
                    entry.selection_generation,
                    digest,
                    dispatch_event_id=None,
                )

        # ── Divergence check: dispatch_receipt dispatch_id must match ──
        if dre is not None and dre.dispatch_id != canonical_event_dispatch_id:
            return _build_decision(
                entry, receipt, task,
                RecoveryAction.FAIL_CLOSED,
                RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE,
                entry.selection_generation,
                digest,
                dispatch_event_id=None,
            )

        # ── Divergence check: tombstone dispatch_id must match ──
        if dte is not None and dte.dispatch_id != canonical_event_dispatch_id:
            return _build_decision(
                entry, receipt, task,
                RecoveryAction.FAIL_CLOSED,
                RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE,
                entry.selection_generation,
                digest,
                dispatch_event_id=None,
            )

        # ── Divergence check: lease holder_dispatch_id must match ──
        if lease is not None and lease.holder_dispatch_id != canonical_event_dispatch_id:
            return _build_decision(
                entry, receipt, task,
                RecoveryAction.FAIL_CLOSED,
                RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE,
                entry.selection_generation,
                digest,
                dispatch_event_id=None,
            )

        if entry.state in (QueuePhase.QUEUED, QueuePhase.SELECTED):
            # Scheduler evidence lags canonical — align to dispatched
            return _build_decision(
                entry, receipt, task,
                RecoveryAction.ADOPT_CANONICAL_DISPATCH,
                RecoveryReason.CANONICAL_ADVANCED,
                entry.selection_generation,
                digest,
                dispatch_event_id=canonical_event_id,
            )

        # dispatched / retired — canonical dispatcher owns lifecycle
        return _build_decision(
            entry, receipt, task,
            RecoveryAction.RETIRE_QUEUE_ENTRY,
            RecoveryReason.CANONICAL_DISPATCHED,
            entry.selection_generation,
            digest,
            dispatch_event_id=canonical_event_id,
        )

    # CASE: canonical task state shows dispatch but NO matching TASK_DISPATCHED event
    has_dispatch_state = task.state in ("dispatched", "review_ready")
    has_dispatch_info = task.current_dispatch is not None

    if has_dispatch_state and has_dispatch_info:
        # Frozen crash matrix: task shows dispatch but no matching event.
        # MUST NOT return ADOPT_CANONICAL_DISPATCH (no event to adopt).
        # MUST NOT fill event-id field with dispatch_id.
        # MUST return WAIT_FOR_EXECUTION_OWNER or FAIL_CLOSED.
        #
        # Rationale: without a canonical TASK_DISPATCHED event we cannot
        # prove dispatch happened at this attempt.  The execution owner
        # (Orchestrator) must resolve; we fail-closed until then.
        return decide(
            RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
            RecoveryReason.PRE_ACK_CRASH,
        )

    # CASE: Pre-ACK crash (task dispatched, no ACK observed, no current_dispatch)
    if task.state == "dispatched":
        return decide(
            RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
            RecoveryReason.PRE_ACK_CRASH,
        )

    # ── 4. Lease evidence — reserved before dispatch ────────────────────
    if lease is not None:
        # Lease exists but no canonical dispatch → lease reserved, no event
        if not has_dispatch_state and not has_dispatch_info:
            if entry.state in (QueuePhase.QUEUED, QueuePhase.SELECTED):
                return decide(
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.LEASE_RESERVED_NO_EVENT,
                )

    # CASE: Scheduler shows dispatched but canonical has no evidence (orphan)
    if entry.state == QueuePhase.DISPATCHED:
        has_canonical_dispatch = _has_any_dispatched_event(
            events, entry.task_id, entry.revision
        )
        if not has_canonical_dispatch and not has_dispatch_state:
            return decide(
                RecoveryAction.FAIL_CLOSED, RecoveryReason.SCHEDULER_ORPHAN
            )

    # ── 5. Decision by queue phase ───────────────────────────────────────

    # queued, not selected → resume
    if entry.state == QueuePhase.QUEUED:
        return decide(
            RecoveryAction.RESUME, RecoveryReason.QUEUED_NOT_SELECTED
        )

    # selected, no TASK_DISPATCHED → invalidate + requeue
    if entry.state == QueuePhase.SELECTED:
        if receipt is None:
            return decide(
                RecoveryAction.INVALIDATE_SELECTION,
                RecoveryReason.SELECTED_NO_EVENT,
            )

        if receipt.phase == ReceiptPhase.SELECTED:
            expected = _receipt_digest(receipt)
            if receipt.content_digest != expected:
                return decide(
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.PARTIAL_RECEIPT,
                )
            return decide(
                RecoveryAction.REQUEUE_SELECTED,
                RecoveryReason.SELECTED_NO_EVENT,
                expected_gen=entry.selection_generation,
            )

        if receipt.phase == ReceiptPhase.DISPATCHED:
            if not has_dispatch_state:
                return decide(
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.RECEIPT_DIVERGENT,
                )
            # Receipt has dispatch_event_id — use it (already validated as EVT- prefix)
            return decide(
                RecoveryAction.ADOPT_CANONICAL_DISPATCH,
                RecoveryReason.CANONICAL_DISPATCHED,
                dispatch_event_id=receipt.dispatch_event_id,
            )

    # dispatched — canonical handled above; orphan handled above
    if entry.state == QueuePhase.DISPATCHED:
        return decide(
            RecoveryAction.RETIRE_QUEUE_ENTRY,
            RecoveryReason.CANONICAL_DISPATCHED,
        )

    # retired — nothing to do
    if entry.state == QueuePhase.RETIRED:
        return decide(RecoveryAction.NO_OP, RecoveryReason.RETIRED_NO_OP)

    raise RecoveryDivergenceError(
        "portfolio_scheduler_recovery:unhandled_case"
    )
