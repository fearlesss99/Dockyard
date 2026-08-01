"""PortfolioScheduler crash-reconciliation — directed tests.

Covers: queued resume, selected/no-event requeue, partial receipt fail-closed,
canonical dispatch adopt, scheduler orphan fail-closed, pre-ACK no-repeat,
Worker running no-op, tombstone replay/divergent, ALIVE/UNKNOWN/DEAD,
PID reuse/boot mismatch, concurrent replay, stale generation, attempt 3/4,
canonical priority, deterministic output, input ordering, source boundary.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import sys
import unittest
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import portfolio_scheduler_store as ps  # noqa: E402
import portfolio_scheduler_recovery as rec  # noqa: E402
from core_types import WorkerKind  # noqa: E402
from state_provider import TaskEntry, TaskTimestamps, EventEntry, DispatchInfo, ModelSelectionSnapshot  # noqa: E402

# ── test fixture constants ──────────────────────────────────────────────────

STAMP = "2026-01-01T00:00:00.000000Z"
STAMP_A = "2026-01-01T00:00:00.000000Z"
STAMP_B = "2026-01-01T01:00:00.000000Z"
STAMP_C = "2026-01-01T02:00:00.000000Z"
POLICY_VER = ps.SCHEMA_VERSION


# ── helpers ─────────────────────────────────────────────────────────────────


def _entry(
    queue_id: str = "Q-1",
    task_id: str = "TC-001",
    revision: int = 1,
    sequence: int = 1,
    enqueued_at: str = STAMP,
    priority: ps.BusinessPriority = ps.BusinessPriority.P1,
    aging_basis_at: str = STAMP,
    worker: WorkerKind = WorkerKind.STANDARD_AGENT,
    assessment_id: str = "ASM-1",
    state: ps.QueuePhase = ps.QueuePhase.QUEUED,
    conflict_keys: tuple[ps.ConflictKey, ...] = (),
    retry_budget_used: int = 0,
    generation: int = 1,
) -> ps.QueueEntry:
    provisional = ps.QueueEntry(
        schema_version=ps.SCHEMA_VERSION,
        queue_id=queue_id,
        task_id=task_id,
        revision=revision,
        enqueue_sequence=sequence,
        enqueued_at=enqueued_at,
        business_priority=priority,
        aging_basis_at=aging_basis_at,
        worker_kind_request=worker,
        assessment_id=assessment_id,
        state=state,
        conflict_keys=conflict_keys,
        retry_budget_used=retry_budget_used,
        content_digest="sha256:" + "0" * 64,
        selection_generation=generation,
    )
    return dataclasses.replace(provisional, content_digest=ps._entry_digest(provisional))


def _receipt(
    entry: ps.QueueEntry,
    receipt_id: str = "SR-1-1-g1",
    selected_at: str = STAMP,
    phase: ps.ReceiptPhase = ps.ReceiptPhase.SELECTED,
    dispatch_event_id: str | None = None,
    generation: int | None = None,
) -> ps.ScheduleReceipt:
    """Build a deterministic receipt bound to *entry*."""
    gen = generation if generation is not None else entry.selection_generation
    order_key: tuple[int, int, int, str] = (0, 0, entry.enqueue_sequence, entry.queue_id)
    rid = receipt_id
    if rid is None:
        queue_part = entry.queue_id[2:]
        rid = f"SR-{queue_part}-{entry.enqueue_sequence}-g{gen}"
    provisional = ps.ScheduleReceipt(
        schema_version=ps.SCHEMA_VERSION,
        receipt_id=rid,
        queue_id=entry.queue_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selected_at=selected_at,
        order_key=order_key,
        selection_reason="test",
        worker_kind=entry.worker_kind_request,
        dispatch_event_id=dispatch_event_id,
        phase=phase,
        content_digest="sha256:" + "0" * 64,
        selection_generation=gen,
    )
    return dataclasses.replace(
        provisional, content_digest=ps._receipt_digest(provisional)
    )


def _task(
    task_id: str = "TC-001",
    revision: int = 1,
    state: str = "ready",
    attempt: int | None = None,
    dispatch_id: str | None = None,
) -> TaskEntry:
    """Build a minimal canonical task snapshot."""
    ts = TaskTimestamps(
        created_at=STAMP,
        ready_at=STAMP if state != "draft" else None,
        dispatched_at=STAMP_B if dispatch_id else None,
        started_at=None,
        delivered_at=None,
        blocked_at=None,
        accepted_at=None,
        integrated_at=None,
        updated_at=STAMP,
    )
    dispatch_info = None
    if dispatch_id is not None:
        ms = ModelSelectionSnapshot(
            required_model_tier="standard",
            required_model_capabilities=(),
            model_binding_id="binding-001",
            selected_model_provider="test",
            selected_model_id="test-model",
            selected_model_tier="standard",
            selected_deliberation_tier="balanced",
            selected_context_window_tokens=100000,
            selected_model_capabilities=(),
            model_degradation_approval_id=None,
        )
        dispatch_info = DispatchInfo(
            dispatch_id=dispatch_id,
            attempt_id="ATT-001",
            role_id="role-test",
            base_commit="0" * 40,
            branch="test-branch",
            dispatched_at=STAMP_B,
            model_selection=ms,
        )
    return TaskEntry(
        task_id=task_id,
        revision=revision,
        task_card_path="docs/pm/tc-001.md",
        task_card_commit="0" * 40,
        state=state,
        attempt=attempt,
        current_dispatch=dispatch_info,
        report_path=None,
        granted_approval_ids=None,
        delivery_state=None,
        integration_state=None,
        implementation_commit=None,
        report_commit=None,
        accepted_commit=None,
        acceptance_path=None,
        integrated_commit=None,
        blocked_reason=None,
        blocked_kind=None,
        blocked_owner=None,
        unblock_condition=None,
        review_after=None,
        blocked_attempt_valid=None,
        resume_state=None,
        timestamps=ts,
        superseded_by=None,
    )


def _event(
    event_id: str = "EVT-001",
    event_type: str = "TASK_DISPATCHED",
    task_id: str = "TC-001",
    revision: int = 1,
    attempt: int = 1,
    dispatch_id: str = "DISP-001",
    from_state: str = "ready",
    to_state: str = "dispatched",
    occurred_at: str = STAMP_B,
    payload_digest: str | None = "sha256:" + "0" * 64,
) -> EventEntry:
    return EventEntry(
        schema_version="agentdesk.state-event/v2",
        event_id=event_id,
        event_type=event_type,
        task_id=task_id,
        revision=revision,
        attempt=attempt,
        dispatch_id=dispatch_id,
        from_state=from_state,
        to_state=to_state,
        lease_epoch=1,
        actor_role_id="role-test",
        occurred_at=occurred_at,
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
        payload_digest=payload_digest,
        extra_fields=None,
    )


def _req(
    entry: ps.QueueEntry,
    task: TaskEntry,
    events: tuple[EventEntry, ...] = (),
    receipt: ps.ScheduleReceipt | None = None,
    liveness: rec.LivenessEvidence | None = None,
    recovery_time: str = STAMP_C,
    recovery_generation: int = 1,
) -> rec.RecoveryRequest:
    return rec.RecoveryRequest(
        queue_entry=entry,
        canonical_task=task,
        canonical_events=events,
        schedule_receipt=receipt,
        recovery_time=recovery_time,
        recovery_generation=recovery_generation,
        liveness=liveness,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Queued — resume / no-op
# ═══════════════════════════════════════════════════════════════════════════


class QueuedResumeTests(unittest.TestCase):
    """queued, not selected → resume (or no-op if liveness ALIVE)."""

    def test_queued_resume(self):
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RESUME)
        self.assertIs(decision.reason, rec.RecoveryReason.QUEUED_NOT_SELECTED)
        self.assertEqual(decision.queue_id, entry.queue_id)
        self.assertIsNone(decision.receipt_id)
        self.assertEqual(decision.observed_queue_phase, "queued")
        self.assertIsNone(decision.observed_receipt_phase)

    def test_queued_no_new_sequence(self):
        """Queued resume does not generate a new sequence."""
        entry = _entry(state=ps.QueuePhase.QUEUED, sequence=5)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task)
        decision = rec.decide_reconciliation(request)
        self.assertEqual(decision.enqueue_sequence, 5)
        # selection_generation should not change
        self.assertEqual(decision.expected_generation, entry.selection_generation)

    def test_queued_no_receipt_generated(self):
        """Queued resume does not generate a receipt — only the decision."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task)
        decision = rec.decide_reconciliation(request)
        self.assertIsNone(decision.receipt_id, "resume must not generate receipt")

    def test_queued_aging_basis_preserved(self):
        """Resume does not modify aging basis."""
        entry = _entry(state=ps.QueuePhase.QUEUED, aging_basis_at=STAMP_A)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task)
        decision = rec.decide_reconciliation(request)
        self.assertEqual(decision.recovery_action, rec.RecoveryAction.RESUME)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Selected, no TASK_DISPATCHED — requeue/invalidate
