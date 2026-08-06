"""Directed live operational projection wiring tests for TC-13.29l.11a."""

from __future__ import annotations

import http.client
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_local_runtime import DockyardLocalRuntime  # noqa: E402
from tests import test_dockyard_local_runtime as fixtures  # noqa: E402
from tests import test_dockyard_post_admission_worker as worker_fixtures  # noqa: E402


class DockyardLiveOperationalProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.DockyardLocalRuntimeTests(
            "test_runtime_starts_real_loopback_and_approves_through_production_owner"
        )
        self.fixture.setUp()
        self.runtime = DockyardLocalRuntime(self.fixture.config)
        self.result = self.runtime.start()

    def tearDown(self) -> None:
        self.runtime.stop()
        self.fixture.tearDown()

    def _get(self, suffix: str) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(
            self.result.address.host,
            self.result.address.port,
            timeout=10,
        )
        connection.request(
            "GET",
            f"/api/dockyard/v1/projects/PRJ-1/{suffix}",
            headers={"Accept": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_frozen_read_endpoints_use_one_real_empty_snapshot(self) -> None:
        overview_status, overview = self._get("overview")
        self.assertEqual(overview_status, 200, overview)
        self.assertEqual(overview["schema_version"], "dockyard.operational-overview/v1")
        self.assertEqual(overview["snapshot_commit"], self.fixture.head)
        self.assertEqual(len(overview["overview"]["snapshot_commit"]), 64)
        self.assertEqual(overview["overview"]["state_counts"], [])
        self.assertEqual(overview["overview"]["provider_summaries"], [])
        self.assertEqual(overview["plan_approval"]["plan_id"], "PLAN-1")

        expected = {
            "tasks": ("dockyard.task-list/v1", "tasks"),
            "runs": ("dockyard.run-list/v1", "runs"),
            "approvals": ("dockyard.approval-list/v1", "approvals"),
            "providers": ("dockyard.provider-list/v1", "providers"),
        }
        for endpoint, (schema, collection) in expected.items():
            with self.subTest(endpoint=endpoint):
                status, body = self._get(endpoint)
                self.assertEqual(status, 200, body)
                self.assertEqual(body["schema_version"], schema)
                self.assertEqual(body["snapshot_commit"], self.fixture.head)
                self.assertEqual(body[collection], [])
        tasks_status, tasks = self._get("tasks")
        self.assertEqual(tasks_status, 200, tasks)
        self.assertEqual(tasks["reviews"], [])

    def test_missing_task_is_typed_and_all_responses_exclude_local_secrets(self) -> None:
        status, missing = self._get("tasks/TC-MISSING")
        self.assertEqual(status, 404, missing)
        self.assertEqual(missing["reason_code"], "TASK_NOT_FOUND")
        observed = [missing]
        for endpoint in ("overview", "tasks", "runs", "approvals", "providers"):
            item_status, body = self._get(endpoint)
            self.assertEqual(item_status, 200, body)
            observed.append(body)
        encoded = json.dumps(observed, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            str(self.fixture.project_root),
            self.fixture.device_token,
            "CSRF-1",
            "DEEPSEEK_API_KEY",
            "stdout",
            "stderr",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_configured_provider_remains_unknown_without_decoder_capability_evidence(self) -> None:
        self.runtime.stop()
        self.runtime = DockyardLocalRuntime(
            self.fixture.config,
            {"claude": worker_fixtures._WorkspaceLocalProvider()},
            (("claude", "2.1.214"),),
        )
        self.result = self.runtime.start()
        status, body = self._get("providers")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["providers"]), 1)
        provider = body["providers"][0]
        self.assertEqual(provider["provider_id"], "claude")
        self.assertEqual(provider["model_id"], "test-basic")
        self.assertEqual(provider["health"], "UNKNOWN")
        self.assertEqual(provider["reason_code"], "EVIDENCE_UNKNOWN")
        self.assertEqual(provider["executable_version"], "2.1.214")
        self.assertIsNone(provider["decoder_version"])
        self.assertEqual(provider["binding_id"], "basic")
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(self.fixture.device_token, encoded)
        self.assertNotIn(str(self.fixture.project_root), encoded)


if __name__ == "__main__":
    unittest.main()
