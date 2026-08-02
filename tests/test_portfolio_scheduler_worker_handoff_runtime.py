"""TC-13.26b targeted tests for admitted-dispatch handoff runtime."""

from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import portfolio_scheduler_worker_handoff_runtime as runtime
from core_types import WorkerKind
from portfolio_scheduler_store import QueuePhase, ReceiptPhase
from portfolio_scheduler_worker_handoff_store import ScheduledDispatchHandoffPhase
from workflow_orchestrator import DispatchCycleRequest, WorkflowOrchestrator
from worktree_lifecycle_store import WorktreePhase


class _Clock:
    def now(self):
        raise AssertionError("clock not used by facade test")
    def monotonic(self):
        return 0.0
    async def sleep(self, seconds):
        return None


def _cycle(root: Path) -> DispatchCycleRequest:
    value = object.__new__(DispatchCycleRequest)
    identity = SimpleNamespace(task_id="TC-026", revision=1, attempt=1, dispatch_id="DSP-26")
    dispatch = SimpleNamespace(identity=identity, workspace=root / "managed", model_selection=SimpleNamespace(model_binding_id="BIND-1"))
    object.__setattr__(value, "dispatch_request", dispatch)
    object.__setattr__(value, "holder_instance_id", "INSTANCE-1")
    return value


def _request(root: Path, attempt: int = 1) -> runtime.AdmittedDispatchStartRequest:
    cycle = _cycle(root)
    cycle.dispatch_request.identity.attempt = attempt
    return runtime.AdmittedDispatchStartRequest(
        runtime.SCHEMA_VERSION, "OP-1", str(root), "Q-1", "RCP-1", "TC-026", 1,
        attempt, "DSP-26", "EVT-26", "MSG-26", 1, "sha256:" + "1" * 64,
        "HNDF-26", "WT-26", "WSL-26", 1, "INSTANCE-1",
        "2026-08-02T03:00:00.000000Z", cycle,
    )


class HandoffRuntimeTypeTests(unittest.TestCase):
    def test_public_field_order_and_frozen_slots(self) -> None:
        self.assertEqual(tuple(runtime.AdmittedDispatchStartRequest.__dataclass_fields__), (
            "schema_version", "operation_id", "project_root", "queue_id", "receipt_id",
            "task_id", "revision", "attempt", "dispatch_id", "dispatch_event_id",
            "outbox_message_id", "selection_generation", "plan_digest", "handoff_id",
            "worktree_id", "lease_id", "lease_epoch", "holder_instance_id", "requested_at",
            "dispatch_cycle_request",
        ))
        self.assertEqual(len(runtime.AdmittedDispatchStartResult.__dataclass_fields__), 12)
        self.assertTrue(runtime.AdmittedDispatchStartRequest.__dataclass_params__.frozen)
        self.assertTrue(hasattr(runtime.AdmittedDispatchStartRequest, "__slots__"))

    def test_attempt_four_fails_before_runtime_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(runtime.AdmittedDispatchInputError):
                _request(Path(directory).resolve(), 4)

    def test_orchestrator_adoption_source_has_no_admission_side_effect(self) -> None:
        source = inspect.getsource(WorkflowOrchestrator.start_admitted_dispatch_execution)
        self.assertNotIn("acquire_worker_slot(", source)
        self.assertNotIn("apply_transition(tr", source)
        self.assertNotIn("run_dispatch_cycle(", source)
        self.assertIn("read_dispatch_receipt", source)


class HandoffRuntimeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_precedes_all_durable_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = _request(root)
            orchestrator = WorkflowOrchestrator(root, _Clock())
            with (
                patch.object(runtime, "resolve_invocation", side_effect=ValueError("provider")),
                patch.object(runtime, "PortfolioSchedulerStore") as scheduler_store,
            ):
                with self.assertRaises(ValueError):
                    await runtime.start_admitted_dispatch(request, {"x": object()}, orchestrator)
            scheduler_store.assert_not_called()

    async def test_happy_path_uses_existing_identity_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = _request(root)
            orchestrator = WorkflowOrchestrator(root, _Clock())
            lease = SimpleNamespace(canonical_worktree=str(request.dispatch_cycle_request.dispatch_request.workspace))
            plan = SimpleNamespace(
                receipt_id="RCP-1", task_id="TC-026", revision=1, dispatch_id="DSP-26",
                event_id="EVT-26", outbox_message_id="MSG-26", selection_generation=1,
                content_digest=request.plan_digest, worker_kind=WorkerKind.BASIC_AGENT, assessment_id="ASM-1",
                expected_snapshot_commit="a" * 40,
                model_selection=SimpleNamespace(model_binding_id="BIND-1"),
            )
            scheduler = MagicMock()
            scheduler.read_admission_plan.return_value = plan
            scheduler.read_queue_entry.return_value = SimpleNamespace(state=QueuePhase.DISPATCHED)
            scheduler.read_schedule_receipt.return_value = SimpleNamespace(phase=ReceiptPhase.DISPATCHED)
            worktrees = MagicMock()
            worktrees.read_record.return_value = SimpleNamespace(
                phase=WorktreePhase.READY,
                worktree_path=str(request.dispatch_cycle_request.dispatch_request.workspace),
                dispatch_id="DSP-26",
            )
            event = SimpleNamespace(event_id="EVT-26", event_type="TASK_DISPATCHED", dispatch_id="DSP-26", from_state="ready", to_state="dispatched", occurred_at="2026-08-02T03:00:00.000000Z")
            snapshot = SimpleNamespace(events=(event,), outbox=(SimpleNamespace(message_id="MSG-26", event_id="EVT-26"),), tasks=(SimpleNamespace(task_id="TC-026", current_dispatch="DSP-26"),))
            process = SimpleNamespace(generation_id="GEN-1", written_at="2026-08-02T03:00:01.000000Z", phase="WORKER_STARTED")
            final_process = SimpleNamespace(generation_id="GEN-1", written_at="2026-08-02T03:00:02.000000Z", phase="FINALIZING")
            execution = SimpleNamespace(wait=AsyncMock(return_value=object()))
            start_execution = AsyncMock(return_value=execution)
            handoffs = MagicMock()
            handoffs.reserve_handoff.return_value = (SimpleNamespace(), SimpleNamespace())
            finalized = SimpleNamespace(phase=ScheduledDispatchHandoffPhase.FINALIZED)
            handoffs.advance_phase.side_effect = [(SimpleNamespace(), SimpleNamespace()), (SimpleNamespace(), SimpleNamespace()), (SimpleNamespace(), SimpleNamespace()), (SimpleNamespace(), SimpleNamespace()), (finalized, SimpleNamespace())]
            with (
                patch.object(runtime, "resolve_invocation"),
                patch.object(runtime, "PortfolioSchedulerStore", return_value=scheduler),
                patch.object(runtime, "_lease", return_value=lease),
                patch.object(runtime, "WorktreeLifecycleStore", return_value=worktrees),
                patch.object(runtime, "StateProvider", return_value=SimpleNamespace(snapshot=lambda: snapshot)),
                patch.object(runtime.dse, "read_dispatch_receipt", side_effect=[process, process, final_process]),
                patch.object(runtime.dse, "read_dispatch_tombstone", return_value=SimpleNamespace(generation_id="GEN-1", finalized_at="2026-08-02T03:00:03.000000Z")),
                patch.object(runtime, "PortfolioSchedulerWorkerHandoffStore", return_value=handoffs),
                patch.object(WorkflowOrchestrator, "start_admitted_dispatch_execution", start_execution),
            ):
                result = await runtime.start_admitted_dispatch(request, {"claude": object()}, orchestrator)
            self.assertEqual(result.phase, ScheduledDispatchHandoffPhase.FINALIZED)
            self.assertEqual(result.outcome, runtime.AdmittedDispatchStartOutcome.STARTED)
            self.assertEqual(handoffs.advance_phase.call_count, 5)
            start_execution.assert_awaited_once()

    async def test_divergent_plan_rejected_before_process_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = _request(root)
            orchestrator = WorkflowOrchestrator(root, _Clock())
            scheduler = MagicMock()
            scheduler.read_admission_plan.return_value = SimpleNamespace(
                receipt_id="RCP-1", task_id="TC-026", revision=1,
                dispatch_id="DSP-WRONG", event_id="EVT-26", outbox_message_id="MSG-26",
                selection_generation=1, content_digest=request.plan_digest,
            )
            scheduler.read_queue_entry.return_value = SimpleNamespace(state=QueuePhase.DISPATCHED)
            scheduler.read_schedule_receipt.return_value = SimpleNamespace(phase=ReceiptPhase.DISPATCHED)
            with (
                patch.object(runtime, "resolve_invocation"),
                patch.object(runtime, "PortfolioSchedulerStore", return_value=scheduler),
                patch.object(runtime.dse, "reserve_receipt") as reserve,
            ):
                with self.assertRaises(runtime.AdmittedDispatchConflictError):
                    await runtime.start_admitted_dispatch(request, {"claude": object()}, orchestrator)
            reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