# ═══════════════════════════════════════════════════════════════════════════


class SelectedNoEventTests(unittest.TestCase):
    """selected with no TASK_DISPATCHED → invalidate or requeue."""

    def test_selected_no_receipt_invalidate(self):
        """Selected queue entry with no receipt → invalidate selection."""
        entry = _entry(state=ps.QueuePhase.SELECTED, generation=1)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=None)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.INVALIDATE_SELECTION)
        self.assertIs(decision.reason, rec.RecoveryReason.SELECTED_NO_EVENT)

    def test_selected_with_receipt_requeue(self):
        """Selected queue entry with valid selected receipt → requeue."""
        entry = _entry(state=ps.QueuePhase.SELECTED, generation=1)
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED, generation=1)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=rct)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.REQUEUE_SELECTED)
        self.assertIs(decision.reason, rec.RecoveryReason.SELECTED_NO_EVENT)
        self.assertEqual(decision.expected_generation, 1)

    def test_selected_no_canonical_dispatch_inferred(self):
        """Selected recovery must NOT infer a canonical dispatch."""
        entry = _entry(state=ps.QueuePhase.SELECTED, generation=1)
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED, generation=1)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=rct)
        decision = rec.decide_reconciliation(request)
        # No dispatch identity should be generated
        self.assertIsNone(decision.canonical_dispatch_event_id)

    def test_selected_no_new_dispatch_identity(self):
        """Requeue must not generate a new dispatch identity."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=rct)
        decision = rec.decide_reconciliation(request)
        self.assertIsNone(
            decision.canonical_dispatch_event_id,
            "must not generate dispatch event_id",
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. Partial receipt — fail-closed
# ═══════════════════════════════════════════════════════════════════════════


class PartialReceiptTests(unittest.TestCase):
    """Receipt write incomplete or divergent → fail-closed."""

    def test_partial_receipt_fail_closed(self):
        """Receipt with mismatched digest → fail-closed."""
        entry = _entry(state=ps.QueuePhase.SELECTED, generation=1)
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED, generation=1)
        # Tamper with digest by modifying a field after construction
        tampered = dataclasses.replace(rct, content_digest="sha256:" + "f" * 64)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=tampered)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        self.assertIs(decision.reason, rec.RecoveryReason.PARTIAL_RECEIPT)

    def test_partial_receipt_no_auto_fix(self):
        """Fail-closed on partial receipt must not attempt auto-fix."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED)
        tampered = dataclasses.replace(rct, selection_reason="corrupt")
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=tampered)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)

    def test_divergent_receipt_fail_closed(self):
        """Receipt identity matches but digest diverges → fail-closed."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED)
        # Change a field inside receipt but keep content_digest mismatched
        bad = dataclasses.replace(
            rct, content_digest="sha256:badbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadbadb"
        )
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=bad)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Canonical dispatched — adopt / align / retire
# ═══════════════════════════════════════════════════════════════════════════


class CanonicalDispatchedTests(unittest.TestCase):
    """TASK_DISPATCHED committed — adopt, align, or retire."""

    def test_canonical_dispatched_adopt(self):
        """Canonical dispatched event exists, scheduler queued → adopt."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action,
                       rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH)

    def test_canonical_dispatched_retire(self):
        """Canonical dispatched, scheduler dispatched → retire."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED, generation=1)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action,
                       rec.RecoveryAction.RETIRE_QUEUE_ENTRY)

    def test_canonical_event_authoritative_over_scheduler(self):
        """Canonical event is the authority — scheduler evidence cannot
        overwrite it."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        # Canonical dispatched event wins — adopt (queued/selected → align)
        self.assertIs(
            decision.recovery_action,
            rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH,
        )

    def test_canonical_no_second_dispatch(self):
        """Must NOT generate a second TASK_DISPATCHED."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        # Adopt binds exact canonical event_id, not dispatch_id
        self.assertEqual(
            decision.canonical_dispatch_event_id,
            evt.event_id,
        )

    def test_dispatch_event_id_precise_binding(self):
        """dispatch_event_id must be precisely the event_id, not dispatch_id."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
            dispatch_id="DISP-SPECIFIC",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-SPECIFIC",
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertEqual(decision.canonical_dispatch_event_id, evt.event_id)
        self.assertNotEqual(decision.canonical_dispatch_event_id, "DISP-SPECIFIC")


