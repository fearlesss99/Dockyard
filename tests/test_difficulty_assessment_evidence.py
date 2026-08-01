"""TC-13.22b.2a — canonical TaskDifficulty evidence codec tests."""

from __future__ import annotations

import ast
import dataclasses
import sys
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import get_type_hints
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402
import difficulty_assessor  # noqa: E402
import difficulty_assessment_evidence as evidence  # noqa: E402
sys.path.pop(0)


TD = core_types.TaskDifficulty
COMMIT = "0123456789abcdef0123456789abcdef01234567"
RATIONALE_KEYS = (
    "scope.bounded",
    "clarity.explicit",
    "concurrency.single_writer",
    "contract.internal",
    "impact.informational",
    "rollback.simple",
    "dependency.none",
)


def _assessment(**overrides: object) -> difficulty_assessor.TaskDifficultyAssessment:
    values: dict[str, object] = {
        "assessment_id": "ASM-TC-13.22b.2a-r1",
        "task_id": "TC-13.22b.2a",
        "revision": 1,
        "rationale_keys": RATIONALE_KEYS,
    }
    values.update(overrides)
    return difficulty_assessor.assess_task_difficulty(**values)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> evidence.DifficultyAssessmentEvidence:
    snapshot_commit = overrides.pop("snapshot_commit", COMMIT)
    return evidence.DifficultyAssessmentEvidence(
        assessment=_assessment(**overrides),
        snapshot_commit=snapshot_commit,  # type: ignore[arg-type]
    )


def _encoded(**overrides: object) -> bytes:
    return evidence.encode_difficulty_assessment_evidence(_evidence(**overrides))


class EvidenceCodecHappyPathTests(unittest.TestCase):
    def test_encode_decode_happy_path(self) -> None:
        original = _evidence()
        encoded = evidence.encode_difficulty_assessment_evidence(original)
        decoded = evidence.decode_difficulty_assessment_evidence(encoded)
        self.assertEqual(decoded, original)
        self.assertEqual(
            evidence.encode_difficulty_assessment_evidence(decoded),
            encoded,
        )


