"""TC-13.16b: comprehensive mock-based tests for ``mad_audit_gateway.py``.

stdlib-only unittest with async support; no real MAD, network, or API calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# -- Load modules under test -------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402
import mad_gateway  # noqa: E402
import mad_audit_gateway  # noqa: E402
sys.path.pop(0)

MadDeliberationDepth = core_types.MadDeliberationDepth


# =========================================================================
# Helpers
# =========================================================================

_ABS_ROOT = Path("C:/abs_root") if sys.platform == "win32" else Path("/abs_root")
_ABS_ARCHIVE = str(_ABS_ROOT / "MAD_HOME" / "deliberations" / "audit-test-id")


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
    from mad_audit_gateway import MadAuditGatewayInput

    defaults = {
        "project_root": Path(tempfile.gettempdir()),
        "task_id": "TC-031",
        "dispatch_id": "DSP-TC031-R2-A1-7F3C",
        "question": "审议问题？",
        "workspace": Path(tempfile.gettempdir()),
        "task_card_commit": "a" * 64,
        "task_card_path": "tasks/TC-031-r2.md",
        "delivery_report_path": "reports/TC-031-r2-a1.md",
        "report_commit": "b" * 64,
        "base_commit": "c" * 64,
        "implementation_commit": "d" * 64,
        "depth": MadDeliberationDepth.DEEP,
    }
    defaults.update(overrides)
    return MadAuditGatewayInput(**defaults)


def _make_audit_result_payload(**overrides) -> dict:
    """Return a valid ``mad.audit-result/v1`` JSON dict (exactly 11 keys)."""
    base: dict = {
        "schema_version": "mad.audit-result/v1",
        "deliberation_id": "20260728T120000Z-audit99",
        "status": "completed",
        "verdict": "pass",
        "issues": [
            {
                "id": "ISS-001",
                "severity": "medium",
                "category": "completeness",
                "title": "Missing edge case coverage",
                "description": "The delivery does not cover edge case X.",
                "location": {
                    "file": "src/main.py",
                    "line": "42",
                    "commit": "cd0123456789abcdef0123456789abcdef012345",
                },
                "recommendation": "Add tests for edge case X.",
            }
        ],
        "evidence": [
            {
                "ref": "EVD-001",
                "type": "git-diff",
                "source": "cd0123456789abcdef0123456789abcdef012345",
                "summary": "Diff between base and implementation",
                "verified": True,
            }
        ],
        "warnings": ["Sample warning"],
        "report": "# 审计报告\n\n这是审计内容。",
        "archive_path": _ABS_ARCHIVE,
        "participants": ["agent-a", "agent-b", "agent-c"],
        "plan": {"depth": "deep"},
    }
    base.update(overrides)
    return base


def _audit_result_bytes(payload: dict | None = None) -> bytes:
    if payload is None:
        payload = _make_audit_result_payload()
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


async def _make_cfg_inp(test_case):
    """Create a valid MadGatewayConfig and MadAuditGatewayInput."""
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
    cfg = mad_gateway.validate_gateway_config(cfg_dict)
    inp = _valid_input()
    return cfg, inp


# =========================================================================
# Module Properties
# =========================================================================


class ModulePropertiesTests(unittest.TestCase):
    """TC-13.16b: Verify module-level contracts."""

    def test_exact_all_exports(self) -> None:
        """__all__ must be exactly 7 names."""
        self.assertEqual(
            len(mad_audit_gateway.__all__), 7,
            f"expected 7, got {mad_audit_gateway.__all__}"
        )
        expected = {
            "MadAuditGatewayInput",
            "MadAuditIssueLocation",
            "MadAuditIssue",
            "MadAuditEvidence",
            "MadAuditPlan",
            "MadAuditGatewayResult",
            "run_audit_gateway",
        }
        self.assertEqual(set(mad_audit_gateway.__all__), expected)

    def test_import_produces_no_output(self) -> None:
        """Importing the module must not write to stdout/stderr."""
        import_name = "mad_audit_gateway"
        # Remove from cache first
        for key in list(sys.modules):
            if key == import_name or key.startswith(import_name + "."):
                del sys.modules[key]
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with (
            redirect_stdout(stdout_capture),
            redirect_stderr(stderr_capture),
        ):
            sys.path.insert(0, str(_SCRIPTS))
            try:
                importlib.import_module(import_name)
            finally:
                sys.path.pop(0)
        self.assertEqual(stdout_capture.getvalue(), "")
        self.assertEqual(stderr_capture.getvalue(), "")

    def test_no_mad_internal_imports(self) -> None:
        """Must not import from MAD internal modules."""
        source = Path(mad_audit_gateway.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from mad.", source)
        self.assertNotIn("import mad.", source)

    def test_no_parallel_exception_hierarchy(self) -> None:
        """Must not define audit-specific exception subclasses."""
        source = Path(mad_audit_gateway.__file__).read_text(encoding="utf-8")
        # The module must import from mad_gateway, not define new GatewayError subclasses
        self.assertIn("from mad_gateway import", source)
        # No new 'class.*Error.*Gateway' except the import
        import_lines = [
            l for l in source.splitlines()
            if "class " in l and "Error" in l and "Gateway" in l
        ]
        self.assertEqual(
            [], import_lines,
            "mad_audit_gateway must not define new GatewayError subclasses"
        )


# =========================================================================
# Input Validation
# =========================================================================


class InputValidationTests(unittest.TestCase):
    """TC-13.16b: Validate MadAuditGatewayInput."""

    def test_valid_input_accepted(self) -> None:
        """A completely valid input should pass validation."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad",
            mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("agent-x",),
            planning_report_agent_id="agent-x",
            audit_agent_ids=("agent-a", "agent-b"),
            audit_report_agent_id="agent-a",
        )
        inp = _valid_input()
        _validate_audit_input(cfg, inp)  # no raise

    def test_config_must_be_madgatewayconfig(self) -> None:
        """config must be MadGatewayConfig instance."""
        from mad_audit_gateway import _validate_audit_input
        inp = _valid_input()
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input("not a config", inp)

    def test_project_root_must_be_absolute_dir(self) -> None:
        """project_root must be an absolute existing directory."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a", "b"), audit_report_agent_id="a",
        )
        inp = _valid_input(project_root=Path("relative/path"))
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_workspace_must_be_absolute_dir(self) -> None:
        """workspace must be an absolute existing directory."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        inp = _valid_input(workspace=Path("/nonexistent/path"))
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_depth_must_be_maddeliberationdepth(self) -> None:
        """depth must be a MadDeliberationDepth, not a bare string."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        inp = _valid_input(depth="deep")  # bare string, not enum
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_commit_must_be_64_char_hex(self) -> None:
        """All four commit fields must be 64-char lowercase hex."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        inp = _valid_input(task_card_commit="abc123")  # too short
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_safe_path_rejects_backslash(self) -> None:
        """Repo-relative paths must not contain backslashes."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        inp = _valid_input(task_card_path="tasks\\bad.md")
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_safe_path_rejects_abs(self) -> None:
        """Repo-relative paths must not be absolute."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        inp = _valid_input(task_card_path="/absolute/path.md")
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_safe_path_rejects_dotdot(self) -> None:
        """Repo-relative paths must not contain .. segments."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        inp = _valid_input(task_card_path="../../etc/passwd")
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_report_agent_must_be_in_audit_agent_ids(self) -> None:
        """audit_report_agent_id must be in audit_agent_ids."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("agent-a",), audit_report_agent_id="agent-b",
        )
        inp = _valid_input()
        with self.assertRaises(mad_gateway.GatewayInputError):
            _validate_audit_input(cfg, inp)

    def test_all_three_depth_values_accepted(self) -> None:
        """Fast, balanced, deep — all three MadDeliberationDepth values."""
        from mad_audit_gateway import _validate_audit_input
        cfg = mad_gateway.MadGatewayConfig(
            mad_executable="mad", mad_home=str(_ABS_ROOT / "MAD_HOME"),
            timeout_seconds=600,
            planning_agent_ids=("x",), planning_report_agent_id="x",
            audit_agent_ids=("a",), audit_report_agent_id="a",
        )
        for depth in (
            MadDeliberationDepth.FAST,
            MadDeliberationDepth.BALANCED,
            MadDeliberationDepth.DEEP,
        ):
            inp = _valid_input(depth=depth)
            _validate_audit_input(cfg, inp)  # no raise


