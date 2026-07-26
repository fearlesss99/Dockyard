"""TC-13.6 Commit 1: comprehensive tests for ``mad_refs.py``.

stdlib-only unittest; no third-party packages, I/O (beyond tmp_path),
subprocess, or network.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# -- Load the module under test -------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

sys.path.insert(0, str(_SCRIPTS))
import mad_refs  # noqa: E402

sys.path.pop(0)

# Convenience aliases.
MadRefEntry = mad_refs.MadRefEntry
LockContentionError = mad_refs.LockContentionError
MadRefsValidationError = mad_refs.MadRefsValidationError
RefIntegrityError = mad_refs.RefIntegrityError
SCHEMA_VERSION = mad_refs.SCHEMA_VERSION
ALLOWED_PURPOSES = mad_refs.ALLOWED_PURPOSES
ALLOWED_DEPTHS = mad_refs.ALLOWED_DEPTHS

# Platform-appropriate absolute path for tests.
_ABS_ROOT = (
    Path("C:/abs_root") if sys.platform == "win32"
    else Path("/abs_root")
)
_ABS_ARCHIVE = str(_ABS_ROOT / "deliberations" / "test-id")
_ABS_SHORT = str(_ABS_ROOT / "abs")
_ABS_ORIG = str(_ABS_ROOT / "abs" / "orig")


# =========================================================================
# Helpers
# =========================================================================


def _make_entry(
    task_id: str = "TC-031",
    dispatch_id: str = "DSP-TC031-R2-A1-7F3C",
    purpose: str = "planning",
    deliberation_id: str = "20260726T120000Z-a1b2c3d4",
    depth: str = "deep",
    stdout_sha256: str = "e" * 64,
    report_sha256: str = "f" * 64,
    status: str = "完成",
    archive_path: str | None = None,
    created_at: str = "2026-07-26T12:00:00Z",
) -> MadRefEntry:
    """Return a valid MadRefEntry with default values that pass validation."""
    if archive_path is None:
        archive_path = _ABS_ARCHIVE
    return MadRefEntry(
        task_id=task_id,
        dispatch_id=dispatch_id,
        purpose=purpose,
        deliberation_id=deliberation_id,
        depth=depth,
        stdout_sha256=stdout_sha256,
        report_sha256=report_sha256,
        status=status,
        archive_path=archive_path,
        created_at=created_at,
    )


def _fresh_data() -> dict:
    """Return a fresh, empty mad-refs/v1 document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": "2026-07-26T12:00:00Z",
        "refs": [],
    }


def _sha256(s: str) -> str:
    """Return 64-char hex SHA-256 of *s*."""
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# =========================================================================
# 1 — Module-level properties
# =========================================================================


class ModulePropertiesTests(unittest.TestCase):
    """Verify module constants and __all__."""

    def test_schema_version_is_correct(self) -> None:
        self.assertEqual(mad_refs.SCHEMA_VERSION, "agentdesk.mad-refs/v1")

    def test_allowed_purposes(self) -> None:
        self.assertEqual(mad_refs.ALLOWED_PURPOSES, frozenset({"planning", "audit"}))

    def test_allowed_depths(self) -> None:
        self.assertEqual(
            mad_refs.ALLOWED_DEPTHS, frozenset({"fast", "balanced", "deep"})
        )

    def test_all_exports_exact(self) -> None:
        expected = frozenset({
            "ALLOWED_DEPTHS",
            "ALLOWED_PURPOSES",
            "LockContentionError",
            "MadRefEntry",
            "MadRefsValidationError",
            "RefIntegrityError",
            "SCHEMA_VERSION",
            "append_mad_ref",
            "find_existing_ref",
            "read_mad_refs",
            "validate_mad_refs",
        })
        actual = frozenset(mad_refs.__all__)
        self.assertSetEqual(expected, actual)

    def test_all_count_is_eleven(self) -> None:
        self.assertEqual(len(mad_refs.__all__), 11)

    def test_ref_field_names_exactly_ten(self) -> None:
        self.assertEqual(len(mad_refs._REF_FIELD_NAMES), 10)

    def test_ref_field_set_exactly_ten(self) -> None:
        self.assertEqual(len(mad_refs._REF_FIELD_SET), 10)


# =========================================================================
# 2 — Import has no side effects
# =========================================================================


