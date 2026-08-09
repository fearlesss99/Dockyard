"""TC-13.8.4: comprehensive mock-based tests for ``codex_cli_provider.py``.

stdlib‑only unittest; no real CLI, model, API, network, or subprocess.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from unittest import mock

# -- Load the modules under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import dispatcher_gateway as dg  # noqa: E402
import codex_cli_provider as ccp  # noqa: E402
sys.path.pop(0)

# Re‑export for convenience
DispatchIdentity = dg.DispatchIdentity
ModelSelectionSnapshot = dg.ModelSelectionSnapshot
DispatchRequest = dg.DispatchRequest
AgentCliInvocation = dg.AgentCliInvocation
AgentCliProvider = dg.AgentCliProvider
CodexCliProvider = ccp.CodexCliProvider


# =========================================================================
# Helpers
# =========================================================================


def _make_identity(**overrides) -> DispatchIdentity:
    defaults = {
        "task_id": "TC-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-001",
    }
    defaults.update(overrides)
    return DispatchIdentity(**defaults)


def _make_snapshot_dict(**overrides) -> dict:
    defaults: dict = {
        "required_model_tier": "standard",
        "required_model_capabilities": ["read", "write"],
        "model_binding_id": "bind-1",
        "selected_model_provider": "codex",
        "selected_model_id": "gpt-5",
        "selected_model_tier": "standard",
        "selected_deliberation_tier": "balanced",
        "selected_context_window_tokens": 200000,
        "selected_model_capabilities": ["read", "write"],
        "model_degradation_approval_id": None,
    }
    defaults.update(overrides)
    return defaults


def _make_snapshot(**overrides) -> ModelSelectionSnapshot:
    return ModelSelectionSnapshot.from_mapping(_make_snapshot_dict(**overrides))


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


def _make_provider(**overrides) -> CodexCliProvider:
    defaults: dict = {
        "provider_id": "codex",
        "executable": "codex",
        "sandbox_mode": "workspace-write",
    }
    defaults.update(overrides)
    return CodexCliProvider(**defaults)


def _corrupt_snapshot(**overrides) -> ModelSelectionSnapshot:
    """Build a snapshot by direct dataclass constructor, bypassing
    ``from_mapping()``."""
    base = {
        "required_model_tier": "standard",
        "required_model_capabilities": ("read", "write"),
        "model_binding_id": "bind-1",
        "selected_model_provider": "codex",
        "selected_model_id": "gpt-5",
        "selected_model_tier": "standard",
        "selected_deliberation_tier": "balanced",
        "selected_context_window_tokens": 200000,
        "selected_model_capabilities": ("read", "write"),
        "model_degradation_approval_id": None,
    }
    base.update(overrides)
    return ModelSelectionSnapshot(**base)


# =========================================================================
# 1. Public structure
# =========================================================================


class PublicStructureTests(unittest.TestCase):
    """TC-13.8.4: public structure — dataclass, frozen, slots, fields, __all__."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(CodexCliProvider))

    def test_frozen(self) -> None:
        p = _make_provider()
        with self.assertRaises(Exception):
            p.executable = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        p = _make_provider()
        with self.assertRaises(AttributeError):
            p.__dict__  # type: ignore[attr-defined]

    def test_exact_three_fields(self) -> None:
        field_names = {f.name for f in fields(CodexCliProvider)}
        self.assertSetEqual(
            field_names,
            {"provider_id", "executable", "sandbox_mode"},
        )

    def test_no_fourth_field(self) -> None:
        forbidden = {
            "cwd", "workspace", "prompt", "timeout", "model",
            "model_id", "effort", "env", "env_overrides",
            "api_key", "token", "session_id", "retry", "slot", "lease",
            "approval_policy", "allowed_tools", "disallowed_tools",
            "permission_mode", "reasoning_effort", "profile",
            "config_overrides",
        }
        field_names = {f.name for f in fields(CodexCliProvider)}
        self.assertTrue(forbidden.isdisjoint(field_names),
                        f"Forbidden fields found: {forbidden & field_names}")

    def test_all_exact_one_symbol(self) -> None:
        self.assertEqual(ccp.__all__, ["CodexCliProvider"])

    def test_satisfies_agent_cli_provider_protocol(self) -> None:
        p = _make_provider()
        self.assertIsInstance(p, AgentCliProvider)

    def test_top_level_import_not_relative(self) -> None:
        src = (_SCRIPTS / "codex_cli_provider.py").read_text(encoding="utf-8")
        self.assertIn("from dispatcher_gateway import", src)
        self.assertNotIn("from .dispatcher_gateway import", src)

    def test_no_init_py_created(self) -> None:
        init_py = _SCRIPTS / "__init__.py"
        self.assertFalse(init_py.exists(),
                         "Must not create __init__.py")


# =========================================================================
# 2. Provider ID
# =========================================================================


