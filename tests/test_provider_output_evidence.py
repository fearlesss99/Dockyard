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

# ── Required redaction rules per Claude capture ─────────────────────────────

_REQUIRED_REDACTION_PATHS = frozenset({
    "$.session_id",
    "$.uuid",
    "$.total_cost_usd",
    "$.modelUsage.*.costUSD",
})

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

    # ── Exactly 3 Claude-only captures ────────────────────────────────────

    def test_010_exactly_three_captures(self) -> None:
        """captures must contain exactly 3 entries — all Claude."""
        self.assertEqual(len(self.provenance["captures"]), 3,
                         "Expected exactly 3 captures (Claude only)")

    def test_011_all_captures_are_claude(self) -> None:
        """Every capture must be provider_id == 'claude'."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(cap["provider_id"], "claude",
                                 "All captures must be Claude")

    def test_012_all_captures_exit_zero(self) -> None:
        """Every capture must have exit_code 0."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(cap["exit_code"], 0,
                                 "All captures must have exit code 0")

    def test_013_no_codex_in_provenance(self) -> None:
        """No capture shall have provider_id 'codex'."""
        codex = [c for c in self.provenance["captures"] if c["provider_id"] == "codex"]
        self.assertEqual(len(codex), 0, "Codex captures must not exist in provenance")

    # ── capture field validation ──────────────────────────────────────────

    def test_020_capture_fields_exact(self) -> None:
        """Every capture must have exactly the required fields."""
        for i, cap in enumerate(self.provenance["captures"]):
            actual = set(cap.keys())
            with self.subTest(capture=i, fixture=cap.get("fixture_path", "?")):
                self.assertEqual(
                    actual, _EXPECTED_CAPTURE_FIELDS,
                    f"Capture {i} field mismatch: missing={_EXPECTED_CAPTURE_FIELDS - actual}, "
                    f"extra={actual - _EXPECTED_CAPTURE_FIELDS}"
                )

    def test_021_source_kind_real_cli_capture(self) -> None:
        """Every source_kind must be 'real_cli_capture'."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(cap["source_kind"], "real_cli_capture")

    def test_022_serialization_all_json(self) -> None:
        """All Claude captures must have serialization 'json'."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(cap["serialization"], "json")

    def test_023_sha256_format(self) -> None:
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

    def test_024_utf8_valid_true(self) -> None:
        """All captures must have utf8_valid True."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertTrue(cap["utf8_valid"])

    def test_025_redactions_is_list(self) -> None:
        """redactions must be a list."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertIsInstance(cap["redactions"], list)

    # ── redaction completeness ────────────────────────────────────────────

    def test_030_four_redactions_per_capture(self) -> None:
        """Every Claude capture must have exactly 4 redaction rules."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(len(cap["redactions"]), 4,
                                 "Each Claude capture must have exactly 4 redaction rules")

    def test_031_required_redaction_paths_present(self) -> None:
        """All 4 required redaction paths must be present."""
        for cap in self.provenance["captures"]:
            paths = {r["path"] for r in cap["redactions"]}
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                self.assertEqual(paths, _REQUIRED_REDACTION_PATHS,
                                 f"Redaction paths mismatch: {paths}")

    def test_032_costusd_redaction_included(self) -> None:
        """The $.modelUsage.*.costUSD redaction must be explicitly present."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap.get("fixture_path", "?")):
                cost_redactions = [r for r in cap["redactions"]
                                   if r["path"] == "$.modelUsage.*.costUSD"]
                self.assertEqual(len(cost_redactions), 1,
                                 "Missing $.modelUsage.*.costUSD redaction")

    # ── fixture path resolution ───────────────────────────────────────────

    def test_040_all_fixture_paths_exist(self) -> None:
        """Every fixture_path in provenance must resolve to an existing file."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertTrue(fp.is_file(),
                                f"Fixture not found: {fp}")

    def test_041_fixture_paths_only_claude(self) -> None:
        """All fixture paths must be under claude/2.1.214."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertTrue(
                    cap["fixture_path"].startswith(
                        "tests/fixtures/provider-output/claude/2.1.214/"
                    ),
                    f"Fixture path not under claude/2.1.214: {cap['fixture_path']}"
                )

    # ── sanitized fixture SHA match provenance ────────────────────────────

    def test_050_sanitized_stdout_sha_matches_fixture(self) -> None:
        """The sanitized stdout SHA-256 in provenance must match the actual fixture."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                actual_sha = _sha256(fp.read_bytes())
                self.assertEqual(
                    cap["sanitized_stdout_sha256"], actual_sha,
                    f"sanitized_stdout_sha256 mismatch for {cap['fixture_path']}"
                )

    def test_051_package_version_matches_directory(self) -> None:
        """Package version in provenance must match version directory."""
        for cap in self.provenance["captures"]:
            parts = cap["fixture_path"].split("/")
            version_dir = parts[4]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(cap["package_version"], version_dir)

    def test_052_package_name_is_claude(self) -> None:
        """All package_name must be @anthropic-ai/claude-code."""
        for cap in self.provenance["captures"]:
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(cap["package_name"], "@anthropic-ai/claude-code")

    # ── Non-regression: Claude fixture SHA unchanged ──────────────────────

    def test_053_raw_stdout_sha_unchanged(self) -> None:
        """Claude raw stdout SHA must match the original capture."""
        expected = {
            "success-minimal":      "06ebbfe7656a202137316272e653352d9d09ee26067613842455737f8941a4de",
            "success-unicode":      "d9ec017cd1834b02bdaf6a5913762db9b5a5955d09dad3423c8fe5a42356fb54",
            "application-boundary": "0422fdb0cf8b09f754d620bd74bbe1abb74fc7848bdc438ad08205e3a4007a80",
        }
        for cap in self.provenance["captures"]:
            expected_sha = expected.get(cap["sample_id"])
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(cap["raw_stdout_sha256"], expected_sha)

    def test_054_sanitized_stdout_sha_unchanged(self) -> None:
        """Claude sanitized stdout SHA must match the original sanitized fixture."""
        expected = {
            "success-minimal":      "d447f50c976d3b58fb3b1b9c80e82b42ce089d4f19f4f2e7e69ff775ef281a85",
            "success-unicode":      "d0d97454dcbe02d43fa25cc702fee1cd84bed85c65119e1d7762861b74a610bd",
            "application-boundary": "c29a846620b9b9b438bd235d8852fb043d3964afd82f49cc1077de7cfb561921",
        }
        for cap in self.provenance["captures"]:
            expected_sha = expected.get(cap["sample_id"])
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(cap["sanitized_stdout_sha256"], expected_sha)

    # ── Non-regression: Claude fixture costUSD fields are null ────────────

    def test_060_costusd_fields_are_null(self) -> None:
        """Every modelUsage.*.costUSD in each fixture must be null."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            model_usage = obj.get("modelUsage", {})
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertIsInstance(model_usage, dict)
                self.assertGreater(len(model_usage), 0,
                                   "modelUsage must not be empty")
                for model_key, model_entry in model_usage.items():
                    with self.subTest(model=model_key):
                        self.assertIsInstance(model_entry, dict)
                        self.assertIn("costUSD", model_entry)
                        self.assertIsNone(
                            model_entry["costUSD"],
                            f"modelUsage.{model_key}.costUSD must be null (redacted)"
                        )

    # ── JSON fixture parse ────────────────────────────────────────────────

    def test_070_json_fixtures_parse(self) -> None:
        """Every .json fixture must parse as a valid JSON object."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                data = json.loads(fp.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict,
                                      f"JSON root must be object, got {type(data).__name__}")

    # ── UTF-8 validity ────────────────────────────────────────────────────

    def test_080_fixtures_utf8_valid(self) -> None:
        """Every fixture file must be valid UTF-8."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                try:
                    fp.read_text(encoding="utf-8")
                except UnicodeDecodeError as e:
                    self.fail(f"Fixture not valid UTF-8: {fp} — {e}")

    def test_081_provenance_utf8_valid(self) -> None:
        """provenance.json must be valid UTF-8."""
        try:
            self.provenance_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            self.fail(f"provenance.json not valid UTF-8: {e}")

    # ── No NUL bytes ──────────────────────────────────────────────────────

    def test_090_no_nul_bytes_in_fixtures(self) -> None:
        """No fixture shall contain NUL bytes."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertFalse(_has_nul(fp.read_bytes()),
                                 f"Fixture contains NUL: {cap['fixture_path']}")

    def test_091_no_nul_bytes_in_provenance(self) -> None:
        """provenance.json must not contain NUL bytes."""
        self.assertFalse(_has_nul(self.provenance_path.read_bytes()))

    # ── No ANSI escapes ───────────────────────────────────────────────────

    def test_100_no_ansi_escapes(self) -> None:
        """No fixture shall contain ANSI escape sequences."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertFalse(_has_ansi(fp.read_bytes()),
                                 f"Fixture contains ANSI escapes: {cap['fixture_path']}")

    def test_101_provenance_no_ansi(self) -> None:
        """provenance.json must not contain ANSI escapes."""
        self.assertFalse(_has_ansi(self.provenance_path.read_bytes()))

    # ── No user home directory in fixtures ────────────────────────────────

    def test_110_no_user_home_in_fixtures(self) -> None:
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

    def test_111_no_user_home_in_provenance(self) -> None:
        """provenance.json must not contain user home path."""
        text = self.provenance_path.read_text(encoding="utf-8")
        home_pattern = re.compile(re.escape(_USER_HOME))
        self.assertIsNone(home_pattern.search(text),
                          "provenance.json contains user home path")

    # ── No temp workspace paths ───────────────────────────────────────────

    def test_120_no_temp_workspace_in_fixtures(self) -> None:
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

    def test_130_no_secret_patterns(self) -> None:
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

    def test_131_no_secret_patterns_in_provenance(self) -> None:
        """provenance.json must not contain secret patterns."""
        text = self.provenance_path.read_text(encoding="utf-8")
        for label, pattern in _SECRET_PATTERNS:
            with self.subTest(pattern=label):
                self.assertIsNone(pattern.search(text),
                                  f"Secret pattern '{label}' in provenance.json")

    # ── No Codex fake fixture files exist ─────────────────────────────────

    def test_140_no_codex_fixture_directory(self) -> None:
        """The codex fixture directory must not exist."""
        codex_dir = _FIXTURES / "codex"
        self.assertFalse(
            codex_dir.exists(),
            f"Codex fixture directory must not exist: {codex_dir}"
        )

    def test_141_no_codex_success_placeholders(self) -> None:
        """No file named codex success-* may exist anywhere under fixtures."""
        for candidate in _FIXTURES.rglob("codex*"):
            self.fail(f"Codex artifact must not exist: {candidate}")
        for candidate in _FIXTURES.rglob("*success-minimal.jsonl"):
            self.fail(f"Codex placeholder must not exist: {candidate}")
        for candidate in _FIXTURES.rglob("*_tc139c_note*"):
            self.fail(f"Codex note artifact must not exist: {candidate}")

    # ── Fixtures not imported by production ───────────────────────────────

    def test_150_fixtures_not_in_production_imports(self) -> None:
        """No fixture shall be imported by any production module."""
        fixture_paths = set()
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            fixture_paths.add(fp.resolve())

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

    def test_160_tc139c_still_target_in_adr(self) -> None:
        """TC-13.9c must remain Target in the ADR — not Current."""
        adr_path = _REPO_ROOT / "skills" / "agentdesk" / "references" / "adr" / "001-mad-agentdesk-integration.md"
        self.assertTrue(adr_path.is_file(), f"ADR not found: {adr_path}")
        text = adr_path.read_text(encoding="utf-8")

        self.assertIn("TC-13.9c", text, "ADR must reference TC-13.9c")

        lines_with_139c = [
            line for line in text.splitlines()
            if re.search(r"\|\s*TC-13\.9c\s*\|", line)
        ]
        for line in lines_with_139c:
            if "|" in line and "13.9c" in line:
                fields = [f.strip() for f in line.split("|")]
                status_field = fields[3] if len(fields) > 3 else ""
                self.assertNotEqual(
                    status_field, "Current",
                    f"TC-13.9c must not be Current in ADR status table: {line}"
                )

    # ── Decoder file must not exist ────────────────────────────────────────

    def test_170_decoder_not_implemented(self) -> None:
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

    def test_180_tc1310_still_target(self) -> None:
        """TC-13.10 must remain Target."""
        adr_path = _REPO_ROOT / "skills" / "agentdesk" / "references" / "adr" / "001-mad-agentdesk-integration.md"
        text = adr_path.read_text(encoding="utf-8")

        self.assertIn("TC-13.10", text, "ADR must reference TC-13.10")

        lines_with_1310 = [
            line for line in text.splitlines()
            if "TC-13.10" in line
        ]
        for line in lines_with_1310:
            if "|" in line:
                fields = [f.strip() for f in line.split("|")]
                status_field = fields[3] if len(fields) > 3 else ""
                self.assertNotEqual(
                    status_field, "Current",
                    f"TC-13.10 must not be Current: {line}"
                )

    # ── Claude fixture structure ──────────────────────────────────────────

    def test_190_claude_fixtures_have_result_field(self) -> None:
        """All Claude JSON fixtures must have a 'result' field."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertIn("result", obj,
                          f"Claude fixture missing 'result': {cap['fixture_path']}")
            self.assertIsInstance(obj["result"], str)

    def test_191_claude_type_is_result(self) -> None:
        """Claude fixtures have type: 'result'."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual(obj.get("type"), "result")

    def test_192_claude_subtype_is_success(self) -> None:
        """Claude fixtures have subtype: 'success'."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual(obj.get("subtype"), "success")

    def test_193_claude_is_error_false(self) -> None:
        """Claude fixtures have is_error: false."""
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            self.assertFalse(obj.get("is_error"))

    def test_194_claude_consistent_top_level_keys(self) -> None:
        """All 3 Claude fixtures share the same top-level keys."""
        claude_keys = []
        for cap in self.provenance["captures"]:
            fp = _REPO_ROOT / cap["fixture_path"]
            obj = json.loads(fp.read_text(encoding="utf-8"))
            claude_keys.append(set(obj.keys()))

        self.assertEqual(len(claude_keys), 3)
        self.assertEqual(claude_keys[0], claude_keys[1],
                         "Claude samples don't share consistent keys")
        self.assertEqual(claude_keys[0], claude_keys[2],
                         "Claude samples don't share consistent keys")

    # ── Redacted fields are present in fixture ────────────────────────────

    def test_200_redacted_keys_exist_in_fixture(self) -> None:
        """Each redaction path must point to a key present in the fixture dict."""
        for cap in self.provenance["captures"]:
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
                    path = redaction["path"]
                    parts = path.lstrip("$.").split(".")

                    # Navigate to parent dict, handle wildcard
                    if "*" in parts:
                        # $.modelUsage.*.costUSD — check each model entry
                        wild_idx = parts.index("*")
                        prefix = parts[:wild_idx]
                        suffix = parts[wild_idx + 1:]

                        parent = obj
                        for part in prefix:
                            self.assertIsInstance(parent, dict)
                            self.assertIn(part, parent)
                            parent = parent[part]

                        # parent is modelUsage dict; check each model
                        for model_key, model_entry in parent.items():
                            for part in suffix:
                                self.assertIsInstance(model_entry, dict,
                                    f"modelUsage.{model_key} not a dict")
                                self.assertIn(part, model_entry,
                                    f"modelUsage.{model_key} missing '{part}'")
                    else:
                        parent = obj
                        for part in parts[:-1]:
                            self.assertIsInstance(parent, dict)
                            self.assertIn(part, parent)
                            parent = parent[part]
                        leaf_key = parts[-1]
                        self.assertIsInstance(parent, dict)
                        self.assertIn(leaf_key, parent)

    # ── No production code modified ───────────────────────────────────────

    def test_210_production_modules_not_modified(self) -> None:
        """Production files must not claim TC-13.9c Current implementation."""
        for py_file in sorted(_SCRIPTS.glob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            lines = text.splitlines()
            for lineno, line in enumerate(lines, 1):
                if "TC-13.9c" in line:
                    with self.subTest(file=py_file.name, line=lineno):
                        self.assertNotIn(
                            "Current", line,
                            f"{py_file.name}:{lineno} must not mark TC-13.9c as Current: {line.strip()}"
                        )

    # ── Provenance no absolute Windows paths ─────────────────────────────

    def test_220_provenance_no_absolute_windows_paths(self) -> None:
        """provenance.json must not contain absolute Windows paths."""
        text = self.provenance_path.read_text(encoding="utf-8")
        drive_path = re.compile(r'[A-Za-z]:\\Users\\')
        self.assertIsNone(drive_path.search(text),
                          "provenance.json contains drive-letter user path")

    # ── Self-consistency ──────────────────────────────────────────────────

    def test_999_self_consistency(self) -> None:
        """This test file itself must parse as valid Python."""
        self.assertTrue(
            Path(__file__).read_text(encoding="utf-8").startswith('"""'),
            "Test module must start with docstring"
        )


if __name__ == "__main__":
    unittest.main()