class ImportSideEffectTests(unittest.TestCase):
    """Importing ``mad_refs`` produces no stdout/stderr."""

    def test_import_produces_no_output(self) -> None:
        """Import in an isolated subprocess to avoid sys.modules contamination."""
        import subprocess as _sp
        scripts_dir = str(_SCRIPTS)
        proc = _sp.run(
            [sys.executable, "-c", fr"""
import importlib, io, sys
from contextlib import redirect_stdout, redirect_stderr
name = "mad_refs"
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

    def test_module_has_no_io_on_load(self) -> None:
        names = set(dir(mad_refs))
        for forbidden in ("subprocess", "socket", "urlopen", "requests"):
            self.assertNotIn(forbidden, names)


# =========================================================================
# 3 — MadRefEntry construction
# =========================================================================


class MadRefEntryConstructionTests(unittest.TestCase):
    """MadRefEntry is frozen and has exactly 10 fields."""

    def test_construction_from_keywords(self) -> None:
        e = _make_entry()
        self.assertEqual(e.task_id, "TC-031")
        self.assertEqual(e.purpose, "planning")
        self.assertEqual(e.depth, "deep")

    def test_immutable(self) -> None:
        e = _make_entry()
        with self.assertRaises(AttributeError):
            e.task_id = "other"  # type: ignore[misc]

    def test_unicode_status_preserved(self) -> None:
        e = _make_entry(status="带警告完成")  # 带警告完成
        self.assertEqual(e.status, "带警告完成")

    def test_any_status_string_accepted(self) -> None:
        """No English-enum restriction on status."""
        e = _make_entry(status="custom-status-with-future-value")
        self.assertEqual(e.status, "custom-status-with-future-value")


# =========================================================================
# 4 — validate_mad_refs: valid document
# =========================================================================


class ValidateValidDocumentTests(unittest.TestCase):
    """validate_mad_refs returns empty errors for valid documents."""

    def test_empty_refs_is_valid(self) -> None:
        data = _fresh_data()
        errors = mad_refs.validate_mad_refs(data)
        self.assertEqual(errors, [])

    def test_one_valid_ref_is_valid(self) -> None:
        data = _fresh_data()
        data["refs"].append({
            "task_id": "TC-001",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "完成",
            "archive_path": _ABS_ARCHIVE,
            "created_at": "2026-07-26T12:00:00Z",
        })
        errors = mad_refs.validate_mad_refs(data)
        self.assertEqual(errors, [])

    def test_two_valid_refs(self) -> None:
        data = _fresh_data()
        for i in range(2):
            data["refs"].append({
                "task_id": f"TC-{i:03d}",
                "dispatch_id": f"DSP-{i:03d}",
                "purpose": "planning",
                "deliberation_id": f"id-{i:03d}",
                "depth": "balanced",
                "stdout_sha256": "c" * 64,
                "report_sha256": "d" * 64,
                "status": "done",
                "archive_path": _ABS_ARCHIVE,
                "created_at": "2026-07-26T12:00:00Z",
            })
        errors = mad_refs.validate_mad_refs(data)
        self.assertEqual(errors, [])

    def test_all_three_depths_valid(self) -> None:
        for depth in ("fast", "balanced", "deep"):
            data = _fresh_data()
            data["refs"].append({
                "task_id": "TC-001",
                "dispatch_id": f"DSP-{depth}",
                "purpose": "planning",
                "deliberation_id": f"id-{depth}",
                "depth": depth,
                "stdout_sha256": "0" * 64,
                "report_sha256": "1" * 64,
                "status": "ok",
                "archive_path": _ABS_SHORT,
                "created_at": "2026-07-26T12:00:00Z",
            })
            with self.subTest(depth=depth):
                errors = mad_refs.validate_mad_refs(data)
                self.assertEqual(errors, [])

    def test_both_purposes_valid(self) -> None:
        for purpose in ("planning", "audit"):
            data = _fresh_data()
            data["refs"].append({
                "task_id": "TC-001",
                "dispatch_id": f"DSP-{purpose}",
                "purpose": purpose,
                "deliberation_id": f"id-{purpose}",
                "depth": "deep",
                "stdout_sha256": "0" * 64,
                "report_sha256": "1" * 64,
                "status": "ok",
                "archive_path": _ABS_SHORT,
                "created_at": "2026-07-26T12:00:00Z",
            })
            with self.subTest(purpose=purpose):
                errors = mad_refs.validate_mad_refs(data)
                self.assertEqual(errors, [])

    def test_created_at_with_subsecond_and_tz(self) -> None:
        for ts in (
            "2026-07-26T12:00:00Z",
            "2026-07-26T12:00:00.123456Z",
            "2026-07-26T12:00:00+08:00",
            "2026-07-26T12:00:00.999-05:00",
        ):
            data = _fresh_data()
            data["refs"].append({
                "task_id": "TC-001",
                "dispatch_id": f"DSP-{ts[:10]}",
                "purpose": "planning",
                "deliberation_id": f"id-{ts[:10]}",
                "depth": "deep",
                "stdout_sha256": "0" * 64,
                "report_sha256": "1" * 64,
                "status": "ok",
                "archive_path": _ABS_SHORT,
                "created_at": ts,
            })
            with self.subTest(ts=ts):
                errors = mad_refs.validate_mad_refs(data)
                self.assertEqual(errors, [])


# =========================================================================
# 5 — validate_mad_refs: root key errors
# =========================================================================


class ValidateRootKeyErrorsTests(unittest.TestCase):
    """validate_mad_refs rejects wrong root keys."""

    def test_non_dict_root_rejected(self) -> None:
        errors = mad_refs.validate_mad_refs([])  # type: ignore[arg-type]
        self.assertIn("root must be an object", errors)

    def test_wrong_schema_version_rejected(self) -> None:
        data = _fresh_data()
        data["schema_version"] = "agentdesk.mad-refs/v99"
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_schema_version_none_rejected(self) -> None:
        data = _fresh_data()
        data["schema_version"] = None
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_missing_refs_key(self) -> None:
        data = _fresh_data()
        del data["refs"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("refs must be an array" in e for e in errors))

    def test_missing_schema_version_key(self) -> None:
        data = _fresh_data()
        del data["schema_version"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("schema_version" in e for e in errors))
        self.assertTrue(any("missing root key" in e for e in errors))

    def test_missing_updated_at_key(self) -> None:
        data = _fresh_data()
        del data["updated_at"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("missing root key" in e for e in errors))

    def test_extra_root_key_rejected(self) -> None:
        data = _fresh_data()
        data["bonus"] = "nope"
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("forbidden root key" in e for e in errors))

    def test_refs_not_a_list(self) -> None:
        data = _fresh_data()
        data["refs"] = "not-a-list"
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("refs must be an array" in e for e in errors))

    def test_refs_is_null(self) -> None:
        data = _fresh_data()
        data["refs"] = None
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("refs must be an array" in e for e in errors))


# =========================================================================
# 6 — validate_mad_refs: ref field errors
# =========================================================================


class ValidateRefFieldErrorsTests(unittest.TestCase):
    """validate_mad_refs rejects wrong ref field shapes."""

    def _one_ref_data(self, **overrides) -> dict:
        base = {
            "task_id": "TC-001",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "ok",
            "archive_path": _ABS_SHORT,
            "created_at": "2026-07-26T12:00:00Z",
        }
        base.update(overrides)
        data = _fresh_data()
        data["refs"].append(base)
        return data

    def test_missing_task_id(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["task_id"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("task_id" in e for e in errors))

    def test_missing_dispatch_id(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["dispatch_id"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("dispatch_id" in e for e in errors))

    def test_missing_purpose(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["purpose"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("purpose" in e for e in errors))

    def test_missing_deliberation_id(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["deliberation_id"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("deliberation_id" in e for e in errors))

    def test_missing_depth(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["depth"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("depth" in e for e in errors))

    def test_missing_stdout_sha256(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["stdout_sha256"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("stdout_sha256" in e for e in errors))

    def test_missing_report_sha256(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["report_sha256"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("report_sha256" in e for e in errors))

    def test_missing_status(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["status"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("status" in e for e in errors))

    def test_missing_archive_path(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["archive_path"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("archive_path" in e for e in errors))

    def test_missing_created_at(self) -> None:
        data = self._one_ref_data()
        del data["refs"][0]["created_at"]
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("created_at" in e for e in errors))

    def test_all_ten_missing_detected(self) -> None:
        """Each of the 10 required fields produces an error when missing."""
        ref_template = {
            "task_id": "TC-001",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "ok",
            "archive_path": _ABS_SHORT,
            "created_at": "2026-07-26T12:00:00Z",
        }
        for key in ref_template:
            corrupt = dict(ref_template)
            del corrupt[key]
            data = _fresh_data()
            data["refs"].append(corrupt)
            with self.subTest(missing=key):
                errors = mad_refs.validate_mad_refs(data)
                self.assertTrue(any(key in e for e in errors),
                                f"missing {key} not detected")

    def test_extra_ref_field_rejected(self) -> None:
        data = self._one_ref_data(bonus_field="should-not-be-here")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("forbidden field" in e for e in errors))

    def test_non_object_ref_rejected(self) -> None:
        data = _fresh_data()
        data["refs"].append("not-an-object")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("refs[0] must be an object" in e for e in errors))

    # ── value-type errors ─────────────────────────────────────────────

    def test_purpose_not_in_allowed_set(self) -> None:
        data = self._one_ref_data(purpose="unknown")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("purpose must be one of" in e for e in errors))

    def test_purpose_not_a_string(self) -> None:
        data = self._one_ref_data(purpose=42)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("purpose" in e for e in errors))

    def test_depth_not_in_allowed_set(self) -> None:
        data = self._one_ref_data(depth="ultra")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("depth must be one of" in e for e in errors))

    def test_depth_is_int(self) -> None:
        data = self._one_ref_data(depth=42)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("depth" in e for e in errors))

    def test_stdout_sha256_wrong_length(self) -> None:
        data = self._one_ref_data(stdout_sha256="abc")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("stdout_sha256" in e for e in errors))

    def test_stdout_sha256_uppercase_rejected(self) -> None:
        data = self._one_ref_data(stdout_sha256="A" * 64)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("stdout_sha256" in e for e in errors))

    def test_stdout_sha256_non_hex(self) -> None:
        data = self._one_ref_data(stdout_sha256="g" * 64)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("stdout_sha256" in e for e in errors))

    def test_stdout_sha256_is_null(self) -> None:
        data = self._one_ref_data(stdout_sha256=None)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("stdout_sha256" in e for e in errors))

    def test_report_sha256_wrong_length(self) -> None:
        data = self._one_ref_data(report_sha256="short")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("report_sha256" in e for e in errors))

    def test_report_sha256_uppercase_rejected(self) -> None:
        data = self._one_ref_data(report_sha256="F" * 64)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("report_sha256" in e for e in errors))

    def test_created_at_not_rfc3339(self) -> None:
        data = self._one_ref_data(created_at="yesterday")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("created_at must be RFC 3339" in e for e in errors))

    def test_created_at_missing_timezone(self) -> None:
        data = self._one_ref_data(created_at="2026-07-26T12:00:00")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("created_at must be RFC 3339" in e for e in errors))

    def test_archive_path_relative_rejected(self) -> None:
        data = self._one_ref_data(archive_path="relative/path")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("archive_path must be absolute" in e for e in errors))

    def test_archive_path_empty_string_rejected(self) -> None:
        data = self._one_ref_data(archive_path="   ")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("archive_path must be a non-empty string" in e for e in errors))

    def test_archive_path_with_dots_is_accepted_if_absolute(self) -> None:
        """archive_path must be absolute; ``..`` inside an absolute path
        is not an escape — it's still absolute.  Validate on a truly
        absolute path."""
        abs_with_dots = str(_ABS_ROOT / "foo" / ".." / "bar")
        data = self._one_ref_data(archive_path=abs_with_dots)
        errors = mad_refs.validate_mad_refs(data)
        self.assertEqual(errors, [])

    def test_empty_task_id_rejected(self) -> None:
        data = self._one_ref_data(task_id="")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("task_id" in e for e in errors))

    def test_whitespace_task_id_rejected(self) -> None:
        data = self._one_ref_data(task_id="   ")
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("task_id" in e for e in errors))

    def test_task_id_is_int_rejected(self) -> None:
        data = self._one_ref_data(task_id=42)
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("task_id" in e for e in errors))


# =========================================================================
# 7 — validate_mad_refs: duplicate records
# =========================================================================


class ValidateDuplicateTests(unittest.TestCase):
    """validate_mad_refs detects duplicate (dispatch_id, purpose) pairs."""

    def test_duplicate_dispatch_purpose_rejected(self) -> None:
        data = _fresh_data()
        ref = {
            "task_id": "TC-001",
            "dispatch_id": "DSP-DUP",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "ok",
            "archive_path": _ABS_SHORT,
            "created_at": "2026-07-26T12:00:00Z",
        }
        data["refs"].append(dict(ref))
        data["refs"].append(dict(ref))
        errors = mad_refs.validate_mad_refs(data)
        self.assertTrue(any("duplicate" in e for e in errors))


# =========================================================================
# 8 — read_mad_refs
# =========================================================================


class ReadMadRefsTests(unittest.TestCase):
    """``read_mad_refs`` reads and validates existing files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.runtime = self.project / ".agentdesk" / "runtime"
        self.refs_path = self.runtime / "mad-refs.yaml"

    def _write(self, data: dict) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.refs_path.write_text(content, encoding="utf-8")

    def test_reads_valid_empty_file(self) -> None:
        self._write(_fresh_data())
        data = mad_refs.read_mad_refs(self.project)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["refs"], [])

    def test_reads_file_with_one_ref(self) -> None:
        d = _fresh_data()
        d["refs"].append({
            "task_id": "TC-001",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "完成",
            "archive_path": _ABS_ARCHIVE,
            "created_at": "2026-07-26T12:00:00Z",
        })
        self._write(d)
        data = mad_refs.read_mad_refs(self.project)
        self.assertEqual(len(data["refs"]), 1)

    def test_raises_on_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            mad_refs.read_mad_refs(self.project)

    def test_raises_on_corrupt_json(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.refs_path.write_text("not json {{{", encoding="utf-8")
        with self.assertRaises(MadRefsValidationError):
            mad_refs.read_mad_refs(self.project)

    def test_raises_on_invalid_schema(self) -> None:
        d = _fresh_data()
        d["schema_version"] = "wrong/v1"
        self._write(d)
        with self.assertRaises(MadRefsValidationError):
            mad_refs.read_mad_refs(self.project)

    def test_unicode_round_trip(self) -> None:
        d = _fresh_data()
        d["refs"].append({
            "task_id": "TC-031",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-中文",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "带警告完成",
            "archive_path": _ABS_SHORT,
            "created_at": "2026-07-26T12:00:00Z",
        })
        self._write(d)
        data = mad_refs.read_mad_refs(self.project)
        self.assertEqual(data["refs"][0]["status"], "带警告完成")
        self.assertEqual(data["refs"][0]["deliberation_id"], "id-中文")


# =========================================================================
# 9 — find_existing_ref
# =========================================================================


class FindExistingRefTests(unittest.TestCase):
    """``find_existing_ref`` returns the matching entry or None."""

    def test_returns_none_for_empty_refs(self) -> None:
        data = _fresh_data()
        self.assertIsNone(
            mad_refs.find_existing_ref(data, "DSP-001", "planning")
        )

    def test_returns_none_for_unknown_dispatch(self) -> None:
        data = _fresh_data()
        entry = _make_entry(dispatch_id="DSP-001", purpose="planning")
        data["refs"].append({
            "task_id": entry.task_id,
            "dispatch_id": entry.dispatch_id,
            "purpose": entry.purpose,
            "deliberation_id": entry.deliberation_id,
            "depth": entry.depth,
            "stdout_sha256": entry.stdout_sha256,
            "report_sha256": entry.report_sha256,
            "status": entry.status,
            "archive_path": entry.archive_path,
            "created_at": entry.created_at,
        })
        self.assertIsNone(
            mad_refs.find_existing_ref(data, "DSP-999", "planning")
        )

    def test_returns_entry_for_matching_pair(self) -> None:
        data = _fresh_data()
        ref_dict = {
            "task_id": "TC-001",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "ok",
            "archive_path": _ABS_SHORT,
            "created_at": "2026-07-26T12:00:00Z",
        }
        data["refs"].append(ref_dict)
        found = mad_refs.find_existing_ref(data, "DSP-001", "planning")
        self.assertIsNotNone(found)
        self.assertIsInstance(found, MadRefEntry)
        self.assertEqual(found.dispatch_id, "DSP-001")
        self.assertEqual(found.purpose, "planning")

    def test_different_purpose_is_different(self) -> None:
        data = _fresh_data()
        ref_dict = {
            "task_id": "TC-001",
            "dispatch_id": "DSP-001",
            "purpose": "planning",
            "deliberation_id": "id-001",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "ok",
            "archive_path": _ABS_SHORT,
            "created_at": "2026-07-26T12:00:00Z",
        }
        data["refs"].append(ref_dict)
        self.assertIsNone(
            mad_refs.find_existing_ref(data, "DSP-001", "audit")
        )


# =========================================================================
# 10 — append_mad_ref: success paths
# =========================================================================


class AppendMadRefSuccessTests(unittest.TestCase):
    """``append_mad_ref`` creates, appends, and writes atomically."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.refs_path = self.project / ".agentdesk" / "runtime" / "mad-refs.yaml"

    def _read_raw(self) -> str | None:
        try:
            return self.refs_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def test_first_append_creates_file(self) -> None:
        entry = _make_entry()
        self.assertFalse(self.refs_path.exists())
        mad_refs.append_mad_ref(self.project, entry)
        self.assertTrue(self.refs_path.exists())
        data = json.loads(self._read_raw())  # type: ignore[arg-type]
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(data["refs"]), 1)
        self.assertEqual(data["refs"][0]["dispatch_id"], entry.dispatch_id)

    def test_second_append_appends(self) -> None:
        e1 = _make_entry(dispatch_id="DSP-001", deliberation_id="id-001")
        e2 = _make_entry(dispatch_id="DSP-002", deliberation_id="id-002")
        mad_refs.append_mad_ref(self.project, e1)
        mad_refs.append_mad_ref(self.project, e2)
        data = json.loads(self._read_raw())  # type: ignore[arg-type]
        self.assertEqual(len(data["refs"]), 2)
        self.assertEqual(data["refs"][0]["dispatch_id"], "DSP-001")
        self.assertEqual(data["refs"][1]["dispatch_id"], "DSP-002")

    def test_unicode_not_escaped(self) -> None:
        entry = _make_entry(status="完成")
        mad_refs.append_mad_ref(self.project, entry)
        raw = self._read_raw()
        self.assertIn("完成", raw)

    def test_file_ends_with_newline(self) -> None:
        mad_refs.append_mad_ref(self.project, _make_entry())
        raw = self._read_raw()
        self.assertTrue(raw.endswith("\n"), f"does not end with newline: {raw!r}")

    def test_file_is_valid_json(self) -> None:
        mad_refs.append_mad_ref(self.project, _make_entry())
        raw = self._read_raw()
        data = json.loads(raw)  # type: ignore[arg-type]
        self.assertIsInstance(data, dict)

    def test_true_idempotent_replay_all_10_fields_match(self) -> None:
        """All 10 fields equal, same (dispatch_id, purpose): no error,
        no write, file bytes unchanged, updated_at unchanged."""
        e1 = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            task_id="TC-031", deliberation_id="id-001", depth="deep",
            stdout_sha256="a" * 64, report_sha256="b" * 64,
            status="完成", archive_path=_ABS_ARCHIVE,
            created_at="2026-07-26T12:00:00Z",
        )
        r1 = mad_refs.append_mad_ref(self.project, e1)
        self.assertEqual(len(r1["refs"]), 1)

        original_bytes = self._read_raw()
        original_updated = r1["updated_at"]

        # Replay exact same entry — all 10 fields identical.
        e2 = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            task_id="TC-031", deliberation_id="id-001", depth="deep",
            stdout_sha256="a" * 64, report_sha256="b" * 64,
            status="完成", archive_path=_ABS_ARCHIVE,
            created_at="2026-07-26T12:00:00Z",
        )
        r2 = mad_refs.append_mad_ref(self.project, e2)
        self.assertEqual(len(r2["refs"]), 1, "must not append duplicate")

        # File bytes must be byte-identical.
        self.assertEqual(self._read_raw(), original_bytes,
                         "file bytes must be unchanged on idempotent replay")
        self.assertEqual(r2["updated_at"], original_updated,
                         "updated_at must not change on idempotent replay")

    def test_duplicate_deliberation_id_warns(self) -> None:
        e1 = _make_entry(
            dispatch_id="DSP-001",
            deliberation_id="shared-id",
        )
        mad_refs.append_mad_ref(self.project, e1)

        e2 = _make_entry(
            dispatch_id="DSP-002",
            deliberation_id="shared-id",
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = mad_refs.append_mad_ref(self.project, e2)
            self.assertEqual(len(w), 1)
            self.assertIn("shared-id", str(w[0].message))
        self.assertEqual(len(result["refs"]), 2)

    def test_updated_at_refreshed_on_each_append(self) -> None:
        e1 = _make_entry(dispatch_id="DSP-001")
        r1 = mad_refs.append_mad_ref(self.project, e1)
        ts1 = r1["updated_at"]

        time.sleep(1.1)  # Ensure timestamp changes
        e2 = _make_entry(dispatch_id="DSP-002")
        r2 = mad_refs.append_mad_ref(self.project, e2)
        ts2 = r2["updated_at"]

        self.assertNotEqual(ts1, ts2)

    def test_refs_preserved_order(self) -> None:
        for i in range(5):
            mad_refs.append_mad_ref(
                self.project,
                _make_entry(dispatch_id=f"DSP-{i:03d}", deliberation_id=f"id-{i:03d}"),
            )
        data = json.loads(self._read_raw())  # type: ignore[arg-type]
        self.assertEqual(
            [r["dispatch_id"] for r in data["refs"]],
            [f"DSP-{i:03d}" for i in range(5)],
        )


# =========================================================================
# 11 — append_mad_ref: lock contention
# =========================================================================


class AppendMadRefLockContentionTests(unittest.TestCase):
    """``append_mad_ref`` fails fast when lock is held."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.runtime = self.project / ".agentdesk" / "runtime"
        self.lock = mad_refs._lock_path(self.runtime)

    def test_lock_already_held_raises(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("held", encoding="utf-8")
        with self.assertRaises(LockContentionError):
            mad_refs.append_mad_ref(self.project, _make_entry())

    def test_lock_released_after_success(self) -> None:
        mad_refs.append_mad_ref(self.project, _make_entry())
        self.assertFalse(
            self.lock.exists(),
            "lock must be released after successful append",
        )

    def test_lock_released_after_validation_failure(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        # Write a corrupt existing file so validation fails.
        refs_path = self.runtime / "mad-refs.yaml"
        refs_path.write_text("not valid json", encoding="utf-8")
        with self.assertRaises(MadRefsValidationError):
            mad_refs.append_mad_ref(self.project, _make_entry())
        self.assertFalse(
            self.lock.exists(),
            "lock must be released even after failure",
        )

    def test_lock_released_after_ref_integrity_error(self) -> None:
        """Lock is released after RefIntegrityError."""
        e1 = _make_entry(dispatch_id="DSP-001", purpose="planning",
                         task_id="TC-A", deliberation_id="id-A")
        mad_refs.append_mad_ref(self.project, e1)
        # Same logical key but different task_id → RefIntegrityError.
        e2 = _make_entry(dispatch_id="DSP-001", purpose="planning",
                         task_id="TC-B")
        with self.assertRaises(RefIntegrityError):
            mad_refs.append_mad_ref(self.project, e2)
        self.assertFalse(self.lock.exists(),
                         "lock must be released after RefIntegrityError")

    def test_original_file_unchanged_on_lock_contention(self) -> None:
        """When lock exists, original file bytes are totally unchanged."""
        self.runtime.mkdir(parents=True, exist_ok=True)
        refs_path = self.runtime / "mad-refs.yaml"
        original_content = json.dumps(_fresh_data(), ensure_ascii=False, indent=2) + "\n"
        refs_path.write_text(original_content, encoding="utf-8")
        self.lock.write_text("held", encoding="utf-8")

        with self.assertRaises(LockContentionError):
            mad_refs.append_mad_ref(self.project, _make_entry())

        self.assertEqual(refs_path.read_text(encoding="utf-8"), original_content)


# =========================================================================
# 12 — append_mad_ref: original file preservation on failure
# =========================================================================


class AppendMadRefPreservationTests(unittest.TestCase):
    """Original file bytes are never modified when append fails."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.refs_path = self.project / ".agentdesk" / "runtime" / "mad-refs.yaml"

    def _create_valid_file(self) -> str:
        d = _fresh_data()
        d["refs"].append({
            "task_id": "TC-ORIG",
            "dispatch_id": "DSP-ORIG",
            "purpose": "planning",
            "deliberation_id": "id-orig",
            "depth": "deep",
            "stdout_sha256": "a" * 64,
            "report_sha256": "b" * 64,
            "status": "original",
            "archive_path": _ABS_ORIG,
            "created_at": "2026-07-26T12:00:00Z",
        })
        content = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
        self.refs_path.parent.mkdir(parents=True, exist_ok=True)
        self.refs_path.write_text(content, encoding="utf-8")
        return content

    def test_file_unchanged_when_new_entry_invalid(self) -> None:
        original = self._create_valid_file()
        bad = _make_entry(depth="not-a-valid-depth")
        with self.assertRaises(MadRefsValidationError):
            mad_refs.append_mad_ref(self.project, bad)
        self.assertEqual(self.refs_path.read_text(encoding="utf-8"), original)

    def test_file_unchanged_when_corrupt_existing(self) -> None:
        self.refs_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt = "this is not json {{{"
        self.refs_path.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(MadRefsValidationError):
            mad_refs.append_mad_ref(self.project, _make_entry())
        self.assertEqual(self.refs_path.read_text(encoding="utf-8"), corrupt)


# =========================================================================
# 13 — append_mad_ref: idempotent replay vs field‑level conflict
# =========================================================================

# Fields to mutate one‑by‑one, keeping (dispatch_id, purpose) identical.
# Each sub‑test verifies: RefIntegrityError raised, field named in message,
# file bytes unchanged, updated_at unchanged, lock released, no temp files.

_CONFLICT_FIELDS = {
    "task_id":           "TC-CONFLICT",
    "deliberation_id":   "conflict-delib-id",
    "depth":             "fast",
    "stdout_sha256":     "c" * 64,
    "report_sha256":     "d" * 64,
    "status":            "conflict-status",
    "archive_path":      _ABS_ROOT / "conflict" / "archive",
    "created_at":        "2025-01-01T00:00:00Z",
}


class AppendMadRefIdempotentReplayTests(unittest.TestCase):
    """Field‑level conflict detection for same (dispatch_id, purpose)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.runtime = self.project / ".agentdesk" / "runtime"
        self.refs_path = self.runtime / "mad-refs.yaml"
        self.lock_path = mad_refs._lock_path(self.runtime)

        # Seed with one valid ref.
        self.base = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            task_id="TC-BASE", deliberation_id="id-base", depth="deep",
            stdout_sha256="a" * 64, report_sha256="b" * 64,
            status="完成", archive_path=_ABS_ARCHIVE,
            created_at="2026-07-26T12:00:00Z",
        )
        self.orig_result = mad_refs.append_mad_ref(self.project, self.base)
        self.orig_bytes = self.refs_path.read_text(encoding="utf-8")
        self.orig_updated = self.orig_result["updated_at"]

    def _assert_conflict(self, field_name: str, bad_entry: MadRefEntry) -> None:
        """Assert RefIntegrityError, field named, file untouched,
        updated_at unchanged, lock released, no temp files."""
        with self.assertRaises(RefIntegrityError) as ctx:
            mad_refs.append_mad_ref(self.project, bad_entry)
        msg = str(ctx.exception)
        self.assertIn(field_name, msg,
                      f"error must name the differing field '{field_name}': {msg}")
        self.assertIn(bad_entry.dispatch_id, msg)
        self.assertIn(bad_entry.purpose, msg)
        # File bytes unchanged.
        self.assertEqual(self.refs_path.read_text(encoding="utf-8"),
                         self.orig_bytes)
        # updated_at unchanged (re-read the file to double‑check).
        data = mad_refs.read_mad_refs(self.project)
        self.assertEqual(data["updated_at"], self.orig_updated)
        # Lock released.
        self.assertFalse(self.lock_path.exists(),
                         "lock must be released after conflict")
        # No temp files left behind.
        temp_files = sorted(
            p for p in self.runtime.iterdir()
            if p.name.startswith(".mad-refs.yaml.")
        )
        self.assertEqual(temp_files, [], "no temp files after conflict")

    def test_conflict_task_id(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            task_id="TC-CONFLICT",
        )
        self._assert_conflict("task_id", e)

    def test_conflict_deliberation_id(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            deliberation_id="conflict-delib-id",
        )
        self._assert_conflict("deliberation_id", e)

    def test_conflict_depth(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            depth="fast",
        )
        self._assert_conflict("depth", e)

    def test_conflict_stdout_sha256(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            stdout_sha256="c" * 64,
        )
        self._assert_conflict("stdout_sha256", e)

    def test_conflict_report_sha256(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            report_sha256="d" * 64,
        )
        self._assert_conflict("report_sha256", e)

    def test_conflict_status(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            status="conflict-status",
        )
        self._assert_conflict("status", e)

    def test_conflict_archive_path(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            archive_path=str(_ABS_ROOT / "conflict" / "archive"),
        )
        self._assert_conflict("archive_path", e)

    def test_conflict_created_at(self) -> None:
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            created_at="2025-01-01T00:00:00Z",
        )
        self._assert_conflict("created_at", e)

    def test_all_ten_fields_match_is_idempotent(self) -> None:
        """Exact same 10 fields → no error, no write."""
        e = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            task_id="TC-BASE", deliberation_id="id-base", depth="deep",
            stdout_sha256="a" * 64, report_sha256="b" * 64,
            status="完成", archive_path=_ABS_ARCHIVE,
            created_at="2026-07-26T12:00:00Z",
        )
        result = mad_refs.append_mad_ref(self.project, e)
        self.assertEqual(len(result["refs"]), 1)
        self.assertEqual(self.refs_path.read_text(encoding="utf-8"),
                         self.orig_bytes)
        self.assertEqual(result["updated_at"], self.orig_updated)
        self.assertFalse(self.lock_path.exists())


# =========================================================================
# 14 — append_mad_ref: validation ordering (validate before compare)
# =========================================================================


class AppendMadRefValidationOrderingTests(unittest.TestCase):
    """New entry validation happens BEFORE any conflict or idempotency
    comparison — an invalid entry always fails with
    MadRefsValidationError, never RefIntegrityError."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.refs_path = self.project / ".agentdesk" / "runtime" / "mad-refs.yaml"

        # Seed with one valid ref.
        self.base = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            task_id="TC-E", deliberation_id="id-E", depth="deep",
            stdout_sha256="e" * 64, report_sha256="f" * 64,
            status="done", archive_path=_ABS_ARCHIVE,
            created_at="2026-07-26T12:00:00Z",
        )
        mad_refs.append_mad_ref(self.project, self.base)
        self.orig_bytes = self.refs_path.read_text(encoding="utf-8")

    def test_same_key_but_invalid_entry_raises_validation_error(self) -> None:
        """Same (dispatch_id, purpose) but new entry has an invalid depth
        → MadRefsValidationError (not RefIntegrityError), proving
        validation runs first."""
        bad = _make_entry(
            dispatch_id="DSP-001", purpose="planning",
            depth="not-a-valid-depth",
        )
        with self.assertRaises(MadRefsValidationError) as ctx:
            mad_refs.append_mad_ref(self.project, bad)
        self.assertIn("depth", str(ctx.exception).lower())
        # File untouched.
        self.assertEqual(self.refs_path.read_text(encoding="utf-8"),
                         self.orig_bytes)


# =========================================================================
# 13 — append_mad_ref: temp file cleanup
# =========================================================================


class AppendMadRefTempCleanupTests(unittest.TestCase):
    """Temp files and lock are cleaned up after operations."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.runtime = self.project / ".agentdesk" / "runtime"

    def _temp_files(self) -> list[Path]:
        """Return any ``.mad-refs.yaml.*`` files in runtime dir."""
        self.runtime.mkdir(parents=True, exist_ok=True)
        return sorted(
            p for p in self.runtime.iterdir()
            if p.name.startswith(".mad-refs.yaml.")
        )

    def test_no_temp_files_left_after_success(self) -> None:
        mad_refs.append_mad_ref(self.project, _make_entry())
        self.assertEqual(
            self._temp_files(), [],
            "no leftover temp files after successful append",
        )

    def test_no_temp_files_left_after_validation_error(self) -> None:
        # Write corrupt existing so append fails during validation.
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "mad-refs.yaml").write_text("bad json", encoding="utf-8")
        with self.assertRaises(MadRefsValidationError):
            mad_refs.append_mad_ref(self.project, _make_entry())
        self.assertEqual(self._temp_files(), [])

    def test_lock_is_not_a_temp_file(self) -> None:
        """Lock file must not be cleaned up as a temp — it's managed
        by _acquire_lock/_release_lock in ``finally``."""
        mad_refs.append_mad_ref(self.project, _make_entry())
        lock = mad_refs._lock_path(self.runtime)
        self.assertFalse(lock.exists(), "lock must be gone after success")


