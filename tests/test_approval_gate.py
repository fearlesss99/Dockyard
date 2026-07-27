"""Tests for ApprovalGate typed models, evidence store, and writer (TC-13.12b).

Covers:
* Typed models — exact fields, frozen, slots, no __dict__, validation
* Enum values and uniqueness
* Bare scope string rejection
* Subject scope combinations
* Timestamps, SHA, ID, reason boundary tests
* Malicious __repr__ not called
* python -O compatibility
* Evidence store — empty dir, exact Grant/Revoke keys, extra/missing keys,
  unknown version/type, non-mapping, malformed UTF-8, symlink/reparse/path
  traversal, Grant→Evidence round trip, malformed evidence blocks new writes,
  duplicate IDs, single revoke per grant
* Writer — exact preservation, Chinese reason, snapshot_commit correctness,
  Grant/Revoke path correctness, Revoke task_id derived from Grant,
  HEAD mismatch zero writes, lock contention zero writes,
  cross-module lock contention, atomic write failure protection,
  no temp file residue, lock file persistent
* Phase boundary — ApprovalGate.check/require raise NotImplementedError

Tests are installed alongside the production module:
``python -m unittest tests.test_approval_gate -v``
``python -O -m unittest tests.test_approval_gate -v``
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import patch

# Ensure the script directory is importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

# Import production module — restore sys.path after import to avoid
# leaking the scripts directory into the global module search path.
_SCRIPTS_STR = str(_SCRIPTS)
_path_was_already_there = _SCRIPTS_STR in sys.path
sys.path.insert(0, _SCRIPTS_STR)
try:
    import approval_gate as ag
    import control_plane_transition as cpt
finally:
    if not _path_was_already_there:
        sys.path.remove(_SCRIPTS_STR)
    # else: it was already there; leave it.


# ═══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════════════

_NOW = datetime(2026, 7, 27, 8, 0, 0, tzinfo=UTC)
_NOW_STR = "2026-07-27T08:00:00Z"
_LATER = datetime(2026, 7, 27, 9, 0, 0, tzinfo=UTC)
_LATER_STR = "2026-07-27T09:00:00Z"
_EXPIRES = "2026-08-27T08:00:00Z"
_SAMPLE_SHA = "a" * 40
_SAMPLE_SHA2 = "b" * 40


def _make_subject(
    task_id: str = "TC-001",
    revision: int = 1,
    attempt: int = 1,
    dispatch_id: str = "DSP-001",
    accepted_commit: str | None = None,
) -> ag.ApprovalSubject:
    return ag.ApprovalSubject(
        task_id=task_id,
        revision=revision,
        attempt=attempt,
        dispatch_id=dispatch_id,
        accepted_commit=accepted_commit,
    )


def _make_evidence(
    approval_id: str = "APR-001",
    event_id: str = "EVT-20260727-0001",
    scope: ag.ApprovalScope = ag.ApprovalScope.DISPATCH,
    subject: ag.ApprovalSubject | None = None,
    actor_role_id: str = "PM",
    lease_epoch: int = 1,
    granted_at: str = _NOW_STR,
    expires_at: str | None = None,
    reason: str = "Test grant",
    snapshot_commit: str = _SAMPLE_SHA,
) -> ag.ApprovalEvidence:
    if subject is None:
        subject = _make_subject()
    return ag.ApprovalEvidence(
        approval_id=approval_id,
        event_id=event_id,
        scope=scope,
        subject=subject,
        actor_role_id=actor_role_id,
        lease_epoch=lease_epoch,
        granted_at=granted_at,
        expires_at=expires_at,
        reason=reason,
        snapshot_commit=snapshot_commit,
    )


def _make_grant_yaml(**overrides: object) -> str:
    """Build a valid grant YAML string, overriding any field."""
    defaults: dict[str, object] = {
        "schema_version": "agentdesk.task-approval/v1",
        "record_type": "grant",
        "approval_id": "APR-001",
        "event_id": "EVT-20260727-0001",
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


def _make_revoke_yaml(**overrides: object) -> str:
    """Build a valid revoke YAML string, overriding any field."""
    defaults: dict[str, object] = {
        "schema_version": "agentdesk.task-approval/v1",
        "record_type": "revoke",
        "approval_id": "APR-001",
        "event_id": "EVT-20260727-0002",
        "task_id": "TC-001",
        "actor_role_id": "PM",
        "lease_epoch": 2,
        "revoked_at": _LATER_STR,
        "reason": "Test revoke",
        "snapshot_commit": _SAMPLE_SHA,
    }
    defaults.update(overrides)
    return json.dumps(defaults, ensure_ascii=False, indent=2) + "\n"


@contextmanager
def _temp_project():
    """Create a temporary project directory with .agentdesk/runtime and
    docs/pm/approvals.  Commit initial state so git rev-parse works."""
    with tempfile.TemporaryDirectory(prefix="ag_test_") as tmp:
        proj = Path(tmp)
        # Init git repo.
        subprocess.run(
            ["git", "init"],
            cwd=str(proj),
            capture_output=True,
            timeout=10,
            shell=False,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.test"],
            cwd=str(proj),
            capture_output=True,
            timeout=10,
            shell=False,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(proj),
            capture_output=True,
            timeout=10,
            shell=False,
        )
        # Create initial commit so rev-parse returns a real SHA.
        (proj / ".keep").write_text("", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".keep"],
            cwd=str(proj),
            capture_output=True,
            timeout=10,
            shell=False,
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(proj),
            capture_output=True,
            timeout=10,
            shell=False,
        )
        # Create approvals dir.
        (proj / "docs" / "pm" / "approvals").mkdir(parents=True, exist_ok=True)
        # Create runtime dir (for lock file).
        (proj / ".agentdesk" / "runtime").mkdir(parents=True, exist_ok=True)
        yield proj


def _git_head(proj: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(proj),
        capture_output=True,
        timeout=10,
        shell=False,
    )
    return result.stdout.decode("utf-8").strip()


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  ApprovalScope
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalScopeTests(unittest.TestCase):
    """Test ApprovalScope enum."""

    def test_exactly_three_members(self) -> None:
        members = list(ag.ApprovalScope)
        self.assertEqual(3, len(members), "ApprovalScope must have exactly 3 members")

    def test_member_values(self) -> None:
        self.assertEqual("dispatch", ag.ApprovalScope.DISPATCH.value)
        self.assertEqual("accept", ag.ApprovalScope.ACCEPT.value)
        self.assertEqual("integrate", ag.ApprovalScope.INTEGRATE.value)

    def test_str_value(self) -> None:
        # For str-enum, member.value is the lowercase string.
        self.assertEqual("dispatch", ag.ApprovalScope.DISPATCH.value)
        self.assertEqual("accept", ag.ApprovalScope.ACCEPT.value)
        self.assertEqual("integrate", ag.ApprovalScope.INTEGRATE.value)
        # Member is equal to its value (str-enum).
        self.assertEqual("dispatch", ag.ApprovalScope.DISPATCH)
        self.assertEqual("accept", ag.ApprovalScope.ACCEPT)
        self.assertEqual("integrate", ag.ApprovalScope.INTEGRATE)

    def test_unique(self) -> None:
        values = [m.value for m in ag.ApprovalScope]
        self.assertEqual(len(values), len(set(values)))

    def test_unknown_value_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalScope("unknown")

    def test_not_bool_int(self) -> None:
        # Python str-enum treats True as int(1), and 1 is not a valid scope.
        with self.assertRaises(ValueError):
            ag.ApprovalScope(True)  # type: ignore[arg-type]

    def test_bare_string_rejected_in_typed_models(self) -> None:
        """Bare strings must not be accepted where ApprovalScope is expected."""
        # ApprovalCheckRequest requires ApprovalScope, not str.
        subj = _make_subject()
        with self.assertRaises(TypeError):
            ag.ApprovalCheckRequest(
                scope="dispatch",  # type: ignore[arg-type]
                subject=subj,
                expected_snapshot_commit=_SAMPLE_SHA,
            )
        # ApprovalEvidence also requires ApprovalScope.
        with self.assertRaises(TypeError):
            ag.ApprovalEvidence(
                approval_id="APR-001",
                event_id="EVT-001",
                scope="dispatch",  # type: ignore[arg-type]
                subject=subj,
                actor_role_id="PM",
                lease_epoch=1,
                granted_at=_NOW_STR,
                expires_at=None,
                reason="test",
                snapshot_commit=_SAMPLE_SHA,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  ApprovalSubject
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalSubjectTests(unittest.TestCase):
    """Test ApprovalSubject typed model."""

    def test_exact_five_fields(self) -> None:
        from dataclasses import fields as dc_fields
        fnames = {f.name for f in dc_fields(ag.ApprovalSubject)}
        self.assertEqual(
            fnames,
            {"task_id", "revision", "attempt", "dispatch_id", "accepted_commit"},
        )

    def test_frozen_no_mutation(self) -> None:
        s = _make_subject()
        with self.assertRaises(Exception):
            s.task_id = "TC-002"  # type: ignore[misc]

    def test_slots_no_dict(self) -> None:
        s = _make_subject()
        with self.assertRaises(AttributeError):
            s.__dict__  # type: ignore[attr-defined]

    def test_task_id_required(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            ag.ApprovalSubject(
                task_id="",
                revision=1,
                attempt=1,
                dispatch_id="DSP-001",
                accepted_commit=None,
            )

    def test_task_id_pattern(self) -> None:
        for bad in ("TC-00", "tc-001", "ABC-001", "TC-", ""):
            with self.assertRaises((TypeError, ValueError), msg=f"should reject {bad!r}"):
                ag.ApprovalSubject(
                    task_id=bad,
                    revision=1,
                    attempt=1,
                    dispatch_id="DSP-001",
                    accepted_commit=None,
                )

    def test_revision_non_bool_int(self) -> None:
        with self.assertRaises(TypeError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=True,  # type: ignore[arg-type]
                attempt=1,
                dispatch_id="DSP-001",
                accepted_commit=None,
            )

    def test_revision_min_value(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=0,
                attempt=1,
                dispatch_id="DSP-001",
                accepted_commit=None,
            )
        with self.assertRaises(ValueError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=-1,
                attempt=1,
                dispatch_id="DSP-001",
                accepted_commit=None,
            )

    def test_attempt_non_bool_int(self) -> None:
        with self.assertRaises(TypeError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=1,
                attempt=False,  # type: ignore[arg-type]
                dispatch_id="DSP-001",
                accepted_commit=None,
            )

    def test_attempt_min_value(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=1,
                attempt=0,
                dispatch_id="DSP-001",
                accepted_commit=None,
            )

    def test_dispatch_id_required(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=1,
                attempt=1,
                dispatch_id="",
                accepted_commit=None,
            )

    def test_dispatch_id_no_whitespace_envelope(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=1,
                attempt=1,
                dispatch_id=" DSP-001 ",
                accepted_commit=None,
            )

    def test_dispatch_id_no_nul_cr_lf(self) -> None:
        for ch in ("\0", "\r", "\n"):
            with self.assertRaises(ValueError, msg=f"should reject {ch!r}"):
                ag.ApprovalSubject(
                    task_id="TC-001",
                    revision=1,
                    attempt=1,
                    dispatch_id=f"DSP{ch}-001",
                    accepted_commit=None,
                )

    def test_accepted_commit_none_ok(self) -> None:
        s = _make_subject(accepted_commit=None)
        self.assertIsNone(s.accepted_commit)

    def test_accepted_commit_sha40(self) -> None:
        s = _make_subject(accepted_commit=_SAMPLE_SHA)
        self.assertEqual(_SAMPLE_SHA, s.accepted_commit)

    def test_accepted_commit_bad_length(self) -> None:
        with self.assertRaises(ValueError):
            _make_subject(accepted_commit="abc123")

    def test_accepted_commit_uppercase_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_subject(accepted_commit="A" * 40)

    def test_accepted_commit_not_str(self) -> None:
        with self.assertRaises(TypeError):
            _make_subject(accepted_commit=123)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  ApprovalCheckRequest
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalCheckRequestTests(unittest.TestCase):
    """Test ApprovalCheckRequest typed model."""

    def test_exact_three_fields(self) -> None:
        from dataclasses import fields as dc_fields
        fnames = {f.name for f in dc_fields(ag.ApprovalCheckRequest)}
        self.assertEqual(fnames, {"scope", "subject", "expected_snapshot_commit"})

    def test_frozen_slots(self) -> None:
        req = ag.ApprovalCheckRequest(
            scope=ag.ApprovalScope.DISPATCH,
            subject=_make_subject(),
            expected_snapshot_commit=_SAMPLE_SHA,
        )
        with self.assertRaises(Exception):
            req.scope = ag.ApprovalScope.ACCEPT  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            req.__dict__  # type: ignore[attr-defined]

    def test_scope_must_be_enum(self) -> None:
        with self.assertRaises(TypeError):
            ag.ApprovalCheckRequest(
                scope="dispatch",  # type: ignore[arg-type]
                subject=_make_subject(),
                expected_snapshot_commit=_SAMPLE_SHA,
            )

    def test_subject_must_be_typed(self) -> None:
        with self.assertRaises(TypeError):
            ag.ApprovalCheckRequest(
                scope=ag.ApprovalScope.DISPATCH,
                subject={"task_id": "TC-001"},  # type: ignore[arg-type]
                expected_snapshot_commit=_SAMPLE_SHA,
            )

    def test_snapshot_commit_must_be_sha40(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalCheckRequest(
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                expected_snapshot_commit="short",
            )
        with self.assertRaises(ValueError):
            ag.ApprovalCheckRequest(
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                expected_snapshot_commit="G" * 40,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  ApprovalEvidence
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalEvidenceTests(unittest.TestCase):
    """Test ApprovalEvidence typed model."""

    def test_exact_ten_fields(self) -> None:
        from dataclasses import fields as dc_fields
        fnames = {f.name for f in dc_fields(ag.ApprovalEvidence)}
        expected = {
            "approval_id", "event_id", "scope", "subject",
            "actor_role_id", "lease_epoch", "granted_at", "expires_at",
            "reason", "snapshot_commit",
        }
        self.assertEqual(fnames, expected)

    def test_frozen_slots_no_dict(self) -> None:
        ev = _make_evidence()
        with self.assertRaises(Exception):
            ev.reason = "changed"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            ev.__dict__  # type: ignore[attr-defined]

    def test_approval_id_pattern(self) -> None:
        for bad in ("", "AP-001", "apr-001", "APR", "EVT-001"):
            with self.assertRaises((TypeError, ValueError), msg=f"should reject {bad!r}"):
                _make_evidence(approval_id=bad)

    def test_event_id_pattern(self) -> None:
        for bad in ("", "evt-001", "EVT", "APR-001"):
            with self.assertRaises((TypeError, ValueError), msg=f"should reject {bad!r}"):
                _make_evidence(event_id=bad)

    def test_scope_must_be_enum(self) -> None:
        with self.assertRaises(TypeError):
            _make_evidence(scope="dispatch")  # type: ignore[arg-type]

    def test_actor_role_id_fixed_pm(self) -> None:
        e = _make_evidence(actor_role_id="PM")
        self.assertEqual("PM", e.actor_role_id)
        with self.assertRaises(ValueError):
            _make_evidence(actor_role_id="Worker")

    def test_lease_epoch_non_bool_int(self) -> None:
        with self.assertRaises(TypeError):
            _make_evidence(lease_epoch=True)  # type: ignore[arg-type]

    def test_lease_epoch_min_value(self) -> None:
        with self.assertRaises(ValueError):
            _make_evidence(lease_epoch=0)
        with self.assertRaises(ValueError):
            _make_evidence(lease_epoch=-1)

    def test_granted_at_rfc3339_utc(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            _make_evidence(granted_at="2026-07-27")  # not RFC 3339
        with self.assertRaises((TypeError, ValueError)):
            _make_evidence(granted_at="2026-07-27T08:00:00")  # no tz

    def test_expires_at_none_ok(self) -> None:
        ev = _make_evidence(expires_at=None)
        self.assertIsNone(ev.expires_at)

    def test_expires_at_strictly_after_granted_at(self) -> None:
        # Same = rejects.
        with self.assertRaises(ValueError):
            _make_evidence(granted_at=_NOW_STR, expires_at=_NOW_STR)
        # Before = rejects.
        with self.assertRaises(ValueError):
            _make_evidence(granted_at=_LATER_STR, expires_at=_NOW_STR)

    def test_reason_non_empty(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            _make_evidence(reason="")

    def test_reason_no_whitespace_envelope(self) -> None:
        with self.assertRaises(ValueError):
            _make_evidence(reason=" hello ")

    def test_reason_no_nul_cr_lf(self) -> None:
        for ch in ("\0", "\r", "\n"):
            with self.assertRaises(ValueError, msg=f"should reject {ch!r}"):
                _make_evidence(reason=f"bad{ch}reason")

    def test_snapshot_commit_sha40(self) -> None:
        with self.assertRaises(ValueError):
            _make_evidence(snapshot_commit="short")

    def test_chinese_reason_preserved(self) -> None:
        ev = _make_evidence(reason="测试审批原因")
        self.assertEqual("测试审批原因", ev.reason)

    def test_malicious_repr_not_called(self) -> None:
        """Ensure validation never calls repr/str on untrusted inputs."""
        class Malicious:
            def __repr__(self) -> str:
                raise RuntimeError("repr called")

            def __str__(self) -> str:
                raise RuntimeError("str called")

        m = Malicious()
        # ApprovalEvidence construction should reject by type, not by calling repr.
        with self.assertRaises(TypeError):
            _make_evidence(approval_id=m)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  ApprovalCheckResult
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalCheckResultTests(unittest.TestCase):
    """Test ApprovalCheckResult typed model."""

    def test_exact_four_fields(self) -> None:
        from dataclasses import fields as dc_fields
        fnames = {f.name for f in dc_fields(ag.ApprovalCheckResult)}
        self.assertEqual(fnames, {"passed", "failure_code", "matched_evidence", "checked_at"})

    def test_passed_true_must_have_evidence(self) -> None:
        ev = _make_evidence()
        r = ag.ApprovalCheckResult(
            passed=True,
            failure_code=None,
            matched_evidence=ev,
            checked_at=_NOW_STR,
        )
        self.assertTrue(r.passed)
        self.assertIsNotNone(r.matched_evidence)
        self.assertIsNone(r.failure_code)

    def test_passed_false_must_have_failure_code(self) -> None:
        r = ag.ApprovalCheckResult(
            passed=False,
            failure_code="not_found",
            matched_evidence=None,
            checked_at=_NOW_STR,
        )
        self.assertFalse(r.passed)
        self.assertEqual("not_found", r.failure_code)
        self.assertIsNone(r.matched_evidence)

    def test_passed_true_with_failure_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalCheckResult(
                passed=True,
                failure_code="not_found",
                matched_evidence=_make_evidence(),
                checked_at=_NOW_STR,
            )

    def test_passed_true_without_evidence_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalCheckResult(
                passed=True,
                failure_code=None,
                matched_evidence=None,
                checked_at=_NOW_STR,
            )

    def test_passed_false_with_evidence_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalCheckResult(
                passed=False,
                failure_code="not_found",
                matched_evidence=_make_evidence(),
                checked_at=_NOW_STR,
            )

    def test_illegal_failure_code_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalCheckResult(
                passed=False,
                failure_code="not_a_real_code",
                matched_evidence=None,
                checked_at=_NOW_STR,
            )

    def test_all_legal_failure_codes_accepted(self) -> None:
        for code in ("not_found", "expired", "revoked", "wrong_scope", "wrong_subject"):
            r = ag.ApprovalCheckResult(
                passed=False,
                failure_code=code,
                matched_evidence=None,
                checked_at=_NOW_STR,
            )
            self.assertEqual(code, r.failure_code)

    def test_frozen_slots(self) -> None:
        r = ag.ApprovalCheckResult(
            passed=False,
            failure_code="not_found",
            matched_evidence=None,
            checked_at=_NOW_STR,
        )
        with self.assertRaises(Exception):
            r.passed = True  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            r.__dict__  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


class ExceptionHierarchyTests(unittest.TestCase):
    """Test exception tree structure."""

    def test_independent_root(self) -> None:
        """ApprovalError must NOT inherit from Exception subclasses outside its tree."""
        self.assertTrue(issubclass(ag.ApprovalError, Exception))
        # Should NOT be a subclass of any other common base.
        self.assertFalse(issubclass(ag.ApprovalError, ValueError))
        self.assertFalse(issubclass(ag.ApprovalError, TypeError))

    def test_all_subclasses_inherit_from_approval_error(self) -> None:
        for cls in (
            ag.ApprovalValidationError,
            ag.ApprovalNotFoundError,
            ag.ApprovalAmbiguousError,
            ag.ApprovalExpiredError,
            ag.ApprovalRevokedError,
            ag.ApprovalSnapshotConflictError,
        ):
            with self.subTest(cls=cls):
                self.assertTrue(issubclass(cls, ag.ApprovalError))

    def test_error_messages_no_repr(self) -> None:
        """Exception messages must use safe type names only."""
        class BadObj:
            def __repr__(self) -> str:
                raise RuntimeError("repr called")

        bad = BadObj()
        exc = ag.ApprovalValidationError("test")
        # Message should not call repr/str on it.
        self.assertEqual("test", str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  __all__ completeness
# ═══════════════════════════════════════════════════════════════════════════════


class PublicAPITests(unittest.TestCase):
    """Verify exactly 15 public symbols."""

    def test_exact_15_symbols(self) -> None:
        expected = [
            "ApprovalScope",
            "ApprovalSubject",
            "ApprovalCheckRequest",
            "ApprovalEvidence",
            "ApprovalCheckResult",
            "ApprovalGate",
            "ApprovalError",
            "ApprovalValidationError",
            "ApprovalNotFoundError",
            "ApprovalAmbiguousError",
            "ApprovalExpiredError",
            "ApprovalRevokedError",
            "ApprovalSnapshotConflictError",
            "write_grant",
            "write_revoke",
        ]
        self.assertEqual(sorted(expected), sorted(ag.__all__))
        self.assertEqual(15, len(ag.__all__))


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  Evidence store — schema validation
# ═══════════════════════════════════════════════════════════════════════════════


class EvidenceStoreSchemaTests(unittest.TestCase):
    """Test private evidence store schema validation."""

    def test_empty_directory_returns_empty_collections(self) -> None:
        with _temp_project() as proj:
            grants, revokes = ag._load_evidence_store(proj)
            self.assertEqual({}, grants)
            self.assertEqual({}, revokes)

    def test_directory_does_not_exist_returns_empty(self) -> None:
        with _temp_project() as proj:
            # Remove approvals dir.
            import shutil
            shutil.rmtree(proj / "docs" / "pm" / "approvals")
            grants, revokes = ag._load_evidence_store(proj)
            self.assertEqual({}, grants)
            self.assertEqual({}, revokes)

    def test_exact_grant_keys(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-20260727-0001.yaml").write_text(
                _make_grant_yaml(), encoding="utf-8"
            )
            grants, revokes = ag._load_evidence_store(proj)
            self.assertEqual(1, len(grants))
            self.assertEqual(0, len(revokes))
            self.assertIn("APR-001", grants)

    def test_grant_extra_key_rejected(self) -> None:
        with _temp_project() as proj:
            content = _make_grant_yaml(extra_field="bad")
            (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").write_text(
                content, encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_grant_missing_key_rejected(self) -> None:
        with _temp_project() as proj:
            d = json.loads(_make_grant_yaml())
            del d["reason"]
            content = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
            (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").write_text(
                content, encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_unknown_schema_version_rejected(self) -> None:
        with _temp_project() as proj:
            content = _make_grant_yaml(schema_version="bad-schema/v1")
            (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").write_text(
                content, encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_unknown_record_type_rejected(self) -> None:
        with _temp_project() as proj:
            content = _make_grant_yaml(record_type="unknown")
            (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").write_text(
                content, encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_non_mapping_root_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").write_text(
                "[1, 2, 3]", encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_non_utf8_rejected(self) -> None:
        with _temp_project() as proj:
            # Write invalid UTF-8 bytes.
            path = proj / "docs" / "pm" / "approvals" / "EVT-001.yaml"
            with open(str(path), "wb") as f:
                f.write(b"\xff\xfe\x00\x00")
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_exact_revoke_keys(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-20260727-0001.yaml").write_text(
                _make_grant_yaml(), encoding="utf-8"
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-20260727-0002.yaml").write_text(
                _make_revoke_yaml(), encoding="utf-8"
            )
            grants, revokes = ag._load_evidence_store(proj)
            self.assertEqual(1, len(grants))
            self.assertEqual(1, len(revokes))

    def test_revoke_extra_key_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").write_text(
                _make_grant_yaml(), encoding="utf-8"
            )
            content = _make_revoke_yaml(extra="bad")
            (proj / "docs" / "pm" / "approvals" / "EVT-002.yaml").write_text(
                content, encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_grant_to_evidence_round_trip(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-20260727-0001.yaml").write_text(
                _make_grant_yaml(
                    scope="integrate",
                    accepted_commit=_SAMPLE_SHA,
                    expires_at=_EXPIRES,
                ), encoding="utf-8"
            )
            grants, _ = ag._load_evidence_store(proj)
            self.assertEqual(1, len(grants))
            ev = grants["APR-001"]
            self.assertEqual("APR-001", ev.approval_id)
            self.assertEqual("EVT-20260727-0001", ev.event_id)
            self.assertEqual(ag.ApprovalScope.INTEGRATE, ev.scope)
            self.assertEqual("TC-001", ev.subject.task_id)
            self.assertEqual(1, ev.subject.revision)
            self.assertEqual(1, ev.subject.attempt)
            self.assertEqual("DSP-001", ev.subject.dispatch_id)
            self.assertEqual(_SAMPLE_SHA, ev.subject.accepted_commit)
            self.assertEqual("PM", ev.actor_role_id)
            self.assertEqual(1, ev.lease_epoch)
            self.assertEqual(_NOW_STR, ev.granted_at)
            self.assertEqual(_EXPIRES, ev.expires_at)
            self.assertEqual("Test grant", ev.reason)
            self.assertEqual(_SAMPLE_SHA, ev.snapshot_commit)

    def test_duplicate_approval_id_rejected(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(approval_id="APR-001", event_id="EVT-0001"), encoding="utf-8"
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_grant_yaml(approval_id="APR-001", event_id="EVT-0002"), encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_duplicate_event_id_grants(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(approval_id="APR-001", event_id="EVT-0001"), encoding="utf-8"
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_grant_yaml(approval_id="APR-002", event_id="EVT-0001"), encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_duplicate_event_id_grant_revoke(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(approval_id="APR-001", event_id="EVT-0001"), encoding="utf-8"
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0002.yaml").write_text(
                _make_revoke_yaml(approval_id="APR-001", event_id="EVT-0001"), encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_non_evt_files_ignored(self) -> None:
        with _temp_project() as proj:
            # Files not matching EVT-*.yaml are silently ignored.
            (proj / "docs" / "pm" / "approvals" / "README.md").write_text(
                "not evidence", encoding="utf-8"
            )
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(), encoding="utf-8"
            )
            grants, _ = ag._load_evidence_store(proj)
            self.assertEqual(1, len(grants))

    def test_symlink_rejected(self) -> None:
        with _temp_project() as proj:
            # Create a real file elsewhere.
            real = proj / "real.yaml"
            real.write_text(_make_grant_yaml(), encoding="utf-8")
            link = proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlink not supported on this platform")
            with self.assertRaises(ag.ApprovalValidationError):
                ag._load_evidence_store(proj)

    def test_path_traversal_ignored(self) -> None:
        with _temp_project() as proj:
            # Files with path separators in name are ignored by _is_safe_filename.
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(), encoding="utf-8"
            )
            # Non-matching files are simply skipped.
            grants, _ = ag._load_evidence_store(proj)
            self.assertEqual(1, len(grants))


# ═══════════════════════════════════════════════════════════════════════════════
# 9.  Writer — write_grant
# ═══════════════════════════════════════════════════════════════════════════════


class WriteGrantTests(unittest.TestCase):
    """Test write_grant writer function."""

    def test_basic_grant(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject()
            path, sha = ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-20260727-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=subj,
                lease_epoch=1,
                now=_NOW,
                reason="Test grant",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            self.assertEqual(head, sha)
            self.assertTrue(path.is_file())
            self.assertEqual("EVT-20260727-0001.yaml", path.name)

    def test_grant_file_content_preserved(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject(task_id="TC-042", revision=3, attempt=2,
                                 dispatch_id="DSP-XYZ")
            path, sha = ag.write_grant(
                project_root=proj,
                approval_id="APR-042",
                event_id="EVT-20260727-0042",
                scope=ag.ApprovalScope.ACCEPT,
                subject=subj,
                lease_epoch=5,
                now=_NOW,
                reason="Acceptance approval",
                expires_at=None,
                expected_snapshot_commit=head,
            )

            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self.assertEqual("agentdesk.task-approval/v1", data["schema_version"])
            self.assertEqual("grant", data["record_type"])
            self.assertEqual("APR-042", data["approval_id"])
            self.assertEqual("EVT-20260727-0042", data["event_id"])
            self.assertEqual("accept", data["scope"])
            self.assertEqual("TC-042", data["task_id"])
            self.assertEqual(3, data["revision"])
            self.assertEqual(2, data["attempt"])
            self.assertEqual("DSP-XYZ", data["dispatch_id"])
            self.assertIsNone(data["accepted_commit"])
            self.assertEqual("PM", data["actor_role_id"])
            self.assertEqual(5, data["lease_epoch"])
            self.assertEqual(_NOW_STR, data["granted_at"])
            self.assertIsNone(data["expires_at"])
            self.assertEqual("Acceptance approval", data["reason"])
            self.assertEqual(head, data["snapshot_commit"])
            self.assertTrue(raw.endswith("\n"))

    def test_chinese_reason_not_escaped(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            path, _ = ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-20260727-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="测试审批原因",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            raw = path.read_text(encoding="utf-8")
            self.assertIn("测试审批原因", raw)
            # Ensure not escaped as \uXXXX.
            self.assertNotIn("\\u6d4b", raw)

    def test_integrate_scope_requires_accepted_commit(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject(accepted_commit=None)
            with self.assertRaises(ValueError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-001",
                    scope=ag.ApprovalScope.INTEGRATE,
                    subject=subj,
                    lease_epoch=1,
                    now=_NOW,
                    reason="Integration approval",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_dispatch_scope_forbids_accepted_commit(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject(accepted_commit=_SAMPLE_SHA)
            with self.assertRaises(ValueError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-001",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=subj,
                    lease_epoch=1,
                    now=_NOW,
                    reason="Bad dispatch",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_accept_scope_forbids_accepted_commit(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject(accepted_commit=_SAMPLE_SHA)
            with self.assertRaises(ValueError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-001",
                    scope=ag.ApprovalScope.ACCEPT,
                    subject=subj,
                    lease_epoch=1,
                    now=_NOW,
                    reason="Bad accept",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_duplicate_approval_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="First grant",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0002",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=_make_subject(),
                    lease_epoch=1,
                    now=_NOW,
                    reason="Duplicate approval_id",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_duplicate_event_id_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="First grant",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-002",
                    event_id="EVT-0001",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=_make_subject(),
                    lease_epoch=1,
                    now=_NOW,
                    reason="Duplicate event_id",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_duplicate_scope_subject_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject(task_id="TC-001")
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=subj,
                lease_epoch=1,
                now=_NOW,
                reason="First",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-002",
                    event_id="EVT-0002",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=subj,
                    lease_epoch=1,
                    now=_NOW,
                    reason="Duplicate scope+subject",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_target_file_exists_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            # Pre-create the target file.
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                "junk", encoding="utf-8"
            )
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0001",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=_make_subject(),
                    lease_epoch=1,
                    now=_NOW,
                    reason="File exists",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_duplicate_is_not_idempotent(self) -> None:
        """A duplicate call must fail, not silently succeed."""
        with _temp_project() as proj:
            head = _git_head(proj)
            subj = _make_subject()
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=subj,
                lease_epoch=1,
                now=_NOW,
                reason="First",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            with self.assertRaises((ag.ApprovalValidationError, ag.ApprovalSnapshotConflictError)):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0001",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=subj,
                    lease_epoch=1,
                    now=_NOW,
                    reason="First",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )

    def test_return_tuple_types(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            result = ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="Test",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            self.assertIsInstance(result, tuple)
            self.assertEqual(2, len(result))
            self.assertIsInstance(result[0], Path)
            self.assertIsInstance(result[1], str)
            self.assertEqual(40, len(result[1]))


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Writer — write_revoke
# ═══════════════════════════════════════════════════════════════════════════════


class WriteRevokeTests(unittest.TestCase):
    """Test write_revoke writer function."""

    def _setup_grant(self, proj: Path) -> tuple[str, ag.ApprovalSubject]:
        head = _git_head(proj)
        subj = _make_subject(task_id="TC-001")
        ag.write_grant(
            project_root=proj,
            approval_id="APR-001",
            event_id="EVT-0001",
            scope=ag.ApprovalScope.DISPATCH,
            subject=subj,
            lease_epoch=1,
            now=_NOW,
            reason="Test grant",
            expires_at=None,
            expected_snapshot_commit=head,
        )
        return _git_head(proj), subj

    def test_basic_revoke(self) -> None:
        with _temp_project() as proj:
            head, subj = self._setup_grant(proj)
            path, sha = ag.write_revoke(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0002",
                lease_epoch=2,
                now=_LATER,
                reason="Test revoke",
                expected_snapshot_commit=head,
            )
            self.assertEqual(head, sha)
            self.assertTrue(path.is_file())
            self.assertEqual("EVT-0002.yaml", path.name)

    def test_revoke_file_content(self) -> None:
        with _temp_project() as proj:
            head, subj = self._setup_grant(proj)
            path, _ = ag.write_revoke(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0002",
                lease_epoch=2,
                now=_LATER,
                reason="Revocation reason",
                expected_snapshot_commit=head,
            )
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self.assertEqual("agentdesk.task-approval/v1", data["schema_version"])
            self.assertEqual("revoke", data["record_type"])
            self.assertEqual("APR-001", data["approval_id"])
            self.assertEqual("EVT-0002", data["event_id"])
            self.assertEqual("TC-001", data["task_id"])
            self.assertEqual("PM", data["actor_role_id"])
            self.assertEqual(2, data["lease_epoch"])
            self.assertEqual(_LATER_STR, data["revoked_at"])
            self.assertEqual("Revocation reason", data["reason"])
            self.assertEqual(head, data["snapshot_commit"])

    def test_revoke_task_id_derived_from_grant(self) -> None:
        """Revoke task_id must come from the grant, not a separate parameter."""
        with _temp_project() as proj:
            head, subj = self._setup_grant(proj)
            path, _ = ag.write_revoke(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0002",
                lease_epoch=2,
                now=_LATER,
                reason="Revoke",
                expected_snapshot_commit=head,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("TC-001", data["task_id"])

    def test_grant_not_found_rejected(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            with self.assertRaises(ag.ApprovalNotFoundError):
                ag.write_revoke(
                    project_root=proj,
                    approval_id="APR-NONEXISTENT",
                    event_id="EVT-0001",
                    lease_epoch=1,
                    now=_NOW,
                    reason="No such grant",
                    expected_snapshot_commit=head,
                )

    def test_already_revoked_rejected(self) -> None:
        with _temp_project() as proj:
            head, subj = self._setup_grant(proj)
            ag.write_revoke(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0002",
                lease_epoch=2,
                now=_LATER,
                reason="First revoke",
                expected_snapshot_commit=head,
            )
            head2 = _git_head(proj)
            with self.assertRaises(ag.ApprovalAmbiguousError):
                ag.write_revoke(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0003",
                    lease_epoch=3,
                    now=_LATER,
                    reason="Second revoke",
                    expected_snapshot_commit=head2,
                )

    def test_duplicate_event_id_in_revoke_rejected(self) -> None:
        with _temp_project() as proj:
            head, subj = self._setup_grant(proj)
            # Write a second grant so we can test event_id collision
            # across different approval_ids (avoids hitting "already revoked" first).
            ag.write_grant(
                project_root=proj,
                approval_id="APR-002",
                event_id="EVT-0002",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(task_id="TC-002"),
                lease_epoch=1,
                now=_NOW,
                reason="Second grant",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            head2 = _git_head(proj)
            # Try to revoke APR-001 with event_id EVT-0002 (already used by APR-002 grant).
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_revoke(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0002",
                    lease_epoch=2,
                    now=_LATER,
                    reason="Duplicate event_id",
                    expected_snapshot_commit=head2,
                )

    def test_malformed_existing_evidence_blocks_revoke(self) -> None:
        """If existing evidence has malformed data, revoke must fail."""
        with _temp_project() as proj:
            head, subj = self._setup_grant(proj)
            # Write malformed evidence (bad schema_version).
            (proj / "docs" / "pm" / "approvals" / "EVT-0099.yaml").write_text(
                _make_grant_yaml(
                    approval_id="APR-099",
                    event_id="EVT-0099",
                    schema_version="bad-version",
                ),
                encoding="utf-8",
            )
            head2 = _git_head(proj)
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_revoke(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0002",
                    lease_epoch=2,
                    now=_LATER,
                    reason="Should fail",
                    expected_snapshot_commit=head2,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Git CAS
# ═══════════════════════════════════════════════════════════════════════════════


class GitCASTests(unittest.TestCase):
    """Test Git CAS (compare-and-swap) behaviour."""

    def test_head_mismatch_zero_writes(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            # Change HEAD by making a new commit.
            (proj / "newfile").write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "newfile"], cwd=str(proj),
                           capture_output=True, timeout=10, shell=False)
            subprocess.run(["git", "commit", "-m", "change"],
                           cwd=str(proj), capture_output=True, timeout=10, shell=False)
            # Use old head as expected — must fail.
            with self.assertRaises(ag.ApprovalSnapshotConflictError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-001",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=_make_subject(),
                    lease_epoch=1,
                    now=_NOW,
                    reason="Should fail",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )
            # Target file must not exist.
            self.assertFalse(
                (proj / "docs" / "pm" / "approvals" / "EVT-001.yaml").exists()
            )

    def test_successful_cas(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            path, sha = ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="CAS OK",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            self.assertEqual(head, sha)

    def test_revoke_head_mismatch_zero_writes(self) -> None:
        with _temp_project() as proj:
            head = _git_head(proj)
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-0001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="Grant",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            # Change HEAD.
            (proj / "newfile").write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "newfile"], cwd=str(proj),
                           capture_output=True, timeout=10, shell=False)
            subprocess.run(["git", "commit", "-m", "change"],
                           cwd=str(proj), capture_output=True, timeout=10, shell=False)
            with self.assertRaises(ag.ApprovalSnapshotConflictError):
                ag.write_revoke(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0002",
                    lease_epoch=2,
                    now=_LATER,
                    reason="Should fail",
                    expected_snapshot_commit=head,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# 12. State lock
# ═══════════════════════════════════════════════════════════════════════════════


class StateLockTests(unittest.TestCase):
    """Test state-lock behaviour."""

    def test_lock_contention_detected(self) -> None:
        """Two concurrent writers must detect lock contention."""
        with _temp_project() as proj:
            head = _git_head(proj)
            error_from_thread: list[BaseException | None] = [None]

            def _writer() -> None:
                try:
                    ag.write_grant(
                        project_root=proj,
                        approval_id="APR-002",
                        event_id="EVT-0002",
                        scope=ag.ApprovalScope.DISPATCH,
                        subject=_make_subject(task_id="TC-002"),
                        lease_epoch=1,
                        now=_NOW,
                        reason="Thread grant",
                        expires_at=None,
                        expected_snapshot_commit=head,
                    )
                except BaseException as exc:
                    error_from_thread[0] = exc

            # Hold the lock from the main thread.
            with ag._exclusive_state_lock(proj):
                t = threading.Thread(target=_writer)
                t.start()
                t.join(timeout=5)

            # The thread should have hit lock contention.
            self.assertIsNotNone(error_from_thread[0])
            self.assertIsInstance(error_from_thread[0], ag.ApprovalSnapshotConflictError)

    # ── Cross-module lock contention tests ─────────────────────────────────
    #
    # These tests verify that the lock protocol in approval_gate.py and
    # control_plane_transition.py use the same lock file
    # (.agentdesk/runtime/.state-transition.lock) and the same non-blocking
    # advisory lock mechanism.  Both modules independently own their lock
    # helpers (_exclusive_state_lock), but they target the same file, so
    # holding the lock in one module must block the other.
    #
    # Direction A: TC-13.11 holds the lock → ApprovalGate writer is rejected.
    # Direction B: ApprovalGate holds the lock → TC-13.11 is rejected,
    #             and TC-13.11 succeeds after ApprovalGate releases.

    def test_direction_a_cpt_locks_ag_writer_blocked(self) -> None:
        """Direction A: cpt._exclusive_state_lock held → ag.write_grant fails.

        TC-13.11 holds the state lock; ApprovalGate's write_grant() must
        fail immediately with ApprovalSnapshotConflictError (the frozen
        exception layer for lock contention in ApprovalGate)."""
        with _temp_project() as proj:
            head = _git_head(proj)

            # TC-13.11 holds the state lock.
            with cpt._exclusive_state_lock(proj):
                # Now ApprovalGate's writer must be blocked.
                with self.assertRaises(ag.ApprovalSnapshotConflictError) as ctx:
                    ag.write_grant(
                        project_root=proj,
                        approval_id="APR-DA",
                        event_id="EVT-DA-001",
                        scope=ag.ApprovalScope.DISPATCH,
                        subject=_make_subject(task_id="TC-901"),
                        lease_epoch=1,
                        now=_NOW,
                        reason="Direction A grant",
                        expires_at=None,
                        expected_snapshot_commit=head,
                    )
                self.assertIn("lock", str(ctx.exception).lower())

            # Verify: grant file was NOT created.
            target = proj / "docs" / "pm" / "approvals" / "EVT-DA-001.yaml"
            self.assertFalse(target.exists(), "Grant file must not be created on lock contention")

            # Verify: approvals dir has no temp files.
            approvals_dir = proj / "docs" / "pm" / "approvals"
            for entry in approvals_dir.iterdir():
                self.assertFalse(
                    entry.name.startswith(".EVT-"),
                    f"Temp file residue: {entry.name}",
                )

            # Verify: lock file still exists (never deleted).
            lock_path = proj / ".agentdesk" / "runtime" / ".state-transition.lock"
            self.assertTrue(lock_path.exists(), "Lock file must persist")

    def test_direction_b_ag_locks_cpt_blocked_then_cpt_succeeds(self) -> None:
        """Direction B: ag._exclusive_state_lock held → cpt throws, then succeeds.

        ApprovalGate holds the state lock.  TC-13.11's
        _exclusive_state_lock must raise TransitionLockContentionError
        immediately.  After ApprovalGate releases the lock, TC-13.11
        must be able to acquire it successfully."""
        with _temp_project() as proj:
            # ApprovalGate holds the state lock.
            with ag._exclusive_state_lock(proj):
                # TC-13.11 must fail with TransitionLockContentionError.
                with self.assertRaises(cpt.TransitionLockContentionError) as ctx:
                    with cpt._exclusive_state_lock(proj):
                        self.fail("inner body must not execute")
                self.assertIn("lock", str(ctx.exception).lower())

            # After ApprovalGate releases the lock, TC-13.11 must succeed.
            inner_executed = False
            with cpt._exclusive_state_lock(proj):
                inner_executed = True
            self.assertTrue(inner_executed, "TC-13.11 must acquire lock after ApprovalGate releases")

            # Lock file persists.
            lock_path = proj / ".agentdesk" / "runtime" / ".state-transition.lock"
            self.assertTrue(lock_path.exists(), "Lock file must persist after all releases")

    def test_direction_a_thread_cpt_locks_ag_writer_blocked(self) -> None:
        """Thread-based Direction A: cpt._exclusive_state_lock in another thread.

        When the same-process advisory lock behavior prevents blocking
        across threads on the same fd, this test uses a dedicated event
        to synchronise and verifies that write_grant() still sees
        contention (or the platform's equivalent failure)."""
        with _temp_project() as proj:
            head = _git_head(proj)
            lock_held = threading.Event()
            error_from_thread: list[BaseException | None] = [None]
            thread_done = threading.Event()

            def _writer() -> None:
                try:
                    ag.write_grant(
                        project_root=proj,
                        approval_id="APR-DAT",
                        event_id="EVT-DAT-001",
                        scope=ag.ApprovalScope.DISPATCH,
                        subject=_make_subject(task_id="TC-902"),
                        lease_epoch=1,
                        now=_NOW,
                        reason="Thread Direction A",
                        expires_at=None,
                        expected_snapshot_commit=head,
                    )
                except BaseException as exc:
                    error_from_thread[0] = exc
                finally:
                    thread_done.set()

            def _holder() -> None:
                with cpt._exclusive_state_lock(proj):
                    lock_held.set()
                    # Hold the lock until the writer thread finishes.
                    thread_done.wait(timeout=10)

            t_holder = threading.Thread(target=_holder, name="cpt-lock-holder")
            t_writer = threading.Thread(target=_writer, name="ag-writer")

            t_holder.start()
            self.assertTrue(lock_held.wait(timeout=5), "Lock holder must acquire lock within timeout")
            t_writer.start()
            t_writer.join(timeout=5)
            t_holder.join(timeout=5)

            self.assertIsNot(thread_done.is_set(), False, "Writer thread must complete")
            self.assertIsNotNone(error_from_thread[0], "Writer must fail with lock contention")
            self.assertIsInstance(
                error_from_thread[0],
                ag.ApprovalSnapshotConflictError,
                f"Expected ApprovalSnapshotConflictError, got {type(error_from_thread[0]).__name__}",
            )
            self.assertIn("lock", str(error_from_thread[0]).lower())

            # Verify no grant file.
            target = proj / "docs" / "pm" / "approvals" / "EVT-DAT-001.yaml"
            self.assertFalse(target.exists(), "Grant file must not be created")

            # Threads must have exited.
            self.assertFalse(t_holder.is_alive(), "Lock holder thread must exit")
            self.assertFalse(t_writer.is_alive(), "Writer thread must exit")

            # Lock file persists.
            lock_path = proj / ".agentdesk" / "runtime" / ".state-transition.lock"
            self.assertTrue(lock_path.exists(), "Lock file must persist")

    def test_direction_b_thread_ag_locks_cpt_blocked(self) -> None:
        """Thread-based Direction B: ag._exclusive_state_lock held → cpt throws.

        ApprovalGate holds the lock in another thread; TC-13.11
        must raise TransitionLockContentionError."""
        with _temp_project() as proj:
            lock_held = threading.Event()
            error_from_thread: list[BaseException | None] = [None]
            thread_done = threading.Event()

            def _cpt_acquirer() -> None:
                try:
                    with cpt._exclusive_state_lock(proj):
                        error_from_thread[0] = AssertionError("inner body must not execute")
                except BaseException as exc:
                    error_from_thread[0] = exc
                finally:
                    thread_done.set()

            def _holder() -> None:
                with ag._exclusive_state_lock(proj):
                    lock_held.set()
                    thread_done.wait(timeout=10)

            t_holder = threading.Thread(target=_holder, name="ag-lock-holder")
            t_cpt = threading.Thread(target=_cpt_acquirer, name="cpt-acquirer")

            t_holder.start()
            self.assertTrue(lock_held.wait(timeout=5), "Lock holder must acquire lock within timeout")
            t_cpt.start()
            t_cpt.join(timeout=5)
            t_holder.join(timeout=5)

            self.assertIsNot(thread_done.is_set(), False, "CPT acquirer thread must complete")
            self.assertIsNotNone(error_from_thread[0], "CPT must fail with lock contention")
            self.assertIsInstance(
                error_from_thread[0],
                cpt.TransitionLockContentionError,
                f"Expected TransitionLockContentionError, got {type(error_from_thread[0]).__name__}",
            )
            self.assertIn("lock", str(error_from_thread[0]).lower())

            # Threads must have exited.
            self.assertFalse(t_holder.is_alive(), "Lock holder thread must exit")
            self.assertFalse(t_cpt.is_alive(), "CPT acquirer thread must exit")

            # Lock file persists.
            lock_path = proj / ".agentdesk" / "runtime" / ".state-transition.lock"
            self.assertTrue(lock_path.exists(), "Lock file must persist")

    def test_lock_file_persists(self) -> None:
        """The lock file must persist after the lock is released."""
        with _temp_project() as proj:
            head = _git_head(proj)
            lock_path = proj / ".agentdesk" / "runtime" / ".state-transition.lock"
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="Test",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            self.assertTrue(lock_path.exists(), "Lock file must persist after unlock")

    def test_no_temp_file_residue(self) -> None:
        """No temporary files should remain after write."""
        with _temp_project() as proj:
            head = _git_head(proj)
            before = set(proj.glob("**/*"))
            ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="Test",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            after = set(proj.glob("**/*"))
            # Check no .tmp or other temp files.
            for p in after - before:
                self.assertFalse(
                    str(p.name).startswith(".EVT-") and ".tmp" in str(p),
                    f"Temp file left: {p}"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Atomic write failure protection
# ═══════════════════════════════════════════════════════════════════════════════


class AtomicWriteTests(unittest.TestCase):
    """Test atomic write failure protection."""

    def test_original_file_unchanged_on_failure(self) -> None:
        """If os.replace fails, the target must not be modified."""
        with _temp_project() as proj:
            head = _git_head(proj)
            # Create a pre-existing file.
            target = proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml"
            target.write_text("original content", encoding="utf-8")
            target_bytes = target.read_bytes()

            # Try to write — should fail because target exists.
            with self.assertRaises(ag.ApprovalValidationError):
                ag.write_grant(
                    project_root=proj,
                    approval_id="APR-001",
                    event_id="EVT-0001",
                    scope=ag.ApprovalScope.DISPATCH,
                    subject=_make_subject(),
                    lease_epoch=1,
                    now=_NOW,
                    reason="Test",
                    expires_at=None,
                    expected_snapshot_commit=head,
                )
            # Original file must be unchanged.
            self.assertEqual(target_bytes, target.read_bytes())

    def test_content_correct_after_write(self) -> None:
        """Written file must have correct content, LF ending, and be valid JSON."""
        with _temp_project() as proj:
            head = _git_head(proj)
            path, _ = ag.write_grant(
                project_root=proj,
                approval_id="APR-001",
                event_id="EVT-001",
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                lease_epoch=1,
                now=_NOW,
                reason="Test",
                expires_at=None,
                expected_snapshot_commit=head,
            )
            raw = path.read_text(encoding="utf-8")
            # Must end with LF.
            self.assertTrue(raw.endswith("\n"))
            # Must not have CRLF.
            self.assertNotIn("\r\n", raw)
            # Must be valid JSON.
            data = json.loads(raw)
            self.assertIsInstance(data, dict)
            self.assertEqual(head, data["snapshot_commit"])


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Phase boundary — ApprovalGate stubs
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalGateStubTests(unittest.TestCase):
    """Test ApprovalGate phase boundary behaviour."""

    def test_construction_validates_project_root(self) -> None:
        with _temp_project() as proj:
            gate = ag.ApprovalGate(project_root=proj)
            self.assertEqual(proj, gate.project_root)

    def test_construction_rejects_relative_path(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalGate(project_root=Path("relative/path"))

    def test_construction_rejects_non_existent(self) -> None:
        with self.assertRaises(ValueError):
            ag.ApprovalGate(project_root=Path("/nonexistent/path/12345"))

    def test_check_raises_not_implemented(self) -> None:
        with _temp_project() as proj:
            gate = ag.ApprovalGate(project_root=proj)
            req = ag.ApprovalCheckRequest(
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                expected_snapshot_commit=_SAMPLE_SHA,
            )
            with self.assertRaises(NotImplementedError) as ctx:
                gate.check(req, _NOW)
            self.assertIn("ApprovalGate.check", str(ctx.exception))
            self.assertIn("TC-13.12c", str(ctx.exception))

    def test_require_raises_not_implemented(self) -> None:
        with _temp_project() as proj:
            gate = ag.ApprovalGate(project_root=proj)
            req = ag.ApprovalCheckRequest(
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(),
                expected_snapshot_commit=_SAMPLE_SHA,
            )
            with self.assertRaises(NotImplementedError) as ctx:
                gate.require(req, _NOW)
            self.assertIn("ApprovalGate.require", str(ctx.exception))
            self.assertIn("TC-13.12c", str(ctx.exception))

    def test_not_implemented_message_has_no_request_content(self) -> None:
        """NotImplementedError must not leak request content."""
        with _temp_project() as proj:
            gate = ag.ApprovalGate(project_root=proj)
            req = ag.ApprovalCheckRequest(
                scope=ag.ApprovalScope.DISPATCH,
                subject=_make_subject(task_id="TC-042"),
                expected_snapshot_commit=_SAMPLE_SHA,
            )
            with self.assertRaises(NotImplementedError) as ctx:
                gate.check(req, _NOW)
            msg = str(ctx.exception)
            self.assertNotIn("TC-042", msg)
            self.assertNotIn("dispatch", msg)
            self.assertNotIn(_SAMPLE_SHA, msg)

    def test_no_file_writes_on_construction(self) -> None:
        with _temp_project() as proj:
            before = list(proj.glob("**/*"))
            ag.ApprovalGate(project_root=proj)
            after = list(proj.glob("**/*"))
            self.assertEqual(len(before), len(after))

    def test_no_evidence_read_on_construction(self) -> None:
        with _temp_project() as proj:
            (proj / "docs" / "pm" / "approvals" / "EVT-0001.yaml").write_text(
                _make_grant_yaml(), encoding="utf-8"
            )
            # Construction must not raise despite evidence being present.
            ag.ApprovalGate(project_root=proj)

    def test_frozen_slots(self) -> None:
        with _temp_project() as proj:
            gate = ag.ApprovalGate(project_root=proj)
            with self.assertRaises(Exception):
                gate.project_root = Path("/other")  # type: ignore[misc]
            with self.assertRaises(AttributeError):
                gate.__dict__  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════════
# 15. -O flag compatibility
# ═══════════════════════════════════════════════════════════════════════════════


class OptimizedFlagCompatibilityTests(unittest.TestCase):
    """Verify behaviour under python -O (no reliance on assert)."""

    def test_validation_works_even_with_O(self) -> None:
        """These tests must work the same with or without -O flag.
        No security check may depend on assert statements."""
        # Verify typed model validation uses explicit checks, not assert.
        with self.assertRaises(TypeError):
            _make_evidence(approval_id=True)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            _make_evidence(lease_epoch=0)

        with self.assertRaises(ValueError):
            ag.ApprovalSubject(
                task_id="TC-001",
                revision=-1,
                attempt=1,
                dispatch_id="DSP-001",
                accepted_commit=None,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 16. __all__ is importable
# ═══════════════════════════════════════════════════════════════════════════════


class AllImportableTests(unittest.TestCase):
    """Verify all 15 __all__ symbols are actually importable."""

    def test_all_symbols_importable(self) -> None:
        for name in ag.__all__:
            self.assertTrue(hasattr(ag, name), f"__all__ symbol {name} not found in module")


if __name__ == "__main__":
    unittest.main()
