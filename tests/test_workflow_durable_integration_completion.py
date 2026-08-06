"""Directed TC-13.29l.5h.3 durable integration-completion tests."""

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
    AcceptanceCycleResult,
    DurableDeliveryReviewEvidence,
    DurableGitIntegrationEvidence,
    DurableIntegrationCompletionRequest,
    WorkflowInputError,
    WorkflowOrchestrator,
)
from tests import test_workflow_orchestrator as base


class DurableIntegrationCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = base._setup_project(state="accepted")
        helper = base.WorkflowOrchestratorAcceptanceCycleTests()
        delivery = helper._make_dispatch_cycle_result().delivery_receipt
        self.delivery = DurableDeliveryReviewEvidence(
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
        identity = delivery.identity
        self.git_evidence = DurableGitIntegrationEvidence(
            operation_id="GIT-INT-001",
            request_content_digest="a" * 64,
            task_id=identity.task_id,
            revision=identity.revision,
            attempt=identity.attempt,
            dispatch_id=identity.dispatch_id,
            report_commit=delivery.report_commit,
            target_branch="main",
            target_head_before="b" * 40,
            phase="FINALIZED",
            method="MERGE_TREE",
            outcome="FINALIZED",
            integrated_commit="d" * 40,
            integrated_tree="e" * 40,
            receipt_content_digest="f" * 64,
        )
        accept_result = TransitionResult(
            task_id=identity.task_id,
            event_id="EVT-ACCEPT-001",
            from_state="review_ready",
            to_state="accepted",
            occurred_at="2026-08-03T00:00:00Z",
            outbox_message_id=None,
        )
        self.accepted = AcceptanceCycleResult(
            task_id=identity.task_id,
            audit_result=base._make_fake_audit_result("pass"),
            accept_transition=accept_result,
            integrate_transition=None,
        )
        integration = base._make_integration_transition_request(
            task_id=identity.task_id,
            revision=identity.revision,
            head_sha=base._git_head(self.tmp),
            integrated_commit=self.git_evidence.integrated_commit,
        )
        integration = replace(
            integration,
            event_context=replace(
                integration.event_context,
                evidence_refs=(self.git_evidence.receipt_content_digest,),
            ),
        )
        self.request = DurableIntegrationCompletionRequest(
            acceptance_cycle_result=self.accepted,
            delivery_evidence=self.delivery,
            integration_evidence=self.git_evidence,
            integration_transition_request=integration,
        )
        self.orchestrator = base._new_orch(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_public_types_have_exact_frozen_slotted_fields(self) -> None:
        self.assertEqual(
            [field.name for field in fields(DurableGitIntegrationEvidence)],
            [
                "operation_id", "request_content_digest", "task_id",
                "revision", "attempt", "dispatch_id", "report_commit",
                "target_branch", "target_head_before", "phase", "method",
                "outcome", "integrated_commit", "integrated_tree",
                "receipt_content_digest",
            ],
        )
        self.assertEqual(
            [field.name for field in fields(DurableIntegrationCompletionRequest)],
            [
                "acceptance_cycle_result", "delivery_evidence",
                "integration_evidence", "integration_transition_request",
            ],
        )
        for kind in (DurableGitIntegrationEvidence, DurableIntegrationCompletionRequest):
            self.assertTrue(kind.__dataclass_params__.frozen)
            self.assertTrue(hasattr(kind, "__slots__"))
            self.assertFalse(hasattr(self.git_evidence if kind is DurableGitIntegrationEvidence else self.request, "__dict__"))

    def test_02_happy_path_publishes_one_lease_free_transition(self) -> None:
        calls: list[object] = []

        def apply(service, transition, lease, now):
            calls.append((transition, lease))
            return TransitionResult(
                task_id="TC-001", event_id=transition.event_id,
                from_state="accepted", to_state="integrated",
                occurred_at="2026-08-03T00:00:01Z", outbox_message_id=None,
            )

        with mock.patch.object(ControlPlaneTransitionService, "apply_transition", apply), \
             mock.patch("workflow_orchestrator.run_audit_gateway") as audit, \
             mock.patch("workflow_orchestrator.acquire_worker_slot") as acquire:
            result = asyncio.run(
                self.orchestrator.run_durable_integration_completion(self.request)
            )
        self.assertEqual(calls, [(self.request.integration_transition_request, None)])
        self.assertIs(result.audit_result, self.accepted.audit_result)
        self.assertIs(result.accept_transition, self.accepted.accept_transition)
        self.assertEqual(result.integrate_transition.to_state, "integrated")
        audit.assert_not_called()
        acquire.assert_not_called()

    def test_03_every_git_identity_substitution_is_prewrite_reject(self) -> None:
        mutations = {
            "task_id": "TC-EVIL",
            "revision": 2,
            "attempt": 2,
            "dispatch_id": "DSP-EVIL",
            "report_commit": "1" * 40,
        }
        with mock.patch.object(ControlPlaneTransitionService, "apply_transition") as apply:
            for field_name, value in mutations.items():
                with self.subTest(field_name=field_name), self.assertRaises(ValueError):
                    replace(
                        self.request,
                        integration_evidence=replace(
                            self.git_evidence, **{field_name: value}
                        ),
                    )
        apply.assert_not_called()

    def test_04_integrated_commit_and_receipt_digest_are_prewrite_bound(self) -> None:
        bad_transition = replace(
            self.request.integration_transition_request,
            payload=replace(
                self.request.integration_transition_request.payload,
                integrated_commit="1" * 40,
            ),
        )
        with self.assertRaises(ValueError):
            replace(self.request, integration_transition_request=bad_transition)

        missing_digest = replace(
            self.request.integration_transition_request,
            event_context=replace(
                self.request.integration_transition_request.event_context,
                evidence_refs=(),
            ),
        )
        with self.assertRaises(ValueError):
            replace(self.request, integration_transition_request=missing_digest)

    def test_05_nonpass_missing_accept_and_reopen_are_rejected(self) -> None:
        variants = (
            replace(self.accepted, audit_result=base._make_fake_audit_result("fail")),
            replace(self.accepted, accept_transition=None),
            replace(
                self.accepted,
                integrate_transition=TransitionResult(
                    task_id="TC-001", event_id="EVT-OLD-INTEGRATE",
                    from_state="accepted", to_state="integrated",
                    occurred_at="2026-08-03T00:00:00Z", outbox_message_id=None,
                ),
            ),
        )
        for accepted in variants:
            with self.subTest(accepted=accepted), self.assertRaises(ValueError):
                replace(self.request, acceptance_cycle_result=accepted)

    def test_06_event_collision_is_rejected_before_transition(self) -> None:
        collision = replace(
            self.request.integration_transition_request,
            event_id=self.delivery.delivery_event_id,
        )
        with self.assertRaises(ValueError):
            replace(self.request, integration_transition_request=collision)

    def test_07_identical_replay_delegates_byte_exact_request(self) -> None:
        seen: list[object] = []
        canonical = TransitionResult(
            task_id="TC-001", event_id="EVT-INTEGRATE-001",
            from_state="accepted", to_state="integrated",
            occurred_at="2026-08-03T00:00:01Z", outbox_message_id=None,
        )

        def apply(service, transition, lease, now):
            seen.append(transition)
            return canonical

        with mock.patch.object(ControlPlaneTransitionService, "apply_transition", apply):
            first = asyncio.run(self.orchestrator.run_durable_integration_completion(self.request))
            second = asyncio.run(self.orchestrator.run_durable_integration_completion(self.request))
        self.assertEqual(seen, [self.request.integration_transition_request] * 2)
        self.assertIs(first.integrate_transition, canonical)
        self.assertIs(second.integrate_transition, canonical)

    def test_08_wrong_runtime_type_and_source_boundaries(self) -> None:
        with mock.patch.object(ControlPlaneTransitionService, "apply_transition") as apply:
            with self.assertRaises(WorkflowInputError):
                asyncio.run(
                    self.orchestrator.run_durable_integration_completion(self.git_evidence)
                )
        apply.assert_not_called()
        source = inspect.getsource(WorkflowOrchestrator.run_durable_integration_completion)
        self.assertNotIn("run_audit_gateway", source)
        self.assertNotIn("acquire_worker_slot", source)
        self.assertNotIn("git_integration_owner", source)
        self.assertNotIn("Dockyard", source)


if __name__ == "__main__":
    unittest.main()
