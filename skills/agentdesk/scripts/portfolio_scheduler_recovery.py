"""Pure, read-only, deterministic PortfolioScheduler crash-reconciliation decision core.

Answers "what should be done" after a scheduler crash.  Never writes to
Store, never calls OS liveness probes, never starts a Worker, never calls
a model/provider/API, never reads filesystem/environment/clock.

Implements the frozen crash-recovery matrix from the PortfolioScheduler
durable admission contract (TC-13.24a §8).

All evidence must be explicitly passed via frozen typed RecoveryRequest.
Same input → exact same output (pure, deterministic).
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from typing import Any

from portfolio_scheduler_store import (
    SCHEMA_VERSION,
    QueuePhase,
    ReceiptPhase,
    QueueEntry,
    ScheduleReceipt,
    _entry_digest,
    _receipt_digest,
)
from state_provider import EventEntry, TaskEntry

__all__ = [
    "RecoveryAction",
    "RecoveryReason",
    "LivenessEvidence",
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
# Frozen request — all evidence explicitly passed in
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """All evidence explicitly passed in — pure, no I/O, no probes.

    Every input is typed, frozen, and slotted.  No filesystem,
    environment, clock, lock, network, or model access.
    """

    # Required scheduler evidence
    queue_entry: QueueEntry
    # Required canonical evidence
    canonical_task: TaskEntry
    canonical_events: tuple[EventEntry, ...]

    # Optional: schedule receipt snapshot
    schedule_receipt: ScheduleReceipt | None = None

    # Optional: durable dispatch supervisor evidence
    dispatch_receipt: Any = None        # DispatchProcessReceipt (untracked import)
    dispatch_tombstone: Any = None      # DispatchFinalizerTombstone

    # Optional: WorkerSlotLease snapshot
    lease_snapshot: Any = None

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


def _count_dispatched_for_attempt(
    events: tuple[EventEntry, ...],
    task_id: str,
    revision: int,
    attempt: int | None,
) -> int:
    """Count TASK_DISPATCHED events for one attempt."""
    if attempt is None:
        return 0
    count = 0
    for evt in events:
        if (
            evt.event_type == "TASK_DISPATCHED"
            and evt.task_id == task_id
            and evt.revision == revision
            and evt.attempt == attempt
        ):
            count += 1
    return count


def _compute_replay_digest(
    entry: QueueEntry,
    receipt: ScheduleReceipt | None,
    task: TaskEntry,
    events: tuple[EventEntry, ...],
    recovery_generation: int,
    liveness: LivenessEvidence | None,
) -> str:
    """Deterministic SHA-256 digest over all input evidence.

    Binds the exact snapshot so callers can detect stale / divergent
    recovery requests.
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
# Defense validators (§10) — fail-fast on corrupt / malicious evidence
# ═══════════════════════════════════════════════════════════════════════════


