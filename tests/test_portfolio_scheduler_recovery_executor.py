"""Incremental tests for the Scheduler-local recovery action executor."""

from __future__ import annotations

import dataclasses
import inspect
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import portfolio_scheduler_recovery as rec  # noqa: E402
import portfolio_scheduler_recovery_executor as exe  # noqa: E402
import portfolio_scheduler_store as ps  # noqa: E402
from core_types import WorkerKind  # noqa: E402
from state_provider import (  # noqa: E402
    DispatchInfo,
    EventEntry,
    ModelSelectionSnapshot,
    TaskEntry,
    TaskTimestamps,
)

STAMP = "2026-01-01T00:00:00.000000Z"
STAMP_B = "2026-01-01T01:00:00.000000Z"
STAMP_C = "2026-01-01T02:00:00.000000Z"


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


class ExecutorFixture(unittest.TestCase):
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
        self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.SELECTED, project_root=self.root)
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
        dispatched_receipt = self.store.read_schedule_receipt(receipt.receipt_id, self.root)
        return dispatched_entry, dispatched_receipt, event


class CodecTypeFieldOrderTests(ExecutorFixture):
    def test_public_types_are_frozen_slotted_and_exact(self) -> None:
        expected = {
            exe.RecoveryExecutionRequest: (
                "schema_version", "execution_id", "recovery_request",
                "recovery_decision", "decision_replay_digest", "expected_queue_phase",
                "expected_receipt_phase", "expected_selection_generation",
                "expected_recovery_generation", "expected_canonical_dispatch_event_id",
                "requested_at",
            ),
            exe.RecoveryExecutionReceipt: (
                "schema_version", "execution_id", "queue_id", "receipt_id", "task_id",
                "revision", "selection_generation", "recovery_generation", "recovery_action",
                "recovery_reason", "canonical_dispatch_event_id", "decision_replay_digest",
                "phase", "reserved_at", "applied_at", "finalized_at", "content_digest",
            ),
            exe.RecoveryExecutionOutcome: (
                "schema_version", "execution_id", "phase", "result", "recovery_action", "receipt",
            ),
        }
        for cls, names in expected.items():
            self.assertTrue(dataclasses.is_dataclass(cls))
            self.assertEqual(tuple(field.name for field in dataclasses.fields(cls)), names)
            self.assertTrue(getattr(cls, "__dataclass_params__").frozen)
            self.assertTrue(hasattr(cls, "__slots__"))

    def test_phase_enum_is_exact_forward_only_sequence(self) -> None:
        self.assertEqual(
            tuple(item.value for item in exe.RecoveryExecutionPhase),
            ("RESERVED", "VALIDATED", "APPLYING", "APPLIED", "FINALIZED"),
        )
        self.assertEqual(exe._PHASE_ORDER, tuple(item.value for item in exe.RecoveryExecutionPhase))

    def test_receipt_round_trip_is_canonical_and_byte_exact(self) -> None:
        entry = make_entry()
        recovery_request = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(recovery_request)
        execution_request = make_execution_request(recovery_request, decision)
        receipt = exe._new_receipt(execution_request)
        encoded = exe.encode_recovery_execution_receipt(receipt)
        decoded = exe.decode_recovery_execution_receipt(encoded)
        self.assertEqual(decoded, receipt)
        self.assertEqual(exe.encode_recovery_execution_receipt(decoded), encoded)

    def test_public_annotations_do_not_expose_untyped_containers(self) -> None:
        for cls in (
            exe.RecoveryExecutionRequest,
            exe.RecoveryExecutionReceipt,
            exe.RecoveryExecutionOutcome,
        ):
            annotations = repr(getattr(cls, "__annotations__"))
            for forbidden in ("Any", "Mapping", "dict", "list", "set"):
                self.assertNotIn(forbidden, annotations)