# =========================================================================
# Command Construction
# =========================================================================


class CommandConstructionTests(unittest.TestCase):
    """TC-13.16b: Exact argv construction."""

    def setUp(self):
        self.cfg = mad_gateway.MadGatewayConfig(
            mad_executable="/usr/bin/mad",
            mad_home="/mad/home",
            timeout_seconds=600,
            planning_agent_ids=("planner",),
            planning_report_agent_id="planner",
            audit_agent_ids=("rev-a", "rev-b", "rev-c"),
            audit_report_agent_id="rev-b",
        )
        self.inp = _valid_input(
            workspace=Path("/tmp/audit-worktree"),
            depth=MadDeliberationDepth.BALANCED,
        )

    def test_argv_is_tuple_not_shell(self) -> None:
        """Command must be a tuple, never a string."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        self.assertIsInstance(cmd, tuple)
        self.assertNotIsInstance(cmd, str)

    def test_executable_is_first_element(self) -> None:
        """First element is the mad executable."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        self.assertEqual(cmd[0], "/usr/bin/mad")

    def test_subcommand_is_audit(self) -> None:
        """Second element is 'audit'."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        self.assertEqual(cmd[1], "audit")

    def test_question_is_positional(self) -> None:
        """Question is passed as positional arg."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        self.assertEqual(cmd[2], self.inp.question)

    def test_workspace_flag(self) -> None:
        """--workspace followed by the path string."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        ws_idx = cmd.index("--workspace")
        self.assertEqual(cmd[ws_idx + 1], str(self.inp.workspace))

    def test_agents_csv_from_config(self) -> None:
        """--agents uses CSV from config.audit_agent_ids."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        agents_idx = cmd.index("--agents")
        self.assertEqual(cmd[agents_idx + 1], "rev-a,rev-b,rev-c")

    def test_report_agent_from_config(self) -> None:
        """--report-agent uses config.audit_report_agent_id."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        ra_idx = cmd.index("--report-agent")
        self.assertEqual(cmd[ra_idx + 1], "rev-b")

    def test_depth_uses_enum_value(self) -> None:
        """--depth uses inp.depth.value (the lowercase string)."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        depth_idx = cmd.index("--depth")
        self.assertEqual(cmd[depth_idx + 1], "balanced")

    def test_confirm_plan_and_format_json_present(self) -> None:
        """--confirm-plan and --format json always appended."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        self.assertIn("--confirm-plan", cmd)
        self.assertIn("--format", cmd)
        fmt_idx = cmd.index("--format")
        self.assertEqual(cmd[fmt_idx + 1], "json")

    def test_convergence_auto_present(self) -> None:
        """--convergence auto always appended."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        conv_idx = cmd.index("--convergence")
        self.assertEqual(cmd[conv_idx + 1], "auto")

    def test_all_shas_in_argv(self) -> None:
        """All four commit SHAs appear in argv."""
        from mad_audit_gateway import _build_audit_command
        cmd = _build_audit_command(self.cfg, self.inp)
        cmd_str = " ".join(cmd)
        self.assertIn(self.inp.task_card_commit, cmd_str)
        self.assertIn(self.inp.report_commit, cmd_str)
        self.assertIn(self.inp.base_commit, cmd_str)
        self.assertIn(self.inp.implementation_commit, cmd_str)


