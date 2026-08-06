"""Directed TC-13.29l.5g.2c.2 Store-to-owner composition tests."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from control_plane_transition import TransitionResult
from dockyard_post_delivery_composition import (
    DockyardPostDeliveryCompositionOutcome,
    DockyardPostDeliveryCompositionRequest,
    DockyardPostDeliveryCompositionTimes,
    run_dockyard_post_delivery_composition,
)
from dockyard_post_delivery_owner_projection import digest_owner_value
import dockyard_post_delivery_review_store as review_store
from git_integration_owner import (
    GitIntegrationOutcome,
    GitIntegrationOwner,
    GitIntegrationPhase,
    GitIntegrationReceipt,
)
from workflow_orchestrator import (
    AcceptanceCycleResult,
    DeliveryRemediationResult,
    WorkflowOrchestrator,
)
from tests import test_workflow_orchestrator as base
from tests import test_dockyard_post_delivery_owner_projection as projection_tests


class DockyardPostDeliveryCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = projection_tests.DockyardPostDeliveryOwnerProjectionTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.orchestrator = base._new_orch(self.root)
        self.git_owner = GitIntegrationOwner(self.root)
        self.times = DockyardPostDeliveryCompositionTimes(
            "2026-08-03T10:00:01Z",
            "2026-08-03T10:00:02Z",
            "2026-08-03T10:00:03Z",
            "2026-08-03T10:00:04Z",
            "2026-08-03T10:00:05Z",
            "2026-08-03T10:00:06Z",
        )
        self.request = DockyardPostDeliveryCompositionRequest(
            self.fixture.projection,
            base._make_fake_mad_gateway_config(),
            self.times,
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _accepted(self, verdict: str) -> AcceptanceCycleResult:
        transition = None
        if verdict == "pass":
            transition = TransitionResult(
                "TC-001", "EVT-ACCEPT-001", "review_ready", "accepted",
                "2026-08-03T10:00:03Z", None,
            )
        return AcceptanceCycleResult(
            "TC-001", base._make_fake_audit_result(verdict), transition, None
        )

    def _git_receipt(self) -> GitIntegrationReceipt:
        request = self.fixture.git_request
        return GitIntegrationReceipt(
            "agentdesk.git-integration/v1",
            request.operation_id,
            request.content_digest,
            request.task_id,
            request.revision,
            request.attempt,
            request.dispatch_id,
            request.source_branch,
            request.report_commit,
            request.target_branch,
            request.expected_target_head,
            GitIntegrationPhase.FINALIZED,
            request.method,
            GitIntegrationOutcome.FINALIZED,
            "d" * 40,
            "e" * 40,
            None,
            request.requested_at,
            request.requested_at,
            request.requested_at,
            request.requested_at,
            "sha256:" + "f" * 64,
        )

    def _route_request(self, verdict: str):
        durable_store = review_store.DockyardPostDeliveryReviewStore(self.root)
        current = durable_store.read_request(self.fixture.review_id)
        if verdict == "fail":
            live = base._make_delivery_remediation_request(
                head_sha=base._git_head(self.root)
            )
            routes = (
                live.return_transition_request,
                live.requeue_transition_request,
                None,
            )
            ids = (routes[0].event_id, routes[1].event_id, None)
            git_request = None
        else:
            live = base._make_blocked_audit_request(
                head_sha=base._git_head(self.root)
            )
            routes = (None, None, live.block_transition_request)
            ids = (None, None, routes[2].event_id)
            git_request = None
        review_id = "REV-" + verdict.upper()
        candidate = review_store.with_request_digest(dataclasses.replace(
            current,
            operation_id="OP-" + verdict.upper(),
            integration_event_id=None,
            integration_request_digest=None,
            return_event_id=ids[0],
            requeue_event_id=ids[1],
            block_event_id=ids[2],
            routing_digest=digest_owner_value(routes),
            content_digest="sha256:" + "0" * 64,
        ))
        durable_store.reserve(review_id, candidate)
        projection = dataclasses.replace(
            self.fixture.projection,
            review_id=review_id,
            git_integration_request=git_request,
            return_transition_request=routes[0],
            requeue_transition_request=routes[1],
            block_transition_request=routes[2],
        )
        return DockyardPostDeliveryCompositionRequest(
            projection, self.request.audit_config, self.times
        ), live

    def test_01_pass_orders_accept_git_integrate_and_finalizes(self) -> None:
        accepted = self._accepted("pass")
        git_receipt = self._git_receipt()
        integrated = dataclasses.replace(
            accepted,
            integrate_transition=TransitionResult(
                "TC-001", "EVT-INTEGRATE-001", "accepted", "integrated",
                "2026-08-03T10:00:05Z", None,
            ),
        )
        order: list[str] = []

        async def acceptance(*args, **kwargs):
            order.append("acceptance")
            return accepted

        def integrate(*args, **kwargs):
            order.append("git")
            return git_receipt

        async def completion(*args, **kwargs):
            order.append("integrated")
            return integrated

        with mock.patch.object(WorkflowOrchestrator, "run_durable_acceptance_cycle", acceptance), \
             mock.patch.object(GitIntegrationOwner, "integrate", integrate), \
             mock.patch.object(WorkflowOrchestrator, "run_durable_integration_completion", completion), \
             mock.patch("dockyard_post_delivery_composition.StateProvider") as state_provider:
            state_provider.return_value.snapshot.return_value = SimpleNamespace(
                read_hexsha=base._git_head(self.root)
            )
            result = asyncio.run(run_dockyard_post_delivery_composition(
                self.request, self.orchestrator, self.git_owner
            ))
        self.assertEqual(order, ["acceptance", "git", "integrated"])
        self.assertIs(result.outcome, DockyardPostDeliveryCompositionOutcome.FINALIZED)
        self.assertIs(result.review_receipt.phase, review_store.DockyardPostDeliveryReviewPhase.FINALIZED)
        self.assertEqual(result.integration_result.integrate_transition.event_id, "EVT-INTEGRATE-001")

    def test_02_fail_uses_existing_durable_remediation_owner(self) -> None:
        request, live = self._route_request("fail")
        accepted = self._accepted("fail")
        remediation = DeliveryRemediationResult(
            "TC-001",
            accepted.audit_result,
            TransitionResult("TC-001", live.return_transition_request.event_id, "review_ready", "returned", self.times.review_applied_at, None),
            TransitionResult("TC-001", live.requeue_transition_request.event_id, "returned", "ready", self.times.review_applied_at, None),
        )
        with mock.patch.object(WorkflowOrchestrator, "run_durable_acceptance_cycle", mock.AsyncMock(return_value=accepted)), \
             mock.patch.object(WorkflowOrchestrator, "run_durable_delivery_remediation", mock.AsyncMock(return_value=remediation)) as owner, \
             mock.patch.object(GitIntegrationOwner, "integrate") as git:
            result = asyncio.run(run_dockyard_post_delivery_composition(
                request, self.orchestrator, self.git_owner
            ))
        owner.assert_awaited_once()
        git.assert_not_called()
        self.assertIs(result.outcome, DockyardPostDeliveryCompositionOutcome.FINALIZED)
        self.assertIsNotNone(result.remediation_result)

    def test_03_blocked_uses_existing_durable_blocked_owner(self) -> None:
        request, live = self._route_request("blocked")
        accepted = self._accepted("blocked")
        block_result = SimpleNamespace(
            block_transition=TransitionResult(
                "TC-001", live.block_transition_request.event_id,
                "review_ready", "blocked", self.times.review_applied_at, None,
            )
        )
        with mock.patch.object(WorkflowOrchestrator, "run_durable_acceptance_cycle", mock.AsyncMock(return_value=accepted)), \
             mock.patch.object(WorkflowOrchestrator, "run_durable_blocked_audit", mock.AsyncMock(return_value=block_result)) as owner, \
             mock.patch.object(GitIntegrationOwner, "integrate") as git:
            result = asyncio.run(run_dockyard_post_delivery_composition(
                request, self.orchestrator, self.git_owner
            ))
        owner.assert_awaited_once()
        git.assert_not_called()
        self.assertIs(result.outcome, DockyardPostDeliveryCompositionOutcome.FINALIZED)
        self.assertIs(result.blocked_result, block_result)

    def test_04_midflight_phase_returns_recovery_required_without_blind_replay(self) -> None:
        durable_store = review_store.DockyardPostDeliveryReviewStore(self.root)
        durable_store.advance(
            self.fixture.review_id,
            review_store.DockyardPostDeliveryReviewPhase.REVIEW_RESERVED,
            review_store.DockyardPostDeliveryReviewOutcome.RESERVED,
            self.times.review_reserved_at,
        )
        with mock.patch.object(WorkflowOrchestrator, "run_durable_acceptance_cycle", mock.AsyncMock()) as acceptance, \
             mock.patch.object(GitIntegrationOwner, "integrate") as git:
            result = asyncio.run(run_dockyard_post_delivery_composition(
                self.request, self.orchestrator, self.git_owner
            ))
        self.assertIs(result.outcome, DockyardPostDeliveryCompositionOutcome.RECOVERY_REQUIRED)
        acceptance.assert_not_awaited()
        git.assert_not_called()

    def test_05_source_keeps_store_locks_outside_owner_calls(self) -> None:
        source = inspect.getsource(run_dockyard_post_delivery_composition)
        self.assertNotIn("_store_lock", source)
        self.assertNotIn("run_audit_gateway", source)
        self.assertNotIn("apply_transition", source)
        self.assertNotIn("subprocess", source)


if __name__ == "__main__":
    unittest.main()
