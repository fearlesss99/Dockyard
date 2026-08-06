"""Directed Dockyard composition tests for real owner-loss recovery."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dispatch_supervisor_evidence as dispatch_evidence  # noqa: E402
from core_types import WorkerKind  # noqa: E402
from dockyard_owner_loss_recovery import (  # noqa: E402
    DockyardOwnerLossRecoveryOutcome,
    DockyardOwnerLossRecoveryRuntime,
)
from dockyard_composition import DockyardPlanReadService  # noqa: E402
from portfolio_scheduler_store import (  # noqa: E402
    AdmissionPlanReservation,
    ModelSelectionSnapshot,
    PortfolioSchedulerStore,
    SCHEMA_VERSION as SCHEDULER_SCHEMA_VERSION,
    _plan_digest,
)
from portfolio_scheduler_worker_handoff_store import (  # noqa: E402
    HANDOFF_SCHEMA_VERSION,
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoff,
    ScheduledDispatchHandoffPhase,
    _with_handoff_digest,
)
from state_provider import StateProvider  # noqa: E402
from tests import test_workflow_owner_loss_e2e as owner_fixtures  # noqa: E402
from tests.test_workflow_orchestrator import FakeClock, _git_head  # noqa: E402
from worker_slot_lease import read_worker_slot_leases  # noqa: E402


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()


class DockyardOwnerLossRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        helper = owner_fixtures.OwnerLossAutomaticRetryE2E(
            "test_alive_is_zero_transition_zero_retry"
        )
        self.root, self.owner_request = helper._prepare_dead_active_dispatch()
        self.plan = self._install_dockyard_source_evidence()
        receipt = dispatch_evidence.read_dispatch_receipt(
            self.root, self.owner_request.expected_dispatch_id
        )
        assert receipt is not None
        token = hashlib.sha256(
            (
                "dockyard-owner-loss\0"
                + self.owner_request.expected_dispatch_id
                + "\0"
                + receipt.generation_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        self.next_dispatch_id = "DSP-OWNER-RETRY-" + token
        self.provider = owner_fixtures._LocalPythonProvider(
            owner_fixtures._claude_success(
                task_id="TC-001",
                revision=1,
                attempt=2,
                dispatch_id=self.next_dispatch_id,
                commit=_git_head(self.root),
            ),
            reservation_project=self.root,
            reservation_dispatch_id=self.next_dispatch_id,
        )
        self.clock = FakeClock(
            start=datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _install_dockyard_source_evidence(self) -> AdmissionPlanReservation:
        source_approval = (
            self.root
            / "docs/pm/approvals"
            / f"EVT-OWNER-DEAD-APPROVAL-{self.owner_request.expected_attempt}.yaml"
        )
        approval = json.loads(source_approval.read_text(encoding="utf-8"))
        approval["expires_at"] = None
        approval["reason"] = "Dockyard confirmed plan approval"
        source_approval.write_text(
            json.dumps(approval, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        card = self.root / "tasks/TC-001/task.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text("Implement the bounded Dockyard recovery fixture.\n", encoding="utf-8")
        head = _git_head(self.root)
        snapshot = StateProvider(self.root).snapshot()
        task = snapshot.tasks[0]
        assert task.current_dispatch is not None
        failed_attempt = self.owner_request.expected_attempt
        model = task.current_dispatch.model_selection
        scheduler_model = ModelSelectionSnapshot(
            model.required_model_tier,
            model.required_model_capabilities,
            model.model_binding_id,
            model.selected_model_provider,
            model.selected_model_id,
            model.selected_model_tier,
            model.selected_deliberation_tier,
            model.selected_context_window_tokens,
            model.selected_model_capabilities,
            model.model_degradation_approval_id,
        )
        leases = read_worker_slot_leases(self.root)["leases"]
        assert isinstance(leases, dict)
        lease_values = tuple(
            value for value in leases.values()
            if isinstance(value, dict)
            and value.get("holder_dispatch_id") == self.owner_request.expected_dispatch_id
        )
        self.assertEqual(len(lease_values), 1)
        lease = lease_values[0]
        plan = AdmissionPlanReservation(
            SCHEDULER_SCHEMA_VERSION,
            "Q-OWNER-LOSS-1",
            "SR-OWNER-LOSS-1",
            "TC-001",
            1,
            1,
            1,
            WorkerKind.ADVANCED_AGENT,
            "ASM-TC-001-r1",
            self.owner_request.expected_dispatch_id,
            next(event.event_id for event in snapshot.events if event.event_type == "TASK_DISPATCHED"),
            next(item.message_id for item in snapshot.outbox if item.dispatch_id == self.owner_request.expected_dispatch_id),
            "agent",
            "tasks/TC-001/task.md",
            task.task_card_commit,
            task.current_dispatch.base_commit,
            task.current_dispatch.branch,
            "reports/report.md",
            scheduler_model,
            "ready",
            failed_attempt - 1,
            failed_attempt,
            head,
            SCHEDULER_SCHEMA_VERSION,
            str(lease["holder_instance_id"]),
            str(self.root),
            "2026-08-04T05:00:00.000000Z",
            "sha256:" + "0" * 64,
        )
        plan = replace(plan, content_digest=_plan_digest(plan))
        PortfolioSchedulerStore(self.root).reserve_admission_plan(plan)
        receipt = dispatch_evidence.read_dispatch_receipt(
            self.root, self.owner_request.expected_dispatch_id
        )
        assert receipt is not None
        handoff = _with_handoff_digest(ScheduledDispatchHandoff(
            HANDOFF_SCHEMA_VERSION,
            "HNDF-OWNER-LOSS-1",
            plan.queue_id,
            plan.content_digest,
            plan.receipt_id,
            plan.task_id,
            plan.revision,
            plan.new_attempt,
            plan.dispatch_id,
            plan.event_id,
            plan.outbox_message_id,
            str(lease["lease_id"]),
            int(lease["lease_epoch"]),
            plan.holder_instance_id,
            plan.worker_kind,
            plan.assessment_id,
            head,
            plan.model_selection.model_binding_id,
            ScheduledDispatchHandoffPhase.ADMISSION_COMMITTED,
            "2026-08-04T05:00:00.000000Z",
            None,
            None,
            None,
            None,
            "sha256:" + "0" * 64,
        ))
        store = PortfolioSchedulerWorkerHandoffStore(self.root)
        store.reserve_handoff(handoff, receipt)
        for phase, timestamp in (
            (ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED, "2026-08-04T05:00:01.000000Z"),
            (ScheduledDispatchHandoffPhase.WORKER_STARTED, "2026-08-04T05:00:02.000000Z"),
            (ScheduledDispatchHandoffPhase.ACKNOWLEDGED, "2026-08-04T05:00:03.000000Z"),
        ):
            store.advance_phase(plan.dispatch_id, phase, timestamp, receipt)
        return plan

    def _runtime(self) -> DockyardOwnerLossRecoveryRuntime:
        return DockyardOwnerLossRecoveryRuntime(
            self.root,
            {"claude": self.provider},
            (("claude", "2.1.214"),),
            clock=self.clock,
        )

    def test_real_dead_process_recovers_and_finalizes_attempt_two(self) -> None:
        runtime = self._runtime()
        results = runtime.recover_pending()
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIs(result.outcome, DockyardOwnerLossRecoveryOutcome.RETRY_FINALIZED)
        self.assertEqual(result.next_dispatch_id, self.next_dispatch_id)
        self.assertEqual(self.provider.build_count, 1)
        self.assertEqual(self.provider.phase_at_worker_build, "RETRY_RESERVED")
        snapshot = StateProvider(self.root).snapshot()
        task = snapshot.tasks[0]
        self.assertEqual((task.state, task.attempt), ("review_ready", 2))
        self.assertEqual(
            sum(event.event_type == "DISPATCH_FAILED" for event in snapshot.events),
            1,
        )
        self.assertEqual(
            sum(event.event_type == "TASK_DISPATCHED" for event in snapshot.events),
            2,
        )
        retry = dispatch_evidence.read_retry_receipt(self.root, self.next_dispatch_id)
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.phase, "RETRY_FINALIZED")

        replay = runtime._recover_identity(
            "TC-001", 1, 1, self.owner_request.expected_dispatch_id
        )
        self.assertEqual(replay, result)
        self.assertEqual(self.provider.build_count, 1)

    def test_alive_is_zero_write(self) -> None:
        before = StateProvider(self.root).snapshot()
        with (
            patch.object(
                dispatch_evidence,
                "probe_process",
                return_value=dispatch_evidence.ProcessLiveness.ALIVE,
            ),
            patch.object(
                dispatch_evidence,
                "probe_dispatch_process_tree",
                return_value=dispatch_evidence.ProcessLiveness.ALIVE,
            ),
        ):
            result = self._runtime().recover_pending()[0]
        self.assertIs(result.outcome, DockyardOwnerLossRecoveryOutcome.ALIVE)
        self.assertEqual(StateProvider(self.root).snapshot().events, before.events)
        self.assertEqual(
            tuple((self.root / "docs/pm/approvals").glob("*OWNER-LOSS*")),
            (),
        )
        self.assertIsNone(dispatch_evidence.read_retry_receipt(self.root, self.next_dispatch_id))

    def test_unknown_is_fail_closed_and_zero_write(self) -> None:
        before = StateProvider(self.root).snapshot()
        with patch.object(
            dispatch_evidence,
            "probe_dispatch_process_tree",
            return_value=dispatch_evidence.ProcessLiveness.UNKNOWN,
        ):
            result = self._runtime().recover_pending()[0]
        self.assertIs(result.outcome, DockyardOwnerLossRecoveryOutcome.FAIL_CLOSED)
        self.assertEqual(StateProvider(self.root).snapshot().events, before.events)
        self.assertIsNone(dispatch_evidence.read_retry_receipt(self.root, self.next_dispatch_id))

    def test_projection_exposes_exact_dead_and_unknown_recovery_state(self) -> None:
        service = object.__new__(DockyardPlanReadService)
        service._project_root = self.root
        service._active_registry = None
        service._retry_provider_ids = frozenset({"claude"})
        snapshot = StateProvider(self.root).snapshot()
        with (
            patch.object(
                dispatch_evidence,
                "probe_process",
                return_value=dispatch_evidence.ProcessLiveness.DEAD,
            ),
            patch.object(
                dispatch_evidence,
                "probe_dispatch_process_tree",
                return_value=dispatch_evidence.ProcessLiveness.DEAD,
            ),
        ):
            dead = service._run_evidence(snapshot)[0]
        self.assertEqual(
            (dead.phase, dead.supervisor_state, dead.worker_state),
            ("RECOVERY_REQUIRED", "DEAD", "DEAD"),
        )
        with patch.object(
            dispatch_evidence,
            "probe_dispatch_process_tree",
            return_value=dispatch_evidence.ProcessLiveness.UNKNOWN,
        ):
            unknown = service._run_evidence(snapshot)[0]
        self.assertEqual(
            (unknown.phase, unknown.supervisor_state, unknown.worker_state),
            ("RECOVERY_REQUIRED", "UNKNOWN", "UNKNOWN"),
        )

    def test_concurrent_recovery_is_one_worker_and_byte_exact_result(self) -> None:
        runtime = self._runtime()
        identity = (
            "TC-001",
            1,
            1,
            self.owner_request.expected_dispatch_id,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: runtime._recover_identity(*identity), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.provider.build_count, 1)
        snapshot = StateProvider(self.root).snapshot()
        self.assertEqual(
            sum(event.event_type == "TASK_DISPATCHED" for event in snapshot.events),
            2,
        )

    def test_attempt_three_recovers_without_reserving_attempt_four(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        helper = owner_fixtures.OwnerLossAutomaticRetryE2E(
            "test_attempt_three_recovers_without_attempt_four"
        )
        self.root, self.owner_request = helper._prepare_dead_active_dispatch(
            failed_attempt=3
        )
        self._install_dockyard_source_evidence()
        runtime = DockyardOwnerLossRecoveryRuntime(
            self.root,
            {"claude": self.provider},
            (("claude", "2.1.214"),),
            clock=self.clock,
        )
        result = runtime.recover_pending()[0]
        self.assertIs(
            result.outcome,
            DockyardOwnerLossRecoveryOutcome.ATTEMPT_LIMIT_REACHED,
        )
        self.assertIsNone(result.next_dispatch_id)
        self.assertEqual(self.provider.build_count, 0)
        snapshot = StateProvider(self.root).snapshot()
        self.assertEqual((snapshot.tasks[0].state, snapshot.tasks[0].attempt), ("ready", 3))
        self.assertFalse(any(event.attempt == 4 for event in snapshot.events))


if __name__ == "__main__":
    unittest.main()