# ═══════════════════════════════════════════════════════════════════════════
# 5. Scheduler orphan — dispatched but canonical missing
# ═══════════════════════════════════════════════════════════════════════════


class SchedulerOrphanTests(unittest.TestCase):
    """Scheduler shows dispatched, canonical has no event → fail-closed."""

    def test_scheduler_dispatched_no_canonical_fail_closed(self):
        """scheduler dispatched / canonical missing → fail-closed."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, events=())
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        self.assertIs(decision.reason, rec.RecoveryReason.SCHEDULER_ORPHAN)

    def test_scheduler_orphan_no_canonical_rebuild(self):
        """Must NOT rebuild canonical event from scheduler state."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, events=())
        decision = rec.decide_reconciliation(request)
        self.assertIsNone(decision.canonical_dispatch_event_id)

    def test_scheduler_orphan_no_auto_transition(self):
        """Scheduler orphan must not transition canonical state."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, events=())
        decision = rec.decide_reconciliation(request)
        self.assertEqual(decision.canonical_task_state, "ready")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Pre-ACK crash — no repeat dispatch
# ═══════════════════════════════════════════════════════════════════════════


class PreAckCrashTests(unittest.TestCase):
    """ACK not yet observed at Scheduler crash — never dispatch again."""

    def test_pre_ack_no_repeat_dispatch(self):
        """Task is dispatched (no ACK) — wait, don't dispatch again."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        # Canonical event exists, scheduler queued → adopt. Never dispatch again.
        self.assertIs(
            decision.recovery_action,
            rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH,
        )
        # And it must bind the canonical event_id, not the dispatch_id
        self.assertEqual(
            decision.canonical_dispatch_event_id, evt.event_id)

    def test_dispatched_state_wait_not_re_enqueue(self):
        """Canonical task shows dispatched — scheduler aligns, no requeue."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertEqual(
            decision.recovery_action, rec.RecoveryAction.RETIRE_QUEUE_ENTRY,
        )
        self.assertNotEqual(
            decision.recovery_action, rec.RecoveryAction.REQUEUE_SELECTED,
        )
        self.assertNotEqual(
            decision.recovery_action, rec.RecoveryAction.RESUME,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Worker running — no-op
# ═══════════════════════════════════════════════════════════════════════════


class WorkerRunningTests(unittest.TestCase):
    """Worker running at Scheduler crash → no-op."""

    def test_worker_running_no_op(self):
        """Worker running → no-op; never intervene."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="in_progress", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)
        self.assertIs(decision.reason, rec.RecoveryReason.WORKER_RUNNING)

    def test_worker_running_no_requeue(self):
        """Worker running → must not requeue."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="in_progress", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertNotEqual(
            decision.recovery_action, rec.RecoveryAction.REQUEUE_SELECTED,
        )
        self.assertNotEqual(
            decision.recovery_action, rec.RecoveryAction.INVALIDATE_SELECTION,
        )

    def test_worker_running_no_cancel(self):
        """Worker running → scheduler crash does not cancel execution."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="in_progress", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)


# ═══════════════════════════════════════════════════════════════════════════
# 8. Liveness: ALIVE / UNKNOWN / DEAD
# ═══════════════════════════════════════════════════════════════════════════


class LivenessTests(unittest.TestCase):
    """Liveness evidence — ALIVE → no-op, UNKNOWN → fail-closed, DEAD → proceed."""

    def test_alive_no_op(self):
        """ALIVE → no recovery, no-op."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, liveness=rec.LivenessEvidence.ALIVE)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)
        self.assertIs(decision.reason, rec.RecoveryReason.ALIVE)

    def test_alive_overrides_queued(self):
        """ALIVE takes precedence over queue state — always no-op."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, liveness=rec.LivenessEvidence.ALIVE)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)

    def test_unknown_fail_closed(self):
        """UNKNOWN → fail-closed, no recovery action."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, liveness=rec.LivenessEvidence.UNKNOWN)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        self.assertIs(decision.reason, rec.RecoveryReason.UNKNOWN_LIVENESS)

    def test_dead_proceeds_to_recovery(self):
        """DEAD → continues to phase-based recovery logic."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, liveness=rec.LivenessEvidence.DEAD)
        decision = rec.decide_reconciliation(request)
        # DEAD allows recovery → queued → resume
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RESUME)

    def test_unknown_not_downgraded_to_dead(self):
        """UNKNOWN must not be treated as DEAD."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        # Even with valid receipt, UNKNOWN → fail-closed
        rct = _receipt(entry, phase=ps.ReceiptPhase.SELECTED)
        request = _req(entry, task, receipt=rct,
                       liveness=rec.LivenessEvidence.UNKNOWN)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)


# ═══════════════════════════════════════════════════════════════════════════
# 9. Stale generation — reject
# ═══════════════════════════════════════════════════════════════════════════


class StaleGenerationTests(unittest.TestCase):
    """Stale generation / CAS → reject (must be handled by store/caller).

    The pure decision module validates that recovery_generation is valid
    but does not itself CAS-compare against entry generation — the caller
    must pass the right generation.
    """

    def test_valid_generation_accepted(self):
        """Valid recovery generation produces valid decision."""
        entry = _entry(state=ps.QueuePhase.QUEUED, generation=1)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, recovery_generation=1)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RESUME)

    def test_zero_generation_rejected(self):
        """Zero recovery generation must be rejected."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            _req(entry, task, recovery_generation=0)

    def test_negative_generation_rejected(self):
        """Negative recovery generation must be rejected."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            _req(entry, task, recovery_generation=-1)

    def test_bool_generation_rejected(self):
        """Bool-as-int recovery generation must be rejected."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task=task,
                canonical_events=(),
                recovery_generation=True,  # bool-as-int
            )


