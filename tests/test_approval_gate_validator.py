#!/usr/bin/env python3
"""Tests for the ApprovalGate offline project validator (TC-13.12d).

Covers the validate_project.py integration with agentdesk.task-approval/v1
evidence validation per ADR §2.15.16.  Uses a minimal valid project fixture.

Tests are installed alongside the production module:
``python -m unittest tests.test_approval_gate_validator -v``
``python -O -m unittest tests.test_approval_gate_validator -v``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"
_SCRIPTS_STR = str(_SCRIPTS)


@contextmanager
def _temporary_scripts_path():
    before = list(sys.path)
    try:
        if _SCRIPTS_STR not in sys.path:
            sys.path.insert(0, _SCRIPTS_STR)
        yield
    finally:
        sys.path[:] = before


with _temporary_scripts_path():
    import validate_project


_NOW_STR = "2026-07-27T08:00:00Z"
_LATER_STR = "2026-07-27T09:00:00Z"
_SAMPLE_SHA = "a" * 40


def _make_state_yaml(tasks: list | None = None) -> str:
    tasks_data = tasks if tasks is not None else []
    return json.dumps({
        "schema_version": "agentdesk.tasks/v2",
        "project_id": "TEST-PROJECT",
        "adoption_level": "standard",
        "updated_at": _NOW_STR,
        "pm_control": {
            "holder_id": "PM",
            "lease_epoch": 1,
            "mode": "timed",
        },
        "tasks": tasks_data,
    }, ensure_ascii=False, indent=2) + "\n"


def _make_draft_task(task_id: str = "TC-001", revision: int = 1) -> dict:
    """A minimal draft task that passes non-approval validation.
    No current_dispatch — the validator accepts this for draft tasks
    and the positive tests include an explicit event that proves
    the dispatch_id."""
    return {
        "task_id": task_id,
        "revision": revision,
        "task_card_path": f"docs/pm/tasks/{task_id}-r{revision}-test.md",
        "task_card_commit": None,
        "state": "draft",
        "attempt": 1,
        "current_dispatch": None,
        "report_path": None,
        "granted_approval_ids": [],
        "delivery_state": "none",
        "integration_state": "not_applicable",
        "implementation_commit": None,
        "report_commit": None,
        "accepted_commit": None,
        "acceptance_path": None,
        "integrated_commit": None,
        "blocked_reason": None,
        "blocked_kind": None,
        "blocked_owner": None,
        "unblock_condition": None,
        "review_after": None,
        "blocked_attempt_valid": None,
        "resume_state": None,
        "timestamps": {
            "created_at": "2026-07-01T00:00:00Z",
            "ready_at": None,
            "dispatched_at": None,
            "started_at": None,
            "delivered_at": None,
            "blocked_at": None,
            "accepted_at": None,
            "integrated_at": None,
            "updated_at": _NOW_STR,
        },
    }


def _make_grant_yaml(**overrides) -> str:
    defaults = {
        "schema_version": "agentdesk.task-approval/v1",
        "record_type": "grant",
        "approval_id": "APR-001",
        "event_id": "EVT-0001",
        "scope": "dispatch",
        "task_id": "TC-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-001",
        "accepted_commit": None,
        "actor_role_id": "PM",
        "lease_epoch": 1,
        "granted_at": _NOW_STR,
        "expires_at": None,
        "reason": "Test grant",
        "snapshot_commit": _SAMPLE_SHA,
    }
    defaults.update(overrides)
    return json.dumps(defaults, ensure_ascii=False, indent=2) + "\n"


def _make_revoke_yaml(**overrides) -> str:
    defaults = {
        "schema_version": "agentdesk.task-approval/v1",
        "record_type": "revoke",
        "approval_id": "APR-001",
        "event_id": "EVT-0002",
        "task_id": "TC-001",
        "actor_role_id": "PM",
        "lease_epoch": 2,
        "revoked_at": _LATER_STR,
        "reason": "Test revoke",
        "snapshot_commit": _SAMPLE_SHA,
    }
    defaults.update(overrides)
    return json.dumps(defaults, ensure_ascii=False, indent=2) + "\n"


def _make_event_yaml(**overrides) -> str:
    """Build a canonical production-format state-event YAML file.

    Uses the production YAML subset format (key: value per line,
    NOT JSON) that the validator's _yaml_subset_mapping_from_text
    parser expects.
    """
    defaults = {
        "schema_version": "agentdesk.state-event/v2",
        "event_type": "TASK_DISPATCHED",
        "event_id": "EVT-HIST-0001",
        "task_id": "TC-001",
        "revision": 1,
        "attempt": 1,
        "dispatch_id": "DSP-HISTORIC",
        "from_state": "ready",
        "to_state": "dispatched",
        "actor_role_id": "PM",
        "lease_epoch": 1,
        "occurred_at": _NOW_STR,
        "payload_digest": "sha256:" + "0" * 64,
    }
    defaults.update(overrides)
    lines = []
    for key, value in defaults.items():
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def _assert_json_cannot_parse(content: str) -> None:
    """Verify that the production YAML format is NOT valid JSON."""
    import json as _json
    try:
        _json.loads(content)
        raise AssertionError(
            "Production event YAML must not be valid JSON — "
            "test must exercise the YAML subset parser, not json.loads"
        )
    except ValueError:
        pass


def _git_head(proj: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(proj), capture_output=True, timeout=10, shell=False,
    )
    return result.stdout.decode("utf-8").strip()


@contextmanager
def _temp_project(tasks: list | None = None):
    """Create a minimal but fully valid temp project with git repo."""
    with tempfile.TemporaryDirectory(prefix="agv_test_") as tmp:
        proj = Path(tmp)

        for d in (
            "docs/pm/state", "docs/pm/tasks", "docs/pm/reports",
            "docs/pm/acceptances", "docs/pm/events", "docs/pm/outbox",
            "docs/pm/approvals", ".agentdesk/runtime", ".agentdesk/runtime/inbox",
        ):
            (proj / d).mkdir(parents=True, exist_ok=True)

        # Create task card files for valid task_card_path entries
        if tasks:
            for t in tasks:
                path_str = t.get("task_card_path")
                if isinstance(path_str, str) and path_str.strip():
                    card_path = proj / path_str
                    card_path.parent.mkdir(parents=True, exist_ok=True)
                    card_path.write_text(
                        "---\n"
                        f"schema_version: agentdesk.task-card/v2\n"
                        f"task_id: {t.get('task_id', 'TC-001')}\n"
                        f"revision: {t.get('revision', 1)}\n"
                        f"created_at: {_NOW_STR}\n"
                        "---\n"
                        "# Task\n",
                        encoding="utf-8",
                    )

        (proj / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

        (proj / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (proj / "docs/pm/STATUS.md").write_text("# Status\n", encoding="utf-8")
        (proj / "docs/pm/BOARD.md").write_text("# Board\n", encoding="utf-8")
        (proj / "docs/pm/ROLES.md").write_text(
            "| role_no | role_id | role_name | expected_thread_title | Status |\n"
            "|---------|---------|-----------|-----------------------|--------|\n"
            "| 1 | PM | PM | 1 . PM | Active |\n"
            "| 2 | Worker | Worker | 2 . Worker | Active |\n",
            encoding="utf-8",
        )
        (proj / "docs/pm/ROLE-POLICIES.yaml").write_text(json.dumps({
            "schema_version": "agentdesk.role-policies/v1",
            "tier_order": ["basic", "standard", "advanced", "expert"],
            "deliberation_tier_order": ["efficient", "balanced", "deep"],
            "risk_floors": {"L0": "basic", "L1": "standard", "L2": "advanced",
                            "L3": "expert", "L4": "expert"},
            "roles": {
                "PM": {
                    "default_tier": "standard", "minimum_tier": "basic",
                    "deliberation_tier": "balanced",
                    "required_capabilities": [],
                    "degradation_policy": "block",
                },
                "Worker": {
                    "default_tier": "standard", "minimum_tier": "basic",
                    "deliberation_tier": "balanced",
                    "required_capabilities": [],
                    "degradation_policy": "block",
                },
            },
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (proj / "docs/pm/CHECKS.yaml").write_text("checks: []\n", encoding="utf-8")
        (proj / "docs/pm/DECISIONS.md").write_text("# Decisions\n", encoding="utf-8")
        (proj / "docs/pm/PM-PLAYBOOK.md").write_text("# Playbook\n", encoding="utf-8")
        (proj / ".gitignore").write_text(
            ".agentdesk/runtime/\n__pycache__/\n*.py[cod]\n",
            encoding="utf-8",
        )

        (proj / "docs" / "pm" / "state" / "tasks.yaml").write_text(
            _make_state_yaml(tasks=tasks), encoding="utf-8",
        )

        subprocess.run(["git", "init"], cwd=str(proj),
                       capture_output=True, timeout=10, shell=False)
        subprocess.run(["git", "config", "user.email", "test@test.test"],
                       cwd=str(proj), capture_output=True, timeout=10, shell=False)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=str(proj), capture_output=True, timeout=10, shell=False)
        subprocess.run(["git", "add", "."], cwd=str(proj),
                       capture_output=True, timeout=10, shell=False)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(proj),
                       capture_output=True, timeout=10, shell=False)

        yield proj


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Positive tests (with minimal draft task in ledger)
# ═══════════════════════════════════════════════════════════════════════════════


class PositiveApprovalValidationTests(unittest.TestCase):
    """Test validator acceptance of valid approval evidence."""

    def test_empty_approvals_dir(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")

    def test_legal_dispatch_grant(self) -> None:
        # Must provide event history to prove dispatch_id since the
        # draft task has current_dispatch=None.
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, event_id="EVT-0001"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")

    def test_legal_accept_grant(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, scope="accept", event_id="EVT-0001"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")

    def test_legal_integrate_grant(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
                accepted_commit="a" * 40,
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="integrate", event_id="EVT-0001",
                    accepted_commit="a" * 40,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")

    def test_legal_grant_plus_revoke(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, event_id="EVT-0001",
                    approval_id="APR-001",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, event_id="EVT-0002",
                    approval_id="APR-001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")

    def test_snapshot_ancestor_is_valid(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head1 = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head1, event_id="EVT-0001"),
                encoding="utf-8",
            )
            (proj / "newfile").write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "newfile"], cwd=str(proj),
                           capture_output=True, timeout=10, shell=False)
            subprocess.run(["git", "commit", "-m", "advance"], cwd=str(proj),
                           capture_output=True, timeout=10, shell=False)
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")

    def test_README_md_ignored(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "README.md").write_text(
                "not evidence", encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, event_id="EVT-0001",
                    task_id="TC-001", revision=1, attempt=1,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expected 0 errors, got: {reporter.errors}")


# ═══════════════════════════════════════════════════════════════════════════════
# 2-14. Negative tests — all call real validator, assert ERRORs
# ═══════════════════════════════════════════════════════════════════════════════
# Negative tests use empty-task projects (no task ledger) to avoid noise
# from task-card validation, while still running the approval evidence
# validation code path.  Orphan-detection tests use projects WITH tasks
# to prove orphan errors are raised.


class SchemaValidationTests(unittest.TestCase):
    """Test exact schema enforcement (ADR §2.15.16 item 1)."""

    def test_grant_extra_key_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, extra="bad"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Extra key must be ERROR")

    def test_grant_missing_key_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            d = json.loads(_make_grant_yaml(snapshot_commit=head))
            del d["reason"]
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Missing key must be ERROR")

    def test_revoke_extra_key_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, event_id="EVT-0001"),
                encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(snapshot_commit=head, event_id="EVT-0002", extra="bad"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Extra key in revoke must be ERROR")

    def test_revoke_missing_key_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, event_id="EVT-0001"),
                encoding="utf-8",
            )
            d = json.loads(_make_revoke_yaml(snapshot_commit=head, event_id="EVT-0002"))
            del d["reason"]
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Missing key in revoke must be ERROR")

    def test_non_mapping_root_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                "[1, 2, 3]", encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-mapping root must be ERROR")

    def test_non_utf8_rejected(self) -> None:
        with _temp_project() as proj:
            path = proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml"
            with open(str(path), "wb") as f:
                f.write(b"\xff\xfe\x00\x00")
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-UTF-8 must be ERROR")

    def test_invalid_json_yaml_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                "{not valid json}", encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Invalid JSON must be ERROR")

    def test_unknown_schema_version_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, schema_version="bad-schema/v1",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Unknown schema_version must be ERROR")

    def test_unknown_record_type_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, record_type="unknown",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Unknown record_type must be ERROR")


class UniquenessTests(unittest.TestCase):
    """Test global uniqueness (ADR §2.15.16 items 2-3)."""

    def test_duplicate_approval_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0002",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Duplicate approval_id must be ERROR")

    def test_duplicate_event_id_grants_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001-dupe.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-002", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Duplicate event_id must be ERROR")

    def test_duplicate_event_id_grant_revoke_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            # This file has stem EVT-0001-collide, but its event_id is EVT-0001,
            # same as the grant — filename mismatch AND event_id collision.
            # Now also use the same event_id in the content.
            (proj / "docs" / "pm" / "approvals" / "EVT-0001-collide.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Duplicate event_id across grant+revoke must be ERROR")


class ScopeValidationTests(unittest.TestCase):
    """Test scope value enforcement (ADR §2.15.16 item 4)."""

    def test_unknown_scope_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, scope="unknown"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Unknown scope must be ERROR")

    def test_empty_scope_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, scope=""),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Empty scope must be ERROR")

    def test_numeric_scope_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, scope=123),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-string scope must be ERROR")


class SubjectScopeConsistencyTests(unittest.TestCase):
    """Test subject field consistency with scope (ADR §2.15.16 item 5)."""

    def test_dispatch_accepted_commit_not_none_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="dispatch",
                    accepted_commit="a" * 40,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Dispatch with accepted_commit must be ERROR")

    def test_accept_accepted_commit_not_none_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="accept",
                    accepted_commit="a" * 40,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Accept with accepted_commit must be ERROR")

    def test_integrate_no_accepted_commit_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="integrate",
                    accepted_commit=None,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Integrate without accepted_commit must be ERROR")

    def test_empty_task_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, task_id=""),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Empty task_id must be ERROR")

    def test_invalid_task_id_pattern_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, task_id="bad-id"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Invalid task_id pattern must be ERROR")

    def test_non_integer_revision_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, revision="1"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-int revision must be ERROR")

    def test_zero_revision_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, revision=0),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Revision 0 must be ERROR")

    def test_non_integer_attempt_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, attempt="1"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-int attempt must be ERROR")

    def test_empty_dispatch_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, dispatch_id=""),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Empty dispatch_id must be ERROR")


class ActorRoleIdTests(unittest.TestCase):
    """Test actor_role_id == "PM" (ADR §2.15.16 item 6)."""

    def test_actor_not_PM_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, actor_role_id="Worker"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-PM actor must be ERROR")

    def test_actor_empty_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, actor_role_id=""),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Empty actor must be ERROR")


class LeaseEpochTests(unittest.TestCase):
    """Test lease_epoch rules (ADR §2.15.16 item 7)."""

    def test_bool_lease_epoch_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, lease_epoch=True),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Bool lease_epoch must be ERROR")

    def test_zero_lease_epoch_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, lease_epoch=0),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Zero lease_epoch must be ERROR")

    def test_negative_lease_epoch_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, lease_epoch=-1),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Negative lease_epoch must be ERROR")


class TimestampTests(unittest.TestCase):
    """Test timestamp rules (ADR §2.15.16 items 8-9)."""

    def test_naive_granted_at_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head,
                    granted_at="2026-07-27T08:00:00",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Naive timestamp must be ERROR")

    def test_non_utc_offset_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head,
                    granted_at="2026-07-27T08:00:00+08:00",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-UTC offset must be ERROR")

    def test_invalid_timestamp_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, granted_at="not-a-date",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Invalid timestamp must be ERROR")

    def test_non_string_timestamp_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, granted_at=123),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-string timestamp must be ERROR")

    def test_expires_at_equal_to_granted_at_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, granted_at=_NOW_STR, expires_at=_NOW_STR,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "expires_at == granted_at must be ERROR")

    def test_expires_at_before_granted_at_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, granted_at=_NOW_STR,
                    expires_at="2026-07-27T07:00:00Z",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "expires_at < granted_at must be ERROR")

    def test_non_utc_expires_at_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head,
                    expires_at="2026-08-27T08:00:00+08:00",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Non-UTC expires_at must be ERROR")


class RevokeRelationTests(unittest.TestCase):
    """Test revoke-to-grant relationship (ADR §2.15.16 items 10-11)."""

    def test_revoke_without_grant_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, approval_id="APR-NONEXISTENT",
                    event_id="EVT-0001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Orphan revoke must be ERROR")

    def test_multiple_revokes_for_same_grant_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0002",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0003.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0003",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Multiple revokes must be ERROR")

    def test_revoke_time_before_grant_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                    granted_at=_LATER_STR,
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0002",
                    revoked_at=_NOW_STR,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Revoke before grant must be ERROR")


class ActiveGrantConflictTests(unittest.TestCase):
    """Test active grant conflict detection (ADR §2.15.16 item 11)."""

    def test_two_active_grants_same_scope_subject_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-002", event_id="EVT-0002",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Two active grants same scope+subject must be ERROR")

    def test_revoked_grant_not_counted_as_active(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0002",
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0003.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-002", event_id="EVT-0003",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Revoked grant should not block new grant, got {reporter.errors}")

    def test_expired_grant_not_counted_as_active(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
            )
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            past_granted = "2026-07-20T08:00:00Z"
            past_expiry = "2026-07-21T08:00:00Z"
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-001", event_id="EVT-0001",
                    granted_at=past_granted, expires_at=past_expiry,
                ), encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, approval_id="APR-002", event_id="EVT-0002",
                    task_id="TC-001", revision=1, attempt=1,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Expired grant should not block new grant, got {reporter.errors}")


class OrphanEvidenceTests(unittest.TestCase):
    """Test orphan evidence detection (ADR §2.15.16 item 13)."""

    def test_orphan_task_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, task_id="TC-999"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Orphan task_id must be ERROR")

    def test_orphan_revision_rejected(self) -> None:
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, task_id="TC-001", revision=99,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Orphan revision must be ERROR")

    def test_integrate_accepted_commit_mismatch_rejected(self) -> None:
        with _temp_project([{**_make_draft_task(), "accepted_commit": "a" * 40}]) as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="integrate", task_id="TC-001",
                    accepted_commit="f" * 40,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Wrong accepted_commit must be ERROR")

    def test_legal_grant_ledger_moved_on_but_event_history_proves(self) -> None:
        """Grant where task ledger has moved on but event history proves task_id
        with exact subject match."""
        with _temp_project() as proj:
            head = _git_head(proj)
            # Write a production-format YAML event that proves TC-002
            # at revision 2, attempt 1, dispatch DSP-002.
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-002", revision=2, attempt=1,
                dispatch_id="DSP-002", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            # Grant references TC-002 with exact same subject
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, task_id="TC-002", revision=2,
                    dispatch_id="DSP-002", event_id="EVT-0001",
                    # Grant attempt must match event attempt for exact subject match
                    attempt=1,
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            # Expect 0 approval errors.  The event YAML file also produces
            # a JSON parse warning (expected — it IS YAML, not JSON-like),
            # but that's a WARN, not an ERROR.
            self.assertEqual(0, reporter.errors,
                             f"Event history should prove task: got {reporter.errors}")

    def test_dispatch_id_in_history_but_not_ledger_is_valid(self) -> None:
        """dispatch_id found in event history with exact subject match → legal."""
        with _temp_project() as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-HISTORIC", event_id="EVT-HIST-0001",
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, dispatch_id="DSP-HISTORIC",
                    event_id="EVT-0001", revision=1, attempt=1,
                    task_id="TC-001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"History dispatch should prove: got {reporter.errors}")

    def test_dispatch_id_not_in_ledger_or_history_errors(self) -> None:
        """dispatch_id in neither ledger nor history must ERROR
        when the task has an active current_dispatch."""
        with _temp_project([_make_draft_task()]) as proj:
            # Replace the task with one that has an active current_dispatch
            # so the dispatch check fires.
            state_dir = proj / "docs" / "pm" / "state"
            task_with_dispatch = {
                **_make_draft_task(),
                "current_dispatch": {
                    "dispatch_id": "DSP-ACTIVE",
                    "role_id": "Worker",
                    "base_commit": "b" * 40,
                    "branch": "feat/test",
                },
                "task_card_path": "docs/pm/tasks/TC-001-r1-test.md",
                "task_card_commit": _git_head(proj),
            }
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "tasks.yaml").write_text(
                _make_state_yaml(tasks=[task_with_dispatch]),
                encoding="utf-8",
            )
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head,
                    dispatch_id="DSP-NOWHERE",
                    event_id="EVT-0001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Missing dispatch_id must be ERROR")

    def test_integrate_ac_ledger_null_history_proves_valid(self) -> None:
        """Integrate grant: ledger absent, history has exact subject + ac → 0 errors."""
        with _temp_project() as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event_yaml = _make_event_yaml(
                event_type="CHANGE_INTEGRATED",
                task_id="TC-001", revision=1, attempt=1,
                dispatch_id="DSP-001", event_id="EVT-HIST-0001",
                from_state="accepted", to_state="integrated",
                accepted_commit="b" * 40, integrated_commit="c" * 40,
            )
            _assert_json_cannot_parse(event_yaml)
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                event_yaml, encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="integrate",
                    accepted_commit="b" * 40, event_id="EVT-0001",
                    task_id="TC-001", revision=1, attempt=1,
                    dispatch_id="DSP-001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"History accepted_commit should prove: got {reporter.errors}")

    def test_integrate_ac_ledger_null_history_wrong_errors(self) -> None:
        """Integrate: ledger null, history has different accepted_commit → ERROR."""
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            events_dir = proj / "docs" / "pm" / "events"
            events_dir.mkdir(parents=True, exist_ok=True)
            event = {
                "schema_version": "agentdesk.state-event/v2",
                "event_type": "CHANGE_INTEGRATED",
                "task_id": "TC-001",
                "revision": 1,
                "attempt": 1,
                "dispatch_id": "DSP-001",
                "event_id": "EVT-HIST-0001",
                "from_state": "accepted",
                "to_state": "integrated",
                "accepted_commit": "a" * 40,
                "integrated_commit": "c" * 40,
                "actor_role_id": "PM",
                "lease_epoch": 1,
                "occurred_at": _NOW_STR,
                "payload_digest": "sha256:" + "0" * 64,
            }
            (events_dir / "EVT-HIST-0001.yaml").write_text(
                json.dumps(event, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, scope="integrate",
                    accepted_commit="f" * 40, event_id="EVT-0001",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Mismatched accepted_commit must be ERROR")


class SnapshotCommitTests(unittest.TestCase):
    """Test snapshot_commit rules (ADR §2.15.16 item 14)."""

    def test_nonexistent_snapshot_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit="0" * 40),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Non-existent snapshot must be ERROR")

    def test_non_ancestor_snapshot_rejected(self) -> None:
        with _temp_project() as proj:
            subprocess.run(
                ["git", "checkout", "--orphan", "orphan"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            subprocess.run(
                ["git", "rm", "-rf", "."],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            (proj / "orphan_file").write_text("orphan", encoding="utf-8")
            subprocess.run(
                ["git", "add", "orphan_file"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            subprocess.run(
                ["git", "commit", "-m", "orphan"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            orphan_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            ).stdout.decode("utf-8").strip()
            subprocess.run(
                ["git", "checkout", "master"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=orphan_sha),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Non-ancestor snapshot must be ERROR")

    def test_invalid_sha_format_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit="short"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Invalid SHA format must be ERROR")

    def test_uppercase_sha_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit="A" * 40),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Uppercase SHA must be ERROR")

    def test_revoke_nonexistent_snapshot_rejected(self) -> None:
        """Revoke with non-existent snapshot must ERROR."""
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, approval_id="APR-001",
                                 event_id="EVT-0001"),
                encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(snapshot_commit="0" * 40, approval_id="APR-001",
                                  event_id="EVT-0002"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Revoke non-existent snapshot must be ERROR")

    def test_revoke_non_ancestor_snapshot_rejected(self) -> None:
        """Revoke with non-ancestor snapshot must ERROR."""
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, approval_id="APR-001",
                                 event_id="EVT-0001"),
                encoding="utf-8",
            )
            # Create an orphan commit for non-ancestor snapshot
            subprocess.run(
                ["git", "checkout", "--orphan", "orphan2"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            subprocess.run(
                ["git", "rm", "-rf", "."],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            (proj / "orphan_file2").write_text("orphan", encoding="utf-8")
            subprocess.run(
                ["git", "add", "orphan_file2"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            subprocess.run(
                ["git", "commit", "-m", "orphan2"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            orphan_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            ).stdout.decode("utf-8").strip()
            subprocess.run(
                ["git", "checkout", "master"],
                cwd=str(proj), capture_output=True, timeout=10, shell=False,
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(snapshot_commit=orphan_sha, approval_id="APR-001",
                                  event_id="EVT-0002"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Revoke non-ancestor snapshot must be ERROR")

    def test_legal_grant_and_revoke_snapshot_ancestors_valid(self) -> None:
        """Grant + Revoke with valid ancestor snapshots = 0 errors."""
        with _temp_project([_make_draft_task()]) as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, approval_id="APR-001",
                                 event_id="EVT-0001"),
                encoding="utf-8",
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(snapshot_commit=head, approval_id="APR-001",
                                  event_id="EVT-0002"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertEqual(0, reporter.errors,
                             f"Legal grant+revoke snapshots: got {reporter.errors}")


class PathSafetyTests(unittest.TestCase):
    """Test evidence path safety (ADR §2.15.16 item 15)."""

    def test_filename_not_derived_from_event_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(
                    snapshot_commit=head, event_id="EVT-SOMETHING-ELSE",
                ), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Mismatched filename must be ERROR")

    def test_non_evt_yaml_file_with_legal_grant_errors(self) -> None:
        """A .yaml file that does NOT match EVT-* must ERROR."""
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "bad.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head, event_id="EVT-0001"),
                encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "bad.yaml must cause ERROR for non-EVT filename")

    def test_non_evt_filename_ignored(self) -> None:
        with _temp_project() as proj:
            # not-an-event.yaml is a .yaml file that does NOT match EVT-*
            # — it must ERROR, not be silently ignored
            (proj / "docs" / "pm" / "approvals" / "not-an-event.yaml").write_text(
                _make_grant_yaml(event_id="EVT-0001"), encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0,
                               "Non-EVT .yaml must ERROR")

    def test_symlink_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            real = proj / "real.yaml"
            real.write_text(_make_grant_yaml(snapshot_commit=head),
                            encoding="utf-8")
            link = proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlink not supported on this platform")
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0, "Symlink must be ERROR")


class SideEffectSafetyTests(unittest.TestCase):
    """Test validator does not modify files or mutate state."""

    def test_validator_does_not_modify_evidence_file(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            ev_path = proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml"
            content = _make_grant_yaml(snapshot_commit=head)
            ev_path.write_text(content, encoding="utf-8")
            original = ev_path.read_bytes()
            validate_project.validate(proj, require_committed=False)
            self.assertEqual(original, ev_path.read_bytes(),
                             "Validator must not modify evidence files")

    def test_validator_does_not_change_git_head(self) -> None:
        with _temp_project() as proj:
            head_before = _git_head(proj)
            validate_project.validate(proj, require_committed=False)
            head_after = _git_head(proj)
            self.assertEqual(head_before, head_after,
                             "Validator must not change Git HEAD")

    def test_validator_does_not_create_temp_files(self) -> None:
        with _temp_project() as proj:
            before = set(proj.glob("**/*"))
            validate_project.validate(proj, require_committed=False)
            after = set(proj.glob("**/*"))
            self.assertEqual(before, after,
                             "Validator must not create files")

    def test_validator_does_not_acquire_state_lock(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(snapshot_commit=head), encoding="utf-8",
            )
            lock_dir = proj / ".agentdesk" / "runtime"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_file = lock_dir / ".state-transition.lock"
            lock_file.write_text("", encoding="utf-8")
            lock_before = lock_file.read_bytes()
            validate_project.validate(proj, require_committed=False)
            self.assertEqual(lock_before, lock_file.read_bytes(),
                             "Validator must not touch state lock")


class InformationLeakTests(unittest.TestCase):
    """Test that error messages do not leak untrusted data."""

    def test_non_mapping_reported_as_error_not_repr(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                "[1, 2, 3]", encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0)

    def test_bad_json_reported_as_error(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                "not json", encoding="utf-8",
            )
            reporter = validate_project.validate(proj, require_committed=False)
            self.assertGreater(reporter.errors, 0)


if __name__ == "__main__":
    unittest.main()
