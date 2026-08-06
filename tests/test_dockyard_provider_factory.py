"""Directed tests for the local Provider composition root."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from claude_code_provider import ClaudeCodeProvider  # noqa: E402
from dockyard_local_runtime import (  # noqa: E402
    DockyardLocalRuntime,
    DockyardLocalRuntimeConfig,
)
from dockyard_provider_factory import (  # noqa: E402
    DockyardProviderFactoryError,
    build_configured_providers,
)
from reasonix_cli_provider import ReasonixCliProvider  # noqa: E402

sys.path.pop(0)


def _bindings_document(reasonix_model: str = "deepseek-v4-flash") -> dict[str, object]:
    return {
        "schema_version": "agentdesk.model-bindings/v2",
        "updated_at": "2026-08-06T00:00:00Z",
        "bindings": {
            "reasonix-basic": {
                "provider": "reasonix",
                "model_id": reasonix_model,
                "tier": "basic",
                "deliberation_tier": "efficient",
                "context_window_tokens": 128000,
                "capabilities": ["coding", "implementation", "read", "testing", "write"],
                "enabled": True,
            },
            "claude-expert": {
                "provider": "claude",
                "model_id": "claude-opus-4-8[1M]",
                "tier": "expert",
                "deliberation_tier": "deep",
                "context_window_tokens": 1000000,
                "capabilities": [
                    "adversarial_review", "architecture", "code_review", "coding",
                    "planning", "risk_assessment", "test_design", "testing",
                ],
                "enabled": True,
            },
        },
    }


class DockyardProviderFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bindings = self.root / ".agentdesk" / "runtime"
        bindings.mkdir(parents=True)
        (bindings / "model-bindings.yaml").write_text(
            json.dumps(_bindings_document(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_builds_reasonix_and_claude_without_starting_them(self) -> None:
        configured = build_configured_providers(self.root)
        self.assertEqual(tuple(provider_id for provider_id, _ in configured.providers), ("claude", "reasonix"))
        self.assertEqual(configured.provider_cli_versions, (("claude", "2.1.214"), ("reasonix", "1.19.1")))
        self.assertEqual(configured.binding_ids, (("claude", "claude-expert"), ("reasonix", "reasonix-basic")))
        providers = configured.as_mapping()
        self.assertIsInstance(providers["reasonix"], ReasonixCliProvider)
        self.assertEqual(providers["reasonix"].executable, "reasonix")
        self.assertEqual(providers["reasonix"].permission_mode, "acceptEdits")
        self.assertEqual(providers["reasonix"].allowed_tools, ("Bash", "Read", "Write", "Edit"))
        self.assertIsInstance(providers["claude"], ClaudeCodeProvider)

    def test_explicit_executable_override_is_preserved(self) -> None:
        configured = build_configured_providers(
            self.root,
            executable_overrides=(("reasonix", r"D:\reasonix.exe"),),
        )
        self.assertEqual(configured.as_mapping()["reasonix"].executable, r"D:\reasonix.exe")

    def test_reasonix_model_cannot_be_upgraded(self) -> None:
        (self.root / ".agentdesk" / "runtime" / "model-bindings.yaml").write_text(
            json.dumps(_bindings_document("deepseek-v4-pro")), encoding="utf-8"
        )
        with self.assertRaises(DockyardProviderFactoryError):
            build_configured_providers(self.root)

    def test_missing_bindings_fail_closed(self) -> None:
        (self.root / ".agentdesk" / "runtime" / "model-bindings.yaml").unlink()
        with self.assertRaises(DockyardProviderFactoryError):
            build_configured_providers(self.root)

    def test_local_runtime_classmethod_injects_mapping(self) -> None:
        registry = self.root / "registry"
        pairing = self.root / "pairing"
        plans = self.root / "plans"
        cards = self.root / "cards"
        for directory in (registry, pairing, plans, cards):
            directory.mkdir()
        config = DockyardLocalRuntimeConfig(
            "RUNTIME-FACTORY",
            "PRJ-FACTORY",
            "PLAN-FACTORY",
            self.root,
            registry,
            pairing,
            plans,
            cards,
            "DEVICE-FACTORY",
            "TOKEN-FACTORY",
            "CSRF-FACTORY",
        )
        runtime = DockyardLocalRuntime.from_local_configuration(config)
        try:
            self.assertEqual(tuple(sorted(runtime._providers or {})), ("claude", "reasonix"))
            self.assertEqual(runtime._provider_cli_versions, (
                ("claude", "2.1.214"),
                ("reasonix", "1.19.1"),
            ))
        finally:
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
