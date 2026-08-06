"""TC-13.26b targeted tests for admitted-dispatch handoff runtime."""

from __future__ import annotations

import inspect
import os
import subprocess
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
from dispatch_supervisor_evidence import ProcessLiveness
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
    @unittest.skipUnless(os.name == "nt", "Windows canonical path rule")
    def test_windows_worktree_identity_is_case_insensitive_but_path_exact(self) -> None:
        self.assertTrue(runtime._same_worktree(r"C:\\Repo\\WT-1", r"c:\\repo\\WT-1"))
        self.assertFalse(runtime._same_worktree(r"C:\\Repo\\WT-1", r"c:\\repo\\WT-2"))

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

    def test_git_worktree_identity_is_reread_from_real_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            runtime._verify_git_worktree(root, head, "main")
            (root / "dirty.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaises(runtime.AdmittedDispatchConflictError):
                runtime._verify_git_worktree(root, head, "main")


class HandoffRuntimeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._git_patcher = patch.object(runtime, "_verify_git_worktree")
        self._git_patcher.start()

    async def asyncTearDown(self) -> None:
        self._git_patcher.stop()

    def _existing_generation_context(self, request):
        plan = SimpleNamespace(
            receipt_id=request.receipt_id, task_id=request.task_id,
            revision=request.revision, dispatch_id=request.dispatch_id,
            event_id=request.dispatch_event_id,
            outbox_message_id=request.outbox_message_id,
            selection_generation=request.selection_generation,
            content_digest=request.plan_digest,
            base_commit="a" * 40, branch="agentdesk/TC-026/r1/a1/DSP-26",
        )
        scheduler = MagicMock()
        scheduler.read_admission_plan.return_value = plan
        scheduler.read_queue_entry.return_value = SimpleNamespace(state=QueuePhase.DISPATCHED)
        scheduler.read_schedule_receipt.return_value = SimpleNamespace(phase=ReceiptPhase.DISPATCHED)
        workspace = str(request.dispatch_cycle_request.dispatch_request.workspace)
        worktrees = MagicMock()
        worktrees.read_record.return_value = SimpleNamespace(
            phase=WorktreePhase.READY, worktree_path=workspace,
            dispatch_id=request.dispatch_id, task_id=request.task_id,
            revision=request.revision, attempt=request.attempt,
            base_commit="a" * 40, head_commit="a" * 40,
            branch="agentdesk/TC-026/r1/a1/DSP-26",
        )
        event = SimpleNamespace(
            event_id=request.dispatch_event_id, event_type="TASK_DISPATCHED",
            dispatch_id=request.dispatch_id, from_state="ready",
            to_state="dispatched", occurred_at=request.requested_at,
        )
        snapshot = SimpleNamespace(
            events=(event,),
            outbox=(SimpleNamespace(message_id=request.outbox_message_id, event_id=event.event_id),),
            tasks=(SimpleNamespace(task_id=request.task_id, current_dispatch=SimpleNamespace(dispatch_id=request.dispatch_id)),),
        )
        return scheduler, worktrees, snapshot, workspace

    async def test_existing_alive_or_dead_generation_requires_recovery_without_write(self) -> None:
        for liveness in (ProcessLiveness.ALIVE, ProcessLiveness.DEAD):
            with self.subTest(liveness=liveness), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                request = _request(root)
                orchestrator = WorkflowOrchestrator(root, _Clock())
                scheduler, worktrees, snapshot, workspace = self._existing_generation_context(request)
                process = SimpleNamespace(generation_id="GEN-EXISTING", phase="SUPERVISOR_READY")
                handoffs = MagicMock()
                handoffs.read_handoff.return_value = SimpleNamespace(
                    dispatch_id=request.dispatch_id, handoff_id=request.handoff_id,
                    plan_digest=request.plan_digest,
                    phase=ScheduledDispatchHandoffPhase.HANDOFF_RESERVED,
                )
                start = AsyncMock()
                with (
                    patch.object(runtime, "resolve_invocation"),
                    patch.object(runtime, "PortfolioSchedulerStore", return_value=scheduler),
                    patch.object(runtime, "_lease", return_value=SimpleNamespace(canonical_worktree=workspace)),
                    patch.object(runtime, "WorktreeLifecycleStore", return_value=worktrees),
                    patch.object(runtime, "StateProvider", return_value=SimpleNamespace(snapshot=lambda: snapshot)),
                    patch.object(runtime.dse, "read_dispatch_receipt", return_value=process),
                    patch.object(runtime.dse, "probe_dispatch_process_tree", return_value=liveness),
                    patch.object(runtime, "PortfolioSchedulerWorkerHandoffStore", return_value=handoffs),
                    patch.object(WorkflowOrchestrator, "start_admitted_dispatch_execution", start),
                ):
                    result = await runtime.start_admitted_dispatch(request, {"claude": object()}, orchestrator)
                self.assertEqual(result.outcome, runtime.AdmittedDispatchStartOutcome.RECOVERY_REQUIRED)
                handoffs.reserve_handoff.assert_not_called()
                start.assert_not_awaited()

    async def test_existing_unknown_generation_fails_closed_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = _request(root)
            orchestrator = WorkflowOrchestrator(root, _Clock())
            scheduler, worktrees, snapshot, workspace = self._existing_generation_context(request)
            process = SimpleNamespace(generation_id="GEN-EXISTING", phase="SUPERVISOR_READY")
            handoffs = MagicMock()
            handoffs.read_handoff.return_value = SimpleNamespace(dispatch_id=request.dispatch_id, handoff_id=request.handoff_id, plan_digest=request.plan_digest, phase=ScheduledDispatchHandoffPhase.HANDOFF_RESERVED)
            with (
                patch.object(runtime, "resolve_invocation"), patch.object(runtime, "PortfolioSchedulerStore", return_value=scheduler),
                patch.object(runtime, "_lease", return_value=SimpleNamespace(canonical_worktree=workspace)), patch.object(runtime, "WorktreeLifecycleStore", return_value=worktrees),
                patch.object(runtime, "StateProvider", return_value=SimpleNamespace(snapshot=lambda: snapshot)), patch.object(runtime.dse, "read_dispatch_receipt", return_value=process),
                patch.object(runtime.dse, "probe_dispatch_process_tree", return_value=ProcessLiveness.UNKNOWN), patch.object(runtime, "PortfolioSchedulerWorkerHandoffStore", return_value=handoffs),
            ):
                with self.assertRaises(runtime.AdmittedDispatchConflictError):
                    await runtime.start_admitted_dispatch(request, {"claude": object()}, orchestrator)
            handoffs.reserve_handoff.assert_not_called()

    async def test_finalized_generation_replays_without_probe_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = _request(root)
            orchestrator = WorkflowOrchestrator(root, _Clock())
            scheduler, worktrees, snapshot, workspace = self._existing_generation_context(request)
            process = SimpleNamespace(generation_id="GEN-FINAL", phase="FINALIZING")
            handoffs = MagicMock()
            handoffs.read_handoff.return_value = SimpleNamespace(
                dispatch_id=request.dispatch_id, handoff_id=request.handoff_id,
                plan_digest=request.plan_digest,
                phase=ScheduledDispatchHandoffPhase.FINALIZED,
            )
            probe = MagicMock()
            with (
                patch.object(runtime, "resolve_invocation"), patch.object(runtime, "PortfolioSchedulerStore", return_value=scheduler),
                patch.object(runtime, "_lease", return_value=SimpleNamespace(canonical_worktree=workspace)), patch.object(runtime, "WorktreeLifecycleStore", return_value=worktrees),
                patch.object(runtime, "StateProvider", return_value=SimpleNamespace(snapshot=lambda: snapshot)), patch.object(runtime.dse, "read_dispatch_receipt", return_value=process),
                patch.object(runtime.dse, "read_dispatch_tombstone", return_value=SimpleNamespace(generation_id="GEN-FINAL")), patch.object(runtime.dse, "probe_dispatch_process_tree", probe),
                patch.object(runtime, "PortfolioSchedulerWorkerHandoffStore", return_value=handoffs),
            ):
                result = await runtime.start_admitted_dispatch(request, {"claude": object()}, orchestrator)
            self.assertEqual(result.outcome, runtime.AdmittedDispatchStartOutcome.REPLAYED)
            probe.assert_not_called()
            handoffs.reserve_handoff.assert_not_called()

    async def test_tombstone_resumes_finalizing_without_worker_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = _request(root)
            orchestrator = WorkflowOrchestrator(root, _Clock())
            scheduler, worktrees, snapshot, workspace = self._existing_generation_context(request)
            process = SimpleNamespace(generation_id="GEN-FINAL", phase="FINALIZING", written_at="2026-08-02T03:00:02.000000Z")
            handoffs = MagicMock()
            handoffs.read_handoff.return_value = SimpleNamespace(dispatch_id=request.dispatch_id, handoff_id=request.handoff_id, plan_digest=request.plan_digest, phase=ScheduledDispatchHandoffPhase.ACKNOWLEDGED)
            final = SimpleNamespace(phase=ScheduledDispatchHandoffPhase.FINALIZED)
            handoffs.advance_phase.side_effect = [(SimpleNamespace(phase=ScheduledDispatchHandoffPhase.FINALIZING), SimpleNamespace()), (final, SimpleNamespace())]
            start = AsyncMock()
            with (
                patch.object(runtime, "resolve_invocation"), patch.object(runtime, "PortfolioSchedulerStore", return_value=scheduler),
                patch.object(runtime, "_lease", return_value=SimpleNamespace(canonical_worktree=workspace)), patch.object(runtime, "WorktreeLifecycleStore", return_value=worktrees),
                patch.object(runtime, "StateProvider", return_value=SimpleNamespace(snapshot=lambda: snapshot)), patch.object(runtime.dse, "read_dispatch_receipt", return_value=process),
                patch.object(runtime.dse, "read_dispatch_tombstone", return_value=SimpleNamespace(generation_id="GEN-FINAL", finalized_at="2026-08-02T03:00:03.000000Z")),
                patch.object(runtime, "PortfolioSchedulerWorkerHandoffStore", return_value=handoffs), patch.object(WorkflowOrchestrator, "start_admitted_dispatch_execution", start),
            ):
                result = await runtime.start_admitted_dispatch(request, {"claude": object()}, orchestrator)
            self.assertEqual(result.outcome, runtime.AdmittedDispatchStartOutcome.REPLAYED)
            self.assertEqual(result.phase, ScheduledDispatchHandoffPhase.FINALIZED)
            self.assertEqual(handoffs.advance_phase.call_count, 2)
            start.assert_not_awaited()
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
                base_commit="a" * 40, branch="agentdesk/TC-026/r1/a1/DSP-26",
            )
            scheduler = MagicMock()
            scheduler.read_admission_plan.return_value = plan
            scheduler.read_queue_entry.return_value = SimpleNamespace(state=QueuePhase.DISPATCHED)
            scheduler.read_schedule_receipt.return_value = SimpleNamespace(phase=ReceiptPhase.DISPATCHED)
            worktrees = MagicMock()
            worktrees.read_record.return_value = SimpleNamespace(
                phase=WorktreePhase.READY,
                worktree_path=str(request.dispatch_cycle_request.dispatch_request.workspace),
                dispatch_id="DSP-26", task_id="TC-026", revision=1, attempt=1,
                base_commit="a" * 40, head_commit="a" * 40,
                branch="agentdesk/TC-026/r1/a1/DSP-26",
            )
            event = SimpleNamespace(event_id="EVT-26", event_type="TASK_DISPATCHED", dispatch_id="DSP-26", from_state="ready", to_state="dispatched", occurred_at="2026-08-02T03:00:00.000000Z")
            snapshot = SimpleNamespace(events=(event,), outbox=(SimpleNamespace(message_id="MSG-26", event_id="EVT-26"),), tasks=(SimpleNamespace(task_id="TC-026", current_dispatch=SimpleNamespace(dispatch_id="DSP-26")),))
            reserved_process = SimpleNamespace(generation_id="GEN-1", written_at="2026-08-02T03:00:00.500000Z", phase="RESERVED")
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
                patch.object(runtime.dse, "read_dispatch_receipt", side_effect=[reserved_process, process, final_process]),
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
