"""TC-13.22b.1 — deterministic TaskDifficulty assessor tests."""

from __future__ import annotations

import ast
import dataclasses
import enum
import sys
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import get_type_hints

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402
import difficulty_assessor as assessor  # noqa: E402
sys.path.pop(0)


TD = core_types.TaskDifficulty
AD = assessor.AssessmentDimension

_BASIC_KEYS = (
    "scope.bounded",
    "clarity.explicit",
    "concurrency.single_writer",
    "contract.internal",
    "impact.informational",
    "rollback.simple",
    "dependency.none",
)
_DIMENSION_INDEX = {dimension: index for index, dimension in enumerate(AD)}


def _assessment(**overrides: object) -> assessor.TaskDifficultyAssessment:
    values: dict[str, object] = {
        "assessment_id": "ASM-TC-13.22b.1-r1",
        "task_id": "TC-13.22b.1",
        "revision": 1,
        "rationale_keys": _BASIC_KEYS,
    }
    values.update(overrides)
    return assessor.assess_task_difficulty(**values)  # type: ignore[arg-type]


def _keys_with(index: int, rationale_key: str) -> tuple[str, ...]:
    keys = list(_BASIC_KEYS)
    keys[index] = rationale_key
    return tuple(keys)


class TaskDifficultyAssessorHappyPathTests(unittest.TestCase):
    """The required BASIC happy path."""

    def test_basic_happy_path(self) -> None:
        result = _assessment()
        self.assertEqual(result.schema_version, "agentdesk.difficulty-assessment/v1")
        self.assertEqual(result.policy_version, "agentdesk.difficulty-policy/v1")
        self.assertEqual(result.minimum_difficulty, TD.BASIC)
        self.assertEqual(result.recommended_difficulty, TD.BASIC)
        self.assertEqual(result.selected_difficulty, TD.BASIC)
        self.assertEqual(result.override_direction, "none")
        self.assertIsNone(result.override_reason)
        self.assertIsNone(result.approval_id)
        self.assertIsInstance(result.dimension_results, tuple)
        self.assertEqual(tuple(item.dimension for item in result.dimension_results), tuple(AD))


class PublicApiTests(unittest.TestCase):
    def test_all_is_exactly_seven_symbols(self) -> None:
        self.assertEqual(
            assessor.__all__,
            [
                "AssessmentDimension",
                "DimensionResult",
                "TaskDifficultyAssessment",
                "DifficultyAssessmentError",
                "DifficultyAssessmentInputError",
                "DifficultyAssessmentStateError",
                "assess_task_difficulty",
            ],
        )

    def test_dimension_enum_is_exact_and_ordered(self) -> None:
        self.assertTrue(issubclass(AD, str))
        self.assertTrue(issubclass(AD, enum.Enum))
        self.assertEqual(
            [item.value for item in AD],
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
        self.assertEqual(len(AD.__members__), 7)

    def test_dimension_result_is_frozen_slots_with_exact_fields(self) -> None:
        self.assertTrue(is_dataclass(assessor.DimensionResult))
        self.assertEqual(
            [field.name for field in fields(assessor.DimensionResult)],
            ["dimension", "difficulty", "rationale_key", "hard_floor"],
        )
        self.assertEqual(
            assessor.DimensionResult.__slots__,
            ("dimension", "difficulty", "rationale_key", "hard_floor"),
        )
        result = _assessment().dimension_results[0]
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            result.difficulty = TD.EXPERT  # type: ignore[misc]

    def test_assessment_is_frozen_slots_with_exact_fields(self) -> None:
        self.assertTrue(is_dataclass(assessor.TaskDifficultyAssessment))
        self.assertEqual(
            [field.name for field in fields(assessor.TaskDifficultyAssessment)],
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
            ],
        )
        self.assertEqual(len(assessor.TaskDifficultyAssessment.__slots__), 12)
        result = _assessment()
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            result.selected_difficulty = TD.EXPERT  # type: ignore[misc]

    def test_public_field_types_are_exactly_typed(self) -> None:
        dimension_types = get_type_hints(assessor.DimensionResult)
        self.assertEqual(
            dimension_types,
            {
                "dimension": AD,
                "difficulty": TD,
                "rationale_key": str,
                "hard_floor": bool,
            },
        )
        assessment_types = get_type_hints(assessor.TaskDifficultyAssessment)
        self.assertEqual(assessment_types["schema_version"], str)
        self.assertEqual(assessment_types["assessment_id"], str)
        self.assertEqual(assessment_types["task_id"], str)
        self.assertEqual(assessment_types["revision"], int)
        self.assertEqual(assessment_types["minimum_difficulty"], TD)
        self.assertEqual(assessment_types["recommended_difficulty"], TD)
        self.assertEqual(assessment_types["selected_difficulty"], TD)
        self.assertEqual(assessment_types["dimension_results"], tuple[assessor.DimensionResult, ...])
        self.assertEqual(assessment_types["override_direction"], str)
        self.assertEqual(str(assessment_types["override_reason"]), "str | None")
        self.assertEqual(str(assessment_types["approval_id"]), "str | None")
        self.assertEqual(assessment_types["policy_version"], str)
        for annotation in (*dimension_types.values(), *assessment_types.values()):
            self.assertNotIn("Any", str(annotation))
            self.assertNotIn("dict", str(annotation).lower())
            self.assertNotIn("mapping", str(annotation).lower())
            self.assertNotIn("list", str(annotation).lower())