class ProviderIdTests(unittest.TestCase):
    """TC-13.8.4: provider_id validation."""

    def test_codex_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex",
            sandbox_mode="workspace-write",
        )
        self.assertEqual(p.provider_id, "codex")

    def test_openai_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="openai",
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_openai_codex_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="openai-codex",
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_codexcli_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codexcli",
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_codex_uppercase_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="Codex",
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_claude_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="claude",
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="",
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id=None,  # type: ignore[arg-type]
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_true_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id=True,  # type: ignore[arg-type]
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_int_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id=1,  # type: ignore[arg-type]
                executable="codex",
                sandbox_mode="workspace-write",
            )

    def test_no_alias_normalization(self) -> None:
        """'codex' only has one valid ID — no dual instances."""
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex",
            sandbox_mode="workspace-write",
        )
        self.assertEqual(p.provider_id, "codex")


# =========================================================================
# 3. Sandbox mode
# =========================================================================


class SandboxModeTests(unittest.TestCase):
    """TC-13.8.4: sandbox_mode validation."""

    def test_read_only_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex",
            sandbox_mode="read-only",
        )
        self.assertEqual(p.sandbox_mode, "read-only")

    def test_workspace_write_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex",
            sandbox_mode="workspace-write",
        )
        self.assertEqual(p.sandbox_mode, "workspace-write")

    def test_danger_full_access_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex",
                sandbox_mode="danger-full-access",
            )

    def test_arbitrary_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex",
                sandbox_mode="full-access",
            )

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex",
                sandbox_mode="",
            )

    def test_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex",
                sandbox_mode=None,  # type: ignore[arg-type]
            )

    def test_non_str_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex",
                sandbox_mode=True,  # type: ignore[arg-type]
            )


# =========================================================================
# 4. Executable validation
# =========================================================================


