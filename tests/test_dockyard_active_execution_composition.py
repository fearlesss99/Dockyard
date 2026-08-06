from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dockyard_active_execution as active
import dockyard_local_runtime as local_runtime
import dockyard_post_admission_worker as post_worker
import pm_external_worker_runtime as external_worker
import portfolio_scheduler_worker_handoff_runtime as handoff


class DockyardActiveExecutionCompositionTests(unittest.TestCase):
    def test_registry_is_injected_through_all_handoff_layers(self) -> None:
        self.assertIn("active_registry", inspect.signature(handoff.start_admitted_dispatch_completion).parameters)
        self.assertIn("active_registry", inspect.signature(external_worker.run_pm_external_worker).parameters)
        self.assertIn("active_registry", inspect.signature(post_worker.DockyardPostAdmissionWorkerRuntime).parameters)
        self.assertIn("DockyardActiveExecutionRegistry", inspect.getsource(local_runtime.DockyardLocalRuntime.start))

    def test_post_admission_accepts_typed_registry_without_touching_disk(self) -> None:
        registry = active.DockyardActiveExecutionRegistry()
        runtime = post_worker.DockyardPostAdmissionWorkerRuntime(
            Path.cwd(), {"claude": object()}, (("claude", "test"),),
            active_registry=registry,
        )
        self.assertIs(runtime.active_registry, registry)

    def test_registry_must_be_exact_type_at_composition_boundary(self) -> None:
        with self.assertRaises(post_worker.DockyardPostAdmissionInputError):
            post_worker.DockyardPostAdmissionWorkerRuntime(
                Path.cwd(), {"claude": object()}, (("claude", "test"),),
                active_registry=object(),
            )

    def test_public_sources_do_not_add_pid_or_provider_probe(self) -> None:
        source = inspect.getsource(active)
        self.assertNotIn("probe_process", source)
        self.assertNotIn("get_current_process_identity", source)
        self.assertNotIn("API_KEY", source)


if __name__ == "__main__":
    unittest.main()