class ZeroWriteAndActionTests(ExecutorFixture):
    def test_no_op_has_no_scheduler_write(self) -> None:
        entry = make_entry()
        recovery_request = make_recovery(
            entry, make_task(), liveness=rec.LivenessEvidence.ALIVE
        )
        decision = rec.decide_reconciliation(recovery_request)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "NO_OP")
        self.assertIsNone(outcome.receipt)
        self.assertFalse((self.root / "docs").exists())

    def test_unknown_is_fail_closed_without_write(self) -> None:
        entry = make_entry()
        recovery_request = make_recovery(
            entry, make_task(), liveness=rec.LivenessEvidence.UNKNOWN
        )
        decision = rec.decide_reconciliation(recovery_request)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "FAIL_CLOSED")
        self.assertFalse((self.root / "docs").exists())

    def test_wait_for_owner_is_zero_write(self) -> None:
        entry = make_entry()
        recovery_request = make_recovery(
            entry,
            make_task(state="dispatched", attempt=1, dispatch_id="DISP-NO-EVENT"),
        )
        decision = rec.decide_reconciliation(recovery_request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.WAIT_FOR_EXECUTION_OWNER)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "NO_OP")
        self.assertFalse((self.root / "docs").exists())

    def test_resume_persists_scheduler_evidence_only(self) -> None:
        entry = make_entry()
        self.persist(entry)
        recovery_request = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(recovery_request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RESUME)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "APPLIED")
        self.assertEqual(outcome.phase, exe.RecoveryExecutionPhase.FINALIZED)
        self.assertIsNotNone(outcome.receipt)
        self.assertEqual(self.store.read_queue_entry(entry.queue_id, self.root), entry)

    def test_selected_without_receipt_fails_closed_without_execution_receipt(self) -> None:
        entry = make_entry()
        self.persist(entry)
        self.store.advance_queue_phase(entry.queue_id, 1, ps.QueuePhase.SELECTED, project_root=self.root)
        selected_entry = dataclasses.replace(
            entry,
            state=ps.QueuePhase.SELECTED,
            content_digest=ps._entry_digest(
                dataclasses.replace(entry, state=ps.QueuePhase.SELECTED)
            ),
        )
        recovery_request = make_recovery(selected_entry, make_task())
        decision = rec.decide_reconciliation(recovery_request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.INVALIDATE_SELECTION)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "FAIL_CLOSED")
        self.assertFalse((self.root / "docs" / "pm" / "portfolio-scheduler" / "recovery-actions").exists())


class MutationActionTests(ExecutorFixture):
    def test_requeue_selected_uses_store_recovery_identity(self) -> None:
        entry, schedule_receipt = self.selected()
        recovery_request = make_recovery(entry, make_task(), schedule_receipt)
        decision = rec.decide_reconciliation(recovery_request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.REQUEUE_SELECTED)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        first = executor.execute(make_execution_request(recovery_request, decision))
        self.assertEqual(first.result, "APPLIED")
        updated = self.store.read_queue_entry(entry.queue_id, self.root)
        self.assertEqual(updated.state, ps.QueuePhase.QUEUED)
        self.assertEqual(updated.selection_generation, 2)
        replay = executor.execute(make_execution_request(recovery_request, decision))
        self.assertEqual(replay.result, "REPLAYED")
        self.assertEqual(self.store.read_queue_entry(entry.queue_id, self.root), updated)

    def test_adopt_canonical_dispatch_aligns_receipt_then_queue(self) -> None:
        entry, schedule_receipt = self.selected()
        event = make_event()
        recovery_request = make_recovery(
            entry,
            make_task(state="dispatched", attempt=1, dispatch_id=event.dispatch_id),
            schedule_receipt,
            (event,),
        )
        decision = rec.decide_reconciliation(recovery_request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.ADOPT_CANONICAL_DISPATCH)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "APPLIED")
        updated_entry = self.store.read_queue_entry(entry.queue_id, self.root)
        updated_receipt = self.store.read_schedule_receipt(schedule_receipt.receipt_id, self.root)
        self.assertEqual(updated_entry.state, ps.QueuePhase.DISPATCHED)
        self.assertEqual(updated_receipt.phase, ps.ReceiptPhase.DISPATCHED)
        self.assertEqual(updated_receipt.dispatch_event_id, event.event_id)

    def test_retire_requires_canonical_event_and_preserves_history(self) -> None:
        entry, schedule_receipt, event = self.dispatched()
        recovery_request = make_recovery(
            entry,
            make_task(state="dispatched", attempt=1, dispatch_id=event.dispatch_id),
            schedule_receipt,
            (event,),
        )
        decision = rec.decide_reconciliation(recovery_request)
        self.assertIs(decision.recovery_action, rec.RecoveryAction.RETIRE_QUEUE_ENTRY)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision)
        )
        self.assertEqual(outcome.result, "APPLIED")
        self.assertEqual(
            self.store.read_queue_entry(entry.queue_id, self.root).state,
            ps.QueuePhase.RETIRED,
        )
        self.assertEqual(
            self.store.read_schedule_receipt(schedule_receipt.receipt_id, self.root).phase,
            ps.ReceiptPhase.DISPATCHED,
        )


