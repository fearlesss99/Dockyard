"""PM-Codex plan.create composition coverage."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_capability_registry import AgentCapability, AgentCapabilityRegistry  # noqa: E402
from core_types import TaskDifficulty  # noqa: E402
from dockyard_composition import (  # noqa: E402
    DockyardCompositionGateway,
    _pm_codex_failure_code,
)
from dockyard_control_api import (  # noqa: E402
    DockyardCommandEnvelope,
    DockyardCommandRequest,
)
from dockyard_plan_store import DockyardPlanStore  # noqa: E402
from dockyard_pm_service import DockyardPmService  # noqa: E402
from dockyard_project_registry import (  # noqa: E402
    DockyardProjectRegistry,
    DockyardRegisterProjectRequest,
)
from dockyard_sse import DockyardSseHub  # noqa: E402
from pm_codex_planner import PmCodexPlanner, PmCodexPlanningError  # noqa: E402

sys.path.pop(0)


SNAPSHOT = "a" * 40
STAMP = "2026-08-07T00:00:00Z"


def _registry() -> AgentCapabilityRegistry:
    return AgentCapabilityRegistry((AgentCapability(
        "agentdesk.agent-capability/v1",
        "codex-expert-gpt-5.6-sol",
        "codex",
        "gpt-5.6-sol",
        (TaskDifficulty.BASIC, TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED, TaskDifficulty.EXPERT),
        ("planning", "read", "coding", "testing", "write"),
        "deep",
        4,
        True,
        True,
    ),))


class PmCodexCompositionTests(unittest.TestCase):
    def test_planner_failures_expose_only_stable_safe_codes(self) -> None:
        self.assertEqual(
            _pm_codex_failure_code(PmCodexPlanningError("pm_codex:proxy_certificate_untrusted")),
            "PM_CODEX_PROXY_CERTIFICATE_UNTRUSTED",
        )
        self.assertEqual(
            _pm_codex_failure_code(PmCodexPlanningError("pm_codex:dispatch_failed")),
            "PM_CODEX_DISPATCH_FAILED",
        )
        self.assertEqual(
            _pm_codex_failure_code(PmCodexPlanningError("pm_codex:plan_not_json")),
            "PM_CODEX_OUTPUT_INVALID",
        )
        self.assertEqual(
            _pm_codex_failure_code(PmCodexPlanningError("pm_codex:local_policy_rejected")),
            "PM_CODEX_PLAN_REJECTED",
        )

    def test_plan_create_uses_codex_before_local_pm_draft_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "init", "--quiet", str(repository)), check=True)
            registry_store = DockyardProjectRegistry(root / "registry")
            project = registry_store.register(DockyardRegisterProjectRequest(
                "PRJ-CODEX", "Dockyard", str(repository), "OP-REGISTER", STAMP,
            )).record
            pm = DockyardPmService(DockyardPlanStore(root / "plans"), root / "docs/pm/tasks")
            capability_registry = _registry()
            planner = PmCodexPlanner(
                repository,
                "codex",
                capability_registry.route(TaskDifficulty.EXPERT, ("planning", "read")),
            )
            gateway = DockyardCompositionGateway(
                pm,
                lambda: SNAPSHOT,
                DockyardSseHub(),
                registry_store,
                project_root=repository,
                plan_id="PLAN-CODEX",
                agent_registry=capability_registry,
                pm_planner=planner,
                now=lambda: STAMP,
            )
            requirement = "Create a real PM-Codex generated plan."
            payload = {"plan_id": "PLAN-CODEX", "requirement": requirement}
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            envelope = DockyardCommandEnvelope(
                "dockyard.command/v1",
                "CMD-CODEX-PLAN",
                "PRJ-CODEX",
                "plan.create",
                "IDEMP-CODEX-PLAN",
                SNAPSHOT,
                project.generation,
                None,
                hashlib.sha256(raw).hexdigest(),
            )
            request = DockyardCommandRequest(envelope, None, raw)
            output = {
                "schema_version": "agentdesk.pm-codex-plan/v1",
                "plan_id": "PLAN-CODEX",
                "tasks": [{
                    "title": "Codex generated task",
                    "description": "A task produced by PM-Codex.",
                    "dependencies": [],
                    "execution_mode": "parallel",
                    "task_type": "implementation",
                    "capabilities": ["implementation", "testing"],
                    "rationale_keys": [
                        "scope.single_module", "clarity.known_pattern",
                        "concurrency.single_writer", "contract.internal",
                        "impact.local_failure", "rollback.tested", "dependency.known",
                    ],
                    "difficulty": "standard",
                    "risk": "L1",
                    "business_priority": "P1",
                }, {
                    "title": "Codex generated verification task",
                    "description": "Verify the independently implemented behavior.",
                    "dependencies": [],
                    "execution_mode": "parallel",
                    "task_type": "qa",
                    "capabilities": ["testing"],
                    "rationale_keys": [
                        "scope.single_module", "clarity.known_pattern",
                        "concurrency.single_writer", "contract.internal",
                        "impact.local_failure", "rollback.tested", "dependency.known",
                    ],
                    "difficulty": "standard",
                    "risk": "L1",
                    "business_priority": "P1",
                }],
            }
            event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output)}}
            calls: list[object] = []

            async def fake_run(dispatch_request, providers):
                calls.append((dispatch_request, providers["codex"]))
                return SimpleNamespace(stdout=(json.dumps(event) + "\n").encode())

            with patch("pm_codex_planner.run_dispatch", fake_run):
                first = gateway.execute(request)
                replay = gateway.execute(request)

            plan = pm.latest("PLAN-CODEX")
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(plan.tasks[0].title, "Codex generated task")
            self.assertEqual(len(plan.tasks), 2)
            self.assertEqual(plan.tasks[1].title, "Codex generated verification task")
            self.assertEqual(plan.tasks[1].execution_mode, "parallel")
            self.assertEqual(plan.tasks[0].provider_id, "codex")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1].sandbox_mode, "read-only")
            self.assertFalse(first.replayed)
            self.assertTrue(replay.replayed)


if __name__ == "__main__":
    unittest.main()