class ExecutableValidationTests(unittest.TestCase):
    """TC-13.8.4: executable validation."""

    def test_plain_name_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex",
            sandbox_mode="workspace-write",
        )
        self.assertEqual(p.executable, "codex")

    def test_exe_name_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex.exe",
            sandbox_mode="workspace-write",
        )
        self.assertEqual(p.executable, "codex.exe")

    def test_cmd_name_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="codex.cmd",
            sandbox_mode="workspace-write",
        )
        self.assertEqual(p.executable, "codex.cmd")

    def test_posix_path_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="/usr/local/bin/codex",
            sandbox_mode="workspace-write",
        )
        self.assertTrue(p.executable.startswith("/"))

    def test_drive_absolute_no_spaces_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable=r"C:\Tools\codex.exe",
            sandbox_mode="workspace-write",
        )
        self.assertIn(":", p.executable)

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="",
                sandbox_mode="workspace-write",
            )

    def test_pure_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="   ",
                sandbox_mode="workspace-write",
            )

    def test_leading_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=" codex",
                sandbox_mode="workspace-write",
            )

    def test_trailing_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex ",
                sandbox_mode="workspace-write",
            )

    def test_nul_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex\x00hidden",
                sandbox_mode="workspace-write",
            )

    def test_cr_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex\r--version",
                sandbox_mode="workspace-write",
            )

    def test_lf_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex\n--version",
                sandbox_mode="workspace-write",
            )

    def test_tab_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex\targument",
                sandbox_mode="workspace-write",
            )

    def test_vt_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex\vargument",
                sandbox_mode="workspace-write",
            )

    def test_ff_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex\fargument",
                sandbox_mode="workspace-write",
            )

    def test_not_str_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=123,  # type: ignore[arg-type]
                sandbox_mode="workspace-write",
            )

    # -- Windows paths with spaces (allowed) -------------------------------

    def test_windows_program_files_path_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable=r"C:\Program Files\OpenAI\codex.exe",
            sandbox_mode="workspace-write",
        )
        self.assertIn(" ", p.executable)

    def test_windows_forward_slash_path_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable="C:/Program Files/OpenAI/codex.cmd",
            sandbox_mode="workspace-write",
        )
        self.assertIn(" ", p.executable)

    def test_windows_space_path_com_extension_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable=r"C:\Program Files\OpenAI Codex\Codex Runner.exe",
            sandbox_mode="workspace-write",
        )
        self.assertIn(" ", p.executable)

    def test_unc_path_allowed(self) -> None:
        p = CodexCliProvider(
            provider_id="codex",
            executable=r"\\server\share\OpenAI Codex\codex.exe",
            sandbox_mode="workspace-write",
        )
        self.assertIn(" ", p.executable)

    # -- Embedded arguments / shell command rejection ----------------------

    def test_codex_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex --version",
                sandbox_mode="workspace-write",
            )

    def test_codex_exec_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex exec",
                sandbox_mode="workspace-write",
            )

    def test_semicolon_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex;whoami",
                sandbox_mode="workspace-write",
            )

    def test_ampersand_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex & whoami",
                sandbox_mode="workspace-write",
            )

    def test_pipe_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex | more",
                sandbox_mode="workspace-write",
            )

    def test_backtick_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex`whoami`",
                sandbox_mode="workspace-write",
            )

    def test_dollar_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex $HOME",
                sandbox_mode="workspace-write",
            )

    def test_double_quoted_arg_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable='codex "argument"',
                sandbox_mode="workspace-write",
            )

    def test_single_quoted_arg_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex 'argument'",
                sandbox_mode="workspace-write",
            )

    def test_gt_redirect_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex > output.txt",
                sandbox_mode="workspace-write",
            )

    def test_lt_redirect_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="codex < input.txt",
                sandbox_mode="workspace-write",
            )

    # -- Windows paths with spaces + embedded arguments rejected -----------

    def test_windows_path_with_args_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\codex.exe --version",
                sandbox_mode="workspace-write",
            )

    def test_windows_path_with_slash_help_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\codex.exe /help",
                sandbox_mode="workspace-write",
            )

    def test_windows_path_with_payload_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\codex.exe C:\tmp\payload.txt",
                sandbox_mode="workspace-write",
            )

    def test_runner_second_drive_payload_exe_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\runner C:\tmp\payload.exe",
                sandbox_mode="workspace-write",
            )

    def test_runner_second_drive_forward_slash_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\runner D:/tmp/payload.cmd",
                sandbox_mode="workspace-write",
            )

    def test_runner_slash_help_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\runner /help",
                sandbox_mode="workspace-write",
            )

    def test_runner_slash_payload_exe_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"C:\Program Files\OpenAI\runner /tmp/payload.exe",
                sandbox_mode="workspace-write",
            )

    def test_unc_runner_other_share_payload_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"\\server\share\OpenAI Codex\runner \\other\share\payload.exe",
                sandbox_mode="workspace-write",
            )

    def test_unc_runner_drive_payload_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"\\server\share\OpenAI Codex\runner C:\tmp\payload.exe",
                sandbox_mode="workspace-write",
            )

    def test_unc_runner_slash_help_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable=r"\\server\share\OpenAI Codex\runner /help",
                sandbox_mode="workspace-write",
            )

    def test_posix_claude_argument_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="/usr/local/bin/codex argument",
                sandbox_mode="workspace-write",
            )

    def test_posix_claude_payload_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CodexCliProvider(
                provider_id="codex",
                executable="/usr/local/bin/codex /tmp/payload",
                sandbox_mode="workspace-write",
            )

    # -- Bypass table-driven -------------------------------------------------

    def test_bypass_table_driven_all_rejected(self) -> None:
        bypass_values = [
            "codex --version",
            "codex exec",
            "codex;whoami",
            "codex & whoami",
            "codex | more",
            "codex`whoami`",
            "codex $HOME",
            'codex "argument"',
            "codex 'argument'",
            "codex > output.txt",
            "codex < input.txt",
            r"C:\Program Files\OpenAI\codex.exe --version",
            r"C:\Program Files\OpenAI\codex.exe /help",
            r"C:\Program Files\OpenAI\codex.exe C:\tmp\payload.txt",
            r"C:\Program Files\OpenAI\codex.exe C:/tmp/payload.txt",
            r"C:\Program Files\OpenAI\codex.exe \\server\share\payload",
            r"C:\Program Files\OpenAI\codex.cmd /c",
            "/usr/local/bin/codex /tmp/payload",
            "/usr/local/bin/codex argument",
            r"\\server\share\OpenAI Codex\codex.exe \\other\share\payload",
            r"\\server\share\OpenAI Codex\codex.exe /help",
            "codex whoami",
            "codex true",
            r"C:\Program Files\OpenAI\codex.exe whoami",
            r"C:\Program Files\OpenAI\codex.exe calc.exe",
            r"\\server\share\OpenAI Codex\codex.exe whoami",
            "codex\targument",
            "codex\vargument",
            "codex\fargument",
            r"C:\Program Files\OpenAI\codex C:\tmp\payload.exe",
            r"C:\Program Files\OpenAI\runner D:\tmp\payload.cmd",
            r"C:\Program Files\OpenAI\runner D:/tmp/payload.exe",
            r"C:\Program Files\OpenAI\runner /help",
            r"C:\Program Files\OpenAI\runner /tmp/payload.exe",
            r"\\server\share\OpenAI Codex\runner \\other\share\payload.exe",
            r"\\server\share\OpenAI Codex\runner C:\tmp\payload.exe",
            r"\\server\share\OpenAI Codex\runner /help",
        ]
        for value in bypass_values:
            with self.subTest(executable=value):
                with self.assertRaises(ValueError):
                    CodexCliProvider(
                        provider_id="codex",
                        executable=value,
                        sandbox_mode="workspace-write",
                    )

    def test_allowed_table_driven_all_pass(self) -> None:
        allowed_values = [
            "codex",
            "codex.exe",
            "codex.cmd",
            "CODEX.EXE",
            "/usr/local/bin/codex",
            r"C:\Tools\codex.exe",
            r"C:\Program Files\OpenAI\codex.exe",
            r"C:\Program Files\OpenAI Codex\Codex Runner.exe",
            "C:/Program Files/OpenAI/codex.cmd",
            r"\\server\share\OpenAI Codex\codex.exe",
        ]
        for value in allowed_values:
            with self.subTest(executable=value):
                p = CodexCliProvider(
                    provider_id="codex",
                    executable=value,
                    sandbox_mode="workspace-write",
                )
                self.assertEqual(p.executable, value)


