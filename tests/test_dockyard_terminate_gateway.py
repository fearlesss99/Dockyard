from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_active_execution import (  # noqa: E402
    DockyardActiveCancellationOutcome,
    DockyardActiveExecutionRegistry,
    make_active_execution_identity,
)
from dockyard_composition import DockyardCompositionGateway  # noqa: E402
from dockyard_control_api import (  # noqa: E402
    DockyardCommandEnvelope,
    DockyardCommandRejected,
    DockyardCommandRequest,
)
from dockyard_plan_store import DockyardPlanStore  # noqa: E402
from dockyard_pm_service import DockyardPmService  # noqa: E402
from dockyard_project_registry import DockyardProjectRegistry  # noqa: E402
from dockyard_sse import DockyardSseHub  # noqa: E402


class DockyardTerminateGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = DockyardActiveExecutionRegistry()
        self.pm = DockyardPmService(
            DockyardPlanStore(self.root / "plans"), self.root / "cards",
        )
        self.gateway = DockyardCompositionGateway(
            self.pm,
            lambda: "a" * 40,
            DockyardSseHub(),
            DockyardProjectRegistry(self.root / "projects"),
            project_root=self.root,
            active_registry=self.registry,
            now=lambda: "2026-08-04T00:00:00Z",
        )
        self.task = SimpleNamespace(
            task_id="TC-001", revision=1, state="in_progress", attempt=2,
            current_dispatch=SimpleNamespace(dispatch_id="DSP-001"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request(self, payload: dict[str, object], command_id: str = "CMD-TERM-1") -> DockyardCommandRequest:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        envelope = DockyardCommandEnvelope(
            "dockyard.command/v1", command_id, "PRJ-1", "dispatch.terminate",
            "IDEMP-" + command_id, "a" * 40, 1, "CONF-TERM-1",
            hashlib.sha256(raw).hexdigest(),
        )
        return DockyardCommandRequest(envelope, "DSP-001", raw)

    def _payload(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": "TC-001", "attempt": 2, "dispatch_id": "DSP-001",
            "generation_id": "GEN-001", "event_id": "EVT-CANCEL-1",
        }
        payload.update(changes)
        return payload

    def _register(self, calls: list[object] | None = None) -> None:
        identity = make_active_execution_identity(
            "TC-001", 1, 2, "DSP-001", "GEN-001", "2026-08-04T00:00:00Z",
        )
        def callback(command: object) -> DockyardActiveCancellationOutcome:
            if calls is not None:
                calls.append(command)
            return DockyardActiveCancellationOutcome(
                "TC-001", "DSP-001", "EVT-CANCEL-1", "2026-08-04T00:00:02Z",
            )
        self.registry.register(identity, callback)

    def test_success_publishes_cancelled_receipt_and_replay(self) -> None:
        calls: list[object] = []
        self._register(calls)
        request = self._request(self._payload())
        with patch("dockyard_composition.StateProvider") as state_provider:
            state_provider.return_value.snapshot.return_value = SimpleNamespace(tasks=(self.task,), events=())
            first = self.gateway.execute(request)
            replay = self.gateway.execute(request)
        self.assertEqual(first.outcome, "cancelled")
        self.assertEqual(first.canonical_event_id, "EVT-CANCEL-1")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.content_digest, first.content_digest)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.registry.snapshot(), ())

    def test_registry_miss_is_recovery_required_without_transition(self) -> None:
        request = self._request(self._payload())
        with patch("dockyard_composition.StateProvider") as state_provider:
            state_provider.return_value.snapshot.return_value = SimpleNamespace(tasks=(self.task,), events=())
            with self.assertRaises(DockyardCommandRejected) as raised:
                self.gateway.execute(request)
        self.assertEqual(raised.exception.code, "RECOVERY_REQUIRED")

    def test_canonical_identity_substitution_is_rejected_before_registry(self) -> None:
        calls: list[object] = []
        self._register(calls)
        request = self._request(self._payload(dispatch_id="DSP-EVIL"))
        with patch("dockyard_composition.StateProvider") as state_provider:
            state_provider.return_value.snapshot.return_value = SimpleNamespace(tasks=(self.task,), events=())
            with self.assertRaises(DockyardCommandRejected) as raised:
                self.gateway.execute(request)
        self.assertEqual(raised.exception.code, "RESOURCE_ID_DIVERGENCE")
        self.assertEqual(calls, [])
        self.assertEqual(self.registry.snapshot()[0].dispatch_id, "DSP-001")

    def test_non_in_progress_task_is_cas_rejected_before_registry(self) -> None:
        self._register()
        request = self._request(self._payload())
        terminal = SimpleNamespace(
            task_id="TC-001", revision=1, state="review_ready", attempt=2,
            current_dispatch=SimpleNamespace(dispatch_id="DSP-001"),
        )
        with patch("dockyard_composition.StateProvider") as state_provider:
            state_provider.return_value.snapshot.return_value = SimpleNamespace(tasks=(terminal,), events=())
            with self.assertRaises(DockyardCommandRejected) as raised:
                self.gateway.execute(request)
        self.assertEqual(raised.exception.code, "DISPATCH_CAS_CONFLICT")
        self.assertEqual(len(self.registry.snapshot()), 1)


if __name__ == "__main__":
    unittest.main()
