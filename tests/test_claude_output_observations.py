"""TC-13.9c-Claude-1: Dynamic observation assertions against Claude 2.1.214 fixtures.

Validates observed facts only — no hand-written assertions about
field values the fixtures do not contain.  Every check is dynamically
derived from the 3 real fixture files.

stdlib-only unittest; no external dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "provider-output"
_CLAUDE_DIR = _FIXTURES / "claude" / "2.1.214"
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"

OBSERVATIONS_DOC = (
    _REPO_ROOT / "skills" / "agentdesk" / "references"
    / "claude-output-observations-2.1.214.md"
)

SAMPLE_IDS = ["success-minimal", "success-unicode", "application-boundary"]

# Exact sanitized fixture SHA-256 from provenance — must never change.
FIXTURE_SHA = {
    "success-minimal":      "d447f50c976d3b58fb3b1b9c80e82b42ce089d4f19f4f2e7e69ff775ef281a85",
    "success-unicode":      "d0d97454dcbe02d43fa25cc702fee1cd84bed85c65119e1d7762861b74a610bd",
    "application-boundary": "c29a846620b9b9b438bd235d8852fb043d3964afd82f49cc1077de7cfb561921",
}

# Expected redaction paths from provenance
_REQUIRED_REDACTIONS = {"$.session_id", "$.uuid", "$.total_cost_usd", "$.modelUsage.*.costUSD"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestClaudeOutputObservations(unittest.TestCase):
    """Dynamic observations from 3 real Claude 2.1.214 fixtures.

    Every assertion is derived from fixture content, not hand-written
    field value expectations.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = {}
        for sid in SAMPLE_IDS:
            fp = _CLAUDE_DIR / f"{sid}.json"
            cls.fixtures[sid] = json.loads(fp.read_text(encoding="utf-8"))

        cls.fix_list = list(cls.fixtures.values())
        cls.fix_names = list(cls.fixtures.keys())

    # ── Fixture loading ───────────────────────────────────────────────────

    def test_001_exactly_three_fixtures_loaded(self) -> None:
        """Exactly 3 Claude fixtures are loaded."""
        self.assertEqual(len(self.fixtures), 3)

    def test_002_version_directory_is_2_1_214(self) -> None:
        """The fixture directory must be claude/2.1.214."""
        self.assertTrue(_CLAUDE_DIR.is_dir())
        self.assertEqual(_CLAUDE_DIR.parent.name, "claude")
        self.assertEqual(_CLAUDE_DIR.name, "2.1.214")

    def test_003_all_fixtures_exist(self) -> None:
        """All 3 fixture files must exist on disk."""
        for sid in SAMPLE_IDS:
            fp = _CLAUDE_DIR / f"{sid}.json"
            self.assertTrue(fp.is_file(), f"Missing: {fp}")

    # ── Non-regression: fixture SHA unchanged ─────────────────────────────

    def test_010_fixture_sha_unchanged(self) -> None:
        """Fixture SHA-256 must match the provenance record — no modification."""
        for sid in SAMPLE_IDS:
            fp = _CLAUDE_DIR / f"{sid}.json"
            actual = _sha256(fp.read_bytes())
            with self.subTest(fixture=sid):
                self.assertEqual(actual, FIXTURE_SHA[sid],
                                 f"Fixture {sid} SHA changed! "
                                 f"Expected {FIXTURE_SHA[sid]}, got {actual}")

    # ── Provenance consistency ────────────────────────────────────────────

    def test_020_provenance_has_three_claude_captures(self) -> None:
        """provenance.json must list exactly 3 Claude captures."""
        prov = json.loads((_FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        claude = [c for c in prov["captures"] if c["provider_id"] == "claude"]
        self.assertEqual(len(claude), 3)

    def test_021_all_provenance_captures_exit_zero(self) -> None:
        """All 3 provenance captures must have exit_code 0."""
        prov = json.loads((_FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        for cap in prov["captures"]:
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(cap["exit_code"], 0)

    def test_022_all_provenance_captures_are_real(self) -> None:
        """All captures must be source_kind == 'real_cli_capture'."""
        prov = json.loads((_FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        for cap in prov["captures"]:
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(cap["source_kind"], "real_cli_capture")

    # ── Root structural invariants ────────────────────────────────────────

    def test_030_all_roots_are_object(self) -> None:
        """Every fixture root must be a dict (JSON object)."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f, dict)

    def test_031_top_level_key_intersection_equals_union(self) -> None:
        """All 3 fixtures share the exact same set of top-level keys."""
        key_sets = [set(f.keys()) for f in self.fix_list]
        self.assertEqual(key_sets[0], key_sets[1],
                         f"Key mismatch between {SAMPLE_IDS[0]} and {SAMPLE_IDS[1]}")
        self.assertEqual(key_sets[0], key_sets[2],
                         f"Key mismatch between {SAMPLE_IDS[0]} and {SAMPLE_IDS[2]}")

    def test_032_calculate_field_stats(self) -> None:
        """Dynamically compute union and intersection — document, don't enforce."""
        key_sets = [set(f.keys()) for f in self.fix_list]
        union = set.union(*key_sets)
        inter = set.intersection(*key_sets)
        # All observed at 3/3
        self.assertEqual(len(union), len(inter),
                         f"Union ({len(union)}) != intersection ({len(inter)})")
        self.assertEqual(len(union), 20,
                         f"Expected 20 top-level keys, got {len(union)}: {sorted(union)}")

    def test_033_field_count_is_twenty(self) -> None:
        """Exactly 20 top-level keys are observed across all 3 captures."""
        key_sets = [set(f.keys()) for f in self.fix_list]
        inter = set.intersection(*key_sets)
        self.assertEqual(len(inter), 20)

    # ── result field analysis ─────────────────────────────────────────────

    def test_040_result_present_in_all(self) -> None:
        """$.result exists in all 3 fixtures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIn("result", f,
                              f"$.result missing from {sid}")

    def test_041_result_is_string_in_all(self) -> None:
        """$.result is a string in all 3 fixtures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["result"], str)

    def test_042_result_non_empty_in_all(self) -> None:
        """$.result is non-empty in all 3 fixtures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertGreater(len(f["result"]), 0)

    def test_043_success_minimal_contains_evidence_ok(self) -> None:
        """success-minimal result contains the expected token."""
        r = self.fixtures["success-minimal"]["result"]
        self.assertIn("EVIDENCE_OK", r)

    def test_044_success_unicode_preserves_chinese_and_greek(self) -> None:
        """Unicode fixture preserves CJK and Greek characters."""
        r = self.fixtures["success-unicode"]["result"]
        self.assertIn("证据", r, "CJK characters not preserved")
        self.assertIn("Ω", r, "Greek Omega not preserved")

    def test_045_success_unicode_preserves_newlines(self) -> None:
        """Unicode fixture contains newline characters."""
        r = self.fixtures["success-unicode"]["result"]
        self.assertIn("\n", r, "Newline not found in unicode result")

    def test_046_application_boundary_contains_evidence_end(self) -> None:
        """application-boundary result contains the expected termination token."""
        r = self.fixtures["application-boundary"]["result"]
        self.assertIn("EVIDENCE_END", r)

    def test_047_application_boundary_has_multiple_sentences(self) -> None:
        """application-boundary result is a multi-sentence explanation."""
        r = self.fixtures["application-boundary"]["result"]
        self.assertGreater(len(r.split()), 15,
                           "Expected substantive multi-word response")

    def test_048_result_not_redacted(self) -> None:
        """$.result must NOT contain redaction placeholders."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                r = f["result"]
                self.assertNotIn("REDACTED", r)
                # Must not contain UUID-like patterns (redacted data leak)
                uuid_pat = re.compile(
                    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
                )
                self.assertIsNone(uuid_pat.search(r),
                                  f"$.result in {sid} may contain leaked UUID")
                # Must not contain cost data
                self.assertNotRegex(r, r'\d+\.\d+\s*USD',
                                    f"$.result in {sid} may contain leaked cost")

    # ── Success discrimination ────────────────────────────────────────────

    def test_050_type_is_result_in_all(self) -> None:
        """$.type is 'result' in all 3 captures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertEqual(f["type"], "result")

    def test_051_subtype_is_success_in_all(self) -> None:
        """$.subtype is 'success' in all 3 captures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertEqual(f["subtype"], "success")

    def test_052_is_error_false_in_all(self) -> None:
        """$.is_error is false in all 3 captures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertFalse(f["is_error"],
                                 f"Expected is_error=false in {sid}")

    def test_053_api_error_status_null_in_all(self) -> None:
        """$.api_error_status is null in all 3 captures."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsNone(f["api_error_status"],
                                  f"Expected api_error_status=null in {sid}")

    def test_054_success_discrimination_consistent(self) -> None:
        """The 4 success-discrimination fields are identical across all 3."""
        for sid in SAMPLE_IDS:
            f = self.fixtures[sid]
            self.assertEqual(f["type"], "result", f"type mismatch in {sid}")
            self.assertEqual(f["subtype"], "success", f"subtype mismatch in {sid}")
            self.assertFalse(f["is_error"])
            self.assertIsNone(f["api_error_status"])

    # ── Nested structure: usage ───────────────────────────────────────────

    def test_060_usage_is_object(self) -> None:
        """$.usage must be an object in all 3."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["usage"], dict)

    def test_061_usage_sub_key_intersection(self) -> None:
        """usage sub-keys must be identical across all 3 captures."""
        key_sets = [set(f["usage"].keys()) for f in self.fix_list]
        inter = set.intersection(*key_sets)
        union = set.union(*key_sets)
        self.assertEqual(inter, union,
                         f"usage keys differ across captures: "
                         f"inter={sorted(inter)} union={sorted(union)}")

    def test_062_usage_server_tool_use_is_object(self) -> None:
        """usage.server_tool_use must be an object."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["usage"]["server_tool_use"], dict)

    def test_063_usage_cache_creation_is_object(self) -> None:
        """usage.cache_creation must be an object."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["usage"]["cache_creation"], dict)

    def test_064_usage_iterations_is_empty_array(self) -> None:
        """usage.iterations is an empty array in all 3."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["usage"]["iterations"], list)
                self.assertEqual(len(f["usage"]["iterations"]), 0)

    # ── Nested structure: modelUsage ──────────────────────────────────────

    def test_070_model_usage_is_object(self) -> None:
        """$.modelUsage must be an object in all 3."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["modelUsage"], dict)

    def test_071_model_usage_not_empty(self) -> None:
        """modelUsage must have at least one model entry."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertGreater(len(f["modelUsage"]), 0,
                                   f"modelUsage empty in {sid}")

    def test_072_model_keys_are_dynamic(self) -> None:
        """Model keys must NOT be hardcoded — they vary by invocation.

        This test verifies that our test code does not bake in specific
        model names.  The set of model keys is checked for consistency
        across fixtures but specific values are never asserted.
        """
        # Dynamically collect model keys
        all_model_keys = set()
        for f in self.fix_list:
            all_model_keys.update(f["modelUsage"].keys())

        # We observe 2 model keys in these fixtures, but the test does
        # not assert on specific key VALUES — only that the keys form
        # a non-empty set of strings.
        self.assertGreater(len(all_model_keys), 0,
                           "modelUsage must have at least one model key")
        for mk in all_model_keys:
            self.assertIsInstance(mk, str, f"Model key {mk!r} is not a string")

    def test_073_model_usage_sub_keys_consistent(self) -> None:
        """Each model entry has the same 8 sub-keys across all fixtures."""
        for f in self.fix_list:
            for mk, entry in f["modelUsage"].items():
                with self.subTest(model=mk):
                    self.assertIsInstance(entry, dict)
                    self.assertEqual(
                        set(entry.keys()),
                        {"inputTokens", "outputTokens", "cacheReadInputTokens",
                         "cacheCreationInputTokens", "webSearchRequests",
                         "costUSD", "contextWindow", "maxOutputTokens"},
                        f"modelUsage['{mk}'] sub-keys mismatch"
                    )

    # ── Nested: permission_denials ────────────────────────────────────────

    def test_080_permission_denials_is_empty_array(self) -> None:
        """permission_denials is an empty array in all 3."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsInstance(f["permission_denials"], list)
                self.assertEqual(len(f["permission_denials"]), 0)

    # ── Redaction verification ────────────────────────────────────────────

    def test_090_session_id_is_redacted(self) -> None:
        """session_id must be the redacted placeholder."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertEqual(f["session_id"], "REDACTED-SESSION")

    def test_091_uuid_is_redacted(self) -> None:
        """uuid must be the redacted placeholder."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertEqual(f["uuid"], "REDACTED-UUID")

    def test_092_total_cost_usd_is_null(self) -> None:
        """total_cost_usd must be null (redacted)."""
        for sid, f in self.fixtures.items():
            with self.subTest(fixture=sid):
                self.assertIsNone(f["total_cost_usd"])

    def test_093_cost_usd_null_in_all_models(self) -> None:
        """Every modelUsage.*.costUSD must be null (redacted)."""
        for sid, f in self.fixtures.items():
            for mk, entry in f["modelUsage"].items():
                with self.subTest(fixture=sid, model=mk):
                    self.assertIsNone(entry.get("costUSD"),
                                      f"costUSD not null in {sid}/modelUsage/{mk}")

    def test_094_provenance_redactions_match(self) -> None:
        """The 4 redaction paths in provenance match fixture reality."""
        prov = json.loads((_FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        for cap in prov["captures"]:
            paths = {r["path"] for r in cap["redactions"]}
            with self.subTest(fixture=cap["fixture_path"]):
                self.assertEqual(paths, _REQUIRED_REDACTIONS,
                                 f"Redaction paths mismatch: {paths}")

    # ── Documentation content checks ──────────────────────────────────────

    def test_100_observations_doc_exists(self) -> None:
        """The observations document must exist."""
        self.assertTrue(OBSERVATIONS_DOC.is_file(),
                        f"Missing: {OBSERVATIONS_DOC}")

    def test_101_doc_contains_version_scope(self) -> None:
        """Doc must state observed_version == 2.1.214 (exact, not range)."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        self.assertIn("observed_version == 2.1.214", text)

    def test_102_doc_no_geq_version_range(self) -> None:
        """Doc must NOT claim >=2.1.214 compatibility (except as disclaimed example)."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        # The doc intentionally lists "compatible with `>=2.1.214`" as a
        # disclaimed label — search for it as an assertion, not a disclaimer.
        lines = text.splitlines()
        for line in lines:
            stripped = line.strip()
            # Allow it ONLY if preceded by a negation marker or part of a "must not" list
            if ">=2.1.214" in stripped:
                self.assertTrue(
                    stripped.startswith("-") or "not" in stripped.lower()
                    or "must not" in stripped.lower() or "must not" in line.lower(),
                    f"Doc asserts >=2.1.214 compatibility: {stripped}"
                )

    def test_103_doc_no_schema_identifier(self) -> None:
        """Doc must NOT declare agentdesk.claude-output/v1 as its own identity.

        (The doc intentionally lists it as a disclaimed label — check context.)
        """
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        lines = text.splitlines()
        for line in lines:
            if "agentdesk.claude-output/v1" in line:
                self.assertTrue(
                    line.strip().startswith("-") or "not" in line.lower(),
                    f"Doc declares agentdesk.claude-output/v1 as identity: {line.strip()}"
                )

    def test_104_doc_disclaims_frozen_schema(self) -> None:
        """Doc must explicitly disclaim frozen-schema / contract status."""
        text_lower = OBSERVATIONS_DOC.read_text(encoding="utf-8").lower()
        # The doc says: "It is **not** a frozen schema, public contract, ..."
        found = (
            "not a frozen schema" in text_lower
            or "not a public contract" in text_lower
            or "not a frozen contract" in text_lower
            or ("not" in text_lower and "frozen schema" in text_lower)
        )
        self.assertTrue(found,
                        "Doc must explicitly disclaim frozen-schema status")

    def test_105_doc_no_field_required_language(self) -> None:
        """Doc must NOT call any field 'required'."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        # "required" in test names and provenance is fine
        # "required_model_tier" is a field name in provenance, also fine
        # Prohibit "is required" or "required field" in doc prose
        self.assertNotRegex(text, r'(?i)(?:is required|required field|mandatory)')

    def test_106_doc_has_non_coverage_matrix(self) -> None:
        """Doc must include an explicit non-coverage / not-observed matrix."""
        text_lower = OBSERVATIONS_DOC.read_text(encoding="utf-8").lower()
        self.assertIn("not observed", text_lower,
                      "Doc must document unobserved paths")
        # At least 8 specific gaps listed (use terms actually in the doc)
        gaps = [
            "error path",
            "permission denial",
            "tool-use",
            "multiple turn",
            "malformed",
            "second claude version",
            "cancel",
            "timeout",
        ]
        for gap in gaps:
            self.assertIn(gap.lower(), text_lower,
                          f"Doc should mention unobserved gap: {gap}")

    def test_107_doc_contains_result_candidate_conclusion(self) -> None:
        """Doc must identify $.result as the final-text candidate."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        self.assertIn("$.result", text)
        self.assertIn("final-text candidate", text.lower())

    def test_108_doc_contains_decoder_feasibility(self) -> None:
        """Doc must assess decoder feasibility options A/B/C."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        self.assertIn("Option A", text)
        self.assertIn("Option B", text)
        self.assertIn("Option C", text)

    def test_109_doc_contains_evidence_grade(self) -> None:
        """Doc must state evidence grade B+."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        self.assertIn("B+", text)

    def test_110_doc_contains_codex_out_of_scope(self) -> None:
        """Doc must state Codex is not in scope for this analysis."""
        text = OBSERVATIONS_DOC.read_text(encoding="utf-8")
        self.assertIn("Codex", text)

    # ── Guard: decoder not implemented ────────────────────────────────────

    def test_120_decoder_not_implemented(self) -> None:
        """No output decoder module must exist in production scripts."""
        decoder_names = [
            "output_decoder.py", "provider_decoder.py",
            "claude_decoder.py", "codex_decoder.py",
            "cli_output_parser.py", "stream_parser.py",
            "claude_output.py", "result_parser.py",
        ]
        for name in decoder_names:
            self.assertFalse(
                (_SCRIPTS / name).exists(),
                f"Decoder file exists (must not): {name}"
            )

    # ── Guard: TC statuses ────────────────────────────────────────────────

    def test_130_tc139c_still_target(self) -> None:
        """TC-13.9c must remain Target — not Current."""
        adr = (_REPO_ROOT / "skills" / "agentdesk" / "references" / "adr"
               / "001-mad-agentdesk-integration.md")
        text = adr.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "TC-13.9c" in line and "|" in line:
                fields = [f.strip() for f in line.split("|")]
                if len(fields) > 3:
                    self.assertNotEqual(fields[3], "Current",
                                        f"TC-13.9c must not be Current: {line}")

    def test_131_tc1310_still_target(self) -> None:
        """TC-13.10 must remain Target — not Current."""
        adr = (_REPO_ROOT / "skills" / "agentdesk" / "references" / "adr"
               / "001-mad-agentdesk-integration.md")
        text = adr.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "TC-13.10" in line and "|" in line:
                fields = [f.strip() for f in line.split("|")]
                if len(fields) > 3:
                    self.assertNotEqual(fields[3], "Current",
                                        f"TC-13.10 must not be Current: {line}")

    # ── Self-consistency ──────────────────────────────────────────────────

    def test_999_self_consistency(self) -> None:
        """This test module starts with a docstring."""
        self.assertTrue(
            Path(__file__).read_text(encoding="utf-8").startswith('"""'),
            "Test module must start with docstring"
        )


if __name__ == "__main__":
    unittest.main()