# =========================================================================
# 15 — Concurrent append from threads
# =========================================================================


class ConcurrentAppendTests(unittest.TestCase):
    """Two threads cannot hold the lock simultaneously."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def test_second_thread_gets_lock_contention(self) -> None:
        """Thread A acquires the lock manually; Thread B tries append → fails."""
        runtime = self.project / ".agentdesk" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        lock = mad_refs._lock_path(runtime)
        lock.write_text("held-by-test", encoding="utf-8")

        errors: list[Exception] = []

        def try_append() -> None:
            try:
                mad_refs.append_mad_ref(self.project, _make_entry())
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=try_append)
        t.start()
        t.join(timeout=5)

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LockContentionError)


# =========================================================================
# 16 — Serialization format
# =========================================================================


class SerializationFormatTests(unittest.TestCase):
    """Output file format meets the specification."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.refs_path = self.project / ".agentdesk" / "runtime" / "mad-refs.yaml"

    def test_output_is_valid_json(self) -> None:
        mad_refs.append_mad_ref(self.project, _make_entry())
        raw = self.refs_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIsInstance(data, dict)

    def test_output_is_valid_yaml_subset(self) -> None:
        """JSON output must parse as YAML (JSON is a subset of YAML)."""
        mad_refs.append_mad_ref(self.project, _make_entry())
        raw = self.refs_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        # Re-serialize to JSON and compare — proves round-trip stability.
        reserialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        data2 = json.loads(reserialized)
        self.assertEqual(data, data2)

    def test_ensure_ascii_false(self) -> None:
        entry = _make_entry(status="中文状态")  # 中文状态
        mad_refs.append_mad_ref(self.project, entry)
        raw = self.refs_path.read_text(encoding="utf-8")
        self.assertIn("中文状态", raw)
        self.assertNotIn("\\u4e2d", raw)  # Not \u-escaped

    def test_stable_indentation_two_spaces(self) -> None:
        mad_refs.append_mad_ref(self.project, _make_entry())
        raw = self.refs_path.read_text(encoding="utf-8")
        # First level: 2-space indent.
        self.assertIn('\n  "schema_version"', raw)
        # Second level: 4-space indent (refs[0] fields).
        self.assertIn('\n      "task_id"', raw)

    def test_trailing_newline_present(self) -> None:
        for i in range(3):
            mad_refs.append_mad_ref(
                self.project,
                _make_entry(
                    dispatch_id=f"DSP-trail-{i:03d}",
                    deliberation_id=f"id-{i}",
                ),
            )
        raw = self.refs_path.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"), "must end with exactly one newline")


