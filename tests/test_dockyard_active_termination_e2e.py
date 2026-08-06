"""Closed-loop active termination verification for TC-13.29l.12e."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_active_execution import (  # noqa: E402
    DockyardActiveCancellationOutcome,
    DockyardActiveExecutionRegistry,
    make_active_execution_identity,
)
from dockyard_composition import (  # noqa: E402
    DockyardCompositionGateway,
    DockyardPlanReadService,
)
from dockyard_control_api import (  # noqa: E402
    DockyardCommandEnvelope,
    DockyardCommandRequest,
    DockyardReadRequest,
)
from dockyard_plan_store import DockyardPlanStore  # noqa: E402
from dockyard_pm_service import DockyardPmService  # noqa: E402
from dockyard_project_registry import (  # noqa: E402
    DockyardProjectRegistry,
    DockyardRegisterProjectRequest,
)
from dockyard_sse import DockyardSseHub  # noqa: E402
import dispatch_supervisor_evidence as dispatch_evidence  # noqa: E402
import control_plane_transition as cpt  # noqa: E402
from tests.test_control_plane_transition import _TransitionTestHarness  # noqa: E402


class DockyardActiveTerminationE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _TransitionTestHarness()
        self.tmpdir, self.root, self.head, _, _ = self.harness._setup_project(
            cpt,
            task_state="in_progress",
            task_id="TC-001",
            revision=1,
            attempt=1,
            current_dispatch={
                "dispatch_id": "DSP-001",
                "role_id": "worker-basic",
                "base_commit": "0" * 40,
                "branch": "main",
            },
            setup_approval_grant=False,
        )
        tasks_path = self.root / "docs" / "pm" / "state" / "tasks.yaml"
        state = json.loads(tasks_path.read_text(encoding="utf-8"))
        state["adoption_level"] = "standard"
        dispatch = state["tasks"][0]["current_dispatch"]
        dispatch.update({
            "attempt_id": "attempt-1",
            "dispatched_at": "2026-08-04T00:00:00Z",
            "model_selection": {
                "required_model_tier": "basic",
                "required_model_capabilities": ["coding"],
                "model_binding_id": "binding-basic",
                "selected_model_provider": "test",
                "selected_model_id": "test-model",
                "selected_model_tier": "basic",
                "selected_deliberation_tier": "efficient",
                "selected_context_window_tokens": 4096,
                "selected_model_capabilities": ["coding"],
                "model_degradation_approval_id": None,
            },
        })
        tasks_path.write_text(json.dumps(state), encoding="utf-8")
        events_root = self.root / "docs" / "pm" / "events"
        dispatch_event = {
            "schema_version": "agentdesk.state-event/v2",
            "event_id": "EVT-DSP-E2E",
            "event_type": "TASK_DISPATCHED",
            "task_id": "TC-001",
            "revision": 1,
            "attempt": 1,
            "dispatch_id": "DSP-001",
            "from_state": "ready",
            "to_state": "dispatched",
            "lease_epoch": 1,
            "actor_role_id": "PM",
            "occurred_at": "2026-08-04T00:00:00Z",
            "source_message_id": None,
            "evidence_refs": [],
            "guard_results": [],
            "payload_digest": "sha256:" + "0" * 64,
        }
        acknowledgement = dict(dispatch_event)
        acknowledgement.update({
            "event_id": "EVT-ACK-E2E",
            "event_type": "DISPATCH_ACKNOWLEDGED",
            "from_state": "dispatched",
            "to_state": "in_progress",
        })
        del acknowledgement["payload_digest"]
        outbox = {
            "schema_version": "agentdesk.outbox-message/v2",
            "message_id": "MSG-DSP-E2E",
            "event_id": "EVT-DSP-E2E",
            "message_type": "TASK_DISPATCHED",
            "dedupe_key": "TASK_DISPATCHED:TC-001:1:1",
            "task_id": "TC-001",
            "revision": 1,
            "attempt": 1,
            "dispatch_id": "DSP-001",
            "destination_role_id": "worker-basic",
            "created_at": "2026-08-04T00:00:00Z",
            "model_selection": dispatch["model_selection"],
            "payload": {
                "task_path": "docs/pm/tasks/TC-001.md",
                "task_card_commit": "a" * 40,
                "base_commit": "a" * 40,
                "branch": "main",
                "report_path": "docs/pm/reports/TC-001-r1.md",
            },
        }
        outbox_bytes = cpt._to_yaml_str(outbox).encode("utf-8")
        (self.root / "docs" / "pm" / "outbox" / "MSG-DSP-E2E.yaml").write_bytes(outbox_bytes)
        dispatch_event["payload_digest"] = "sha256:" + hashlib.sha256(outbox_bytes).hexdigest()
        (events_root / "EVT-DSP-E2E.yaml").write_text(cpt._to_yaml_str(dispatch_event), encoding="utf-8")
        (events_root / "EVT-ACK-E2E.yaml").write_text(cpt._to_yaml_str(acknowledgement), encoding="utf-8")
        self.registry_root = self.root / "registry"
        self.project_registry = DockyardProjectRegistry(self.registry_root)
        self.project_registry.register(DockyardRegisterProjectRequest(
            "test-project", "Dockyard", str(self.root), "OP-REGISTER", "2026-08-04T00:00:00Z",
        ))
        self.pm = DockyardPmService(DockyardPlanStore(self.root / "plans"), self.root / "docs/pm/tasks")
        dispatch_evidence.reserve_receipt(
            self.root,
            task_id="TC-001",
            revision=1,
            attempt=1,
            dispatch_id="DSP-001",
            lease_epoch=1,
            holder_instance_id="runner-1",
            generation_id="GEN-001",
            creator_pid=1,
            creator_creation_time="2026-08-04T00:00:00Z",
            boot_id="boot-1",
        )
        dispatch_evidence.advance_to_supervisor_ready(
            self.root,
            dispatch_id="DSP-001",
            generation_id="GEN-001",
            supervisor_pid=2,
            supervisor_creation_time="2026-08-04T00:00:00Z",
        )
        dispatch_evidence.advance_to_worker_started(
            self.root,
            dispatch_id="DSP-001",
            generation_id="GEN-001",
            worker_pid=3,
            worker_creation_time="2026-08-04T00:00:00Z",
            worker_process_group=None,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _compose(self) -> tuple[DockyardPlanReadService, DockyardCompositionGateway, DockyardActiveExecutionRegistry, list[object]]:
        registry = DockyardActiveExecutionRegistry()
        calls: list[object] = []

        def cancel(command: object) -> DockyardActiveCancellationOutcome:
            calls.append(command)
            return DockyardActiveCancellationOutcome(
                "TC-001", "DSP-001", "EVT-CANCEL-E2E", "2026-08-04T00:00:02Z",
            )

        registry.register(
            make_active_execution_identity(
                "TC-001", 1, 1, "DSP-001", "GEN-001", "2026-08-04T00:00:01Z",
            ),
            cancel,
        )
        read_service = DockyardPlanReadService(
            lambda: self.head,
            self.project_registry,
            self.pm,
            "test-project",
            "PLAN-1",
            project_root=self.root,
            active_registry=registry,
        )
        gateway = DockyardCompositionGateway(
            self.pm,
            lambda: self.head,
            DockyardSseHub(),
            self.project_registry,
            project_root=self.root,
            active_registry=registry,
            now=lambda: "2026-08-04T00:00:02Z",
        )
        return read_service, gateway, registry, calls

    def _request(self, payload: dict[str, object], event_id: str = "EVT-CANCEL-E2E") -> DockyardCommandRequest:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = DockyardCommandEnvelope(
            "dockyard.command/v1", "CMD-TERM-E2E", "test-project", "dispatch.terminate",
            "IDEMP-TERM-E2E", self.head, 1, "CONF-TERM-E2E", hashlib.sha256(raw).hexdigest(),
        )
        return DockyardCommandRequest(envelope, "DSP-001", raw)

    def test_runs_evidence_reaches_real_gateway_and_replays(self) -> None:
        read_service, gateway, registry, calls = self._compose()
        response = read_service.read(DockyardReadRequest("runs", "test-project", None))
        body = json.loads(response.body)
        run = body["runs"][0]
        self.assertTrue(run["termination_available"])
        payload = {
            "task_id": run["task_id"],
            "attempt": run["attempt"],
            "dispatch_id": run["dispatch_id"],
            "generation_id": run["generation_id"],
            "event_id": run["termination_event_id"],
        }
        request = self._request(payload)
        first = gateway.execute(request)
        replay = gateway.execute(request)
        self.assertEqual(first.outcome, "cancelled")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(registry.snapshot(), ())

    def test_generation_divergence_is_not_projected_as_terminable(self) -> None:
        read_service, _, _, _ = self._compose()
        dispatch_evidence.reserve_receipt  # keep durable receipt path explicit in this scenario
        registry = DockyardActiveExecutionRegistry()
        registry.register(
            make_active_execution_identity(
                "TC-001", 1, 1, "DSP-001", "GEN-OTHER", "2026-08-04T00:00:01Z",
            ),
            lambda command: DockyardActiveCancellationOutcome(
                "TC-001", "DSP-001", "EVT-CANCEL-E2E", "2026-08-04T00:00:02Z",
            ),
        )
        divergent = DockyardPlanReadService(
            lambda: self.head,
            self.project_registry,
            self.pm,
            "test-project",
            "PLAN-1",
            project_root=self.root,
            active_registry=registry,
        )
        body = json.loads(divergent.read(DockyardReadRequest("runs", "test-project", None)).body)
        self.assertFalse(body["runs"][0]["termination_available"])
        self.assertIsNone(body["runs"][0]["termination_event_id"])
        self.assertIsNotNone(read_service)


if __name__ == "__main__":
    unittest.main()
