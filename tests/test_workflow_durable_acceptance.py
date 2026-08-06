"""Directed tests for TC-13.29l.5g.2a durable Interface #22 runtime."""

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
    DurableAcceptanceCycleRequest,
    DurableDeliveryReviewEvidence,
    WorkflowInputError,
    WorkflowOrchestrator,
)

from tests import test_workflow_orchestrator as base


class DurableAcceptanceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = base._setup_project()
        base._init_tasks_yaml(
            self.tmp,
            task_id="TC-001",
            state="review_ready",
            revision=1,
            dispatch_id="DSP-001",
        )
        helper = base.WorkflowOrchestratorAcceptanceCycleTests()
        delivery = helper._make_dispatch_cycle_result().delivery_receipt
        self.evidence = DurableDeliveryReviewEvidence(
            delivery_receipt=delivery,
            dispatch_generation_id="GEN-DSP-001",
            external_worker_receipt_digest="c" * 64,
            dispatch_event_id="EVT-DISP-001",
            acknowledge_event_id="EVT-ACK-001",
            delivery_event_id="EVT-DEL-001",
            delivery_event_digest="d" * 64,
            worktree_id="WT-001",
            workspace=self.tmp,
            implementation_commit=delivery.implementation_commit,
            report_commit=delivery.report_commit,
            worker_kind=WorkerKind.ADVANCED_AGENT,
        )
        head = base._git_head(self.tmp)
        self.request = DurableAcceptanceCycleRequest(
            delivery_evidence=self.evidence,
            audit_input=base._make_mad_audit_gateway_input(workspace=self.tmp),
            acceptance_transition_request=base._make_acceptance_transition_request(
                head_sha=head
            ),
            integration_transition_request=None,
            worker_kind=WorkerKind.ADVANCED_AGENT,
            holder_instance_id="durable-review-owner",
        )
        self.config = base._make_fake_mad_gateway_config()
        self.orchestrator = base._new_orch(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_public_types_have_exact_frozen_slotted_fields(self) -> None:
        self.assertEqual(
            [field.name for field in fields(DurableDeliveryReviewEvidence)],
            [
                "delivery_receipt",
                "dispatch_generation_id",
                "external_worker_receipt_digest",
                "dispatch_event_id",
                "acknowledge_event_id",
                "delivery_event_id",
                "delivery_event_digest",
                "worktree_id",
                "workspace",
                "implementation_commit",
                "report_commit",
                "worker_kind",
            ],
        )
        self.assertEqual(
            [field.name for field in fields(DurableAcceptanceCycleRequest)],
            [
                "delivery_evidence",
                "audit_input",
                "acceptance_transition_request",
                "integration_transition_request",
                "worker_kind",
                "holder_instance_id",
            ],
        )
        for kind in (DurableDeliveryReviewEvidence, DurableAcceptanceCycleRequest):
            self.assertTrue(kind.__dataclass_params__.frozen)
            self.assertTrue(hasattr(kind, "__slots__"))

    def test_02_identity_substitution_rejected_before_mad_or_lease(self) -> None:
        bad_audit = replace(self.request.audit_input, dispatch_id="DSP-EVIL")
        with self.assertRaises(ValueError), \
             mock.patch("workflow_orchestrator.run_audit_gateway") as audit, \
             mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire:
            DurableAcceptanceCycleRequest(
                delivery_evidence=self.evidence,
                audit_input=bad_audit,
                acceptance_transition_request=self.request.acceptance_transition_request,
                integration_transition_request=None,
                worker_kind=self.request.worker_kind,
                holder_instance_id=self.request.holder_instance_id,
            )
        audit.assert_not_called()
        acquire.assert_not_called()

    def test_03_event_collision_rejected_before_owner_execution(self) -> None:
        bad_acceptance = replace(
            self.request.acceptance_transition_request,
            event_id=self.evidence.delivery_event_id,
        )
        with self.assertRaises(ValueError):
            DurableAcceptanceCycleRequest(
                delivery_evidence=self.evidence,
                audit_input=self.request.audit_input,
                acceptance_transition_request=bad_acceptance,
                integration_transition_request=None,
                worker_kind=self.request.worker_kind,
                holder_instance_id=self.request.holder_instance_id,
            )

    def test_04_pass_runs_audit_then_lease_then_accept(self) -> None:
        order: list[str] = []

        async def audit(*args, **kwargs):
            order.append("audit")
            return base._make_fake_audit_result("pass")

        def acquire(*args, **kwargs):
            order.append("lease")
            return mock.sentinel.review_lease

        def apply(service, request, lease, now):
            order.append("accept")
            self.assertIs(lease, mock.sentinel.review_lease)
            return TransitionResult(
                task_id="TC-001",
                event_id=request.event_id,
                from_state="review_ready",
                to_state="accepted",
                occurred_at="2026-08-03T00:00:00Z",
                outbox_message_id=None,
            )

        with mock.patch("workflow_orchestrator.run_audit_gateway", side_effect=audit), \
             mock.patch("workflow_orchestrator.acquire_worker_slot", side_effect=acquire), \
             mock.patch("workflow_orchestrator.release_worker_slot") as release, \
             mock.patch.object(ControlPlaneTransitionService, "apply_transition", apply):
            result = asyncio.run(
                self.orchestrator.run_durable_acceptance_cycle(self.request, self.config)
            )
        self.assertEqual(order, ["audit", "lease", "accept"])
        self.assertIsNotNone(result.accept_transition)
        release.assert_called_once()

    def test_05_fail_returns_zero_lease_and_zero_transition(self) -> None:
        async def audit(*args, **kwargs):
            return base._make_fake_audit_result("fail")

        with mock.patch("workflow_orchestrator.run_audit_gateway", side_effect=audit), \
             mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire, \
             mock.patch.object(ControlPlaneTransitionService, "apply_transition") as apply:
            result = asyncio.run(
                self.orchestrator.run_durable_acceptance_cycle(self.request, self.config)
            )
        self.assertIsNone(result.accept_transition)
        self.assertIsNone(result.integrate_transition)
        acquire.assert_not_called()
        apply.assert_not_called()

    def test_06_optional_integration_runs_after_review_lease_release(self) -> None:
        integration = base._make_integration_transition_request(
            head_sha=base._git_head(self.tmp)
        )
        request = replace(self.request, integration_transition_request=integration)
        order: list[str] = []

        async def audit(*args, **kwargs):
            return base._make_fake_audit_result("pass")

        def apply(service, transition, lease, now):
            order.append(transition.event_type)
            if transition.event_type == "CHANGE_INTEGRATED":
                self.assertIsNone(lease)
            return TransitionResult(
                task_id="TC-001",
                event_id=transition.event_id,
                from_state="review_ready" if lease is not None else "accepted",
                to_state="accepted" if lease is not None else "integrated",
                occurred_at="2026-08-03T00:00:00Z",
                outbox_message_id=None,
            )

        def release(*args, **kwargs):
            order.append("RELEASE")

        with mock.patch("workflow_orchestrator.run_audit_gateway", side_effect=audit), \
             mock.patch("workflow_orchestrator.acquire_worker_slot", return_value=mock.sentinel.review_lease), \
             mock.patch("workflow_orchestrator.release_worker_slot", side_effect=release), \
             mock.patch.object(ControlPlaneTransitionService, "apply_transition", apply):
            result = asyncio.run(
                self.orchestrator.run_durable_acceptance_cycle(request, self.config)
            )
        self.assertEqual(order, ["DELIVERY_ACCEPTED", "RELEASE", "CHANGE_INTEGRATED"])
        self.assertIsNotNone(result.integrate_transition)

    def test_07_wrong_runtime_request_rejected_before_audit(self) -> None:
        with mock.patch("workflow_orchestrator.run_audit_gateway") as audit:
            with self.assertRaises(WorkflowInputError):
                asyncio.run(
                    self.orchestrator.run_durable_acceptance_cycle(
                        self.request.audit_input, self.config
                    )
                )
        audit.assert_not_called()

    def test_08_source_has_no_dockyard_reverse_dependency_or_fabrication(self) -> None:
        source = inspect.getsource(WorkflowOrchestrator.run_durable_acceptance_cycle)
        self.assertNotIn("Dockyard", source)
        self.assertNotIn("DispatchCycleResult(", source)
        module_source = (SCRIPTS / "workflow_orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn("import dockyard", module_source.lower())

    def test_09_worker_kind_substitution_rejected_before_mad_or_lease(self) -> None:
        with self.assertRaises(ValueError), \
             mock.patch("workflow_orchestrator.run_audit_gateway") as audit, \
             mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire:
            DurableAcceptanceCycleRequest(
                delivery_evidence=self.evidence,
                audit_input=self.request.audit_input,
                acceptance_transition_request=self.request.acceptance_transition_request,
                integration_transition_request=None,
                worker_kind=WorkerKind.EXPERT_AGENT,
                holder_instance_id=self.request.holder_instance_id,
            )
        audit.assert_not_called()
        acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