# =========================================================================
# Environment
# =========================================================================


class EnvironmentTests(unittest.TestCase):
    """TC-13.16b: Subprocess environment."""

    def test_mad_home_is_set(self) -> None:
        """MAD_HOME must be set in the env."""
        from mad_audit_gateway import _build_env
        env = _build_env("/my/mad/home")
        self.assertIn("MAD_HOME", env)
        self.assertEqual(env["MAD_HOME"], "/my/mad/home")

    def test_mad_participant_not_set(self) -> None:
        """MAD_PARTICIPANT must NOT be explicitly set by the Gateway."""
        from mad_audit_gateway import _build_env
        # Save and remove MAD_PARTICIPANT from parent env to test
        old_val = os.environ.pop("MAD_PARTICIPANT", None)
        try:
            env = _build_env("/my/mad/home")
            # MAD_PARTICIPANT should not be set by _build_env
            # (it may still be inherited from parent, but we cleared it)
            self.assertNotIn("MAD_PARTICIPANT", env)
        finally:
            if old_val is not None:
                os.environ["MAD_PARTICIPANT"] = old_val


# =========================================================================
# Successful Run
# =========================================================================


class SuccessfulAuditGatewayRunTests(unittest.TestCase):
    """TC-13.16b: Happy path through run_audit_gateway."""

    async def _run_with_mock(self, exit_code=0, stdout=None, stderr=b"",
                             raise_on_wait=None):
        """Helper: run_audit_gateway with mocked subprocess."""
        cfg, inp = await _make_cfg_inp(self)
        if stdout is None:
            stdout = _audit_result_bytes()

        def _factory(*args, **kwargs):
            return _fake_process_factory(
                exit_code=exit_code, stdout=stdout,
                stderr=stderr, raise_on_wait=raise_on_wait,
            )

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ) as mock_append:
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                result = await mad_audit_gateway.run_audit_gateway(cfg, inp)
            return result, mock_append, stdout

    def test_happy_path_returns_result(self) -> None:
        """Successful run returns MadAuditGatewayResult."""
        result, mock_append, stdout = asyncio.run(
            self._run_with_mock()
        )
        self.assertIsInstance(result, mad_audit_gateway.MadAuditGatewayResult)

    def test_deliberation_id(self) -> None:
        """deliberation_id is extracted from JSON."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertEqual(result.deliberation_id, "20260728T120000Z-audit99")

    def test_status_is_completed(self) -> None:
        """status is 'completed'."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertEqual(result.status, "completed")

    def test_verdict_is_pass(self) -> None:
        """verdict is 'pass'."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertEqual(result.verdict, "pass")

    def test_issues_are_parsed(self) -> None:
        """issues are parsed into MadAuditIssue objects."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertIsInstance(result.issues, tuple)
        self.assertGreater(len(result.issues), 0)
        issue = result.issues[0]
        self.assertIsInstance(issue, mad_audit_gateway.MadAuditIssue)
        self.assertEqual(issue.id, "ISS-001")
        self.assertEqual(issue.severity, "medium")
        self.assertEqual(issue.category, "completeness")
        self.assertIsInstance(issue.location, mad_audit_gateway.MadAuditIssueLocation)
        self.assertEqual(issue.location.file, "src/main.py")

    def test_evidence_parsed(self) -> None:
        """evidence is parsed into MadAuditEvidence objects."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertIsInstance(result.evidence, tuple)
        self.assertGreater(len(result.evidence), 0)
        ev = result.evidence[0]
        self.assertIsInstance(ev, mad_audit_gateway.MadAuditEvidence)
        self.assertEqual(ev.ref, "EVD-001")
        self.assertIs(ev.verified, True)  # strict True

    def test_plan_is_mad_audit_plan(self) -> None:
        """plan is a MadAuditPlan with depth only."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertIsInstance(result.plan, mad_audit_gateway.MadAuditPlan)
        self.assertEqual(result.plan.depth, MadDeliberationDepth.DEEP)

    def test_participants_match_config_order(self) -> None:
        """participants must match config.audit_agent_ids."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertEqual(
            result.participants,
            ("agent-a", "agent-b", "agent-c"),
        )

    def test_stdout_sha256_correct(self) -> None:
        """stdout_sha256 is SHA-256 of raw stdout bytes."""
        result, _, stdout = asyncio.run(self._run_with_mock())
        self.assertEqual(result.stdout_sha256, _sha256(stdout))

    def test_report_sha256_correct(self) -> None:
        """report_sha256 is SHA-256 of UTF-8 report string."""
        result, _, _ = asyncio.run(self._run_with_mock())
        expected = _sha256("# 审计报告\n\n这是审计内容。".encode("utf-8"))
        self.assertEqual(result.report_sha256, expected)

    def test_archive_path_is_absolute(self) -> None:
        """archive_path must be absolute."""
        result, _, _ = asyncio.run(self._run_with_mock())
        self.assertTrue(
            Path(result.archive_path).is_absolute() or
            (sys.platform == "win32" and len(result.archive_path) >= 3 and
             result.archive_path[1] == ":")
        )

    def test_mad_ref_appended_once(self) -> None:
        """append_mad_ref called exactly once on success."""
        _, mock_append, _ = asyncio.run(self._run_with_mock())
        self.assertEqual(mock_append.call_count, 1)

    def test_mad_ref_purpose_is_audit(self) -> None:
        """MadRefEntry purpose must be 'audit'."""
        _, mock_append, _ = asyncio.run(self._run_with_mock())
        entry = mock_append.call_args[0][1]
        self.assertEqual(entry.purpose, "audit")

    def test_mad_ref_depth_matches_input(self) -> None:
        """MadRefEntry.depth must match inp.depth.value."""
        _, mock_append, _ = asyncio.run(self._run_with_mock())
        entry = mock_append.call_args[0][1]
        self.assertEqual(entry.depth, "deep")

    def test_all_three_depths_work(self) -> None:
        """fast, balanced, deep all produce valid results."""
        from unittest import mock as _m

        async def _run_one(depth):
            cfg, inp = await _make_cfg_inp(self)
            inp = _valid_input(depth=depth)
            payload = _make_audit_result_payload(plan={"depth": depth.value})
            stdout = _audit_result_bytes(payload)

            def _factory(*args, **kwargs):
                return _fake_process_factory(stdout=stdout)

            with _m.patch.object(mad_audit_gateway.mad_refs, "append_mad_ref"):
                with _m.patch("asyncio.create_subprocess_exec", side_effect=_factory):
                    return await mad_audit_gateway.run_audit_gateway(cfg, inp)

        for depth in (
            MadDeliberationDepth.FAST,
            MadDeliberationDepth.BALANCED,
            MadDeliberationDepth.DEEP,
        ):
            result = asyncio.run(_run_one(depth))
            self.assertEqual(result.plan.depth, depth)

    def test_plan_depth_must_match_input(self) -> None:
        """If plan.depth != inp.depth.value, validation fails."""
        payload = _make_audit_result_payload(
            plan={"depth": "fast"}  # input is deep
        )
        stdout = _audit_result_bytes(payload)
        with self.assertRaises(mad_gateway.GatewayOutputValidationError):
            asyncio.run(self._run_with_mock(stdout=stdout))


# =========================================================================
# Non-Zero Exit Codes
# =========================================================================


class NonZeroExitCodeTests(unittest.TestCase):
    """TC-13.16b: Exit code mapping."""

    async def _run_with_exit(self, exit_code: int):
        cfg, inp = await _make_cfg_inp(self)
        stdout = _audit_result_bytes()

        def _factory(*args, **kwargs):
            return _fake_process_factory(exit_code=exit_code, stdout=stdout)

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ) as mock_append:
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                try:
                    await mad_audit_gateway.run_audit_gateway(cfg, inp)
                except Exception as e:
                    return e, mock_append
            return None, mock_append

    def test_exit_1_raises_gateway_exit1_error(self) -> None:
        """Exit 1 → GatewayExit1Error."""
        exc, _ = asyncio.run(self._run_with_exit(1))
        self.assertIsInstance(exc, mad_gateway.GatewayExit1Error)

    def test_exit_2_raises_gateway_exit2_error(self) -> None:
        """Exit 2 → GatewayExit2Error."""
        exc, _ = asyncio.run(self._run_with_exit(2))
        self.assertIsInstance(exc, mad_gateway.GatewayExit2Error)

    def test_exit_3_raises_gateway_exit3_error(self) -> None:
        """Exit 3 → GatewayExit3Error."""
        exc, _ = asyncio.run(self._run_with_exit(3))
        self.assertIsInstance(exc, mad_gateway.GatewayExit3Error)

    def test_exit_130_raises_gateway_exit130_error(self) -> None:
        """Exit 130 → GatewayExit130Error."""
        exc, _ = asyncio.run(self._run_with_exit(130))
        self.assertIsInstance(exc, mad_gateway.GatewayExit130Error)

    def test_unknown_exit_raises_unknown_error(self) -> None:
        """Unknown exit → GatewayUnknownExitError."""
        exc, _ = asyncio.run(self._run_with_exit(137))
        self.assertIsInstance(exc, mad_gateway.GatewayUnknownExitError)

    def test_non_zero_never_calls_append_mad_ref(self) -> None:
        """Non-zero exit must not write mad-ref."""
        for exit_code in (1, 2, 3, 130, 137):
            _, mock_append = asyncio.run(self._run_with_exit(exit_code))
            self.assertEqual(
                mock_append.call_count, 0,
                f"append_mad_ref called for exit {exit_code}"
            )


# =========================================================================
# Timeout
# =========================================================================


class TimeoutTests(unittest.TestCase):
    """TC-13.16b: Timeout handling."""

    async def _run_timeout(self):
        cfg, inp = await _make_cfg_inp(self)
        # Override timeout to a tiny value
        cfg_dict = {
            "mad_executable": cfg.mad_executable,
            "mad_home": cfg.mad_home,
            "timeout_seconds": 1,
            "planning_agent_ids": tuple(cfg.planning_agent_ids),
            "planning_report_agent_id": cfg.planning_report_agent_id,
            "audit_agent_ids": tuple(cfg.audit_agent_ids),
            "audit_report_agent_id": cfg.audit_report_agent_id,
        }
        short_cfg = mad_gateway.MadGatewayConfig(**cfg_dict)

        def _factory(*args, **kwargs):
            return _fake_process_factory(
                stdout=_audit_result_bytes(),
                raise_on_wait=TimeoutError(),
            )

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ) as mock_append:
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                try:
                    await mad_audit_gateway.run_audit_gateway(short_cfg, inp)
                except Exception as e:
                    return e, mock_append
            return None, mock_append

    def test_timeout_raises_gateway_timeout_error(self) -> None:
        """Timeout raises GatewayTimeoutError."""
        exc, _ = asyncio.run(self._run_timeout())
        self.assertIsInstance(exc, mad_gateway.GatewayTimeoutError)

    def test_timeout_never_calls_append_mad_ref(self) -> None:
        """Timeout must not write mad-ref."""
        _, mock_append = asyncio.run(self._run_timeout())
        self.assertEqual(mock_append.call_count, 0)


# =========================================================================
# Output Validation Failures
# =========================================================================


class OutputValidationFailureTests(unittest.TestCase):
    """TC-13.16b: Fail-closed on bad MAD output."""

    async def _run_with_payload(self, payload: dict):
        cfg, inp = await _make_cfg_inp(self)
        stdout = _audit_result_bytes(payload)

        def _factory(*args, **kwargs):
            return _fake_process_factory(stdout=stdout)

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ) as mock_append:
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                try:
                    await mad_audit_gateway.run_audit_gateway(cfg, inp)
                except Exception as e:
                    return e, mock_append
            return None, mock_append

    def test_non_json_stdout_raises(self) -> None:
        """Non-JSON stdout raises GatewayStdoutNotJsonError."""
        cfg, inp = asyncio.run(_make_cfg_inp(self))
        stdout = b"not json at all"

        def _factory(*args, **kwargs):
            return _fake_process_factory(stdout=stdout)

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ) as mock_append:
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                try:
                    asyncio.run(mad_audit_gateway.run_audit_gateway(cfg, inp))
                except Exception as exc:
                    self.assertIsInstance(
                        exc, mad_gateway.GatewayStdoutNotJsonError
                    )
                    return
            self.fail("Expected exception")

    def test_wrong_schema_version_raises(self) -> None:
        """Wrong schema_version raises GatewaySchemaVersionError."""
        payload = _make_audit_result_payload(
            schema_version="mad.run-result/v1"
        )
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewaySchemaVersionError)
        self.assertEqual(mock_append.call_count, 0)

    def test_missing_key_raises(self) -> None:
        """Missing root key raises GatewayOutputValidationError."""
        payload = _make_audit_result_payload()
        del payload["verdict"]
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_extra_key_raises(self) -> None:
        """Extra root key raises GatewayOutputValidationError."""
        payload = _make_audit_result_payload()
        payload["extra_field"] = "nope"
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_unknown_verdict_raises(self) -> None:
        """Unknown verdict raises GatewayOutputValidationError."""
        payload = _make_audit_result_payload(verdict="maybe")
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_status_not_completed_raises(self) -> None:
        """status != 'completed' raises GatewayOutputValidationError."""
        payload = _make_audit_result_payload(status="failed")
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_verified_not_bool_raises(self) -> None:
        """evidence.verified that is not strict bool raises."""
        payload = _make_audit_result_payload(evidence=[
            {
                "ref": "EVD-001",
                "type": "git-diff",
                "source": "abc",
                "summary": "x",
                "verified": 1,  # not True/False
            }
        ])
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_verified_truthy_string_raises(self) -> None:
        """evidence.verified = 'true' (string, not bool) raises."""
        payload = _make_audit_result_payload(evidence=[
            {
                "ref": "EVD-001",
                "type": "git-diff",
                "source": "abc",
                "summary": "x",
                "verified": "true",
            }
        ])
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_extra_issue_key_raises(self) -> None:
        """Extra key in an issue raises."""
        payload = _make_audit_result_payload()
        payload["issues"][0]["extra"] = "nope"
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_plan_not_dict_raises(self) -> None:
        """plan as non-dict raises."""
        payload = _make_audit_result_payload(plan=["not", "a", "dict"])
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_plan_extra_key_raises(self) -> None:
        """plan with extra key beyond 'depth' raises."""
        payload = _make_audit_result_payload(
            plan={"depth": "deep", "extra": "field"}
        )
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)

    def test_participants_empty_raises(self) -> None:
        """Empty participants list raises."""
        payload = _make_audit_result_payload(participants=[])
        exc, mock_append = asyncio.run(self._run_with_payload(payload))
        self.assertIsInstance(exc, mad_gateway.GatewayOutputValidationError)
        self.assertEqual(mock_append.call_count, 0)


# =========================================================================
# Data Model Immutability
# =========================================================================


class DataModelImmutabilityTests(unittest.TestCase):
    """TC-13.16b: Frozen/slots dataclass invariants."""

    def test_issue_location_is_frozen_slots(self) -> None:
        """MadAuditIssueLocation is frozen and slotted."""
        loc = mad_audit_gateway.MadAuditIssueLocation(
            file="src/x.py", line="10",
            commit="c" * 64,
        )
        self.assertTrue(hasattr(loc, "__slots__"))
        with self.assertRaises(Exception):
            loc.file = "new"  # type: ignore[misc]

    def test_issue_is_frozen_slots(self) -> None:
        """MadAuditIssue is frozen and slotted."""
        loc = mad_audit_gateway.MadAuditIssueLocation(
            file="src/x.py", line=None,
            commit="c" * 64,
        )
        issue = mad_audit_gateway.MadAuditIssue(
            id="ISS-001", severity="medium", category="completeness",
            title="T", description="D", location=loc, recommendation="R",
        )
        self.assertTrue(hasattr(issue, "__slots__"))
        with self.assertRaises(Exception):
            issue.severity = "high"  # type: ignore[misc]

    def test_evidence_is_frozen_slots(self) -> None:
        """MadAuditEvidence is frozen and slotted."""
        ev = mad_audit_gateway.MadAuditEvidence(
            ref="EVD-1", type="git-diff",
            source="abc", summary="S", verified=True,
        )
        self.assertTrue(hasattr(ev, "__slots__"))
        with self.assertRaises(Exception):
            ev.verified = False  # type: ignore[misc]

    def test_plan_is_frozen_slots(self) -> None:
        """MadAuditPlan is frozen, slotted, and has exactly 1 field."""
        plan = mad_audit_gateway.MadAuditPlan(depth=MadDeliberationDepth.DEEP)
        self.assertTrue(hasattr(plan, "__slots__"))
        self.assertEqual(len(plan.__slots__), 1)
        with self.assertRaises(Exception):
            plan.depth = MadDeliberationDepth.FAST  # type: ignore[misc]

    def test_issues_is_tuple_not_list(self) -> None:
        """MadAuditGatewayResult.issues is a tuple, not list."""
        result, _, _ = asyncio.run(self._happy_run())
        self.assertIsInstance(result.issues, tuple)

    def test_evidence_is_tuple_not_list(self) -> None:
        """MadAuditGatewayResult.evidence is a tuple, not list."""
        result, _, _ = asyncio.run(self._happy_run())
        self.assertIsInstance(result.evidence, tuple)

    async def _happy_run(self):
        """Minimal happy-path runner."""
        from unittest import mock as _mock
        cfg, inp = await _make_cfg_inp(self)
        stdout = _audit_result_bytes()

        def _factory(*args, **kwargs):
            return _fake_process_factory(stdout=stdout)

        with _mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ):
            with _mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                result = await mad_audit_gateway.run_audit_gateway(cfg, inp)
                return result, None, stdout


# =========================================================================
# Exception Leak Prevention
# =========================================================================


class ExceptionLeakPreventionTests(unittest.TestCase):
    """TC-13.16b: Exceptions must not leak raw data."""

    async def _fail_with_payload(self, payload: dict):
        cfg, inp = await _make_cfg_inp(self)
        stdout = json.dumps(payload).encode("utf-8")

        def _factory(*args, **kwargs):
            return _fake_process_factory(stdout=stdout)

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ):
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                try:
                    await mad_audit_gateway.run_audit_gateway(cfg, inp)
                except Exception as e:
                    return e
            return None

    def test_exception_str_no_raw_stdout(self) -> None:
        """Exception string must not contain raw report or stdout."""
        payload = _make_audit_result_payload(status="bad-status")
        exc = asyncio.run(self._fail_with_payload(payload))
        exc_str = str(exc)
        self.assertNotIn("# 审计报告", exc_str)
        self.assertNotIn("审计内容", exc_str)

    def test_exception_str_no_workspace(self) -> None:
        """Exception string must not contain workspace path."""
        payload = _make_audit_result_payload()
        del payload["verdict"]  # trigger validation error
        exc = asyncio.run(self._fail_with_payload(payload))
        exc_str = str(exc)
        self.assertNotIn(str(Path(tempfile.gettempdir())), exc_str)

    def test_non_zero_exit_exception_no_stderr_bytes(self) -> None:
        """Non-zero exit exception must not have raw stderr in message."""
        cfg, inp = asyncio.run(_make_cfg_inp(self))
        secret = b"SECRET_KEY=12345"
        stdout = _audit_result_bytes()

        def _factory(*args, **kwargs):
            return _fake_process_factory(
                exit_code=1, stdout=stdout, stderr=secret
            )

        with mock.patch.object(
            mad_audit_gateway.mad_refs, "append_mad_ref"
        ):
            with mock.patch(
                "asyncio.create_subprocess_exec",
                side_effect=_factory,
            ):
                try:
                    asyncio.run(mad_audit_gateway.run_audit_gateway(cfg, inp))
                except Exception as exc:
                    exc_str = str(exc)
                    self.assertNotIn("SECRET_KEY", exc_str)
                    self.assertNotIn("12345", exc_str)
                    return
            self.fail("Expected exception")
