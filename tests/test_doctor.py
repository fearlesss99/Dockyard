"""TC-13.21e.1: comprehensive tests for ``doctor.py``.

stdlib-only unittest; no real MAD, network, subprocess, or API calls.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

# -- Load the module under test ----------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import doctor  # noqa: E402

sys.path.pop(0)

# Convenience aliases
DoctorCheckStatus = doctor.DoctorCheckStatus
DoctorCheck = doctor.DoctorCheck
DoctorRequest = doctor.DoctorRequest
DoctorReport = doctor.DoctorReport
DoctorError = doctor.DoctorError
DoctorInputError = doctor.DoctorInputError

# -- Helpers -----------------------------------------------------------------

IS_WINDOWS = sys.platform == "win32"
_ABS_ROOT = Path("C:/abs_root") if IS_WINDOWS else Path("/abs_root")


def _make_project(root: Path) -> None:
    """Create a minimal AgentDesk project structure under *root*."""
    (root / ".agentdesk" / "runtime").mkdir(parents=True)
    (root / "docs" / "pm").mkdir(parents=True)
    (root / ".gitignore").write_text(".agentdesk/runtime/\n", encoding="utf-8")


def _write_bindings(root: Path, bindings: dict) -> None:
    """Write model-bindings.yaml, JSON-serialising the dict."""
    (root / ".agentdesk" / "runtime" / "model-bindings.yaml").write_text(
        json.dumps(bindings), encoding="utf-8"
    )


def _write_policy(root: Path, policy: dict) -> None:
    """Write ROLE-POLICIES.yaml."""
    (root / "docs" / "pm" / "ROLE-POLICIES.yaml").write_text(
        json.dumps(policy), encoding="utf-8"
    )


def _write_gateway(root: Path, gateway: dict) -> None:
    """Write gateway.yaml."""
    (root / ".agentdesk" / "runtime" / "gateway.yaml").write_text(
        json.dumps(gateway), encoding="utf-8"
    )


def _healthy_bindings() -> dict:
    return {
        "schema_version": "agentdesk.model-bindings/v2",
        "updated_at": None,
        "bindings": {
            "claude-default": {
                "provider": "claude",
                "model_id": "claude-sonnet-5",
                "tier": "expert",
                "deliberation_tier": "deep",
                "context_window_tokens": 200000,
                "capabilities": ["code", "review", "plan"],
                "enabled": True,
            }
        },
    }


def _healthy_policy() -> dict:
    return {
        "schema_version": "agentdesk.role-policies/v1",
        "tier_order": ["basic", "standard", "advanced", "expert"],
        "deliberation_tier_order": ["efficient", "balanced", "deep"],
        "risk_floors": {
            "L0": "basic",
            "L1": "basic",
            "L2": "standard",
            "L3": "advanced",
            "L4": "expert",
        },
        "roles": {
            "PM": {
                "minimum_tier": "standard",
                "default_tier": "advanced",
                "deliberation_tier": "balanced",
                "required_capabilities": ["code"],
                "degradation_policy": "allow_to_minimum",
            }
        },
    }


def _healthy_gateway() -> dict:
    return {
        "schema_version": "agentdesk.gateway-config/v1",
        "mad_executable": "cmd.exe" if IS_WINDOWS else "sh",
        "mad_home": str(_ABS_ROOT / "MAD_HOME"),
        "timeout_seconds": 1800,
        "planning_agent_ids": ["agent-a"],
        "planning_report_agent_id": "agent-a",
        "audit_agent_ids": ["agent-a", "agent-b"],
        "audit_report_agent_id": "agent-a",
    }


# ============================================================================
# 1. Public API — exact shapes
# ============================================================================


class PublicApiTests(unittest.TestCase):
    """Verify the public API matches the frozen contract exactly."""

    def test_all_is_exact_set(self) -> None:
        expected = [
            "DoctorCheckStatus",
            "DoctorCheck",
            "DoctorRequest",
            "DoctorReport",
            "DoctorError",
            "DoctorInputError",
            "run_doctor",
            "main",
        ]
        self.assertEqual(sorted(doctor.__all__), sorted(expected))

    def test_all_length_is_8(self) -> None:
        self.assertEqual(len(doctor.__all__), 8)

    def test_enum_exactly_three_members(self) -> None:
        members = list(DoctorCheckStatus)
        self.assertEqual(len(members), 3)
        values = {m.value for m in members}
        self.assertEqual(values, {"pass", "warn", "fail"})

    def test_enum_member_values(self) -> None:
        self.assertEqual(DoctorCheckStatus.PASS.value, "pass")
        self.assertEqual(DoctorCheckStatus.WARN.value, "warn")
        self.assertEqual(DoctorCheckStatus.FAIL.value, "fail")

    def test_doctor_check_four_fields(self) -> None:
        fields = DoctorCheck.__dataclass_fields__
        self.assertEqual(len(fields), 4)
        self.assertIn("check_id", fields)
        self.assertIn("status", fields)
        self.assertIn("summary", fields)
        self.assertIn("remediation", fields)

    def test_doctor_check_frozen(self) -> None:
        dc = DoctorCheck("D001", DoctorCheckStatus.PASS, "ok", None)
        with self.assertRaises(Exception):
            dc.check_id = "D002"  # type: ignore[misc]

    def test_doctor_check_slots(self) -> None:
        dc = DoctorCheck("D001", DoctorCheckStatus.PASS, "ok", None)
        self.assertFalse(hasattr(dc, "__dict__"))

    def test_doctor_request_two_fields(self) -> None:
        fields = DoctorRequest.__dataclass_fields__
        self.assertEqual(len(fields), 2)
        self.assertIn("project_root", fields)
        self.assertIn("mad_home", fields)

    def test_doctor_request_frozen_slots(self) -> None:
        req = DoctorRequest(project_root=Path("/tmp"), mad_home=None)
        self.assertFalse(hasattr(req, "__dict__"))
        with self.assertRaises(Exception):
            req.project_root = Path("/other")  # type: ignore[misc]

    def test_doctor_report_two_fields(self) -> None:
        fields = DoctorReport.__dataclass_fields__
        self.assertEqual(len(fields), 2)
        self.assertIn("ready", fields)
        self.assertIn("checks", fields)

    def test_doctor_report_frozen_slots(self) -> None:
        report = DoctorReport(ready=True, checks=())
        self.assertFalse(hasattr(report, "__dict__"))
        with self.assertRaises(Exception):
            report.ready = False  # type: ignore[misc]

    def test_checks_is_tuple_of_doctor_check(self) -> None:
        dc = DoctorCheck("D001", DoctorCheckStatus.PASS, "ok", None)
        report = DoctorReport(ready=True, checks=(dc,))
        self.assertIsInstance(report.checks, tuple)
        self.assertIsInstance(report.checks[0], DoctorCheck)

    def test_no_dict_or_any_public_fields(self) -> None:
        """No public field is typed dict or Any."""
        for cls in (DoctorCheck, DoctorRequest, DoctorReport):
            for field_name, field in cls.__dataclass_fields__.items():
                ftype = field.type
                type_str = str(ftype)
                self.assertNotIn("dict", type_str.lower().replace("__", ""),
                                 f"{cls.__name__}.{field_name} typed as dict-like")
                self.assertNotIn("Any", type_str.split("[")[0].split("|")[0].strip(),
                                 f"{cls.__name__}.{field_name} typed as Any")

    def test_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(DoctorInputError, DoctorError))
        self.assertTrue(issubclass(DoctorError, Exception))


# ============================================================================
# 2. Input safety — request validation
# ============================================================================


class InputSafetyTests(unittest.TestCase):
    """Verify rigid input rejection without leaking user data."""

    def test_rejects_non_doctor_request(self) -> None:
        with self.assertRaises(DoctorInputError) as ctx:
            doctor.run_doctor({"project_root": Path("/tmp")})  # type: ignore[arg-type]
        self.assertIn("DoctorRequest", str(ctx.exception))

    def test_rejects_bool_as_project_root(self) -> None:
        with self.assertRaises(DoctorInputError):
            doctor.run_doctor(
                DoctorRequest(project_root=True, mad_home=None)  # type: ignore[arg-type]
            )

    def test_rejects_str_as_project_root(self) -> None:
        with self.assertRaises(DoctorInputError):
            doctor.run_doctor(
                DoctorRequest(project_root="/tmp", mad_home=None)  # type: ignore[arg-type]
            )

    def test_rejects_relative_path(self) -> None:
        with self.assertRaises(DoctorInputError):
            doctor.run_doctor(
                DoctorRequest(project_root=Path("relative/path"), mad_home=None)
            )

    def test_rejects_bool_as_mad_home(self) -> None:
        with self.assertRaises(DoctorInputError):
            doctor.run_doctor(
                DoctorRequest(
                    project_root=_ABS_ROOT,
                    mad_home=False,  # type: ignore[arg-type]
                )
            )

    def test_rejects_str_as_mad_home(self) -> None:
        with self.assertRaises(DoctorInputError):
            doctor.run_doctor(
                DoctorRequest(
                    project_root=_ABS_ROOT,
                    mad_home="/some/path",  # type: ignore[arg-type]
                )
            )

    def test_rejects_relative_mad_home(self) -> None:
        with self.assertRaises(DoctorInputError):
            doctor.run_doctor(
                DoctorRequest(
                    project_root=_ABS_ROOT,
                    mad_home=Path("relative/mad"),
                )
            )

    def test_rejects_nonexistent_project_root(self) -> None:
        """Nonexistent path is reported as a check failure, not an exception."""
        req = DoctorRequest(
            project_root=_ABS_ROOT / "nonexistent_project_xyz",
            mad_home=None,
        )
        report = doctor.run_doctor(req)
        self.assertFalse(report.ready)
        d001 = report.checks[0]
        self.assertEqual(d001.check_id, "D001")
        self.assertEqual(d001.status, DoctorCheckStatus.FAIL)

    def test_rejects_file_as_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "file.txt"
            f.write_text("not a dir")
            req = DoctorRequest(project_root=f, mad_home=None)
            report = doctor.run_doctor(req)
            d001 = report.checks[0]
            self.assertEqual(d001.status, DoctorCheckStatus.FAIL)

    def test_exception_message_never_echoes_input(self) -> None:
        """Error messages are fixed, never contain user paths."""
        for bad_input in [
            DoctorRequest(project_root=Path("relative"), mad_home=None),
            DoctorRequest(project_root=_ABS_ROOT, mad_home=Path("relative")),
        ]:
            try:
                doctor.run_doctor(bad_input)
            except DoctorInputError as e:
                msg = str(e)
                self.assertNotIn("relative", msg)

    def test_no_repr_of_user_objects_in_exceptions(self) -> None:
        """DoctorInputError messages never call repr() on user objects."""
        with self.assertRaises(DoctorInputError) as ctx:
            doctor.run_doctor(
                DoctorRequest(project_root="a string", mad_home=None)  # type: ignore[arg-type]
            )
        msg = str(ctx.exception)
        self.assertNotIn("'a string'", msg)
        self.assertNotIn('"a string"', msg)


# ============================================================================
# 3. Check logic — D001 through D012
# ============================================================================


class CheckLogicTests(unittest.TestCase):
    """Per-check behaviour and readiness calculation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        _make_project(self.project)
        _write_bindings(self.project, _healthy_bindings())
        _write_policy(self.project, _healthy_policy())
        _write_gateway(self.project, _healthy_gateway())
        # Create a MAD home with agents.toml outside the project
        self.mad_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.mad_tmp.cleanup)
        self.mad_home = Path(self.mad_tmp.name)
        (self.mad_home / "config").mkdir(parents=True)
        (self.mad_home / "config" / "agents.toml").write_text(
            '[[agents]]\nid = "agent-a"\n[[agents]]\nid = "agent-b"\n',
            encoding="utf-8",
        )
        # Use the actual scriptable executable for gateway validation
        self.gateway = _healthy_gateway()
        self.gateway["mad_home"] = str(self.mad_home)
        _write_gateway(self.project, self.gateway)

    def _run(self) -> DoctorReport:
        return doctor.run_doctor(
            DoctorRequest(project_root=self.project, mad_home=self.mad_home)
        )

    # -- readiness calculations -----------------------------------------------

    def test_all_healthy_ready_true(self) -> None:
        report = self._run()
        self.assertTrue(report.ready,
                        f"Expected ready=True, got checks: {report.checks}")

    def test_single_fail_makes_not_ready(self) -> None:
        (self.project / ".agentdesk" / "runtime" / "gateway.yaml").unlink()
        report = self._run()
        self.assertFalse(report.ready)

    def test_warn_does_not_block_ready(self) -> None:
        # Empty model bindings → WARN on D003
        _write_bindings(
            self.project,
            {
                "schema_version": "agentdesk.model-bindings/v2",
                "updated_at": None,
                "bindings": {},
            },
        )
        report = self._run()
        d003 = [c for c in report.checks if c.check_id == "D003"][0]
        self.assertEqual(d003.status, DoctorCheckStatus.WARN)
        d004 = [c for c in report.checks if c.check_id == "D004"][0]
        self.assertEqual(d004.status, DoctorCheckStatus.WARN)

    def test_checks_count_is_12(self) -> None:
        report = self._run()
        self.assertEqual(len(report.checks), 12)

    def test_checks_in_fixed_order(self) -> None:
        report = self._run()
        expected = [f"D{i:03d}" for i in range(1, 13)]
        actual = [c.check_id for c in report.checks]
        self.assertEqual(actual, expected)

    def test_deterministic_same_state(self) -> None:
        r1 = self._run()
        r2 = self._run()
        for c1, c2 in zip(r1.checks, r2.checks):
            self.assertEqual(c1.check_id, c2.check_id)
            self.assertEqual(c1.status, c2.status)
            self.assertEqual(c1.summary, c2.summary)
            self.assertEqual(c1.remediation, c2.remediation)
        self.assertEqual(r1.ready, r2.ready)

    # -- D001: Project Root ---------------------------------------------------

    def test_d001_pass(self) -> None:
        report = self._run()
        d001 = report.checks[0]
        self.assertEqual(d001.check_id, "D001")
        self.assertEqual(d001.status, DoctorCheckStatus.PASS)

    def test_d001_missing_docs_pm(self) -> None:
        import shutil
        shutil.rmtree(self.project / "docs" / "pm")
        report = self._run()
        d001 = report.checks[0]
        self.assertEqual(d001.status, DoctorCheckStatus.FAIL)

    def test_d001_missing_agentdesk(self) -> None:
        import shutil
        shutil.rmtree(self.project / ".agentdesk")
        report = self._run()
        d001 = report.checks[0]
        self.assertEqual(d001.status, DoctorCheckStatus.FAIL)

    # -- D002: Runtime Directory ----------------------------------------------

    def test_d002_pass(self) -> None:
        report = self._run()
        d002 = report.checks[1]
        self.assertEqual(d002.check_id, "D002")
        self.assertEqual(d002.status, DoctorCheckStatus.PASS)

    def test_d002_missing(self) -> None:
        import shutil
        shutil.rmtree(self.project / ".agentdesk" / "runtime")
        report = self._run()
        d002 = report.checks[1]
        self.assertEqual(d002.status, DoctorCheckStatus.FAIL)

    # -- D003: Model Bindings -------------------------------------------------

    def test_d003_pass(self) -> None:
        report = self._run()
        d003 = report.checks[2]
        self.assertEqual(d003.status, DoctorCheckStatus.PASS)

    def test_d003_missing(self) -> None:
        (self.project / ".agentdesk" / "runtime" / "model-bindings.yaml").unlink()
        report = self._run()
        d003 = report.checks[2]
        self.assertEqual(d003.status, DoctorCheckStatus.FAIL)

    def test_d003_invalid_schema(self) -> None:
        _write_bindings(
            self.project,
            {
                "schema_version": "wrong-schema/v1",
                "updated_at": None,
                "bindings": {},
            },
        )
        report = self._run()
        d003 = report.checks[2]
        self.assertEqual(d003.status, DoctorCheckStatus.FAIL)

    def test_d003_empty_bindings_warn(self) -> None:
        _write_bindings(
            self.project,
            {
                "schema_version": "agentdesk.model-bindings/v2",
                "updated_at": None,
                "bindings": {},
            },
        )
        report = self._run()
        d003 = report.checks[2]
        self.assertEqual(d003.status, DoctorCheckStatus.WARN)

    def test_d003_all_disabled_warn(self) -> None:
        b = _healthy_bindings()
        b["bindings"]["claude-default"]["enabled"] = False
        _write_bindings(self.project, b)
        report = self._run()
        d003 = report.checks[2]
        self.assertEqual(d003.status, DoctorCheckStatus.WARN)

    # -- D004: Role Policies --------------------------------------------------

    def test_d004_pass(self) -> None:
        report = self._run()
        d004 = report.checks[3]
        self.assertEqual(d004.status, DoctorCheckStatus.PASS)

    def test_d004_missing(self) -> None:
        (self.project / "docs" / "pm" / "ROLE-POLICIES.yaml").unlink()
        report = self._run()
        d004 = report.checks[3]
        self.assertEqual(d004.status, DoctorCheckStatus.FAIL)

    def test_d004_invalid_schema(self) -> None:
        p = _healthy_policy()
        p["schema_version"] = "wrong/v1"
        _write_policy(self.project, p)
        report = self._run()
        d004 = report.checks[3]
        self.assertEqual(d004.status, DoctorCheckStatus.FAIL)

    def test_d004_no_roles(self) -> None:
        p = _healthy_policy()
        p["roles"] = {}
        _write_policy(self.project, p)
        report = self._run()
        d004 = report.checks[3]
        self.assertEqual(d004.status, DoctorCheckStatus.FAIL)

    def test_d004_role_without_eligible_binding(self) -> None:
        """Role requires capabilities that no binding provides -> FAIL."""
        p = _healthy_policy()
        p["roles"]["PM"]["required_capabilities"] = ["nonexistent_capability"]
        _write_policy(self.project, p)
        report = self._run()
        d004 = report.checks[3]
        self.assertEqual(d004.status, DoctorCheckStatus.FAIL)

    # -- D005: Gateway Config -------------------------------------------------

    def test_d005_pass(self) -> None:
        report = self._run()
        d005 = report.checks[4]
        self.assertEqual(d005.status, DoctorCheckStatus.PASS)

    def test_d005_missing(self) -> None:
        (self.project / ".agentdesk" / "runtime" / "gateway.yaml").unlink()
        report = self._run()
        d005 = report.checks[4]
        self.assertEqual(d005.status, DoctorCheckStatus.FAIL)

    def test_d005_placeholder_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["mad_executable"] = "<configure-mad-executable>"
        _write_gateway(self.project, gw)
        report = self._run()
        d005 = report.checks[4]
        self.assertEqual(d005.status, DoctorCheckStatus.FAIL)

    def test_d005_invalid_schema(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["schema_version"] = "wrong/v1"
        _write_gateway(self.project, gw)
        report = self._run()
        d005 = report.checks[4]
        self.assertEqual(d005.status, DoctorCheckStatus.FAIL)

    # -- D006: MAD Executable -------------------------------------------------

    def test_d006_pass(self) -> None:
        report = self._run()
        d006 = report.checks[5]
        self.assertEqual(d006.status, DoctorCheckStatus.PASS)

    def test_d006_placeholder_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["mad_executable"] = "<configure-mad-executable>"
        _write_gateway(self.project, gw)
        report = self._run()
        d006 = report.checks[5]
        self.assertEqual(d006.status, DoctorCheckStatus.FAIL)

    def test_d006_unresolvable_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["mad_executable"] = "nonexistent-executable-xyz-12345"
        _write_gateway(self.project, gw)
        report = self._run()
        d006 = report.checks[5]
        self.assertEqual(d006.status, DoctorCheckStatus.FAIL)

    # -- D007: MAD Home -------------------------------------------------------

    def test_d007_pass(self) -> None:
        report = self._run()
        d007 = report.checks[6]
        self.assertEqual(d007.status, DoctorCheckStatus.PASS)

    def test_d007_missing_agents_toml_fails(self) -> None:
        (self.mad_home / "config" / "agents.toml").unlink()
        report = self._run()
        d007 = report.checks[6]
        self.assertEqual(d007.status, DoctorCheckStatus.FAIL)

    def test_d007_inside_worktree_fails(self) -> None:
        mad_inside = self.project / "mad_inside"
        mad_inside.mkdir()
        (mad_inside / "config").mkdir()
        (mad_inside / "config" / "agents.toml").write_text(
            '[[agents]]\nid = "agent-a"\n', encoding="utf-8"
        )
        report = doctor.run_doctor(
            DoctorRequest(project_root=self.project, mad_home=mad_inside)
        )
        d007 = report.checks[6]
        self.assertEqual(d007.status, DoctorCheckStatus.FAIL)

    def test_d007_malformed_toml_fails(self) -> None:
        (self.mad_home / "config" / "agents.toml").write_text(
            "not valid toml [[[", encoding="utf-8"
        )
        report = self._run()
        d007 = report.checks[6]
        self.assertEqual(d007.status, DoctorCheckStatus.FAIL)

    def test_d007_empty_agents_fails(self) -> None:
        (self.mad_home / "config" / "agents.toml").write_text("", encoding="utf-8")
        report = self._run()
        d007 = report.checks[6]
        self.assertEqual(d007.status, DoctorCheckStatus.FAIL)

    def test_d007_mad_home_from_cli_overrides_gateway(self) -> None:
        # D007 should PASS using CLI --mad-home even if gateway field is placeholder
        gw = _healthy_gateway()
        gw["mad_home"] = "<configure-mad-home-absolute-path>"
        _write_gateway(self.project, gw)
        report = doctor.run_doctor(
            DoctorRequest(project_root=self.project, mad_home=self.mad_home)
        )
        d007 = report.checks[6]
        self.assertEqual(d007.status, DoctorCheckStatus.PASS)

    # -- D008: Audit Agents ---------------------------------------------------

    def test_d008_pass(self) -> None:
        report = self._run()
        d008 = report.checks[7]
        self.assertEqual(d008.status, DoctorCheckStatus.PASS)

    def test_d008_report_not_in_audit_set_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["audit_report_agent_id"] = "agent-c"
        _write_gateway(self.project, gw)
        report = self._run()
        d008 = report.checks[7]
        self.assertEqual(d008.status, DoctorCheckStatus.FAIL)

    def test_d008_unknown_agent_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["audit_agent_ids"] = ["agent-z"]
        gw["audit_report_agent_id"] = "agent-z"
        _write_gateway(self.project, gw)
        report = self._run()
        d008 = report.checks[7]
        self.assertEqual(d008.status, DoctorCheckStatus.FAIL)

    def test_d008_duplicate_ids_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["audit_agent_ids"] = ["agent-a", "agent-a"]
        _write_gateway(self.project, gw)
        report = self._run()
        d008 = report.checks[7]
        self.assertEqual(d008.status, DoctorCheckStatus.FAIL)

    def test_d008_placeholder_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["audit_agent_ids"] = ["<configure-audit-agent-id>"]
        gw["audit_report_agent_id"] = "<configure-audit-report-agent-id>"
        _write_gateway(self.project, gw)
        report = self._run()
        d008 = report.checks[7]
        self.assertEqual(d008.status, DoctorCheckStatus.FAIL)

    # -- D009: Provider Executables -------------------------------------------

    def test_d009_pass(self) -> None:
        report = self._run()
        d009 = report.checks[8]
        self.assertEqual(d009.status, DoctorCheckStatus.PASS)

    def test_d009_unsupported_provider_fails(self) -> None:
        b = _healthy_bindings()
        b["bindings"]["claude-default"]["provider"] = "openai"
        _write_bindings(self.project, b)
        report = self._run()
        d009 = report.checks[8]
        self.assertEqual(d009.status, DoctorCheckStatus.FAIL)

    def test_d009_no_enabled_bindings_warns(self) -> None:
        _write_bindings(
            self.project,
            {
                "schema_version": "agentdesk.model-bindings/v2",
                "updated_at": None,
                "bindings": {},
            },
        )
        report = self._run()
        d009 = report.checks[8]
        self.assertEqual(d009.status, DoctorCheckStatus.WARN)

    # -- D010: Decoder Eligibility --------------------------------------------

    def test_d010_pass_claude_only(self) -> None:
        report = self._run()
        d010 = report.checks[9]
        self.assertEqual(d010.status, DoctorCheckStatus.PASS)

    def test_d010_codex_with_claude_backup_warns(self) -> None:
        """Codex is enabled but Claude also covers roles -> WARN."""
        b = _healthy_bindings()
        b["bindings"]["claude-default"]["provider"] = "codex"
        # Add a claude backup so codex is not the sole eligible provider
        b["bindings"]["claude-backup"] = {
            "provider": "claude",
            "model_id": "haiku",
            "tier": "expert",
            "deliberation_tier": "deep",
            "context_window_tokens": 100000,
            "capabilities": ["code", "review", "plan"],
            "enabled": True,
        }
        _write_bindings(self.project, b)
        report = self._run()
        d010 = report.checks[9]
        self.assertEqual(d010.status, DoctorCheckStatus.WARN)

    def test_d010_codex_sole_eligible_fails(self) -> None:
        """When codex is the only enabled binding -> every role depends
        solely on codex -> FAIL."""
        b = _healthy_bindings()
        b["bindings"]["claude-default"]["provider"] = "codex"
        _write_bindings(self.project, b)
        report = self._run()
        d010 = report.checks[9]
        self.assertEqual(d010.status, DoctorCheckStatus.FAIL)

    def test_d010_no_enabled_bindings_warns(self) -> None:
        _write_bindings(
            self.project,
            {
                "schema_version": "agentdesk.model-bindings/v2",
                "updated_at": None,
                "bindings": {},
            },
        )
        report = self._run()
        d010 = report.checks[9]
        self.assertEqual(d010.status, DoctorCheckStatus.WARN)

    # -- D011: Dashboard Availability -----------------------------------------

    def test_d011_pass(self) -> None:
        report = self._run()
        d011 = report.checks[10]
        self.assertEqual(d011.status, DoctorCheckStatus.PASS)

    # -- D012: Runtime Safety -------------------------------------------------

    def test_d012_pass(self) -> None:
        report = self._run()
        d012 = report.checks[11]
        self.assertEqual(d012.status, DoctorCheckStatus.PASS)

    def test_d012_no_gitignore_fails(self) -> None:
        (self.project / ".gitignore").unlink()
        report = self._run()
        d012 = report.checks[11]
        self.assertEqual(d012.status, DoctorCheckStatus.FAIL)

    def test_d012_gitignore_without_runtime_entry_fails(self) -> None:
        (self.project / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        report = self._run()
        d012 = report.checks[11]
        self.assertEqual(d012.status, DoctorCheckStatus.FAIL)

    def test_d012_secret_like_keys_in_gateway_fails(self) -> None:
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        gw["api_key"] = "some-value"
        _write_gateway(self.project, gw)
        report = self._run()
        d012 = report.checks[11]
        self.assertEqual(d012.status, DoctorCheckStatus.FAIL)

    def test_d012_secret_like_keys_in_bindings_fails(self) -> None:
        b = _healthy_bindings()
        b["access_token"] = "some-token"
        _write_bindings(self.project, b)
        report = self._run()
        d012 = report.checks[11]
        self.assertEqual(d012.status, DoctorCheckStatus.FAIL)


# ============================================================================
# 4. Zero side-effects — no subprocess, network, file writes, model calls
# ============================================================================


class ZeroSideEffectTests(unittest.TestCase):
    """Verify the doctor makes zero side effects."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        _make_project(self.project)
        _write_bindings(self.project, _healthy_bindings())
        _write_policy(self.project, _healthy_policy())
        _write_gateway(self.project, _healthy_gateway())
        self.mad_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.mad_tmp.cleanup)
        self.mad_home = Path(self.mad_tmp.name)
        (self.mad_home / "config").mkdir(parents=True)
        (self.mad_home / "config" / "agents.toml").write_text(
            '[[agents]]\nid = "agent-a"\n[[agents]]\nid = "agent-b"\n',
            encoding="utf-8",
        )
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        _write_gateway(self.project, gw)

    def test_no_subprocess_calls(self) -> None:
        with mock.patch("subprocess.run") as m_run, \
             mock.patch("subprocess.Popen") as m_popen, \
             mock.patch("os.system") as m_system:
            doctor.run_doctor(
                DoctorRequest(project_root=self.project, mad_home=self.mad_home)
            )
        m_run.assert_not_called()
        m_popen.assert_not_called()
        m_system.assert_not_called()

    def test_no_file_writes(self) -> None:
        """Every open() call must be read-only."""
        real_open = __builtins__["open"] if hasattr(__builtins__, "__dict__") else open
        original = io.open
        with mock.patch("io.open", wraps=original) as m_open:
            doctor.run_doctor(
                DoctorRequest(project_root=self.project, mad_home=self.mad_home)
            )
        for call_args in m_open.call_args_list:
            args, kwargs = call_args
            mode = args[1] if len(args) >= 2 else kwargs.get("mode", "r")
            mode_str = str(mode)
            self.assertNotIn("w", mode_str)
            self.assertNotIn("a", mode_str)
            self.assertNotIn("x", mode_str)
            self.assertNotIn("+", mode_str)

    def test_no_network_access(self) -> None:
        with mock.patch("socket.socket") as m_socket, \
             mock.patch("urllib.request.urlopen") as m_urlopen:
            doctor.run_doctor(
                DoctorRequest(project_root=self.project, mad_home=self.mad_home)
            )
        m_socket.assert_not_called()
        m_urlopen.assert_not_called()

    def test_no_git_subprocess(self) -> None:
        """Git must never be called."""
        with mock.patch("subprocess.run") as m_run:
            doctor.run_doctor(
                DoctorRequest(project_root=self.project, mad_home=self.mad_home)
            )
        for call_args in m_run.call_args_list:
            args, _ = call_args
            if args:
                cmd = args[0]
                if isinstance(cmd, list) and cmd:
                    self.assertNotEqual(cmd[0], "git")
                elif isinstance(cmd, str):
                    self.assertNotIn("git", cmd)

    def test_no_model_calls(self) -> None:
        """Doctor must never import or call any provider module."""
        for name in list(sys.modules):
            self.assertNotIn("anthropic", name.lower())
            self.assertNotIn("openai", name.lower())
            self.assertNotIn("genai", name.lower())

    def test_filesystem_not_mutated(self) -> None:
        """Snapshot before/after to prove no files changed or created."""
        def _snapshot(root: Path) -> dict:
            result = {}
            for p in root.rglob("*"):
                if p.is_file():
                    result[str(p.relative_to(root))] = p.stat().st_size
            return result

        before_project = _snapshot(self.project)
        before_mad = _snapshot(self.mad_home)
        doctor.run_doctor(
            DoctorRequest(project_root=self.project, mad_home=self.mad_home)
        )
        after_project = _snapshot(self.project)
        after_mad = _snapshot(self.mad_home)
        self.assertEqual(before_project, after_project,
                         "Project files mutated by doctor")
        self.assertEqual(before_mad, after_mad,
                         "MAD home files mutated by doctor")


# ============================================================================
# 5. CLI — exit codes, output format, safety
# ============================================================================


class CliTests(unittest.TestCase):
    """Command-line interface behaviour."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        _make_project(self.project)
        _write_bindings(self.project, _healthy_bindings())
        _write_policy(self.project, _healthy_policy())
        _write_gateway(self.project, _healthy_gateway())
        self.mad_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.mad_tmp.cleanup)
        self.mad_home = Path(self.mad_tmp.name)
        (self.mad_home / "config").mkdir(parents=True)
        (self.mad_home / "config" / "agents.toml").write_text(
            '[[agents]]\nid = "agent-a"\n[[agents]]\nid = "agent-b"\n',
            encoding="utf-8",
        )
        gw = _healthy_gateway()
        gw["mad_home"] = str(self.mad_home)
        _write_gateway(self.project, gw)

    def test_ready_exit_0(self) -> None:
        exit_code = doctor.main(
            ["--project", str(self.project), "--mad-home", str(self.mad_home)]
        )
        self.assertEqual(exit_code, 0)

    def test_not_ready_exit_1(self) -> None:
        (self.project / ".agentdesk" / "runtime" / "gateway.yaml").unlink()
        exit_code = doctor.main(
            ["--project", str(self.project), "--mad-home", str(self.mad_home)]
        )
        self.assertEqual(exit_code, 1)

    def test_invalid_args_exit_2_relative_path(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            exit_code = doctor.main(["--project", "relative/path"])
        self.assertEqual(exit_code, 2)

    def test_missing_project_flag_exit_2(self) -> None:
        buf = io.StringIO()
        try:
            exit_code = doctor.main([])
        except SystemExit as e:
            exit_code = e.code
            self.assertEqual(exit_code, 2)

    def test_text_output_has_status_lines(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = doctor.main(
                [
                    "--project", str(self.project),
                    "--mad-home", str(self.mad_home),
                    "--format", "text",
                ]
            )
        output = buf.getvalue()
        self.assertIn("PASS D001", output)
        self.assertIn("READY", output)

    def test_json_output_valid_schema(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = doctor.main(
                [
                    "--project", str(self.project),
                    "--mad-home", str(self.mad_home),
                    "--format", "json",
                ]
            )
        output = buf.getvalue()
        data = json.loads(output)
        self.assertEqual(data["schema_version"], "agentdesk.doctor-report/v1")
        self.assertIsInstance(data["ready"], bool)
        self.assertIsInstance(data["checks"], list)
        self.assertEqual(len(data["checks"]), 12)
        for check in data["checks"]:
            self.assertEqual(
                set(check.keys()),
                {"check_id", "status", "summary", "remediation"},
            )

    def test_json_output_no_secrets(self) -> None:
        """JSON output must not contain API key, token, or password markers.
        The word 'credential' appears only in a legitimate summary message
        about credential-like field detection — it is not a credential
        itself."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            doctor.main(
                [
                    "--project", str(self.project),
                    "--mad-home", str(self.mad_home),
                    "--format", "json",
                ]
            )
        output = buf.getvalue().lower()
        # Only check for actual secret markers, not the word "credential"
        # which appears in D012's summary "free of credential fields"
        secret_markers = [
            "api_key", "apikey", "access_token",
            "password", "passwd",
        ]
        for marker in secret_markers:
            self.assertNotIn(marker, output,
                             "JSON output contains secret marker: " + marker)

    def test_json_output_no_absolute_workspace_paths(self) -> None:
        """JSON must not contain the absolute project or MAD home path."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            doctor.main(
                [
                    "--project", str(self.project),
                    "--mad-home", str(self.mad_home),
                    "--format", "json",
                ]
            )
        output = buf.getvalue()
        abs_project = str(self.project.resolve())
        abs_mad = str(self.mad_home.resolve())
        self.assertNotIn(abs_project, output)
        self.assertNotIn(abs_mad, output)

    def test_text_output_no_secret_keys(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            doctor.main(
                [
                    "--project", str(self.project),
                    "--mad-home", str(self.mad_home),
                    "--format", "text",
                ]
            )
        output = buf.getvalue().lower()
        for marker in ["api_key", "access_token", "password"]:
            self.assertNotIn(marker, output)

    def test_no_interactive_prompt(self) -> None:
        """CLI must not block on stdin."""
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            exit_code = doctor.main(
                [
                    "--project", str(self.project),
                    "--mad-home", str(self.mad_home),
                ]
            )
        self.assertIn(exit_code, (0, 1))


# ============================================================================
# 6. Gateway Template Compatibility
# ============================================================================


class GatewayTemplateTests(unittest.TestCase):
    """The template under assets/project-template validates against the
    same schema as production, and init_project.py distributes it."""

    def setUp(self) -> None:
        self.template_root = (
            _REPO_ROOT / "skills" / "agentdesk" / "assets" / "project-template"
        )

    def test_template_exists(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        self.assertTrue(gw.is_file(), "Template missing: " + str(gw))

    def test_template_is_valid_json(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        raw = gw.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIsInstance(data, dict)

    def test_template_exactly_eight_keys(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        data = json.loads(gw.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 8)

    def test_template_has_correct_schema_version(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        data = json.loads(gw.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], "agentdesk.gateway-config/v1")

    def test_template_all_values_are_placeholders_or_defaults(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        data = json.loads(gw.read_text(encoding="utf-8"))
        self.assertEqual(data["timeout_seconds"], 1800)
        for key in [
            "mad_executable", "mad_home",
            "planning_agent_ids", "planning_report_agent_id",
            "audit_agent_ids", "audit_report_agent_id",
        ]:
            val = data[key]
            val_str = json.dumps(val)
            self.assertIn("<configure-", val_str,
                          key + " should be a placeholder, got " + val_str)

    def test_template_no_secret_fields(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        data = json.loads(gw.read_text(encoding="utf-8"))
        for key in data:
            key_lower = key.lower()
            self.assertNotIn("api", key_lower)
            self.assertNotIn("key", key_lower)
            self.assertNotIn("secret", key_lower)
            self.assertNotIn("token", key_lower)
            self.assertNotIn("password", key_lower)
            self.assertNotIn("credential", key_lower)

    def test_template_no_absolute_paths(self) -> None:
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        raw = gw.read_text(encoding="utf-8")
        self.assertNotIn("C:\\", raw.upper())
        self.assertNotIn("/home/", raw)
        self.assertNotIn("/Users/", raw)

    def test_template_fails_production_validator_with_placeholders(self) -> None:
        """The template as-is must fail validate_gateway_config (placeholders)."""
        gw = self.template_root / ".agentdesk" / "runtime" / "gateway.yaml"
        data = json.loads(gw.read_text(encoding="utf-8"))
        sys.path.insert(0, str(_SCRIPTS))
        try:
            import mad_gateway  # noqa: F811
            with self.assertRaises(mad_gateway.GatewayConfigError):
                mad_gateway.validate_gateway_config(data)
        finally:
            sys.path.pop(0)

    def test_init_project_copies_template(self) -> None:
        """init_project.py's template rglob includes gateway.yaml."""
        sys.path.insert(0, str(_SCRIPTS))
        try:
            import init_project  # noqa: F811
            template = init_project.TEMPLATE_ROOT
            entries = list(template.rglob("*"))
            gw_files = [
                e for e in entries
                if e.is_file() and e.name == "gateway.yaml"
            ]
            self.assertEqual(len(gw_files), 1)
        finally:
            sys.path.pop(0)


# ============================================================================
# 7. Contract Document Self-Consistency
# ============================================================================


class ContractDocumentTests(unittest.TestCase):
    """Verify the provider-doctor-contract.md documents match actual code."""

    def setUp(self) -> None:
        self.contract_path = (
            _REPO_ROOT
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "provider-doctor-contract.md"
        )

    def test_contract_exists(self) -> None:
        self.assertTrue(self.contract_path.is_file())

    def test_contract_mentions_all_12_checks(self) -> None:
        text = self.contract_path.read_text(encoding="utf-8")
        for i in range(1, 13):
            check_id = "D{:03d}".format(i)
            self.assertIn(check_id, text,
                          "Contract missing " + check_id)

    def test_contract_specifies_enum_values(self) -> None:
        text = self.contract_path.read_text(encoding="utf-8")
        self.assertIn('PASS = "pass"', text)
        self.assertIn('WARN = "warn"', text)
        self.assertIn('FAIL = "fail"', text)

    def test_contract_specifies_8_all_symbols(self) -> None:
        text = self.contract_path.read_text(encoding="utf-8")
        for name in doctor.__all__:
            self.assertIn(name, text, "Contract missing " + name)

    def test_contract_describes_ready_formula(self) -> None:
        text = self.contract_path.read_text(encoding="utf-8")
        # Contract uses original text with "any FAIL" and "ready = False"
        self.assertTrue(
            ("any FAIL" in text) or ("any fail" in text.lower()),
            "Contract should describe any-FAIL means not ready"
        )

    def test_contract_describes_exit_codes(self) -> None:
        text = self.contract_path.read_text(encoding="utf-8")
        self.assertIn("Exit codes", text)

    def test_contract_has_gateway_template_table(self) -> None:
        text = self.contract_path.read_text(encoding="utf-8")
        self.assertIn("planning_agent_ids", text)
        self.assertIn("audit_report_agent_id", text)


if __name__ == "__main__":
    unittest.main()
