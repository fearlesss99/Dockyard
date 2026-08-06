"""Real local Dockyard closed-loop E2E coverage for TC-13.29l.10."""

from __future__ import annotations

import hashlib
import http.client
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dockyard_terminal_owner_composition as terminal
import dockyard_composition
import workflow_orchestrator
from dockyard_local_runtime import DockyardLocalRuntime
from state_provider import StateProvider
from tests import test_dockyard_local_runtime as local_fixtures
from tests import test_dockyard_post_admission_worker as worker_fixtures
from tests import test_workflow_orchestrator as workflow_fixtures
from tests import test_dockyard_owner_loss_recovery as recovery_fixtures


class DockyardLocalClosedLoopE2ETests(unittest.TestCase):
    """Exercise production owners through the real loopback HTTP boundary."""

    def setUp(self) -> None:
        self.fixture = local_fixtures.DockyardLocalRuntimeTests(
            "test_runtime_starts_real_loopback_and_approves_through_production_owner"
        )
        self.fixture.setUp()
        self.plan_root = Path(self.fixture.temp.name) / "e2e-plans"
        self.plan_root.mkdir()
        self.config = replace(
            self.fixture.config,
            runtime_id="RUNTIME-E2E",
            plan_id="PLAN-E2E",
            plan_store_root=self.plan_root,
        )
        self.target_branch = "dockyard-e2e-target"
        terminal._git(
            self.fixture.project_root,
            "branch",
            self.target_branch,
            "HEAD",
        )
        worker_config = (
            self.fixture.project_root
            / ".agentdesk/runtime/external-workers.yaml"
        )
        worker_config.parent.mkdir(parents=True, exist_ok=True)
        worker_config.write_text(
            json.dumps({
                "schema_version": "agentdesk.external-workers/v1",
                "updated_at": "2026-08-03T04:00:00Z",
                "workers": {"R1": {
                    "endpoint_id": "EXT-R1",
                    "role_id": "R1",
                    "provider_id": "claude",
                    "model_binding_id": "basic",
                    "executable": sys.executable,
                    "permission_mode": "deterministic",
                    "status": "verified",
                }},
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        self.runtime: DockyardLocalRuntime | None = None
        self.result = None

    def tearDown(self) -> None:
        if self.runtime is not None:
            self.runtime.stop()
        self.fixture.tearDown()

    def _start(self, *, workers: bool) -> None:
        if workers:
            self.runtime = DockyardLocalRuntime(
                self.config,
                {"claude": worker_fixtures._WorkspaceLocalProvider()},
                (("claude", "2.1.214"),),
                audit_config=workflow_fixtures._make_fake_mad_gateway_config(),
                integration_target_branch=self.target_branch,
                clock=worker_fixtures._Clock(),
            )
        else:
            self.runtime = DockyardLocalRuntime(self.config)
        self.result = self.runtime.start()

    def _get(self, path: str) -> tuple[int, dict[str, object]]:
        assert self.result is not None
        connection = http.client.HTTPConnection(
            self.result.address.host,
            self.result.address.port,
            timeout=30,
        )
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def _post(
        self,
        path: str,
        command_type: str,
        command_id: str,
        expected_snapshot: str,
        expected_revision: int,
        payload: dict[str, object],
        *,
        confirmation_id: str | None = None,
        method: str = "POST",
    ) -> tuple[int, dict[str, object]]:
        assert self.result is not None
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        idempotency_key = "IDEMP-" + command_id
        envelope = {
            "schema_version": "dockyard.command/v1",
            "command_id": command_id,
            "project_id": "PRJ-1",
            "command_type": command_type,
            "idempotency_key": idempotency_key,
            "expected_snapshot_commit": expected_snapshot,
            "expected_revision": expected_revision,
            "confirmation_id": confirmation_id,
            "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps(
            {"envelope": envelope, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            self.result.address.host,
            self.result.address.port,
            timeout=60,
        )
        connection.request(method, path, body, {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": self.config.browser_origin,
            "Authorization": f"Bearer {self.fixture.device_token}",
            "X-Dockyard-Device-Id": "DEV-1",
            "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": idempotency_key,
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def _create_and_submit(self) -> dict[str, object]:
        project_status, project = self._get(
            "/api/dockyard/v1/projects/PRJ-1"
        )
        self.assertEqual(project_status, 200, project)
        requirement = (
            "实现 Dockyard 本机闭环，保留显式审批、MAD 审议与 Git 集成门禁。"
        )
        created_status, created = self._post(
            "/api/dockyard/v1/projects/PRJ-1/plans",
            "plan.create",
            "CMD-E2E-CREATE",
            str(project["snapshot_commit"]),
            int(project["project_generation"]),
            {"plan_id": "PLAN-E2E", "requirement": requirement},
        )
        self.assertEqual(created_status, 200, created)
        self.assertEqual(created["outcome"], "drafted")
        plan_status, plan = self._get(
            "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-E2E"
        )
        self.assertEqual(plan_status, 200, plan)
        task = dict(plan["tasks"][0])
        task["provider_id"] = "claude"
        task["model_id"] = "test-basic"
        submitted_status, submitted = self._post(
            "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-E2E",
            "plan.update",
            "CMD-E2E-SUBMIT",
            str(plan["snapshot_commit"]),
            int(plan["revision"]),
            {
                "plan_id": "PLAN-E2E",
                "requirement": requirement,
                "tasks": [task],
                "submit_for_approval": True,
            },
            method="PATCH",
        )
        self.assertEqual(submitted_status, 200, submitted)
        self.assertEqual(submitted["outcome"], "approval_pending")
        overview_status, overview = self._get(
            "/api/dockyard/v1/projects/PRJ-1/overview"
        )
        self.assertEqual(overview_status, 200, overview)
        self.assertEqual(overview["schema_version"], "dockyard.operational-overview/v1")
        pending = overview["plan_approval"]
        self.assertIsNotNone(pending)
        return pending

    def test_requirement_to_explicit_acceptance_and_integration(self) -> None:
        self._start(workers=True)
        pending = self._create_and_submit()
        before = StateProvider(self.fixture.project_root).snapshot()
        self.assertEqual(before.tasks, ())
        self.assertNotIn(
            "TASK_DISPATCHED",
            [event.event_type for event in before.events],
        )

        async def audit(*args, **kwargs):
            return workflow_fixtures._make_fake_audit_result("pass")

        with patch.object(workflow_orchestrator, "run_audit_gateway", audit):
            approved_status, approved = self._post(
                "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-E2E/approve",
                "plan.approve",
                "CMD-E2E-APPROVE",
                str(pending["snapshot_commit"]),
                int(pending["revision"]),
                {
                    "plan_id": "PLAN-E2E",
                    "plan_digest": pending["plan_digest"],
                    "task_count": pending["task_count"],
                },
                confirmation_id="CONF-E2E-APPROVE",
            )
        self.assertEqual(approved_status, 200, approved)
        self.assertEqual(approved["outcome"], "materialized")

        reviews_status, reviews = self._get(
            "/api/dockyard/v1/projects/PRJ-1/tasks"
        )
        self.assertEqual(reviews_status, 200, reviews)
        self.assertEqual(len(reviews["reviews"]), 1)
        review = reviews["reviews"][0]
        self.assertEqual(review["audit_verdict"], "pass")
        self.assertEqual(review["review_phase"], "MAD_COMPLETED")
        runs_status, runs = self._get(
            "/api/dockyard/v1/projects/PRJ-1/runs"
        )
        self.assertEqual(runs_status, 200, runs)
        self.assertEqual(len(runs["runs"]), 1)
        run = runs["runs"][0]
        self.assertEqual(run["dispatch_id"], review["dispatch_id"])
        self.assertEqual(run["phase"], "FINALIZED")
        self.assertEqual(run["supervisor_state"], "NOT_APPLICABLE")
        self.assertEqual(run["worker_state"], "NOT_APPLICABLE")
        self.assertEqual(run["heartbeat_state"], "DONE")
        self.assertEqual(run["lease_state"], "RELEASED")
        self.assertFalse(any("pid" in key for key in run))
        receipt = dockyard_composition.dispatch_evidence.read_dispatch_receipt(
            self.fixture.project_root,
            review["dispatch_id"],
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        worker_started = replace(
            receipt,
            phase="WORKER_STARTED",
        )
        with (
            patch.object(
                dockyard_composition.dispatch_evidence,
                "read_dispatch_receipt",
                return_value=worker_started,
            ),
            patch.object(
                dockyard_composition.dispatch_evidence,
                "read_dispatch_tombstone",
                return_value=None,
            ),
        ):
            ack_status, ack_runs = self._get(
                "/api/dockyard/v1/projects/PRJ-1/runs"
            )
        self.assertEqual(ack_status, 200, ack_runs)
        self.assertEqual(ack_runs["runs"][0]["phase"], "ACKNOWLEDGED")
        self.assertEqual(ack_runs["runs"][0]["worker_state"], "ALIVE")
        with patch.object(
            dockyard_composition.dispatch_evidence,
            "read_dispatch_receipt",
            return_value=replace(receipt, revision=receipt.revision + 1),
        ):
            divergent_status, divergent = self._get(
                "/api/dockyard/v1/projects/PRJ-1/runs"
            )
        self.assertEqual(divergent_status, 500, divergent)
        self.assertEqual(divergent["error_code"], "READ_FAILED")
        review_ready = StateProvider(self.fixture.project_root).snapshot()
        self.assertEqual(review_ready.tasks[0].state, "review_ready")
        self.assertNotIn(
            "DELIVERY_ACCEPTED",
            [event.event_type for event in review_ready.events],
        )

        accepted_status, accepted = self._post(
            f"/api/dockyard/v1/projects/PRJ-1/deliveries/{review['task_id']}/accept",
            "delivery.accept",
            "CMD-E2E-ACCEPT",
            str(review["snapshot_commit"]),
            int(review["revision"]),
            {
                "task_id": review["task_id"],
                "implementation_commit": review["implementation_commit"],
                "report_commit": review["report_commit"],
            },
            confirmation_id="CONF-E2E-ACCEPT",
        )
        self.assertEqual(accepted_status, 200, accepted)
        self.assertEqual(accepted["outcome"], "integrated")
        final = StateProvider(self.fixture.project_root).snapshot()
        self.assertEqual(final.tasks[0].state, "integrated")
        event_types = [event.event_type for event in final.events]
        for event_type in (
            "TASK_DISPATCHED",
            "DISPATCH_ACKNOWLEDGED",
            "DELIVERY_SUBMITTED",
            "DELIVERY_ACCEPTED",
            "CHANGE_INTEGRATED",
        ):
            self.assertEqual(event_types.count(event_type), 1, event_types)
        observed = {event.event_type: event for event in final.events}
        self.assertLess(
            observed["TASK_DISPATCHED"].occurred_at,
            observed["DISPATCH_ACKNOWLEDGED"].occurred_at,
        )
        self.assertLess(
            observed["DISPATCH_ACKNOWLEDGED"].occurred_at,
            observed["DELIVERY_SUBMITTED"].occurred_at,
        )
        self.assertLess(
            observed["DELIVERY_SUBMITTED"].occurred_at,
            observed["DELIVERY_ACCEPTED"].occurred_at,
        )
        self.assertLess(
            observed["DELIVERY_ACCEPTED"].occurred_at,
            observed["CHANGE_INTEGRATED"].occurred_at,
        )

    def test_plan_waiting_for_approval_has_zero_dispatch(self) -> None:
        self._start(workers=False)
        pending = self._create_and_submit()
        self.assertEqual(pending["phase"], "APPROVAL_PENDING")
        snapshot = StateProvider(self.fixture.project_root).snapshot()
        self.assertEqual(snapshot.tasks, ())
        self.assertNotIn(
            "TASK_DISPATCHED",
            [event.event_type for event in snapshot.events],
        )
        self.assertFalse(
            (self.fixture.project_root / ".agentdesk/runtime/portfolio-scheduler").exists()
        )

    def test_stale_requirement_snapshot_is_zero_write(self) -> None:
        self._start(workers=False)
        project_status, project = self._get(
            "/api/dockyard/v1/projects/PRJ-1"
        )
        self.assertEqual(project_status, 200, project)
        status, result = self._post(
            "/api/dockyard/v1/projects/PRJ-1/plans",
            "plan.create",
            "CMD-E2E-STALE",
            "f" * 40,
            int(project["project_generation"]),
            {"plan_id": "PLAN-E2E", "requirement": "不得写入"},
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error_code"], "SNAPSHOT_STALE")
        plan_status, plan = self._get(
            "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-E2E"
        )
        self.assertEqual(plan_status, 404, plan)

    def test_unavailable_provider_is_rejected_before_dispatch(self) -> None:
        self._start(workers=False)
        pending = self._create_and_submit()
        status, result = self._post(
            "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-E2E/approve",
            "plan.approve",
            "CMD-E2E-NOT-READY",
            str(pending["snapshot_commit"]),
            int(pending["revision"]),
            {
                "plan_id": "PLAN-E2E",
                "plan_digest": pending["plan_digest"],
                "task_count": pending["task_count"],
            },
            confirmation_id="CONF-E2E-NOT-READY",
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error_code"], "PROVIDER_NOT_READY")
        snapshot = StateProvider(self.fixture.project_root).snapshot()
        self.assertEqual(snapshot.tasks, ())
        self.assertEqual(snapshot.events, ())
        self.assertFalse(
            (self.fixture.project_root / ".agentdesk/runtime/portfolio-scheduler").exists()
        )


class DockyardOwnerLossLocalRuntimeE2E(unittest.TestCase):
    """A real loopback runtime starts the existing owner-loss recovery owner."""

    def setUp(self) -> None:
        self.fixture = recovery_fixtures.DockyardOwnerLossRecoveryTests(
            "test_real_dead_process_recovers_and_finalizes_attempt_two"
        )
        self.fixture.setUp()
        self.runtime: DockyardLocalRuntime | None = None
        self.extra_directories: tuple[Path, ...] = ()

    def tearDown(self) -> None:
        if self.runtime is not None:
            self.runtime.stop()
        import shutil
        for directory in self.extra_directories:
            shutil.rmtree(directory, ignore_errors=True)
        self.fixture.tearDown()

    def test_runtime_recovers_dead_owner_and_projects_retry_over_http(self) -> None:
        root = self.fixture.root
        snapshot = StateProvider(root).snapshot()
        registry_root = root.parent / (root.name + "-dockyard-registry")
        pairing_root = root.parent / (root.name + "-dockyard-pairing")
        plan_root = root.parent / (root.name + "-dockyard-plans")
        self.extra_directories = (registry_root, pairing_root, plan_root)
        for directory in (registry_root, pairing_root, plan_root):
            directory.mkdir(parents=True, exist_ok=True)
        bindings = root / ".agentdesk/runtime/model-bindings.yaml"
        bindings.parent.mkdir(parents=True, exist_ok=True)
        bindings.write_text(json.dumps({
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": "2026-08-04T06:00:00Z",
            "bindings": {
                "binding-001": {
                    "provider": "claude",
                    "model_id": "test-model",
                    "tier": "advanced",
                    "deliberation_tier": "balanced",
                    "context_window_tokens": 200000,
                    "capabilities": [],
                    "enabled": True,
                },
            },
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        local_fixtures.DockyardProjectRegistry(registry_root).register(
            local_fixtures.DockyardRegisterProjectRequest(
                snapshot.project_id,
                "Dockyard owner-loss E2E",
                str(root),
                "OP-REGISTER-OWNER-LOSS",
                "2026-08-04T06:00:00Z",
            )
        )
        pairing = local_fixtures.DockyardPairingStore(pairing_root)
        issued = pairing.issue(local_fixtures.DockyardPairingIssueRequest(
            "PAIR-OWNER-LOSS",
            "2026-08-04T05:00:00Z",
            "2026-08-04T05:05:00Z",
            3,
            "OP-ISSUE-OWNER-LOSS",
        ))
        device = pairing.consume(local_fixtures.DockyardPairingConsumeRequest(
            "PAIR-OWNER-LOSS",
            issued.pairing_code,
            "DEV-OWNER-LOSS",
            "Owner-loss browser",
            (
                local_fixtures.DockyardDeviceScope.APPROVE,
                local_fixtures.DockyardDeviceScope.RETRY,
            ),
            "2026-08-04T05:01:00Z",
            "OP-CONSUME-OWNER-LOSS",
        ))
        config = local_fixtures.DockyardLocalRuntimeConfig(
            "RUNTIME-OWNER-LOSS",
            snapshot.project_id,
            "PLAN-OWNER-LOSS",
            root,
            registry_root,
            pairing_root,
            plan_root,
            root / "tasks",
            "DEV-OWNER-LOSS",
            device.device_token,
            "CSRF-OWNER-LOSS",
            "http://127.0.0.1:5173",
            "127.0.0.1",
            0,
            "2026-08-04T06:00:00Z",
        )
        self.runtime = DockyardLocalRuntime(
            config,
            {"claude": self.fixture.provider},
            (("claude", "2.1.214"),),
            clock=self.fixture.clock,
        )
        result = self.runtime.start()
        recovery = self.runtime._recovery_runtime
        self.assertIsNotNone(recovery)
        assert recovery is not None
        recovered = recovery.wait(30)
        self.assertEqual(len(recovered), 1)
        self.assertIs(
            recovered[0].outcome,
            recovery_fixtures.DockyardOwnerLossRecoveryOutcome.RETRY_FINALIZED,
        )
        connection = http.client.HTTPConnection(
            result.address.host,
            result.address.port,
            timeout=30,
        )
        connection.request(
            "GET",
            f"/api/dockyard/v1/projects/{snapshot.project_id}/runs",
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200, body)
        retry_runs = tuple(
            run for run in body["runs"]
            if run["dispatch_id"] == self.fixture.next_dispatch_id
        )
        self.assertEqual(len(retry_runs), 1)
        self.assertEqual(retry_runs[0]["phase"], "FINALIZED")
        final = StateProvider(root).snapshot()
        self.assertEqual(
            (final.tasks[0].state, final.tasks[0].attempt),
            ("review_ready", 2),
        )


if __name__ == "__main__":
    unittest.main()
