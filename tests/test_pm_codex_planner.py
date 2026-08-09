"""Directed PM-Codex planner and strict decoder coverage."""

from __future__ import annotations

import json
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
from dispatcher_gateway import DispatchLaunchError, DispatchNonZeroExitError  # noqa: E402
from pm_codex_planner import (  # noqa: E402
    PmCodexPlanner,
    PmCodexPlanningError,
    _decode_plan,
    _is_untrusted_proxy_certificate,
)
from pm_task_decomposer import PmCodexTaskSpec, decompose_codex_tasks  # noqa: E402
from portfolio_scheduler_store import BusinessPriority  # noqa: E402

sys.path.pop(0)


SNAPSHOT = "a" * 40
PLAN_ID = "PLAN-CODEX"
RATIONALE = [
    "scope.single_module",
    "clarity.known_pattern",
    "concurrency.single_writer",
    "contract.internal",
    "impact.local_failure",
    "rollback.tested",
    "dependency.known",
]


def _registry() -> AgentCapabilityRegistry:
    return AgentCapabilityRegistry((
        AgentCapability(
            "agentdesk.agent-capability/v1", "reasonix-basic-deepseek-v4-flash",
            "reasonix", "deepseek-v4-flash", (TaskDifficulty.BASIC,),
            ("coding", "implementation", "read", "testing", "write"), "efficient", 4, False, True,
        ),
        AgentCapability(
            "agentdesk.agent-capability/v1", "claude-opus",
            "claude", "opus", (TaskDifficulty.BASIC, TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED, TaskDifficulty.EXPERT),
            ("architecture", "code_review", "coding", "implementation", "planning", "read", "risk_assessment", "test_design", "testing", "write"), "deep", 4, False, True,
        ),
        AgentCapability(
            "agentdesk.agent-capability/v1", "codex-expert-gpt-5.6-sol",
            "codex", "gpt-5.6-sol", (TaskDifficulty.BASIC, TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED, TaskDifficulty.EXPERT),
            ("architecture", "code_review", "coding", "implementation", "planning", "read", "risk_assessment", "test_design", "testing", "write"), "deep", 4, True, True,
        ),
    ))


def _payload(plan_id: str = PLAN_ID) -> dict[str, object]:
    return {
        "schema_version": "agentdesk.pm-codex-plan/v1",
        "plan_id": plan_id,
        "tasks": [{
            "title": "Implement the bounded change",
            "description": "Implement the bounded change and add focused tests.",
            "dependencies": [],
            "execution_mode": "parallel",
            "task_type": "implementation",
            "capabilities": ["implementation", "testing"],
            "rationale_keys": RATIONALE,
            "difficulty": "standard",
            "risk": "L1",
            "business_priority": "P1",
        }],
    }


