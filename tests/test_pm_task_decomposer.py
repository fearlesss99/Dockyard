"""Directed tests for PM decomposition and route evidence."""

from __future__ import annotations

import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "agentdesk" / "scripts"))
from agent_capability_registry import AgentCapability, AgentCapabilityRegistry  # noqa: E402
from core_types import TaskDifficulty  # noqa: E402
from pm_task_decomposer import (  # noqa: E402
    PmTaskRoutingOverride,
    decompose_requirement,
)
from pm_repository_context import PmRepositoryInventory  # noqa: E402
sys.path.pop(0)


SNAPSHOT = "a" * 40


def _registry() -> AgentCapabilityRegistry:
    return AgentCapabilityRegistry((
        AgentCapability(
            "agentdesk.agent-capability/v1", "reasonix-basic", "reasonix",
            "deepseek-v4-flash", (TaskDifficulty.BASIC,),
            ("implementation", "testing"), "efficient", 4,
        ),
        AgentCapability(
            "agentdesk.agent-capability/v1", "codex-expert", "codex",
            "gpt-5.6-sol", (TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED, TaskDifficulty.EXPERT),
            ("implementation", "testing"), "deep", 4, True,
        ),
    ), 4)


class PmTaskDecomposerTests(unittest.TestCase):
    def test_pm_task_identity_matches_worktree_numeric_contract(self) -> None:
        kwargs = dict(
            plan_id="PLAN-ID-CONTRACT",
            requirement="鏌ョ湅 Runner 杩炴帴鐘舵€�",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
        )
        first = decompose_requirement(**kwargs).tasks[0].task_id
        replay = decompose_requirement(**kwargs).tasks[0].task_id
        self.assertRegex(first, re.compile(r"^TC-[0-9]{20}$"))
        self.assertEqual(first, replay)

    def test_plain_requirement_is_one_automatically_routed_task(self) -> None:
        result = decompose_requirement(
            plan_id="PLAN-1",
            requirement="查看本地页面",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
        )
        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(result.tasks[0].route.provider_id, "reasonix")
        self.assertEqual(result.tasks[0].plan_task.selected_difficulty, TaskDifficulty.BASIC)
        self.assertEqual(result.max_concurrency, 4)

    def test_numbered_requirement_is_split_and_validation_depends_on_implementation(self) -> None:
        result = decompose_requirement(
            plan_id="PLAN-2",
            requirement="1. 增加设置接口\n2. 编写测试验证接口",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
        )
        self.assertEqual(len(result.tasks), 2)
        self.assertEqual(result.tasks[1].dependencies, (result.tasks[0].task_id,))
        self.assertEqual(result.tasks[1].execution_mode, "serial")
        self.assertEqual(result.tasks[1].task_type, "validation")

    def test_unmarked_multiline_prose_is_not_split_accidentally(self) -> None:
        result = decompose_requirement(
            plan_id="PLAN-PROSE",
            requirement="完成本地页面\n并保留现有审批边界",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
        )
        self.assertEqual(len(result.tasks), 1)

    def test_repository_evidence_is_bound_into_decomposition_result(self) -> None:
        inventory = PmRepositoryInventory(
            "agentdesk.pm-repository-inventory/v1", SNAPSHOT, 10,
            ("apps", "skills"), (("py", 8), ("tsx", 2)), "b" * 64,
        )
        result = decompose_requirement(
            plan_id="PLAN-REPO",
            requirement="检查项目全部模块",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
            repository_inventory=inventory,
        )
        self.assertEqual(result.repository_digest, "b" * 64)
        self.assertEqual(result.tasks[0].difficulty, TaskDifficulty.ADVANCED)

    def test_project_mention_does_not_promote_bounded_readme_task(self) -> None:
        result = decompose_requirement(
            plan_id="PLAN-README",
            requirement="在项目 README 中补充本地运行说明，并新增一个最小 Markdown 示例文件。",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
            repository_inventory=PmRepositoryInventory(
                "agentdesk.pm-repository-inventory/v1", SNAPSHOT, 10,
                ("README.md",), (("md", 1),), "c" * 64,
            ),
        )
        self.assertEqual(result.tasks[0].difficulty, TaskDifficulty.BASIC)
        self.assertEqual(result.tasks[0].route.provider_id, "reasonix")

    def test_user_override_has_precedence_over_automatic_route(self) -> None:
        result = decompose_requirement(
            plan_id="PLAN-3",
            requirement="完成基础实现",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
            routing_overrides=(PmTaskRoutingOverride("基础实现", preferred_agent="codex-expert"),),
        )
        self.assertEqual(result.tasks[0].route.agent_id, "codex-expert")
        self.assertEqual(result.tasks[0].route.selection_source, "preferred")

    def test_digest_is_deterministic(self) -> None:
        kwargs = dict(
            plan_id="PLAN-4",
            requirement="1. 改代码\n2. 测试验证",
            snapshot_commit=SNAPSHOT,
            registry=_registry(),
        )
        self.assertEqual(decompose_requirement(**kwargs), decompose_requirement(**kwargs))


if __name__ == "__main__":
    unittest.main()