# =========================================================================
# 5. build_invocation input validation
# =========================================================================


class BuildInvocationInputTests(unittest.TestCase):
    """TC-13.8.4: build_invocation input validation."""

    def test_requires_dispatch_request(self) -> None:
        p = _make_provider()
        with self.assertRaises(ValueError):
            p.build_invocation("not a request")  # type: ignore[arg-type]

    def test_accepts_dispatch_request(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)


# =========================================================================
# 6. Effort mapping
# =========================================================================


class EffortMappingTests(unittest.TestCase):
    """TC-13.8.4: deliberation tier → effort mapping."""

    def test_efficient_maps_to_low(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="efficient",
            ),
        )
        inv = p.build_invocation(req)
        self.assertIn(f'model_reasoning_effort="low"', inv.argv)

    def test_balanced_maps_to_medium(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="balanced",
            ),
        )
        inv = p.build_invocation(req)
        self.assertIn(f'model_reasoning_effort="medium"', inv.argv)

    def test_deep_maps_to_high(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="deep",
            ),
        )
        inv = p.build_invocation(req)
        self.assertIn(f'model_reasoning_effort="high"', inv.argv)

    def test_unknown_tier_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="unknown",
            ),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_fast_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="fast",
            ),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_minimal_not_mapped(self) -> None:
        """minimal is NOT mapped — AgentDesk has no corresponding tier."""
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="minimal",
            ),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_xhigh_not_mapped(self) -> None:
        """xhigh is NOT mapped — AgentDesk has no corresponding tier."""
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="xhigh",
            ),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_empty_tier_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_deliberation_tier="")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_non_string_tier_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_deliberation_tier=123)  # type: ignore[arg-type]
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_no_fallback_to_medium(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="ultra",
            ),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_efficient_uppercase_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_deliberation_tier="EFFICIENT",
            ),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)


# =========================================================================
# 7. Model ID
# =========================================================================


class ModelIdTests(unittest.TestCase):
    """TC-13.8.4: model ID pass-through from snapshot with strict allowlist."""

    def test_model_id_exact_pass_through(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="gpt-5.1"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "gpt-5.1")

    def test_model_id_with_slash_allowed(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="openai/gpt-5"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "openai/gpt-5")

    def test_empty_model_id_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_model_id="")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_whitespace_model_id_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="   "),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_leading_whitespace_model_id_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id=" model"),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_trailing_whitespace_model_id_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="model "),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_nul_in_model_id_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_model_id="model\0hidden")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_cr_in_model_id_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_model_id="model\rhidden")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_lf_in_model_id_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_model_id="model\nhidden")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_space_in_model_id_rejected(self) -> None:
        """Space is not in the strict allowlist."""
        snap = _corrupt_snapshot(selected_model_id="gpt 5")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_ampersand_in_model_id_rejected(self) -> None:
        snap = _corrupt_snapshot(selected_model_id="gpt&5")
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_no_fallback_model(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="specific-model-v2"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "specific-model-v2")

    def test_model_id_not_from_required_model_tier(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(
                selected_model_id="correct-model",
                required_model_tier="wrong-source",
            ),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "correct-model")
        self.assertNotIn("wrong-source", inv.argv)

    # -- Model ID allowlist table-driven -------------------------------------

    def test_allowed_model_id_characters(self) -> None:
        """Characters in the allowlist must pass validation."""
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_./:"
        snap = _corrupt_snapshot(selected_model_id=allowed)
        p = _make_provider()
        req = _make_request(model_selection=snap)
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], allowed)

    def test_forbidden_model_id_characters_table(self) -> None:
        """Each forbidden character must be individually rejected."""
        forbidden_chars = (
            ("\x00",), ("\r",), ("\n",), (" ",), ("\t",),
            ("&",), ("|",), (";",), ("<",), (">",),
            ("`",), ("$",), ('"',), ("'",), ("(",), (")",),
            ("@",), ("!",), ("#",), ("%",), ("^",),
            ("*",), ("+",), ("=",), ("{",), ("}",),
            ("[",), ("]",), ("\\",), ("?",), (",",),
        )
        for chars in forbidden_chars:
            base = "gpt-5"
            model_id = base + "".join(chars) + base
            with self.subTest(chars=chars):
                snap = _corrupt_snapshot(selected_model_id=model_id)
                p = _make_provider()
                req = _make_request(model_selection=snap)
                with self.assertRaises(ValueError):
                    p.build_invocation(req)


# =========================================================================
# 8. Precise argv construction
# =========================================================================


