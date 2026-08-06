"""Directed requirement-to-PM-plan composition tests for TC-13.29l.7."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from dockyard_composition import (  # noqa: E402
    DockyardCompositionGateway,
    DockyardPairingAuthorizer,
    DockyardPlanReadService,
    _default_task,
)
from dockyard_control_api import DockyardApiConfig, DockyardControlServer  # noqa: E402
from dockyard_pairing import (  # noqa: E402
    DockyardDeviceScope,
    DockyardPairingConsumeRequest,
    DockyardPairingIssueRequest,
    DockyardPairingStore,
)
from dockyard_plan_store import DockyardPlanPhase, DockyardPlanStore  # noqa: E402
from dockyard_pm_service import DockyardPmService  # noqa: E402
from dockyard_project_registry import (  # noqa: E402
    DockyardProjectRegistry,
    DockyardRegisterProjectRequest,
)
from dockyard_sse import DockyardSseHub  # noqa: E402
from dockyard_web_runtime import (  # noqa: E402
    DockyardBrowserBootstrap,
    DockyardWebRuntime,
    DockyardWebRuntimeConfig,
)
from worktree_lifecycle_store import expected_branch  # noqa: E402
sys.path.pop(0)

SNAPSHOT = "a" * 40
STAMP = "2026-08-03T08:00:00Z"


class DockyardRequirementCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        subprocess.run(("git", "init", "--quiet", str(self.repository)), check=True)
        self.registry = DockyardProjectRegistry(self.root / ".agentdesk/runtime/projects")
        self.project = self.registry.register(DockyardRegisterProjectRequest(
            "PRJ-1", "Dockyard", str(self.repository), "OP-REGISTER", STAMP,
        )).record
        self.pm = DockyardPmService(
            DockyardPlanStore(self.root / ".agentdesk/runtime/plans"),
            self.root / "docs/pm/tasks",
        )
        pairing = DockyardPairingStore(self.root / ".agentdesk/runtime/pairing")
        issued = pairing.issue(DockyardPairingIssueRequest(
            "PAIR-1", STAMP, "2026-08-03T08:10:00Z", 3, "OP-ISSUE",
        ))
        paired = pairing.consume(DockyardPairingConsumeRequest(
            "PAIR-1", issued.pairing_code, "DEV-1", "Dockyard browser",
            (DockyardDeviceScope.APPROVE,), STAMP, "OP-CONSUME",
        ))
        self.token = paired.device_token
        self.hub = DockyardSseHub(max_backlog=16, max_streams=2)
        web_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        web_probe.bind(("127.0.0.1", 0))
        self.web_port = web_probe.getsockname()[1]
        web_probe.close()
        self.web_origin = f"http://127.0.0.1:{self.web_port}"
        gateway = DockyardCompositionGateway(
            self.pm, lambda: SNAPSHOT, self.hub, self.registry,
            plan_id="PLAN-1", now=lambda: STAMP,
        )
        read_service = DockyardPlanReadService(
            lambda: SNAPSHOT, self.registry, self.pm, "PRJ-1", "PLAN-1",
        )
        self.server = DockyardControlServer(
            DockyardApiConfig(
                "127.0.0.1", 0, ("127.0.0.1",),
                ("http://dockyard.local", self.web_origin),
            ),
            read_service,
            gateway,
            DockyardPairingAuthorizer(pairing, "CSRF-1", now=lambda: STAMP),
            self.hub,
        )
        self.address = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def _read(self, suffix: str) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=3)
        path = "/api/dockyard/v1/projects/PRJ-1"
        if suffix:
            path += "/" + suffix
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def _command(
        self,
        command_type: str,
        endpoint: str,
        method: str,
        payload: dict[str, object],
        revision: int,
        command_id: str,
        snapshot: str = SNAPSHOT,
    ) -> tuple[int, dict[str, object]]:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        idempotency = "IDEMP-" + command_id
        envelope = {
            "schema_version": "dockyard.command/v1",
            "command_id": command_id,
            "project_id": "PRJ-1",
            "command_type": command_type,
            "idempotency_key": idempotency,
            "expected_snapshot_commit": snapshot,
            "expected_revision": revision,
            "confirmation_id": None,
            "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=3)
        connection.request(method, endpoint, body, {
            "Content-Type": "application/json",
            "Origin": "http://dockyard.local",
            "Authorization": f"Bearer {self.token}",
            "X-Dockyard-Device-Id": "DEV-1",
            "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": idempotency,
        })
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def _create(self, *, command_id: str = "CMD-CREATE-1", requirement: str = "实现真实需求入口"):
        return self._command(
            "plan.create",
            "/api/dockyard/v1/projects/PRJ-1/plans",
            "POST",
            {"plan_id": "PLAN-1", "requirement": requirement},
            self.project.generation,
            command_id,
        )

    def test_real_control_api_creates_pm_owned_draft_and_safe_projection(self) -> None:
        project_status, project = self._read("")
        self.assertEqual(project_status, 200)
        self.assertEqual(project["plan_id"], "PLAN-1")
        status, receipt = self._create()
        self.assertEqual(status, 200)
        self.assertEqual(receipt["outcome"], "drafted")
        plan = self.pm.latest("PLAN-1")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIs(plan.phase, DockyardPlanPhase.DRAFTED)
        self.assertEqual(plan.pm_owner_id, "PM-DOCKYARD")
        self.assertEqual(plan.tasks[0].base_commit, SNAPSHOT)
        self.assertFalse(any((self.root / "docs/pm/tasks").glob("*.md")))
        projection_status, projection = self._read("plans/PLAN-1")
        self.assertEqual(projection_status, 200)
        self.assertEqual(projection["schema_version"], "dockyard.plan-projection/v1")
        self.assertEqual(projection["tasks"][0]["task_id"], plan.tasks[0].task_id)
        self.assertNotIn("base_commit", projection["tasks"][0])

    def test_default_task_identity_is_canonical_stable_and_plan_bound(self) -> None:
        first = _default_task("PLAN-1", "实现真实需求入口", SNAPSHOT)
        replay = _default_task("PLAN-1", "替换需求不改变计划身份", SNAPSHOT)
        other = _default_task("PLAN-2", "实现真实需求入口", SNAPSHOT)
        self.assertEqual(first.task_id, replay.task_id)
        self.assertNotEqual(first.task_id, other.task_id)
        self.assertIsNotNone(re.fullmatch(r"TC-[0-9]{20}", first.task_id))
        self.assertEqual(
            expected_branch(first.task_id, 1, 1, "DSP-TEST-001"),
            f"agentdesk/{first.task_id}/r1/a1/DSP-TEST-001",
        )

    def test_edit_submission_advances_to_approval_pending_without_dispatch(self) -> None:
        self.assertEqual(self._create()[0], 200)
        _, projection = self._read("plans/PLAN-1")
        task = dict(projection["tasks"][0])
        task["title"] = "实现并验证真实需求入口"
        task["provider_id"] = "reasonix"
        task["model_id"] = "deepseek-v4-flash"
        payload = {
            "plan_id": "PLAN-1",
            "requirement": projection["requirement"],
            "tasks": [task],
            "submit_for_approval": True,
        }
        status, receipt = self._command(
            "plan.update",
            "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1",
            "PATCH",
            payload,
            int(projection["revision"]),
            "CMD-UPDATE-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(receipt["outcome"], "approval_pending")
        latest = self.pm.latest("PLAN-1")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIs(latest.phase, DockyardPlanPhase.APPROVAL_PENDING)
        self.assertEqual(latest.tasks[0].provider_id, "reasonix")
        self.assertFalse(any((self.root / "docs/pm/tasks").glob("*.md")))
        approval_status, approval = self._read("overview")
        self.assertEqual(approval_status, 200)
        self.assertEqual(approval["phase"], "APPROVAL_PENDING")

    def test_create_replay_is_byte_exact_and_divergence_is_rejected(self) -> None:
        first_status, first = self._create()
        replay_status, replay = self._create()
        self.assertEqual((first_status, replay_status), (200, 200))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["content_digest"], first["content_digest"])
        divergent_status, divergent = self._create(requirement="替换后的恶意需求")
        self.assertEqual(divergent_status, 409)
        self.assertEqual(divergent["error_code"], "PLAN_CONFLICT")
        self.assertEqual(self.pm.latest("PLAN-1").requirement, "实现真实需求入口")

    def test_stale_snapshot_rejects_before_plan_write(self) -> None:
        status, result = self._command(
            "plan.create",
            "/api/dockyard/v1/projects/PRJ-1/plans",
            "POST",
            {"plan_id": "PLAN-1", "requirement": "不得写入"},
            self.project.generation,
            "CMD-STALE-1",
            "b" * 40,
        )
        self.assertEqual(status, 409)
        self.assertEqual(result["error_code"], "SNAPSHOT_STALE")
        self.assertIsNone(self.pm.latest("PLAN-1"))

    def test_plan_identity_substitution_is_rejected_before_pm_write(self) -> None:
        status, result = self._command(
            "plan.create",
            "/api/dockyard/v1/projects/PRJ-1/plans",
            "POST",
            {"plan_id": "PLAN-EVIL", "requirement": "替换计划身份"},
            self.project.generation,
            "CMD-EVIL-1",
        )
        self.assertEqual(status, 409)
        self.assertEqual(result["error_code"], "PLAN_ID_DIVERGENCE")
        self.assertIsNone(self.pm.latest("PLAN-EVIL"))

    def test_exact_loopback_origin_receives_bounded_cors_and_preflight(self) -> None:
        origin = "http://dockyard.local"
        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=3)
        connection.request("GET", "/api/dockyard/v1/projects/PRJ-1", headers={"Origin": origin})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin)
        connection.close()

        connection = http.client.HTTPConnection(self.address.host, self.address.port, timeout=3)
        connection.request("OPTIONS", "/api/dockyard/v1/projects/PRJ-1/plans", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization, content-type, x-dockyard-device-id, "
                "x-dockyard-csrf, x-dockyard-idempotency-key"
            ),
        })
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin)
        self.assertIn("PATCH", response.getheader("Access-Control-Allow-Methods"))
        connection.close()

    def test_same_origin_web_shell_proxies_plan_create_to_control_owner(self) -> None:
        assets = self.root / "web-dist"
        assets.mkdir()
        (assets / "index.html").write_text("<!doctype html><div id='root'></div>\n", encoding="utf-8")
        web_port = self.web_port
        origin = self.web_origin
        runtime = DockyardWebRuntime(DockyardWebRuntimeConfig(
            assets,
            DockyardBrowserBootstrap(origin, "PRJ-1", "DEV-1", self.token, "CSRF-1"),
            "127.0.0.1",
            web_port,
            f"http://{self.address.host}:{self.address.port}",
        ))
        runtime.start()
        try:
            payload = {"plan_id": "PLAN-1", "requirement": "通过同源 Web shell 创建草案"}
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            envelope = {
                "schema_version": "dockyard.command/v1",
                "command_id": "CMD-PROXY-1",
                "project_id": "PRJ-1",
                "command_type": "plan.create",
                "idempotency_key": "IDEMP-PROXY-1",
                "expected_snapshot_commit": SNAPSHOT,
                "expected_revision": self.project.generation,
                "confirmation_id": None,
                "payload_digest": hashlib.sha256(raw).hexdigest(),
            }
            body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
            connection.request("POST", "/api/dockyard/v1/projects/PRJ-1/plans", body, {
                "Content-Type": "application/json",
                "Origin": origin,
                "Authorization": f"Bearer {self.token}",
                "X-Dockyard-Device-Id": "DEV-1",
                "X-Dockyard-CSRF": "CSRF-1",
                "X-Dockyard-Idempotency-Key": "IDEMP-PROXY-1",
            })
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(result["outcome"], "drafted")
            self.assertEqual(self.pm.latest("PLAN-1").requirement, payload["requirement"])
        finally:
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