class CrashReplayAndDivergenceTests(ExecutorFixture):
    def test_reserved_crash_resumes_forward_only(self) -> None:
        entry = make_entry()
        self.persist(entry)
        recovery_request = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(recovery_request)
        execution_request = make_execution_request(recovery_request, decision)
        with ps._store_lock(self.root):
            store_root = ps._prepare_store(self.root, True)
            exe._write_execution_receipt(store_root, exe._new_receipt(execution_request))
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(execution_request)
        self.assertEqual(outcome.result, "APPLIED")
        self.assertEqual(outcome.phase, exe.RecoveryExecutionPhase.FINALIZED)

    def test_finalized_replay_is_byte_exact(self) -> None:
        entry = make_entry()
        self.persist(entry)
        recovery_request = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(recovery_request)
        execution_request = make_execution_request(recovery_request, decision)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        first = executor.execute(execution_request)
        path = self.root / "docs" / "pm" / "portfolio-scheduler" / "recovery-actions" / entry.queue_id / "g1.yaml"
        before = path.read_bytes()
        second = executor.execute(execution_request)
        self.assertEqual(second.result, "REPLAYED")
        self.assertEqual(second.receipt, first.receipt)
        self.assertEqual(path.read_bytes(), before)

    def test_same_generation_divergent_digest_is_typed_conflict(self) -> None:
        entry = make_entry()
        self.persist(entry)
        first_request = make_recovery(entry, make_task())
        first_decision = rec.decide_reconciliation(first_request)
        executor = exe.PortfolioSchedulerRecoveryExecutor(self.root)
        executor.execute(make_execution_request(first_request, first_decision))
        unrelated_event = make_event("EVT-UNRELATED", "DISP-UNRELATED")
        second_request = make_recovery(entry, make_task(), events=(unrelated_event,))
        second_decision = rec.decide_reconciliation(second_request)
        with self.assertRaises(exe.PortfolioSchedulerRecoveryExecutorConflictError):
            executor.execute(make_execution_request(second_request, second_decision))


class ConcurrencyLivenessAndAttemptTests(ExecutorFixture):
    def test_concurrent_same_execution_has_one_apply_and_one_replay(self) -> None:
        entry = make_entry()
        self.persist(entry)
        recovery_request = make_recovery(entry, make_task())
        decision = rec.decide_reconciliation(recovery_request)
        execution_request = make_execution_request(recovery_request, decision)

        def run() -> str:
            try:
                return exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(execution_request).result
            except ps.PortfolioSchedulerLockConflictError:
                return "CONFLICT"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: run(), (1, 2)))
        self.assertEqual(results.count("APPLIED"), 1)
        self.assertIn(results[0], ("APPLIED", "REPLAYED", "CONFLICT"))
        self.assertIn(results[1], ("APPLIED", "REPLAYED", "CONFLICT"))
        self.assertNotEqual(results[0], results[1], "one call must be the durable winner")

    def test_alive_unknown_dead_are_not_reclassified(self) -> None:
        for liveness, expected in (
            (rec.LivenessEvidence.ALIVE, "NO_OP"),
            (rec.LivenessEvidence.UNKNOWN, "FAIL_CLOSED"),
        ):
            entry = make_entry()
            recovery_request = make_recovery(make_entry(), make_task(), liveness=liveness)
            decision = rec.decide_reconciliation(recovery_request)
            outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
                make_execution_request(recovery_request, decision, f"EXE-{liveness.value}")
            )
            self.assertEqual(outcome.result, expected)
        entry = make_entry()
        self.persist(entry)
        recovery_request = make_recovery(entry, make_task(), liveness=rec.LivenessEvidence.DEAD)
        decision = rec.decide_reconciliation(recovery_request)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision, "EXE-dead")
        )
        self.assertEqual(outcome.result, "APPLIED")

    def test_attempt_three_is_allowed_and_attempt_four_fails_before_write(self) -> None:
        entry = make_entry(retry_budget_used=2)
        self.persist(entry)
        recovery_request = make_recovery(entry, make_task(attempt=3))
        decision = rec.decide_reconciliation(recovery_request)
        outcome = exe.PortfolioSchedulerRecoveryExecutor(self.root).execute(
            make_execution_request(recovery_request, decision, "EXE-attempt-3")
        )
        self.assertEqual(outcome.result, "APPLIED")
        fourth = make_entry(retry_budget_used=3)
        with self.assertRaises(rec.RecoveryDefenseError):
            make_recovery(fourth, make_task(attempt=4))


class SourceBoundaryTests(unittest.TestCase):
    def test_executor_has_no_forbidden_runtime_boundary(self) -> None:
        source = inspect.getsource(exe)
        for forbidden in (
            "portfolio_scheduler_admission",
            "portfolio_scheduler_policy",
            "worker_slot_lease",
            "control_plane_transition",
            "workflow_orchestrator",
            "subprocess",
            "socket",
            "requests",
            "httpx",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("StateProvider(", source)