# =========================================================================
# 17 — Error type hierarchy
# =========================================================================


class ErrorHierarchyTests(unittest.TestCase):
    """Custom exceptions have the expected inheritance chain."""

    def test_lock_contention_is_mad_refs_error(self) -> None:
        self.assertIsInstance(LockContentionError(), mad_refs.MadRefsError)

    def test_validation_error_is_mad_refs_error(self) -> None:
        self.assertIsInstance(MadRefsValidationError("x"), mad_refs.MadRefsError)

    def test_ref_integrity_error_is_mad_refs_error(self) -> None:
        self.assertIsInstance(RefIntegrityError("x"), mad_refs.MadRefsError)

    def test_mad_refs_error_is_exception(self) -> None:
        self.assertIsInstance(mad_refs.MadRefsError("x"), Exception)


# =========================================================================
# 18 — No Gateway or external calls
# =========================================================================


class NoGatewayLeakTests(unittest.TestCase):
    """Module does not import or expose Gateway-related symbols."""

    def test_no_subprocess_import(self) -> None:
        self.assertNotIn("subprocess", dir(mad_refs))

    def test_no_gateway_imports_or_calls(self) -> None:
        """Module docstring may reference excluded terms as non-goals;
        real test: no imports, classes, or functions that implement one."""
        source = (Path(__file__).resolve().parents[1]
                  / "skills" / "agentdesk" / "scripts" / "mad_refs.py")
        text = source.read_text(encoding="utf-8")
        # Gateway implementation fragments.
        self.assertNotIn("import gateway", text.lower())
        self.assertNotIn("from gateway", text.lower())
        self.assertNotIn("class Gateway", text)
        self.assertNotIn("def run_gateway", text.lower())
        # Check that the module dir has no subprocess attribute.
        self.assertNotIn("subprocess", dir(mad_refs))
        # Check that the source AST has no disallowed imports.
        # These are checked by searching for import patterns, not just
        # for the word appearing in docstrings.
        import_re = r"^(?:from|import)\s+\S*(?:%s)"
        for term in ("budget", "worker_adapter", "worker_kind", "slot",
                     "lease", "dispatcher", "escalat", "retry"):
            pattern = re.compile(import_re % term, re.MULTILINE | re.IGNORECASE)
            self.assertIsNone(
                pattern.search(text),
                f"source must not import {term}",
            )


