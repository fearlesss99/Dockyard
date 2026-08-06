"""Directed TC-13.29l.1 production composition tests."""

from __future__ import annotations

import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from dockyard_composition import (  # noqa: E402
    DockyardCompositionGateway,
    DockyardLocalReadService,
    DockyardPairingAuthorizer,
)
from dockyard_control_api import DockyardApiConfig, DockyardControlServer  # noqa: E402
from dockyard_pairing import (  # noqa: E402
    DockyardDeviceScope,
    DockyardPairingConsumeRequest,
    DockyardPairingIssueRequest,
    DockyardPairingStore,
)
from dockyard_plan_store import DockyardPlanPhase, DockyardPlanStore, DockyardPlanTask  # noqa: E402
from dockyard_pm_service import (  # noqa: E402
    DockyardPlanApprovalRequest,
    DockyardPlanDraftRequest,
    DockyardPlanTransitionRequest,
    DockyardPmService,
)
from dockyard_project_registry import (  # noqa: E402
    DockyardProjectPhase,
    DockyardProjectRegistry,
    DockyardRegisterProjectRequest,
)
from dockyard_sse import DockyardSseHub  # noqa: E402
from portfolio_scheduler_store import BusinessPriority  # noqa: E402
sys.path.pop(0)


_SNAPSHOT = "a" * 40
_NOW = "2026-08-03T01:00:00Z"


class DockyardCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.repository = root / "repository"
        self.repository.mkdir()
        subprocess.run(
            ("git", "init", "--quiet", str(self.repository)),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (self.repository / "README.md").write_text("kept\n", encoding="utf-8")
        self.registry = DockyardProjectRegistry(root / ".agentdesk" / "runtime" / "projects")
        self.project = self.registry.register(DockyardRegisterProjectRequest(
            "PRJ-1", "Dockyard", str(self.repository), "OP-REGISTER", "2026-08-03T00:00:00Z",
        )).record
        self.pm = DockyardPmService(
            DockyardPlanStore(root / ".agentdesk" / "runtime" / "plans"),
            root / "docs" / "pm" / "tasks",
        )
        task = DockyardPlanTask(
            "TC-001", "Control API", "Implement the local control API.", (),
            "parallel", "R1", "implementation", "codex", "gpt-5.6-sol",
            "standard", 12000, 3, _SNAPSHOT,
            BusinessPriority.P1,
            ("scope.bounded", "clarity.explicit", "concurrency.single_writer",
             "contract.internal", "impact.informational", "rollback.simple",
             "dependency.none"),
            None, None, None, "L0", ("coding",), None, 0, 1,
        )
        draft = self.pm.draft(DockyardPlanDraftRequest(
            "PLAN-1", "PRJ-1", "Compose Dockyard.", (task,), "PM-1",
            "2026-08-03T00:00:00Z", "OP-DRAFT",
        ))
        self.pending = self.pm.request_approval(DockyardPlanTransitionRequest(
            draft.plan_id, draft.revision, draft.content_digest,
            "2026-08-03T00:01:00Z", "OP-PENDING",
        ))
        pairing = DockyardPairingStore(root / ".agentdesk" / "runtime" / "pairing")
        issued = pairing.issue(DockyardPairingIssueRequest(
            "PAIR-1", "2026-08-03T00:00:00Z", "2026-08-03T00:05:00Z", 3, "OP-ISSUE",
        ))
        consumed = pairing.consume(DockyardPairingConsumeRequest(
            "PAIR-1", issued.pairing_code, "DEV-1", "Dockyard local browser",
            (DockyardDeviceScope.APPROVE, DockyardDeviceScope.RETRY), "2026-08-03T00:01:00Z", "OP-CONSUME",
        ))
        self.token = consumed.device_token
        self.hub = DockyardSseHub(max_backlog=8, max_streams=2)
        self.now = _NOW
        gateway = DockyardCompositionGateway(
            self.pm,
            lambda: _SNAPSHOT,
            self.hub,
            self.registry,
            now=lambda: self.now,
        )
        self.server = DockyardControlServer(
            DockyardApiConfig("127.0.0.1", 0, ("127.0.0.1",), ("http://dockyard.local",)),
            DockyardLocalReadService(lambda: _SNAPSHOT),
            gateway,
            DockyardPairingAuthorizer(pairing, "CSRF-1", now=lambda: self.now),
            self.hub,
        )
        self.address = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def _request(self, snapshot: str = _SNAPSHOT) -> tuple[int, dict[str, object]]:
        payload = {"plan_id": "PLAN-1", "plan_digest": self.pending.plan_digest, "task_count": 1}
        payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1",
            "command_id": "CMD-APPROVE-1",
            "project_id": "PRJ-1",
            "command_type": "plan.approve",
            "idempotency_key": "IDEMP-APPROVE-1",
            "expected_snapshot_commit": snapshot,
            "expected_revision": self.pending.revision,
            "confirmation_id": "CONF-APPROVE-1",
            "payload_digest": hashlib.sha256(payload_bytes).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=2)
        connection.request("POST", "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1/approve", body, {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": "http://dockyard.local",
            "Authorization": f"Bearer {self.token}",
            "X-Dockyard-Device-Id": "DEV-1",
            "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": "IDEMP-APPROVE-1",
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_real_loopback_approval_reaches_pm_once_and_publishes_sse(self) -> None:
        status, first = self._request()
        self.assertEqual(status, 200)
        self.assertEqual(first["outcome"], "materialized")
        approved = self.pm.latest("PLAN-1")
        self.assertIsNotNone(approved)
        assert approved is not None
        self.assertEqual(approved.revision, self.pending.revision + 2)
        self.assertIs(approved.phase, DockyardPlanPhase.MATERIALIZED)
        self.assertTrue(approved.last_operation_id.startswith("MAT-"))
        self.assertTrue((self.root / "docs" / "pm" / "tasks" / "TC-001-r1-dockyard.md").is_file())
        self.assertEqual(len(self.hub.replay("PRJ-1", 0)), 1)
        self.assertEqual(self.hub.replay("PRJ-1", 0)[0].event_type, "approval.changed")
        self.now = "2026-08-03T02:00:00Z"
        replay_status, replay = self._request()
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["content_digest"], first["content_digest"])
        self.assertEqual(self.pm.latest("PLAN-1"), approved)
        self.assertEqual(len(self.hub.replay("PRJ-1", 0)), 1)

    def test_approved_crash_window_resumes_materialization(self) -> None:
        self.pm.approve(DockyardPlanApprovalRequest(
            self.pending.plan_id,
            self.pending.revision,
            self.pending.content_digest,
            self.pending.plan_digest,
            len(self.pending.tasks),
            _NOW,
            "CONF-APPROVE-1",
            "CMD-APPROVE-1",
        ))
        status, response = self._request()
        self.assertEqual(status, 200)
        self.assertEqual(response["outcome"], "materialized")
        latest = self.pm.latest("PLAN-1")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest.phase, DockyardPlanPhase.MATERIALIZED)
        self.assertTrue((self.root / "docs" / "pm" / "tasks" / "TC-001-r1-dockyard.md").is_file())

    def _remove_request(self, revision: int | None = None) -> tuple[int, dict[str, object]]:
        payload = {"project_id": "PRJ-1", "acknowledgement": "repository remains untouched"}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1", "command_id": "CMD-REMOVE-1",
            "project_id": "PRJ-1", "command_type": "project.remove", "idempotency_key": "IDEMP-REMOVE-1",
            "expected_snapshot_commit": _SNAPSHOT,
            "expected_revision": self.project.generation if revision is None else revision,
            "confirmation_id": "CONF-REMOVE-1", "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=2)
        connection.request("POST", "/api/dockyard/v1/projects/PRJ-1/remove", body, {
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Origin": "http://dockyard.local", "Authorization": f"Bearer {self.token}",
            "X-Dockyard-Device-Id": "DEV-1", "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": "IDEMP-REMOVE-1",
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_project_remove_is_soft_forward_only_and_byte_exact_replay(self) -> None:
        status, first = self._remove_request()
        self.assertEqual(status, 200)
        self.assertEqual(first["outcome"], "removal_pending")
        pending = self.registry.read("PRJ-1")
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertIs(pending.phase, DockyardProjectPhase.REMOVAL_PENDING)
        self.assertTrue(self.repository.is_dir())
        self.assertEqual((self.repository / "README.md").read_text(encoding="utf-8"), "kept\n")
        self.now = "2026-08-03T03:00:00Z"
        replay_status, replay = self._remove_request()
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["content_digest"], first["content_digest"])
        self.assertEqual(self.registry.read("PRJ-1"), pending)

    def test_project_remove_rejects_stale_generation_before_write(self) -> None:
        status, response = self._remove_request(self.project.generation + 1)
        self.assertEqual(status, 409)
        self.assertEqual(response["error_code"], "REVISION_STALE")
        self.assertEqual(self.registry.read("PRJ-1"), self.project)

    def test_stale_snapshot_is_rejected_before_pm_write(self) -> None:
        status, response = self._request("b" * 40)
        self.assertEqual(status, 409)
        self.assertEqual(response["error_code"], "SNAPSHOT_STALE")
        self.assertEqual(self.pm.latest("PLAN-1"), self.pending)

    def test_unwired_lifecycle_command_is_explicitly_not_ready(self) -> None:
        # The Control API must not pretend that a retry owner exists before it is composed.
        payload = {"attempt": 2}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1", "command_id": "CMD-RETRY-1",
            "project_id": "PRJ-1", "command_type": "task.retry", "idempotency_key": "IDEMP-RETRY-1",
            "expected_snapshot_commit": _SNAPSHOT, "expected_revision": 1,
            "confirmation_id": "CONF-RETRY-1", "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=2)
        connection.request("POST", "/api/dockyard/v1/projects/PRJ-1/tasks/TC-001/retry", body, {
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Origin": "http://dockyard.local", "Authorization": f"Bearer {self.token}",
            "X-Dockyard-Device-Id": "DEV-1", "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": "IDEMP-RETRY-1",
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(result["error_code"], "COMMAND_OWNER_UNAVAILABLE")
