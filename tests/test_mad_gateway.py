"""TC-13.6 Commit 2: comprehensive mock‑based tests for ``mad_gateway.py``.

stdlib‑only unittest with async support; no real MAD, network, or API calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# -- Load the modules under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402
import mad_gateway  # noqa: E402

sys.path.pop(0)

MadDeliberationDepth = core_types.MadDeliberationDepth


# =========================================================================
# Helpers
# =========================================================================

_ABS_ROOT = Path("C:/abs_root") if sys.platform == "win32" else Path("/abs_root")
_ABS_ARCHIVE = str(_ABS_ROOT / "MAD_HOME" / "deliberations" / "test-id")


def _valid_config_dict(**overrides) -> dict:
    base: dict = {
        "schema_version": "agentdesk.gateway-config/v1",
        "mad_executable": "mad",
        "mad_home": str(_ABS_ROOT / "MAD_HOME"),
        "timeout_seconds": 1800,
        "planning_agent_ids": ["agent-a", "agent-b"],
        "planning_report_agent_id": "agent-a",
        "audit_agent_ids": ["agent-a", "agent-b", "agent-c"],
        "audit_report_agent_id": "agent-a",
    }
    base.update(overrides)
    return base


def _valid_input(**overrides):
    from mad_gateway import MadGatewayInput

    defaults = {
        "project_root": Path(tempfile.gettempdir()),
        "task_id": "TC-031",
        "dispatch_id": "DSP-TC031-R2-A1-7F3C",
        "question": "审议问题？",
        "workspace": Path(tempfile.gettempdir()),
        "depth": MadDeliberationDepth.DEEP,
        "agent_ids": ("agent-a", "agent-b"),
        "report_agent_id": "agent-a",
    }
    defaults.update(overrides)
    return MadGatewayInput(**defaults)


def _make_result_payload(**overrides) -> dict:
    """Return a valid ``mad.run-result/v1`` JSON dict."""
    base: dict = {
        "schema_version": "mad.run-result/v1",
        "deliberation_id": "20260726T120000Z-a1b2c3d4",
        "status": "完成",
        "report": "# 测试报告\n\n这是报告内容。",
        "archive_path": _ABS_ARCHIVE,
        "warnings": [],
        "participants": ["agent-a", "agent-b"],
        "convergence": {
            "strategy": "auto",
            "triggered": False,
            "reason": "阈值未满足",
            "marked_participants": 0,
            "disputes": [],
            "status": "未触发",
        },
        "plan": {
            "participants": [
                {"id": "agent-a", "name": "A", "adapter": "fake", "model": None, "role": ""},
                {"id": "agent-b", "name": "B", "adapter": "fake", "model": None, "role": ""},
            ],
            "report_agent_id": "agent-a",
            "organizer_agent_id": None,
            "source": "manual",
            "depth": "deep",
            "critic_agent_id": None,
        },
    }
    base.update(overrides)
    return base


def _result_bytes(payload: dict | None = None) -> bytes:
    if payload is None:
        payload = _make_result_payload()
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _fake_process_factory(
    exit_code: int = 0,
    stdout: bytes | None = None,
    stderr: bytes = b"",
    raise_on_wait: Exception | None = None,
    pid: int = 99999,
):
    """Return a mock asyncio subprocess Process."""

    class _FakeProcess:
        returncode = exit_code
        pid_val = pid

        async def communicate(self, input=None):
            if raise_on_wait:
                raise raise_on_wait
            return (stdout, stderr)

        async def wait(self):
            if raise_on_wait:
                raise raise_on_wait
            return exit_code

    return _FakeProcess()


def _fake_which(exe: str) -> str | None:
    """Fake shutil.which — resolves 'mad' to a test path."""
    if exe == "mad":
        fake = _ABS_ROOT / "bin" / "mad"
        return str(fake)
    return None


async def _make_cfg_inp(test_case):
    """Create a valid MadGatewayConfig and MadGatewayInput.
    Returns (cfg, inp)."""
    cfg_dict = _valid_config_dict()
    tmp = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp.cleanup)
    mad_home = Path(tmp.name)
    agents_dir = mad_home / "config"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "agents.toml").write_text("# fake\n", encoding="utf-8")
    cfg_dict["mad_home"] = str(mad_home)
    exe_path = mad_home / "bin" / "mad"
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    exe_path.write_text("fake", encoding="utf-8")
    if sys.platform != "win32":
        import stat
        exe_path.chmod(exe_path.stat().st_mode | stat.S_IEXEC)
    cfg_dict["mad_executable"] = str(exe_path)
    cfg = mad_gateway.MadGatewayConfig(**{
        k: cfg_dict[k] for k in [
            "mad_executable", "mad_home", "timeout_seconds",
            "planning_agent_ids", "planning_report_agent_id",
            "audit_agent_ids", "audit_report_agent_id",
        ]
    })
    inp = _valid_input()
    return cfg, inp


async def _run_gateway_with_mock(
    test_case: unittest.TestCase,
    *,
    exit_code: int = 0,
    stdout_bytes: bytes | None = None,
    stderr_bytes: bytes = b"",
    which_side_effect=None,
    subprocess_side_effect=None,
    config_overrides: dict | None = None,
    input_overrides: dict | None = None,
    inject_mad_home: bool = True,
    append_mad_ref_override=None,
):
    """Run ``run_gateway`` with mocked subprocess and executable resolution.

    Returns the result or raises the thrown exception.
    If *inject_mad_home* is True, creates a fake ``config/agents.toml``
    in the configured ``mad_home`` so config validation passes.
    """
    cfg_dict = _valid_config_dict()
    if config_overrides:
        cfg_dict.update(config_overrides)

    # Inject a fake agents.toml at mad_home/config/agents.toml if needed.
    tmp = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp.cleanup)
    mad_home = Path(tmp.name)
    agents_dir = mad_home / "config"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "agents.toml").write_text("# fake\n", encoding="utf-8")
    cfg_dict["mad_home"] = str(mad_home)

    # Also set the executable so resolution succeeds by default.
    exe_path = mad_home / "bin" / "mad"
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    exe_path.write_text("#!/bin/sh\necho fake", encoding="utf-8")
    if sys.platform != "win32":
        import stat
        exe_path.chmod(exe_path.stat().st_mode | stat.S_IEXEC)
    cfg_dict["mad_executable"] = str(exe_path)

    cfg = mad_gateway.MadGatewayConfig(**{
        k: cfg_dict[k] for k in [
            "mad_executable", "mad_home", "timeout_seconds",
            "planning_agent_ids", "planning_report_agent_id",
            "audit_agent_ids", "audit_report_agent_id",
        ]
    })

    inp = _valid_input()
    if input_overrides:
        inp = _valid_input(**input_overrides)

    patches = []
    if which_side_effect is not None:
        p = mock.patch("shutil.which", side_effect=which_side_effect)
        patches.append(p)

    # Mock mad_refs.append_mad_ref via its module reference inside
    # mad_gateway.  When the caller provides a custom mock via
    # ``append_mad_ref_override``, use it directly — do NOT create a
    # second patch that would replace the test's own mock.
    if append_mad_ref_override is not None:
        p_append = mock.patch.object(
            mad_gateway.mad_refs, "append_mad_ref",
            append_mad_ref_override,
        )
    else:
        p_append = mock.patch.object(
            mad_gateway.mad_refs, "append_mad_ref",
            return_value={"refs": []},
        )
    patches.append(p_append)

    for p in patches:
        p.start()
        test_case.addCleanup(p.stop)

    # Build the fake subprocess.
    if stdout_bytes is None:
        stdout_bytes = _result_bytes()

    if subprocess_side_effect is not None:
        with mock.patch(
            "asyncio.create_subprocess_exec",
            side_effect=subprocess_side_effect,
        ):
            return await mad_gateway.run_gateway(cfg, inp)

    fake_proc = _fake_process_factory(
        exit_code=exit_code,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
    )
    with mock.patch(
        "asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        return await mad_gateway.run_gateway(cfg, inp)


# =========================================================================
# 1 — Module-level properties & import side effects
# =========================================================================


class ModulePropertiesTests(unittest.TestCase):
    """Verify constants and __all__."""

    def test_gateway_config_schema_matches(self) -> None:
        self.assertEqual(
            mad_gateway.GATEWAY_CONFIG_SCHEMA,
            "agentdesk.gateway-config/v1",
        )

    def test_mad_run_result_schema_matches(self) -> None:
        self.assertEqual(
            mad_gateway.MAD_RUN_RESULT_SCHEMA,
            "mad.run-result/v1",
        )

    def test_all_has_expected_names(self) -> None:
        for name in (
            "MadGatewayInput", "MadGatewayResult", "MadGatewayConfig",
            "run_gateway",
        ):
            self.assertIn(name, mad_gateway.__all__, f"{name} missing from __all__")

    def test_import_produces_no_output(self) -> None:
        """Import in an isolated subprocess to avoid sys.modules contamination."""
        import subprocess as _sp
        scripts_dir = str(_SCRIPTS)
        proc = _sp.run(
            [sys.executable, "-c", fr"""