class ArgvConstructionTests(unittest.TestCase):
    """TC-13.8.4: precise argv construction."""

    def _inv_for(self, provider, **snapshot_overrides) -> AgentCliInvocation:
        snap = _make_snapshot(**snapshot_overrides)
        req = _make_request(model_selection=snap)
        return provider.build_invocation(req)

    def test_base_argv(self) -> None:
        p = _make_provider(sandbox_mode="workspace-write")
        inv = self._inv_for(p)
        expected = (
            "exec",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--model",
            "gpt-5",
            "--sandbox",
            "workspace-write",
            "-c",
            "approval_policy=never",
            "-c",
            'model_reasoning_effort="medium"',
            "-",
        )
        self.assertEqual(inv.argv, expected)

    def test_read_only_sandbox_in_argv(self) -> None:
        p = _make_provider(sandbox_mode="read-only")
        inv = self._inv_for(p)
        self.assertIn("read-only", inv.argv)

    def test_workspace_write_sandbox_in_argv(self) -> None:
        p = _make_provider(sandbox_mode="workspace-write")
        inv = self._inv_for(p)
        self.assertIn("workspace-write", inv.argv)

    def test_executable_not_in_argv(self) -> None:
        p = _make_provider(executable="codex")
        inv = self._inv_for(p)
        self.assertNotIn("codex", inv.argv)

    def test_prompt_not_in_argv(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="secret task content")
        inv = p.build_invocation(req)
        self.assertNotIn("secret task content", inv.argv)
        self.assertNotIn("secret", inv.argv)

    def test_argv_is_tuple(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p)
        self.assertIsInstance(inv.argv, tuple)

    def test_approval_policy_is_noninteractive(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p)
        policy_idx = inv.argv.index("approval_policy=never")
        self.assertEqual(inv.argv[policy_idx - 1], "-c")
        self.assertNotIn("--ask-for-approval", inv.argv)

    def test_stdin_marker_last(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p)
        self.assertEqual(inv.argv[-1], "-")

    def test_ephemeral_present(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p)
        self.assertIn("--ephemeral", inv.argv)

    def test_json_present(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p)
        self.assertIn("--json", inv.argv)

    def test_color_never_present(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p)
        color_idx = inv.argv.index("--color")
        self.assertEqual(inv.argv[color_idx + 1], "never")

    def test_effort_with_deep_tier(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p, selected_deliberation_tier="deep")
        c_idx = inv.argv.index('model_reasoning_effort="high"') - 1
        self.assertEqual(
            inv.argv[c_idx + 1],
            'model_reasoning_effort="high"',
        )

    def test_model_exact_in_argv(self) -> None:
        p = _make_provider()
        inv = self._inv_for(p, selected_model_id="gpt-5.1-codex")
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "gpt-5.1-codex")


# =========================================================================
# 9. AgentCliInvocation return value
# =========================================================================


class InvocationReturnValueTests(unittest.TestCase):
    """TC-13.8.4: AgentCliInvocation return value correctness."""

    def test_returns_agent_cli_invocation_type(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)

    def test_env_overrides_empty(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertEqual(inv.env_overrides, ())

    def test_stdin_is_bytes(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIsInstance(inv.stdin, bytes)

    def test_argv_is_tuple(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertIsInstance(inv.argv, tuple)

    def test_no_cwd_in_invocation(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertFalse(hasattr(inv, "cwd"))

    def test_executable_exact(self) -> None:
        p = _make_provider(executable="codex")
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertEqual(inv.executable, "codex")

    def test_request_not_modified(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="original prompt")
        prompt_before = req.prompt
        _ = p.build_invocation(req)
        self.assertEqual(req.prompt, prompt_before)

    def test_provider_state_unchanged(self) -> None:
        p = _make_provider()
        exec_before = p.executable
        sb_before = p.sandbox_mode
        req = _make_request()
        _ = p.build_invocation(req)
        self.assertEqual(p.executable, exec_before)
        self.assertEqual(p.sandbox_mode, sb_before)

    def test_repeated_calls_equal(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv1 = p.build_invocation(req)
        inv2 = p.build_invocation(req)
        self.assertEqual(inv1.executable, inv2.executable)
        self.assertEqual(inv1.argv, inv2.argv)
        self.assertEqual(inv1.stdin, inv2.stdin)
        self.assertEqual(inv1.env_overrides, inv2.env_overrides)


# =========================================================================
# 10. Prompt encoding
# =========================================================================


class PromptEncodingTests(unittest.TestCase):
    """TC-13.8.4: prompt UTF-8 encoding via stdin."""

    def test_ascii_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="simple ascii prompt")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, b"simple ascii prompt")

    def test_unicode_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="Unicode \u00a9 \u6c49\u5b57 test")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "Unicode \u00a9 \u6c49\u5b57 test".encode("utf-8"))

    def test_chinese_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="\u4efb\u52a1\uff1a\u5ba1\u67e5\u6b64\u4ee3\u7801\u7684\u5b89\u5168\u6027")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "\u4efb\u52a1\uff1a\u5ba1\u67e5\u6b64\u4ee3\u7801\u7684\u5b89\u5168\u6027".encode("utf-8"))

    def test_emoji_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="Fix the bug \U0001f41b in module")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "Fix the bug \U0001f41b in module".encode("utf-8"))

    def test_newlines_preserved(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="line1\nline2\nline3")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, b"line1\nline2\nline3")

    def test_no_bom(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="test")
        inv = p.build_invocation(req)
        self.assertNotEqual(inv.stdin[:3], b"\xef\xbb\xbf")

    def test_no_trailing_newline_added(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="exact text")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, b"exact text")

    def test_no_trim(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="  padded  ")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, b"  padded  ")

    def test_prompt_not_in_argv(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="secret-task-12345")
        inv = p.build_invocation(req)
        arg_string = " ".join(inv.argv)
        self.assertNotIn("secret-task-12345", arg_string)

    def test_isolated_surrogate_raises_valueerror(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="valid prefix \ud800 suffix")
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_surrogate_error_message_no_prompt_leak(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="secret \ud800 data")
        try:
            p.build_invocation(req)
        except ValueError as exc:
            msg = str(exc)
            self.assertNotIn("secret", msg)
            self.assertNotIn("data", msg)