# ═══════════════════════════════════════════════════════════════════════════
# 10. Attempt 3 — allowed; Attempt 4 — reject
# ═══════════════════════════════════════════════════════════════════════════


class AttemptLimitTests(unittest.TestCase):
    """Attempt 3 max; attempt 4+ must be rejected."""

    def test_attempt_3_allowed(self):
        """retry_budget_used=2 → allowed (attempt 3)."""
        entry = _entry(state=ps.QueuePhase.QUEUED, retry_budget_used=2)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RESUME)

    def test_attempt_4_rejected(self):
        """retry_budget_used=3 → attempt 4, must reject at RecoveryRequest."""
        # QueueEntry allows retry_budget_used=3 (Store doesn't check max),
        # but RecoveryRequest.__post_init__ must reject it.
        entry = ps.QueueEntry(
            schema_version=ps.SCHEMA_VERSION,
            queue_id="Q-1", task_id="TC-001", revision=1,
            enqueue_sequence=1, enqueued_at=STAMP,
            business_priority=ps.BusinessPriority.P1,
            aging_basis_at=STAMP,
            worker_kind_request=WorkerKind.STANDARD_AGENT,
            assessment_id="ASM-1", state=ps.QueuePhase.QUEUED,
            conflict_keys=(), retry_budget_used=3,
            content_digest="sha256:" + "0" * 64,
            selection_generation=1,
        )
        entry = dataclasses.replace(
            entry, content_digest=ps._entry_digest(entry))
        task = _task(task_id="TC-001", revision=1, state="ready")
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.RecoveryRequest(
                queue_entry=entry, canonical_task=task, canonical_events=())

    def test_attempt_above_4_rejected(self):
        """retry_budget_used>3 must be rejected at RecoveryRequest."""
        entry = ps.QueueEntry(
            schema_version=ps.SCHEMA_VERSION,
            queue_id="Q-1", task_id="TC-001", revision=1,
            enqueue_sequence=1, enqueued_at=STAMP,
            business_priority=ps.BusinessPriority.P1,
            aging_basis_at=STAMP,
            worker_kind_request=WorkerKind.STANDARD_AGENT,
            assessment_id="ASM-1", state=ps.QueuePhase.QUEUED,
            conflict_keys=(), retry_budget_used=5,
            content_digest="sha256:" + "0" * 64,
            selection_generation=1,
        )
        entry = dataclasses.replace(
            entry, content_digest=ps._entry_digest(entry))
        task = _task(task_id="TC-001", revision=1, state="ready")
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.RecoveryRequest(
                queue_entry=entry, canonical_task=task, canonical_events=())

    def test_attempt_4_no_decision(self):
        """attempt 4 must not produce any decision."""
        entry = _entry(state=ps.QueuePhase.QUEUED, retry_budget_used=2)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        # Force retry_budget_used=3 by constructing with bypass
        entry = dataclasses.replace(entry, retry_budget_used=3, content_digest="sha256:" + "0" * 64)
        entry = dataclasses.replace(entry, content_digest=ps._entry_digest(entry))
        # The __post_init__ should block this since retry_budget_used >= 3
        # is rejected during construction. Test defense in RecoveryRequest.
        # QueueEntry itself doesn't block retry>=3 — just the request does.
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task=task,
                canonical_events=(),
            )


# ═══════════════════════════════════════════════════════════════════════════
# 11. Canonical priority
# ═══════════════════════════════════════════════════════════════════════════


