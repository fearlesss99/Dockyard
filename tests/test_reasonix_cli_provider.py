"""TC-13.28a.2 Phase A: ReasonixCliProvider unit tests.

Tests the adapter's ``build_invocation()`` output shape, argv validation,
input rejection, and frozen boundary enforcement.  Zero real CLI, model,
API, network, or subprocess.

Uses the dispatcher_gateway types via import; no filesystem access.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import dispatcher_gateway as dg  # noqa: E402
import reasonix_cli_provider as rcp  # noqa: E402

sys.path.pop(0)

DispatchIdentity = dg.DispatchIdentity
ModelSelectionSnapshot = dg.ModelSelectionSnapshot
DispatchRequest = dg.DispatchRequest
AgentCliInvocation = dg.AgentCliInvocation
ReasonixCliProvider = rcp.ReasonixCliProvider


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_identity(**overrides) -> DispatchIdentity:
    defaults = {
        "task_id": "TC-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-001",
    }
    defaults.update(overrides)
    return DispatchIdentity(**defaults)


def _make_snapshot(**overrides) -> ModelSelectionSnapshot:
    defaults: dict = {
        "required_model_tier": "basic",
        "required_model_capabilities": ["read", "write"],
        "model_binding_id": "bind-reasonix-1",
        "selected_model_provider": "reasonix",
        "selected_model_id": "deepseek-v4-flash",
        "selected_model_tier": "basic",
        "selected_deliberation_tier": "efficient",
        "selected_context_window_tokens": 128000,
        "selected_model_capabilities": ["read", "write"],
        "model_degradation_approval_id": None,
    }
    defaults.update(overrides)
    return ModelSelectionSnapshot.from_mapping(defaults)


def _make_request(**overrides) -> DispatchRequest:
    defaults: dict = {
        "identity": _make_identity(),
        "workspace": Path(tempfile.gettempdir()),
        "prompt": "test prompt",
        "model_selection": _make_snapshot(),
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return DispatchRequest(**defaults)


def _make_provider(**overrides) -> ReasonixCliProvider:
    defaults: dict = {
        "provider_id": "reasonix",
        "executable": "reasonix",
        "permission_mode": "acceptEdits",
        "allowed_tools": (),
        "max_steps": 12,
    }
    defaults.update(overrides)
    return ReasonixCliProvider(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# Test suite
# ═══════════════════════════════════════════════════════════════════════════════


class ReasonixCliProviderInitTests(unittest.TestCase):
    """Construction-time validation."""

    def test_default_construction(self) -> None:
        p = _make_provider()
        self.assertEqual(p.provider_id, "reasonix")
        self.assertEqual(p.max_steps, 12)

    def test_rejects_wrong_provider_id(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(provider_id="codex")

    def test_rejects_empty_executable(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(executable="")

    def test_rejects_whitespace_executable(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(executable="  reasonix  ")

    def test_rejects_nul_in_executable(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(executable="reasonix\x00")

    def test_rejects_shell_metachar_in_executable(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(executable="reasonix&")

    def test_rejects_manual_permission_mode(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(permission_mode="manual")

    def test_rejects_auto_permission_mode(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(permission_mode="auto")

    def test_rejects_bypassPermissions(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(permission_mode="bypassPermissions")

    def test_accepts_acceptEdits(self) -> None:
        p = _make_provider(permission_mode="acceptEdits")
        self.assertEqual(p.permission_mode, "acceptEdits")

    def test_rejects_empty_allowed_tool(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(allowed_tools=("Bash", "", "Read"))

    def test_rejects_whitespace_allowed_tool(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(allowed_tools=("Bash", " Read "))

    def test_rejects_duplicate_allowed_tool(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(allowed_tools=("Bash", "Bash"))

    def test_rejects_max_steps_0(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(max_steps=0)

    def test_rejects_max_steps_13(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(max_steps=13)

    def test_rejects_bool_max_steps(self) -> None:
        with self.assertRaises(ValueError):
            _make_provider(max_steps=True)


class ReasonixCliProviderBuildInvocationTests(unittest.TestCase):
    """``build_invocation()`` output shape and argv content."""

    def test_build_invocation_returns_agent_cli_invocation(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)

    def test_argv_starts_with_run(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertEqual(inv.argv[0], "run")

    def test_model_flag_uses_reasonix_provider_route(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "deepseek-flash")
        self.assertEqual(
            req.model_selection.selected_model_id,
            "deepseek-v4-flash",
        )

    def test_project_config_maps_route_without_secret(self) -> None:
        text = (_REPO_ROOT / "reasonix.toml").read_text(encoding="utf-8")
        self.assertIn('name = "deepseek-flash"', text)
        self.assertIn('model = "deepseek-v4-flash"', text)
        self.assertIn('api_key_env = "DEEPSEEK_API_KEY"', text)
        self.assertNotIn("sk-", text)

    def test_profile_flag_is_economy(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        idx = inv.argv.index("--profile")
        self.assertEqual(inv.argv[idx + 1], "economy")

    def test_max_steps_flag_is_12(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        idx = inv.argv.index("--max-steps")
        self.assertEqual(inv.argv[idx + 1], "12")

    def test_output_format_is_json(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        idx = inv.argv.index("--output-format")
        self.assertEqual(inv.argv[idx + 1], "json")

    def test_permission_mode_frozen(self) -> None:
        p = _make_provider(permission_mode="acceptEdits")
        req = _make_request()
        inv = p.build_invocation(req)
        idx = inv.argv.index("--permission-mode")
        self.assertEqual(inv.argv[idx + 1], "acceptEdits")

    def test_print_flag_present(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIn("--print", inv.argv)

    def test_stdin_is_utf8_bytes_from_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="hello world")
        inv = p.build_invocation(req)
        self.assertIsInstance(inv.stdin, bytes)
        self.assertEqual(inv.stdin, b"hello world")

    def test_stdin_unicode_preserved(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="—— Unicode ——")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "—— Unicode ——".encode("utf-8"))

    def test_with_allowed_tools(self) -> None:
        p = _make_provider(
            permission_mode="acceptEdits",
            allowed_tools=("Bash", "Read", "Write"),
        )
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIn("--allowed-tools", inv.argv)
        at_idx = inv.argv.index("--allowed-tools")
        tools = list(inv.argv[at_idx + 1:at_idx + 4])
        self.assertEqual(tools, ["Bash", "Read", "Write"])

    def test_rejects_non_reasonix_model_id(self) -> None:
        p = _make_provider()
        snap = _make_snapshot(selected_model_id="gpt-5")
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_rejects_deepseek_v4_pro_model_id(self) -> None:
        p = _make_provider()
        snap = _make_snapshot(selected_model_id="deepseek-v4-pro")
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_env_overrides_empty(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertEqual(inv.env_overrides, ())

    def test_no_api_key_in_argv(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        argv_str = " ".join(inv.argv)
        self.assertNotIn("api_key", argv_str.lower())
        self.assertNotIn("--api-key", argv_str)
        self.assertNotIn("--token", argv_str)

    def test_no_auto_flag(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("--auto", inv.argv)
        self.assertNotIn("-y", inv.argv)

    def test_no_yolo_flag(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("--yolo", inv.argv)

    def test_no_bypassPermissions(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        perms = [a for a in inv.argv if "bypass" in a.lower()]
        self.assertEqual(perms, [])


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()
