"""Canonical in-memory evidence codec for TaskDifficulty assessments.

The codec deliberately implements a small, deterministic YAML subset instead
of depending on a YAML package.  It never reads or writes files, invokes Git,
or performs ancestry or dispatch validation.  The existing deterministic
assessor remains the single source of TaskDifficulty policy invariants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from difficulty_assessor import (
    AssessmentDimension,
    DifficultyAssessmentError,
    DimensionResult,
    TaskDifficultyAssessment,
    assess_task_difficulty,
)
from core_types import TaskDifficulty

__all__ = [
    "DifficultyAssessmentEvidence",
    "DifficultyEvidenceError",
    "DifficultyEvidenceInputError",
    "DifficultyEvidenceSchemaError",
    "DifficultyEvidenceSecurityError",
    "encode_difficulty_assessment_evidence",
    "decode_difficulty_assessment_evidence",
]


@dataclass(frozen=True, slots=True)
class DifficultyAssessmentEvidence:
    """An assessment value plus its evidence-envelope snapshot commit."""

    assessment: TaskDifficultyAssessment
    snapshot_commit: str


class DifficultyEvidenceError(ValueError):
    """Base class for canonical evidence failures."""


class DifficultyEvidenceInputError(DifficultyEvidenceError):
    """Raised for invalid in-memory evidence input types or values."""


class DifficultyEvidenceSchemaError(DifficultyEvidenceError):
    """Raised when bytes do not match the exact canonical evidence schema."""


class DifficultyEvidenceSecurityError(DifficultyEvidenceError):
    """Raised for unsafe byte encodings or prohibited YAML features."""


_SCHEMA_VERSION = "agentdesk.difficulty-assessment/v1"
_POLICY_VERSION = "agentdesk.difficulty-policy/v1"
_ROOT_KEYS = (
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
)
_DIMENSION_KEYS = (
    "dimension",
    "difficulty",
    "rationale_key",
    "hard_floor",
)
_POSITIVE_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SNAPSHOT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_BOM = b"\xef\xbb\xbf"


def _input_error(code: str) -> DifficultyEvidenceInputError:
    return DifficultyEvidenceInputError(f"difficulty_evidence:{code}")


def _schema_error(code: str) -> DifficultyEvidenceSchemaError:
    return DifficultyEvidenceSchemaError(f"difficulty_evidence:{code}")


def _security_error(code: str) -> DifficultyEvidenceSecurityError:
    return DifficultyEvidenceSecurityError(f"difficulty_evidence:{code}")


def _require_safe_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise _input_error(f"{field}_type")
    if not value:
        raise _input_error(f"{field}_empty")
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 or codepoint == 0x7F:
            raise _input_error(f"{field}_control_character")
    return value


def _validate_snapshot_commit(value: object) -> str:
    snapshot_commit = _require_safe_string(value, "snapshot_commit")
    if _SNAPSHOT_COMMIT_PATTERN.fullmatch(snapshot_commit) is None:
        raise _input_error("snapshot_commit_format")
    return snapshot_commit


def _quote_string(value: object, field: str) -> str:
    text = _require_safe_string(value, field)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _scalar(value: object, field: str) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        if value < 0:
            raise _input_error(f"{field}_integer_range")
        return str(value)
    if type(value) is str:
        return _quote_string(value, field)
    raise _input_error(f"{field}_scalar_type")


def _validate_assessment(assessment: object) -> TaskDifficultyAssessment:
    if type(assessment) is not TaskDifficultyAssessment:
        raise _input_error("assessment_type")

    if type(assessment.schema_version) is not str:
        raise _input_error("schema_version_type")
    if assessment.schema_version != _SCHEMA_VERSION:
        raise _input_error("schema_version_value")
    if type(assessment.assessment_id) is not str:
        raise _input_error("assessment_id_type")
    if type(assessment.task_id) is not str:
        raise _input_error("task_id_type")
    if type(assessment.revision) is not int:
        raise _input_error("revision_type")
    for field, value in (
        ("minimum_difficulty", assessment.minimum_difficulty),
        ("recommended_difficulty", assessment.recommended_difficulty),
        ("selected_difficulty", assessment.selected_difficulty),
    ):
        if type(value) is not TaskDifficulty:
            raise _input_error(f"{field}_type")
    if type(assessment.dimension_results) is not tuple:
        raise _input_error("dimension_results_type")
    if len(assessment.dimension_results) != len(AssessmentDimension):
        raise _input_error("dimension_results_count")
    for item in assessment.dimension_results:
        if type(item) is not DimensionResult:
            raise _input_error("dimension_result_type")
        if type(item.dimension) is not AssessmentDimension:
            raise _input_error("dimension_type")
        if type(item.difficulty) is not TaskDifficulty:
            raise _input_error("dimension_difficulty_type")
        if type(item.rationale_key) is not str:
            raise _input_error("rationale_key_type")
        if type(item.hard_floor) is not bool:
            raise _input_error("hard_floor_type")
    if type(assessment.override_direction) is not str:
        raise _input_error("override_direction_type")
    if assessment.override_reason is not None and type(assessment.override_reason) is not str:
        raise _input_error("override_reason_type")
    if assessment.approval_id is not None and type(assessment.approval_id) is not str:
        raise _input_error("approval_id_type")
    if type(assessment.policy_version) is not str:
        raise _input_error("policy_version_type")
    if assessment.policy_version != _POLICY_VERSION:
        raise _input_error("policy_version_value")

    try:
        recomputed = assess_task_difficulty(
            assessment_id=assessment.assessment_id,
            task_id=assessment.task_id,
            revision=assessment.revision,
            rationale_keys=tuple(
                item.rationale_key for item in assessment.dimension_results
            ),
            selected_difficulty=assessment.selected_difficulty,
            override_reason=assessment.override_reason,
            approval_id=assessment.approval_id,
        )
    except (DifficultyAssessmentError, TypeError, ValueError):
        raise _input_error("assessment_invariant") from None
    if recomputed != assessment:
        raise _input_error("assessment_invariant")
    return assessment


def encode_difficulty_assessment_evidence(
    evidence: DifficultyAssessmentEvidence,
) -> bytes:
    """Encode evidence as deterministic UTF-8 canonical YAML bytes."""
    if type(evidence) is not DifficultyAssessmentEvidence:
        raise _input_error("evidence_type")
    assessment = _validate_assessment(evidence.assessment)
    snapshot_commit = _validate_snapshot_commit(evidence.snapshot_commit)

    lines = [
        f"schema_version: {_scalar(assessment.schema_version, 'schema_version')}",
        f"assessment_id: {_scalar(assessment.assessment_id, 'assessment_id')}",
        f"task_id: {_scalar(assessment.task_id, 'task_id')}",
        f"revision: {_scalar(assessment.revision, 'revision')}",
        f"minimum_difficulty: {_scalar(assessment.minimum_difficulty.value, 'minimum_difficulty')}",
        f"recommended_difficulty: {_scalar(assessment.recommended_difficulty.value, 'recommended_difficulty')}",
        f"selected_difficulty: {_scalar(assessment.selected_difficulty.value, 'selected_difficulty')}",
        "dimension_results:",
    ]
    for item in assessment.dimension_results:
        lines.extend(
            (
                f"  - dimension: {_scalar(item.dimension.value, 'dimension')}",
                f"    difficulty: {_scalar(item.difficulty.value, 'dimension_difficulty')}",
                f"    rationale_key: {_scalar(item.rationale_key, 'rationale_key')}",
                f"    hard_floor: {_scalar(item.hard_floor, 'hard_floor')}",
            )
        )
    lines.extend(
        (
            f"override_direction: {_scalar(assessment.override_direction, 'override_direction')}",
            f"override_reason: {_scalar(assessment.override_reason, 'override_reason')}",
            f"approval_id: {_scalar(assessment.approval_id, 'approval_id')}",
            f"policy_version: {_scalar(assessment.policy_version, 'policy_version')}",
            f"snapshot_commit: {_scalar(snapshot_commit, 'snapshot_commit')}",
        )
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _parse_quoted_scalar(token: str, field: str) -> str:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise _schema_error(f"{field}_scalar")
    inner = token[1:-1]
    characters: list[str] = []
    index = 0
    while index < len(inner):
        character = inner[index]
        if character == "\\":
            index += 1
            if index >= len(inner) or inner[index] not in ('"', "\\"):
                raise _schema_error(f"{field}_escape")
            characters.append(inner[index])
        elif character == '"':
            raise _schema_error(f"{field}_quote")
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            raise _schema_error(f"{field}_control_character")
        else:
            characters.append(character)
        index += 1
    value = "".join(characters)
    if _quote_string(value, field) != token:
        raise _schema_error(f"{field}_noncanonical_scalar")
    return value


def _parse_scalar(token: str, field: str) -> object:
    if token == "null":
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    if token.startswith('"'):
        return _parse_quoted_scalar(token, field)
    if _POSITIVE_INTEGER_PATTERN.fullmatch(token) is not None:
        try:
            return int(token)
        except ValueError:
            raise _schema_error(f"{field}_integer") from None
    raise _schema_error(f"{field}_scalar")


def _parse_root_scalar(line: str, key: str) -> object:
    prefix = f"{key}: "
    if not line.startswith(prefix):
        raise _schema_error(f"root_{key}_order")
    return _parse_scalar(line[len(prefix):], key)


def _parse_dimension_scalar(line: str, key: str) -> object:
    prefix = f"    {key}: "
    if not line.startswith(prefix):
        raise _schema_error(f"dimension_{key}_order")
    return _parse_scalar(line[len(prefix):], key)


def _parse_document(data: bytes) -> tuple[object, ...]:
    if type(data) is not bytes:
        raise _input_error("encoded_type")
    if data.startswith(_BOM):
        raise _security_error("bom")
    if b"\r" in data:
        raise _security_error("carriage_return")
    if b"\t" in data:
        raise _security_error("tab")
    if b"\x00" in data:
        raise _security_error("nul")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _security_error("utf8") from None
    if not text.endswith("\n"):
        raise _schema_error("final_lf")
    if text.endswith("\n\n"):
        raise _schema_error("extra_final_lf")
    lines = text[:-1].split("\n")
    if not lines or any(line == "" for line in lines):
        raise _schema_error("blank_line")

    values: list[object] = []
    cursor = 0
    for key in _ROOT_KEYS[:8]:
        if key == "dimension_results":
            if lines[cursor] != "dimension_results:":
                raise _schema_error("root_dimension_results_order")
            cursor += 1
            dimensions: list[tuple[object, object, object, object]] = []
            for _ in AssessmentDimension:
                if cursor >= len(lines):
                    raise _schema_error("dimension_count")
                prefix = "  - dimension: "
                line = lines[cursor]
                if not line.startswith(prefix):
                    raise _schema_error("dimension_order")
                dimension = _parse_quoted_scalar(line[len(prefix):], "dimension")
                cursor += 1
                if cursor >= len(lines):
                    raise _schema_error("dimension_count")
                difficulty = _parse_dimension_scalar(lines[cursor], "difficulty")
                cursor += 1
                if cursor >= len(lines):
                    raise _schema_error("dimension_count")
                rationale_key = _parse_dimension_scalar(lines[cursor], "rationale_key")
                cursor += 1
                if cursor >= len(lines):
                    raise _schema_error("dimension_count")
                hard_floor = _parse_dimension_scalar(lines[cursor], "hard_floor")
                cursor += 1
                dimensions.append((dimension, difficulty, rationale_key, hard_floor))
            values.append(tuple(dimensions))
        else:
            if cursor >= len(lines):
                raise _schema_error("root_count")
            values.append(_parse_root_scalar(lines[cursor], key))
            cursor += 1

    for key in _ROOT_KEYS[8:]:
        if cursor >= len(lines):
            raise _schema_error("root_count")
        values.append(_parse_root_scalar(lines[cursor], key))
        cursor += 1
    if cursor != len(lines):
        raise _schema_error("trailing_content")
    return tuple(values)


def _as_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise _schema_error(f"{field}_type")
    return value


def _as_difficulty(value: object, field: str) -> TaskDifficulty:
    text = _as_string(value, field)
    try:
        return TaskDifficulty(text)
    except ValueError:
        raise _schema_error(f"{field}_enum") from None


def _rebuild_assessment(values: tuple[object, ...]) -> DifficultyAssessmentEvidence:
    (
        schema_version,
        assessment_id,
        task_id,
        revision,
        minimum_difficulty,
        recommended_difficulty,
        selected_difficulty,
        dimension_values,
        override_direction,
        override_reason,
        approval_id,
        policy_version,
        snapshot_commit,
    ) = values
    if schema_version != _SCHEMA_VERSION:
        raise _schema_error("schema_version_value")
    if policy_version != _POLICY_VERSION:
        raise _schema_error("policy_version_value")
    if type(revision) is not int:
        raise _schema_error("revision_type")
    if type(dimension_values) is not tuple or len(dimension_values) != len(AssessmentDimension):
        raise _schema_error("dimension_results_shape")
    assessment_id = _as_string(assessment_id, "assessment_id")
    task_id = _as_string(task_id, "task_id")
    override_direction = _as_string(override_direction, "override_direction")
    if override_reason is not None:
        override_reason = _as_string(override_reason, "override_reason")
    if approval_id is not None:
        approval_id = _as_string(approval_id, "approval_id")
    snapshot_commit = _as_string(snapshot_commit, "snapshot_commit")
    if _SNAPSHOT_COMMIT_PATTERN.fullmatch(snapshot_commit) is None:
        raise _schema_error("snapshot_commit_format")

    parsed_dimensions: list[tuple[str, str, str, bool]] = []
    for value in dimension_values:
        if type(value) is not tuple or len(value) != len(_DIMENSION_KEYS):
            raise _schema_error("dimension_results_shape")
        dimension, difficulty, rationale_key, hard_floor = value
        parsed_dimensions.append(
            (
                _as_string(dimension, "dimension"),
                _as_string(difficulty, "dimension_difficulty"),
                _as_string(rationale_key, "rationale_key"),
                hard_floor,
            )
        )
        if type(hard_floor) is not bool:
            raise _schema_error("hard_floor_type")

    minimum = _as_difficulty(minimum_difficulty, "minimum_difficulty")
    recommended = _as_difficulty(recommended_difficulty, "recommended_difficulty")
    selected = _as_difficulty(selected_difficulty, "selected_difficulty")
    rationale_keys = tuple(item[2] for item in parsed_dimensions)
    try:
        assessment = assess_task_difficulty(
            assessment_id=assessment_id,
            task_id=task_id,
            revision=revision,
            rationale_keys=rationale_keys,
            selected_difficulty=selected,
            override_reason=override_reason,
            approval_id=approval_id,
        )
    except (DifficultyAssessmentError, TypeError, ValueError):
        raise _schema_error("assessment_invariant") from None

    if assessment.schema_version != schema_version:
        raise _schema_error("schema_version_value")
    if assessment.minimum_difficulty is not minimum:
        raise _schema_error("minimum_difficulty_value")
    if assessment.recommended_difficulty is not recommended:
        raise _schema_error("recommended_difficulty_value")
    if assessment.override_direction != override_direction:
        raise _schema_error("override_direction_value")
    for index, value in enumerate(parsed_dimensions):
        dimension, difficulty, rationale_key, hard_floor = value
        expected = assessment.dimension_results[index]
        if (
            dimension != expected.dimension.value
            or difficulty != expected.difficulty.value
            or rationale_key != expected.rationale_key
            or hard_floor is not expected.hard_floor
        ):
            raise _schema_error("dimension_result_value")

    evidence = DifficultyAssessmentEvidence(
        assessment=assessment,
        snapshot_commit=snapshot_commit,
    )
    return evidence


def decode_difficulty_assessment_evidence(
    data: bytes,
) -> DifficultyAssessmentEvidence:
    """Decode exact canonical UTF-8 YAML bytes into frozen evidence."""
    values = _parse_document(data)
    evidence = _rebuild_assessment(values)
    if encode_difficulty_assessment_evidence(evidence) != data:
        raise _schema_error("noncanonical_encoding")
    return evidence
