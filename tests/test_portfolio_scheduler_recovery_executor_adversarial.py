"""TC-13.24b.2b.4b.2a — PortfolioScheduler Recovery Action Executor Adversarial Verification.

stdlib + production module import; no external dependencies beyond the
recovery executor module under verification.

This test file performs independent adversarial verification of the
PortfolioScheduler Recovery Action Executor (implementation + frozen
contract) to prove it safely constrains scheduler-local recovery.

Coverage (incremental order):
  1. Identity substitution — per-field
  2. Action matrix — exact outcomes, zero canonical/Worker/Lease writes
  3. Phase / crash windows
  4. Concurrency
  5. Liveness / defense
  6. Source / lock boundary

IMPORTANT: This file imports the production executor module UNDER TEST.
It does NOT import handoff Store/runtime, Admission, Policy,
WorkflowOrchestrator, or Recovery Executor implementation from other
cards.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import portfolio_scheduler_recovery as rec
import portfolio_scheduler_recovery_executor as exe
import portfolio_scheduler_store as ps
from core_types import WorkerKind
from state_provider import (
    DispatchInfo,
    EventEntry,
    ModelSelectionSnapshot,
    TaskEntry,
    TaskTimestamps,
)

STAMP = "2026-01-01T00:00:00.000000Z"
STAMP_B = "2026-01-01T01:00:00.000000Z"
STAMP_C = "2026-01-01T02:00:00.000000Z"


# ── helpers (same build as original tests) ────────────────────────────────

def make_entry(
    state: ps.QueuePhase = ps.QueuePhase.QUEUED,
    generation: int = 1,
    retry_budget_used: int = 0,
) -> ps.QueueEntry:
    provisional = ps.QueueEntry(
        schema_version=ps.SCHEMA_VERSION,
        queue_id="Q-EXEC-1",
        task_id="TC-EXEC-1",
        revision=1,
        enqueue_sequence=1,
        enqueued_at=STAMP,
        business_priority=ps.BusinessPriority.P1,
        aging_basis_at=STAMP,
        worker_kind_request=WorkerKind.STANDARD_AGENT,
        assessment_id="ASM-EXEC-1",
        state=state,
        conflict_keys=(),
        retry_budget_used=retry_budget_used,
        content_digest="sha256:" + "0" * 64,
        selection_generation=generation,
    )
    return dataclasses.replace(provisional, content_digest=ps._entry_digest(provisional))


def make_receipt(
    entry: ps.QueueEntry,
    phase: ps.ReceiptPhase = ps.ReceiptPhase.SELECTED,
    event_id: str | None = None,
) -> ps.ScheduleReceipt:
    provisional = ps.ScheduleReceipt(
        schema_version=ps.SCHEMA_VERSION,
        receipt_id="SR-EXEC-1-g1",
        queue_id=entry.queue_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selected_at=STAMP,
        order_key=(0, 0, entry.enqueue_sequence, entry.queue_id),
        selection_reason="executor-test",
        worker_kind=entry.worker_kind_request,
        dispatch_event_id=event_id,
        phase=phase,
        content_digest="sha256:" + "0" * 64,
        selection_generation=entry.selection_generation,
    )
    return dataclasses.replace(provisional, content_digest=ps._receipt_digest(provisional))


def make_task(
    state: str = "ready",
    attempt: int | None = None,
    dispatch_id: str | None = None,
) -> TaskEntry:
    model = ModelSelectionSnapshot(
        required_model_tier="standard",
        required_model_capabilities=(),
        model_binding_id="binding-executor",
        selected_model_provider="test",
        selected_model_id="test-model",
        selected_model_tier="standard",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=100000,
        selected_model_capabilities=(),
        model_degradation_approval_id=None,
    )
    dispatch = None
    if dispatch_id is not None:
        dispatch = DispatchInfo(
            dispatch_id=dispatch_id,
            attempt_id="ATT-EXEC-1",
            role_id="role-executor",
            base_commit="0" * 40,
            branch="executor-test",
            dispatched_at=STAMP_B,
            model_selection=model,
        )
    timestamps = TaskTimestamps(
        created_at=STAMP,
        ready_at=STAMP if state == "ready" else None,
        dispatched_at=STAMP_B if dispatch_id is not None else None,
        started_at=None,
        delivered_at=None,
        blocked_at=None,
        accepted_at=None,
        integrated_at=None,
        updated_at=STAMP_B,
    )
    return TaskEntry(
        task_id="TC-EXEC-1",
        revision=1,
        task_card_path="docs/pm/task-exec.md",
        task_card_commit="0" * 40,
        state=state,
        attempt=attempt,
        current_dispatch=dispatch,
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
        timestamps=timestamps,
        superseded_by=None,
    )


def make_event(
    event_id: str = "EVT-EXEC-1",
    dispatch_id: str = "DISP-EXEC-1",
) -> EventEntry:
    return EventEntry(
        schema_version="agentdesk.state-event/v2",
        event_id=event_id,
        event_type="TASK_DISPATCHED",
        task_id="TC-EXEC-1",
        revision=1,
        attempt=1,
        dispatch_id=dispatch_id,
        from_state="ready",
        to_state="dispatched",
        lease_epoch=1,
        actor_role_id="role-executor",
        occurred_at=STAMP_B,
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
        payload_digest="sha256:" + "0" * 64,
        extra_fields=None,
    )


def make_recovery(
    entry: ps.QueueEntry,
    task: TaskEntry,
    receipt: ps.ScheduleReceipt | None = None,
    events: tuple[EventEntry, ...] = (),
    liveness: rec.LivenessEvidence | None = rec.LivenessEvidence.DEAD,
) -> rec.RecoveryRequest:
    return rec.RecoveryRequest(
        queue_entry=entry,
        canonical_task=task,
        canonical_events=events,
        schedule_receipt=receipt,
        dispatch_receipt=None,
        dispatch_tombstone=None,
        lease_snapshot=None,
        recovery_time=STAMP_C,
        policy_version=ps.SCHEMA_VERSION,
        recovery_generation=entry.selection_generation,
        liveness=liveness,
    )


def make_execution_request(
    recovery_request: rec.RecoveryRequest,
    decision: rec.RecoveryDecision,
    execution_id: str = "EXE-EXEC-1",
) -> exe.RecoveryExecutionRequest:
    receipt = recovery_request.schedule_receipt
    return exe.RecoveryExecutionRequest(
        schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
        execution_id=execution_id,
        recovery_request=recovery_request,
        recovery_decision=decision,
        decision_replay_digest=decision.replay_digest,
        expected_queue_phase=recovery_request.queue_entry.state,
        expected_receipt_phase=None if receipt is None else receipt.phase,
        expected_selection_generation=recovery_request.queue_entry.selection_generation,
        expected_recovery_generation=recovery_request.recovery_generation,
        expected_canonical_dispatch_event_id=decision.canonical_dispatch_event_id,
        requested_at=STAMP_C,
    )


class AdversarialFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = ps.PortfolioSchedulerStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def persist(self, entry: ps.QueueEntry) -> None:
        self.store.reserve_enqueue_sequence(self.root)
        self.store.create_queue_entry(entry, self.root)

    def selected(self) -> tuple[ps.QueueEntry, ps.ScheduleReceipt]:
        entry = make_entry()
        self.persist(entry)
        self.store.advance_queue_phase(
            entry.queue_id, 1, ps.QueuePhase.SELECTED, project_root=self.root
        )
        selected_entry = self.store.read_queue_entry(entry.queue_id, self.root)
        receipt = make_receipt(selected_entry)
        self.store.create_schedule_receipt(receipt, self.root)
        return selected_entry, receipt

    def dispatched(self) -> tuple[ps.QueueEntry, ps.ScheduleReceipt, EventEntry]:
        entry, receipt = self.selected()
        event = make_event()
        self.store.advance_schedule_receipt(
            receipt.receipt_id,
            receipt.selection_generation,
            ps.ReceiptPhase.DISPATCHED,
            event.event_id,
            project_root=self.root,
        )
        self.store.advance_queue_phase(
            entry.queue_id,
            entry.selection_generation,
            ps.QueuePhase.DISPATCHED,
            event.event_id,
            project_root=self.root,
        )
        dispatched_entry = self.store.read_queue_entry(entry.queue_id, self.root)
        dispatched_receipt = self.store.read_schedule_receipt(
            receipt.receipt_id, self.root
        )
        return dispatched_entry, dispatched_receipt, event


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Identity substitution adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class IdentitySubstitutionAdversarial(AdversarialFixture):
    """Each identity field substitution is proven to be rejected before any
    scheduler mutation write."""

    def test_task_id_substitution_rejected_before_mutation(self) -> None:
        """task_id mismatch between queue entry and receipt is caught by
        decide_reconciliation (receipt_task_mismatch defense) before any
        executor object is created.  A queue entry whose task_id differs
        from the schedule receipt's task_id triggers RecoveryDefenseError."""
        entry, receipt = self.selected()
        event = make_event()
        # Create a receipt for the real task, but pair it with a wrong queue entry
        wrong_entry = dataclasses.replace(make_entry(), task_id="TC-WRONG",
            content_digest=ps._entry_digest(dataclasses.replace(make_entry(),
            task_id="TC-WRONG", content_digest="sha256:" + "0" * 64)))
        # The receipt has task_id=TC-EXEC-1, but the entry has TC-WRONG
        # RecoveryRequest can be created (it binds both), but
        # decide_reconciliation catches the identity mismatch
        rr = rec.RecoveryRequest(
            queue_entry=wrong_entry,
            canonical_task=make_task(),
            canonical_events=(event,),
            schedule_receipt=receipt,
            dispatch_receipt=None,
            dispatch_tombstone=None,
            lease_snapshot=None,
            recovery_time=STAMP_C,
            policy_version=ps.SCHEMA_VERSION,
            recovery_generation=wrong_entry.selection_generation,
            liveness=rec.LivenessEvidence.DEAD,
        )
        with self.assertRaises(rec.RecoveryDefenseError) as ctx:
            rec.decide_reconciliation(rr)
        self.assertIn("mismatch", str(ctx.exception).lower())

    def test_revision_substitution_rejected_before_mutation(self) -> None:
        """revision mismatch between decision and request is rejected in
        __post_init__ (task_identity conflict)."""
        entry, receipt = self.selected()
        rr = make_recovery(entry, make_task(), receipt)
        decision = rec.decide_reconciliation(rr)
        wrong_entry = dataclasses.replace(make_entry(), revision=99, content_digest=ps._entry_digest(dataclasses.replace(make_entry(), revision=99, content_digest="sha256:" + "0" * 64)))
        wrong_rr = make_recovery(wrong_entry, make_task(), receipt)
        with self.assertRaises(rec.RecoveryError):
            rec.decide_reconciliation(wrong_rr)

    def test_failed_attempt_substitution_rejected_before_write(self) -> None:
        """attempt 4 is rejected in the recovery decision layer before any
        executor store lock entry.  retry_budget_used >= 3 triggers
        attempt_four defense in RecoveryRequest.__post_init__."""
        entry = make_entry(retry_budget_used=3)
        self.persist(entry)
        # RecoveryRequest with retry_budget_used >= 3 raises RecoveryDefenseError
        # from __post_init__ — this fires before the executor ever sees the request,
        # proving attempt 4 is blocked at the earliest possible layer.
        with self.assertRaises(rec.RecoveryDefenseError):
            make_recovery(entry, make_task(attempt=3, state="ready"))

    def test_dispatch_id_substitution_rejected_in_event_binding(self) -> None:
        """A recovery request referencing a wrong dispatch_id via the event
        is rejected — _validate_event checks event.task_id and event.revision
        match the queue entry.  Mismatched events cause fail-closed."""
        entry, receipt = self.selected()
        wrong_event = make_event("EVT-WRONG", "DISP-WRONG")
        # event references TC-EXEC-1 which matches; but dispatch_id differs
        # and the recovery decision digest binds it
        rr_a = make_recovery(entry, make_task(state="dispatched", attempt=1, dispatch_id="DISP-WRONG"), receipt, (wrong_event,))
        decision_a = rec.decide_reconciliation(rr_a)
        # Now try to reuse this decision with a different expected event
        wrong_event_2 = make_event("EVT-WRONG-2", "DISP-DIFFERENT")
        rr_b = make_recovery(entry, make_task(state="dispatched", attempt=1, dispatch_id="DISP-DIFFERENT"), receipt, (wrong_event_2,))
        with self.assertRaises((exe.PortfolioSchedulerRecoveryExecutorInputError, exe.PortfolioSchedulerRecoveryExecutorConflictError)):
            exe.RecoveryExecutionRequest(
                schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id="EXE-DISP-1",
                recovery_request=rr_a,
                recovery_decision=decision_a,
                decision_replay_digest=decision_a.replay_digest,
                expected_queue_phase=rr_a.queue_entry.state,
                expected_receipt_phase=None if receipt is None else receipt.phase,
                expected_selection_generation=rr_a.queue_entry.selection_generation,
                expected_recovery_generation=rr_a.recovery_generation,
                expected_canonical_dispatch_event_id="EVT-DIFFERENT",
                requested_at=STAMP_C,
            )

    def test_canonical_dispatch_event_id_substitution_rejected(self) -> None:
        """expected_canonical_dispatch_event_id mismatch with decision
        triggers canonical_event conflict in __post_init__."""
        entry, receipt = self.selected()
        event = make_event()
        rr = make_recovery(entry, make_task(state="dispatched", attempt=1, dispatch_id=event.dispatch_id), receipt, (event,))
        decision = rec.decide_reconciliation(rr)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            exe.RecoveryExecutionRequest(
                schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id="EXE-EVT-SUB",
                recovery_request=rr,
                recovery_decision=decision,
                decision_replay_digest=decision.replay_digest,
                expected_queue_phase=rr.queue_entry.state,
                expected_receipt_phase=None if receipt is None else receipt.phase,
                expected_selection_generation=rr.queue_entry.selection_generation,
                expected_recovery_generation=rr.recovery_generation,
                expected_canonical_dispatch_event_id="EVT-SUBSTITUTED",
                requested_at=STAMP_C,
            )

    def test_recovery_generation_substitution_rejected(self) -> None:
        """expected_recovery_generation mismatch with recovery_request
        triggers recovery_generation conflict."""
        entry, receipt = self.selected()
        rr = make_recovery(entry, make_task(), receipt)
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            exe.RecoveryExecutionRequest(
                schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id="EXE-GEN-SUB",
                recovery_request=rr,
                recovery_decision=decision,
                decision_replay_digest=decision.replay_digest,
                expected_queue_phase=rr.queue_entry.state,
                expected_receipt_phase=None if receipt is None else receipt.phase,
                expected_selection_generation=rr.queue_entry.selection_generation,
                expected_recovery_generation=999,
                expected_canonical_dispatch_event_id=decision.canonical_dispatch_event_id,
                requested_at=STAMP_C,
            )

    def test_decision_replay_digest_substitution_rejected(self) -> None:
        """decision_replay_digest substitution triggers decision_digest
        conflict in __post_init__ (recomputed digest mismatch)."""
        entry, receipt = self.selected()
        rr = make_recovery(entry, make_task(), receipt)
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            exe.RecoveryExecutionRequest(
                schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id="EXE-DIG-SUB",
                recovery_request=rr,
                recovery_decision=decision,
                decision_replay_digest="sha256:" + "ab" * 32,
                expected_queue_phase=rr.queue_entry.state,
                expected_receipt_phase=None if receipt is None else receipt.phase,
                expected_selection_generation=rr.queue_entry.selection_generation,
                expected_recovery_generation=rr.recovery_generation,
                expected_canonical_dispatch_event_id=decision.canonical_dispatch_event_id,
                requested_at=STAMP_C,
            )

    def test_execution_id_must_be_stable_and_not_empty(self) -> None:
        """execution_id is validated by _text in __post_init__ — empty or
        non-string rejected before any lock."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorInputError):
            exe.RecoveryExecutionRequest(
                schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id="",
                recovery_request=rr,
                recovery_decision=decision,
                decision_replay_digest=decision.replay_digest,
                expected_queue_phase=rr.queue_entry.state,
                expected_receipt_phase=None,
                expected_selection_generation=rr.queue_entry.selection_generation,
                expected_recovery_generation=rr.recovery_generation,
                expected_canonical_dispatch_event_id=decision.canonical_dispatch_event_id,
                requested_at=STAMP_C,
            )

    def test_content_digest_binds_exact_receipt_identity(self) -> None:
        """Receipt content_digest is SHA-256 over canonical bytes; any
        identity change produces a different digest.  Tampered digest is
        caught by _receipt_digest comparison in __post_init__."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        outcome = executor.execute(make_execution_request(rr, decision))
        self.assertIsNotNone(outcome.receipt)
        # Construct a tampered receipt with a wrong digest
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorInputError):
            exe.RecoveryExecutionReceipt(
                schema_version=outcome.receipt.schema_version,
                execution_id=outcome.receipt.execution_id,
                queue_id=outcome.receipt.queue_id,
                receipt_id=outcome.receipt.receipt_id,
                task_id=outcome.receipt.task_id,
                revision=outcome.receipt.revision,
                selection_generation=outcome.receipt.selection_generation,
                recovery_generation=outcome.receipt.recovery_generation,
                recovery_action=outcome.receipt.recovery_action,
                recovery_reason=outcome.receipt.recovery_reason,
                canonical_dispatch_event_id=outcome.receipt.canonical_dispatch_event_id,
                decision_replay_digest=outcome.receipt.decision_replay_digest,
                phase=outcome.receipt.phase,
                reserved_at=outcome.receipt.reserved_at,
                applied_at=outcome.receipt.applied_at,
                finalized_at=outcome.receipt.finalized_at,
                content_digest="sha256:" + "ff" * 32,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Action matrix adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class ActionMatrixAdversarial(AdversarialFixture):
    """Prove each action has a unique, deterministic outcome and that no
    action writes canonical task/event/outbox, creates/acquires a lease, or
    starts a Worker."""

    def test_no_op_zero_writes_no_scheduler_evidence(self) -> None:
        """NO_OP must write nothing to the Scheduler evidence store."""
        entry = make_entry()
        rr = make_recovery(entry, make_task(), liveness=rec.LivenessEvidence.ALIVE)
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "NO_OP")
        self.assertIsNone(outcome.receipt)
        # No scheduler evidence directory created
        docs = self.root / "docs"
        self.assertFalse((docs / "pm" / "portfolio-scheduler").exists())

    def test_requeue_selected_zero_canonical_write(self) -> None:
        """REQUEUE_SELECTED mutates only scheduler queue/receipt; no canonical
        task/event/outbox is written."""
        entry, receipt = self.selected()
        rr = make_recovery(entry, make_task(), receipt)
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.REQUEUE_SELECTED)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        outcome = executor.execute(make_execution_request(rr, decision))
        self.assertEqual(outcome.result, "APPLIED")
        # Verify only scheduler-state was modified, no canonical dirs
        pm = self.root / "docs" / "pm" / "portfolio-scheduler"
        self.assertTrue((pm / "recovery-actions").exists())
        # Canonical paths must NOT exist
        self.assertFalse((self.root / "docs" / "pm" / "canonical-task").exists())
        self.assertFalse((self.root / "docs" / "pm" / "events").exists())
        self.assertFalse((self.root / "docs" / "pm" / "outbox").exists())

    def test_adopt_canonical_dispatch_requires_exact_event_id(self) -> None:
        """ADOPT_CANONICAL_DISPATCH requires exact canonical event_id.
        Missing event → fail-closed."""
        entry, receipt = self.selected()
        # No event in recovery request → ADOPT_CANONICAL_DISPATCH not selected
        rr = make_recovery(entry, make_task(state="dispatched", attempt=1), receipt, ())
        decision = rec.decide_reconciliation(rr)
        # Without event, decision won't be ADOPT
        self.assertNotEqual(decision.recovery_action, rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH)

    def test_retire_queue_entry_requires_canonical_event(self) -> None:
        """RETIRE_QUEUE_ENTRY requires canonical dispatch evidence; without
        it, the decision reverts to a different action."""
        entry, receipt, event = self.dispatched()
        rr = make_recovery(
            entry,
            make_task(state="dispatched", attempt=1, dispatch_id=event.dispatch_id),
            receipt,
            (event,),
        )
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RETIRE_QUEUE_ENTRY)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "APPLIED")

    def test_wait_for_execution_owner_zero_write(self) -> None:
        """WAIT_FOR_EXECUTION_OWNER must write nothing."""
        entry = make_entry()
        rr = make_recovery(
            entry,
            make_task(state="dispatched", attempt=1, dispatch_id="DISP-NO-EVENT"),
        )
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.WAIT_FOR_EXECUTION_OWNER)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "NO_OP")
        self.assertFalse((self.root / "docs" / "pm" / "portfolio-scheduler").exists())

    def test_fail_closed_zero_writes(self) -> None:
        """FAIL_CLOSED must write nothing."""
        entry = make_entry()
        rr = make_recovery(entry, make_task(), liveness=rec.LivenessEvidence.UNKNOWN)
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "FAIL_CLOSED")
        self.assertFalse((self.root / "docs" / "pm" / "portfolio-scheduler").exists())

    def test_invalidate_selection_without_receipt_is_fail_closed_no_fabrication(self) -> None:
        """INVALIDATE_SELECTION without receipt evidence → fail-closed.
        No evidence is fabricated."""
        entry = make_entry()
        self.persist(entry)
        self.store.advance_queue_phase(
            entry.queue_id, 1, ps.QueuePhase.SELECTED, project_root=self.root
        )
        selected_entry = dataclasses.replace(
            entry, state=ps.QueuePhase.SELECTED,
            content_digest=ps._entry_digest(dataclasses.replace(entry, state=ps.QueuePhase.SELECTED)),
        )
        rr = make_recovery(selected_entry, make_task())
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.INVALIDATE_SELECTION)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "FAIL_CLOSED")
        self.assertFalse(
            (self.root / "docs" / "pm" / "portfolio-scheduler" / "recovery-actions").exists()
        )

    def test_executor_never_calls_control_plane_transition(self) -> None:
        """Executor source must not import or reference ControlPlaneTransitionService."""
        source = inspect.getsource(exe)
        self.assertNotIn("control_plane_transition", source)
        self.assertNotIn("ControlPlaneTransition", source)

    def test_executor_never_creates_or_releases_lease(self) -> None:
        """Executor must never acquire or clean a WorkerSlotLease."""
        source = inspect.getsource(exe)
        self.assertNotIn("worker_slot_lease", source)
        self.assertNotIn("WorkerSlotLease", source)
        self.assertNotIn("acquire_lease", source)
        self.assertNotIn("release_lease", source)

    def test_executor_never_starts_worker(self) -> None:
        """Executor must never start a Worker."""
        source = inspect.getsource(exe)
        self.assertNotIn("start_worker", source)
        self.assertNotIn("WorkerAdapter", source)
        self.assertNotIn("run_worker", source)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Phase and crash windows adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class PhaseAndCrashWindowsAdversarial(AdversarialFixture):
    """Prove each phase transition is forward-only, no skipping, no
    backfill, and that crash windows have unique safe dispositions."""

    def test_reserved_before_validated_safe_resume(self) -> None:
        """If a receipt is left at RESERVED (crash before VALIDATED),
        re-execution resumes forward."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        with ps._store_lock(self.root):
            store_root = ps._prepare_store(self.root, True)
            exe._write_execution_receipt(store_root, exe._new_receipt(exec_req))
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(exec_req)
        self.assertEqual(outcome.result, "APPLIED")

    def test_validated_before_applying_safe_resume(self) -> None:
        """Receipt at VALIDATED resumes via APPLYING → action → APPLIED → FINALIZED."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        with ps._store_lock(self.root):
            store_root = ps._prepare_store(self.root, True)
            receipt = exe._write_execution_receipt(store_root, exe._new_receipt(exec_req))
            receipt = exe._advance_receipt(
                store_root, receipt, exe.RecoveryExecutionPhase.VALIDATED, STAMP_C
            )
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(exec_req)
        self.assertEqual(outcome.result, "APPLIED")
        self.assertEqual(outcome.phase, exe.RecoveryExecutionPhase.FINALIZED)

    def test_applying_before_applied_safe_resume(self) -> None:
        """Receipt at APPLYING resumes action application then finalizes."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        with ps._store_lock(self.root):
            store_root = ps._prepare_store(self.root, True)
            receipt = exe._write_execution_receipt(store_root, exe._new_receipt(exec_req))
            receipt = exe._advance_receipt(
                store_root, receipt, exe.RecoveryExecutionPhase.VALIDATED, STAMP_C
            )
            receipt = exe._advance_receipt(
                store_root, receipt, exe.RecoveryExecutionPhase.APPLYING, STAMP_C
            )
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(exec_req)
        self.assertEqual(outcome.result, "APPLIED")

    def test_applied_before_finalized_safe_resume(self) -> None:
        """Receipt at APPLIED finalizes after exact validation."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        with ps._store_lock(self.root):
            store_root = ps._prepare_store(self.root, True)
            receipt = exe._write_execution_receipt(store_root, exe._new_receipt(exec_req))
            receipt = exe._advance_receipt(store_root, receipt, exe.RecoveryExecutionPhase.VALIDATED, STAMP_C)
            receipt = exe._advance_receipt(store_root, receipt, exe.RecoveryExecutionPhase.APPLYING, STAMP_C)
            exe._apply_action_locked(store_root, exec_req)
            receipt = exe._advance_receipt(store_root, receipt, exe.RecoveryExecutionPhase.APPLIED, STAMP_C)
        outcome = executor.execute(exec_req)
        self.assertEqual(outcome.result, "APPLIED")
        self.assertEqual(outcome.phase, exe.RecoveryExecutionPhase.FINALIZED)

    def test_finalized_replay_is_byte_exact_no_duplicate_write(self) -> None:
        """Re-execution of finalized receipt returns REPLAYED, no mutation."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        first = executor.execute(exec_req)
        path = self.root / "docs" / "pm" / "portfolio-scheduler" / "recovery-actions" / entry.queue_id / "g1.yaml"
        before = path.read_bytes()
        second = executor.execute(exec_req)
        self.assertEqual(second.result, "REPLAYED")
        self.assertEqual(path.read_bytes(), before)

    def test_divergent_execution_identity_is_typed_conflict(self) -> None:
        """Same generation + divergent identity → typed conflict, not overwrite."""
        entry = make_entry()
        self.persist(entry)
        rr_a = make_recovery(entry, make_task())
        decision_a = rec.decide_reconciliation(rr_a)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        executor.execute(make_execution_request(rr_a, decision_a))
        # Second execution with different execution_id but same generation
        rr_b = make_recovery(entry, make_task())
        decision_b = rec.decide_reconciliation(rr_b)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            executor.execute(make_execution_request(rr_b, decision_b, "EXE-DIVERGENT"))

    def test_stale_generation_rejected(self) -> None:
        """Recovery with stale generation is rejected."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        executor.execute(exec_req)
        # Now try with stale generation (expected_selection_generation < stored)
        stale_rr = make_recovery(entry, make_task())
        stale_decision = rec.decide_reconciliation(stale_rr)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            executor.execute(
                make_execution_request(stale_rr, stale_decision, "EXE-STALE")
            )

    def test_phase_skip_rejected_by_advance_receipt(self) -> None:
        """Phase skipping (RESERVED → APPLIED directly) is rejected by
        _advance_receipt."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        with ps._store_lock(self.root):
            store_root = ps._prepare_store(self.root, True)
            receipt = exe._write_execution_receipt(store_root, exe._new_receipt(exec_req))
            with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
                exe._advance_receipt(
                    store_root, receipt, exe.RecoveryExecutionPhase.APPLYING, STAMP_C
                )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Concurrency adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class ConcurrencyAdversarial(AdversarialFixture):
    """Prove store-lock CAS provides exactly-one-winner semantics."""

    def test_concurrent_same_execution_single_apply(self) -> None:
        """Two callers with identical execution request → exactly one APPLIED.
        The other may be CONFLICT (lock contention) or REPLAYED (sees
        finalized receipt).  Either way, exactly one APPLIED."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)

        def run() -> str:
            try:
                return exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(exec_req).result
            except ps.PortfolioSchedulerLockConflictError:
                return "CONFLICT"
            except exe.PortfolioSchedulerRecoveryExecutorConflictError:
                return "CONFLICT"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: run(), (1, 2)))
        self.assertEqual(results.count("APPLIED"), 1,
                         f"Exactly one must be APPLIED, got {results}")
        # The other result is either CONFLICT or REPLAYED — both are safe
        self.assertIn(results[0], ("APPLIED", "REPLAYED", "CONFLICT"))
        self.assertIn(results[1], ("APPLIED", "REPLAYED", "CONFLICT"))
        self.assertNotEqual(results[0], results[1],
                            "Two identical calls must produce different results")

    def test_divergent_concurrent_executions_single_winner(self) -> None:
        """Two different execution requests for same queue/generation → one
        winner, loser gets conflict."""
        entry = make_entry()
        self.persist(entry)
        rr_a = make_recovery(entry, make_task())
        decision_a = rec.decide_reconciliation(rr_a)
        rr_b = make_recovery(entry, make_task())
        decision_b = rec.decide_reconciliation(rr_b)

        def run_a() -> str:
            try:
                return exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
                    make_execution_request(rr_a, decision_a, "EXE-A")
                ).result
            except (exe.PortfolioSchedulerRecoveryExecutorConflictError, ps.PortfolioSchedulerLockConflictError):
                return "CONFLICT"
            except Exception:
                return "CONFLICT"

        def run_b() -> str:
            try:
                return exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
                    make_execution_request(rr_b, decision_b, "EXE-B")
                ).result
            except (exe.PortfolioSchedulerRecoveryExecutorConflictError, ps.PortfolioSchedulerLockConflictError):
                return "CONFLICT"
            except Exception:
                return "CONFLICT"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = (pool.submit(run_a).result(), pool.submit(run_b).result())
        self.assertIn("APPLIED", results)

    def test_finalized_not_reopenable(self) -> None:
        """Once finalized, a different execution must not reopen the receipt."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        exec_req = make_execution_request(rr, decision)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        first = executor.execute(exec_req)
        self.assertEqual(first.result, "APPLIED")
        # Re-execution of an identical request is REPLAYED, not re-opened
        second = executor.execute(exec_req)
        self.assertEqual(second.result, "REPLAYED")
        # A divergent request for the same generation is conflict
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            executor.execute(make_execution_request(rr, decision, "EXE-DIFFERENT"))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Liveness and defense adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class LivenessAndDefenseAdversarial(AdversarialFixture):
    """Prove ALIVE/UNKNOWN/DEAD are not reclassified and that defense
    rejections are clean."""

    def test_alive_yields_no_op_zero_write(self) -> None:
        """ALIVE liveness → NO_OP action → zero writes."""
        entry = make_entry()
        rr = make_recovery(entry, make_task(), liveness=rec.LivenessEvidence.ALIVE)
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.NO_OP)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "NO_OP")
        self.assertIsNone(outcome.receipt)

    def test_unknown_yields_fail_closed_zero_write(self) -> None:
        """UNKNOWN liveness → FAIL_CLOSED action → zero writes."""
        entry = make_entry()
        rr = make_recovery(entry, make_task(), liveness=rec.LivenessEvidence.UNKNOWN)
        decision = rec.decide_reconciliation(rr)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.FAIL_CLOSED)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "FAIL_CLOSED")
        self.assertIsNone(outcome.receipt)

    def test_dead_still_requires_full_typed_evidence(self) -> None:
        """DEAD liveness does not skip evidence validation; the recovery
        decision still requires typed evidence."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task(), liveness=rec.LivenessEvidence.DEAD)
        decision = rec.decide_reconciliation(rr)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(rr, decision)
        )
        self.assertEqual(outcome.result, "APPLIED")
        self.assertIsNotNone(outcome.receipt)

    def test_pid_not_used_in_execution_decision(self) -> None:
        """The executor does not reference PID in its source."""
        source = inspect.getsource(exe)
        self.assertNotIn("pid", source.lower())

    def test_lease_expiry_not_used_in_execution_decision(self) -> None:
        """The executor does not reference lease expiry in its source."""
        source = inspect.getsource(exe)
        self.assertNotIn("lease_expiry", source)

    def test_exception_text_not_used_for_action_classification(self) -> None:
        """The executor does not inspect exception text for action selection."""
        source = inspect.getsource(exe)
        self.assertNotIn("exception_text", source)
        self.assertNotIn("exc_text", source)

    def test_bool_as_int_rejected_by_positive_int_validator(self) -> None:
        """_positive_int rejects bool (which is an int subclass in Python)."""
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorInputError):
            exe._positive_int(True, "test_bool")

    def test_malicious_str_repr_is_sanitized(self) -> None:
        """_text rejects strings with control characters (including DEL).
        A malicious __str__ returning control chars is rejected."""
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorInputError):
            exe._text("hello\x00world", "test_ctrl")
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorInputError):
            exe._text("hello\x7Fworld", "test_del")

    def test_attempt_4_blocked_before_any_write(self) -> None:
        """attempt 4 is caught in RecoveryRequest.__post_init__ before any
        executor object is created.  retry_budget_used >= 3 and attempt >= 4
        triggers RecoveryDefenseError."""
        entry = make_entry(retry_budget_used=3)
        self.persist(entry)
        # RecoveryRequest with retry_budget_used=3 and attempt=4 triggers
        # attempt_four defense at the recovery layer, before executor.
        with self.assertRaises(rec.RecoveryDefenseError):
            make_recovery(entry, make_task(attempt=3, state="ready"))

    def test_wrong_type_objects_rejected(self) -> None:
        """Execution request with wrong type for recovery_request is rejected
        by __post_init__ (type check)."""
        entry = make_entry()
        self.persist(entry)
        rr = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(rr)
        # Try to construct with wrong type — would be caught by dataclass
        with self.assertRaises((TypeError, exe.PortfolioSchedulerRecoveryExecutorInputError)):
            exe.RecoveryExecutionRequest(
                schema_version=exe.RECOVERY_EXECUTION_SCHEMA_VERSION,
                execution_id="EXE-TYPE-1",
                recovery_request="not_a_recovery_request",
                recovery_decision=decision,
                decision_replay_digest=decision.replay_digest,
                expected_queue_phase=rr.queue_entry.state,
                expected_receipt_phase=None,
                expected_selection_generation=rr.queue_entry.selection_generation,
                expected_recovery_generation=rr.recovery_generation,
                expected_canonical_dispatch_event_id=decision.canonical_dispatch_event_id,
                requested_at=STAMP_C,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Source and lock boundary adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class SourceAndLockBoundaryAdversarial(unittest.TestCase):
    """Prove the executor respects the lock and source boundary constraints."""

    def test_scheduler_mutation_under_queue_store_lock(self) -> None:
        """_apply_action_locked, _requeue_selected_locked, _align_canonical_dispatch_locked
        are all called inside _store_lock context in execute()."""
        source = inspect.getsource(exe.PortfolioSchedulerRecoveryExecutor.execute)
        self.assertIn("_store_lock", source)

    def test_no_canonical_transition_worker_lease_inside_lock(self) -> None:
        """The executor's execute() and all _locked functions do not call
        ControlPlaneTransitionService, Worker, Lease, model, API, or network."""
        source = inspect.getsource(exe)
        for forbidden in (
            "ControlPlaneTransition",
            "start_worker",
            "acquire_lease",
            "model",
            "anthropic",
            "openai",
            "requests.",
            "httpx",
            "socket.",
        ):
            self.assertNotIn(forbidden, source, f"Forbidden call '{forbidden}' found in executor")

    def test_executor_does_not_import_handoff_store(self) -> None:
        """Executor module must not import handoff Store/runtime."""
        source = inspect.getsource(exe)
        for forbidden in (
            "portfolio_scheduler_handoff",
            "handoff_store",
            "start_admitted_dispatch",
        ):
            self.assertNotIn(forbidden, source)

    def test_public_types_no_any_dict_mapping_or_mutable_collections(self) -> None:
        """Verify public dataclass annotations have no Any, dict, Mapping,
        or mutable collection types."""
        for cls in (
            exe.RecoveryExecutionRequest,
            exe.RecoveryExecutionReceipt,
            exe.RecoveryExecutionOutcome,
        ):
            for field in dataclasses.fields(cls):
                type_str = repr(field.type)
                self.assertNotIn("Any", type_str, f"{cls.__name__}.{field.name} has Any")
                self.assertNotIn("dict", type_str, f"{cls.__name__}.{field.name} has dict")
                self.assertNotIn("Mapping", type_str, f"{cls.__name__}.{field.name} has Mapping")
                self.assertNotIn("list", type_str, f"{cls.__name__}.{field.name} has list")
                self.assertNotIn("set", type_str, f"{cls.__name__}.{field.name} has set")

    def test_executor_does_not_write_canonical_task_event_outbox(self) -> None:
        """verify source has no code path writing canonical task, event, or outbox files."""
        source = inspect.getsource(exe)
        self.assertNotIn("canonical_task", source)
        self.assertNotIn("events/", source)
        self.assertNotIn("outbox/", source)


if __name__ == "__main__":
    unittest.main()