import importlib, io, sys
from contextlib import redirect_stdout, redirect_stderr
name = "mad_gateway"
stdout = io.StringIO()
stderr = io.StringIO()
with redirect_stdout(stdout), redirect_stderr(stderr):
    sys.path.insert(0, {scripts_dir!r})
    try:
        importlib.import_module(name)
    finally:
        sys.path.remove({scripts_dir!r})
sys.stdout.write(stdout.getvalue())
sys.stderr.write(stderr.getvalue())
"""],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(proc.stdout.decode("utf-8", errors="replace"), "")
        self.assertEqual(proc.stderr.decode("utf-8", errors="replace"), "")

    def test_no_mad_internal_imports(self) -> None:
        """Must not import from MAD's own packages."""
        source = (_SCRIPTS / "mad_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("from mad.", source)
        self.assertNotIn("import mad.", source)


# =========================================================================
# 1b — Import isolation regression tests (module-identity stability)
# =========================================================================


class ImportIsolationRegressionTests(unittest.TestCase):
    """After test_import_produces_no_output, module identities are intact."""

    def test_mad_gateway_module_identity_intact_after_import_test(self) -> None:
        """sys.modules['mad_gateway'] unchanged after the import test runs."""
        pre = id(sys.modules["mad_gateway"])
        # Run the import test (it uses a subprocess — must not affect us)
        ModulePropertiesTests("test_import_produces_no_output").run()
        post = id(sys.modules["mad_gateway"])
        self.assertEqual(pre, post,
            "sys.modules['mad_gateway'] must be the same object "
            "after test_import_produces_no_output")

    def test_mad_refs_module_identity_intact_after_import_test(self) -> None:
        """sys.modules['mad_refs'] unchanged after the import test runs."""
        pre = id(sys.modules["mad_refs"])
        ModulePropertiesTests("test_import_produces_no_output").run()
        post = id(sys.modules["mad_refs"])
        self.assertEqual(pre, post,
            "sys.modules['mad_refs'] must be the same object "
            "after test_import_produces_no_output")

    def test_mad_gateway_mad_refs_is_sys_modules(self) -> None:
        """mad_gateway.mad_refs is sys.modules['mad_refs']."""
        self.assertIs(
            mad_gateway.mad_refs, sys.modules["mad_refs"],
            "mad_gateway.mad_refs must be the same object as "
            "sys.modules['mad_refs']",
        )

    def test_mad_gateway_depth_type_identity(self) -> None:
        """mad_gateway references the same MadDeliberationDepth from core_types."""
        # mad_gateway does `from core_types import MadDeliberationDepth`
        # Verify it's the same object as core_types.MadDeliberationDepth.
        self.assertIs(
            mad_gateway.MadDeliberationDepth,
            core_types.MadDeliberationDepth,
            "mad_gateway.MadDeliberationDepth must be "
            "core_types.MadDeliberationDepth",
        )

    def test_sys_path_unchanged_after_import_test(self) -> None:
        """sys.path identical before and after test_import_produces_no_output."""
        pre = list(sys.path)
        ModulePropertiesTests("test_import_produces_no_output").run()
        post = list(sys.path)
        self.assertEqual(pre, post,
            "sys.path must be unchanged after test_import_produces_no_output")

    def test_append_mad_ref_mock_works_after_import_test(self) -> None:
        """After the import test runs, mocking append_mad_ref still works."""
        ModulePropertiesTests("test_import_produces_no_output").run()
        m = mock.MagicMock(return_value={"refs": []})
        async def _test():
            await _run_gateway_with_mock(
                self, append_mad_ref_override=m,
            )
            self.assertEqual(m.call_count, 1,
                "append_mad_ref mock must receive exactly one call")
        asyncio.run(_test())

    def test_import_test_twice_no_identity_split(self) -> None:
        """Running the import test twice does not split module identities."""
        pre_gw = id(sys.modules["mad_gateway"])
        pre_mr = id(sys.modules["mad_refs"])
        ModulePropertiesTests("test_import_produces_no_output").run()
        ModulePropertiesTests("test_import_produces_no_output").run()
        post_gw = id(sys.modules["mad_gateway"])
        post_mr = id(sys.modules["mad_refs"])
        self.assertEqual(pre_gw, post_gw,
            "mad_gateway identity must be intact after two import test runs")
        self.assertEqual(pre_mr, post_mr,
            "mad_refs identity must be intact after two import test runs")


# =========================================================================
# 2 — Gateway config validation
# =========================================================================


class GatewayConfigValidationTests(unittest.TestCase):
    """validate_gateway_config accepts valid and rejects invalid configs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mad_home = Path(self.tmp.name)
        (self.mad_home / "config").mkdir(parents=True)
        (self.mad_home / "config" / "agents.toml").write_text("# fake\n", encoding="utf-8")

    def _cfg(self, **overrides) -> dict:
        d = _valid_config_dict()
        d["mad_home"] = str(self.mad_home)
        # Use an absolute executable so resolution succeeds.
        exe = self.mad_home / "bin" / "mad"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("fake", encoding="utf-8")
        if sys.platform != "win32":
            import stat
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        d["mad_executable"] = str(exe)
        d.update(overrides)
        return d

    def test_valid_config_passes(self) -> None:
        cfg = mad_gateway.validate_gateway_config(self._cfg())
        self.assertEqual(cfg.planning_agent_ids, ("agent-a", "agent-b"))

    def test_not_a_dict_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config([])  # type: ignore[arg-type]

    def test_wrong_schema_version_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(schema_version="v2"))

    def test_missing_key_rejected(self) -> None:
        d = self._cfg()
        del d["timeout_seconds"]
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(d)

    def test_extra_key_rejected(self) -> None:
        d = self._cfg()
        d["bonus"] = "nope"
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(d)

    def test_mad_home_not_absolute_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(mad_home="relative"))

    def test_mad_home_missing_agents_toml_rejected(self) -> None:
        # Remove agents.toml that setUp created.
        (self.mad_home / "config" / "agents.toml").unlink()
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg())

    def test_timeout_seconds_bool_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(timeout_seconds=True))

    def test_timeout_seconds_zero_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(timeout_seconds=0))

    def test_timeout_seconds_negative_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(timeout_seconds=-5))

    def test_timeout_seconds_float_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(timeout_seconds=1.5))

    def test_planning_agent_ids_empty_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(planning_agent_ids=[]))

    def test_planning_agent_ids_not_list_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(planning_agent_ids="a,b"))

    def test_planning_agent_ids_has_empty_string_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(planning_agent_ids=["a", ""]))

    def test_planning_agent_ids_duplicate_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(self._cfg(planning_agent_ids=["a", "a"]))

    def test_planning_report_not_in_planning_agents_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(
                self._cfg(
                    planning_agent_ids=["agent-a"],
                    planning_report_agent_id="agent-b",
                )
            )

    def test_audit_report_not_in_audit_agents_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(
                self._cfg(
                    audit_agent_ids=["agent-a"],
                    audit_report_agent_id="agent-b",
                )
            )

    def test_executable_not_found_rejected(self) -> None:
        with self.assertRaises(mad_gateway.GatewayConfigError):
            mad_gateway.validate_gateway_config(
                self._cfg(mad_executable="/nonexistent/path/xyzzy")
            )


# =========================================================================
# 3 — Gateway input validation
# =========================================================================


class GatewayInputValidationTests(unittest.TestCase):
    """validate_gateway_input rejects invalid inputs."""

    def test_valid_input_passes(self) -> None:
        inp = _valid_input()
        # Should not raise.
        mad_gateway.validate_gateway_input(inp)

    def test_bare_string_depth_rejected(self) -> None:
        inp = _valid_input(depth="deep")  # type: ignore[arg-type]
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)

    def test_wrong_enum_type_rejected(self) -> None:
        from core_types import TaskDifficulty
        inp = _valid_input(depth=TaskDifficulty.BASIC)  # type: ignore[arg-type]
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)

    def test_empty_task_id_rejected(self) -> None:
        inp = _valid_input(task_id="")
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)

    def test_empty_dispatch_id_rejected(self) -> None:
        inp = _valid_input(dispatch_id="")
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)

    def test_empty_question_rejected(self) -> None:
        inp = _valid_input(question="   ")
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)

    def test_report_not_in_agents_rejected(self) -> None:
        inp = _valid_input(agent_ids=("a",), report_agent_id="b")
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)

    def test_duplicate_agent_ids_rejected(self) -> None:
        inp = _valid_input(agent_ids=("a", "a"))
        with self.assertRaises(mad_gateway.GatewayInputError):
            mad_gateway.validate_gateway_input(inp)


# =========================================================================
# 4 — Exact command argv
# =========================================================================


class CommandConstructionTests(unittest.TestCase):
    """_build_command produces the exact argv tuple."""

    def test_exact_argv_structure(self) -> None:
        inp = _valid_input(
            question="测试问题",
            workspace=Path("/ws/test"),
            agent_ids=("agent-a", "agent-b"),
            report_agent_id="agent-a",
            depth=MadDeliberationDepth.DEEP,
        )
        cmd = mad_gateway._build_command("/usr/local/bin/mad", inp)
        expected = (
            "/usr/local/bin/mad",
            "deliberate",
            "测试问题",
            "--workspace",
            str(Path("/ws/test")),
            "--agents",
            "agent-a,agent-b",
            "--report-agent",
            "agent-a",
            "--depth",
            "deep",
            "--confirm-plan",
            "--format",
            "json",
        )
        self.assertEqual(cmd, expected)

    def test_question_with_spaces_is_single_arg(self) -> None:
        inp = _valid_input(question="这是一个 带空格 的问题？")
        cmd = mad_gateway._build_command("/bin/mad", inp)
        self.assertIn("这是一个 带空格 的问题？", cmd)
        # Must be exactly one element, not split across args.
        self.assertEqual(cmd[2], "这是一个 带空格 的问题？")

    def test_agent_ids_in_order(self) -> None:
        inp = _valid_input(agent_ids=("c", "a", "b"))
        cmd = mad_gateway._build_command("/bin/mad", inp)
        agents_idx = cmd.index("--agents")
        self.assertEqual(cmd[agents_idx + 1], "c,a,b")

    def test_fast_depth_maps_to_fast(self) -> None:
        inp = _valid_input(depth=MadDeliberationDepth.FAST)
        cmd = mad_gateway._build_command("/bin/mad", inp)
        depth_idx = cmd.index("--depth")
        self.assertEqual(cmd[depth_idx + 1], "fast")

    def test_balanced_depth_maps_to_balanced(self) -> None:
        inp = _valid_input(depth=MadDeliberationDepth.BALANCED)
        cmd = mad_gateway._build_command("/bin/mad", inp)
        depth_idx = cmd.index("--depth")
        self.assertEqual(cmd[depth_idx + 1], "balanced")

    def test_deep_depth_maps_to_deep(self) -> None:
        inp = _valid_input(depth=MadDeliberationDepth.DEEP)
        cmd = mad_gateway._build_command("/bin/mad", inp)
        depth_idx = cmd.index("--depth")
        self.assertEqual(cmd[depth_idx + 1], "deep")

    def test_no_interactive_flag(self) -> None:
        inp = _valid_input()
        cmd = mad_gateway._build_command("/bin/mad", inp)
        self.assertNotIn("--interactive", cmd)

    def test_no_shell_true(self) -> None:
        """_build_command returns a tuple, never a string — no shell=True."""
        inp = _valid_input()
        cmd = mad_gateway._build_command("/bin/mad", inp)
        self.assertIsInstance(cmd, tuple)
        # Every element is a string.
        for item in cmd:
            self.assertIsInstance(item, str)

    def test_confirm_plan_present(self) -> None:
        inp = _valid_input()
        cmd = mad_gateway._build_command("/bin/mad", inp)
        self.assertIn("--confirm-plan", cmd)


# =========================================================================
# 5 — Environment variables
# =========================================================================


class EnvironmentTests(unittest.TestCase):
    """_build_env sets MAD_HOME, does NOT set MAD_PARTICIPANT."""

    def test_env_includes_mad_home(self) -> None:
        env = mad_gateway._build_env("/my/mad/home")
        self.assertEqual(env["MAD_HOME"], "/my/mad/home")

    def test_env_does_not_set_mad_participant(self) -> None:
        """Gateway must not set MAD_PARTICIPANT — MAD does that internally."""
        with mock.patch.dict(os.environ, {}, clear=True):
            env = mad_gateway._build_env("/x")
            self.assertNotIn("MAD_PARTICIPANT", env)

    def test_env_preserves_existing_non_mad_participant(self) -> None:
        """If parent has MAD_PARTICIPANT=1 pre‑existing, Gateway copies it
        but does not actively write it."""
        with mock.patch.dict(os.environ, {"MAD_PARTICIPANT": "1", "HOME": "/h"}, clear=True):
            env = mad_gateway._build_env("/x")
            # It's in the parent env, so it gets copied.
            self.assertEqual(env.get("MAD_PARTICIPANT"), "1")
            # But it's not Gateway that set it — just inherited.
            self.assertEqual(env["HOME"], "/h")

    def test_env_is_a_copy_not_a_mutation(self) -> None:
        saved = dict(os.environ)
        _ = mad_gateway._build_env("/x")
        self.assertEqual(os.environ, saved, "os.environ must not be mutated")


# =========================================================================
# 6 — Successful gateway run (happy path)
# =========================================================================


class SuccessfulGatewayRunTests(unittest.TestCase):
    """End‑to‑end successful gateway invocation."""

    def test_successful_run_returns_result(self) -> None:
        async def _test():
            result = await _run_gateway_with_mock(self)
            self.assertEqual(result.deliberation_id, "20260726T120000Z-a1b2c3d4")
            self.assertEqual(result.status, "完成")
            self.assertEqual(result.exit_code, 0)
        asyncio.run(_test())

    def test_stdout_sha256_computed_correctly(self) -> None:
        payload = _make_result_payload()
        raw = _result_bytes(payload)
        expected_sha = _sha256(raw)

        async def _test():
            result = await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(result.stdout_sha256, expected_sha)
        asyncio.run(_test())

    def test_report_sha256_computed_correctly(self) -> None:
        payload = _make_result_payload(report="# 特定报告内容")
        raw = _result_bytes(payload)
        expected_report_sha = _sha256("# 特定报告内容".encode("utf-8"))

        async def _test():
            result = await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(result.report_sha256, expected_report_sha)
        asyncio.run(_test())

    def test_unicode_status_preserved(self) -> None:
        payload = _make_result_payload(status="带警告完成")  # 带警告完成
        raw = _result_bytes(payload)

        async def _test():
            result = await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(result.status, "带警告完成")
        asyncio.run(_test())

    def test_extra_top_level_keys_allowed(self) -> None:
        """Forward‑compatible: unknown top‑level keys don't cause failure."""
        payload = _make_result_payload()
        payload["future_field"] = "some_value"
        payload["another_future"] = 42
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        async def _test():
            result = await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertIsInstance(result, mad_gateway.MadGatewayResult)
        asyncio.run(_test())

    def test_warnings_list_str_accepted(self) -> None:
        payload = _make_result_payload(warnings=["w1", "w2"])
        raw = _result_bytes(payload)

        async def _test():
            result = await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(result.warnings, ("w1", "w2"))
        asyncio.run(_test())

    def test_append_mad_ref_is_called(self) -> None:
        m = mock.MagicMock(return_value={"refs": []})
        async def _test():
            result = await _run_gateway_with_mock(
                self, append_mad_ref_override=m)
            self.assertEqual(m.call_count, 1,
                             "append_mad_ref must be called once")
            call_args = m.call_args
            entry = call_args[0][1]
            self.assertEqual(entry.task_id, "TC-031")
            self.assertEqual(entry.dispatch_id, "DSP-TC031-R2-A1-7F3C")
            self.assertEqual(entry.purpose, "planning")
            self.assertEqual(entry.depth, "deep")
            self.assertEqual(len(entry.stdout_sha256), 64)
            self.assertEqual(len(entry.report_sha256), 64)
        asyncio.run(_test())

    def test_append_mad_ref_error_propagates(self) -> None:
        from mad_refs import LockContentionError
        def _raise_lock(*a, **kw):
            raise LockContentionError("locked")
        async def _test():
            with self.assertRaises(LockContentionError):
                await _run_gateway_with_mock(
                    self, append_mad_ref_override=_raise_lock)
        asyncio.run(_test())


# =========================================================================
# 7 — Non‑zero exit codes
# =========================================================================


class NonZeroExitCodeTests(unittest.TestCase):
    """Each known exit code maps to the correct exception subclass."""

    def _assert_exit_raises(self, exit_code: int, expected_cls: type) -> None:
        async def _test():
            with self.assertRaises(expected_cls) as ctx:
                await _run_gateway_with_mock(self, exit_code=exit_code)
            exc = ctx.exception
            self.assertEqual(exc.exit_code, exit_code)
        asyncio.run(_test())

    def test_exit_0_is_ok(self) -> None:
        async def _test():
            result = await _run_gateway_with_mock(self, exit_code=0)
            self.assertEqual(result.exit_code, 0)
        asyncio.run(_test())

    def test_exit_1_raises_exit1_error(self) -> None:
        self._assert_exit_raises(1, mad_gateway.GatewayExit1Error)

    def test_exit_2_raises_exit2_error(self) -> None:
        self._assert_exit_raises(2, mad_gateway.GatewayExit2Error)

    def test_exit_3_raises_exit3_error(self) -> None:
        self._assert_exit_raises(3, mad_gateway.GatewayExit3Error)

    def test_exit_130_raises_exit130_error(self) -> None:
        self._assert_exit_raises(130, mad_gateway.GatewayExit130Error)

    def test_exit_137_raises_unknown_exit_error(self) -> None:
        self._assert_exit_raises(137, mad_gateway.GatewayUnknownExitError)

    def test_exit_255_raises_unknown_exit_error(self) -> None:
        self._assert_exit_raises(255, mad_gateway.GatewayUnknownExitError)

    def test_non_zero_exit_does_not_call_append_mad_ref(self) -> None:
        m = mock.MagicMock()
        async def _test():
            with self.assertRaises(mad_gateway.GatewayNonZeroExitError):
                await _run_gateway_with_mock(self, exit_code=1,
                    append_mad_ref_override=m)
            m.assert_not_called()
        asyncio.run(_test())


# =========================================================================
# 8 — Timeout
# =========================================================================


class TimeoutTests(unittest.TestCase):
    """Timeout raises GatewayTimeoutError."""

    def test_timeout_raises_gateway_timeout_error(self) -> None:
        async def _test():
            fake = _fake_process_factory(
                exit_code=-1,
                raise_on_wait=TimeoutError(),
            )
            with mock.patch("asyncio.create_subprocess_exec", return_value=fake):
                with self.assertRaises(mad_gateway.GatewayTimeoutError):
                    cfg, inp = await _make_cfg_inp(self)
                    await mad_gateway.run_gateway(cfg, inp)
        asyncio.run(_test())

    def test_timeout_does_not_call_append_mad_ref(self) -> None:
        m = mock.MagicMock()
        async def _test():
            fake = _fake_process_factory(
                exit_code=-1,
                raise_on_wait=TimeoutError(),
            )
            with mock.patch("asyncio.create_subprocess_exec", return_value=fake):
                with self.assertRaises(mad_gateway.GatewayTimeoutError):
                    cfg, inp = await _make_cfg_inp(self)
                    # Patch after cfg creation so config validation passes
                    with mock.patch.object(
                        mad_gateway.mad_refs, "append_mad_ref", m,
                    ):
                        await mad_gateway.run_gateway(cfg, inp)
            m.assert_not_called()
        asyncio.run(_test())


# =========================================================================
# 9 — stdout validation failures
# =========================================================================


class StdoutValidationFailureTests(unittest.TestCase):
    """Fail‑closed for every stdout validation step."""

    def test_non_utf8_stdout_raises(self) -> None:
        raw = b"\xff\xfe\x00\x01"  # Not valid UTF-8
        async def _test():
            with self.assertRaises(mad_gateway.GatewayStdoutNotUtf8Error) as ctx:
                await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(ctx.exception.stdout_sha256, _sha256(raw))
        asyncio.run(_test())

    def test_non_json_stdout_raises(self) -> None:
        raw = b"this is not json"
        async def _test():
            with self.assertRaises(mad_gateway.GatewayStdoutNotJsonError) as ctx:
                await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(ctx.exception.stdout_sha256, _sha256(raw))
        asyncio.run(_test())

    def test_array_root_raises(self) -> None:
        raw = json.dumps([1, 2, 3]).encode("utf-8")
        async def _test():
            with self.assertRaises(mad_gateway.GatewayStdoutRootNotObjectError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_string_root_raises(self) -> None:
        raw = json.dumps("hello").encode("utf-8")
        async def _test():
            with self.assertRaises(mad_gateway.GatewayStdoutRootNotObjectError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_missing_schema_version_raises(self) -> None:
        p = _make_result_payload()
        del p["schema_version"]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewaySchemaVersionError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_wrong_schema_version_raises(self) -> None:
        p = _make_result_payload(schema_version="mad.run-result/v99")
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewaySchemaVersionError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_schema_version_bool_raises(self) -> None:
        p = _make_result_payload(schema_version=True)  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewaySchemaVersionError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_schema_version_null_raises(self) -> None:
        p = _make_result_payload(schema_version=None)  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewaySchemaVersionError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_missing_deliberation_id_raises(self) -> None:
        p = _make_result_payload()
        del p["deliberation_id"]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError) as ctx:
                await _run_gateway_with_mock(self, stdout_bytes=raw)
            self.assertEqual(ctx.exception.field, "deliberation_id")
        asyncio.run(_test())

    def test_empty_deliberation_id_raises(self) -> None:
        p = _make_result_payload(deliberation_id="")
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_missing_status_raises(self) -> None:
        p = _make_result_payload()
        del p["status"]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_missing_report_raises(self) -> None:
        p = _make_result_payload()
        del p["report"]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_report_not_string_raises(self) -> None:
        p = _make_result_payload(report=42)  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_archive_path_relative_raises(self) -> None:
        p = _make_result_payload(archive_path="relative/path")
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_participants_not_list_raises(self) -> None:
        p = _make_result_payload(participants="agent-a")  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_participants_list_of_objects_raises(self) -> None:
        """participants as object array must be rejected (V1 is list[str])."""
        p = _make_result_payload(participants=[{"id": "a"}])  # type: ignore[list-item]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_warnings_not_list_raises(self) -> None:
        p = _make_result_payload(warnings="not-a-list")  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_convergence_not_object_raises(self) -> None:
        p = _make_result_payload(convergence="not-obj")  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_plan_not_object_raises(self) -> None:
        p = _make_result_payload(plan=[])  # type: ignore[arg-type]
        raw = _result_bytes(p)
        async def _test():
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw)
        asyncio.run(_test())

    def test_validation_failure_does_not_call_append(self) -> None:
        m = mock.MagicMock()
        async def _test():
            p = _make_result_payload()
            del p["report"]
            raw = _result_bytes(p)
            with self.assertRaises(mad_gateway.GatewayOutputValidationError):
                await _run_gateway_with_mock(self, stdout_bytes=raw,
                    append_mad_ref_override=m)
            m.assert_not_called()
        asyncio.run(_test())


# =========================================================================
# 10 — Error hierarchy
# =========================================================================


class ErrorHierarchyTests(unittest.TestCase):
    """Custom exceptions have the expected inheritance chain."""

    def test_input_error_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayInputError("x"), mad_gateway.GatewayError
        )

    def test_config_error_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayConfigError("x"), mad_gateway.GatewayError
        )

    def test_executable_not_found_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayExecutableNotFoundError("x"), mad_gateway.GatewayError
        )

    def test_launch_error_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayLaunchError("x"), mad_gateway.GatewayError
        )

    def test_timeout_error_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayTimeoutError("x"), mad_gateway.GatewayError
        )

    def test_non_zero_exit_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayNonZeroExitError(1), mad_gateway.GatewayError
        )

    def test_exit1_is_non_zero_exit(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayExit1Error(1), mad_gateway.GatewayNonZeroExitError
        )

    def test_exit2_is_non_zero_exit(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayExit2Error(2), mad_gateway.GatewayNonZeroExitError
        )

    def test_exit3_is_non_zero_exit(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayExit3Error(3), mad_gateway.GatewayNonZeroExitError
        )

    def test_exit130_is_non_zero_exit(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayExit130Error(130), mad_gateway.GatewayNonZeroExitError
        )

    def test_unknown_exit_is_non_zero_exit(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayUnknownExitError(99), mad_gateway.GatewayNonZeroExitError
        )

    def test_stdout_not_utf8_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayStdoutNotUtf8Error(""), mad_gateway.GatewayError
        )

    def test_stdout_not_json_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayStdoutNotJsonError(""), mad_gateway.GatewayError
        )

    def test_root_not_object_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayStdoutRootNotObjectError(""), mad_gateway.GatewayError
        )

    def test_schema_version_error_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewaySchemaVersionError(""), mad_gateway.GatewayError
        )

    def test_output_validation_error_is_gateway_error(self) -> None:
        self.assertIsInstance(
            mad_gateway.GatewayOutputValidationError("x", ""), mad_gateway.GatewayError
        )


