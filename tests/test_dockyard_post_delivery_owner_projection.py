"""Directed TC-13.29l.5g.2c.1 owner-plan projection tests."""

from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dockyard_post_delivery_review_store as store
from core_types import WorkerKind
from dockyard_post_delivery_owner_projection import (
    DockyardOwnerPlanProjectionRequest,
    DockyardOwnerProjectionConflictError,
    DockyardGitIntegrationIntent,
    DockyardPostDeliveryOwnerPlan,
    digest_owner_value,
    digest_worktree_identity,
    project_post_delivery_owner_plan,
)
from git_integration_owner import (
    GitIntegrationMethod,
    GitIntegrationRequest,
    SCHEMA_VERSION as GIT_SCHEMA,
    with_request_digest as with_git_digest,
)
from tests import test_workflow_orchestrator as base


class DockyardPostDeliveryOwnerProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = base._setup_project(state="review_ready")
        delivery = base.WorkflowOrchestratorAcceptanceCycleTests()._make_dispatch_cycle_result().delivery_receipt
        identity = delivery.identity
        self.audit_input = base._make_mad_audit_gateway_input(
            task_id=identity.task_id,
            dispatch_id=identity.dispatch_id,
            workspace=self.root,
            implementation_commit=delivery.implementation_commit,
            report_commit=delivery.report_commit,
        )
        head = base._git_head(self.root)
        self.acceptance = base._make_acceptance_transition_request(
            task_id=identity.task_id,
            dispatch_id=identity.dispatch_id,
            revision=identity.revision,
            attempt=identity.attempt,
            head_sha=head,
            accepted_commit=delivery.implementation_commit,
        )
        self.git_request = with_git_digest(GitIntegrationRequest(
            GIT_SCHEMA,
            "GIT-REV-001",
            self.root,
            identity.task_id,
            identity.revision,
            identity.attempt,
            identity.dispatch_id,
            self.root,
            "feature/TC-001",
            self.audit_input.base_commit,
            delivery.implementation_commit,
            delivery.report_commit,
            "main",
            head,
            GitIntegrationMethod.MERGE_TREE,
            "Integrate TC-001",
            "2026-08-03T10:00:00Z",
            "sha256:" + "0" * 64,
        ))
        self.review_id = "REV-001"
        review = store.with_request_digest(store.DockyardPostDeliveryReviewRequest(
            store.SCHEMA_VERSION,
            "OP-REV-001",
            "PROJECT-001",
            identity.task_id,
            identity.revision,
            identity.attempt,
            identity.dispatch_id,
            "GEN-DSP-001",
            "QUEUE-001",
            "SCHEDULE-001",
            "sha256:" + "1" * 64,
            "HANDOFF-001",
            "sha256:" + "2" * 64,
            "EVT-DISP-001",
            "sha256:" + "3" * 64,
            "EVT-ACK-001",
            "sha256:" + "4" * 64,
            "EVT-DEL-001",
            "sha256:" + "5" * 64,
            digest_owner_value(delivery),
            delivery.implementation_commit,
            delivery.report_commit,
            "WT-001",
            digest_worktree_identity(
                "WT-001", self.root, "feature/TC-001", self.audit_input.base_commit
            ),
            "feature/TC-001",
            self.audit_input.base_commit,
            digest_owner_value(self.audit_input),
            "MAD-CONFIG-001",
            self.acceptance.event_id,
            digest_owner_value(self.acceptance),
            "EVT-INTEGRATE-001",
            self.git_request.content_digest,
            None,
            None,
            None,
            digest_owner_value((None, None, None)),
            "2026-08-03T10:00:00Z",
            "sha256:" + "0" * 64,
        ))
        store.DockyardPostDeliveryReviewStore(self.root).reserve(self.review_id, review)
        self.projection = DockyardOwnerPlanProjectionRequest(
            self.root,
            self.review_id,
            delivery,
            self.root,
            WorkerKind.ADVANCED_AGENT,
            self.audit_input,
            self.acceptance,
            self.git_request,
            None,
            None,
            None,
            "dockyard-review-owner",
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_01_plan_has_exact_repaired_twelve_fields(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(DockyardPostDeliveryOwnerPlan)],
            [
                "review_id", "delivery_evidence", "audit_input",
                "acceptance_transition_request", "git_integration_intent",
                "return_transition_request", "requeue_transition_request",
                "block_transition_request", "worker_kind", "holder_instance_id",
                "audit_config_id", "content_digest",
            ],
        )
        self.assertTrue(DockyardPostDeliveryOwnerPlan.__dataclass_params__.frozen)
        self.assertTrue(hasattr(DockyardPostDeliveryOwnerPlan, "__slots__"))
        self.assertEqual(
            [field.name for field in dataclasses.fields(DockyardGitIntegrationIntent)],
            ["event_id", "request"],
        )

    def test_02_projects_exact_owner_inputs_without_integration_transition(self) -> None:
        plan = project_post_delivery_owner_plan(self.projection)
        self.assertEqual(plan.review_id, self.review_id)
        self.assertEqual(plan.git_integration_intent.event_id, "EVT-INTEGRATE-001")
        self.assertIs(plan.git_integration_intent.request, self.git_request)
        self.assertIs(plan.audit_input, self.audit_input)
        self.assertIs(plan.acceptance_transition_request, self.acceptance)
        self.assertEqual(plan.delivery_evidence.delivery_receipt, self.projection.delivery_receipt)
        self.assertEqual(plan.audit_config_id, "MAD-CONFIG-001")
        self.assertRegex(plan.content_digest, r"^sha256:[0-9a-f]{64}$")

    def test_03_identity_substitution_fails_closed(self) -> None:
        evil = dataclasses.replace(
            self.projection,
            delivery_receipt=dataclasses.replace(
                self.projection.delivery_receipt,
                report_commit="1" * 40,
            ),
        )
        with self.assertRaises(DockyardOwnerProjectionConflictError):
            project_post_delivery_owner_plan(evil)

    def test_04_audit_and_acceptance_digest_substitution_fails_closed(self) -> None:
        with self.assertRaises(DockyardOwnerProjectionConflictError):
            project_post_delivery_owner_plan(dataclasses.replace(
                self.projection,
                audit_input=dataclasses.replace(self.audit_input, question="changed"),
            ))
        with self.assertRaises(DockyardOwnerProjectionConflictError):
            project_post_delivery_owner_plan(dataclasses.replace(
                self.projection,
                acceptance_transition_request=dataclasses.replace(
                    self.acceptance, event_id="EVT-ACCEPT-EVIL"
                ),
            ))

    def test_05_git_request_substitution_fails_closed(self) -> None:
        evil = dataclasses.replace(
            self.git_request,
            expected_target_head="1" * 40,
        )
        with self.assertRaises(DockyardOwnerProjectionConflictError):
            project_post_delivery_owner_plan(dataclasses.replace(
                self.projection, git_integration_request=evil
            ))

    def test_06_identical_projection_is_byte_exact(self) -> None:
        first = project_post_delivery_owner_plan(self.projection)
        second = project_post_delivery_owner_plan(self.projection)
        self.assertEqual(first, second)
        self.assertEqual(first.content_digest, second.content_digest)

    def test_07_finalized_review_cannot_be_reopened(self) -> None:
        review_store = store.DockyardPostDeliveryReviewStore(self.root)
        review_store.advance(
            self.review_id,
            store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED,
            store.DockyardPostDeliveryReviewOutcome.RESERVED,
            "2026-08-03T10:00:01Z",
        )
        review_store.advance(
            self.review_id,
            store.DockyardPostDeliveryReviewPhase.MAD_STARTED,
            store.DockyardPostDeliveryReviewOutcome.RESERVED,
            "2026-08-03T10:00:02Z",
        )
        review_store.advance(
            self.review_id,
            store.DockyardPostDeliveryReviewPhase.MAD_COMPLETED,
            store.DockyardPostDeliveryReviewOutcome.AUDIT_PASS,
            "2026-08-03T10:00:03Z",
            audit_result_id="AUDIT-001",
            audit_result_digest="sha256:" + "6" * 64,
            audit_verdict="pass",
        )
        review_store.advance(
            self.review_id,
            store.DockyardPostDeliveryReviewPhase.REVIEW_APPLIED,
            store.DockyardPostDeliveryReviewOutcome.ACCEPTED,
            "2026-08-03T10:00:04Z",
            applied_event_id=self.acceptance.event_id,
        )
        review_store.advance(
            self.review_id,
            store.DockyardPostDeliveryReviewPhase.INTEGRATION_APPLIED,
            store.DockyardPostDeliveryReviewOutcome.INTEGRATED,
            "2026-08-03T10:00:05Z",
            applied_event_id="EVT-INTEGRATE-001",
        )
        review_store.advance(
            self.review_id,
            store.DockyardPostDeliveryReviewPhase.FINALIZED,
            store.DockyardPostDeliveryReviewOutcome.FINALIZED,
            "2026-08-03T10:00:06Z",
        )
        with self.assertRaises(DockyardOwnerProjectionConflictError):
            project_post_delivery_owner_plan(self.projection)

    def test_08_projection_is_side_effect_free(self) -> None:
        with mock.patch("workflow_orchestrator.run_audit_gateway") as mad, \
             mock.patch("workflow_orchestrator.acquire_worker_slot") as lease, \
             mock.patch("subprocess.run") as process:
            project_post_delivery_owner_plan(self.projection)
        mad.assert_not_called()
        lease.assert_not_called()
        process.assert_not_called()
        source = inspect.getsource(project_post_delivery_owner_plan)
        for forbidden in ("apply_transition", "run_audit_gateway", "subprocess", "run_worker"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