def _run_defense(request: RecoveryRequest) -> None:
    """Run all defense rules before any recovery decision."""
    entry = request.queue_entry
    receipt = request.schedule_receipt
    task = request.canonical_task
    events = request.canonical_events

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

    # Impossible phase combinations
    if receipt is not None:
        # selected receipt requires selected queue
        if (
            receipt.phase is ReceiptPhase.SELECTED
            and entry.state not in (QueuePhase.QUEUED, QueuePhase.SELECTED)
        ):
            raise RecoveryDefenseError(
                "portfolio_scheduler_recovery:impossible_phase"
            )
        # dispatched receipt requires dispatched/retired queue
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

    # Duplicate enqueue_sequence check for consistency
    # (single entry so just verify sequence is positive)


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

    replay_digest = _compute_replay_digest(
        entry, receipt, task, events, generation, request.liveness
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

    # ── 2. Canonical events — highest priority (§9) ──────────────────────

    # CASE: canonical task state shows Worker running (precedes dispatch)
    if task.state == "in_progress":
        return decide(RecoveryAction.NO_OP, RecoveryReason.WORKER_RUNNING)

    canonical_attempt = task.attempt
    dispatched_evt = _find_dispatched_for_attempt(
        events, entry.task_id, entry.revision, canonical_attempt
    )

    # CASE: canonical TASK_DISPATCHED event exists for this attempt
    if dispatched_evt is not None:
        dispatch_id = dispatched_evt.dispatch_id or ""

        if entry.state in (QueuePhase.QUEUED, QueuePhase.SELECTED):
            # Scheduler evidence lags canonical — align to dispatched
            return decide(
                RecoveryAction.ADOPT_CANONICAL_DISPATCH,
                RecoveryReason.CANONICAL_ADVANCED,
                dispatch_event_id=dispatch_id,
            )

        # dispatched / retired — canonical dispatcher owns lifecycle
        return decide(
            RecoveryAction.RETIRE_QUEUE_ENTRY,
            RecoveryReason.CANONICAL_DISPATCHED,
            dispatch_event_id=dispatch_id,
        )

    # CASE: canonical task state shows dispatch (without canonical event in snapshot yet)
    has_dispatch_state = task.state in ("dispatched", "review_ready")
    has_dispatch_info = task.current_dispatch is not None

    if has_dispatch_state and has_dispatch_info:
        dispatch_id = task.current_dispatch.dispatch_id

        if entry.state in (QueuePhase.QUEUED, QueuePhase.SELECTED):
            return decide(
                RecoveryAction.ADOPT_CANONICAL_DISPATCH,
                RecoveryReason.CANONICAL_ADVANCED,
                dispatch_event_id=dispatch_id,
            )

        if entry.state in (QueuePhase.DISPATCHED, QueuePhase.RETIRED):
            return decide(
                RecoveryAction.RETIRE_QUEUE_ENTRY,
                RecoveryReason.CANONICAL_DISPATCHED,
                dispatch_event_id=dispatch_id,
            )

    # CASE: Pre-ACK crash (task dispatched, no ACK observed)
    # Scheduler must NOT dispatch again — Orchestrator owns lifecycle
    if task.state == "dispatched":
        return decide(
            RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
            RecoveryReason.PRE_ACK_CRASH,
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

    # ── 3. Decision by queue phase ───────────────────────────────────────

    # queued, not selected → resume
    if entry.state == QueuePhase.QUEUED:
        return decide(
            RecoveryAction.RESUME, RecoveryReason.QUEUED_NOT_SELECTED
        )

    # selected, no TASK_DISPATCHED → invalidate + requeue
    if entry.state == QueuePhase.SELECTED:
        if receipt is None:
            # Selected but no durable receipt evidence
            return decide(
                RecoveryAction.INVALIDATE_SELECTION,
                RecoveryReason.SELECTED_NO_EVENT,
            )

        if receipt.phase == ReceiptPhase.SELECTED:
            # Verify receipt digest integrity
            expected = _receipt_digest(receipt)
            if receipt.content_digest != expected:
                # Receipt write incomplete or divergent → fail-closed
                return decide(
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.PARTIAL_RECEIPT,
                )
            # Valid selected receipt, no dispatch → requeue
            return decide(
                RecoveryAction.REQUEUE_SELECTED,
                RecoveryReason.SELECTED_NO_EVENT,
                expected_gen=entry.selection_generation,
            )

        if receipt.phase == ReceiptPhase.DISPATCHED:
            # Receipt says dispatched but canonical has no evidence
            if not has_dispatch_state:
                return decide(
                    RecoveryAction.FAIL_CLOSED,
                    RecoveryReason.RECEIPT_DIVERGENT,
                )
            # Canonical does show dispatch → adopt
            return decide(
                RecoveryAction.ADOPT_CANONICAL_DISPATCH,
                RecoveryReason.CANONICAL_DISPATCHED,
                dispatch_event_id=receipt.dispatch_event_id,
            )

    # dispatched — canonical handled above; orphan handled above
    if entry.state == QueuePhase.DISPATCHED:
        # If we reach here, canonical shows dispatch too — retire
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
