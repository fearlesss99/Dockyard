"""Deterministic PM TaskDifficulty assessment (TC-13.22b.1).

This module is a pure, synchronous policy function.  It accepts the seven
frozen rationale keys, produces the ordered dimension results, aggregates the
minimum and recommended difficulty, and applies the PM selection rules.  It
does not read task cards, Git, approval evidence, or assessment files.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from core_types import TaskDifficulty

__all__ = [
    "AssessmentDimension",
    "DimensionResult",
    "TaskDifficultyAssessment",
    "DifficultyAssessmentError",
    "DifficultyAssessmentInputError",
    "DifficultyAssessmentStateError",
    "assess_task_difficulty",
]


@enum.unique
class AssessmentDimension(str, enum.Enum):
    """The seven ordered dimensions of a PM difficulty assessment."""

    MODIFICATION_SCOPE = "modification_scope"
    REQUIREMENT_CLARITY = "requirement_clarity"
    STATE_CONCURRENCY = "state_concurrency"
    PUBLIC_CONTRACT = "public_contract"
    FAILURE_IMPACT = "failure_impact"
    ROLLBACK_COMPLEXITY = "rollback_complexity"
    DEPENDENCY_CONFLICT = "dependency_conflict"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DimensionResult:
    """The frozen policy result for one assessment dimension."""

    dimension: AssessmentDimension
    difficulty: TaskDifficulty
    rationale_key: str
    hard_floor: bool


@dataclass(frozen=True, slots=True)
class TaskDifficultyAssessment:
    """The frozen twelve-field assessment value consumed before dispatch."""

    schema_version: str
    assessment_id: str
    task_id: str
    revision: int
    minimum_difficulty: TaskDifficulty
    recommended_difficulty: TaskDifficulty
    selected_difficulty: TaskDifficulty
    dimension_results: tuple[DimensionResult, ...]
    override_direction: str
    override_reason: str | None
    approval_id: str | None
    policy_version: str


class DifficultyAssessmentError(ValueError):
    """Base class for deterministic assessment failures."""


class DifficultyAssessmentInputError(DifficultyAssessmentError):
    """Raised when input shape or policy keys are invalid."""


class DifficultyAssessmentStateError(DifficultyAssessmentError):
    """Raised when a selection violates a hard-floor state rule."""


_SCHEMA_VERSION = "agentdesk.difficulty-assessment/v1"
_POLICY_VERSION = "agentdesk.difficulty-policy/v1"
_ASSESSMENT_ID_PATTERN = re.compile(r"ASM-[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_OVERRIDE_REASONS = (
    "pm_scope_calibration",
    "pm_known_pattern",
    "pm_bounded_failure_impact",
    "pm_reviewed_rollback",
)
_DIFFICULTY_ORDER = (
    TaskDifficulty.BASIC,
    TaskDifficulty.STANDARD,
    TaskDifficulty.ADVANCED,
    TaskDifficulty.EXPERT,
)
_DIMENSION_ORDER = tuple(AssessmentDimension)

# The policy is deliberately a tuple of tuples: callers cannot mutate the
# rationale mapping or supply their own difficulty/hard-floor metadata.
_RATIONALE_POLICY = (
    ("scope.bounded", AssessmentDimension.MODIFICATION_SCOPE, TaskDifficulty.BASIC, False),
    ("scope.single_module", AssessmentDimension.MODIFICATION_SCOPE, TaskDifficulty.STANDARD, False),
    ("scope.cross_module", AssessmentDimension.MODIFICATION_SCOPE, TaskDifficulty.ADVANCED, False),
    ("scope.architecture", AssessmentDimension.MODIFICATION_SCOPE, TaskDifficulty.EXPERT, False),
    ("clarity.explicit", AssessmentDimension.REQUIREMENT_CLARITY, TaskDifficulty.BASIC, False),
    ("clarity.known_pattern", AssessmentDimension.REQUIREMENT_CLARITY, TaskDifficulty.STANDARD, False),
    ("clarity.ambiguous_constraints", AssessmentDimension.REQUIREMENT_CLARITY, TaskDifficulty.ADVANCED, False),
    ("clarity.open_decision", AssessmentDimension.REQUIREMENT_CLARITY, TaskDifficulty.EXPERT, False),
    ("concurrency.single_writer", AssessmentDimension.STATE_CONCURRENCY, TaskDifficulty.BASIC, False),
    ("concurrency.shared_state", AssessmentDimension.STATE_CONCURRENCY, TaskDifficulty.STANDARD, False),
    ("concurrency.lock_cas", AssessmentDimension.STATE_CONCURRENCY, TaskDifficulty.ADVANCED, True),
    ("concurrency.cross_process_recovery", AssessmentDimension.STATE_CONCURRENCY, TaskDifficulty.EXPERT, True),
    ("contract.internal", AssessmentDimension.PUBLIC_CONTRACT, TaskDifficulty.BASIC, False),
    ("contract.additive", AssessmentDimension.PUBLIC_CONTRACT, TaskDifficulty.STANDARD, False),
    ("contract.compatibility_sensitive", AssessmentDimension.PUBLIC_CONTRACT, TaskDifficulty.ADVANCED, False),
    ("contract.breaking_migration", AssessmentDimension.PUBLIC_CONTRACT, TaskDifficulty.EXPERT, True),
    ("impact.informational", AssessmentDimension.FAILURE_IMPACT, TaskDifficulty.BASIC, False),
    ("impact.local_failure", AssessmentDimension.FAILURE_IMPACT, TaskDifficulty.STANDARD, False),
    ("impact.service_degradation", AssessmentDimension.FAILURE_IMPACT, TaskDifficulty.ADVANCED, False),
    ("impact.security_boundary", AssessmentDimension.FAILURE_IMPACT, TaskDifficulty.EXPERT, True),
    ("impact.data_corruption", AssessmentDimension.FAILURE_IMPACT, TaskDifficulty.EXPERT, True),
    ("rollback.simple", AssessmentDimension.ROLLBACK_COMPLEXITY, TaskDifficulty.BASIC, False),
    ("rollback.tested", AssessmentDimension.ROLLBACK_COMPLEXITY, TaskDifficulty.STANDARD, False),
    ("rollback.multi_step", AssessmentDimension.ROLLBACK_COMPLEXITY, TaskDifficulty.ADVANCED, False),
    ("rollback.irreversible", AssessmentDimension.ROLLBACK_COMPLEXITY, TaskDifficulty.EXPERT, True),
    ("dependency.none", AssessmentDimension.DEPENDENCY_CONFLICT, TaskDifficulty.BASIC, False),
    ("dependency.known", AssessmentDimension.DEPENDENCY_CONFLICT, TaskDifficulty.STANDARD, False),
    ("dependency.cross_repository", AssessmentDimension.DEPENDENCY_CONFLICT, TaskDifficulty.ADVANCED, False),
    ("dependency.unresolved", AssessmentDimension.DEPENDENCY_CONFLICT, TaskDifficulty.EXPERT, False),
)


def _input_error(code: str) -> DifficultyAssessmentInputError:
    return DifficultyAssessmentInputError(f"difficulty_assessment:{code}")


def _state_error(code: str) -> DifficultyAssessmentStateError:
    return DifficultyAssessmentStateError(f"difficulty_assessment:{code}")


def _clean_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise _input_error(f"{field}_type")
    if not value or value.strip() != value:
        raise _input_error(f"{field}_blank_or_wrapped")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise _input_error(f"{field}_control_character")
    return value


def _validate_assessment_id(value: object) -> str:
    assessment_id = _clean_string(value, "assessment_id")
    if _ASSESSMENT_ID_PATTERN.fullmatch(assessment_id) is None:
        raise _input_error("assessment_id_format")
    return assessment_id


def _validate_optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _clean_string(value, field)


def _validate_task_difficulty(value: object) -> TaskDifficulty:
    if type(value) is not TaskDifficulty:
        raise _input_error("difficulty_type")
    return value


def _policy_for(rationale_key: str) -> tuple[AssessmentDimension, TaskDifficulty, bool] | None:
    for key, dimension, difficulty, hard_floor in _RATIONALE_POLICY:
        if rationale_key == key:
            return dimension, difficulty, hard_floor
    return None


def _validated_dimension_results(
    rationale_keys: object,
) -> tuple[DimensionResult, ...]:
    if type(rationale_keys) is not tuple:
        raise _input_error("rationale_keys_type")
    if len(rationale_keys) != len(_DIMENSION_ORDER):
        raise _input_error("rationale_keys_count")

    results: tuple[DimensionResult, ...] = ()
    for index, rationale_key in enumerate(rationale_keys):
        _clean_string(rationale_key, "rationale_key")
        policy = _policy_for(rationale_key)
        if policy is None:
            raise _input_error("rationale_key_unknown")
        dimension, difficulty, hard_floor = policy
        if dimension is not _DIMENSION_ORDER[index]:
            raise _input_error("rationale_dimension_order")
        results += (
            DimensionResult(
                dimension=dimension,
                difficulty=difficulty,
                rationale_key=rationale_key,
                hard_floor=hard_floor,
            ),
        )
    return results


def _difficulty_index(value: TaskDifficulty) -> int:
    for index, difficulty in enumerate(_DIFFICULTY_ORDER):
        if value is difficulty:
            return index
    raise AssertionError("difficulty policy contains an unknown enum member")


def _maximum_difficulty(values: tuple[TaskDifficulty, ...]) -> TaskDifficulty:
    maximum = TaskDifficulty.BASIC
    for value in values:
        if _difficulty_index(value) > _difficulty_index(maximum):
            maximum = value
    return maximum


def _validate_approval_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("APR-") or len(value) == len("APR-"):
        raise _input_error("approval_id_format")
    return value


def assess_task_difficulty(
    *,
    assessment_id: str,
    task_id: str,
    revision: int,
    rationale_keys: tuple[str, ...],
    selected_difficulty: TaskDifficulty | None = None,
    override_reason: str | None = None,
    approval_id: str | None = None,
) -> TaskDifficultyAssessment:
    """Build a deterministic, frozen TaskDifficulty assessment.

    The caller supplies only the seven frozen rationale keys.  Difficulty and
    hard-floor values come exclusively from this module's policy table.
    """
    validated_assessment_id = _validate_assessment_id(assessment_id)
    validated_task_id = _clean_string(task_id, "task_id")
    if type(revision) is not int:
        raise _input_error("revision_type")
    if revision < 1:
        raise _input_error("revision_range")

    dimension_results = _validated_dimension_results(rationale_keys)
    validated_selected = (
        None
        if selected_difficulty is None
        else _validate_task_difficulty(selected_difficulty)
    )
    validated_reason = _validate_optional_string(override_reason, "override_reason")
    validated_approval = _validate_approval_id(
        _validate_optional_string(approval_id, "approval_id")
    )

    all_difficulties = tuple(result.difficulty for result in dimension_results)
    hard_floor_difficulties = tuple(
        result.difficulty for result in dimension_results if result.hard_floor
    )
    minimum_difficulty = (
        TaskDifficulty.BASIC
        if not hard_floor_difficulties
        else _maximum_difficulty(hard_floor_difficulties)
    )
    recommended_difficulty = _maximum_difficulty(all_difficulties)
    selected = recommended_difficulty if validated_selected is None else validated_selected

    if _difficulty_index(selected) < _difficulty_index(minimum_difficulty):
        raise _state_error("selected_below_minimum")

    if selected is recommended_difficulty:
        if validated_reason is not None or validated_approval is not None:
            raise _input_error("override_fields_without_downward_override")
        override_direction = "none"
        validated_reason = None
        validated_approval = None
    elif _difficulty_index(selected) > _difficulty_index(recommended_difficulty):
        if validated_reason is not None or validated_approval is not None:
            raise _input_error("override_fields_without_downward_override")
        override_direction = "up"
        validated_reason = None
        validated_approval = None
    else:
        if validated_reason not in _OVERRIDE_REASONS:
            raise _input_error("downward_override_reason")
        if validated_approval is None:
            raise _input_error("downward_override_approval")
        override_direction = "down"

    return TaskDifficultyAssessment(
        schema_version=_SCHEMA_VERSION,
        assessment_id=validated_assessment_id,
        task_id=validated_task_id,
        revision=revision,
        minimum_difficulty=minimum_difficulty,
        recommended_difficulty=recommended_difficulty,
        selected_difficulty=selected,
        dimension_results=dimension_results,
        override_direction=override_direction,
        override_reason=validated_reason,
        approval_id=validated_approval,
        policy_version=_POLICY_VERSION,
    )
