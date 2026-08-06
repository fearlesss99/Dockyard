"""Real local retry-owner verification for TC-13.29l.13c."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import control_plane_transition as cpt  # noqa: E402
from dockyard_control_api import DockyardCommandRejected  # noqa: E402
import dispatch_supervisor_evidence as dispatch_evidence  # noqa: E402
import difficulty_assessment_evidence as assessment_evidence  # noqa: E402
import difficulty_assessment_store as assessment_store  # noqa: E402
import difficulty_assessor  # noqa: E402
from core_types import TaskDifficulty, WorkerKind  # noqa: E402
from dockyard_composition import DockyardCompositionGateway  # noqa: E402
from dockyard_plan_store import DockyardPlanStore  # noqa: E402
from dockyard_pm_service import DockyardPmService  # noqa: E402
from dockyard_project_registry import (  # noqa: E402
    DockyardProjectRegistry,
    DockyardRegisterProjectRequest,
)
from dockyard_sse import DockyardSseHub  # noqa: E402
from dispatcher_gateway import AgentCliInvocation  # noqa: E402
from portfolio_scheduler_worker_handoff_store import (  # noqa: E402
    HANDOFF_SCHEMA_VERSION,
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoff,
    ScheduledDispatchHandoffPhase,
    _with_handoff_digest,
)
from portfolio_scheduler_store import (  # noqa: E402
    AdmissionPlanReservation,
    ModelSelectionSnapshot as SchedulerModelSelectionSnapshot,
    PortfolioSchedulerStore,
    SCHEMA_VERSION as SCHEDULER_SCHEMA_VERSION,
    _plan_digest,
)
from state_provider import StateProvider  # noqa: E402
from tests.test_control_plane_transition import _TransitionTestHarness  # noqa: E402


class _LocalSuccessProvider:
    provider_id = "claude"

    def build_invocation(self, request):
        identity = request.identity
        wrapper = {
            "type": "result", "subtype": "success", "is_error": False,
            "api_error_status": None, "duration_ms": 1, "duration_api_ms": 1,
            "ttft_ms": 1, "ttft_stream_ms": 1, "time_to_request_ms": 1,
            "num_turns": 1,
            "result": json.dumps({
                "schema_version": "agentdesk.worker-output/v1",
                "task_id": identity.task_id, "revision": identity.revision,
                "attempt": identity.attempt, "dispatch_id": identity.dispatch_id,
                "status": "completed", "implementation_commit": "a" * 40,
                "report_commit": "b" * 40, "summary": "local retry",
                "warnings": [],
            }, separators=(",", ":")),
            "stop_reason": "end_turn", "session_id": "dockyard-retry",
            "total_cost_usd": None,
            "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 1,
                "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                "service_tier": "standard",
                "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}},
            "modelUsage": {}, "permission_denials": [],
            "terminal_reason": "completed", "fast_mode_state": "off",
            "uuid": "dockyard-retry-result",
        }
        encoded = json.dumps(wrapper, separators=(",", ":"))
        code = f"import sys;sys.stdout.write({encoded!r})"
        return AgentCliInvocation(sys.executable, ("-c", code), None, ())


def _git(root: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(root), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


class DockyardRetryClosedLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _TransitionTestHarness()
        self.tmp, self.root, _, _, _ = self.harness._setup_project(
            cpt, task_state="ready", task_id="TC-001", revision=1,
            attempt=1, current_dispatch=None, setup_approval_grant=False,
        )
        (self.root / "docs/pm/tasks").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/pm/tasks/TC-001.md").write_text("retry task\n", encoding="utf-8")
        _git(self.root, "add", "docs/pm/tasks/TC-001.md")
        _git(self.root, "commit", "-q", "-m", "test: retry task card")
        self.head = _git(self.root, "rev-parse", "HEAD")
        self._write_failed_attempt()

        self.registry = DockyardProjectRegistry(self.root / "registry")
        self.registry.register(DockyardRegisterProjectRequest(
            "PRJ-1", "Dockyard", str(self.root), "OP-REGISTER", "2026-08-04T00:00:00Z",
        ))
        self.pm = DockyardPmService(DockyardPlanStore(self.root / "plans"), self.root / "docs/pm/tasks")
        self.gateway = DockyardCompositionGateway(
            self.pm, lambda: _git(self.root, "rev-parse", "HEAD"), DockyardSseHub(),
            self.registry, project_root=self.root, providers={"claude": _LocalSuccessProvider()},
            provider_cli_versions=(("claude", "2.1.214"),), ready_provider_ids=("claude",),
            now=lambda: "2026-08-04T01:00:00Z",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_failed_attempt(self) -> None:
        tasks_path = self.root / "docs/pm/state/tasks.yaml"
        state = json.loads(tasks_path.read_text(encoding="utf-8"))
        task = state["tasks"][0]
        task["task_card_commit"] = self.head
        task["attempt"] = 1
        task["state"] = "ready"
        task["current_dispatch"] = None
        state["adoption_level"] = "standard"
        tasks_path.write_text(json.dumps(state), encoding="utf-8")
        model = {
            "required_model_tier": "standard", "required_model_capabilities": ["coding"],
            "model_binding_id": "binding-claude", "selected_model_provider": "claude",
            "selected_model_id": "claude-sonnet", "selected_model_tier": "standard",
            "selected_deliberation_tier": "balanced", "selected_context_window_tokens": 128000,
            "selected_model_capabilities": ["coding"], "model_degradation_approval_id": None,
        }
        outbox = {
            "schema_version": "agentdesk.outbox-message/v2", "message_id": "MSG-001",
            "event_id": "EVT-001-DSP", "message_type": "TASK_DISPATCHED",
            "dedupe_key": "TASK_DISPATCHED:TC-001:1:1", "task_id": "TC-001",
            "revision": 1, "attempt": 1, "dispatch_id": "DSP-001",
            "destination_role_id": "R1", "created_at": "2026-08-04T00:00:00Z",
            "model_selection": model,
            "payload": {"task_path": "docs/pm/tasks/TC-001.md", "task_card_commit": self.head,
                "base_commit": self.head, "branch": "main", "report_path": "docs/pm/reports/TC-001.md"},
        }
        outbox_dir = self.root / "docs/pm/outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        outbox_bytes = cpt._to_yaml_str(outbox).encode("utf-8")
        (outbox_dir / "MSG-001.yaml").write_bytes(outbox_bytes)
        events_dir = self.root / "docs/pm/events"
        common = {
            "schema_version": "agentdesk.state-event/v2", "task_id": "TC-001", "revision": 1,
            "attempt": 1, "lease_epoch": 1, "actor_role_id": "R1",
            "source_message_id": None, "evidence_refs": ["docs/pm/evidence/retry.yaml"],
            "guard_results": [],
        }
        events = [
            dict(common, event_id="EVT-001-DSP", event_type="TASK_DISPATCHED", dispatch_id="DSP-001",
                 from_state="ready", to_state="dispatched", occurred_at="2026-08-04T00:00:01Z",
                 payload_digest="sha256:" + hashlib.sha256(outbox_bytes).hexdigest()),
            dict(common, event_id="EVT-002-ACK", event_type="DISPATCH_ACKNOWLEDGED", dispatch_id="DSP-001",
                 from_state="dispatched", to_state="in_progress", occurred_at="2026-08-04T00:00:02Z"),
            dict(common, event_id="EVT-003-FAILED", event_type="DISPATCH_FAILED", dispatch_id="DSP-001",
                 from_state="in_progress", to_state="ready", occurred_at="2026-08-04T00:00:03Z",
                 failure_kind="worker_failed"),
        ]
        for event in events:
            (events_dir / (event["event_id"] + ".yaml")).write_text(cpt._to_yaml_str(event), encoding="utf-8")
        receipt = dispatch_evidence.reserve_receipt(
            self.root, task_id="TC-001", revision=1, attempt=1, dispatch_id="DSP-001",
            lease_epoch=1, holder_instance_id="runner-1", generation_id="GEN-001",
            creator_pid=1, creator_creation_time="2026-08-04T00:00:00Z", boot_id="boot-1",
        )
        dispatch_evidence.advance_to_supervisor_ready(self.root, dispatch_id="DSP-001", generation_id="GEN-001", supervisor_pid=2, supervisor_creation_time="2026-08-04T00:00:00Z")
        dispatch_evidence.advance_to_worker_started(self.root, dispatch_id="DSP-001", generation_id="GEN-001", worker_pid=3, worker_creation_time="2026-08-04T00:00:00Z", worker_process_group=None)
        dispatch_evidence.advance_to_finalizing(self.root, dispatch_id="DSP-001", generation_id="GEN-001")
        dispatch_evidence.write_finalizer_tombstone(
            self.root, task_id="TC-001", revision=1, attempt=1, dispatch_id="DSP-001",
            generation_id="GEN-001", winner="completion", worker_done=True,
            heartbeat_done=True, release_completed=True, failure_kind="worker_failed",
        )
        assessment = difficulty_assessor.assess_task_difficulty(
            task_id="TC-001", revision=1, assessment_id="ASM-RETRY-1",
            rationale_keys=("scope.bounded", "clarity.explicit", "concurrency.single_writer", "contract.internal", "impact.informational", "rollback.simple", "dependency.none"),
        )
        assessment_store.DifficultyAssessmentStore().write(assessment_store.DifficultyAssessmentStoreRequest(
            self.root, assessment_evidence.DifficultyAssessmentEvidence(assessment, self.head), self.head,
        ))
        handoff = _with_handoff_digest(ScheduledDispatchHandoff(
            HANDOFF_SCHEMA_VERSION, "HNDF-RETRY-1", "Q-RETRY-1", "sha256:" + "a" * 64, "SR-RETRY-1",
            "TC-001", 1, 1, "DSP-001", "EVT-001-DSP", "MSG-001", "LEASE-RETRY-1", 1,
            "runner-1", WorkerKind.BASIC_AGENT, "ASM-RETRY-1", self.head,
            "binding-claude", ScheduledDispatchHandoffPhase.ADMISSION_COMMITTED,
            "2026-08-04T00:00:00.000000Z", None, None, None, None, "sha256:" + "0" * 64,
        ))
        store = PortfolioSchedulerWorkerHandoffStore(self.root)
        scheduler_model = SchedulerModelSelectionSnapshot(
            "standard", ("coding",), "binding-claude", "claude", "claude-sonnet",
            "standard", "balanced", 128000, ("coding",), None,
        )
        plan = AdmissionPlanReservation(
            SCHEDULER_SCHEMA_VERSION, "Q-RETRY-1", "SR-RETRY-1", "TC-001", 1, 1, 1,
            WorkerKind.BASIC_AGENT, "ASM-RETRY-1", "DSP-001", "EVT-001-DSP", "MSG-001",
            "R1", "docs/pm/tasks/TC-001.md", self.head, self.head, "main",
            "docs/pm/reports/TC-001.md", scheduler_model, "ready", 0, 1, self.head,
            SCHEDULER_SCHEMA_VERSION, "runner-1", str(self.root), "2026-08-04T00:00:00.000000Z",
            "sha256:" + "0" * 64,
        )
        PortfolioSchedulerStore(self.root).reserve_admission_plan(
            replace(plan, content_digest=_plan_digest(plan))
        )
        handoff = _with_handoff_digest(
            replace(handoff, plan_digest=_plan_digest(plan))
        )
        store.reserve_handoff(handoff, dispatch_receipt=dispatch_evidence.read_dispatch_receipt(self.root, "DSP-001"))
        for phase, timestamp in (
            (ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED, "2026-08-04T00:00:04.000000Z"),
            (ScheduledDispatchHandoffPhase.WORKER_STARTED, "2026-08-04T00:00:05.000000Z"),
            (ScheduledDispatchHandoffPhase.ACKNOWLEDGED, "2026-08-04T00:00:06.000000Z"),
            (ScheduledDispatchHandoffPhase.FINALIZING, "2026-08-04T00:00:07.000000Z"),
            (ScheduledDispatchHandoffPhase.FINALIZED, "2026-08-04T00:00:08.000000Z"),
        ):
            store.advance_phase("DSP-001", phase, timestamp, dispatch_receipt=dispatch_evidence.read_dispatch_receipt(self.root, "DSP-001"))
        # The retry is a real gated dispatch, so provision the exact attempt-2
        # grant before invoking the composition gateway.  The grant remains
        # local evidence; the production gate still validates its CAS.
        command_id = "CMD-RETRY-E2E-1"
        suffix = hashlib.sha256(("dockyard-retry\0" + command_id).encode("utf-8")).hexdigest()[:32]
        next_dispatch_id = "DSP-RETRY-" + suffix
        approval = {
            "schema_version": "agentdesk.task-approval/v1",
            "record_type": "grant",
            "approval_id": "APR-RETRY-E2E-1",
            "event_id": "EVT-RETRY-APPROVAL-1",
            "scope": "dispatch",
            "task_id": "TC-001",
            "revision": 1,
            "attempt": 2,
            "dispatch_id": next_dispatch_id,
            "accepted_commit": None,
            "actor_role_id": "PM",
            "lease_epoch": 1,
            "granted_at": "2026-08-04T00:00:09Z",
            "expires_at": None,
            "reason": "approved retry E2E",
            "snapshot_commit": self.head,
        }
        approvals_dir = self.root / "docs/pm/approvals"
        approvals_dir.mkdir(parents=True, exist_ok=True)
        (approvals_dir / "EVT-RETRY-APPROVAL-1.yaml").write_text(
            json.dumps(approval, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _request(self, command_id: str = "CMD-RETRY-E2E-1"):
        payload = {"task_id": "TC-001", "failed_attempt": 1, "dispatch_id": "DSP-001", "generation_id": "GEN-001", "event_id": "EVT-003-FAILED", "provider_id": "claude", "model_id": "claude-sonnet", "upgrade": False}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        from dockyard_control_api import (
            DockyardCommandEnvelope,
            DockyardCommandRejected,
            DockyardCommandRequest,
        )
        return DockyardCommandRequest(DockyardCommandEnvelope("dockyard.command/v1", command_id, "PRJ-1", "task.retry", "IDEMP-" + command_id, self.head, 1, "CONF-RETRY-E2E", hashlib.sha256(raw).hexdigest()), "TC-001", raw)

    def test_retry_reaches_real_orchestrator_and_replays(self) -> None:
        first = self.gateway.execute(self._request())
        self.assertEqual(first.outcome, "retry_started")
        replay = self.gateway.execute(self._request())
        self.assertTrue(replay.replayed)
        snapshot = StateProvider(self.root).snapshot()
        self.assertEqual(snapshot.tasks[0].state, "review_ready")
        self.assertEqual(snapshot.tasks[0].attempt, 2)
        self.assertEqual(sum(event.event_type == "TASK_DISPATCHED" for event in snapshot.events), 2)

    def test_attempt_three_has_zero_write(self) -> None:
        tasks_path = self.root / "docs/pm/state/tasks.yaml"
        state = json.loads(tasks_path.read_text(encoding="utf-8"))
        state["tasks"][0]["attempt"] = 3
        tasks_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(DockyardCommandRejected) as ctx:
            self.gateway.execute(self._request("CMD-RETRY-E2E-3"))
        self.assertEqual(ctx.exception.code, "RETRY_CAS_CONFLICT")
        self.assertEqual(len(StateProvider(self.root).snapshot().events), 3)


if __name__ == "__main__":
    unittest.main()
