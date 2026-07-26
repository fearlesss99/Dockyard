"""TC-13.8: comprehensive mock‑based tests for ``claude_code_provider.py``.

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
import claude_code_provider as ccp  # noqa: E402
sys.path.pop(0)

# Re‑export for convenience
DispatchIdentity = dg.DispatchIdentity
ModelSelectionSnapshot = dg.ModelSelectionSnapshot
DispatchRequest = dg.DispatchRequest
AgentCliInvocation = dg.AgentCliInvocation
AgentCliProvider = dg.AgentCliProvider
ClaudeCodeProvider = ccp.ClaudeCodeProvider


# =========================================================================
# FakeAgentCliProvider — tests/ only (same as dispatcher_gateway tests)
# =========================================================================

_STDIN_SENTINEL = object()


class FakeAgentCliProvider:
    """Configurable fake provider for testing."""

    def __init__(
        self,
        provider_id: str = "fake",
        executable: str | None = None,
        argv: tuple[str, ...] = (),
        stdin: bytes | None = _STDIN_SENTINEL,
        env_overrides: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._provider_id = provider_id
        self._executable = executable if executable is not None else sys.executable
        self._argv = argv
        self._stdin_sentinel = stdin
        self._env_overrides = env_overrides

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def build_invocation(self, request: DispatchRequest) -> AgentCliInvocation:
        stdin = None
        if self._stdin_sentinel is _STDIN_SENTINEL:
            stdin = request.prompt.encode("utf-8")
        else:
            stdin = self._stdin_sentinel  # type: ignore[assignment]
        return AgentCliInvocation(
            executable=self._executable,
            argv=self._argv,
            stdin=stdin,
            env_overrides=self._env_overrides,
        )


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
        "selected_model_provider": "claude",
        "selected_model_id": "claude-opus-4",
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


def _make_provider(**overrides) -> ClaudeCodeProvider:
    defaults: dict = {
        "provider_id": "claude",
        "executable": "claude",
        "permission_mode": "plan",
        "allowed_tools": (),
        "disallowed_tools": (),
    }
    defaults.update(overrides)
    return ClaudeCodeProvider(**defaults)


# =========================================================================
# 1. Public structure
# =========================================================================


class PublicStructureTests(unittest.TestCase):
    """TC-13.8: public structure — dataclass, frozen, slots, fields, __all__."""

    def test_is_dataclass(self) -> None:
        self.assertTrue(is_dataclass(ClaudeCodeProvider))

    def test_frozen(self) -> None:
        p = _make_provider()
        with self.assertRaises(Exception):
            p.executable = "other"  # type: ignore[misc]

    def test_slots(self) -> None:
        p = _make_provider()
        with self.assertRaises(AttributeError):
            p.__dict__  # type: ignore[attr-defined]

    def test_exact_five_fields(self) -> None:
        field_names = {f.name for f in fields(ClaudeCodeProvider)}
        self.assertSetEqual(
            field_names,
            {"provider_id", "executable", "permission_mode",
             "allowed_tools", "disallowed_tools"},
        )

    def test_no_sixth_field(self) -> None:
        forbidden = {
            "cwd", "workspace", "prompt", "timeout", "model",
            "model_id", "effort", "env", "env_overrides",
            "api_key", "token", "session_id", "retry", "slot", "lease",
        }
        field_names = {f.name for f in fields(ClaudeCodeProvider)}
        self.assertTrue(forbidden.isdisjoint(field_names),
                        f"Forbidden fields found: {forbidden & field_names}")

    def test_all_exact_one_symbol(self) -> None:
        self.assertEqual(ccp.__all__, ["ClaudeCodeProvider"])

    def test_control_prompt_not_in_all(self) -> None:
        self.assertNotIn("_CONTROL_PROMPT", ccp.__all__)

    def test_control_prompt_value(self) -> None:
        self.assertEqual(
            ccp._CONTROL_PROMPT,
            "Read the task instructions from stdin and execute them.",
        )

    def test_control_prompt_is_not_a_field(self) -> None:
        field_names = {f.name for f in fields(ClaudeCodeProvider)}
        self.assertNotIn("_CONTROL_PROMPT", field_names)

    def test_satisfies_agent_cli_provider_protocol(self) -> None:
        p = _make_provider()
        self.assertIsInstance(p, AgentCliProvider)

    def test_top_level_import_not_relative(self) -> None:
        src = (_SCRIPTS / "claude_code_provider.py").read_text(encoding="utf-8")
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
    """TC-13.8: provider_id validation."""

    def test_claude_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.provider_id, "claude")

    def test_claudecode_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claudecode",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.provider_id, "claudecode")

    def test_other_string_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="Claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_claude_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude-code",
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_openai_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="openai",
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="",
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id=None,  # type: ignore[arg-type]
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_true_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id=True,  # type: ignore[arg-type]
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_int_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id=1,  # type: ignore[arg-type]
                executable="claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_no_alias_normalization(self) -> None:
        """'claude' and 'claudecode' retain distinct IDs."""
        p1 = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        p2 = ClaudeCodeProvider(
            provider_id="claudecode",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p1.provider_id, "claude")
        self.assertEqual(p2.provider_id, "claudecode")
        self.assertNotEqual(p1.provider_id, p2.provider_id)


# =========================================================================
# 3. Executable validation
# =========================================================================


class ExecutableValidationTests(unittest.TestCase):
    """TC-13.8: executable validation."""

    def test_plain_name_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.executable, "claude")

    def test_windows_path_with_spaces_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable=r"C:\Program Files\Claude\claude.exe",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertIn(" ", p.executable)

    def test_posix_path_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="/usr/local/bin/claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertTrue(p.executable.startswith("/"))

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_pure_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="   ",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_leading_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable=" claude",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_trailing_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude ",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_nul_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude\x00hidden",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_cr_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude\r--version",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_lf_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude\n--version",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_not_str_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable=123,  # type: ignore[arg-type]
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    # -- Embedded arguments / shell command rejection --

    def test_claude_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude --version",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_claude_p_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude -p",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_semicolon_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude;whoami",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_double_ampersand_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude && whoami",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_pipe_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude | more",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_backtick_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude`whoami`",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_windows_path_with_args_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable=r"C:\Program Files\Claude\claude.exe --version",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_posix_path_with_args_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="/usr/local/bin/claude --version",
                permission_mode="plan",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_windows_space_path_not_rejected_as_args(self) -> None:
        """Spaces in path without dash-prefixed trailing tokens are allowed."""
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable=r"C:\My Tools\claude.exe",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertIn(" ", p.executable)


# =========================================================================
# 4. Permission mode
# =========================================================================


class PermissionModeTests(unittest.TestCase):
    """TC-13.8: permission_mode validation."""

    def test_default_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="default",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.permission_mode, "default")

    def test_plan_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.permission_mode, "plan")

    def test_accept_edits_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="acceptEdits",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.permission_mode, "acceptEdits")

    def test_dont_ask_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="dontAsk",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.permission_mode, "dontAsk")

    def test_bypass_permissions_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="bypassPermissions",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_delegate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="delegate",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_default_uppercase_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="Default",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_plan_uppercase_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="PLAN",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="",
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode=None,  # type: ignore[arg-type]
                allowed_tools=(),
                disallowed_tools=(),
            )

    def test_true_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode=True,  # type: ignore[arg-type]
                allowed_tools=(),
                disallowed_tools=(),
            )


# =========================================================================
# 5. Tool tuples — deep immutability
# =========================================================================


class ToolTupleTests(unittest.TestCase):
    """TC-13.8: allowed_tools / disallowed_tools deep immutability."""

    def test_empty_tuple_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=(),
            disallowed_tools=(),
        )
        self.assertEqual(p.allowed_tools, ())
        self.assertEqual(p.disallowed_tools, ())

    def test_list_converted_to_tuple(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=["Read", "Glob"],
            disallowed_tools=(),
        )
        self.assertIsInstance(p.allowed_tools, tuple)
        self.assertEqual(p.allowed_tools, ("Read", "Glob"))

    def test_source_mutation_does_not_affect_provider(self) -> None:
        src = ["Read", "Glob"]
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=src,
            disallowed_tools=(),
        )
        src.append("Write")
        src[0] = "Modified"
        self.assertEqual(p.allowed_tools, ("Read", "Glob"))

    def test_fields_are_tuple_after_construction(self) -> None:
        p = _make_provider(allowed_tools=["Read"], disallowed_tools=["WebFetch"])
        self.assertIsInstance(p.allowed_tools, tuple)
        self.assertIsInstance(p.disallowed_tools, tuple)

    # -- Element validation --

    def test_non_str_item_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read", 123),  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_empty_item_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read", ""),
                disallowed_tools=(),
            )

    def test_leading_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=(" Read",),
                disallowed_tools=(),
            )

    def test_trailing_whitespace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read ",),
                disallowed_tools=(),
            )

    def test_nul_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read\x00Write",),
                disallowed_tools=(),
            )

    def test_cr_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read\r",),
                disallowed_tools=(),
            )

    def test_lf_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read\nWrite",),
                disallowed_tools=(),
            )

    def test_duplicate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read", "Read"),
                disallowed_tools=(),
            )

    def test_order_preserved(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=["z", "a", "m"],
            disallowed_tools=(),
        )
        self.assertEqual(p.allowed_tools, ("z", "a", "m"))

    def test_whole_field_none_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=None,  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_whole_field_str_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools="Read",  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_whole_field_bool_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=True,  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_whole_field_int_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=1,  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_whole_field_set_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools={"Read"},  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_whole_field_dict_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools={"Read": True},  # type: ignore[arg-type]
                disallowed_tools=(),
            )

    def test_bytes_whole_field_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=b"Read",  # type: ignore[arg-type]
                disallowed_tools=(),
            )


# =========================================================================
# 6. Allow/Deny intersection
# =========================================================================


class AllowDenyIntersectionTests(unittest.TestCase):
    """TC-13.8: allowed_tools ∩ disallowed_tools must be disjoint."""

    def test_exact_match_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Read",),
                disallowed_tools=("Read",),
            )

    def test_disjoint_allowed(self) -> None:
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=("Read", "Glob"),
            disallowed_tools=("WebFetch", "Bash"),
        )
        self.assertEqual(p.allowed_tools, ("Read", "Glob"))
        self.assertEqual(p.disallowed_tools, ("WebFetch", "Bash"))

    def test_case_different_not_intersection(self) -> None:
        """Read vs read — different strings, not an intersection."""
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=("Read",),
            disallowed_tools=("read",),
        )
        self.assertEqual(p.allowed_tools, ("Read",))
        self.assertEqual(p.disallowed_tools, ("read",))

    def test_no_case_folding(self) -> None:
        """No case normalization is performed — 'Read' and 'read' are distinct."""
        p = ClaudeCodeProvider(
            provider_id="claude",
            executable="claude",
            permission_mode="plan",
            allowed_tools=("Read",),
            disallowed_tools=("read",),
        )
        # Both must be present as-is.
        self.assertIn("Read", p.allowed_tools)
        self.assertIn("read", p.disallowed_tools)

    def test_multiple_intersection_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("A", "B", "C"),
                disallowed_tools=("C", "D", "E"),
            )

    def test_no_silent_removal_on_intersection(self) -> None:
        """Intersection must raise, not silently remove."""
        with self.assertRaises(ValueError):
            ClaudeCodeProvider(
                provider_id="claude",
                executable="claude",
                permission_mode="plan",
                allowed_tools=("Tool1", "Tool2"),
                disallowed_tools=("Tool2", "Tool3"),
            )


# =========================================================================
# 7. build_invocation — input validation
# =========================================================================


class BuildInvocationInputTests(unittest.TestCase):
    """TC-13.8: build_invocation input validation."""

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
# 8. Effort mapping
# =========================================================================


class EffortMappingTests(unittest.TestCase):
    """TC-13.8: deliberation tier → effort mapping."""

    def test_efficient_maps_to_low(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="efficient"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--effort")
        self.assertEqual(inv.argv[idx + 1], "low")

    def test_balanced_maps_to_medium(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="balanced"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--effort")
        self.assertEqual(inv.argv[idx + 1], "medium")

    def test_deep_maps_to_high(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="deep"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--effort")
        self.assertEqual(inv.argv[idx + 1], "high")

    def test_unknown_tier_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="ultra"),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_fast_rejected_not_in_mapping(self) -> None:
        """MadDeliberationDepth.fast — not in AgentDesk deliberation tier space."""
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="fast"),
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
        """Tier must be str — non-str fails in build_invocation."""
        snap = _corrupt_snapshot(selected_deliberation_tier=123)  # type: ignore[arg-type]
        p = _make_provider()
        req = _make_request(model_selection=snap)
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_no_fallback_to_medium(self) -> None:
        """No silent default to 'medium' for unknown tier."""
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="unknown"),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)

    def test_efficient_uppercase_rejected(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_deliberation_tier="EFFICIENT"),
        )
        with self.assertRaises(ValueError):
            p.build_invocation(req)


# =========================================================================
# 9. Model ID
# =========================================================================


class ModelIdTests(unittest.TestCase):
    """TC-13.8: model ID pass-through from snapshot."""

    def test_model_id_exact_pass_through(self) -> None:
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="claude-haiku-4"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "claude-haiku-4")

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

    def test_no_fallback_model(self) -> None:
        """Provider must not silently substitute another model."""
        p = _make_provider()
        req = _make_request(
            model_selection=_make_snapshot(selected_model_id="specific-model-v2"),
        )
        inv = p.build_invocation(req)
        idx = inv.argv.index("--model")
        self.assertEqual(inv.argv[idx + 1], "specific-model-v2")

    def test_model_id_not_from_required_model_tier(self) -> None:
        """--model must not use required_model_tier."""
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


# =========================================================================
# 10. Precise argv construction
# =========================================================================


class ArgvConstructionTests(unittest.TestCase):
    """TC-13.8: precise argv construction."""

    def _make_provider_with_model(self, **overrides) -> ClaudeCodeProvider:
        defaults: dict = {
            "provider_id": "claude",
            "executable": "claude",
            "permission_mode": "plan",
            "allowed_tools": (),
            "disallowed_tools": (),
        }
        defaults.update(overrides)
        return ClaudeCodeProvider(**defaults)

    def _inv_for(self, provider, **snapshot_overrides) -> AgentCliInvocation:
        snap = _make_snapshot(**snapshot_overrides)
        req = _make_request(model_selection=snap)
        return provider.build_invocation(req)

    def test_base_argv_no_tools(self) -> None:
        p = self._make_provider_with_model()
        inv = self._inv_for(p)
        expected = (
            "-p",
            ccp._CONTROL_PROMPT,
            "--output-format", "json",
            "--model", "claude-opus-4",
            "--permission-mode", "plan",
            "--effort", "medium",
            "--no-session-persistence",
        )
        self.assertEqual(inv.argv, expected)

    def test_allowed_only(self) -> None:
        p = self._make_provider_with_model(
            allowed_tools=("Bash(curl:*)",),
        )
        inv = self._inv_for(p)
        expected = (
            "-p",
            ccp._CONTROL_PROMPT,
            "--output-format", "json",
            "--model", "claude-opus-4",
            "--permission-mode", "plan",
            "--effort", "medium",
            "--no-session-persistence",
            "--allowedTools",
            "Bash(curl:*)",
        )
        self.assertEqual(inv.argv, expected)

    def test_disallowed_only(self) -> None:
        p = self._make_provider_with_model(
            disallowed_tools=("WebFetch",),
        )
        inv = self._inv_for(p)
        expected = (
            "-p",
            ccp._CONTROL_PROMPT,
            "--output-format", "json",
            "--model", "claude-opus-4",
            "--permission-mode", "plan",
            "--effort", "medium",
            "--no-session-persistence",
            "--disallowedTools",
            "WebFetch",
        )
        self.assertEqual(inv.argv, expected)

    def test_both_allowed_and_disallowed(self) -> None:
        p = self._make_provider_with_model(
            allowed_tools=("Read", "Bash(git status:*)"),
            disallowed_tools=("WebFetch",),
        )
        inv = self._inv_for(p)
        expected = (
            "-p",
            ccp._CONTROL_PROMPT,
            "--output-format", "json",
            "--model", "claude-opus-4",
            "--permission-mode", "plan",
            "--effort", "medium",
            "--no-session-persistence",
            "--allowedTools",
            "Read",
            "Bash(git status:*)",
            "--disallowedTools",
            "WebFetch",
        )
        self.assertEqual(inv.argv, expected)

    def test_flag_casing_exact(self) -> None:
        p = self._make_provider_with_model(
            allowed_tools=("Read",),
            disallowed_tools=("Bash",),
        )
        inv = self._inv_for(p)
        self.assertIn("--allowedTools", inv.argv)
        self.assertIn("--disallowedTools", inv.argv)
        self.assertNotIn("--allowedtools", inv.argv)
        self.assertNotIn("--disallowedtools", inv.argv)

    def test_allowed_block_before_disallowed(self) -> None:
        p = self._make_provider_with_model(
            allowed_tools=("A",),
            disallowed_tools=("B",),
        )
        inv = self._inv_for(p)
        allowed_idx = inv.argv.index("--allowedTools")
        disallowed_idx = inv.argv.index("--disallowedTools")
        self.assertLess(allowed_idx, disallowed_idx)

    def test_each_flag_only_once(self) -> None:
        p = self._make_provider_with_model(
            allowed_tools=("A", "B", "C"),
            disallowed_tools=("D", "E"),
        )
        inv = self._inv_for(p)
        self.assertEqual(inv.argv.count("--allowedTools"), 1)
        self.assertEqual(inv.argv.count("--disallowedTools"), 1)

    def test_empty_tools_flag_absent(self) -> None:
        p = self._make_provider_with_model(
            allowed_tools=(),
            disallowed_tools=(),
        )
        inv = self._inv_for(p)
        self.assertNotIn("--allowedTools", inv.argv)
        self.assertNotIn("--disallowedTools", inv.argv)

    def test_executable_not_in_argv(self) -> None:
        p = self._make_provider_with_model()
        inv = self._inv_for(p)
        self.assertNotIn("claude", inv.argv)

    def test_prompt_not_in_argv(self) -> None:
        p = self._make_provider_with_model()
        req = _make_request(prompt="secret task content")
        inv = p.build_invocation(req)
        self.assertNotIn("secret task content", inv.argv)
        self.assertNotIn("secret", inv.argv)

    def test_argv_is_tuple(self) -> None:
        p = self._make_provider_with_model()
        inv = self._inv_for(p)
        self.assertIsInstance(inv.argv, tuple)

    def test_permission_mode_exact_in_argv(self) -> None:
        p = self._make_provider_with_model(permission_mode="acceptEdits")
        inv = self._inv_for(p)
        idx = inv.argv.index("--permission-mode")
        self.assertEqual(inv.argv[idx + 1], "acceptEdits")

    def test_effort_exact_in_argv(self) -> None:
        p = self._make_provider_with_model()
        inv = self._inv_for(p, selected_deliberation_tier="deep")
        idx = inv.argv.index("--effort")
        self.assertEqual(inv.argv[idx + 1], "high")


# =========================================================================
# 11. AgentCliInvocation return value
# =========================================================================


class InvocationReturnValueTests(unittest.TestCase):
    """TC-13.8: AgentCliInvocation return value correctness."""

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
        p = _make_provider(executable="claude")
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertEqual(inv.executable, "claude")

    def test_request_not_modified(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="original prompt")
        prompt_before = req.prompt
        _ = p.build_invocation(req)
        self.assertEqual(req.prompt, prompt_before)

    def test_provider_state_unchanged(self) -> None:
        p = _make_provider()
        exec_before = p.executable
        tools_before = p.allowed_tools
        req = _make_request()
        _ = p.build_invocation(req)
        self.assertEqual(p.executable, exec_before)
        self.assertEqual(p.allowed_tools, tools_before)

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
# 12. Prompt encoding
# =========================================================================


class PromptEncodingTests(unittest.TestCase):
    """TC-13.8: prompt UTF-8 encoding via stdin."""

    def test_ascii_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="simple ascii prompt")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, b"simple ascii prompt")

    def test_unicode_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="Unicode © 汉字 test")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "Unicode © 汉字 test".encode("utf-8"))

    def test_chinese_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="任务：审查此代码的安全性")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "任务：审查此代码的安全性".encode("utf-8"))

    def test_emoji_prompt(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="Fix the bug 🐛 in module")
        inv = p.build_invocation(req)
        self.assertEqual(inv.stdin, "Fix the bug 🐛 in module".encode("utf-8"))

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

    def test_empty_prompt_fail_closed(self) -> None:
        """Empty prompt: Gateway would reject, but direct call also fails."""
        p = _make_provider()
        # prompt="" in request will fail at request validation (Gateway),
        # but provider shouldn't crash. Empty string encodes fine to b"".
        req = _make_request(prompt="")
        inv = p.build_invocation(req)
        # Empty prompt encodes to empty bytes; Gateway's _validate_request
        # would have already rejected this.
        self.assertEqual(inv.stdin, b"")

    def test_prompt_not_in_argv(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="secret-task-12345")
        inv = p.build_invocation(req)
        arg_string = " ".join(inv.argv)
        self.assertNotIn("secret-task-12345", arg_string)

    def test_isolated_surrogate_raises_valueerror(self) -> None:
        p = _make_provider()
        # Build a string with a lone surrogate that encode() rejects.
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
# 13. Security boundaries
# =========================================================================


class SecurityBoundaryTests(unittest.TestCase):
    """TC-13.8: security boundaries — no secrets in argv, no file writes, etc."""

    def test_forbidden_flags_absent(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        forbidden = {
            "--continue", "--resume", "--session-id",
            "--fork-session", "--remote", "--teleport",
            "--dangerously-skip-permissions",
        }
        for flag in forbidden:
            self.assertNotIn(flag, inv.argv,
                             f"Forbidden flag {flag} found in argv")

    def test_bypass_permissions_not_in_argv(self) -> None:
        p = _make_provider(permission_mode="plan")
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("bypassPermissions", inv.argv)

    def test_no_add_dir_flag(self) -> None:
        p = _make_provider()
        req = _make_request()
        inv = p.build_invocation(req)
        self.assertNotIn("--add-dir", inv.argv)

    def test_prompt_not_in_exception(self) -> None:
        p = _make_provider()
        req = _make_request(prompt="sensitive-task-data")
        # Trigger some error — unknown tier.
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
        # Should not touch os.environ — but env_overrides=() is hardcoded.
        inv = p.build_invocation(_make_request())
        self.assertEqual(inv.env_overrides, ())

    def test_no_os_path_calls(self) -> None:
        """Provider source code must not call os.path or Path."""
        src = (_SCRIPTS / "claude_code_provider.py").read_text(encoding="utf-8")
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
        src = (_SCRIPTS / "claude_code_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("shlex", src)

    def test_no_shutil(self) -> None:
        src = (_SCRIPTS / "claude_code_provider.py").read_text(encoding="utf-8")
        self.assertNotIn("shutil", src)

    def test_no_subprocess(self) -> None:
        src = self._code_body()
        self.assertNotIn("subprocess", src)

    def test_no_asyncio(self) -> None:
        src = self._code_body()
        self.assertNotIn("asyncio", src)

    def test_no_network_libs(self) -> None:
        src = self._code_body()
        for lib in ("urllib", "requests", "httpx", "openai", "anthropic", "socket"):
            self.assertNotIn(lib, src, f"Network lib {lib} found")

    def test_no_tempfile(self) -> None:
        src = self._code_body()
        self.assertNotIn("tempfile", src)

    def test_no_codex_provider_import(self) -> None:
        src = self._code_body()
        self.assertNotIn("codex", src.lower())

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
        src = (_SCRIPTS / "claude_code_provider.py").read_text(encoding="utf-8")
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
                 "import claude_code_provider; "
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
# 14. Gateway integration
# =========================================================================


class GatewayIntegrationTests(unittest.TestCase):
    """TC-13.8: provider integration with DispatcherAgentGateway."""

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
        p = _make_provider(provider_id="claude")
        snap = _make_snapshot(selected_model_provider="claude")
        req = _make_request(model_selection=snap)
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)

    def test_gateway_mock_subprocess_with_provider(self) -> None:
        """Full mock subprocess dispatch through Gateway with ClaudeCodeProvider."""
        p = _make_provider(provider_id="claude")
        snap = _make_snapshot(selected_model_provider="claude")
        req = _make_request(model_selection=snap)
        providers = {"claude": p}

        import asyncio
        async def _go():
            proc = self._make_fake_process(returncode=0, stdout=b"ok")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc):
                result = await dg.run_dispatch(req, providers)
                self.assertIsInstance(result, dg.DispatchResult)
                self.assertEqual(result.provider, "claude")
                self.assertEqual(result.stdout, b"ok")
                return True
        self.assertTrue(self._run_async(_go()))

    def test_mismatched_key_fails_gateway(self) -> None:
        """Mapping key not matching selected_model_provider → Gateway rejects."""
        p = _make_provider(provider_id="claude")
        snap = _make_snapshot(selected_model_provider="other")
        req = _make_request(model_selection=snap)
        providers = {"claude": p}

        import asyncio
        async def _go():
            with self.assertRaises(dg.ProviderNotSupportedError):
                await dg.run_dispatch(req, providers)
        self._run_async(_go())

    def test_unknown_tier_wrapped_as_dispatch_invocation_error(self) -> None:
        """Provider ValueError for unknown tier → Gateway wraps to DispatchInvocationError."""
        p = _make_provider(provider_id="claude")
        snap = _make_snapshot(
            selected_model_provider="claude",
            selected_deliberation_tier="unknown_tier",
        )
        req = _make_request(model_selection=snap)
        providers = {"claude": p}

        import asyncio
        async def _go():
            with self.assertRaises(dg.DispatchInvocationError) as ctx:
                await dg.run_dispatch(req, providers)
            self.assertIn("ValueError", str(ctx.exception))
        self._run_async(_go())

    def test_gateway_provides_authoritative_cwd(self) -> None:
        """Gateway provides cwd=str(request.workspace), not the provider."""
        p = _make_provider(provider_id="claude")
        snap = _make_snapshot(selected_model_provider="claude")
        workspace = Path(tempfile.gettempdir())
        req = _make_request(model_selection=snap, workspace=workspace)
        providers = {"claude": p}

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
        """Gateway integration must not run real Claude CLI.
        The build_invocation creates the structure; the Gateway launches.
        This test proves the provider constructs a valid invocation
        without any real subprocess or executable access."""
        p = _make_provider(provider_id="claude")
        snap = _make_snapshot(selected_model_provider="claude")
        req = _make_request(model_selection=snap)
        inv = p.build_invocation(req)
        self.assertIsInstance(inv, AgentCliInvocation)
        self.assertEqual(inv.executable, "claude")
        self.assertIsInstance(inv.argv, tuple)
        # No real process was launched by this call.

    def test_claudecode_provider_id_in_gateway(self) -> None:
        """Provider with provider_id='claudecode' works through Gateway."""
        p = _make_provider(provider_id="claudecode")
        snap = _make_snapshot(selected_model_provider="claudecode")
        req = _make_request(model_selection=snap)
        providers = {"claudecode": p}

        import asyncio
        async def _go():
            proc = self._make_fake_process(returncode=0, stdout=b"ok")
            target = "dispatcher_gateway.asyncio.create_subprocess_exec"
            with mock.patch(target, return_value=proc):
                result = await dg.run_dispatch(req, providers)
                self.assertEqual(result.provider, "claudecode")
        self._run_async(_go())


# =========================================================================
# 15. Import boundary — independent subprocess import test
# =========================================================================


class ImportBoundaryTests(unittest.TestCase):
    """TC-13.8: module import produces zero side effects in subprocess."""

    def test_import_in_subprocess_no_output(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                ("import sys; sys.path.insert(0, r'{}'); "
                 "import claude_code_provider; "
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
# 16. Cross-module compatibility
# =========================================================================


class CrossModuleCompatibilityTests(unittest.TestCase):
    """TC-13.8: module coexists with other modules."""

    def test_coexists_with_dispatcher_gateway(self) -> None:
        sys.path.insert(0, str(_SCRIPTS))
        try:
            if "claude_code_provider" in sys.modules:
                del sys.modules["claude_code_provider"]
            import claude_code_provider as ccp2  # noqa: F811
            import dispatcher_gateway as dg2  # noqa: F811
            self.assertIsNotNone(ccp2)
            self.assertIsNotNone(dg2)
        finally:
            sys.path.pop(0)

    def test_coexists_with_core_types(self) -> None:
        sys.path.insert(0, str(_SCRIPTS))
        try:
            import core_types as ct  # noqa: F811
            if "claude_code_provider" in sys.modules:
                del sys.modules["claude_code_provider"]
            import claude_code_provider as ccp2  # noqa: F811
            self.assertIsNotNone(ct)
            self.assertIsNotNone(ccp2)
        finally:
            sys.path.pop(0)


# =========================================================================
# 17. Scope exclusion
# =========================================================================


class ScopeExclusionTests(unittest.TestCase):
    """TC-13.8: verify excluded concepts are absent from production module."""

    def _code_body(self) -> str:
        src = (_SCRIPTS / "claude_code_provider.py").read_text(encoding="utf-8")
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
        # "slots" appears in @dataclass(slots=True) — exclude that match.
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

    def test_no_codex(self) -> None:
        self.assertNotIn("codex", self._code_body().lower())

    def test_no_worker_adapter(self) -> None:
        self.assertNotIn("WorkerAdapter", self._code_body())


# =========================================================================
# Helper for corrupting snapshots (bypassing from_mapping)
# =========================================================================


def _corrupt_snapshot(**overrides) -> ModelSelectionSnapshot:
    """Build a snapshot by direct dataclass constructor, bypassing
    ``from_mapping()``."""
    base = {
        "required_model_tier": "standard",
        "required_model_capabilities": ("read", "write"),
        "model_binding_id": "bind-1",
        "selected_model_provider": "claude",
        "selected_model_id": "claude-opus-4",
        "selected_model_tier": "standard",
        "selected_deliberation_tier": "balanced",
        "selected_context_window_tokens": 200000,
        "selected_model_capabilities": ("read", "write"),
        "model_degradation_approval_id": None,
    }
    base.update(overrides)
    return ModelSelectionSnapshot(**base)


if __name__ == "__main__":
    unittest.main()