class FrozenPolicyTests(unittest.TestCase):
    def test_every_frozen_rationale_maps_to_dimension_difficulty_and_floor(self) -> None:
        expected = (
            ("scope.bounded", AD.MODIFICATION_SCOPE, TD.BASIC, False),
            ("scope.single_module", AD.MODIFICATION_SCOPE, TD.STANDARD, False),
            ("scope.cross_module", AD.MODIFICATION_SCOPE, TD.ADVANCED, False),
            ("scope.architecture", AD.MODIFICATION_SCOPE, TD.EXPERT, False),
            ("clarity.explicit", AD.REQUIREMENT_CLARITY, TD.BASIC, False),
            ("clarity.known_pattern", AD.REQUIREMENT_CLARITY, TD.STANDARD, False),
            ("clarity.ambiguous_constraints", AD.REQUIREMENT_CLARITY, TD.ADVANCED, False),
            ("clarity.open_decision", AD.REQUIREMENT_CLARITY, TD.EXPERT, False),
            ("concurrency.single_writer", AD.STATE_CONCURRENCY, TD.BASIC, False),
            ("concurrency.shared_state", AD.STATE_CONCURRENCY, TD.STANDARD, False),
            ("concurrency.lock_cas", AD.STATE_CONCURRENCY, TD.ADVANCED, True),
            ("concurrency.cross_process_recovery", AD.STATE_CONCURRENCY, TD.EXPERT, True),
            ("contract.internal", AD.PUBLIC_CONTRACT, TD.BASIC, False),
            ("contract.additive", AD.PUBLIC_CONTRACT, TD.STANDARD, False),
            ("contract.compatibility_sensitive", AD.PUBLIC_CONTRACT, TD.ADVANCED, False),
            ("contract.breaking_migration", AD.PUBLIC_CONTRACT, TD.EXPERT, True),
            ("impact.informational", AD.FAILURE_IMPACT, TD.BASIC, False),
            ("impact.local_failure", AD.FAILURE_IMPACT, TD.STANDARD, False),
            ("impact.service_degradation", AD.FAILURE_IMPACT, TD.ADVANCED, False),
            ("impact.security_boundary", AD.FAILURE_IMPACT, TD.EXPERT, True),
            ("impact.data_corruption", AD.FAILURE_IMPACT, TD.EXPERT, True),
            ("rollback.simple", AD.ROLLBACK_COMPLEXITY, TD.BASIC, False),
            ("rollback.tested", AD.ROLLBACK_COMPLEXITY, TD.STANDARD, False),
            ("rollback.multi_step", AD.ROLLBACK_COMPLEXITY, TD.ADVANCED, False),
            ("rollback.irreversible", AD.ROLLBACK_COMPLEXITY, TD.EXPERT, True),
            ("dependency.none", AD.DEPENDENCY_CONFLICT, TD.BASIC, False),
            ("dependency.known", AD.DEPENDENCY_CONFLICT, TD.STANDARD, False),
            ("dependency.cross_repository", AD.DEPENDENCY_CONFLICT, TD.ADVANCED, False),
            ("dependency.unresolved", AD.DEPENDENCY_CONFLICT, TD.EXPERT, False),
        )
        self.assertEqual(len(expected), 29)
        for rationale_key, dimension, difficulty, hard_floor in expected:
            with self.subTest(rationale_key=rationale_key):
                result = _assessment(
                    rationale_keys=_keys_with(_DIMENSION_INDEX[dimension], rationale_key)
                )
                actual = result.dimension_results[_DIMENSION_INDEX[dimension]]
                self.assertEqual(
                    (actual.dimension, actual.difficulty, actual.hard_floor),
                    (dimension, difficulty, hard_floor),
                )
                self.assertEqual(actual.rationale_key, rationale_key)

    def test_unknown_rationale_fails_closed(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(rationale_keys=_keys_with(0, "scope.invented"))

    def test_duplicate_dimension_fails_closed(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(rationale_keys=("scope.bounded",) * 7)

    def test_missing_dimension_fails_closed(self) -> None:
        keys = _BASIC_KEYS[:-1] + ("scope.bounded",)
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(rationale_keys=keys)

    def test_dimension_order_fails_closed(self) -> None:
        keys = (_BASIC_KEYS[1], _BASIC_KEYS[0], *_BASIC_KEYS[2:])
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(rationale_keys=keys)


class AggregationTests(unittest.TestCase):
    def test_lock_cas_has_advanced_minimum(self) -> None:
        result = _assessment(rationale_keys=_keys_with(2, "concurrency.lock_cas"))
        self.assertEqual(result.minimum_difficulty, TD.ADVANCED)
        self.assertEqual(result.recommended_difficulty, TD.ADVANCED)

    def test_cross_process_recovery_has_expert_minimum(self) -> None:
        result = _assessment(
            rationale_keys=_keys_with(2, "concurrency.cross_process_recovery")
        )
        self.assertEqual(result.minimum_difficulty, TD.EXPERT)
        self.assertEqual(result.recommended_difficulty, TD.EXPERT)

    def test_each_expert_hard_floor_is_expert_minimum(self) -> None:
        for index, rationale_key in (
            (3, "contract.breaking_migration"),
            (4, "impact.security_boundary"),
            (4, "impact.data_corruption"),
            (5, "rollback.irreversible"),
        ):
            with self.subTest(rationale_key=rationale_key):
                result = _assessment(
                    rationale_keys=_keys_with(index, rationale_key)
                )
                self.assertEqual(result.minimum_difficulty, TD.EXPERT)

    def test_cross_repository_is_advanced_without_hard_floor(self) -> None:
        result = _assessment(
            rationale_keys=_keys_with(6, "dependency.cross_repository")
        )
        self.assertEqual(result.minimum_difficulty, TD.BASIC)
        self.assertEqual(result.recommended_difficulty, TD.ADVANCED)

    def test_recommended_is_maximum_of_all_seven_dimensions(self) -> None:
        result = _assessment(
            rationale_keys=(
                "scope.cross_module",
                "clarity.explicit",
                "concurrency.single_writer",
                "contract.compatibility_sensitive",
                "impact.informational",
                "rollback.simple",
                "dependency.known",
            )
        )
        self.assertEqual(result.minimum_difficulty, TD.BASIC)
        self.assertEqual(result.recommended_difficulty, TD.ADVANCED)


class OverrideTests(unittest.TestCase):
    def test_upward_selection_needs_no_approval(self) -> None:
        result = _assessment(selected_difficulty=TD.STANDARD)
        self.assertEqual(result.override_direction, "up")
        self.assertEqual(result.selected_difficulty, TD.STANDARD)
        self.assertIsNone(result.override_reason)
        self.assertIsNone(result.approval_id)

    def test_valid_downward_selection_requires_reason_and_approval_shape(self) -> None:
        result = _assessment(
            rationale_keys=_keys_with(0, "scope.architecture"),
            selected_difficulty=TD.ADVANCED,
            override_reason="pm_scope_calibration",
            approval_id="APR-TC-13.22b.1-1",
        )
        self.assertEqual(result.minimum_difficulty, TD.BASIC)
        self.assertEqual(result.recommended_difficulty, TD.EXPERT)
        self.assertEqual(result.selected_difficulty, TD.ADVANCED)
        self.assertEqual(result.override_direction, "down")

    def test_downward_selection_without_reason_is_rejected(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(
                rationale_keys=_keys_with(0, "scope.architecture"),
                selected_difficulty=TD.ADVANCED,
                approval_id="APR-1",
            )

    def test_downward_selection_with_unknown_reason_is_rejected(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(
                rationale_keys=_keys_with(0, "scope.architecture"),
                selected_difficulty=TD.ADVANCED,
                override_reason="llm_suggestion",
                approval_id="APR-1",
            )

    def test_downward_selection_without_approval_is_rejected(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(
                rationale_keys=_keys_with(0, "scope.architecture"),
                selected_difficulty=TD.ADVANCED,
                override_reason="pm_scope_calibration",
            )

    def test_downward_selection_with_bad_approval_format_is_rejected(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(
                rationale_keys=_keys_with(0, "scope.architecture"),
                selected_difficulty=TD.ADVANCED,
                override_reason="pm_scope_calibration",
                approval_id="APPROVAL-1",
            )

    def test_selection_below_minimum_is_rejected_even_with_approval(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentStateError):
            _assessment(
                rationale_keys=_keys_with(2, "concurrency.lock_cas"),
                selected_difficulty=TD.STANDARD,
                override_reason="pm_known_pattern",
                approval_id="APR-1",
            )

    def test_override_fields_are_rejected_for_none_or_up(self) -> None:
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(override_reason="pm_known_pattern", approval_id="APR-1")
        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(
                selected_difficulty=TD.STANDARD,
                override_reason="pm_known_pattern",
                approval_id="APR-1",
            )


class InputSafetyTests(unittest.TestCase):
    def test_input_shape_and_identity_rules_are_strict(self) -> None:
        class StringSubclass(str):
            pass

        class OtherEnum(str, enum.Enum):
            BASIC = "basic"

        invalid_cases = (
            {"assessment_id": "ASM-"},
            {"assessment_id": "bad-id"},
            {"task_id": ""},
            {"task_id": " TC-1"},
            {"revision": True},
            {"revision": 0},
            {"rationale_keys": list(_BASIC_KEYS)},
            {"selected_difficulty": "basic"},
            {"selected_difficulty": OtherEnum.BASIC},
            {"assessment_id": StringSubclass("ASM-1")},
            {"task_id": StringSubclass("TC-1")},
            {"rationale_keys": (StringSubclass(_BASIC_KEYS[0]), *_BASIC_KEYS[1:])},
            {"override_reason": StringSubclass("pm_known_pattern")},
            {"approval_id": StringSubclass("APR-1")},
            {"task_id": "TC-1\nsecret"},
            {"task_id": "TC-1\rsecret"},
            {"task_id": "TC-1\x00secret"},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(assessor.DifficultyAssessmentInputError):
                    _assessment(**overrides)

    def test_malicious_repr_is_never_called(self) -> None:
        class Evil:
            def __repr__(self) -> str:
                raise AssertionError("repr was called")

            def __str__(self) -> str:
                raise AssertionError("str was called")

        with self.assertRaises(assessor.DifficultyAssessmentInputError):
            _assessment(assessment_id=Evil())

    def test_exception_does_not_leak_raw_input(self) -> None:
        secret = "ASM-SECRET-RAW-INPUT"
        with self.assertRaises(assessor.DifficultyAssessmentInputError) as context:
            _assessment(assessment_id="ASM-\n" + secret)
        self.assertNotIn(secret, str(context.exception))

    def test_same_input_is_equal_and_hashable(self) -> None:
        first = _assessment()
        second = _assessment()
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_production_module_has_no_forbidden_runtime_dependencies(self) -> None:
        source_path = _SCRIPTS / "difficulty_assessor.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden_import_roots = {
            "subprocess",
            "socket",
            "datetime",
            "random",
            "uuid",
            "pathlib",
            "os",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden_import_roots)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    (node.module or "").split(".")[0], forbidden_import_roots
                )
            if isinstance(node, ast.Name):
                self.assertNotIn(
                    node.id,
                    {"WorkerKind", "ApprovalGate", "WorkflowOrchestrator", "StateProvider"},
                )
        self.assertNotIn("select_model", source_path.read_text(encoding="utf-8"))
        self.assertNotIn("provider_cli", source_path.read_text(encoding="utf-8"))
