from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import http.client
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/agentdesk/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dockyard_post_admission_worker as post_admission
import dockyard_terminal_owner_composition as terminal
import workflow_orchestrator
from dockyard_local_runtime import DockyardLocalRuntime, DockyardLocalRuntimeInputError
from dockyard_post_delivery_composition import DockyardPostDeliveryCompositionOutcome
from dockyard_mad_result_store import DockyardMadResultStore
from dockyard_post_delivery_review_store import (
    DockyardPostDeliveryReviewOutcome,
    DockyardPostDeliveryReviewPhase,
    DockyardPostDeliveryReviewStore,
)
from pm_materialization_admission_handoff import MaterializationAdmissionRuntime
from tests import test_workflow_orchestrator as workflow_tests
from tests import test_dockyard_post_admission_worker as post_tests

_Clock = post_tests._Clock
_WorkspaceLocalProvider = post_tests._WorkspaceLocalProvider


class DockyardTerminalOwnerCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = post_tests.DockyardPostAdmissionWorkerTests(
            "test_exact_scheduler_worktree_and_lease_evidence_build_typed_worker_request"
        )
        self.fixture.setUp()
        self.fixture._prepare_admission_only()
        self.provider = _WorkspaceLocalProvider()
        config = self.fixture.fx.project_root / ".agentdesk/runtime/external-workers.yaml"
        config.write_text(
            """{
  "schema_version": "agentdesk.external-workers/v1",
  "updated_at": "2026-08-03T04:00:00Z",
  "workers": {
    "R1": {
      "endpoint_id": "EXT-R1",
      "role_id": "R1",
      "provider_id": "claude",
      "model_binding_id": "basic",
      "executable": "%s",
      "permission_mode": "deterministic",
      "status": "verified"
    }
  }
}
""" % sys.executable.replace("\\", "\\\\"),
            encoding="utf-8",
        )
        self.clock = _Clock()
        self.worker_run = post_admission.DockyardPostAdmissionWorkerRuntime(
            self.fixture.fx.project_root,
            {"claude": self.provider},
            (("claude", "2.1.214"),),
            clock=self.clock,
        ).execute(self.fixture.receipt)
        self.audit_config = workflow_tests._make_fake_mad_gateway_config()
        self.branch = terminal._git(
            self.fixture.fx.project_root, "branch", "--show-current"
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _runtime(self) -> terminal.DockyardTerminalOwnerCompositionRuntime:
        return terminal.DockyardTerminalOwnerCompositionRuntime(
            self.fixture.fx.project_root,
            self.audit_config,
            self.branch,
            clock=self.clock,
        )

    def test_reserves_exact_delivery_evidence_before_post_delivery_owner(self) -> None:
        audit = workflow_tests._make_fake_audit_result("pass")
        with patch.object(
            workflow_orchestrator.WorkflowOrchestrator,
            "run_durable_audit_cycle",
            AsyncMock(return_value=audit),
        ):
            result = asyncio.run(
                self._runtime().execute(self.fixture.receipt, self.worker_run)
            )
        store = DockyardPostDeliveryReviewStore(self.fixture.fx.project_root)
        durable = store.read_request(result.review_id)
        receipt = store.read_receipt(result.review_id)
        self.assertEqual(result.outcome, DockyardPostDeliveryCompositionOutcome.RECOVERY_REQUIRED)
        self.assertEqual(result.review_phase, DockyardPostDeliveryReviewPhase.MAD_COMPLETED)
        self.assertEqual(durable.dispatch_id, self.worker_run.receipt.dispatch_id)
        self.assertEqual(
            durable.external_worker_receipt_digest,
            self.worker_run.receipt.content_digest,
        )
        self.assertEqual(
            durable.delivery_receipt_digest,
            terminal.digest_owner_value(self.worker_run.dispatch_cycle_result.delivery_receipt),
        )
        self.assertEqual(receipt.phase, DockyardPostDeliveryReviewPhase.MAD_COMPLETED)
        self.assertEqual(DockyardMadResultStore(self.fixture.fx.project_root).read(result.review_id), audit)
        self.assertIsNotNone(durable.integration_event_id)
        self.assertIsNotNone(durable.integration_request_digest)
        self.assertIsNotNone(durable.return_event_id)
        self.assertIsNotNone(durable.requeue_event_id)

    def test_identity_substitution_rejected_before_review_reservation(self) -> None:
        bad = dataclasses.replace(
            self.worker_run,
            receipt=dataclasses.replace(
                self.worker_run.receipt,
                dispatch_id="DSP-SUBSTITUTED",
            ),
        )
        with self.assertRaises(terminal.DockyardTerminalCompositionConflictError):
            asyncio.run(self._runtime().execute(self.fixture.receipt, bad))
        review_root = self.fixture.fx.project_root / ".agentdesk/runtime/dockyard-post-delivery"
        self.assertFalse(review_root.exists())

    def test_post_admission_invokes_terminal_owner_after_worker_result(self) -> None:
        owner = self._runtime()
        owner.execute = AsyncMock(return_value=object())
        replay = post_admission.DockyardPostAdmissionWorkerRuntime(
            self.fixture.fx.project_root,
            {"claude": self.provider},
            (("claude", "2.1.214"),),
            clock=self.clock,
            terminal_owner=owner,
        ).execute(self.fixture.receipt)
        owner.execute.assert_awaited_once_with(self.fixture.receipt, replay)

    def test_real_pass_route_waits_for_explicit_accept_approval(self) -> None:
        target_branch = "dockyard-test-target"
        terminal._git(
            self.fixture.fx.project_root,
            "branch",
            target_branch,
            "HEAD",
        )

        async def audit(*args, **kwargs):
            return workflow_tests._make_fake_audit_result("pass")

        runtime = terminal.DockyardTerminalOwnerCompositionRuntime(
            self.fixture.fx.project_root,
            self.audit_config,
            target_branch,
            clock=self.clock,
        )
        with patch.object(workflow_orchestrator, "run_audit_gateway", audit):
            result = asyncio.run(runtime.execute(self.fixture.receipt, self.worker_run))
        self.assertEqual(result.outcome, DockyardPostDeliveryCompositionOutcome.RECOVERY_REQUIRED)
        self.assertEqual(result.review_phase, DockyardPostDeliveryReviewPhase.MAD_COMPLETED)
        snapshot = terminal.StateProvider(self.fixture.fx.project_root).snapshot()
        self.assertEqual(snapshot.tasks[0].state, "review_ready")
        self.assertNotIn("DELIVERY_ACCEPTED", [event.event_type for event in snapshot.events])
        self.assertNotIn("CHANGE_INTEGRATED", [event.event_type for event in snapshot.events])

        accepted = runtime.accept(
            self.worker_run.receipt.task_id,
            self.worker_run.receipt.implementation_commit,
            self.worker_run.receipt.report_commit,
            terminal._git(self.fixture.fx.project_root, "rev-parse", "HEAD"),
            "CMD-ACCEPT-001",
            "CONF-ACCEPT-001",
        )
        self.assertEqual(accepted.outcome, DockyardPostDeliveryCompositionOutcome.FINALIZED)
        accepted_snapshot = terminal.StateProvider(self.fixture.fx.project_root).snapshot()
        self.assertEqual(accepted_snapshot.tasks[0].state, "integrated")
        event_types = [event.event_type for event in accepted_snapshot.events]
        self.assertEqual(event_types.count("DELIVERY_ACCEPTED"), 1)
        self.assertEqual(event_types.count("CHANGE_INTEGRATED"), 1)
        integrated_event = next(
            event for event in accepted_snapshot.events
            if event.event_type == "CHANGE_INTEGRATED"
        )
        self.assertIn(("equivalence_method", "tree"), integrated_event.extra_fields)
        self.assertIn(("equivalence_result", "passed"), integrated_event.extra_fields)

    def test_real_fail_route_returns_and_requeues_only_after_user_confirmation(self) -> None:
        async def audit(*args, **kwargs):
            return workflow_tests._make_fake_audit_result("fail")

        runtime = self._runtime()
        with patch.object(workflow_orchestrator, "run_audit_gateway", audit):
            result = asyncio.run(runtime.execute(self.fixture.receipt, self.worker_run))
        self.assertEqual(result.review_phase, DockyardPostDeliveryReviewPhase.MAD_COMPLETED)
        before = terminal.StateProvider(self.fixture.fx.project_root).snapshot()
        self.assertEqual(before.tasks[0].state, "review_ready")
        self.assertNotIn("DELIVERY_RETURNED", [event.event_type for event in before.events])
        self.assertNotIn("TASK_REQUEUED", [event.event_type for event in before.events])

        returned = runtime.return_delivery(
            self.worker_run.receipt.task_id,
            self.worker_run.receipt.implementation_commit,
            self.worker_run.receipt.report_commit,
            terminal._git(self.fixture.fx.project_root, "rev-parse", "HEAD"),
            "CMD-RETURN-001",
            "CONF-RETURN-001",
        )
        self.assertEqual(returned.outcome, DockyardPostDeliveryCompositionOutcome.FINALIZED)
        after = terminal.StateProvider(self.fixture.fx.project_root).snapshot()
        self.assertEqual(after.tasks[0].state, "ready")
        self.assertIsNone(after.tasks[0].current_dispatch)
        event_types = [event.event_type for event in after.events]
        self.assertEqual(event_types.count("DELIVERY_RETURNED"), 1)
        self.assertEqual(event_types.count("TASK_REQUEUED"), 1)
        self.assertNotIn("DELIVERY_ACCEPTED", event_types)
        self.assertNotIn("CHANGE_INTEGRATED", event_types)
        receipt = DockyardPostDeliveryReviewStore(
            self.fixture.fx.project_root
        ).read_receipt(result.review_id)
        self.assertEqual(receipt.phase, DockyardPostDeliveryReviewPhase.FINALIZED)
        self.assertIsNotNone(receipt.return_event_id)
        self.assertIsNotNone(receipt.requeue_event_id)
        self.assertIsNone(receipt.integration_event_id)

    def test_pass_review_cannot_be_returned(self) -> None:
        audit = workflow_tests._make_fake_audit_result("pass")
        with patch.object(
            workflow_orchestrator.WorkflowOrchestrator,
            "run_durable_audit_cycle",
            AsyncMock(return_value=audit),
        ):
            asyncio.run(self._runtime().execute(self.fixture.receipt, self.worker_run))
        before = terminal.StateProvider(self.fixture.fx.project_root).snapshot()
        with self.assertRaises(terminal.DockyardTerminalCompositionConflictError):
            self._runtime().return_delivery(
                self.worker_run.receipt.task_id,
                self.worker_run.receipt.implementation_commit,
                self.worker_run.receipt.report_commit,
                terminal._git(self.fixture.fx.project_root, "rev-parse", "HEAD"),
                "CMD-RETURN-PASS",
                "CONF-RETURN-PASS",
            )
        self.assertEqual(terminal.StateProvider(self.fixture.fx.project_root).snapshot(), before)

    def test_local_runtime_requires_terminal_configuration_as_one_pair(self) -> None:
        with self.assertRaises(DockyardLocalRuntimeInputError):
            DockyardLocalRuntime(
                self.fixture.fx.config,
                {"claude": self.provider},
                (("claude", "2.1.214"),),
                audit_config=self.audit_config,
            )
        runtime = DockyardLocalRuntime(
            self.fixture.fx.config,
            {"claude": self.provider},
            (("claude", "2.1.214"),),
            audit_config=self.audit_config,
            integration_target_branch=self.branch,
            clock=self.clock,
        )
        self.assertIsNotNone(runtime)

    def test_loopback_review_projection_and_accept_command_close_the_task(self) -> None:
        fresh = post_tests.DockyardLocalRuntimeTests(
            "test_runtime_starts_real_loopback_and_approves_through_production_owner"
        )
        fresh.setUp()
        provider = _WorkspaceLocalProvider()
        config = fresh.project_root / ".agentdesk/runtime/external-workers.yaml"
        config.write_text(json.dumps({
            "schema_version": "agentdesk.external-workers/v1",
            "updated_at": "2026-08-03T04:00:00Z",
            "workers": {"R1": {
                "endpoint_id": "EXT-R1", "role_id": "R1",
                "provider_id": "claude", "model_binding_id": "basic",
                "executable": sys.executable, "permission_mode": "deterministic",
                "status": "verified",
            }},
        }, indent=2) + "\n", encoding="utf-8")
        target_branch = "dockyard-http-target"
        terminal._git(fresh.project_root, "branch", target_branch, "HEAD")
        local = DockyardLocalRuntime(
            fresh.config,
            {"claude": provider},
            (("claude", "2.1.214"),),
            audit_config=self.audit_config,
            integration_target_branch=target_branch,
            clock=_Clock(),
        )
        fresh.runtimes.append(local)

        async def audit(*args, **kwargs):
            return workflow_tests._make_fake_audit_result("pass")

        try:
            with patch.object(workflow_orchestrator, "run_audit_gateway", audit):
                address = local.start().address
                plan_payload = {
                    "plan_id": "PLAN-1",
                    "plan_digest": fresh.pending.plan_digest,
                    "task_count": 1,
                }
                plan_response = self._post(
                    fresh, address,
                    "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1/approve",
                    "plan.approve", "CMD-HTTP-PLAN", "IDEMP-HTTP-PLAN",
                    fresh.head, fresh.pending.revision, "CONF-HTTP-PLAN", plan_payload,
                )
            self.assertEqual((plan_response[0], plan_response[1]["outcome"]), (200, "materialized"))

            connection = http.client.HTTPConnection(address.host, address.port, timeout=20)
            connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/tasks")
            response = connection.getresponse()
            reviews = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, reviews)
            self.assertEqual(len(reviews["reviews"]), 1)
            review = reviews["reviews"][0]
            self.assertEqual((review["audit_verdict"], review["review_phase"]), ("pass", "MAD_COMPLETED"))
            connection = http.client.HTTPConnection(address.host, address.port, timeout=20)
            connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/runs")
            response = connection.getresponse()
            pass_runs = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, pass_runs)
            self.assertEqual(pass_runs["runs"][0]["phase"], "FINALIZED")
            self.assertFalse(pass_runs["runs"][0]["retry_available"])
            self.assertIsNone(pass_runs["runs"][0]["retry_failure_kind"])
            accept_payload = {
                "task_id": review["task_id"],
                "implementation_commit": review["implementation_commit"],
                "report_commit": review["report_commit"],
            }
            stale = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{review['task_id']}/accept",
                "delivery.accept", "CMD-HTTP-STALE", "IDEMP-HTTP-STALE",
                review["snapshot_commit"], review["revision"] + 1,
                "CONF-HTTP-STALE", accept_payload,
            )
            self.assertEqual(stale[0], 409, stale[1])
            self.assertEqual(stale[1]["error_code"], "REVISION_STALE")
            stale_snapshot = terminal.StateProvider(fresh.project_root).snapshot()
            self.assertEqual(stale_snapshot.tasks[0].state, "review_ready")
            self.assertNotIn(
                "DELIVERY_ACCEPTED",
                [event.event_type for event in stale_snapshot.events],
            )
            accepted = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{review['task_id']}/accept",
                "delivery.accept", "CMD-HTTP-ACCEPT", "IDEMP-HTTP-ACCEPT",
                review["snapshot_commit"], review["revision"],
                "CONF-HTTP-ACCEPT", accept_payload,
            )
            self.assertEqual(accepted[0], 200, accepted[1])
            self.assertEqual(accepted[1]["outcome"], "integrated")
            self.assertEqual(terminal.StateProvider(fresh.project_root).snapshot().tasks[0].state, "integrated")
        finally:
            fresh.tearDown()

    def test_loopback_return_redispatch_review_and_accept_close_task(self) -> None:
        fresh = post_tests.DockyardLocalRuntimeTests(
            "test_runtime_starts_real_loopback_and_approves_through_production_owner"
        )
        fresh.setUp()
        provider = _WorkspaceLocalProvider()
        config = fresh.project_root / ".agentdesk/runtime/external-workers.yaml"
        config.write_text(json.dumps({
            "schema_version": "agentdesk.external-workers/v1",
            "updated_at": "2026-08-03T04:00:00Z",
            "workers": {"R1": {
                "endpoint_id": "EXT-R1", "role_id": "R1",
                "provider_id": "claude", "model_binding_id": "basic",
                "executable": sys.executable, "permission_mode": "deterministic",
                "status": "verified",
            }},
        }, indent=2) + "\n", encoding="utf-8")
        target_branch = "dockyard-http-return-target"
        terminal._git(fresh.project_root, "branch", target_branch, "HEAD")
        local = DockyardLocalRuntime(
            fresh.config,
            {"claude": provider},
            (("claude", "2.1.214"),),
            audit_config=self.audit_config,
            integration_target_branch=target_branch,
            clock=_Clock(),
        )
        fresh.runtimes.append(local)

        audit_verdicts = iter(("fail", "pass"))

        async def audit(*args, **kwargs):
            return workflow_tests._make_fake_audit_result(next(audit_verdicts))

        audit_patch = patch.object(workflow_orchestrator, "run_audit_gateway", audit)
        try:
            audit_patch.start()
            address = local.start().address
            approved = self._post(
                fresh,
                address,
                "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1/approve",
                "plan.approve",
                "CMD-HTTP-RETURN-PLAN",
                "IDEMP-HTTP-RETURN-PLAN",
                fresh.head,
                fresh.pending.revision,
                "CONF-HTTP-RETURN-PLAN",
                {
                    "plan_id": "PLAN-1",
                    "plan_digest": fresh.pending.plan_digest,
                    "task_count": 1,
                },
            )
            self.assertEqual((approved[0], approved[1]["outcome"]), (200, "materialized"))
            connection = http.client.HTTPConnection(address.host, address.port, timeout=20)
            connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/tasks")
            response = connection.getresponse()
            reviews = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, reviews)
            review = reviews["reviews"][0]
            self.assertEqual(review["audit_verdict"], "fail")
            calls_before_return = provider.calls
            returned = self._post(
                fresh,
                address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{review['task_id']}/return",
                "delivery.return",
                "CMD-HTTP-RETURN",
                "IDEMP-HTTP-RETURN",
                review["snapshot_commit"],
                review["revision"],
                "CONF-HTTP-RETURN",
                {
                    "task_id": review["task_id"],
                    "implementation_commit": review["implementation_commit"],
                    "report_commit": review["report_commit"],
                },
            )
            self.assertEqual((returned[0], returned[1]["outcome"]), (200, "returned"))
            self.assertIsNotNone(returned[1]["canonical_event_id"])
            self.assertEqual(provider.calls, calls_before_return)
            snapshot = terminal.StateProvider(fresh.project_root).snapshot()
            self.assertEqual(snapshot.tasks[0].state, "ready")
            self.assertIsNone(snapshot.tasks[0].current_dispatch)
            event_types = [event.event_type for event in snapshot.events]
            self.assertEqual(event_types.count("DELIVERY_RETURNED"), 1)
            self.assertEqual(event_types.count("TASK_REQUEUED"), 1)

            connection = http.client.HTTPConnection(address.host, address.port, timeout=20)
            connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/runs")
            response = connection.getresponse()
            runs = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, runs)
            self.assertEqual(len(runs["runs"]), 1)
            returned_run = runs["runs"][0]
            self.assertEqual(returned_run["phase"], "RETURNED")
            self.assertTrue(returned_run["retry_available"])
            self.assertEqual(
                returned_run["retry_failure_kind"], "delivery_returned"
            )
            self.assertEqual(returned_run["retry_provider_id"], "claude")
            self.assertIsNotNone(returned_run["retry_event_id"])
            self.assertIsNotNone(returned_run["generation_id"])

            retry_payload = {
                "task_id": returned_run["task_id"],
                "failed_attempt": returned_run["attempt"],
                "dispatch_id": returned_run["dispatch_id"],
                "generation_id": returned_run["generation_id"],
                "event_id": returned_run["retry_event_id"],
                "provider_id": returned_run["retry_provider_id"],
                "model_id": returned_run["retry_model_id"],
                "upgrade": False,
            }
            before_reject = terminal.StateProvider(fresh.project_root).snapshot()
            original_card = (
                fresh.project_root / before_reject.tasks[0].task_card_path
            ).read_text(encoding="utf-8")
            approvals_root = fresh.project_root / "docs/pm/approvals"
            approval_count = len(tuple(approvals_root.glob("*.yaml")))
            stale_retry = self._post(
                fresh,
                address,
                f"/api/dockyard/v1/projects/PRJ-1/tasks/{review['task_id']}/retry",
                "task.retry",
                "CMD-HTTP-REDISPATCH-STALE",
                "IDEMP-HTTP-REDISPATCH-STALE",
                runs["snapshot_commit"],
                returned_run["revision"] + 1,
                "CONF-HTTP-REDISPATCH-STALE",
                retry_payload,
            )
            self.assertEqual(stale_retry[0], 409, stale_retry[1])
            self.assertEqual(stale_retry[1]["error_code"], "RETRY_CAS_CONFLICT")
            divergent_retry = self._post(
                fresh,
                address,
                f"/api/dockyard/v1/projects/PRJ-1/tasks/{review['task_id']}/retry",
                "task.retry",
                "CMD-HTTP-REDISPATCH-DIVERGENT",
                "IDEMP-HTTP-REDISPATCH-DIVERGENT",
                runs["snapshot_commit"],
                returned_run["revision"],
                "CONF-HTTP-REDISPATCH-DIVERGENT",
                {**retry_payload, "event_id": "EVT-REQUEUED-DIVERGENT"},
            )
            self.assertEqual(divergent_retry[0], 409, divergent_retry[1])
            self.assertEqual(
                divergent_retry[1]["error_code"], "RETRY_EVIDENCE_UNAVAILABLE"
            )
            self.assertEqual(
                terminal.StateProvider(fresh.project_root).snapshot(), before_reject
            )
            self.assertEqual(len(tuple(approvals_root.glob("*.yaml"))), approval_count)
            self.assertEqual(provider.calls, calls_before_return)

            redispatched = self._post(
                fresh,
                address,
                f"/api/dockyard/v1/projects/PRJ-1/tasks/{review['task_id']}/retry",
                "task.retry",
                "CMD-HTTP-REDISPATCH",
                "IDEMP-HTTP-REDISPATCH",
                runs["snapshot_commit"],
                returned_run["revision"],
                "CONF-HTTP-REDISPATCH",
                retry_payload,
            )
            self.assertEqual(redispatched[0], 200, redispatched[1])
            self.assertEqual(redispatched[1]["outcome"], "retry_started")
            self.assertEqual(provider.calls, calls_before_return + 1)
            redispatch_replay = self._post(
                fresh,
                address,
                f"/api/dockyard/v1/projects/PRJ-1/tasks/{review['task_id']}/retry",
                "task.retry",
                "CMD-HTTP-REDISPATCH",
                "IDEMP-HTTP-REDISPATCH",
                runs["snapshot_commit"],
                returned_run["revision"],
                "CONF-HTTP-REDISPATCH",
                retry_payload,
            )
            self.assertEqual(redispatch_replay[0], 200, redispatch_replay[1])
            self.assertTrue(redispatch_replay[1]["replayed"])
            self.assertEqual(provider.calls, calls_before_return + 1)
            self.assertEqual(provider.requests[-1].identity.attempt, 2)
            self.assertTrue(
                provider.requests[-1].prompt.startswith(original_card.rstrip())
            )
            self.assertIn("Dockyard MAD 返修反馈", provider.requests[-1].prompt)
            self.assertIn("audit report", provider.requests[-1].prompt)

            retried = terminal.StateProvider(fresh.project_root).snapshot()
            self.assertEqual(retried.tasks[0].state, "review_ready")
            self.assertEqual(retried.tasks[0].attempt, 2)
            self.assertIsNotNone(retried.tasks[0].current_dispatch)
            assert retried.tasks[0].current_dispatch is not None
            self.assertNotEqual(
                retried.tasks[0].current_dispatch.dispatch_id,
                returned_run["dispatch_id"],
            )
            task_events = tuple(
                event for event in retried.events
                if event.task_id == review["task_id"]
            )
            task_event_types = tuple(event.event_type for event in task_events)
            self.assertEqual(task_event_types.count("TASK_DISPATCHED"), 2)
            self.assertEqual(task_event_types.count("DISPATCH_ACKNOWLEDGED"), 2)
            self.assertEqual(task_event_types.count("DELIVERY_SUBMITTED"), 2)
            self.assertEqual(task_event_types.count("DELIVERY_RETURNED"), 1)
            self.assertEqual(task_event_types.count("TASK_REQUEUED"), 1)
            self.assertEqual(
                tuple(event.attempt for event in task_events if event.attempt == 3),
                (),
            )

            connection = http.client.HTTPConnection(address.host, address.port, timeout=20)
            connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/tasks")
            response = connection.getresponse()
            second_reviews = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, second_reviews)
            self.assertEqual(len(second_reviews["reviews"]), 1)
            second_review = second_reviews["reviews"][0]
            self.assertEqual(second_review["attempt"], 2)
            self.assertEqual(second_review["audit_verdict"], "pass")
            self.assertNotEqual(second_review["dispatch_id"], review["dispatch_id"])
            redispatch_path = (
                fresh.project_root
                / ".agentdesk/runtime/dockyard-redispatch"
                / f"{second_review['dispatch_id']}.json"
            )
            held_path = redispatch_path.with_suffix(".held")
            before_missing = terminal.StateProvider(fresh.project_root).snapshot()
            redispatch_path.rename(held_path)
            try:
                missing = self._post(
                    fresh,
                    address,
                    f"/api/dockyard/v1/projects/PRJ-1/deliveries/{review['task_id']}/accept",
                    "delivery.accept",
                    "CMD-HTTP-REDISPATCH-MISSING",
                    "IDEMP-HTTP-REDISPATCH-MISSING",
                    second_review["snapshot_commit"],
                    second_review["revision"],
                    "CONF-HTTP-REDISPATCH-MISSING",
                    {
                        "task_id": second_review["task_id"],
                        "implementation_commit": second_review["implementation_commit"],
                        "report_commit": second_review["report_commit"],
                    },
                )
                self.assertEqual(missing[0], 409, missing[1])
                self.assertEqual(missing[1]["error_code"], "DELIVERY_ACCEPT_CONFLICT")
                self.assertEqual(
                    terminal.StateProvider(fresh.project_root).snapshot(),
                    before_missing,
                )
            finally:
                held_path.rename(redispatch_path)
            accepted = self._post(
                fresh,
                address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{review['task_id']}/accept",
                "delivery.accept",
                "CMD-HTTP-REDISPATCH-ACCEPT",
                "IDEMP-HTTP-REDISPATCH-ACCEPT",
                second_review["snapshot_commit"],
                second_review["revision"],
                "CONF-HTTP-REDISPATCH-ACCEPT",
                {
                    "task_id": second_review["task_id"],
                    "implementation_commit": second_review["implementation_commit"],
                    "report_commit": second_review["report_commit"],
                },
            )
            self.assertEqual(accepted[0], 200, accepted[1])
            self.assertEqual(accepted[1]["outcome"], "integrated")
            self.assertEqual(
                terminal.StateProvider(fresh.project_root).snapshot().tasks[0].state,
                "integrated",
            )
        finally:
            audit_patch.stop()
            fresh.tearDown()

    def test_loopback_failed_redispatches_stop_at_attempt_three(self) -> None:
        fresh = post_tests.DockyardLocalRuntimeTests(
            "test_runtime_starts_real_loopback_and_approves_through_production_owner"
        )
        fresh.setUp()
        provider = _WorkspaceLocalProvider()
        config = fresh.project_root / ".agentdesk/runtime/external-workers.yaml"
        config.write_text(json.dumps({
            "schema_version": "agentdesk.external-workers/v1",
            "updated_at": "2026-08-03T04:00:00Z",
            "workers": {"R1": {
                "endpoint_id": "EXT-R1", "role_id": "R1",
                "provider_id": "claude", "model_binding_id": "basic",
                "executable": sys.executable, "permission_mode": "deterministic",
                "status": "verified",
            }},
        }, indent=2) + "\n", encoding="utf-8")
        target_branch = "dockyard-http-return-twice-target"
        terminal._git(fresh.project_root, "branch", target_branch, "HEAD")
        local = DockyardLocalRuntime(
            fresh.config,
            {"claude": provider},
            (("claude", "2.1.214"),),
            audit_config=self.audit_config,
            integration_target_branch=target_branch,
            clock=_Clock(),
        )
        fresh.runtimes.append(local)

        async def audit(*args, **kwargs):
            return workflow_tests._make_fake_audit_result("fail")

        audit_patch = patch.object(workflow_orchestrator, "run_audit_gateway", audit)
        try:
            audit_patch.start()
            address = local.start().address
            approved = self._post(
                fresh, address,
                "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1/approve",
                "plan.approve", "CMD-TWICE-PLAN", "IDEMP-TWICE-PLAN",
                fresh.head, fresh.pending.revision, "CONF-TWICE-PLAN",
                {
                    "plan_id": "PLAN-1",
                    "plan_digest": fresh.pending.plan_digest,
                    "task_count": 1,
                },
            )
            self.assertEqual(approved[0], 200, approved[1])

            def get(endpoint: str) -> dict[str, object]:
                connection = http.client.HTTPConnection(
                    address.host, address.port, timeout=20
                )
                connection.request(
                    "GET", f"/api/dockyard/v1/projects/PRJ-1/{endpoint}"
                )
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200, body)
                return body

            first_review = get("tasks")["reviews"][0]
            first_return = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{first_review['task_id']}/return",
                "delivery.return", "CMD-TWICE-RETURN-1", "IDEMP-TWICE-RETURN-1",
                first_review["snapshot_commit"], first_review["revision"],
                "CONF-TWICE-RETURN-1",
                {
                    "task_id": first_review["task_id"],
                    "implementation_commit": first_review["implementation_commit"],
                    "report_commit": first_review["report_commit"],
                },
            )
            self.assertEqual(first_return[0], 200, first_return[1])
            calls_before_redispatch = provider.calls
            returned_run = get("runs")["runs"][0]
            redispatch = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/tasks/{first_review['task_id']}/retry",
                "task.retry", "CMD-TWICE-REDISPATCH", "IDEMP-TWICE-REDISPATCH",
                first_review["snapshot_commit"], returned_run["revision"],
                "CONF-TWICE-REDISPATCH",
                {
                    "task_id": returned_run["task_id"],
                    "failed_attempt": returned_run["attempt"],
                    "dispatch_id": returned_run["dispatch_id"],
                    "generation_id": returned_run["generation_id"],
                    "event_id": returned_run["retry_event_id"],
                    "provider_id": returned_run["retry_provider_id"],
                    "model_id": returned_run["retry_model_id"],
                    "upgrade": False,
                },
            )
            self.assertEqual(redispatch[0], 200, redispatch[1])
            second_review = get("tasks")["reviews"][0]
            self.assertEqual(second_review["attempt"], 2)
            self.assertEqual(second_review["audit_verdict"], "fail")
            second_return = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{second_review['task_id']}/return",
                "delivery.return", "CMD-TWICE-RETURN-2", "IDEMP-TWICE-RETURN-2",
                second_review["snapshot_commit"], second_review["revision"],
                "CONF-TWICE-RETURN-2",
                {
                    "task_id": second_review["task_id"],
                    "implementation_commit": second_review["implementation_commit"],
                    "report_commit": second_review["report_commit"],
                },
            )
            self.assertEqual(second_return[0], 200, second_return[1])
            snapshot = terminal.StateProvider(fresh.project_root).snapshot()
            self.assertEqual(snapshot.tasks[0].state, "ready")
            self.assertEqual(snapshot.tasks[0].attempt, 2)
            self.assertIsNone(snapshot.tasks[0].current_dispatch)
            self.assertEqual(provider.calls, calls_before_redispatch + 1)
            self.assertFalse(any(event.attempt == 3 for event in snapshot.events))
            final_runs = get("runs")
            final_run = final_runs["runs"][0]
            self.assertEqual(final_run["phase"], "RETURNED")
            self.assertTrue(final_run["retry_available"])
            third_dispatch = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/tasks/{first_review['task_id']}/retry",
                "task.retry", "CMD-TWICE-REDISPATCH-3", "IDEMP-TWICE-REDISPATCH-3",
                final_runs["snapshot_commit"],
                final_run["revision"],
                "CONF-TWICE-REDISPATCH-3",
                {
                    "task_id": final_run["task_id"],
                    "failed_attempt": final_run["attempt"],
                    "dispatch_id": final_run["dispatch_id"],
                    "generation_id": final_run["generation_id"],
                    "event_id": final_run["retry_event_id"],
                    "provider_id": final_run["retry_provider_id"],
                    "model_id": final_run["retry_model_id"],
                    "upgrade": False,
                },
            )
            self.assertEqual(third_dispatch[0], 200, third_dispatch[1])
            third_review = get("tasks")["reviews"][0]
            self.assertEqual(third_review["attempt"], 3)
            third_return = self._post(
                fresh, address,
                f"/api/dockyard/v1/projects/PRJ-1/deliveries/{third_review['task_id']}/return",
                "delivery.return", "CMD-TWICE-RETURN-3", "IDEMP-TWICE-RETURN-3",
                third_review["snapshot_commit"], third_review["revision"],
                "CONF-TWICE-RETURN-3",
                {
                    "task_id": third_review["task_id"],
                    "implementation_commit": third_review["implementation_commit"],
                    "report_commit": third_review["report_commit"],
                },
            )
            self.assertEqual(third_return[0], 200, third_return[1])
            capped = terminal.StateProvider(fresh.project_root).snapshot()
            self.assertEqual(capped.tasks[0].state, "ready")
            self.assertEqual(capped.tasks[0].attempt, 3)
            capped_run = get("runs")["runs"][0]
            self.assertEqual(capped_run["phase"], "RETURNED")
            self.assertFalse(capped_run["retry_available"])
            self.assertIsNone(capped_run["retry_event_id"])
        finally:
            audit_patch.stop()
            fresh.tearDown()

    def _post(
        self,
        fixture,
        address,
        path: str,
        command_type: str,
        command_id: str,
        idempotency_key: str,
        snapshot: str,
        revision: int,
        confirmation_id: str,
        payload: dict[str, object],
    ):
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1",
            "command_id": command_id,
            "project_id": "PRJ-1",
            "command_type": command_type,
            "idempotency_key": idempotency_key,
            "expected_snapshot_commit": snapshot,
            "expected_revision": revision,
            "confirmation_id": confirmation_id,
            "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(address.host, address.port, timeout=30)
        connection.request("POST", path, body, {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": "http://127.0.0.1:5173",
            "Authorization": f"Bearer {fixture.device_token}",
            "X-Dockyard-Device-Id": "DEV-1",
            "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": idempotency_key,
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result


if __name__ == "__main__":
    unittest.main()
