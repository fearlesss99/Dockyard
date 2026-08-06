"""Directed active-ownership projection tests for TC-13.29l.12d."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dispatch_supervisor_evidence as dispatch_evidence  # noqa: E402
from dockyard_active_execution import (  # noqa: E402
    DockyardActiveExecutionRegistry,
    make_active_execution_identity,
)
from dockyard_composition import DockyardPlanReadService  # noqa: E402
from tests.test_dockyard_projection import _snapshot  # noqa: E402


class DockyardActiveTerminationProjectionTests(unittest.TestCase):
    def _service(self, registry: DockyardActiveExecutionRegistry) -> DockyardPlanReadService:
        service = object.__new__(DockyardPlanReadService)
        service._project_root = ROOT
        service._active_registry = registry
        return service

    def _receipt(self, generation_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            task_id="TC-ACTIVE",
            revision=1,
            attempt=1,
            dispatch_id="DSP-ACTIVE",
            generation_id=generation_id,
            phase=dispatch_evidence.DispatchReceiptPhase.WORKER_STARTED.value,
            written_at="2026-08-04T00:00:00Z",
            creator_pid=1,
            creator_creation_time="2026-08-04T00:00:00Z",
            supervisor_pid=None,
            supervisor_creation_time=None,
            worker_pid=None,
            worker_creation_time=None,
            worker_process_group=None,
            boot_id="boot-active",
        )

    def test_exact_active_registry_identity_enables_termination(self) -> None:
        registry = DockyardActiveExecutionRegistry()
        identity = make_active_execution_identity(
            "TC-ACTIVE", 1, 1, "DSP-ACTIVE", "GEN-ACTIVE", "2026-08-04T00:00:00Z",
        )
        registry.register(identity, lambda command: None)
        service = self._service(registry)
        with patch.object(dispatch_evidence, "read_dispatch_receipt", return_value=self._receipt("GEN-ACTIVE")), \
             patch.object(dispatch_evidence, "read_dispatch_tombstone", return_value=None):
            run = service._run_evidence(_snapshot(False))[0]
        self.assertTrue(run.termination_available)
        self.assertEqual(run.generation_id, "GEN-ACTIVE")
        self.assertTrue(run.termination_event_id.startswith("EVT-CANCEL-"))

    def test_generation_divergence_fails_closed_without_termination(self) -> None:
        registry = DockyardActiveExecutionRegistry()
        identity = make_active_execution_identity(
            "TC-ACTIVE", 1, 1, "DSP-ACTIVE", "GEN-ACTIVE", "2026-08-04T00:00:00Z",
        )
        registry.register(identity, lambda command: None)
        service = self._service(registry)
        with patch.object(dispatch_evidence, "read_dispatch_receipt", return_value=self._receipt("GEN-OTHER")), \
             patch.object(dispatch_evidence, "read_dispatch_tombstone", return_value=None):
            run = service._run_evidence(_snapshot(False))[0]
        self.assertFalse(run.termination_available)
        self.assertIsNone(run.termination_event_id)


if __name__ == "__main__":
    unittest.main()
