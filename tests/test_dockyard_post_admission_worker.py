from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/agentdesk/scripts"
sys.path.insert(0, str(SCRIPTS))

import dockyard_post_admission_worker as post
from dockyard_composition import DockyardCompositionGateway
from dockyard_control_api import DockyardCommandEnvelope, DockyardCommandRequest
from dockyard_local_runtime import DockyardLocalRuntime
from dockyard_project_registry import DockyardProjectRegistry
from dockyard_sse import DockyardSseHub
from dockyard_task_admission_composition import DockyardTaskAdmissionCompositionRuntime
from dispatcher_gateway import AgentCliInvocation
from pm_materialization_admission_handoff import MaterializationAdmissionRuntime
from portfolio_scheduler_store import PortfolioSchedulerStore
from state_provider import StateProvider
from tests.test_dockyard_local_runtime import DockyardLocalRuntimeTests


class _Clock:
    def __init__(self) -> None:
        self.value = None
        self.mono = 0.0

    def now(self):
        if self.value is None:
            self.value = datetime.now(timezone.utc) + timedelta(seconds=1)
        result = self.value
        self.value += timedelta(seconds=1)
        return result

    def monotonic(self):
        result = self.mono
        self.mono += 1
        return result

    async def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.mono += seconds
        await asyncio.sleep(0)


class _LocalProvider:
    provider_id = "claude"
    executable = sys.executable

    def __init__(self, stdout: bytes) -> None:
        encoded = base64.b64encode(stdout).decode("ascii")
        self.code = f"import base64,sys;sys.stdout.buffer.write(base64.b64decode({encoded!r}))"
        self.calls = 0

    def build_invocation(self, request):
        self.calls += 1
        return AgentCliInvocation(sys.executable, ("-c", self.code), None, ())


class _WorkspaceLocalProvider:
    provider_id = "claude"
    executable = sys.executable

    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def build_invocation(self, request):
        self.calls += 1
        self.requests.append(request)
        identity = request.identity
        code = f"""
import json
import subprocess
import sys
from pathlib import Path

Path("dockyard-implementation.txt").write_text(
    "implemented attempt " + str({identity.attempt!r}) + "\\n", encoding="utf-8"
)
subprocess.run(["git", "add", "dockyard-implementation.txt"], check=True)
subprocess.run(["git", "commit", "--quiet", "-m", "test: deterministic implementation"], check=True)
implementation_commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
).stdout.strip()
Path("reports").mkdir(exist_ok=True)
Path("reports/dockyard-delivery.md").write_text(
    "delivery attempt " + str({identity.attempt!r}) + "\\n", encoding="utf-8"
)
subprocess.run(["git", "add", "reports/dockyard-delivery.md"], check=True)
subprocess.run(["git", "commit", "--quiet", "-m", "docs: deterministic delivery"], check=True)
report_commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
).stdout.strip()
envelope = {{
    "schema_version": "agentdesk.worker-output/v1",
    "task_id": {identity.task_id!r},
    "revision": {identity.revision!r},
    "attempt": {identity.attempt!r},
    "dispatch_id": {identity.dispatch_id!r},
    "status": "completed",
    "implementation_commit": implementation_commit,
    "report_commit": report_commit,
    "summary": "deterministic local delivery",
    "warnings": [],
}}
wrapper = {{
    "type": "result", "subtype": "success", "is_error": False,
    "api_error_status": None, "duration_ms": 1, "duration_api_ms": 1,
    "ttft_ms": 1, "ttft_stream_ms": 1, "time_to_request_ms": 1,
    "num_turns": 1, "result": json.dumps(envelope, separators=(",", ":")),
    "stop_reason": "end_turn", "session_id": "dockyard-local",
    "total_cost_usd": None,
    "usage": {{"input_tokens": 1, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "output_tokens": 1,
        "server_tool_use": {{"web_search_requests": 0, "web_fetch_requests": 0}},
        "service_tier": "standard",
        "cache_creation": {{"ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0}}}},
    "modelUsage": {{}}, "permission_denials": [],
    "terminal_reason": "completed", "fast_mode_state": "off",
    "uuid": "dockyard-local-result",
}}
sys.stdout.write(json.dumps(wrapper, separators=(",", ":")))
"""
        return AgentCliInvocation(sys.executable, ("-c", code), None, ())


