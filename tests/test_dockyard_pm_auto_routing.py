"""Small production-composition proof for PM auto decomposition."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "agentdesk" / "scripts"))
from agent_capability_registry import AgentCapability, AgentCapabilityRegistry  # noqa: E402
from core_types import TaskDifficulty  # noqa: E402
from dockyard_composition import DockyardCompositionGateway  # noqa: E402
from dockyard_control_api import DockyardCommandEnvelope, DockyardCommandRequest  # noqa: E402
from dockyard_plan_store import DockyardPlanStore  # noqa: E402
from dockyard_pm_service import DockyardPmService  # noqa: E402
from dockyard_project_registry import DockyardProjectRegistry, DockyardRegisterProjectRequest  # noqa: E402
from dockyard_sse import DockyardSseHub  # noqa: E402
sys.path.pop(0)


class DockyardPmAutoRoutingTests(unittest.TestCase):
    def test_create_plan_uses_git_bound_decomposition_and_routes_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(("git", "init", "--quiet", str(repository)), check=True)
            (repository / "module.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(repository), "add", "--", "."), check=True)
            subprocess.run((
                "git", "-C", str(repository), "-c", "user.name=PM Test",
                "-c", "user.email=pm@example.invalid", "commit", "--quiet", "-m", "fixture",
            ), check=True)
            snapshot = subprocess.run(
                ("git", "-C", str(repository), "rev-parse", "HEAD"),
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout.strip()
            project_registry = DockyardProjectRegistry(root / "registry")
            project_registry.register(DockyardRegisterProjectRequest(
                "PRJ-1", "Dockyard", str(repository), "OP-1", "2026-08-06T00:00:00Z",
            ))
            pm = DockyardPmService(
                DockyardPlanStore(root / "plans"), root / "task-cards",
            )
            registry = AgentCapabilityRegistry((
                AgentCapability(
                    "agentdesk.agent-capability/v1", "reasonix-basic", "reasonix",
                    "deepseek-v4-flash", (TaskDifficulty.BASIC,),
                    ("implementation", "testing"), "efficient",
                ),
                AgentCapability(
                    "agentdesk.agent-capability/v1", "codex-expert", "codex",
                    "gpt-5.6-sol", (TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED, TaskDifficulty.EXPERT),
                    ("implementation", "testing"), "deep", 4, True,
                ),
            ))
            gateway = DockyardCompositionGateway(
                pm, lambda: snapshot, DockyardSseHub(), project_registry,
                project_root=repository, plan_id="PLAN-1", agent_registry=registry,
                now=lambda: "2026-08-06T00:00:00Z",
            )
            payload = json.dumps({
                "plan_id": "PLAN-1",
                "requirement": "1. 实现模块\n2. 测试验证",
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request = DockyardCommandRequest(
                DockyardCommandEnvelope(
                    "dockyard.command/v1", "CMD-1", "PRJ-1", "plan.create", "IDEM-1",
                    snapshot, 1, None, hashlib.sha256(payload).hexdigest(),
                ), None, payload,
            )
            receipt = gateway.execute(request)
            self.assertEqual(receipt.outcome, "drafted")
            plan = pm.latest("PLAN-1")
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(len(plan.tasks), 2)
            self.assertEqual(plan.tasks[0].provider_id, "codex")
            self.assertEqual(plan.tasks[1].dependencies, (plan.tasks[0].task_id,))

    def test_multiline_requirement_produces_projection_safe_single_line_title(self) -> None:
        from pm_task_decomposer import decompose_requirement

        registry = AgentCapabilityRegistry((
            AgentCapability(
                "agentdesk.agent-capability/v1", "codex-expert", "codex",
                "gpt-5.6-sol", (TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED, TaskDifficulty.EXPERT),
                ("implementation", "testing"), "deep", 4, True,
            ),
        ))
        requirement = "增加 Runner 摘要：\n\n1. 显示连接状态。\n2. 增加前端测试。"
        result = decompose_requirement(
            plan_id="PLAN-MULTILINE",
            requirement=requirement,
            snapshot_commit="a" * 40,
            registry=registry,
        )
        self.assertNotIn("\n", result.tasks[0].title)
        self.assertNotIn("\r", result.tasks[0].title)
        self.assertLessEqual(len(result.tasks[0].title), 256)


if __name__ == "__main__":
    unittest.main()
