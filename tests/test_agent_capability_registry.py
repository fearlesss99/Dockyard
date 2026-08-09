"""Directed tests for deterministic PM agent capability routing."""

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "agentdesk" / "scripts"))
from agent_capability_registry import (  # noqa: E402
    AgentCapability,
    AgentCapabilityRegistry,
    AgentRoutingUnavailableError,
)
from core_types import TaskDifficulty  # noqa: E402
sys.path.pop(0)


def _registry() -> AgentCapabilityRegistry:
    return AgentCapabilityRegistry((
        AgentCapability(
            "agentdesk.agent-capability/v1", "reasonix-basic", "reasonix",
            "deepseek-v4-flash", (TaskDifficulty.BASIC,),
            ("implementation", "testing"), "efficient", 4,
        ),
        AgentCapability(
            "agentdesk.agent-capability/v1", "claude-standard", "claude",
            "claude-sonnet", (TaskDifficulty.BASIC, TaskDifficulty.STANDARD),
            ("implementation", "testing"), "balanced", 4,
        ),
        AgentCapability(
            "agentdesk.agent-capability/v1", "codex-expert", "codex",
            "gpt-5.6-sol", (
                TaskDifficulty.STANDARD, TaskDifficulty.ADVANCED,
                TaskDifficulty.EXPERT,
            ), ("implementation", "testing"), "deep", 4, True,
        ),
    ), 4)


class AgentCapabilityRegistryTests(unittest.TestCase):
    def test_difficulty_selects_provider_without_downgrade(self) -> None:
        registry = _registry()
        self.assertEqual(registry.route(TaskDifficulty.BASIC).provider_id, "reasonix")
        self.assertEqual(registry.route(TaskDifficulty.STANDARD).provider_id, "claude")
        self.assertEqual(registry.route(TaskDifficulty.ADVANCED).provider_id, "codex")
        self.assertEqual(registry.route(TaskDifficulty.EXPERT).model_id, "gpt-5.6-sol")

    def test_preferred_and_forced_overrides_are_explicit(self) -> None:
        registry = _registry()
        preferred = registry.route(TaskDifficulty.BASIC, preferred_agent="claude-standard")
        self.assertEqual((preferred.agent_id, preferred.selection_source), ("claude-standard", "preferred"))
        forced = registry.route(TaskDifficulty.STANDARD, forced_agent="codex-expert")
        self.assertEqual((forced.agent_id, forced.selection_source), ("codex-expert", "forced"))

    def test_forbidden_and_unavailable_are_fail_closed(self) -> None:
        registry = _registry()
        with self.assertRaises(AgentRoutingUnavailableError):
            registry.route(TaskDifficulty.BASIC, forbidden_agents=("reasonix-basic", "claude-standard"))
        with self.assertRaises(AgentRoutingUnavailableError):
            registry.route(TaskDifficulty.EXPERT, forced_agent="claude-standard")

    def test_max_concurrency_is_bounded(self) -> None:
        self.assertEqual(_registry().max_concurrency, 4)

    def test_coding_capability_satisfies_implementation_without_covering_testing(self) -> None:
        registry = AgentCapabilityRegistry((
            AgentCapability(
                "agentdesk.agent-capability/v1", "coding-agent", "claude",
                "claude-opus", (TaskDifficulty.ADVANCED,),
                ("coding",), "deep", 1,
            ),
        ))
        self.assertEqual(
            registry.route(TaskDifficulty.ADVANCED, ("implementation",)).agent_id,
            "coding-agent",
        )
        with self.assertRaises(AgentRoutingUnavailableError):
            registry.route(TaskDifficulty.ADVANCED, ("testing",))

    def test_malformed_bindings_do_not_silently_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindings = root / ".agentdesk" / "runtime"
            bindings.mkdir(parents=True)
            (bindings / "model-bindings.yaml").write_text(
                json.dumps({"schema_version": "wrong"}), encoding="utf-8"
            )
            from agent_capability_registry import AgentRoutingInputError
            with self.assertRaises(AgentRoutingInputError):
                AgentCapabilityRegistry.from_project(root)

    def test_empty_bindings_are_not_treated_as_a_default_agent(self) -> None:
        registry = AgentCapabilityRegistry(())
        with self.assertRaises(AgentRoutingUnavailableError):
            registry.route(TaskDifficulty.BASIC)


if __name__ == "__main__":
    unittest.main()