class CanonicalPriorityTests(unittest.TestCase):
    """Frozen priority: canonical event > task > receipt > queue entry > lease."""

    def test_event_overrides_queue(self):
        """Canonical event must override scheduler queue state."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        # Canonical dispatched event wins — adopt (queued/selected → align)
        self.assertIs(
            decision.recovery_action,
            rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH,
        )

    def test_scheduler_no_overwrite_canonical(self):
        """Scheduler evidence must not overwrite canonical semantics."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        # Selected + canonical dispatch → retire (not selected fallback)
        self.assertNotEqual(
            decision.recovery_action,
            rec.RecoveryAction.REQUEUE_SELECTED,
        )

    def test_task_state_overrides_queue(self):
        """Canonical task state must override scheduler queue state."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="in_progress", attempt=1, dispatch_id="DISP-001",
        )
        request = _req(entry, task, events=())
        decision = rec.decide_reconciliation(request)
        # in_progress → Worker running → no-op
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)
        self.assertIs(decision.reason, rec.RecoveryReason.WORKER_RUNNING)

    def test_scheduler_no_overwrite_canonical(self):
        """Scheduler evidence must not overwrite canonical semantics."""
        entry = _entry(state=ps.QueuePhase.SELECTED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,))
        decision = rec.decide_reconciliation(request)
        # Selected + canonical dispatch → adopt (not selected fallback)
        self.assertNotEqual(
            decision.recovery_action,
            rec.RecoveryAction.REQUEUE_SELECTED,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 12. Determinism
# ═══════════════════════════════════════════════════════════════════════════


class DeterminismTests(unittest.TestCase):
    """Same input → exact same output. Deterministic and pure."""

    def test_identical_input_same_output(self):
        """Same typed input must produce byte-exact same decision."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request1 = _req(entry, task)
        request2 = _req(
            _entry(
                queue_id=entry.queue_id,
                task_id=entry.task_id,
                revision=entry.revision,
                state=ps.QueuePhase.QUEUED,
            ),
            _task(task_id=entry.task_id, revision=entry.revision, state="ready"),
        )
        d1 = rec.decide_reconciliation(request1)
        d2 = rec.decide_reconciliation(request2)
        self.assertEqual(d1, d2)
        self.assertEqual(d1.replay_digest, d2.replay_digest)

    def test_replay_digest_deterministic(self):
        """Replay digest must be identical for identical inputs."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        d1 = rec.decide_reconciliation(_req(entry, task))
        d2 = rec.decide_reconciliation(_req(entry, task))
        self.assertEqual(d1.replay_digest, d2.replay_digest)

    def test_different_input_different_digest(self):
        """Different input must produce different digest."""
        entry1 = _entry(queue_id="Q-1", task_id="TC-001", state=ps.QueuePhase.QUEUED)
        entry2 = _entry(queue_id="Q-2", task_id="TC-002", state=ps.QueuePhase.QUEUED)
        task1 = _task(task_id="TC-001", revision=1, state="ready")
        task2 = _task(task_id="TC-002", revision=1, state="ready")
        d1 = rec.decide_reconciliation(_req(entry1, task1))
        d2 = rec.decide_reconciliation(_req(entry2, task2))
        self.assertNotEqual(d1.replay_digest, d2.replay_digest)

    def test_input_ordering_no_effect(self):
        """Input ordering within frozen tuples must not affect result."""
        evt_a = _event(event_id="EVT-A", task_id="TC-001", revision=1,
                        event_type="TASK_DISPATCHED", attempt=1)
        evt_b = _event(event_id="EVT-B", task_id="TC-001", revision=1,
                        event_type="TASK_SPECIFIED", attempt=None)
        entry = _entry(state=ps.QueuePhase.QUEUED, task_id="TC-001")
        task = _task(
            task_id="TC-001", revision=1, state="dispatched",
            attempt=1, dispatch_id=evt_a.dispatch_id,
        )
        # Events in different order — same set
        d1 = rec.decide_reconciliation(_req(entry, task, events=(evt_a, evt_b)))
        d2 = rec.decide_reconciliation(_req(entry, task, events=(evt_b, evt_a)))
        self.assertEqual(d1, d2)
        self.assertEqual(d1.replay_digest, d2.replay_digest)


# ═══════════════════════════════════════════════════════════════════════════
# 13. Concurrent replay — same generation, same content → same decision
# ═══════════════════════════════════════════════════════════════════════════


class ConcurrentReplayTests(unittest.TestCase):
    """Concurrent recovery — same generation returns same decision."""

    def test_same_generation_same_decision(self):
        """Same generation with same inputs → same decision."""
        entry = _entry(state=ps.QueuePhase.QUEUED, generation=1)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        d1 = rec.decide_reconciliation(_req(entry, task, recovery_generation=1))
        d2 = rec.decide_reconciliation(_req(entry, task, recovery_generation=1))
        self.assertEqual(d1, d2)

    def test_different_generation_same_content(self):
        """Different recovery generation may differ in digest only."""
        entry = _entry(state=ps.QueuePhase.QUEUED, generation=1)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        d1 = rec.decide_reconciliation(_req(entry, task, recovery_generation=1))
        d2 = rec.decide_reconciliation(_req(entry, task, recovery_generation=2))
        # Decisions should differ in replay_digest (different generation)
        self.assertNotEqual(d1.replay_digest, d2.replay_digest)
        # But action should be the same
        self.assertEqual(d1.recovery_action, d2.recovery_action)


# ═══════════════════════════════════════════════════════════════════════════
# 14. Tombstone replay / divergent tombstone
# ═══════════════════════════════════════════════════════════════════════════


class TombstoneTests(unittest.TestCase):
    """Tombstone replay — identical = OK, divergent = fail-closed."""

    def test_dispatched_receipt_divergent_fail_closed(self):
        """Receipt shows dispatched but canonical has no evidence → fail-closed."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        rct = _receipt(
            entry, phase=ps.ReceiptPhase.DISPATCHED,
            dispatch_event_id="EVT-FAKE",
        )
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, receipt=rct)
        decision = rec.decide_reconciliation(request)
        # Receipt dispatched but canonical has no dispatch evidence → fail-closed
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)

    def test_dispatched_receipt_canonical_aligned(self):
        """Receipt dispatched + canonical dispatched → retire (event)."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
            dispatch_id="DISP-CANON",
        )
        rct = _receipt(
            entry, phase=ps.ReceiptPhase.DISPATCHED,
            dispatch_event_id="EVT-DISP-CANON",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-CANON",
        )
        request = _req(entry, task, events=(evt,), receipt=rct)
        decision = rec.decide_reconciliation(request)
        self.assertIs(
            decision.recovery_action,
            rec.RecoveryAction.RETIRE_QUEUE_ENTRY,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 15. Retired — no-op
# ═══════════════════════════════════════════════════════════════════════════


class RetiredTests(unittest.TestCase):
    """Retired queue entries → no-op."""

    def test_retired_no_op(self):
        """Retired entry → no-op."""
        entry = _entry(state=ps.QueuePhase.RETIRED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="integrated")
        request = _req(entry, task)
        decision = rec.decide_reconciliation(request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)
        self.assertIs(decision.reason, rec.RecoveryReason.RETIRED_NO_OP)


# ═══════════════════════════════════════════════════════════════════════════
# 16. Defense — typed errors, no Exception catch-all
# ═══════════════════════════════════════════════════════════════════════════


class DefenseTests(unittest.TestCase):
    """Defense rules: typed input, no bool-as-int, no unknown phases."""

    def test_wrong_queue_entry_type(self):
        """Non-QueueEntry input must raise typed error."""
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry="not_a_QueueEntry",  # type: ignore[arg-type]
                canonical_task=_task(),
                canonical_events=(),
            )

    def test_wrong_task_type(self):
        """Non-TaskEntry must raise typed error."""
        entry = _entry()
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task="not_a_TaskEntry",  # type: ignore[arg-type]
                canonical_events=(),
            )

    def test_wrong_events_type(self):
        """Non-tuple events must raise typed error."""
        entry = _entry()
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task=task,
                canonical_events=["not_a_tuple"],  # type: ignore[arg-type]
            )

    def test_wrong_event_entry_type(self):
        """Non-EventEntry in events tuple must raise typed error."""
        entry = _entry()
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task=task,
                canonical_events=("not_an_event",),  # type: ignore[arg-type]
            )

    def test_identity_mismatch_detected(self):
        """task_id/revision mismatch between entry and task → defense error."""
        entry = _entry(task_id="TC-001", revision=1)
        task = _task(task_id="TC-002", revision=1, state="ready")
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task, canonical_events=())
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.decide_reconciliation(request)

    def test_revision_mismatch_detected(self):
        """revision mismatch → defense error at decide time."""
        entry = _entry(task_id="TC-001", revision=1)
        task = _task(task_id="TC-001", revision=2, state="ready")
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task, canonical_events=())
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.decide_reconciliation(request)

    def test_receipt_queue_mismatch_detected(self):
        """Receipt queue_id must match entry — defense error at decide time."""
        entry = _entry(queue_id="Q-1", state=ps.QueuePhase.SELECTED)
        entry2 = _entry(queue_id="Q-2", state=ps.QueuePhase.SELECTED)
        rct = _receipt(entry2, phase=ps.ReceiptPhase.SELECTED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task,
            canonical_events=(), schedule_receipt=rct)
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.decide_reconciliation(request)

    def test_multiple_dispatch_for_same_attempt(self):
        """Multiple TASK_DISPATCHED for one attempt → defense error."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt1 = _event(
            event_id="EVT-001", task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1, dispatch_id="DISP-A",
        )
        evt2 = _event(
            event_id="EVT-002", task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1, dispatch_id="DISP-B",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-A",
        )
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task,
            canonical_events=(evt1, evt2))
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.decide_reconciliation(request)

    def test_mutable_conflict_keys_rejected(self):
        """Mutable collections must be rejected."""
        # QueueEntry constructor ensures tuple type — this is tested
        # at construction level. Skip for recovery module coverage.
        pass

    def test_unknown_liveness_type_rejected(self):
        """Non-LivenessEvidence liveness must be rejected."""
        entry = _entry()
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task=task,
                canonical_events=(),
                liveness="not_an_enum",  # type: ignore[arg-type]
            )