def _wrapper(task_id: str, revision: int, attempt: int, dispatch_id: str, implementation_commit: str, report_commit: str) -> bytes:
    envelope = {
        "schema_version": "agentdesk.worker-output/v1", "task_id": task_id,
        "revision": revision, "attempt": attempt, "dispatch_id": dispatch_id,
        "status": "completed", "implementation_commit": implementation_commit,
        "report_commit": report_commit, "summary": "deterministic local delivery",
        "warnings": [],
    }
    wrapper = {
        "type": "result", "subtype": "success", "is_error": False,
        "api_error_status": None, "duration_ms": 1, "duration_api_ms": 1,
        "ttft_ms": 1, "ttft_stream_ms": 1, "time_to_request_ms": 1,
        "num_turns": 1, "result": json.dumps(envelope, separators=(",", ":")),
        "stop_reason": "end_turn", "session_id": "dockyard-local", "total_cost_usd": None,
        "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}, "service_tier": "standard", "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}},
        "modelUsage": {}, "permission_denials": [], "terminal_reason": "completed",
        "fast_mode_state": "off", "uuid": "dockyard-local-result",
    }
    return json.dumps(wrapper, separators=(",", ":")).encode()


class DockyardPostAdmissionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = DockyardLocalRuntimeTests("test_runtime_starts_real_loopback_and_approves_through_production_owner")
        self.fx.setUp()

    def _prepare_admission_only(self) -> None:
        payload = {
            "plan_id": "PLAN-1",
            "plan_digest": self.fx.pending.plan_digest,
            "task_count": 1,
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        envelope = DockyardCommandEnvelope(
            "dockyard.command/v1",
            "CMD-ADMISSION-ONLY",
            "PRJ-1",
            "plan.approve",
            "IDEMP-ADMISSION-ONLY",
            self.fx.head,
            self.fx.pending.revision,
            "CONF-ADMISSION-ONLY",
            hashlib.sha256(raw).hexdigest(),
        )
        result = DockyardCompositionGateway(
            self.fx.pm,
            lambda: self.fx.head,
            DockyardSseHub(),
            DockyardProjectRegistry(self.fx.registry_root),
            DockyardTaskAdmissionCompositionRuntime(self.fx.project_root),
            plan_id="PLAN-1",
            project_root=self.fx.project_root,
            ready_provider_ids=("claude",),
        ).execute(DockyardCommandRequest(envelope, "PLAN-1", raw))
        self.assertEqual(result.outcome, "materialized")
        runtime = MaterializationAdmissionRuntime(self.fx.project_root)
        receipt_path = next((self.fx.project_root / ".agentdesk/runtime/materialization-admission/receipts").glob("*.yaml"))
        self.receipt = runtime.evidence_store.read_receipt(receipt_path.stem)
        assert self.receipt is not None and self.receipt.queue_id is not None
        self.plan = PortfolioSchedulerStore(self.fx.project_root).read_admission_plan(self.receipt.queue_id, 1)

    def tearDown(self) -> None:
        self.fx.tearDown()

    def test_exact_scheduler_worktree_and_lease_evidence_build_typed_worker_request(self) -> None:
        self._prepare_admission_only()
        provider = _LocalProvider(b"")
        expected = object()
        with patch.object(post, "run_pm_external_worker", AsyncMock(return_value=expected)) as run:
            result = post.DockyardPostAdmissionWorkerRuntime(
                self.fx.project_root, {"claude": provider}, (("claude", "2.1.214"),), clock=_Clock()
            ).execute(self.receipt)
        self.assertIs(result, expected)
        request, providers, orchestrator = run.await_args.args
        handoff = request.handoff_request
        self.assertEqual((handoff.dispatch_id, handoff.dispatch_event_id), (self.plan.dispatch_id, self.plan.event_id))
        self.assertEqual(handoff.dispatch_cycle_request.dispatch_request.workspace, Path(self.plan.canonical_worktree))
        self.assertEqual(providers["claude"], provider)
        self.assertEqual(orchestrator.project_root, self.fx.project_root)

    def test_divergent_plan_identity_rejects_before_worker_owner(self) -> None:
        self._prepare_admission_only()
        divergent = replace(self.plan, dispatch_id="DSP-DIVERGENT")
        scheduler = MagicMock()
        scheduler.read_admission_plan.return_value = divergent
        provider = _LocalProvider(b"")
        with (
            patch.object(post, "PortfolioSchedulerStore", return_value=scheduler),
            patch.object(post, "run_pm_external_worker", AsyncMock()) as run,
        ):
            with self.assertRaises(post.DockyardPostAdmissionConflictError):
                post.DockyardPostAdmissionWorkerRuntime(
                    self.fx.project_root,
                    {"claude": provider},
                    (("claude", "2.1.214"),),
                    clock=_Clock(),
                ).execute(self.receipt)
        run.assert_not_awaited()

    def test_finalized_tombstone_records_typed_dispatch_failure(self) -> None:
        self._prepare_admission_only()
        tombstone = SimpleNamespace(
            task_id=self.plan.task_id,
            revision=self.plan.revision,
            attempt=self.plan.new_attempt,
            dispatch_id=self.plan.dispatch_id,
            winner="completion",
            worker_done=True,
            heartbeat_done=True,
            release_completed=True,
            failure_kind="worker_failed",
        )
        task = SimpleNamespace(
            task_id=self.plan.task_id,
            revision=self.plan.revision,
            attempt=self.plan.new_attempt,
            state="in_progress",
            current_dispatch=SimpleNamespace(dispatch_id=self.plan.dispatch_id),
        )
        state_provider = MagicMock()
        state_provider.snapshot.return_value = SimpleNamespace(
            tasks=(task,), events=(),
        )
        transition_service = MagicMock()
        runtime = post.DockyardPostAdmissionWorkerRuntime(
            self.fx.project_root,
            {"claude": _LocalProvider(b"")},
            (("claude", "2.1.214"),),
            clock=_Clock(),
        )
        with (
            patch.object(post._dse, "read_dispatch_tombstone", return_value=tombstone),
            patch.object(post, "StateProvider", return_value=state_provider),
            patch.object(
                post, "ControlPlaneTransitionService",
                return_value=transition_service,
            ),
        ):
            runtime._record_finalized_dispatch_failure(self.plan)

        transition = transition_service.apply_transition.call_args.args[0]
        self.assertEqual(transition.cas.expected_state, "in_progress")
        self.assertEqual(transition.event_type, "DISPATCH_FAILED")
        self.assertEqual(
            transition.dispatch_cas.expected_dispatch_id,
            self.plan.dispatch_id,
        )
        self.assertEqual(transition.payload.failure_kind, "worker_failed")
        self.assertEqual(
            transition.event_context.evidence_refs,
            (
                f"dispatch-supervisor/{self.plan.dispatch_id}.receipt",
                f"dispatch-supervisor/{self.plan.dispatch_id}.tombstone",
            ),
        )
        self.assertIsNone(
            transition_service.apply_transition.call_args.kwargs["lease"]
        )

    def test_incomplete_tombstone_fails_closed_without_transition(self) -> None:
        self._prepare_admission_only()
        tombstone = SimpleNamespace(
            task_id=self.plan.task_id,
            revision=self.plan.revision,
            attempt=self.plan.new_attempt,
            dispatch_id=self.plan.dispatch_id,
            winner="completion",
            worker_done=True,
            heartbeat_done=True,
            release_completed=False,
            failure_kind="worker_failed",
        )
        state_provider = MagicMock()
        transition_service = MagicMock()
        runtime = post.DockyardPostAdmissionWorkerRuntime(
            self.fx.project_root,
            {"claude": _LocalProvider(b"")},
            (("claude", "2.1.214"),),
            clock=_Clock(),
        )
        with (
            patch.object(post._dse, "read_dispatch_tombstone", return_value=tombstone),
            patch.object(post, "StateProvider", return_value=state_provider),
            patch.object(
                post, "ControlPlaneTransitionService",
                return_value=transition_service,
            ),
        ):
            with self.assertRaises(post.DockyardPostAdmissionConflictError):
                runtime._record_finalized_dispatch_failure(self.plan)

        state_provider.snapshot.assert_not_called()
        transition_service.apply_transition.assert_not_called()

    def test_real_local_helper_advances_ack_delivery_and_finalizes_handoff(self) -> None:
        provider = _WorkspaceLocalProvider()
        config = self.fx.project_root / ".agentdesk/runtime/external-workers.yaml"
        config.write_text(json.dumps({
            "schema_version": "agentdesk.external-workers/v1", "updated_at": "2026-08-03T04:00:00Z",
            "workers": {"R1": {"endpoint_id": "EXT-R1", "role_id": "R1", "provider_id": "claude", "model_binding_id": "basic", "executable": sys.executable, "permission_mode": "deterministic", "status": "verified"}},
        }, indent=2) + "\n", encoding="utf-8")
        local = DockyardLocalRuntime(
            self.fx.config,
            {"claude": provider},
            (("claude", "2.1.214"),),
            clock=_Clock(),
        )
        self.fx.runtimes.append(local)
        address = local.start().address
        payload = {
            "plan_id": "PLAN-1",
            "plan_digest": self.fx.pending.plan_digest,
            "task_count": 1,
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1",
            "command_id": "CMD-POST-WORKER-1",
            "project_id": "PRJ-1",
            "command_type": "plan.approve",
            "idempotency_key": "IDEMP-POST-WORKER-1",
            "expected_snapshot_commit": self.fx.head,
            "expected_revision": self.fx.pending.revision,
            "confirmation_id": "CONF-POST-WORKER-1",
            "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(address.host, address.port, timeout=20)
        connection.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1/approve",
            body,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Origin": "http://127.0.0.1:5173",
                "Authorization": f"Bearer {self.fx.device_token}",
                "X-Dockyard-Device-Id": "DEV-1",
                "X-Dockyard-CSRF": "CSRF-1",
                "X-Dockyard-Idempotency-Key": "IDEMP-POST-WORKER-1",
            },
        )
        response = connection.getresponse()
        response_body = json.loads(response.read())
        connection.close()
        self.assertIn("outcome", response_body, response_body)
        self.assertEqual((response.status, response_body["outcome"]), (200, "materialized"))
        admission = MaterializationAdmissionRuntime(self.fx.project_root)
        receipt_path = next(
            (
                self.fx.project_root
                / ".agentdesk/runtime/materialization-admission/receipts"
            ).glob("*.yaml")
        )
        receipt = admission.evidence_store.read_receipt(receipt_path.stem)
        assert receipt is not None and receipt.queue_id is not None
        plan = PortfolioSchedulerStore(self.fx.project_root).read_admission_plan(
            receipt.queue_id, 1
        )
        self.assertEqual(provider.calls, 2)  # preflight resolution + one Worker start
        self.assertEqual(
            len(
                tuple(
                    (self.fx.project_root / ".agentdesk/runtime/dispatch-supervisor").glob(
                        "*.receipt.yaml"
                    )
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                tuple(
                    (
                        self.fx.project_root
                        / "docs/pm/portfolio-scheduler/worker-handoffs"
                    ).glob("*.yaml")
                )
            ),
            2,
        )
        snapshot = StateProvider(self.fx.project_root).snapshot()
        self.assertEqual(
            sorted(event.event_type for event in snapshot.events),
            ["DELIVERY_SUBMITTED", "DISPATCH_ACKNOWLEDGED", "TASK_DISPATCHED"],
        )
        transitions = {
            event.event_type: (event.from_state, event.to_state)
            for event in snapshot.events
        }
        self.assertEqual(
            transitions,
            {
                "TASK_DISPATCHED": ("ready", "dispatched"),
                "DISPATCH_ACKNOWLEDGED": ("dispatched", "in_progress"),
                "DELIVERY_SUBMITTED": ("in_progress", "review_ready"),
            },
        )
        self.assertEqual(snapshot.tasks[0].state, "review_ready")
        replay = post.DockyardPostAdmissionWorkerRuntime(
            self.fx.project_root,
            {"claude": provider},
            (("claude", "2.1.214"),),
            clock=_Clock(),
        ).execute(receipt)
        self.assertTrue(replay.receipt.delivery_result_available)
        self.assertEqual(replay.receipt.dispatch_id, plan.dispatch_id)
        self.assertIsNone(replay.dispatch_cycle_result)
        self.assertEqual(provider.calls, 2)
        replay_snapshot = StateProvider(self.fx.project_root).snapshot()
        self.assertEqual(replay_snapshot.events, snapshot.events)


if __name__ == "__main__":
    unittest.main()