class PublicApiTests(unittest.TestCase):
    def test_all_is_exactly_seven_symbols(self) -> None:
        self.assertEqual(
            evidence.__all__,
            [
                "DifficultyAssessmentEvidence",
                "DifficultyEvidenceError",
                "DifficultyEvidenceInputError",
                "DifficultyEvidenceSchemaError",
                "DifficultyEvidenceSecurityError",
                "encode_difficulty_assessment_evidence",
                "decode_difficulty_assessment_evidence",
            ],
        )

    def test_evidence_is_frozen_slots_with_two_fields(self) -> None:
        self.assertTrue(is_dataclass(evidence.DifficultyAssessmentEvidence))
        self.assertEqual(
            [field.name for field in fields(evidence.DifficultyAssessmentEvidence)],
            ["assessment", "snapshot_commit"],
        )
        self.assertEqual(
            evidence.DifficultyAssessmentEvidence.__slots__,
            ("assessment", "snapshot_commit"),
        )
        value = _evidence()
        self.assertFalse(hasattr(value, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.snapshot_commit = COMMIT  # type: ignore[misc]

    def test_public_field_types_are_exactly_typed(self) -> None:
        types = get_type_hints(evidence.DifficultyAssessmentEvidence)
        self.assertEqual(types["assessment"], difficulty_assessor.TaskDifficultyAssessment)
        self.assertEqual(types["snapshot_commit"], str)
        for annotation in types.values():
            self.assertNotIn("Any", str(annotation))
            self.assertNotIn("dict", str(annotation).lower())
            self.assertNotIn("mapping", str(annotation).lower())
            self.assertNotIn("list", str(annotation).lower())


class EncodeCanonicalTests(unittest.TestCase):
    def test_root_keys_are_exact_and_ordered(self) -> None:
        lines = _encoded().decode("utf-8").splitlines()
        root_keys = [
            line.split(":", 1)[0]
            for line in lines
            if not line.startswith("  ")
        ]
        self.assertEqual(
            root_keys,
            [
                "schema_version",
                "assessment_id",
                "task_id",
                "revision",
                "minimum_difficulty",
                "recommended_difficulty",
                "selected_difficulty",
                "dimension_results",
                "override_direction",
                "override_reason",
                "approval_id",
                "policy_version",
                "snapshot_commit",
            ],
        )

    def test_bytes_are_utf8_lf_and_single_final_newline(self) -> None:
        encoded = _encoded()
        self.assertFalse(encoded.startswith(b"\xef\xbb\xbf"))
        encoded.decode("utf-8")
        self.assertNotIn(b"\r", encoded)
        self.assertNotIn(b"\t", encoded)
        self.assertNotIn(b"\x00", encoded)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertFalse(encoded.endswith(b"\n\n"))
        self.assertNotIn(b"{", encoded)
        self.assertNotIn(b"}", encoded)

    def test_dimensions_and_scalars_are_canonical(self) -> None:
        text = _encoded().decode("utf-8")
        self.assertIn('schema_version: "agentdesk.difficulty-assessment/v1"', text)
        self.assertIn('minimum_difficulty: "basic"', text)
        self.assertIn('recommended_difficulty: "basic"', text)
        self.assertIn('selected_difficulty: "basic"', text)
        self.assertIn("hard_floor: false", text)
        self.assertIn("override_reason: null", text)
        self.assertIn("approval_id: null", text)
        dimensions = [
            line.split('"', 2)[1]
            for line in text.splitlines()
            if line.startswith("  - dimension:")
        ]
        self.assertEqual(
            dimensions,
            [
                "modification_scope",
                "requirement_clarity",
                "state_concurrency",
                "public_contract",
                "failure_impact",
                "rollback_complexity",
                "dependency_conflict",
            ],
        )

    def test_encoding_is_deterministic_and_does_not_mutate_input(self) -> None:
        original = _evidence()
        before = original
        first = evidence.encode_difficulty_assessment_evidence(original)
        second = evidence.encode_difficulty_assessment_evidence(original)
        self.assertEqual(first, second)
        self.assertEqual(original, before)

    def test_quoted_string_scalar_escapes_round_trip(self) -> None:
        original = _evidence(task_id='TC "quoted" \\ task')
        encoded = evidence.encode_difficulty_assessment_evidence(original)
        self.assertIn('task_id: "TC \\"quoted\\" \\\\ task"', encoded.decode("utf-8"))
        self.assertEqual(evidence.decode_difficulty_assessment_evidence(encoded), original)

    def test_snapshot_commit_requires_lowercase_forty_hex(self) -> None:
        for value in (
            "",
            "A" * 40,
            "g" * 40,
            "0" * 39,
            "0" * 41,
            " " + "0" * 40,
            "0" * 20 + "\n" + "0" * 19,
        ):
            with self.subTest(value=value):
                with self.assertRaises(evidence.DifficultyEvidenceInputError):
                    _encoded(snapshot_commit=value)


class DecodeRejectTests(unittest.TestCase):
    def assertRejects(self, data: object) -> None:
        with self.assertRaises(evidence.DifficultyEvidenceError):
            evidence.decode_difficulty_assessment_evidence(data)  # type: ignore[arg-type]

    def test_decode_accepts_exact_bytes_only(self) -> None:
        encoded = _encoded()

        class BytesSubclass(bytes):
            pass

        for value in (
            encoded.decode("utf-8"),
            bytearray(encoded),
            memoryview(encoded),
            BytesSubclass(encoded),
        ):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(evidence.DifficultyEvidenceInputError):
                    evidence.decode_difficulty_assessment_evidence(value)  # type: ignore[arg-type]

    def test_rejects_encoding_boundaries(self) -> None:
        encoded = _encoded()
        for value in (
            b"\xff" + encoded,
            b"\xef\xbb\xbf" + encoded,
            encoded.replace(b"\n", b"\r\n", 1),
            encoded.replace(b" ", b"\t", 1),
            encoded + b"\x00",
            encoded[:-1],
            encoded + b"\n",
        ):
            with self.subTest(prefix=value[:12]):
                self.assertRejects(value)

    def test_rejects_root_missing_extra_duplicate_and_reordered_keys(self) -> None:
        encoded = _encoded()
        lines = encoded.decode("utf-8").splitlines()
        cases = (
            lines[:11] + lines[12:],
            lines + ['unexpected: "value"'],
            [lines[1], lines[0], *lines[2:]],
            [lines[0], lines[0], *lines[1:]],
        )
        for case in cases:
            with self.subTest(first_lines=case[:2]):
                self.assertRejects(("\n".join(case) + "\n").encode("utf-8"))

    def test_rejects_dimension_missing_extra_duplicate_and_reordered(self) -> None:
        lines = _encoded().decode("utf-8").splitlines()
        start = lines.index("dimension_results:") + 1
        blocks = [lines[start + index:start + index + 4] for index in range(0, 28, 4)]
        before = lines[:start]
        after = lines[start + 28:]
        def flatten(items: list[list[str]]) -> list[str]:
            return [line for block in items for line in block]

        cases = (
            before + flatten(blocks[:-1]) + after,
            before + flatten([*blocks, blocks[0]]) + after,
            before + flatten([blocks[1], blocks[0], *blocks[2:]]) + after,
        )
        for case in cases:
            with self.subTest(dimension_lines=case[start:start + 4]):
                self.assertRejects(("\n".join(case) + "\n").encode("utf-8"))

    def test_rejects_noncanonical_scalars_json_and_yaml_features(self) -> None:
        encoded = _encoded()
        text = encoded.decode("utf-8")
        cases = (
            text.replace(
                'schema_version: "agentdesk.difficulty-assessment/v1"',
                "schema_version: agentdesk.difficulty-assessment/v1",
            ),
            text.replace(
                'schema_version: "agentdesk.difficulty-assessment/v1"',
                'schema_version: &anchor "agentdesk.difficulty-assessment/v1"',
            ),
            text.replace(
                'schema_version: "agentdesk.difficulty-assessment/v1"',
                "schema_version: *anchor",
            ),
            text.replace(
                'schema_version: "agentdesk.difficulty-assessment/v1"',
                'schema_version: !tag "agentdesk.difficulty-assessment/v1"',
            ),
            '{"schema_version": "agentdesk.difficulty-assessment/v1"}\n',
            text + "---\n",
            text + 'trailing: "content"\n',
        )
        for case in cases:
            with self.subTest(prefix=case[:40]):
                self.assertRejects(case.encode("utf-8"))

    def test_rejects_invalid_snapshot_and_dimension_values(self) -> None:
        text = _encoded().decode("utf-8")
        cases = (
            text.replace(f'snapshot_commit: "{COMMIT}"', 'snapshot_commit: "' + "A" * 40 + '"'),
            text.replace('hard_floor: false', 'hard_floor: null', 1),
            text.replace('difficulty: "basic"', 'difficulty: "unknown"', 1),
            text.replace('dimension: "modification_scope"', 'dimension: "unknown"', 1),
            text.replace('override_reason: null', 'override_reason: false'),
        )
        for case in cases:
            with self.subTest(prefix=case[-80:]):
                self.assertRejects(case.encode("utf-8"))

    def test_rejects_when_reencoding_is_not_byte_identical(self) -> None:
        encoded = _encoded()
        with patch.object(
            evidence,
            "encode_difficulty_assessment_evidence",
            return_value=b"different",
        ):
            with self.assertRaises(evidence.DifficultyEvidenceSchemaError):
                evidence.decode_difficulty_assessment_evidence(encoded)


class SecurityBoundaryTests(unittest.TestCase):
    def test_error_messages_do_not_leak_input(self) -> None:
        secret = "ASM-SECRET-TASK-IDENTITY"
        bad = _encoded().replace(
            b'assessment_id: "ASM-TC-13.22b.2a-r1"',
            ('assessment_id: "' + secret + '\\nraw"').encode("utf-8"),
        )
        with self.assertRaises(evidence.DifficultyEvidenceError) as context:
            evidence.decode_difficulty_assessment_evidence(bad)
        self.assertNotIn(secret, str(context.exception))

    def test_malicious_repr_and_str_are_never_called(self) -> None:
        class Evil:
            def __repr__(self) -> str:
                raise AssertionError("repr was called")

            def __str__(self) -> str:
                raise AssertionError("str was called")

        with self.assertRaises(evidence.DifficultyEvidenceInputError):
            evidence.encode_difficulty_assessment_evidence(Evil())  # type: ignore[arg-type]

    def test_production_module_has_no_io_or_third_party_yaml_dependency(self) -> None:
        source_path = _SCRIPTS / "difficulty_assessment_evidence.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_import_roots = {
            "yaml",
            "PyYAML",
            "json",
            "os",
            "pathlib",
            "subprocess",
            "socket",
            "datetime",
            "random",
            "uuid",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden_import_roots)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    (node.module or "").split(".")[0], forbidden_import_roots
                )
        for forbidden in (
            "ApprovalGate",
            "WorkflowOrchestrator",
            "StateProvider",
            "validate_project",
            "select_model",
            "provider_cli",
        ):
            self.assertNotIn(forbidden, source)