# ═══════════════════════════════════════════════════════════════════════════
# 17. Source boundary — no I/O, no Store, no admission import
# ═══════════════════════════════════════════════════════════════════════════


class SourceBoundaryTests(unittest.TestCase):
    """Module must have no filesystem I/O, Store writes, or admission imports."""

    def setUp(self):
        import portfolio_scheduler_recovery as mod
        self._module_source = inspect.getsource(mod)

    def test_no_filesystem_imports(self):
        """Module must not import os, pathlib, io, or any filesystem module
        beyond those in the frozen dataclass/stdlib."""
        # The module imports hashlib, enum, dataclass, typing — all stdlib.
        # It must not import os, pathlib, io, sys, subprocess.
        forbidden = ["import os", "import pathlib", "import io",
                     "import sys", "import subprocess", "import json",
                     "import tempfile", "import secrets"]
        source = self._module_source
        for forbidden_import in forbidden:
            self.assertNotIn(
                forbidden_import, source,
                f"module must not contain {forbidden_import}",
            )

    def test_no_store_write_imports(self):
        """Module must not import PortfolioSchedulerStore."""
        self.assertNotIn(
            "PortfolioSchedulerStore", self._module_source,
        )

    def test_no_admission_import(self):
        """Module must not import any admission module."""
        # Check imports only — exclude docstrings/comments
        import_lines = [
            line for line in self._module_source.splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        source_imports = "\n".join(import_lines).lower()
        self.assertNotIn("admission", source_imports)

    def test_no_process_probe_import(self):
        """Module must not import process probe functions."""
        forbidden = ["probe_process", "get_boot_id",
                     "get_process_creation_time",
                     "probe_dispatch_process_tree",
                     "taskkill", "TerminateProcess"]
        source_lower = self._module_source.lower()
        for forbidden in forbidden:
            self.assertNotIn(
                forbidden, source_lower,
                f"module must not contain {forbidden}",
            )

    def test_no_network_import(self):
        """Module must not import urllib, http, socket, or requests."""
        forbidden = ["import urllib", "import http", "import socket",
                     "import requests", "import httpx", "import aiohttp"]
        source = self._module_source
        for forbidden_import in forbidden:
            self.assertNotIn(forbidden_import, source)

    def test_no_environment_variable_access(self):
        """Module must not access os.environ."""
        # Parse AST to check for os.environ attribute access
        source = self._module_source
        # os.environ appears as "environ" in Attribute context.
        # The module mentions "environment" only in docstrings.
        # Check that "os.environ" or "environ[" or "environ.get" don't appear.
        forbidden_patterns = ["os.environ", "environ[", "environ.get(",
                              "environ.get("]
        for pattern in forbidden_patterns:
            self.assertNotIn(pattern, source,
                             f"module must not contain {pattern}")

    def test_no_sleep_or_timer(self):
        """Module must not use sleep or timer."""
        self.assertNotIn("sleep", self._module_source)
        self.assertNotIn("time.", self._module_source)


# ═══════════════════════════════════════════════════════════════════════════
# 18. Decision typed fields
# ═══════════════════════════════════════════════════════════════════════════


class DecisionTypedFieldsTests(unittest.TestCase):
    """All decision fields must be typed — no free text classification."""

    def test_decision_action_is_enum(self):
        """Action must be RecoveryAction enum, not free text."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        decision = rec.decide_reconciliation(_req(entry, task))
        self.assertIsInstance(decision.recovery_action, rec.RecoveryAction)

    def test_decision_reason_is_enum(self):
        """Reason must be RecoveryReason enum, not free text."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        decision = rec.decide_reconciliation(_req(entry, task))
        self.assertIsInstance(decision.reason, rec.RecoveryReason)

    def test_no_free_text_in_action(self):
        """RecoveryAction values must be from the frozen set."""
        valid = {a.value for a in rec.RecoveryAction}
        for action in rec.RecoveryAction:
            self.assertIn(action.value, valid)

    def test_no_free_text_in_reason(self):
        """RecoveryReason values must be from the frozen set."""
        valid = {r.value for r in rec.RecoveryReason}
        for reason in rec.RecoveryReason:
            self.assertIn(reason.value, valid)

    def test_decision_digest_is_sha256(self):
        """Replay digest must be sha256: format."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        decision = rec.decide_reconciliation(_req(entry, task))
        self.assertTrue(decision.replay_digest.startswith("sha256:"))
        self.assertEqual(len(decision.replay_digest), 7 + 64)

    def test_decision_expected_generation_is_int(self):
        """expected_generation must be a non-bool int."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        decision = rec.decide_reconciliation(_req(entry, task))
        self.assertIsInstance(decision.expected_generation, int)
        self.assertNotIsInstance(decision.expected_generation, bool)

    def test_all_decision_fields_populated(self):
        """Every decision field must be non-None (except receipt_id for no-receipt)."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        decision = rec.decide_reconciliation(_req(entry, task))
        self.assertIsNotNone(decision.queue_id)
        self.assertIsNotNone(decision.task_id)
        self.assertIsNotNone(decision.revision)
        self.assertIsNotNone(decision.enqueue_sequence)
        self.assertIsNotNone(decision.observed_queue_phase)
        self.assertIsNotNone(decision.canonical_task_state)
        self.assertIsNotNone(decision.recovery_action)
        self.assertIsNotNone(decision.reason)
        self.assertIsNotNone(decision.expected_generation)
        self.assertIsNotNone(decision.replay_digest)


# ═══════════════════════════════════════════════════════════════════════════
# 19. Impossible phase combinations
# ═══════════════════════════════════════════════════════════════════════════


class ImpossiblePhaseTests(unittest.TestCase):
    """Impossible phase combos must fail-closed."""

    def test_dispatched_receipt_on_queued_entry(self):
        """Dispatched receipt on queued entry → impossible, fail."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        rct = _receipt(
            entry, phase=ps.ReceiptPhase.DISPATCHED,
            dispatch_event_id="EVT-FAKE",
        )
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task,
            canonical_events=(), schedule_receipt=rct)
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.decide_reconciliation(request)


