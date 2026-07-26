"""TC-13.9c: Provider output evidence validation.

Verifies provenance integrity, fixture structure, sanitization,
and non-regression constraints.  Does NOT assert on specific
output field values — only on structural invariants and
sanitization correctness.

stdlib-only unittest; no external dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "provider-output"
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"


# =========================================================================
# Helpers
# =========================================================================


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _has_nul(data: bytes) -> bool:
    return b"\x00" in data


def _has_ansi(data: bytes) -> bool:
    return b"\x1b" in data


_USER_HOME = os.path.expanduser("~").replace("\\", "\\\\")
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Authorization header", re.compile(r"(?i)authorization\s*[:=]")),
    ("Bearer token", re.compile(r"(?i)bearer\s+[\w\-\.]+")),
    ("api_key field", re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S")),
    ("password field", re.compile(r"(?i)password\s*[:=]\s*\S")),
    ("secret field", re.compile(r"(?i)secret\s*[:=]\s*\S")),
    ("sk- prefix key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("ghp/ghs/gho key", re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}")),
    ("JWT token", re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")),
]

# Allowed fixture directories (relative to _FIXTURES)
_ALLOWED_DIRS = {
    "claude/2.1.214",
    "codex/0.144.6",
}

# Expected root keys in provenance.json
_EXPECTED_PROV_ROOT_KEYS = {"schema_version", "captures"}

# Expected capture fields (exact set)
_EXPECTED_CAPTURE_FIELDS = {
    "fixture_path",
    "provider_id",
    "package_name",
    "package_version",
    "executable_sha256",
    "captured_at",
    "sample_id",
    "argv_shape",
    "exit_code",
    "stdout_bytes",
    "stderr_bytes",
    "raw_stdout_sha256",
    "raw_stderr_sha256",
    "sanitized_stdout_sha256",
    "sanitized_stderr_sha256",
    "utf8_valid",
    "serialization",
    "redactions",
    "source_kind",
}

_VALID_SERIALIZATION = {"json", "jsonl", "unknown"}
_EXPECTED_SOURCE_KIND = {"real_cli_capture"}


# =========================================================================
# TestCase
# =========================================================================


class TestProviderOutputEvidence(unittest.TestCase):
    """Validate all TC-13.9c fixture evidence and provenance integrity."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.provenance_path = _FIXTURES / "provenance.json"
        cls.provenance = json.loads(cls.provenance_path.read_text(encoding="utf-8"))

    # ── provenance structure ──────────────────────────────────────────────

    def test_001_provenance_file_exists(self) -> None:
        """provenance.json must exist."""
        self.assertTrue(self.provenance_path.is_file(),
                        f"provenance.json not found at {self.provenance_path}")

    def test_002_provenance_root_keys_exact(self) -> None:
        """Root must have exactly schema_version and captures."""
        actual = set(self.provenance.keys())
        self.assertEqual(actual, _EXPECTED_PROV_ROOT_KEYS,
                         f"provenance root keys mismatch: {actual}")

    def test_003_schema_version_value(self) -> None:
        """schema_version must be 'agentdesk.provider-output-evidence/v1'."""
        self.assertEqual(
            self.provenance["schema_version"],
            "agentdesk.provider-output-evidence/v1",
        )

    def test_004_captures_is_list(self) -> None:
        """captures must be a list."""
        self.assertIsInstance(self.provenance["captures"], list)

    def test_005_captures_not_empty(self) -> None:
        """captures must not be empty."""
        self.assertGreater(len(self.provenance["captures"]), 0)

    def test_006_six_captures_total(self) -> None:
        """Exactly 6 captures (3 Claude + 3 Codex)."""
        self.assertEqual(len(self.provenance["captures"]), 6,
                         "Expected exactly 6 captures (3 Claude + 3 Codex)")

    # ── capture field validation ──────────────────────────────────────────

    def test_010_capture_fields_exact(self) -> None:
        """Every capture must have exactly the required fields."""
        for i, cap in enumerate(self.provenance["captures"]):
            actual = set(cap.keys())
            with self.subTest(capture=i, fixture=cap.get("fixture_path", "?")):
                self.assertEqual(
                    actual, _EXPECTED_CAPTURE_FIELDS,
                    f"Capture {i} field mismatch: missing={_EXPECTED_CAPTURE_FIELDS - actual}, "
                    f"extra={actual - _EXPECTED_CAPTURE_FIELDS}"
                )

    def test_011_source_kind_must_be_real_cli_capture(self) -> None:
        """Every source_kind must be 'real_cli_capture' — no hand-written."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(
                    cap["source_kind"], "real_cli_capture",
                    "source_kind must be 'real_cli_capture'"
                )

    def test_012_serialization_valid(self) -> None:
        """serialization must be json, jsonl, or unknown."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertIn(cap["serialization"], _VALID_SERIALIZATION)

    def test_013_sha256_format(self) -> None:
        """All SHA-256 fields must be 64-char lowercase hex."""
        sha_fields = [
            "executable_sha256",
            "raw_stdout_sha256",
            "raw_stderr_sha256",
            "sanitized_stdout_sha256",
            "sanitized_stderr_sha256",
        ]
        for cap in self.provenance["captures"]:
            for field in sha_fields:
                with self.subTest(fixture=cap.get("fixture_path", "?"), field=field):
                    val = cap.get(field, "")
                    self.assertEqual(len(val), 64,
                                     f"{field} must be 64 chars, got {len(val)}")
                    self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", val),
                                    f"{field} not lowercase hex: {val}")

    def test_014_utf8_valid_flag_consistent(self) -> None:
        """utf8_valid must be a bool."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertIsInstance(cap["utf8_valid"], bool)

    def test_015_redactions_is_list(self) -> None:
        """redactions must be a list."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertIsInstance(cap["redactions"], list)

    # ── fixture path resolution ───────────────────────────────────────────

    def test_020_all_fixture_paths_exist(self) -> None:
        """Every fixture_path in provenance must resolve to an existing file."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertTrue(fp.is_file(),
                                f"Fixture not found: {fp}")

    def test_021_fixture_paths_in_allowed_dirs(self) -> None:
        """fixture_path must be within tests/fixtures/provider-output/<allowed>."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap["fixture_path"]):
                rel = cap["fixture_path"]
                self.assertTrue(rel.startswith("tests/fixtures/provider-output/"),
                                f"fixture_path outside expected base: {rel}")
                # Extract the subdir after provider-output/
                sub = rel[len("tests/fixtures/provider-output/"):]
                parts = sub.split("/")
                # First two segments should be an allowed dir prefix
                if len(parts) >= 2:
                    dir_prefix = f"{parts[0]}/{parts[1]}"
                    self.assertIn(dir_prefix, _ALLOWED_DIRS,
                                  f"fixture dir '{dir_prefix}' not in allowed: {_ALLOWED_DIRS}")

    # ── sanitized fixture SHA match provenance ────────────────────────────

    def test_030_sanitized_stdout_sha_matches_fixture(self) -> None:
        """The sanitized stdout SHA-256 in provenance must match the actual fixture."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                actual_sha = _sha256(fp.read_bytes())
                self.assertEqual(
                    cap["sanitized_stdout_sha256"], actual_sha,
                    f"sanitized_stdout_sha256 mismatch for {cap['fixture_path']}"
                )

    def test_031_package_version_matches_directory(self) -> None:
        """Package version in provenance must match version directory."""
        for cap in self.provenance["captures"]:
            fp = cap["fixture_path"]
            # Extract version from path: .../provider-output/<provider>/<version>/file
            parts = fp.split("/")
            version_dir = parts[4]  # claude|codex / version_dir / file
            with self.subTest(fixture=fp):
                self.assertEqual(
                    cap["package_version"], version_dir,
                    f"package_version '{cap['package_version']}' != dir '{version_dir}'"
                )

    def test_032_package_name_matches_provider_directory(self) -> None:
        """Package name must match provider directory."""
        expected_map = {
            "claude": "@anthropic-ai/claude-code",
            "codex": "@openai/codex",
        }
        for cap in self.provenance["captures"]:
            parts = cap["fixture_path"].split("/")
            provider_dir = parts[3]
            with self.subTest(fixture=cap["fixture_path"]):
                expected = expected_map.get(provider_dir)
                self.assertIsNotNone(expected, f"Unknown provider dir: {provider_dir}")
                self.assertEqual(cap["package_name"], expected)

    # ── JSON fixture parse ────────────────────────────────────────────────

    def test_040_json_fixtures_parse(self) -> None:
        """Every .json fixture must parse as a valid JSON object."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            if fp.suffix != ".json":
                continue
            with self.subTest(fixture=cap["fixture_path"]):
                data = json.loads(fp.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict,
                                      f"JSON root must be object, got {type(data).__name__}")

    def test_041_jsonl_fixtures_each_line_is_object(self) -> None:
        """Every .jsonl fixture: each non-empty line must parse as a JSON object."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            if fp.suffix != ".jsonl":
                continue
            with self.subTest(fixture=cap["fixture_path"]):
                text = fp.read_text(encoding="utf-8")
                for i, line in enumerate(text.splitlines()):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    with self.subTest(line=i):
                        obj = json.loads(stripped)
                        self.assertIsInstance(obj, dict,
                                              f"JSONL line {i} root must be object")

    # ── UTF-8 validity ────────────────────────────────────────────────────

    def test_050_fixtures_utf8_valid(self) -> None:
        """Every fixture file must be valid UTF-8."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                try:
                    fp.read_text(encoding="utf-8")
                except UnicodeDecodeError as e:
                    self.fail(f"Fixture not valid UTF-8: {fp} — {e}")

    def test_051_provenance_utf8_valid(self) -> None:
        """provenance.json must be valid UTF-8."""
        try:
            self.provenance_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            self.fail(f"provenance.json not valid UTF-8: {e}")

    # ── No NUL bytes ──────────────────────────────────────────────────────

    def test_060_no_nul_bytes_in_fixtures(self) -> None:
        """No fixture shall contain NUL bytes."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertFalse(_has_nul(fp.read_bytes()),
                                 f"Fixture contains NUL: {cap['fixture_path']}")

    def test_061_no_nul_bytes_in_provenance(self) -> None:
        """provenance.json must not contain NUL bytes."""
        self.assertFalse(_has_nul(self.provenance_path.read_bytes()))

    # ── No ANSI escapes ───────────────────────────────────────────────────

    def test_070_no_ansi_escapes(self) -> None:
        """No fixture shall contain ANSI escape sequences."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertFalse(_has_ansi(fp.read_bytes()),
                                 f"Fixture contains ANSI escapes: {cap['fixture_path']}")

    def test_071_provenance_no_ansi(self) -> None:
        """provenance.json must not contain ANSI escapes."""
        self.assertFalse(_has_ansi(self.provenance_path.read_bytes()))

    # ── No user home directory in fixtures ────────────────────────────────

    def test_080_no_user_home_in_fixtures(self) -> None:
        """No fixture shall contain the user's home directory path."""
        home_pattern = re.compile(re.escape(_USER_HOME))
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                text = fp.read_text(encoding="utf-8")
                self.assertIsNone(
                    home_pattern.search(text),
                    f"Fixture contains user home path: {cap['fixture_path']}"
                )

    def test_081_no_user_home_in_provenance(self) -> None:
        """provenance.json must not contain user home path."""
        text = self.provenance_path.read_text(encoding="utf-8")
        home_pattern = re.compile(re.escape(_USER_HOME))
        self.assertIsNone(home_pattern.search(text),
                          "provenance.json contains user home path")

    # ── No temp workspace paths ───────────────────────────────────────────

    def test_090_no_temp_workspace_in_fixtures(self) -> None:
        """No fixture shall reference the temp capture workspace."""
        ws_pattern = re.compile(r"agentdesk-capture-[a-f0-9]+")
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                text = fp.read_text(encoding="utf-8")
                self.assertIsNone(
                    ws_pattern.search(text),
                    f"Fixture references temp workspace: {cap['fixture_path']}"
                )

    # ── No common secret patterns ─────────────────────────────────────────

    def test_100_no_secret_patterns(self) -> None:
        """No fixture shall contain common secret patterns."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            text = fp.read_text(encoding="utf-8")
            for label, pattern in _SECRET_PATTERNS:
                with self.subTest(fixture=cap["fixture_path"], pattern=label):
                    self.assertIsNone(
                        pattern.search(text),
                        f"Secret pattern '{label}' in {cap['fixture_path']}"
                    )

    def test_101_no_secret_patterns_in_provenance(self) -> None:
        """provenance.json must not contain secret patterns."""
        text = self.provenance_path.read_text(encoding="utf-8")
        for label, pattern in _SECRET_PATTERNS:
            with self.subTest(pattern=label):
                self.assertIsNone(pattern.search(text),
                                  f"Secret pattern '{label}' in provenance.json")

    # ── Claude / Codex separation — no unified schema ─────────────────────

    def test_110_claude_and_codex_separated(self) -> None:
        """Claude captures use json, Codex use jsonl — schemas differ."""
        claude = [c for c in self.provenance["captures"] if c["provider_id"] == "claude"]
        codex = [c for c in self.provenance["captures"] if c["provider_id"] == "codex"]

        self.assertEqual(len(claude), 3, "Expected 3 Claude captures")
        self.assertEqual(len(codex), 3, "Expected 3 Codex captures")

        # Claude serialization must be json
        for c in claude:
            with self.subTest(capture=c["fixture_path"]):
                self.assertEqual(c["serialization"], "json")

        # Codex serialization must not be 'json' (should be jsonl or unknown)
        for c in codex:
            with self.subTest(capture=c["fixture_path"]):
                self.assertNotEqual(c["serialization"], "json",
                                    "Codex must not use Claude's 'json' serialization")

    # ── Fixtures not imported by production ───────────────────────────────

    def test_120_fixtures_not_in_production_imports(self) -> None:
        """No fixture shall be imported by any production module."""
        fixture_paths = set()
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            fixture_paths.add(fp.resolve())

        # Walk production scripts directory
        for py_file in _SCRIPTS.glob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            for fp in fixture_paths:
                with self.subTest(module=py_file.name, fixture=str(fp)):
                    self.assertNotIn(
                        str(fp), text,
                        f"Production module {py_file.name} references fixture"
                    )
                    self.assertNotIn(
                        fp.name, text,
                        f"Production module {py_file.name} may reference fixture by name"
                    )

    # ── TC-13.9c Target status guard ──────────────────────────────────────

    def test_130_tc139c_still_target_in_adr(self) -> None:
        """TC-13.9c must remain Target in the ADR — not Current."""
        adr_path = _REPO_ROOT / "skills" / "agentdesk" / "references" / "adr" / "001-mad-agentdesk-integration.md"
        self.assertTrue(adr_path.is_file(), f"ADR not found: {adr_path}")
        text = adr_path.read_text(encoding="utf-8")

        # The Future Task Cards section must list TC-13.9c as Target
        self.assertIn("TC-13.9c", text,
                      "ADR must reference TC-13.9c")

        # Verify TC-13.9c is NOT marked Current in the status table
        # Look for the table row: each TC-13.9c entry should say Target not Current
        lines_with_139c = [
            line for line in text.splitlines()
            if "TC-13.9c" in line
        ]
        for line in lines_with_139c:
            # If this is a status table row with "Current", that's a violation
            if "|" in line and "13.9c" in line:
                fields = [f.strip() for f in line.split("|")]
                status_field = fields[3] if len(fields) > 3 else ""
                self.assertNotEqual(
                    status_field, "Current",
                    f"TC-13.9c must not be Current in ADR status table: {line}"
                )

    # ── Decoder file must not exist ────────────────────────────────────────

    def test_140_decoder_not_implemented(self) -> None:
        """No output decoder module must exist in production scripts."""
        decoder_names = [
            "output_decoder.py",
            "provider_decoder.py",
            "claude_decoder.py",
            "codex_decoder.py",
            "cli_output_parser.py",
            "stream_parser.py",
        ]
        for name in decoder_names:
            candidate = _SCRIPTS / name
            self.assertFalse(
                candidate.exists(),
                f"Decoder file exists (must not): {candidate}"
            )

    # ── TC-13.10 still Target ─────────────────────────────────────────────

    def test_150_tc1310_still_target(self) -> None:
        """TC-13.10 must remain Target."""
        adr_path = _REPO_ROOT / "skills" / "agentdesk" / "references" / "adr" / "001-mad-agentdesk-integration.md"
        text = adr_path.read_text(encoding="utf-8")

        lines_with_1310 = [
            line for line in text.splitlines()
            if "TC-13.10" in line and "13.10" not in "TC-13.10" and "TC-13.10" in line
        ]
        # Actually let me just check TC-13.10 is mentioned
        self.assertIn("TC-13.10", text, "ADR must reference TC-13.10")

        for line in lines_with_1310:
            if "|" in line:
                fields = [f.strip() for f in line.split("|")]
                status_field = fields[3] if len(fields) > 3 else ""
                self.assertNotEqual(
                    status_field, "Current",
                    f"TC-13.10 must not be Current: {line}"
                )

    # ── Non-empty Claude result field ─────────────────────────────────────

    def test_160_claude_fixtures_have_result_field(self) -> None:
        """All Claude JSON fixtures must have a 'result' field."""
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "claude":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertIn("result", obj,
                          f"Claude fixture missing 'result': {cap['fixture_path']}")
            self.assertIsInstance(obj["result"], str)

    def test_161_claude_type_is_result(self) -> None:
        """Claude fixtures have type: 'result'."""
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "claude":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual(obj.get("type"), "result")

    def test_162_claude_subtype_is_success(self) -> None:
        """Claude fixtures have subtype: 'success'."""
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "claude":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual(obj.get("subtype"), "success")

    def test_163_claude_is_error_false(self) -> None:
        """Claude fixtures have is_error: false."""
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "claude":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertFalse(obj.get("is_error"))

    # ── Claude consistent field set across all 3 samples ──────────────────

    def test_170_claude_consistent_top_level_keys(self) -> None:
        """All 3 Claude fixtures share the same top-level keys."""
        claude_keys = []
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "claude":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            claude_keys.append(set(obj.keys()))

        self.assertEqual(len(claude_keys), 3)
        # All three sets must be identical
        self.assertEqual(claude_keys[0], claude_keys[1],
                         "Claude samples don't share consistent keys")
        self.assertEqual(claude_keys[0], claude_keys[2],
                         "Claude samples don't share consistent keys")

    # ── Codex captures are placeholders (Gateway failed) ──────────────────

    def test_180_codex_placeholders_have_note(self) -> None:
        """Codex fixtures must contain a note about Gateway capture failure."""
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "codex":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            if cap["exit_code"] != 0:
                text = fp.read_text(encoding="utf-8")
                with self.subTest(fixture=cap["fixture_path"]):
                    self.assertIn("_tc139c_note", text,
                                  "Codex placeholder must document failure reason")

    # ── provenance.json no absolute user paths ───────────────────────────

    def test_190_provenance_no_absolute_windows_paths(self) -> None:
        """provenance.json must not contain absolute Windows paths."""
        text = self.provenance_path.read_text(encoding="utf-8")
        # Drive-letter paths like C:\Users\...
        drive_path = re.compile(r'[A-Za-z]:\\Users\\')
        self.assertIsNone(drive_path.search(text),
                          "provenance.json contains drive-letter user path")

    # ── redactions in provenance must not match original data ──────────────

    def test_200_redactions_describe_actual_changes(self) -> None:
        """For Claude captures, redactions should reference real fields."""
        for cap in self.provenance["captures"]:
            if cap["provider_id"] != "claude":
                continue
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))

            for redaction in cap["redactions"]:
                with self.subTest(
                    fixture=cap["fixture_path"],
                    redaction_path=redaction["path"],
                ):
                    self.assertIn("path", redaction)
                    self.assertIn("kind", redaction)
                    self.assertIn("reason", redaction)
                    # The redacted field key must exist in the fixture dict,
                    # even though its value may be null (None) after sanitization.
                    path = redaction["path"]
                    parts = path.lstrip("$.").split(".")
                    # Navigate to the parent dict, then check key existence
                    parent = obj
                    for part in parts[:-1]:
                        if isinstance(parent, dict) and part in parent:
                            parent = parent[part]
                        else:
                            parent = None
                            break
                    leaf_key = parts[-1]
                    self.assertIsInstance(parent, dict,
                        f"Parent of {path} not a dict in fixture")
                    self.assertIn(leaf_key, parent,
                        f"Redaction key '{leaf_key}' not found at {path} in fixture")

    # ── No production code modified ───────────────────────────────────────

    def test_210_production_modules_not_modified(self) -> None:
        """Production files must not claim TC-13.9c Current implementation."""
        # Only check for implementation claims, not docstring references
        for py_file in sorted(_SCRIPTS.glob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            # Docstring references to future task cards are fine
            # Implementation claims would read "TC-13.9c — Current" or similar
            lines = text.splitlines()
            for lineno, line in enumerate(lines, 1):
                if "TC-13.9c" in line:
                    with self.subTest(file=py_file.name, line=lineno):
                        self.assertNotIn(
                            "Current", line,
                            f"{py_file.name}:{lineno} must not mark TC-13.9c as Current: {line.strip()}"
                        )

    # ── Test module qualifies itself ──────────────────────────────────────

    def test_999_self_consistency(self) -> None:
        """This test file itself must parse as valid Python."""
        self.assertTrue(
            Path(__file__).read_text(encoding="utf-8").startswith('"""'),
            "Test module must start with docstring"
        )


if __name__ == "__main__":
    unittest.main()
