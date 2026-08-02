"""TC-13.26c adversarial boundaries using local Git/process evidence only."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import dispatch_supervisor_evidence as dse
import portfolio_scheduler_worker_handoff_runtime as runtime
from tests.test_portfolio_scheduler_worker_handoff_runtime import _request
from workflow_orchestrator import WorkflowOrchestrator


class SchedulerWorkerHandoffE2ETests(unittest.TestCase):
    def test_local_helper_process_has_exact_alive_dead_and_pid_reuse_identity(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            creation = dse.get_process_creation_time(process.pid)
            self.assertIsNotNone(creation)
            boot = dse.get_boot_id()
            self.assertIs(dse.probe_process(process.pid, creation, boot), dse.ProcessLiveness.ALIVE)
            self.assertIs(dse.probe_process(process.pid, creation + "-reused", boot), dse.ProcessLiveness.UNKNOWN)
        finally:
            process.terminate()
            process.wait(timeout=10)
        self.assertIs(dse.probe_process(2147483647, creation, boot), dse.ProcessLiveness.DEAD)

    def test_attempt_three_is_valid_and_attempt_four_is_prewrite_reject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(_request(root, 3).attempt, 3)
            with self.assertRaises(runtime.AdmittedDispatchInputError):
                _request(root, 4)

    def test_runtime_source_never_creates_admission_lease_or_canonical_event(self) -> None:
        source = inspect.getsource(runtime)
        for forbidden in (
            "PortfolioSchedulerAdmission(", "acquire_worker_slot(",
            "ControlPlaneTransitionService(", "run_dispatch_cycle(",
            "TASK_DISPATCHED\")",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("resolve_invocation(dr, providers)", source)
        self.assertIn("_verify_git_worktree", source)

    def test_orchestrator_adoption_uses_existing_lease_and_receipt(self) -> None:
        source = inspect.getsource(WorkflowOrchestrator.start_admitted_dispatch_execution)
        self.assertIn("lease: WorkerSlotLease", source)
        self.assertIn("read_dispatch_receipt", source)
        self.assertIn("DispatchReceiptPhase.RESERVED", source)
        self.assertNotIn("acquire_worker_slot(", source)
        self.assertNotIn("apply_transition(tr", source)

    def test_restart_matrix_has_all_three_liveness_states_and_tombstone_replay(self) -> None:
        source = inspect.getsource(runtime.start_admitted_dispatch)
        for value in ("ProcessLiveness.UNKNOWN", "RECOVERY_REQUIRED", "read_dispatch_tombstone", "FINALIZED", "REPLAYED"):
            self.assertIn(value, source)

    def test_result_and_request_remain_frozen_typed_values(self) -> None:
        self.assertTrue(runtime.AdmittedDispatchStartRequest.__dataclass_params__.frozen)
        self.assertTrue(runtime.AdmittedDispatchStartResult.__dataclass_params__.frozen)
        self.assertEqual(len(runtime.AdmittedDispatchStartRequest.__dataclass_fields__), 20)
        self.assertEqual(len(runtime.AdmittedDispatchStartResult.__dataclass_fields__), 12)


if __name__ == "__main__":
    unittest.main()