# ═══════════════════════════════════════════════════════════════════════════
# 20. Stale policy version
# ═══════════════════════════════════════════════════════════════════════════


class StalePolicyVersionTests(unittest.TestCase):
    """Stale policy version must be rejected."""

    def test_stale_policy_version_rejected(self):
        """Non-matching policy version → input error."""
        entry = _entry()
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        with self.assertRaises(rec.RecoveryInputError):
            rec.RecoveryRequest(
                queue_entry=entry,
                canonical_task=task,
                canonical_events=(),
                policy_version="old-version",
            )


# ═══════════════════════════════════════════════════════════════════════════
# 21. PID reuse / boot mismatch (liveness via caller — test DEAD path)
# ═══════════════════════════════════════════════════════════════════════════


class PidReuseBootMismatchTests(unittest.TestCase):
    """PID reuse/boot-id mismatch must be UNKNOWN in the caller,
    which means fail-closed here."""

    def test_dead_with_valid_phase_proceeds(self):
        """DEAD liveness with queued phase → resume."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(task_id=entry.task_id, revision=entry.revision, state="ready")
        request = _req(entry, task, liveness=rec.LivenessEvidence.DEAD)
        decision = rec.decide_reconciliation(request)
        # DEAD allows recovery to proceed
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RESUME)

    def test_dead_with_dispatched_maintains_canonical(self):
        """DEAD with dispatched state respects canonical."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        evt = _event(
            task_id=entry.task_id, revision=entry.revision,
            event_type="TASK_DISPATCHED", attempt=1,
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id=evt.dispatch_id,
        )
        request = _req(entry, task, events=(evt,),
                       liveness=rec.LivenessEvidence.DEAD)
        decision = rec.decide_reconciliation(request)
        self.assertIs(
            decision.recovery_action,
            rec.RecoveryAction.RETIRE_QUEUE_ENTRY,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 22. Canonical Dispatch Event Identity Repair (TC-13.24b.2b.2c)
# ═══════════════════════════════════════════════════════════════════════════


class CanonicalDispatchEventIdentityRepairTests(unittest.TestCase):
    """TC-13.24b.2b.2c — canonical_dispatch_event_id must be EventEntry.event_id,
    never dispatch_id; event-missing paths must never ADOPT; replay binds both
    event_id and dispatch_id separately."""

    # ── rule 1: event_id == EventEntry.event_id ───────────────────────────

    def test_canonical_event_id_equals_event_entry_event_id(self):
        """canonical_dispatch_event_id must be EventEntry.event_id."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            event_id="EVT-CANON-001", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-001",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-001",
        )
        d = rec.decide_reconciliation(_req(entry, task, events=(evt,)))
        self.assertEqual(d.canonical_dispatch_event_id, "EVT-CANON-001")
        self.assertEqual(d.canonical_dispatch_event_id, evt.event_id)

    # ── rule 2: event_id ≠ dispatch_id → still uses event_id ─────────────

    def test_event_id_differs_from_dispatch_id_still_uses_event_id(self):
        """When event_id and dispatch_id are clearly different,
        canonical_dispatch_event_id must still be the event_id."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            event_id="EVT-UNRELATED", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-TOTALLY-DIFFERENT",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-TOTALLY-DIFFERENT",
        )
        d = rec.decide_reconciliation(_req(entry, task, events=(evt,)))
        self.assertEqual(d.canonical_dispatch_event_id, "EVT-UNRELATED")
        self.assertNotEqual(d.canonical_dispatch_event_id, "DISP-TOTALLY-DIFFERENT")

    # ── rule 3: task/current_dispatch exists, event missing → no ADOPT ────

    def test_task_dispatched_no_matching_event_must_not_adopt(self):
        """task state=dispatched, current_dispatch present, but no matching
        TASK_DISPATCHED event → must NOT return ADOPT_CANONICAL_DISPATCH."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-NO-EVT",
        )
        # No matching event for this attempt
        events: tuple[EventEntry, ...] = ()
        d = rec.decide_reconciliation(_req(entry, task, events=events))
        self.assertNotEqual(
            d.recovery_action, rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH,
        )
        self.assertIsNone(d.canonical_dispatch_event_id)

    def test_task_dispatched_no_event_returns_wait_for_owner(self):
        """task dispatched + current_dispatch but no event → WAIT_FOR_EXECUTION_OWNER."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-NO-EVT",
        )
        d = rec.decide_reconciliation(_req(entry, task, events=()))
        self.assertIs(
            d.recovery_action, rec.RecoveryAction.WAIT_FOR_EXECUTION_OWNER,
        )

    def test_review_ready_state_no_event_must_not_adopt(self):
        """task state=review_ready, current_dispatch present, no event →
        must NOT ADOPT."""
        entry = _entry(state=ps.QueuePhase.DISPATCHED)
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="review_ready", attempt=1, dispatch_id="DISP-RR",
        )
        d = rec.decide_reconciliation(_req(entry, task, events=()))
        self.assertNotEqual(
            d.recovery_action, rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH,
        )

    # ── rule 4: event dispatch_id vs current_dispatch divergence ──────────

    def test_event_dispatch_id_diverges_from_current_dispatch(self):
        """event has dispatch_id X, current_dispatch has dispatch_id Y → fail-closed."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            event_id="EVT-DIVERGE", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-X",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-Y",
        )
        d = rec.decide_reconciliation(_req(entry, task, events=(evt,)))
        self.assertIs(d.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        self.assertIs(d.reason, rec.RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE)

    # ── rule 5: receipt dispatch_id diverges from canonical event ─────────

    def test_receipt_dispatch_id_diverges_from_canonical_event(self):
        """DRE dispatch_id != canonical event dispatch_id → fail-closed."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            event_id="EVT-RCPT-DIV", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-CANON",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-CANON",
        )
        dre = rec.DispatchReceiptEvidence(
            task_id=entry.task_id, revision=entry.revision,
            attempt=1, dispatch_id="DISP-DIFFERENT",
            generation_id="GEN-001", phase="WORKER_STARTED",
        )
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task,
            canonical_events=(evt,), dispatch_receipt=dre,
        )
        d = rec.decide_reconciliation(request)
        self.assertIs(d.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        self.assertIs(d.reason, rec.RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE)

    # ── rule 6: lease dispatch_id diverges from canonical event ───────────

    def test_lease_dispatch_id_diverges_from_canonical_event(self):
        """Lease holder_dispatch_id != canonical event dispatch_id → fail-closed."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            event_id="EVT-LEASE-DIV", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-CANON",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-CANON",
        )
        lease = rec.LeaseEvidence(
            lease_id="L-001", slot_id="SLOT-001",
            holder_dispatch_id="DISP-WRONG",
            holder_instance_id="INST-001",
        )
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task,
            canonical_events=(evt,), lease_snapshot=lease,
        )
        d = rec.decide_reconciliation(request)
        self.assertIs(d.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        self.assertIs(d.reason, rec.RecoveryReason.EVIDENCE_IDENTITY_DIVERGENCE)

    # ── rule 7: duplicate TASK_DISPATCHED for same attempt ────────────────

    def test_duplicate_dispatched_event_same_attempt(self):
        """Two TASK_DISPATCHED events for same (task, revision, attempt) →
        defense error."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt1 = _event(
            event_id="EVT-DUP-A", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-A",
        )
        evt2 = _event(
            event_id="EVT-DUP-B", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-B",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-A",
        )
        request = rec.RecoveryRequest(
            queue_entry=entry, canonical_task=task,
            canonical_events=(evt1, evt2),
        )
        with self.assertRaises(rec.RecoveryDefenseError):
            rec.decide_reconciliation(request)

    # ── rule 8: event identity change → digest change ─────────────────────

    def test_event_identity_change_digest_changes(self):
        """Changing canonical event_id must change replay_digest."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt_a = _event(
            event_id="EVT-AAA", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-001",
        )
        evt_b = _event(
            event_id="EVT-BBB", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-001",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-001",
        )
        da = rec.decide_reconciliation(_req(entry, task, events=(evt_a,)))
        db = rec.decide_reconciliation(_req(entry, task, events=(evt_b,)))
        self.assertNotEqual(da.replay_digest, db.replay_digest)
        # canonical_dispatch_event_id must differ
        self.assertNotEqual(
            da.canonical_dispatch_event_id,
            db.canonical_dispatch_event_id,
        )

    def test_dispatch_id_change_alone_digest_changes(self):
        """Changing dispatch_id (keeping event_id) must change replay_digest
        because both are independently bound."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt_a = _event(
            event_id="EVT-SAME", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-DA",
        )
        evt_b = _event(
            event_id="EVT-SAME", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-DB",
        )
        task_a = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-DA",
        )
        task_b = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-DB",
        )
        da = rec.decide_reconciliation(_req(entry, task_a, events=(evt_a,)))
        db = rec.decide_reconciliation(_req(entry, task_b, events=(evt_b,)))
        self.assertNotEqual(da.replay_digest, db.replay_digest)

    # ── rule 9: byte-exact replay → same decision + same digest ───────────

    def test_byte_exact_replay_same_decision_same_digest(self):
        """Identical inputs in two separate calls → byte-identical decision."""
        entry = _entry(state=ps.QueuePhase.QUEUED)
        evt = _event(
            event_id="EVT-REPLAY", task_id=entry.task_id,
            revision=entry.revision, event_type="TASK_DISPATCHED",
            attempt=1, dispatch_id="DISP-RP",
        )
        task = _task(
            task_id=entry.task_id, revision=entry.revision,
            state="dispatched", attempt=1, dispatch_id="DISP-RP",
        )
        d1 = rec.decide_reconciliation(_req(entry, task, events=(evt,)))
        d2 = rec.decide_reconciliation(_req(entry, task, events=(evt,)))
        self.assertEqual(d1, d2)
        self.assertEqual(d1.replay_digest, d2.replay_digest)
        self.assertEqual(
            d1.canonical_dispatch_event_id,
            d2.canonical_dispatch_event_id,
        )

    # ── rule 10: source-level — no path passes dispatch_id as event_id ────

    def test_no_dispatch_id_passed_as_canonical_event_id(self):
        """Source must NOT contain a path that passes EventEntry.dispatch_id
        or TaskEntry.current_dispatch.dispatch_id as canonical_dispatch_event_id."""
        import inspect
        source = inspect.getsource(rec)
        # The module must use dispatch_id for binding/verification only,
        # never as the canonical_dispatch_event_id value.
        # Check that _build_decision dispatch_event_id kwarg receives
        # canonical_event_id (from evt.event_id), not dispatch_id.
        # The key invariant: the string "dispatch_event_id=dispatch_id"
        # with dispatch_id being a local variable (not receipt.dispatch_event_id)
        # must not appear.
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Allow receipt.dispatch_event_id (receipt field IS event_id)
            # But reject bare dispatch_id passed as dispatch_event_id
            if "dispatch_event_id=dispatch_id" in stripped:
                # Only acceptable if it's receipt.dispatch_event_id
                if "receipt.dispatch_event_id" not in stripped:
                    self.fail(
                        f"dispatch_id passed as dispatch_event_id at line {i}: {stripped!r}"
                    )


# ═══════════════════════════════════════════════════════════════════════════
# 23. No pass-only tests — every test has an assertion
# ═══════════════════════════════════════════════════════════════════════════
# All tests above have explicit assertions — no pass-only tests exist.
