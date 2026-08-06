"""Directed TC-13.29l.13b retry-owner boundary tests."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "skills" / "agentdesk" / "scripts"))

from dockyard_composition import DockyardCompositionGateway  # noqa: E402
from dockyard_control_api import (  # noqa: E402
    DockyardCommandEnvelope,
    DockyardCommandRejected,
    DockyardCommandRequest,
)


class DockyardRetryRuntimeBoundaryTests(unittest.TestCase):
    def _request(self, payload: dict[str, object]) -> DockyardCommandRequest:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        envelope = DockyardCommandEnvelope(
            "dockyard.command/v1",
            "CMD-RETRY-1",
            "PRJ-1",
            "task.retry",
            "IDEMP-RETRY-1",
            "a" * 40,
            1,
            "CONF-RETRY-1",
            __import__("hashlib").sha256(raw).hexdigest(),
        )
        return DockyardCommandRequest(envelope, "TC-001", raw)

    def _gateway(self, *, providers=None, ready_provider_ids=()):
        gateway = object.__new__(DockyardCompositionGateway)
        gateway._project_root = _ROOT
        gateway._providers = providers
        gateway._ready_provider_ids = frozenset(ready_provider_ids)
        gateway._provider_cli_versions = {}
        gateway._retry_receipts = {}
        gateway._retry_receipts_lock = threading.Lock()
        return gateway

    def test_retry_without_canonical_owner_is_not_ready(self) -> None:
        gateway = self._gateway()
        with self.assertRaises(DockyardCommandRejected) as raised:
            gateway._retry_task(self._request({}), "a" * 40)
        self.assertEqual(raised.exception.code, "COMMAND_OWNER_UNAVAILABLE")

    def test_provider_not_ready_is_rejected_before_state_read(self) -> None:
        gateway = self._gateway(providers={"claude": object()}, ready_provider_ids=())
        payload = {
            "task_id": "TC-001", "failed_attempt": 1, "dispatch_id": "DSP-1",
            "generation_id": "GEN-1", "event_id": "EVT-FAILED-1",
            "provider_id": "claude", "model_id": "claude-sonnet", "upgrade": False,
        }
        with self.assertRaises(DockyardCommandRejected) as raised:
            gateway._retry_task(self._request(payload), "a" * 40)
        self.assertEqual(raised.exception.code, "PROVIDER_NOT_READY")

    def test_payload_shape_is_typed_and_fail_closed(self) -> None:
        gateway = self._gateway(providers={"claude": object()}, ready_provider_ids=("claude",))
        with self.assertRaises(DockyardCommandRejected) as raised:
            gateway._retry_task(self._request({"task_id": "TC-001"}), "a" * 40)
        self.assertEqual(raised.exception.code, "COMMAND_PAYLOAD_INVALID")


if __name__ == "__main__":
    unittest.main()
