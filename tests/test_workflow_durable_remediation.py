"""Directed TC-13.29l.5g.2b durable fail/blocked owner tests."""

from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from control_plane_transition import ControlPlaneTransitionService, TransitionResult
from core_types import WorkerKind
from workflow_orchestrator import (
    DurableBlockedAuditRequest,
    DurableDeliveryRemediationRequest,
    DurableDeliveryReviewEvidence,
    WorkflowOrchestrator,
)
from tests import test_workflow_orchestrator as base


def _evidence(live_request) -> DurableDeliveryReviewEvidence:
    receipt = live_request.dispatch_cycle_result.delivery_receipt
    worker_kind = live_request.dispatch_cycle_result.worker_result.worker_kind
    return DurableDeliveryReviewEvidence(
        delivery_receipt=receipt,
        dispatch_generation_id="GEN-DSP-001",
        external_worker_receipt_digest="c" * 64,
        dispatch_event_id="EVT-DISP-001",
        acknowledge_event_id="EVT-ACK-001",
        delivery_event_id="EVT-DEL-001",
        delivery_event_digest="d" * 64,
        worktree_id="WT-001",
        workspace=Path.cwd().resolve(),
        implementation_commit=receipt.implementation_commit,
        report_commit=receipt.report_commit,
        worker_kind=worker_kind,
    )


class DurableRemediationBlockedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = base._setup_project()
        self.orchestrator = base._new_orch(self.tmp)
        head = base._git_head(self.tmp)
        live_fail = base._make_delivery_remediation_request(head_sha=head)
        live_blocked = base._make_blocked_audit_request(head_sha=head)
        self.fail_request = DurableDeliveryRemediationRequest(
            acceptance_cycle_result=live_fail.acceptance_cycle_result,
            delivery_evidence=_evidence(live_fail),
            return_transition_request=live_fail.return_transition_request,
            requeue_transition_request=live_fail.requeue_transition_request,
            worker_kind=live_fail.worker_kind,
            holder_instance_id=live_fail.holder_instance_id,
        )
        self.blocked_request = DurableBlockedAuditRequest(
            acceptance_cycle_result=live_blocked.acceptance_cycle_result,
            delivery_evidence=_evidence(live_blocked),
            block_transition_request=live_blocked.block_transition_request,
            current_worker_kind=live_blocked.current_worker_kind,
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_public_types_have_exact_fields(self) -> None:
        self.assertEqual([field.name for field in fields(DurableDeliveryRemediationRequest)], [
            "acceptance_cycle_result", "delivery_evidence",
            "return_transition_request", "requeue_transition_request",
            "worker_kind", "holder_instance_id",
        ])
        self.assertEqual([field.name for field in fields(DurableBlockedAuditRequest)], [
            "acceptance_cycle_result", "delivery_evidence",
            "block_transition_request", "current_worker_kind",
        ])

    def test_02_remediation_worker_kind_substitution_is_prelease_reject(self) -> None:
        with mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire:
            with self.assertRaises(ValueError):
                replace(self.fail_request, worker_kind=WorkerKind.EXPERT_AGENT)
        acquire.assert_not_called()

    def test_03_blocked_worker_kind_substitution_is_pretransition_reject(self) -> None:
        with mock.patch("workflow_orchestrator.evaluate_escalation") as escalation, \
             mock.patch.object(ControlPlaneTransitionService, "apply_transition") as apply:
            with self.assertRaises(ValueError):
                replace(
                    self.blocked_request,
                    current_worker_kind=WorkerKind.EXPERT_AGENT,
                )
        escalation.assert_not_called()
        apply.assert_not_called()

    def test_04_fail_route_orders_lease_return_release_requeue(self) -> None:
        order: list[str] = []

        def apply(service, request, lease, now):
            order.append(request.event_type)
            if request.event_type == "TASK_REQUEUED":
                self.assertIsNone(lease)
            return TransitionResult(
                task_id="TC-001", event_id=request.event_id,
                from_state="review_ready", to_state="returned",
                occurred_at="2026-08-03T00:00:00Z", outbox_message_id=None,
            )

        with mock.patch("workflow_orchestrator.acquire_worker_slot", side_effect=lambda *a, **k: order.append("LEASE") or mock.sentinel.lease), \
             mock.patch("workflow_orchestrator.release_worker_slot", side_effect=lambda *a, **k: order.append("RELEASE")), \
             mock.patch.object(ControlPlaneTransitionService, "apply_transition", apply):
            result = asyncio.run(
                self.orchestrator.run_durable_delivery_remediation(self.fail_request)
            )
        self.assertEqual(order, ["LEASE", "DELIVERY_RETURNED", "RELEASE", "TASK_REQUEUED"])
        self.assertEqual(result.audit_result.verdict, "fail")

    def test_05_blocked_route_escalates_then_writes_without_lease(self) -> None:
        order: list[str] = []
        real_escalation = __import__("workflow_orchestrator").evaluate_escalation

        def escalation(request):
            order.append("ESCALATE")
            return real_escalation(request)

        def apply(service, request, lease, now):
            order.append("TASK_BLOCKED")
            self.assertIsNone(lease)
            return TransitionResult(
                task_id="TC-001", event_id=request.event_id,
                from_state="review_ready", to_state="blocked",
                occurred_at="2026-08-03T00:00:00Z", outbox_message_id=None,
            )

        with mock.patch("workflow_orchestrator.evaluate_escalation", side_effect=escalation), \
             mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire, \
             mock.patch.object(ControlPlaneTransitionService, "apply_transition", apply):
            result = asyncio.run(
                self.orchestrator.run_durable_blocked_audit(self.blocked_request)
            )
        self.assertEqual(order, ["ESCALATE", "TASK_BLOCKED"])
        acquire.assert_not_called()
        self.assertEqual(result.audit_result.verdict, "blocked")

    def test_06_wrong_verdict_is_rejected_before_owner_calls(self) -> None:
        wrong = replace(
            self.fail_request.acceptance_cycle_result,
            audit_result=base._make_fake_audit_result("pass"),
        )
        with mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire:
            with self.assertRaises(ValueError):
                replace(self.fail_request, acceptance_cycle_result=wrong)
        acquire.assert_not_called()

    def test_07_lifecycle_event_collision_is_rejected(self) -> None:
        bad_return = replace(
            self.fail_request.return_transition_request,
            event_id=self.fail_request.delivery_evidence.delivery_event_id,
        )
        with self.assertRaises(ValueError):
            replace(self.fail_request, return_transition_request=bad_return)

    def test_08_source_has_no_dockyard_or_dispatch_result_fabrication(self) -> None:
        for method in (
            WorkflowOrchestrator.run_durable_delivery_remediation,
            WorkflowOrchestrator.run_durable_blocked_audit,
        ):
            source = inspect.getsource(method)
            self.assertNotIn("Dockyard", source)
            self.assertNotIn("DispatchCycleResult(", source)


if __name__ == "__main__":
    unittest.main()