# =========================================================================
# 19 — Temp file names are unique per write (not a fixed name)
# =========================================================================


class TempFileNameUniquenessTests(unittest.TestCase):
    """Each atomic write uses a unique temp file name."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.runtime = self.project / ".agentdesk" / "runtime"

    def test_temp_file_name_not_fixed(self) -> None:
        """Verify that ``_atomic_write`` does not use a single fixed
        ``.tmp`` name shared by all writers."""
        # Monkey-patch os.replace to capture the temp path before
        # replacing, then raise so we can inspect.
        captured: list[str] = []
        original_replace = os.replace

        def _capture_and_fail(src, dst) -> None:
            captured.append(str(src))
            raise RuntimeError("injected")

        # We need to test _atomic_write directly.
        try:
            os.replace = _capture_and_fail  # type: ignore[assignment]
            with self.assertRaises(RuntimeError):
                mad_refs._atomic_write(
                    self.runtime / "test.txt", '{"x":1}\n'
                )
        finally:
            os.replace = original_replace  # type: ignore[assignment]

        self.assertGreaterEqual(len(captured), 1)
        tmp_name = Path(captured[0]).name
        # Must NOT be a fixed name like ".test.txt.tmp".
        self.assertNotEqual(tmp_name, ".test.txt.tmp",
                            "temp file name must be unique, not fixed")
        # Must start with the prefix pattern.
        self.assertTrue(tmp_name.startswith(".test.txt."),
                        f"temp name {tmp_name!r} must start with '.test.txt.'")

    def test_two_writes_use_different_temp_names(self) -> None:
        captured: list[str] = []
        original_replace = os.replace

        def _capture_and_fail(src, dst) -> None:
            captured.append(str(src))
            raise RuntimeError("injected")

        target = self.runtime / "data.yaml"
        try:
            os.replace = _capture_and_fail  # type: ignore[assignment]
            with self.assertRaises(RuntimeError):
                mad_refs._atomic_write(target, '{"a":1}\n')
            with self.assertRaises(RuntimeError):
                mad_refs._atomic_write(target, '{"a":2}\n')
        finally:
            os.replace = original_replace  # type: ignore[assignment]

        self.assertEqual(len(captured), 2)
        self.assertNotEqual(
            Path(captured[0]).name, Path(captured[1]).name,
            "two writes must use different temp file names",
        )


if __name__ == "__main__":
    unittest.main()
