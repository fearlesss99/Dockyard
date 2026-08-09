"""Directed TC-13.29l.2 local runtime bootstrap tests."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from dockyard_local_runtime import (  # noqa: E402
    DockyardLocalRuntime,
    DockyardLocalRuntimeConfig,
    DockyardLocalRuntimeConflictError,
    DockyardLocalRuntimeInputError,
    DockyardLocalRuntimePreconditionError,
    _git_head,
)
from dockyard_pairing import (  # noqa: E402
    DockyardDeviceScope,
    DockyardPairingConsumeRequest,
    DockyardPairingIssueRequest,
    DockyardPairingStore,
)
from dockyard_plan_store import DockyardPlanStore, DockyardPlanTask  # noqa: E402
from pm_materialization_admission_handoff import MaterializationAdmissionConflictError  # noqa: E402
from portfolio_scheduler_worker_handoff_store import PortfolioSchedulerWorkerHandoffNotFoundError  # noqa: E402
from dockyard_task_admission_composition import DockyardAdmissionCompositionRecoveryRequired  # noqa: E402
from dockyard_pm_service import (  # noqa: E402
    DockyardPlanApprovalRequest,
    DockyardPlanDraftRequest,
    DockyardPlanTransitionRequest,
    DockyardPmService,
)
from dockyard_project_registry import (  # noqa: E402
    DockyardProjectRegistry,
    DockyardRegisterProjectRequest,
)
from portfolio_scheduler_store import BusinessPriority  # noqa: E402
from state_provider import StateProvider  # noqa: E402
from worker_slot_lease import read_worker_slot_leases  # noqa: E402
sys.path.pop(0)

_NOW = "2026-08-03T04:00:00Z"
_PAIR_CREATED = "2020-01-01T00:00:00Z"
_PAIR_CONSUMED = "2020-01-01T00:01:00Z"
_PAIR_EXPIRES = "2020-01-01T00:05:00Z"


class DockyardLocalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project_root = root / "project"
        self.project_root.mkdir()
        subprocess.run(("git", "init", "--quiet", str(self.project_root)), check=True)
        subprocess.run(("git", "-C", str(self.project_root), "config", "user.email", "dockyard@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(self.project_root), "config", "user.name", "Dockyard Test"), check=True)
        (self.project_root / "README.md").write_text("runtime\n", encoding="utf-8")
        policy = self.project_root / "docs" / "pm" / "ROLE-POLICIES.yaml"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({
            "schema_version": "agentdesk.role-policies/v1",
            "tier_order": ["basic", "standard", "advanced", "expert"],
            "deliberation_tier_order": ["efficient", "balanced", "deep"],
            "risk_floors": {"L0": "basic", "L1": "standard", "L2": "advanced", "L3": "expert", "L4": "expert"},
            "roles": {"R1": {"default_tier": "basic", "minimum_tier": "basic", "deliberation_tier": "efficient", "required_capabilities": ["coding"], "degradation_policy": "block"}},
        }) + "\n", encoding="utf-8")
        state = self.project_root / "docs" / "pm" / "state" / "tasks.yaml"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({
            "schema_version": "agentdesk.tasks/v2", "project_id": "PRJ-1",
            "adoption_level": "standard", "updated_at": _NOW,
            "pm_control": {"holder_id": "PM-1", "lease_epoch": 1, "mode": "manual"},
            "tasks": [],
        }) + "\n", encoding="utf-8")
        for relative in ("docs/pm/events", "docs/pm/outbox", "docs/pm/acceptances"):
            directory = self.project_root / relative
            directory.mkdir(parents=True)
            (directory / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.project_root), "add", "-A"), check=True)
        subprocess.run(("git", "-C", str(self.project_root), "commit", "--quiet", "-m", "test"), check=True)
        self.head = subprocess.run(
            ("git", "-C", str(self.project_root), "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()

        bindings = self.project_root / ".agentdesk" / "runtime" / "model-bindings.yaml"
        bindings.parent.mkdir(parents=True)
        bindings.write_text(json.dumps({
            "schema_version": "agentdesk.model-bindings/v2", "updated_at": _NOW,
            "bindings": {"basic": {"provider": "claude", "model_id": "test-basic", "tier": "expert", "deliberation_tier": "deep", "context_window_tokens": 64000, "capabilities": ["coding", "testing"], "enabled": True}},
        }) + "\n", encoding="utf-8")

        self.registry_root = root / "registry"
        self.pairing_root = root / "pairing"
        self.plan_root = root / "plans"
        self.cards_root = self.project_root / "docs" / "pm" / "tasks"
        for directory in (self.registry_root, self.pairing_root, self.plan_root, self.cards_root):
            directory.mkdir(parents=True, exist_ok=True)
        registry = DockyardProjectRegistry(self.registry_root)
        registry.register(DockyardRegisterProjectRequest(
            "PRJ-1", "Dockyard", str(self.project_root), "OP-REGISTER", _NOW,
        ))
        pairing = DockyardPairingStore(self.pairing_root)
        issued = pairing.issue(DockyardPairingIssueRequest(
            "PAIR-1", _PAIR_CREATED, _PAIR_EXPIRES, 3, "OP-ISSUE",
        ))
        consumed = pairing.consume(DockyardPairingConsumeRequest(
            "PAIR-1", issued.pairing_code, "DEV-1", "Browser",
            (DockyardDeviceScope.APPROVE, DockyardDeviceScope.RETRY),
            _PAIR_CONSUMED,
            "OP-CONSUME",
        ))
        self.device_token = consumed.device_token

        pm = DockyardPmService(DockyardPlanStore(self.plan_root), self.cards_root)
        task = DockyardPlanTask(
            "TC-001", "Runtime", "Exercise runtime.", (), "parallel", "R1",
            "implementation", "claude", "test-basic", "standard", 8000, 3, self.head,
            BusinessPriority.P1,
            ("scope.bounded", "clarity.explicit", "concurrency.single_writer",
             "contract.internal", "impact.informational", "rollback.simple",
             "dependency.none"),
            None, None, None, "L0", ("coding",), None, 0, 1,
        )
        draft = pm.draft(DockyardPlanDraftRequest(
            "PLAN-1", "PRJ-1", "Runtime plan", (task,), "PM-1", _NOW, "OP-DRAFT",
        ))
        self.pending = pm.request_approval(DockyardPlanTransitionRequest(
            draft.plan_id, draft.revision, draft.content_digest,
            "2026-08-03T04:01:00Z", "OP-PENDING",
        ))
        self.pm = pm
        self.config = DockyardLocalRuntimeConfig(
            "RUNTIME-1", "PRJ-1", "PLAN-1", self.project_root, self.registry_root,
            self.pairing_root, self.plan_root, self.cards_root,
            "DEV-1", self.device_token, "CSRF-1",
            "http://127.0.0.1:5173", "127.0.0.1", 0, _NOW,
        )
        self.runtimes: list[DockyardLocalRuntime] = []

    def test_git_head_ignores_inherited_repository_scope(self) -> None:
        hostile_environment = {
            "GIT_DIR": str(self.project_root / "missing-git-dir"),
            "GIT_WORK_TREE": str(self.project_root / "missing-work-tree"),
            "GIT_INDEX_FILE": str(self.project_root / "missing-index"),
            "GIT_COMMON_DIR": str(self.project_root / "missing-common-dir"),
        }
        with mock.patch.dict(os.environ, hostile_environment, clear=False):
            self.assertEqual(_git_head(self.project_root), self.head)

    def tearDown(self) -> None:
        for runtime in reversed(self.runtimes):
            runtime.stop()
        self.temp.cleanup()

    def _runtime(self, config: DockyardLocalRuntimeConfig | None = None) -> DockyardLocalRuntime:
        runtime = DockyardLocalRuntime(self.config if config is None else config)
        self.runtimes.append(runtime)
        return runtime

    def test_runtime_starts_real_loopback_and_approves_through_production_owner(self) -> None:
        """A runtime without ready providers rejects approval before dispatch."""
        runtime = self._runtime()
        result = runtime.start()
        payload = {"plan_id": "PLAN-1", "plan_digest": self.pending.plan_digest, "task_count": 1}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1", "command_id": "CMD-APPROVE-1",
            "project_id": "PRJ-1", "command_type": "plan.approve", "idempotency_key": "IDEMP-1",
            "expected_snapshot_commit": self.head, "expected_revision": self.pending.revision,
            "confirmation_id": "CONF-1", "payload_digest": hashlib.sha256(raw).hexdigest(),
        }
        body = json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")
        connection = http.client.HTTPConnection(result.address.host, result.address.port, timeout=20)
        connection.request("POST", "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-1/approve", body, {
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Origin": "http://127.0.0.1:5173", "Authorization": f"Bearer {self.device_token}",
            "X-Dockyard-Device-Id": "DEV-1", "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": "IDEMP-1",
        })
        response = connection.getresponse()
        response_body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(response_body["error_code"], "PROVIDER_NOT_READY")
        latest = self.pm.latest("PLAN-1")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.phase.value, "APPROVAL_PENDING")
        self.assertFalse((self.cards_root / "TC-001-r1-dockyard.md").exists())
        snapshot = StateProvider(self.project_root).snapshot()
        self.assertEqual(snapshot.events, ())
        self.assertEqual(snapshot.tasks, ())
        self.assertEqual(len(tuple((self.project_root / "docs/pm/approvals").glob("*.yaml"))), 0)
        leases = read_worker_slot_leases(self.project_root)
        self.assertEqual(len(leases["leases"]), 0)
        progress = tuple((self.project_root / ".agentdesk/runtime/dockyard-admission/progress").glob("*.yaml"))
        self.assertEqual(progress, ())

    def test_start_is_byte_exact_replay_and_second_owner_is_rejected(self) -> None:
        first = self._runtime()
        first_result = first.start()
        self.assertEqual(first.start(), first_result)
        second = self._runtime()
        with self.assertRaises(DockyardLocalRuntimeConflictError):
            second.start()

    def test_stranded_materialized_plan_does_not_prevent_loopback_start(self) -> None:
        stranded = SimpleNamespace(plan_id="PLAN-1")
        with mock.patch.object(DockyardPlanStore, "current_session", return_value=stranded), mock.patch(
            "dockyard_local_runtime._resume_current_materialized_plan",
            side_effect=MaterializationAdmissionConflictError("handoff:dirty_worktree"),
        ):
            result = self._runtime().start()
        connection = http.client.HTTPConnection(result.address.host, result.address.port, timeout=2)
        connection.request("GET", "/api/dockyard/v1/health")
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "ready")

    def test_missing_stranded_handoff_does_not_prevent_loopback_start(self) -> None:
        stranded = SimpleNamespace(plan_id="PLAN-1")
        with mock.patch.object(DockyardPlanStore, "current_session", return_value=stranded), mock.patch(
            "dockyard_local_runtime._resume_current_materialized_plan",
            side_effect=PortfolioSchedulerWorkerHandoffNotFoundError(
                "portfolio_scheduler_worker_handoff:handoff_pair"
            ),
        ):
            result = self._runtime().start()
        connection = http.client.HTTPConnection(result.address.host, result.address.port, timeout=2)
        connection.request("GET", "/api/dockyard/v1/health")
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "ready")

    def test_stranded_admission_recovery_does_not_prevent_loopback_start(self) -> None:
        stranded = SimpleNamespace(plan_id="PLAN-1")
        with mock.patch.object(DockyardPlanStore, "current_session", return_value=stranded), mock.patch(
            "dockyard_local_runtime._resume_current_materialized_plan",
            side_effect=DockyardAdmissionCompositionRecoveryRequired(
                "composition:handoff_recovery_required"
            ),
        ):
            result = self._runtime().start()
        connection = http.client.HTTPConnection(result.address.host, result.address.port, timeout=2)
        connection.request("GET", "/api/dockyard/v1/health")
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "ready")

    def test_missing_project_and_device_fail_before_listener(self) -> None:
        empty_registry = Path(self.temp.name) / "empty-registry"
        empty_registry.mkdir()
        missing_project = DockyardLocalRuntimeConfig(
            "RUNTIME-MISSING", "PRJ-MISSING", "PLAN-1", self.project_root, empty_registry,
            self.pairing_root, self.plan_root, self.cards_root,
            "DEV-1", self.device_token, "CSRF-1", started_at=_NOW,
        )
        with self.assertRaises(DockyardLocalRuntimePreconditionError):
            self._runtime(missing_project).start()

        missing_device_root = Path(self.temp.name) / "empty-pairing"
        missing_device_root.mkdir()
        missing_device = DockyardLocalRuntimeConfig(
            "RUNTIME-MISSING-DEVICE", "PRJ-1", "PLAN-1", self.project_root, self.registry_root,
            missing_device_root, self.plan_root, self.cards_root,
            "DEV-MISSING", "TOKEN-MISSING", "CSRF-1", started_at=_NOW,
        )
        with self.assertRaises(DockyardLocalRuntimePreconditionError):
            self._runtime(missing_device).start()

    def test_wrong_device_token_fails_before_listener_and_pairing_write(self) -> None:
        store = DockyardPairingStore(self.pairing_root)
        before = store.read_device("DEV-1")
        wrong_token = DockyardLocalRuntimeConfig(
            "RUNTIME-WRONG-TOKEN", "PRJ-1", "PLAN-1", self.project_root, self.registry_root,
            self.pairing_root, self.plan_root, self.cards_root,
            "DEV-1", "WRONG-TOKEN", "CSRF-1", started_at=_NOW,
        )
        with self.assertRaises(DockyardLocalRuntimePreconditionError):
            self._runtime(wrong_token).start()
        self.assertEqual(store.read_device("DEV-1"), before)

    def test_malformed_non_loopback_config_is_typed(self) -> None:
        with self.assertRaises(DockyardLocalRuntimeInputError):
            DockyardLocalRuntimeConfig(
                "RUNTIME-BAD", "PRJ-1", "PLAN-1", self.project_root, self.registry_root,
                self.pairing_root, self.plan_root, self.cards_root,
                "DEV-1", self.device_token, "CSRF-1", "https://example.invalid", started_at=_NOW,
            )

    def test_bootstrap_is_runtime_only_sanitized_and_matches_bound_address(self) -> None:
        result = self._runtime().start()
        value = result.browser_bootstrap.as_window_value()
        self.assertEqual(value["apiBaseUrl"], f"http://{result.address.host}:{result.address.port}")
        self.assertEqual(value["projectId"], "PRJ-1")
        self.assertEqual(value["deviceId"], "DEV-1")
        script = result.browser_bootstrap.script_bytes()
        self.assertIn(b"window.__DOCKYARD_LOOPBACK__=", script)
        self.assertNotIn(str(self.project_root).encode("utf-8"), script)
        for forbidden in (b"DEEPSEEK_API_KEY", b"prompt", b"stdout", b"stderr", b"session"):
            self.assertNotIn(forbidden, script)
        self.assertNotIn(self.device_token, repr(result))
        self.assertNotIn("CSRF-1", repr(result))

    def test_overview_exposes_exact_pending_plan_evidence_and_content_etag(self) -> None:
        result = self._runtime().start()
        connection = http.client.HTTPConnection(result.address.host, result.address.port, timeout=2)
        connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/overview", headers={
            "Origin": "http://127.0.0.1:5173",
        })
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        projection = json.loads(raw)
        self.assertEqual(response.status, 200)
        self.assertEqual(set(projection), {
            "schema_version", "project_id", "snapshot_commit", "overview",
            "plan_approval", "content_digest",
        })
        self.assertEqual(projection["project_id"], "PRJ-1")
        self.assertEqual(projection["snapshot_commit"], self.head)
        approval = projection["plan_approval"]
        self.assertEqual(approval["plan_id"], "PLAN-1")
        self.assertEqual(approval["revision"], self.pending.revision)
        self.assertEqual(approval["plan_digest"], self.pending.plan_digest)
        self.assertEqual(approval["task_count"], 1)
        self.assertEqual(response.getheader("ETag"), f'"{projection["content_digest"]}"')
        self.assertNotIn(str(self.project_root).encode("utf-8"), raw)

    def test_non_pending_plan_keeps_operational_overview_without_approval(self) -> None:
        self.pm.approve(DockyardPlanApprovalRequest(
            self.pending.plan_id,
            self.pending.revision,
            self.pending.content_digest,
            self.pending.plan_digest,
            len(self.pending.tasks),
            "2026-08-03T04:02:00Z",
            "CONF-DIRECT",
            "OP-DIRECT-APPROVE",
        ))
        result = self._runtime().start()
        connection = http.client.HTTPConnection(result.address.host, result.address.port, timeout=2)
        connection.request("GET", "/api/dockyard/v1/projects/PRJ-1/overview")
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["schema_version"], "dockyard.operational-overview/v1")
        self.assertIsNone(body["plan_approval"])
        self.assertEqual(body["overview"]["project_id"], "PRJ-1")

    def test_stop_is_idempotent_and_releases_listener(self) -> None:
        runtime = self._runtime()
        result = runtime.start()
        runtime.stop()
        runtime.stop()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            self.assertNotEqual(probe.connect_ex((result.address.host, result.address.port)), 0)
        finally:
            probe.close()

    def test_explicit_web_assets_start_in_memory_browser_shell(self) -> None:
        assets = Path(self.temp.name) / "web-dist"
        assets.mkdir()
        (assets / "index.html").write_text(
            "<!doctype html><div id='root'></div>"
            "<script src='/dockyard-runtime.js'></script>\n",
            encoding="utf-8",
        )
        port_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_probe.bind(("127.0.0.1", 0))
        web_port = port_probe.getsockname()[1]
        port_probe.close()
        config = replace(
            self.config,
            browser_origin=f"http://127.0.0.1:{web_port}",
            web_assets_root=assets,
        )
        result = self._runtime(config).start()
        self.assertEqual(result.browser_url, config.browser_origin + "/")
        self.assertEqual(result.browser_bootstrap.api_base_url, config.browser_origin)
        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
        connection.request("GET", "/dockyard-runtime.js")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(body, result.browser_bootstrap.script_bytes())
        self.assertFalse((assets / "dockyard-runtime.js").exists())
        connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
        connection.request("GET", "/api/dockyard/v1/health")
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "ready")


if __name__ == "__main__":
    unittest.main()