class PmCodexPlannerTests(unittest.TestCase):
    def test_proxy_certificate_classification_is_safe_and_specific(self) -> None:
        certificate_error = DispatchNonZeroExitError(
            1, "a" * 64, "b" * 64, b"invalid peer certificate: UnknownIssuer",
        )
        self.assertTrue(_is_untrusted_proxy_certificate(certificate_error))
        other_error = DispatchNonZeroExitError(
            1, "a" * 64, "b" * 64, b"authentication failed",
        )
        self.assertFalse(_is_untrusted_proxy_certificate(other_error))

    def test_decoder_requires_exact_schema_and_rationale_order(self) -> None:
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(_payload())},
        }
        specs = _decode_plan((json.dumps(event) + "\n").encode(), PLAN_ID)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].business_priority.value, "P1")
        self.assertEqual(specs[0].difficulty, TaskDifficulty.STANDARD)

        bad = _payload()
        bad["tasks"] = [dict(bad["tasks"][0], rationale_keys=list(reversed(RATIONALE)))]  # type: ignore[index]
        bad_event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(bad)}}
        with self.assertRaises(PmCodexPlanningError):
            _decode_plan((json.dumps(bad_event) + "\n").encode(), PLAN_ID)

    def test_planner_uses_read_only_codex_and_local_route_policy(self) -> None:
        captured: dict[str, object] = {}

        async def fake_run(request, providers):
            captured["request"] = request
            captured["provider"] = providers["codex"]
            event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_payload())}}
            return SimpleNamespace(stdout=(json.dumps(event) + "\n").encode())

        with tempfile.TemporaryDirectory() as directory:
            planner = PmCodexPlanner(
                Path(directory),
                "codex",
                _registry().route(TaskDifficulty.EXPERT, ("planning", "read")),
            )
            with patch("pm_codex_planner.run_dispatch", fake_run):
                result = planner.plan(
                    plan_id=PLAN_ID,
                    requirement="Add a bounded feature.",
                    snapshot_commit=SNAPSHOT,
                    command_id="CMD-CODEX-1",
                    registry=_registry(),
                )
        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(result.tasks[0].plan_task.provider_id, "claude")
        provider = captured["provider"]
        self.assertEqual(provider.sandbox_mode, "read-only")
        request = captured["request"]
        self.assertIn("Add a bounded feature.", request.prompt)
        self.assertIn("multiple Agents", request.prompt)
        self.assertIn("MUST read these project-local files", request.prompt)
        self.assertIn("Reasonix/deepseek-v4-flash", request.prompt)
        self.assertEqual(request.model_selection.selected_model_provider, "codex")

    def test_planner_retries_one_transient_cli_start_failure(self) -> None:
        attempts = 0

        async def flaky_run(request, providers):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise DispatchLaunchError("cold-start")
            event = {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(_payload())},
            }
            return SimpleNamespace(stdout=(json.dumps(event) + "\n").encode())

        with tempfile.TemporaryDirectory() as directory:
            planner = PmCodexPlanner(
                Path(directory),
                "codex",
                _registry().route(TaskDifficulty.EXPERT, ("planning", "read")),
            )
            with patch("pm_codex_planner.run_dispatch", flaky_run):
                result = planner.plan(
                    plan_id=PLAN_ID,
                    requirement="Add a bounded feature.",
                    snapshot_commit=SNAPSHOT,
                    command_id="CMD-CODEX-RETRY",
                    registry=_registry(),
                )

        self.assertEqual(attempts, 2)
        self.assertEqual(len(result.tasks), 1)

    def test_local_router_uses_only_registered_cost_ordered_bindings(self) -> None:
        basic_rationale = (
            "scope.bounded", "clarity.explicit", "concurrency.single_writer",
            "contract.internal", "impact.informational", "rollback.simple", "dependency.none",
        )
        advanced_rationale = (
            "scope.cross_module", "clarity.ambiguous_constraints", "concurrency.lock_cas",
            "contract.compatibility_sensitive", "impact.service_degradation", "rollback.multi_step", "dependency.cross_repository",
        )
        specs = (
            PmCodexTaskSpec(
                "Bounded Basic", "Make one bounded implementation change.", (), "parallel", "implementation",
                ("implementation", "testing"), basic_rationale, TaskDifficulty.BASIC, "L0", BusinessPriority.P1,
            ),
            PmCodexTaskSpec(
                "Routine Standard", "Implement one known-pattern module change.", (), "parallel", "implementation",
                ("implementation", "testing"), tuple(RATIONALE), TaskDifficulty.STANDARD, "L1", BusinessPriority.P1,
            ),
            PmCodexTaskSpec(
                "Cross Module", "Coordinate a compatibility-sensitive cross-module change.", (), "parallel", "implementation",
                ("implementation", "architecture"), advanced_rationale, TaskDifficulty.ADVANCED, "L2", BusinessPriority.P1,
            ),
        )
        result = decompose_codex_tasks(
            plan_id=PLAN_ID, task_specs=specs, snapshot_commit=SNAPSHOT, registry=_registry(),
        )
        self.assertEqual(
            tuple(task.plan_task.provider_id for task in result.tasks),
            ("reasonix", "claude", "codex"),
        )
        self.assertEqual(result.tasks[0].plan_task.model_id, "deepseek-v4-flash")

    def test_decoder_and_policy_reject_missing_or_downgraded_difficulty(self) -> None:
        missing = _payload()
        del missing["tasks"][0]["difficulty"]  # type: ignore[index]
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(missing)}}
        with self.assertRaises(PmCodexPlanningError):
            _decode_plan((json.dumps(event) + "\n").encode(), PLAN_ID)

        downgraded = _payload()
        downgraded["tasks"] = [dict(downgraded["tasks"][0], difficulty="basic")]  # type: ignore[index]
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(downgraded)}}
        specs = _decode_plan((json.dumps(event) + "\n").encode(), PLAN_ID)
        with self.assertRaises(ValueError):
            decompose_codex_tasks(
                plan_id=PLAN_ID, task_specs=specs, snapshot_commit=SNAPSHOT, registry=_registry(),
            )

    def test_identity_or_dependency_divergence_fails_closed(self) -> None:
        wrong = _payload("OTHER-PLAN")
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(wrong)}}
        with self.assertRaises(PmCodexPlanningError):
            _decode_plan((json.dumps(event) + "\n").encode(), PLAN_ID)

        invalid = _payload()
        invalid["tasks"] = [dict(invalid["tasks"][0], dependencies=[2])]  # type: ignore[index]
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(invalid)}}
        specs = _decode_plan((json.dumps(event) + "\n").encode(), PLAN_ID)
        with self.assertRaises(ValueError):
            decompose_codex_tasks(
                plan_id=PLAN_ID,
                task_specs=specs,
                snapshot_commit=SNAPSHOT,
                registry=_registry(),
            )


if __name__ == "__main__":
    unittest.main()