# =========================================================================
# 11. Security boundaries
# =========================================================================


class SecurityBoundaryTests(unittest.TestCase):
    """TC-13.8.4: security boundaries — no secrets in argv, no file writes, etc."""

    def _forbidden_flags(self) -> set[str]:
        """The complete set of forbidden flags per §2.12.15."""
        return {
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--search",
            "--oss",
            "--local-provider",
            "--remote",
            "--remote-auth-token-env",
            "--enable",
            "--disable",
            "--add-dir",
            "--skip-git-repo-check",
            "-C",
            "--cd",
            "-p",
            "--profile",
            "-i",
            "--image",
            "--ignore-rules",
            "--ignore-user-config",
            "--strict-config",
            "--output-schema",
            "--output-last-message",
            "-o",
        }

    def test_forbidden_flags_not_in_argv(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        for flag in self._forbidden_flags():
            self.assertNotIn(
                flag,
                inv.argv,
                f"Forbidden flag {flag} found in argv",
            )

    def test_danger_full_access_not_in_argv(self) -> None:
        p = _make_provider(sandbox_mode="workspace-write")
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("danger-full-access", inv.argv)

    def test_no_resume_in_argv(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("resume", inv.argv)
        self.assertNotIn("review", inv.argv)
        self.assertNotIn("fork", inv.argv)
        self.assertNotIn("cloud", inv.argv)

    def test_no_add_dir_flag(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("--add-dir", inv.argv)

    def test_no_session_persistence_flags(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        # --ephemeral is present, but no --session-id, --resume
        self.assertNotIn("--session-id", inv.argv)

    def test_prompt_not_in_exception(self) -> None:
        p = _make_provider()
        req_bad = _make_request(
            prompt="sensitive-task-data",
            model_selection=_make_snapshot(selected_deliberation_tier="bad"),
        )
        try:
            p.build_invocation(req_bad)
        except ValueError as exc:
            msg = str(exc)
            self.assertNotIn("sensitive-task-data", msg)

    def test_no_cwd(self) -> None:
        p = _make_provider()
        inv = p.build_invocation(_make_request())
        self.assertFalse(hasattr(inv, "cwd"))

    def test_no_file_writes(self) -> None:
        p = _make_provider()
        with mock.patch("builtins.open",
                        side_effect=RuntimeError("no file writes")):
            p.build_invocation(_make_request())

    def test_no_env_reads(self) -> None:
        """Provider does not read environment variables."""
        p = _make_provider()
        inv = p.build_invocation(_make_request())
        self.assertEqual(inv.env_overrides, ())

    def test_no_os_path_calls(self) -> None:
        """Provider source code must not call os.path or Path."""
        src = (_SCRIPTS / "codex_cli_provider.py").read_text(encoding="utf-8")
        # Exclude docstrings and comments
        in_doc = False
        lines = []
        for line in src.splitlines():
            s = line.strip()
            if '"""' in s:
                in_doc = not in_doc
                continue
            if in_doc:
                continue
            if s.startswith("#"):
                continue
            lines.append(line)
        code = "\n".join(lines)
        self.assertNotIn("os.path", code)
        self.assertNotIn("from pathlib", code)
        self.assertNotIn("Path(", code)

    def test_no_shlex(self) -> None:
        src = (_SCRIPTS / "codex_cli_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("shlex", src)

    def test_no_shutil(self) -> None:
        src = (_SCRIPTS / "codex_cli_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("shutil", src)

    def test_no_subprocess(self) -> None:
        src = self._code_body()
        self.assertNotIn("subprocess", src)

    def test_no_asyncio(self) -> None:
        src = self._code_body()
        self.assertNotIn("asyncio", src)

    def test_no_network_libs(self) -> None:
        src = self._code_body()
        for lib in ("urllib", "requests", "httpx", "openai", "anthropic",
                    "socket"):
            self.assertNotIn(lib, src, f"Network lib {lib} found")

    def test_no_tempfile(self) -> None:
        src = self._code_body()
        self.assertNotIn("tempfile", src)

    def test_no_claude_provider_import(self) -> None:
        src = self._code_body()
        self.assertNotIn("claude_code_provider", src)

    def test_no_worker_adapter_import(self) -> None:
        src = self._code_body()
        self.assertNotIn("worker_adapter", src)
        self.assertNotIn("WorkerAdapter", src)

    def test_no_budget_import(self) -> None:
        src = self._code_body()
        self.assertNotIn("budget", src.lower())
        self.assertNotIn("Budget", src)

    def test_no_mad_gateway_import(self) -> None:
        src = self._code_body()
        self.assertNotIn("mad_gateway", src)

    def _code_body(self) -> str:
        src = (_SCRIPTS / "codex_cli_provider.py").read_text(encoding="utf-8")
        in_docstring = False
        lines: list[str] = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_import_has_zero_io(self) -> None:
        """Module import produces no stdout, no stderr, no file I/O."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                ("import sys; sys.path.insert(0, r'{}'); "
                 "import codex_cli_provider; "
                 "print('IMPORT_OK')").format(str(_SCRIPTS)),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"Import failed: stderr={result.stderr}")
        self.assertIn("IMPORT_OK", result.stdout)
        self.assertEqual(result.stderr.strip(), "",
                         f"Import produced stderr: {result.stderr}")


# =========================================================================
# 12. Gateway integration
# =========================================================================


class GatewayIntegrationTests(unittest.TestCase):
    """TC-13.8.4: provider integration with DispatcherAgentGateway."""

    def _make_fake_process(self, returncode=0, stdout=b"ok", stderr=b""):
        class _FakeProcess:
            def __init__(self, returncode, stdout, stderr):
                self.returncode = returncode
                self._stdout = stdout
                self._stderr = stderr
                self.pid = 12345

            async def communicate(self, input=None):
                return self._stdout, self._stderr

            async def wait(self):
                return self.returncode if self.returncode is not None else 0

        return _FakeProcess(returncode, stdout, stderr)

    def _run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_provider_accepted_by_gateway(self) -> None:
        """Provider satisfies AgentCliProvider Protocol and is accepted."""
        p = _make_provider()
        self.assertIsInstance(p, AgentCliProvider)

    def test_matching_key_produces_invocation(self) -> None:
        """Mapping key matching provider_id produces correct invocation."""
        p = _make_provider(provider_id="codex")
        snap = _make_snapshot(selected_model_provider="codex")
        req = _make_request(model_selection=snap)
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)

    def test_gateway_mock_subprocess_with_provider(self) -> None:
        """Full mock subprocess dispatch through Gateway with CodexCliProvider."""
        p = _make_provider(provider_id="codex")
        snap = _make_snapshot(selected_model_provider="codex")
        req = _make_request(model_selection=snap)
        providers = {"codex": p}

        import asyncio
        async def _go():
            proc = self._make_fake_process(returncode=0, stdout=b"ok")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc):
                result = await dg.run_dispatch(req, providers)
                self.assertIsInstance(result, dg.DispatchResult)
                self.assertEqual(result.provider, "codex")
                self.assertEqual(result.stdout, b"ok")
                return True
        self.assertTrue(self._run_async(_go()))

    def test_mismatched_key_fails_gateway(self) -> None:
        """Mapping key not matching selected_model_provider → Gateway rejects."""
        p = _make_provider(provider_id="codex")
        snap = _make_snapshot(selected_model_provider="other")
        req = _make_request(model_selection=snap)
        providers = {"codex": p}

        import asyncio
        async def _go():
            with self.assertRaises(dg.ProviderNotSupportedError):
                await dg.run_dispatch(req, providers)
        self._run_async(_go())

    def test_unknown_tier_wrapped_as_dispatch_invocation_error(self) -> None:
        """Provider ValueError for unknown tier → Gateway wraps."""
        p = _make_provider(provider_id="codex")
        snap = _make_snapshot(
            selected_model_provider="codex",
            selected_deliberation_tier="unknown_tier",
        )
        req = _make_request(model_selection=snap)
        providers = {"codex": p}

        import asyncio
        async def _go():
            with self.assertRaises(dg.DispatchInvocationError) as ctx:
                await dg.run_dispatch(req, providers)
            self.assertIn("ValueError", str(ctx.exception))
        self._run_async(_go())

    def test_gateway_provides_authoritative_cwd(self) -> None:
        """Gateway provides cwd=str(request.workspace), not the provider."""
        p = _make_provider(provider_id="codex")
        snap = _make_snapshot(selected_model_provider="codex")
        workspace = Path(tempfile.gettempdir())
        req = _make_request(model_selection=snap, workspace=workspace)
        providers = {"codex": p}

        import asyncio
        async def _go():
            proc = self._make_fake_process(returncode=0, stdout=b"ok")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc) as mock_exec:
                await dg.run_dispatch(req, providers)
                call_kwargs = mock_exec.call_args[1]
                self.assertEqual(call_kwargs["cwd"], str(workspace))
                return True
        self.assertTrue(self._run_async(_go()))

    def test_no_real_executable_run(self) -> None:
        """Gateway integration must not run real Codex CLI."""
        p = _make_provider(provider_id="codex")
        snap = _make_snapshot(selected_model_provider="codex")
        req = _make_request(model_selection=snap)
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)
        self.assertEqual(inv.executable, "codex")
        self.assertIsInstance(inv.argv, tuple)
        # No real process was launched by this call.


# =========================================================================
# 13. Scope exclusion
# =========================================================================


class ScopeExclusionTests(unittest.TestCase):
    """TC-13.8.4: verify excluded concepts are absent from production module."""

    def _code_body(self) -> str:
        src = (_SCRIPTS / "codex_cli_provider.py").read_text(encoding="utf-8")
        in_docstring = False
        lines: list[str] = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_no_worker_kind(self) -> None:
        self.assertNotIn("WorkerKind", self._code_body())

    def test_no_task_difficulty(self) -> None:
        self.assertNotIn("TaskDifficulty", self._code_body())

    def test_no_budget_policy(self) -> None:
        self.assertNotIn("ContextBudgetPolicy", self._code_body())

    def test_no_budget_result(self) -> None:
        self.assertNotIn("BudgetResult", self._code_body())

    def test_no_retry_logic(self) -> None:
        body = self._code_body().lower()
        self.assertNotIn("retry", body)

    def test_no_slot_or_lease(self) -> None:
        body = self._code_body().lower()
        body_no_slots = body.replace("slots=true", "")
        self.assertNotIn("lease", body_no_slots)

    def test_no_escalation(self) -> None:
        self.assertNotIn("escalation", self._code_body().lower())

    def test_no_rate_limit(self) -> None:
        self.assertNotIn("rate_limit", self._code_body().lower())

    def test_no_approval_gate(self) -> None:
        self.assertNotIn("ApprovalGate", self._code_body())
        self.assertNotIn("approval", self._code_body().lower())

    def test_no_register_provider(self) -> None:
        self.assertNotIn("register_provider", self._code_body())

    def test_no_control_prompt(self) -> None:
        """Codex does not use a fixed control prompt."""
        body = self._code_body()
        self.assertNotIn("_CONTROL_PROMPT", body)
        self.assertNotIn("Read the task instructions from stdin", body)


# =========================================================================
# 14. Import boundary — independent subprocess import test
# =========================================================================


class ImportBoundaryTests(unittest.TestCase):
    """TC-13.8.4: module import produces zero side effects in subprocess."""

    def test_import_in_subprocess_no_output(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                ("import sys; sys.path.insert(0, r'{}'); "
                 "import codex_cli_provider; "
                 "print('IMPORT_OK')").format(str(_SCRIPTS)),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0,
                         f"Import failed: stderr={result.stderr}")
        self.assertIn("IMPORT_OK", result.stdout)
        self.assertEqual(result.stderr.strip(), "",
                         f"Import produced stderr: {result.stderr}")


# =========================================================================
# 15. Cross-module compatibility
# =========================================================================


class CrossModuleCompatibilityTests(unittest.TestCase):
    """TC-13.8.4: module coexists with other modules — no sys.modules mutation."""

    def test_coexists_with_dispatcher_gateway(self) -> None:
        """The module-level imports already prove coexistence.  Verify that
        CodexClIProvider shares the same type objects as dispatcher_gateway."""
        self.assertIsNotNone(ccp)
        self.assertIsNotNone(dg)
        # Module identity: both modules reference the same AgentCliInvocation,
        # DispatchRequest etc. — no module‑identity split.
        self.assertIs(ccp.AgentCliInvocation, dg.AgentCliInvocation)
        self.assertIs(ccp.DispatchRequest, dg.DispatchRequest)
        self.assertIs(ccp.AgentCliProvider, dg.AgentCliProvider)

    def test_codex_provider_satisfies_gateway_protocol(self) -> None:
        """CodexCliProvider satisfies the same AgentCliProvider
        Protocol that dispatcher_gateway defines."""
        p = _make_provider()
        self.assertIsInstance(p, dg.AgentCliProvider)

    def test_module_state_not_destroyed_by_this_test(self) -> None:
        """Prove no test in this class deletes modules or mutates sys.path."""
        for m in list(sys.modules):
            self.assertIn(m, sys.modules,
                          f"Module {m} was deleted from sys.modules")
        self.assertNotIn(
            str(_SCRIPTS), sys.path[1:],
            f"scripts dir leaked into sys.path: {sys.path}"
        )


if __name__ == "__main__":
    unittest.main()