# =========================================================================
# 11 — Launch failure
# =========================================================================


class LaunchFailureTests(unittest.TestCase):
    """OSError during subprocess creation → GatewayLaunchError."""

    def test_launch_os_error_raises(self) -> None:
        async def _test():
            with self.assertRaises(mad_gateway.GatewayLaunchError):
                await _run_gateway_with_mock(
                    self,
                    subprocess_side_effect=OSError("cannot exec"),
                )
        asyncio.run(_test())


# =========================================================================
# 12 — No gateway, model, or network calls
# =========================================================================


class NoLeakTests(unittest.TestCase):
    """Module does not import Gateway‑forbidden symbols."""

    def test_no_mad_internal_imports(self) -> None:
        source = (_SCRIPTS / "mad_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("from mad.", source)
        self.assertNotIn("import mad.adapters", source)
        self.assertNotIn("import mad.models", source)

    def test_no_real_mad_calls(self) -> None:
        """Tests must not invoke real MAD executable.

        The word 'subprocess.run' may appear in comments/docstrings;
        the real check is that the Gateway module imports asyncio
        subprocess, not the synchronous stdlib subprocess module."""
        source = (_SCRIPTS / "mad_gateway.py").read_text(encoding="utf-8")
        # The gateway must not use subprocess.run or subprocess.call.
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("subprocess.call", source)

    def test_no_network_imports(self) -> None:
        names = set(dir(mad_gateway))
        for forbidden in ("urllib", "http", "requests", "socket"):
            self.assertNotIn(forbidden, names)


# =========================================================================
# 13 — MadRefEntry integration
# =========================================================================


class MadRefIntegrationTests(unittest.TestCase):
    """append_mad_ref is called with exactly 10 correct fields."""

    def test_mad_ref_entry_fields_exact(self) -> None:
        m = mock.MagicMock(return_value={"refs": []})
        async def _test():
            await _run_gateway_with_mock(
                self, append_mad_ref_override=m)
            self.assertIsNotNone(m.call_args,
                                 "append_mad_ref was not called")
            call_args = m.call_args
            self.assertEqual(len(call_args[0]), 2)  # project_root, entry
            entry = call_args[0][1]
            self.assertEqual(entry.task_id, "TC-031")
            self.assertEqual(entry.dispatch_id, "DSP-TC031-R2-A1-7F3C")
            self.assertEqual(entry.purpose, "planning")
            self.assertEqual(entry.deliberation_id, "20260726T120000Z-a1b2c3d4")
            self.assertEqual(entry.depth, "deep")
            self.assertEqual(len(entry.stdout_sha256), 64)
            self.assertEqual(len(entry.report_sha256), 64)
            self.assertEqual(entry.status, "完成")
            self.assertEqual(entry.archive_path, _ABS_ARCHIVE)
            self.assertIn("T", entry.created_at)
            self.assertIn("Z", entry.created_at)
        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
