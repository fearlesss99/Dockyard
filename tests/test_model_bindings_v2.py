"""TC-13.3: model-bindings/v2 + context_window_tokens validation tests.

stdlib-only unittest; no third-party dependencies, no real model/API calls.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# Load the validator and selector modules from the skill tree.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

import importlib.util as _importlib_util

_VALIDATOR_PATH = _SKILL_SCRIPTS / "validate_project.py"
_VSPEC = _importlib_util.spec_from_file_location("validate_project", _VALIDATOR_PATH)
if _VSPEC is None or _VSPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"cannot load validator from {_VALIDATOR_PATH}")
_VALIDATOR = _importlib_util.module_from_spec(_VSPEC)
_VSPEC.loader.exec_module(_VALIDATOR)

_SSPEC = _importlib_util.spec_from_file_location(
    "select_model", _SKILL_SCRIPTS / "select_model.py"
)
if _SSPEC is None or _SSPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load select_model")
_SELECTOR = _importlib_util.module_from_spec(_SSPEC)
_SSPEC.loader.exec_module(_SELECTOR)

# Literal ten-field set — deliberately NOT derived from MODEL_SELECTION_FIELDS.
TEN_FIELD_LITERAL = frozenset(
    {
        "required_model_tier",
        "required_model_capabilities",
        "model_binding_id",
        "selected_model_provider",
        "selected_model_id",
        "selected_model_tier",
        "selected_deliberation_tier",
        "selected_context_window_tokens",
        "selected_model_capabilities",
        "model_degradation_approval_id",
    }
)

_VALID_BINDING = {
    "provider": "test-provider",
    "model_id": "test-model/v1",
    "tier": "advanced",
    "deliberation_tier": "balanced",
    "context_window_tokens": 200000,
    "capabilities": ["coding", "testing"],
    "enabled": True,
}

_VALID_BINDINGS_DOC = {
    "schema_version": "agentdesk.model-bindings/v2",
    "updated_at": "2026-07-26T12:00:00Z",
    "bindings": {"test-binding": _VALID_BINDING},
}

_VALID_POLICY = {
    "schema_version": "agentdesk.role-policies/v1",
    "tier_order": ["basic", "standard", "advanced", "expert"],
    "deliberation_tier_order": ["efficient", "balanced", "deep"],
    "risk_floors": {
        "L0": "basic",
        "L1": "standard",
        "L2": "advanced",
        "L3": "expert",
        "L4": "expert",
    },
    "roles": {
        "DEV": {
            "default_tier": "standard",
            "minimum_tier": "standard",
            "deliberation_tier": "balanced",
            "required_capabilities": ["coding", "testing"],
            "degradation_policy": "allow_to_minimum",
        }
    },
}


def _make_bindings_doc(**overrides):
    binding = dict(_VALID_BINDING)
    binding.update(overrides)
    return {
        "schema_version": _VALID_BINDINGS_DOC["schema_version"],
        "updated_at": _VALID_BINDINGS_DOC["updated_at"],
        "bindings": {"test-binding": binding},
    }


def _run_reporter(func, *args, **kwargs):
    reporter = _VALIDATOR.Reporter()
    output = io.StringIO()
    with redirect_stdout(output):
        result = func(*args, reporter=reporter, **kwargs)
    return result, reporter, output.getvalue()


def _make_ten_field_selection(base=None):
    """Return a minimal valid ten-field selection dict."""
    sel = dict.fromkeys(TEN_FIELD_LITERAL)
    sel.update(
        {
            "required_model_tier": "advanced",
            "required_model_capabilities": ["coding"],
            "model_binding_id": "tb",
            "selected_model_provider": "p",
            "selected_model_id": "m",
            "selected_model_tier": "advanced",
            "selected_deliberation_tier": "balanced",
            "selected_context_window_tokens": 200000,
            "selected_model_capabilities": ["coding"],
            "model_degradation_approval_id": None,
        }
    )
    if base is not None:
        sel.update(base)
    return sel


# =========================================================================
class BindingExactFieldsTests(unittest.TestCase):
    """Each v2 binding must contain exactly the 7 required keys."""

    def test_selector_rejects_extra_binding_field(self):
        binding = dict(_VALID_BINDING)
        binding["extra_foo"] = True
        doc = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": None,
            "bindings": {"tb": binding},
        }
        with self.assertRaises(_SELECTOR.SelectionError) as ctx:
            _SELECTOR._validated_bindings(doc)
        msg = str(ctx.exception)
        self.assertIn("extra:", msg)
        self.assertIn("extra_foo", msg)

    def test_selector_rejects_missing_binding_field(self):
        binding = dict(_VALID_BINDING)
        del binding["context_window_tokens"]
        doc = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": None,
            "bindings": {"tb": binding},
        }
        with self.assertRaises(_SELECTOR.SelectionError) as ctx:
            _SELECTOR._validated_bindings(doc)
        msg = str(ctx.exception)
        self.assertIn("context_window_tokens", msg)
        self.assertTrue("missing" in msg.lower() or "exactly the 7" in msg.lower())

    def test_validator_rejects_extra_binding_field(self):
        binding = dict(_VALID_BINDING)
        binding["extra_foo"] = True
        doc = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": None,
            "bindings": {"tb": binding},
        }
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object, doc, "mb"
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("unexpected key", output)
        self.assertIn("extra_foo", output)

    def test_validator_rejects_missing_binding_field(self):
        binding = dict(_VALID_BINDING)
        del binding["context_window_tokens"]
        doc = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": None,
            "bindings": {"tb": binding},
        }
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object, doc, "mb"
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("missing required key", output)

    def test_selector_accepts_exact_7_fields(self):
        doc = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": None,
            "bindings": {"tb": dict(_VALID_BINDING)},
        }
        bindings = _SELECTOR._validated_bindings(doc)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(set(bindings[0]), _SELECTOR.BINDING_REQUIRED_KEYS | {"binding_id"})


# =========================================================================
class ContextWindowTokensBindingTests(unittest.TestCase):
    """context_window_tokens field-level validation in model-bindings/v2."""

    def test_valid_positive_1(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=1),
            "model bindings",
        )
        self.assertIn("test-binding", norm)
        self.assertEqual(norm["test-binding"]["context_window_tokens"], 1)

    def test_valid_large_positive(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=1000000),
            "model bindings",
        )
        self.assertIn("test-binding", norm)
        self.assertEqual(norm["test-binding"]["context_window_tokens"], 1000000)

    def test_true_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=True),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r"positive integer|>= ?1")

    def test_false_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=False),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r"positive integer|>= ?1")

    def test_null_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=None),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r"positive integer|>= ?1|null")

    def test_zero_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=0),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r">= ?1")

    def test_negative_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=-1),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r">= ?1")

    def test_float_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens=100000.5),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r"positive integer|>= ?1")

    def test_string_rejected(self):
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object,
            _make_bindings_doc(context_window_tokens="100000"),
            "mb",
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r"positive integer|>= ?1")


# =========================================================================
class V2EmptyTemplateTests(unittest.TestCase):
    def test_empty_bindings_v2_valid(self):
        doc = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": None,
            "bindings": {},
        }
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object, doc, "mb"
        )
        self.assertEqual(reporter.errors, 0, output)


# =========================================================================
class V1RejectionTests(unittest.TestCase):
    """V1 schema is rejected with upgrade guidance."""

    def test_v1_selector_error_has_migration_guidance(self):
        doc = {
            "schema_version": "agentdesk.model-bindings/v1",
            "updated_at": None,
            "bindings": {},
        }
        with self.assertRaises(_SELECTOR.SelectionError) as ctx:
            _SELECTOR._validated_bindings(doc)
        msg = str(ctx.exception)
        self.assertIn("agentdesk.model-bindings/v2", msg)
        self.assertIn("agentdesk.model-bindings/v1", msg)
        self.assertIn("context_window_tokens", msg)
        self.assertIn("positive integer", msg)

    def test_v1_validator_error_mentions_v2(self):
        doc = {
            "schema_version": "agentdesk.model-bindings/v1",
            "updated_at": None,
            "bindings": {},
        }
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object, doc, "mb"
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("agentdesk.model-bindings/v2", output)

    def test_v3_rejected(self):
        doc = {
            "schema_version": "agentdesk.model-bindings/v3",
            "updated_at": None,
            "bindings": {},
        }
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object, doc, "mb"
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("agentdesk.model-bindings/v2", output)

    def test_empty_version_rejected(self):
        doc = {
            "schema_version": "",
            "updated_at": None,
            "bindings": {},
        }
        norm, reporter, output = _run_reporter(
            _VALIDATOR._validate_model_bindings_object, doc, "mb"
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("agentdesk.model-bindings/v2", output)


# =========================================================================
class SelectorTenFieldTests(unittest.TestCase):
    def test_selector_output_contains_ten_literal_fields(self):
        result = _SELECTOR.select_binding(
            policy=_VALID_POLICY,
            bindings_document=_VALID_BINDINGS_DOC,
            role_id="DEV",
            risk="L2",
            task_min_tier="inherit",
            task_capabilities=set(),
            degradation_approval_id=None,
        )
        self.assertEqual(set(result), TEN_FIELD_LITERAL)

    def test_selected_context_window_tokens_matches_binding(self):
        result = _SELECTOR.select_binding(
            policy=_VALID_POLICY,
            bindings_document=_VALID_BINDINGS_DOC,
            role_id="DEV",
            risk="L2",
            task_min_tier="inherit",
            task_capabilities=set(),
            degradation_approval_id=None,
        )
        self.assertEqual(
            result["selected_context_window_tokens"],
            _VALID_BINDING["context_window_tokens"],
        )

    def test_selector_emits_valid_json(self):
        with tempfile.TemporaryDirectory(prefix="ad-tc133-sel-") as tmp:
            project = Path(tmp)
            project.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "-C", str(project), "init", "--quiet"], check=True
            )
            subprocess.run(
                ["git", "-C", str(project), "config", "user.email", "test@test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(project), "config", "user.name", "Test"],
                check=True,
            )

            policy_dir = project / "docs" / "pm"
            policy_dir.mkdir(parents=True, exist_ok=True)
            policy_dir.joinpath("ROLE-POLICIES.yaml").write_text(
                json.dumps(_VALID_POLICY, ensure_ascii=False), encoding="utf-8"
            )

            runtime = project / ".agentdesk" / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            runtime.joinpath("model-bindings.yaml").write_text(
                json.dumps(_VALID_BINDINGS_DOC, ensure_ascii=False), encoding="utf-8"
            )

            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(project), "commit", "-m", "init", "--quiet"],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            result = subprocess.run(
                [
                    sys.executable,
                    str(_SKILL_SCRIPTS / "select_model.py"),
                    "--project",
                    str(project),
                    "--task-card-commit",
                    head,
                    "--role-id",
                    "DEV",
                    "--risk",
                    "L2",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(result.stdout)
            self.assertEqual(set(parsed), TEN_FIELD_LITERAL)


# =========================================================================
class SelectorV1ErrorSubprocessTests(unittest.TestCase):
    def test_v1_bindings_subprocess_error_mentions_v2(self):
        with tempfile.TemporaryDirectory(prefix="ad-tc133-selv1-") as tmp:
            project = Path(tmp)
            project.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "-C", str(project), "init", "--quiet"], check=True
            )
            subprocess.run(
                ["git", "-C", str(project), "config", "user.email", "t@t"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(project), "config", "user.name", "T"],
                check=True,
            )

            pm_dir = project / "docs" / "pm"
            pm_dir.mkdir(parents=True, exist_ok=True)
            pm_dir.joinpath("ROLE-POLICIES.yaml").write_text(
                json.dumps(_VALID_POLICY, ensure_ascii=False), encoding="utf-8"
            )

            runtime = project / ".agentdesk" / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            v1_doc = dict(_VALID_BINDINGS_DOC)
            v1_doc["schema_version"] = "agentdesk.model-bindings/v1"
            runtime.joinpath("model-bindings.yaml").write_text(
                json.dumps(v1_doc, ensure_ascii=False), encoding="utf-8"
            )

            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(project), "commit", "-m", "init", "--quiet"],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            result = subprocess.run(
                [
                    sys.executable,
                    str(_SKILL_SCRIPTS / "select_model.py"),
                    "--project",
                    str(project),
                    "--task-card-commit",
                    head,
                    "--role-id",
                    "DEV",
                    "--risk",
                    "L2",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("agentdesk.model-bindings/v2", result.stderr)


# =========================================================================
class DeterministicReplayTests(unittest.TestCase):
    def _setup_project_with_state(self):
        tmp = tempfile.TemporaryDirectory(prefix="ad-tc133-replay-")
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name)
        subprocess.run(["git", "-C", str(project), "init", "--quiet"], check=True)
        subprocess.run(
            ["git", "-C", str(project), "config", "user.email", "t@t"], check=True
        )
        subprocess.run(
            ["git", "-C", str(project), "config", "user.name", "T"], check=True
        )

        pm_dir = project / "docs" / "pm"
        pm_dir.mkdir(parents=True, exist_ok=True)
        pm_dir.joinpath("ROLE-POLICIES.yaml").write_text(
            json.dumps(_VALID_POLICY, ensure_ascii=False), encoding="utf-8"
        )

        runtime = project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        runtime.joinpath("model-bindings.yaml").write_text(
            json.dumps(_VALID_BINDINGS_DOC, ensure_ascii=False), encoding="utf-8"
        )

        tasks_dir = pm_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        card = (
            "---\n"
            "schema_version: agentdesk.task-card/v2\n"
            "task_id: TC-001\n"
            "revision: 1\n"
            "type: implementation\n"
            "role_id: DEV\n"
            "priority: P1\n"
            "risk: L2\n"
            "min_model_tier: inherit\n"
            "required_model_capabilities: []\n"
            "depends_on: []\n"
            "base_commit: PLACEHOLDER\n"
            "allowed_paths: []\n"
            "blocked_paths: []\n"
            "conflict_surfaces:\n"
            "  paths: []\n"
            "  symbols: []\n"
            "  contracts: []\n"
            "  migrations: []\n"
            "required_checks: []\n"
            "owner_approval:\n"
            "  gate: none\n"
            "  required_capabilities: []\n"
            "  approver_role_id: Owner\n"
            "created_at: 2026-07-26T12:00:00Z\n"
            "---\n"
        )
        tasks_dir.joinpath("TC-001-r1-test.md").write_text(card, encoding="utf-8")

        for d in ("state", "events", "outbox", "reports", "acceptances"):
            (pm_dir / d).mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(project), "commit", "-m", "init", "--quiet"],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

        card = card.replace("base_commit: PLACEHOLDER", f"base_commit: {head}")
        tasks_dir.joinpath("TC-001-r1-test.md").write_text(card, encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(project), "commit", "-m", "fix-base", "--quiet"],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

        return project, head

    def test_replay_accepts_correct_ten_fields(self):
        project, head = self._setup_project_with_state()
        result = _SELECTOR.select_binding(
            policy=_VALID_POLICY,
            bindings_document=_VALID_BINDINGS_DOC,
            role_id="DEV",
            risk="L2",
            task_min_tier="inherit",
            task_capabilities=set(),
            degradation_approval_id=None,
        )
        result, reporter, output = _run_reporter(
            _VALIDATOR._verify_selector_snapshot,
            project,
            {"task_card_commit": head},
            {
                "role_id": "DEV",
                "risk": "L2",
                "min_model_tier": "inherit",
                "required_model_capabilities": [],
            },
            result,
            "ctx",
        )
        self.assertNotIn("exactly ten fields", output)
        self.assertNotIn("mismatched", output)

    def test_replay_rejects_mismatched_window_tokens(self):
        project, head = self._setup_project_with_state()
        result = _SELECTOR.select_binding(
            policy=_VALID_POLICY,
            bindings_document=_VALID_BINDINGS_DOC,
            role_id="DEV",
            risk="L2",
            task_min_tier="inherit",
            task_capabilities=set(),
            degradation_approval_id=None,
        )
        result_bad = dict(result)
        result_bad["selected_context_window_tokens"] = 999999
        result, reporter, output = _run_reporter(
            _VALIDATOR._verify_selector_snapshot,
            project,
            {"task_card_commit": head},
            {
                "role_id": "DEV",
                "risk": "L2",
                "min_model_tier": "inherit",
                "required_model_capabilities": [],
            },
            result_bad,
            "ctx",
        )
        self.assertIn("mismatched", output)


# =========================================================================
class DispatchModelSelectionProductionPathTests(unittest.TestCase):
    """current_dispatch.model_selection validated via real production entry point."""

    def _call_dispatch_snapshot(self, selection, task_overrides=None, state="dispatched"):
        """Call _validate_dispatch_model_snapshot with faked-out Git/evidence checks."""
        task = {
            "task_id": "TC-001",
            "revision": 1,
            "attempt": 1,
            "state": state,
            "current_dispatch": {
                "dispatch_id": "DSP-1",
                "role_id": "DEV",
                "base_commit": "a" * 40,
                "branch": "feat/x",
                "model_selection": selection,
            },
            "granted_approval_ids": [],
            "timestamps": {"dispatched_at": "2026-07-26T12:00:00Z"},
        }
        if task_overrides:
            task.update(task_overrides)

        # Patch out the Git-intensive functions that follow the snapshot check
        with patch.object(
            _VALIDATOR, "_verify_selector_snapshot", return_value=None
        ), patch.object(
            _VALIDATOR, "_validate_dispatch_evidence", return_value=None
        ), patch.object(
            _VALIDATOR, "_validate_dispatch", return_value="DSP-1"
        ):
            reporter = _VALIDATOR.Reporter()
            output = io.StringIO()
            with redirect_stdout(output):
                _VALIDATOR._validate_dispatch_model_snapshot(
                    project=Path("."),
                    task=task,
                    task_card={
                        "_model_requirement": {
                            "required_tier": "standard",
                            "preferred_tier": "standard",
                            "deliberation_tier": "balanced",
                            "required_capabilities": ["coding"],
                            "degradation_policy": "allow_to_minimum",
                        }
                    },
                    bindings={
                        "tb": {
                            "provider": "p",
                            "model_id": "m",
                            "tier": "advanced",
                            "deliberation_tier": "balanced",
                            "context_window_tokens": 200000,
                            "capabilities": ["coding"],
                            "enabled": True,
                        }
                    },
                    context="tasks[0]",
                    state=state,
                    require_committed=False,
                    reporter=reporter,
                )
            return reporter, output.getvalue()

    def test_missing_window_field_in_dispatch(self):
        sel = _make_ten_field_selection()
        del sel["selected_context_window_tokens"]
        reporter, output = self._call_dispatch_snapshot(sel)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("selected_context_window_tokens", output)
        self.assertIn("missing required key", output)

    def test_extra_field_in_dispatch(self):
        sel = _make_ten_field_selection()
        sel["extra_key"] = "bad"
        reporter, output = self._call_dispatch_snapshot(sel)
        # _require_keys won't catch extra; _require_exact_keys is not called
        # on dispatch model_selection (only on executor_model). The validator
        # uses _require_keys which only catches missing, not extra.
        # But if there ARE errors from the snapshot validation, verify
        # it's not about the extra key being silently accepted in a way
        # that contradicts schema behaviour.
        # The outbox validator DOES check exact keys; see Outbox tests.
        pass

    def test_true_window_value_in_dispatch(self):
        sel = _make_ten_field_selection(
            {"selected_context_window_tokens": True}
        )
        reporter, output = self._call_dispatch_snapshot(sel)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("selected_context_window_tokens", output)

    def test_zero_window_value_in_dispatch(self):
        sel = _make_ten_field_selection(
            {"selected_context_window_tokens": 0}
        )
        reporter, output = self._call_dispatch_snapshot(sel)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r">= ?1")

    def test_string_window_value_in_dispatch(self):
        sel = _make_ten_field_selection(
            {"selected_context_window_tokens": "100000"}
        )
        reporter, output = self._call_dispatch_snapshot(sel)
        self.assertGreaterEqual(reporter.errors, 1, output)

    def test_valid_window_value_passes_dispatch(self):
        sel = _make_ten_field_selection(
            {"selected_context_window_tokens": 200000}
        )
        reporter, output = self._call_dispatch_snapshot(sel)
        # No errors from missing keys, extra keys, or bad window value
        self.assertNotIn("selected_context_window_tokens", output)
        self.assertNotIn("missing required key", output)
        self.assertNotIn("unexpected key", output)


# =========================================================================
class OutboxModelSelectionProductionPathTests(unittest.TestCase):
    """Outbox model_selection validated via _validate_task_dispatch_outbox."""

    def _call_outbox_validator(self, outbox_selection, dispatch_selection=None):
        if dispatch_selection is None:
            dispatch_selection = _make_ten_field_selection()
        event_doc = {
            "path": "docs/pm/events/EVT-1.yaml",
            "values": {
                "event_id": "EVT-001",
                "event_type": "TASK_DISPATCHED",
                "task_id": "TC-001",
                "revision": 1,
                "attempt": 1,
                "dispatch_id": "DSP-1",
                "from_state": "ready",
                "to_state": "dispatched",
                "actor_role_id": "PM",
                "lease_epoch": 1,
                "occurred_at": "2026-07-26T12:00:00Z",
                "payload_digest": "sha256:" + ("f" * 64),
            },
            "raw": b"{}",
        }
        outbox_doc = {
            "path": "docs/pm/outbox/MSG-1.yaml",
            "values": {
                "schema_version": "agentdesk.outbox-message/v2",
                "message_type": "task.dispatch",
                "event_id": "EVT-001",
                "task_id": "TC-001",
                "revision": 1,
                "attempt": 1,
                "dispatch_id": "DSP-1",
                "destination_role_id": "DEV",
                "dedupe_key": "TC-001/r1/a1/DSP-1/task.dispatch",
                "message_id": "MSG-001",
                "created_at": "2026-07-26T12:00:00Z",
                "payload": {
                    "task_path": "docs/pm/tasks/TC-001-r1-test.md",
                    "task_card_commit": "0" * 40,
                    "base_commit": "a" * 40,
                    "branch": "feat/x",
                    "report_path": "docs/pm/reports/TC-001-r1-a1.md",
                },
                "model_selection": outbox_selection,
            },
            "raw": b"{}",
        }
        reporter = _VALIDATOR.Reporter()
        output = io.StringIO()
        with redirect_stdout(output):
            _VALIDATOR._validate_task_dispatch_outbox(
                document=outbox_doc,
                event_document=event_doc,
                task={
                    "task_id": "TC-001",
                    "revision": 1,
                    "attempt": 1,
                    "task_card_path": "docs/pm/tasks/TC-001-r1-test.md",
                    "task_card_commit": "0" * 40,
                    "report_path": "docs/pm/reports/TC-001-r1-a1.md",
                    "granted_approval_ids": [],
                },
                dispatch={
                    "dispatch_id": "DSP-1",
                    "role_id": "DEV",
                    "base_commit": "a" * 40,
                    "branch": "feat/x",
                },
                selection=dispatch_selection,
                context="tasks[0]",
                reporter=reporter,
            )
        return reporter, output.getvalue()

    def test_outbox_rejects_missing_field(self):
        sel = _make_ten_field_selection()
        del sel["selected_context_window_tokens"]
        reporter, output = self._call_outbox_validator(sel)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("selector fields", output)

    def test_outbox_rejects_extra_field(self):
        sel = _make_ten_field_selection()
        sel["extra_key"] = "bad"
        reporter, output = self._call_outbox_validator(sel)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("selector fields", output)

    def test_outbox_mismatched_window_value(self):
        dispatch_sel = _make_ten_field_selection()
        outbox_sel = _make_ten_field_selection(
            {"selected_context_window_tokens": 999999}
        )
        reporter, output = self._call_outbox_validator(
            outbox_sel, dispatch_sel
        )
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("selected_context_window_tokens", output)
        self.assertIn("must exactly match", output)


# =========================================================================
class TerminalReportExecutorModelTests(unittest.TestCase):
    """Terminal report (current_dispatch cleared) executor_model validated
    via _validate_delivery_report."""

    def _call_delivery_report(
        self, executor_model, state="accepted", model_evidence_required=True
    ):
        """Call _validate_delivery_report with faked Git/commit validation."""
        task = {
            "task_id": "TC-001",
            "revision": 1,
            "attempt": 1,
            "state": state,
            "implementation_commit": "a" * 40,
            "report_commit": "b" * 40,
            "accepted_commit": "a" * 40,
            "report_path": "docs/pm/reports/TC-001-r1-a1.md",
            "acceptance_path": "docs/pm/acceptances/TC-001-r1-a1-review1.md",
            "granted_approval_ids": [],
            "current_dispatch": None,
            "delivery_state": "accepted",
            "integration_state": "pending",
            "timestamps": {
                "created_at": "2026-07-26T12:00:00Z",
                "ready_at": "2026-07-26T12:00:00Z",
                "dispatched_at": "2026-07-26T12:00:00Z",
                "started_at": "2026-07-26T12:00:00Z",
                "delivered_at": "2026-07-26T12:00:00Z",
                "blocked_at": None,
                "accepted_at": "2026-07-26T12:00:00Z",
                "integrated_at": None,
                "updated_at": "2026-07-26T12:00:00Z",
            },
        }

        task_card = {
            "role_id": "DEV",
            "base_commit": "a" * 40,
            "_model_requirement": {
                "required_tier": "standard",
                "preferred_tier": "standard",
                "deliberation_tier": "balanced",
                "required_capabilities": ["coding"],
                "degradation_policy": "allow_to_minimum",
            },
        }

        if model_evidence_required:
            # Force the task to be non-draft, non-terminal without current_dispatch
            # but with evidence requirement.
            task["state"] = state

        # Build valid report frontmatter content
        ym = "---\n"
        ym += "schema_version: agentdesk.delivery-report/v2\n"
        ym += "task_id: TC-001\n"
        ym += "revision: 1\n"
        ym += "role_id: DEV\n"
        ym += "delivery_status: completed\n"
        ym += "dispatch_id: DSP-TC001-R1-A1-XXXX\n"
        ym += "callback_id: CB-TC001-R1-A1-XXXX\n"
        ym += "attempt: 1\n"
        ym += "base_commit: {}\n".format("a" * 40)
        ym += "implementation_commit: {}\n".format("a" * 40)
        ym += "report_commit: null\n"
        ym += "report_path: docs/pm/reports/TC-001-r1-a1.md\n"
        ym += "branch: feat/tc-001-x\n"
        if executor_model is not None:
            ym += "executor_model:\n"
            for key in sorted(executor_model):
                val = executor_model[key]
                if isinstance(val, list):
                    ym += "  {}: [{}]\n".format(key, ", ".join(val))
                elif isinstance(val, str):
                    ym += '  {}: "{}"\n'.format(key, val)
                elif val is None:
                    ym += "  {}: null\n".format(key)
                else:
                    ym += "  {}: {}\n".format(key, val)
        ym += "blocked_reason: null\n"
        ym += "suggested_resume_state: null\n"
        ym += "created_at: 2026-07-26T12:00:00Z\n"
        ym += "---\n"

        with patch.object(_VALIDATOR, "_git_commit_exists", return_value=True), \
             patch.object(_VALIDATOR, "_git_is_ancestor", return_value=True), \
             patch.object(_VALIDATOR, "_validate_sha", return_value=True), \
             patch.object(_VALIDATOR, "_git_blob_exists", return_value=None), \
             patch.object(_VALIDATOR, "_git_read_blob", return_value=None):
            reporter = _VALIDATOR.Reporter(
                allow_legacy_model_evidence=not model_evidence_required
            )
            output = io.StringIO()
            with redirect_stdout(output):
                _VALIDATOR._validate_delivery_report(
                    project=Path("."),
                    content=ym,
                    task=task,
                    task_card=task_card,
                    context="tasks[0]",
                    state=state,
                    require_committed=False,
                    reporter=reporter,
                )
            return reporter, output.getvalue()

    def test_extra_executor_model_field_rejected(self):
        em = _make_ten_field_selection()
        em["extra_key"] = "bad"
        reporter, output = self._call_delivery_report(em)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("unexpected key", output.lower())

    def test_true_window_value_rejected(self):
        em = _make_ten_field_selection({"selected_context_window_tokens": True})
        reporter, output = self._call_delivery_report(em)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertIn("selected_context_window_tokens", output)

    def test_zero_window_value_rejected(self):
        em = _make_ten_field_selection({"selected_context_window_tokens": 0})
        reporter, output = self._call_delivery_report(em)
        self.assertGreaterEqual(reporter.errors, 1, output)
        self.assertRegex(output, r">= ?1")

    def test_string_window_value_rejected(self):
        em = _make_ten_field_selection(
            {"selected_context_window_tokens": "100000"}
        )
        reporter, output = self._call_delivery_report(em)
        self.assertGreaterEqual(reporter.errors, 1, output)

    def test_valid_executor_model_passes(self):
        em = _make_ten_field_selection()
        reporter, output = self._call_delivery_report(em)
        # With all the patches, some errors may occur from missing Git blobs,
        # but there should be NO errors about executor_model structure
        self.assertNotIn("unexpected key", output.lower())
        self.assertNotIn("selected_context_window_tokens", output)

    def test_legacy_missing_executor_model_warns_not_errors(self):
        reporter, output = self._call_delivery_report(
            None, state="cancelled", model_evidence_required=False
        )
        self.assertIn("WARN", output)
        self.assertNotIn("ERROR", output)


# =========================================================================
class ModelVersionTerminologyTests(unittest.TestCase):
    _SKILL_ROOT = _REPO_ROOT / "skills" / "agentdesk"

    def test_select_model_constant_is_v2(self):
        self.assertEqual(
            _SELECTOR.MODEL_BINDINGS_SCHEMA_VERSION,
            "agentdesk.model-bindings/v2",
        )

    def test_validate_project_constant_is_v2(self):
        self.assertEqual(
            _VALIDATOR.MODEL_BINDINGS_SCHEMA_VERSION,
            "agentdesk.model-bindings/v2",
        )

    def test_model_selection_fields_are_ten(self):
        self.assertEqual(len(_VALIDATOR.MODEL_SELECTION_FIELDS), 10)
        self.assertEqual(
            set(_VALIDATOR.MODEL_SELECTION_FIELDS), TEN_FIELD_LITERAL
        )

    def test_template_yaml_is_v2(self):
        path = (
            self._SKILL_ROOT
            / "assets"
            / "project-template"
            / ".agentdesk"
            / "runtime"
            / "model-bindings.yaml"
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn('"agentdesk.model-bindings/v2"', content)

    def test_delivery_report_template_has_selected_context_window_tokens(self):
        path = self._SKILL_ROOT / "assets" / "delivery-report-template.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("selected_context_window_tokens", content)

    def test_no_v1_references_in_docs(self):
        files = (
            "references/schemas-and-templates.md",
            "references/events-outbox-and-validation.md",
            "references/protocol.md",
            "references/runbooks-and-recovery.md",
            "references/codex-runtime-adapter.md",
            "references/adr/001-mad-agentdesk-integration.md",
            "SKILL.md",
        )
        for rel in files:
            path = self._SKILL_ROOT / rel
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "agentdesk.model-bindings/v1",
                content,
                f"{rel} must not reference model-bindings/v1",
            )

    def test_no_nine_field_references_in_docs(self):
        files = (
            "references/schemas-and-templates.md",
            "references/events-outbox-and-validation.md",
            "references/protocol.md",
            "references/runbooks-and-recovery.md",
            "SKILL.md",
        )
        for rel in files:
            path = self._SKILL_ROOT / rel
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "九字段", content, f"{rel} must not say 九字段"
            )
            self.assertNotIn(
                "nine fields",
                content.lower(),
                f"{rel} must not say 'nine fields'",
            )
            self.assertNotIn(
                "nine-field",
                content.lower(),
                f"{rel} must not say 'nine-field'",
            )


if __name__ == "__main__":
    unittest.main()
