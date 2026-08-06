"""Typed PM task admission preparation before materialization handoff."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import threading
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath

from core_types import TaskDifficulty, WorkerKind
from difficulty_assessment_evidence import (
    DifficultyAssessmentEvidence,
    encode_difficulty_assessment_evidence,
)
from difficulty_assessment_store import (
    DifficultyAssessmentStore,
    DifficultyAssessmentStoreRequest,
)
from difficulty_assessor import assess_task_difficulty
from dispatcher_gateway import ModelSelectionSnapshot
from pm_materialization_admission_handoff import (
    HANDOFF_SCHEMA_VERSION,
    MaterializationAdmissionPlanTemplate,
    MaterializationAdmissionRequest,
    MaterializedTaskEvidence,
    _atomic,
    _canonical,
    _content_digest,
    _mapping,
    canonical_worktree_identity,
    repository_identity,
)
from portfolio_scheduler_store import BusinessPriority, SCHEMA_VERSION
from worktree_lifecycle_store import (
    derive_worktree_id,
    expected_branch as expected_dispatch_branch,
    expected_worktree_path,
)
from select_model import (
    MODEL_BINDINGS_FILE,
    MODEL_BINDINGS_SCHEMA_VERSION,
    ROLE_POLICY_FILE,
    _load_policy_at_commit,
    _load_worktree_json_object,
    select_binding,
)

__all__ = [
    "PREPARATION_SCHEMA_VERSION",
    "AdmissionPreparationConflictError",
    "AdmissionPreparationError",
    "AdmissionPreparationHandoffInputs",
    "AdmissionPreparationInputError",
    "AdmissionPreparationOutcome",
    "AdmissionPreparationPhase",
    "AdmissionPreparationReceipt",
    "AdmissionPreparationRequest",
    "AdmissionPreparationResult",
    "PmTaskAdmissionProfile",
    "PmTaskAdmissionPreparationRuntime",
]

PREPARATION_SCHEMA_VERSION = "agentdesk.pm-task-admission-preparation/v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


class AdmissionPreparationError(Exception):
    pass


class AdmissionPreparationInputError(AdmissionPreparationError):
    pass


class AdmissionPreparationConflictError(AdmissionPreparationError):
    pass


class AdmissionPreparationPhase(str, Enum):
    PROFILE_BOUND = "PROFILE_BOUND"
    ASSESSMENT_COMMITTED = "ASSESSMENT_COMMITTED"
    MODEL_SELECTION_COMMITTED = "MODEL_SELECTION_COMMITTED"
    HANDOFF_INPUTS_BOUND = "HANDOFF_INPUTS_BOUND"
    FINALIZED = "FINALIZED"


class AdmissionPreparationOutcome(str, Enum):
    PREPARED = "PREPARED"
    REPLAYED = "REPLAYED"
    FINALIZED = "FINALIZED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REJECTED = "REJECTED"


_PHASES = tuple(AdmissionPreparationPhase)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AdmissionPreparationInputError(f"admission_preparation:{field}")
    if any(char in value for char in ("\0", "\r", "\n")):
        raise AdmissionPreparationInputError(f"admission_preparation:{field}")
    return value


def _id(value: object, field: str, prefix: str = "") -> str:
    text = _text(value, field)
    if _ID.fullmatch(text) is None or (prefix and not text.startswith(prefix)):
        raise AdmissionPreparationInputError(f"admission_preparation:{field}")
    return text


def _int(value: object, field: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise AdmissionPreparationInputError(f"admission_preparation:{field}")
    return value


def _digest(value: object, field: str) -> str:
    text = _text(value, field)
    if _DIGEST.fullmatch(text) is None:
        raise AdmissionPreparationInputError(f"admission_preparation:{field}")
    return text


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if _SHA.fullmatch(text) is None:
        raise AdmissionPreparationInputError(f"admission_preparation:{field}")
    return text


@dataclass(frozen=True, slots=True)
class PmTaskAdmissionProfile:
    schema_version: str
    profile_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    task_id: str
    revision: int
    business_priority: BusinessPriority
    difficulty_rationale_keys: tuple[str, ...]
    selected_difficulty: TaskDifficulty | None
    difficulty_override_reason: str | None
    difficulty_approval_id: str | None
    risk: str
    task_capabilities: tuple[str, ...]
    degradation_approval_id: str | None
    expected_task_attempt: int
    new_attempt: int
    prepared_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != PREPARATION_SCHEMA_VERSION:
            raise AdmissionPreparationInputError("admission_preparation:profile_schema")
        for name in ("profile_id", "project_id", "plan_id", "task_id"):
            _id(getattr(self, name), name)
        _int(self.plan_revision, "plan_revision")
        _int(self.revision, "revision")
        if type(self.business_priority) is not BusinessPriority:
            raise AdmissionPreparationInputError("admission_preparation:business_priority")
        if type(self.difficulty_rationale_keys) is not tuple or len(self.difficulty_rationale_keys) != 7:
            raise AdmissionPreparationInputError("admission_preparation:rationale_keys")
        for value in self.difficulty_rationale_keys:
            _text(value, "rationale_key")
        if self.selected_difficulty is not None and type(self.selected_difficulty) is not TaskDifficulty:
            raise AdmissionPreparationInputError("admission_preparation:selected_difficulty")
        for name in ("difficulty_override_reason", "difficulty_approval_id", "degradation_approval_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        _text(self.risk, "risk")
        if type(self.task_capabilities) is not tuple:
            raise AdmissionPreparationInputError("admission_preparation:task_capabilities")
        for value in self.task_capabilities:
            _text(value, "task_capability")
        _int(self.expected_task_attempt, "expected_task_attempt", 0)
        _int(self.new_attempt, "new_attempt", 1)
        if self.expected_task_attempt > 2 or self.new_attempt > 3 or self.new_attempt != self.expected_task_attempt + 1:
            raise AdmissionPreparationInputError("admission_preparation:attempt_limit")
        _text(self.prepared_at, "prepared_at")
        _digest(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class AdmissionPreparationRequest:
    schema_version: str
    preparation_id: str
    profile_id: str
    profile_content_digest: str
    materialized_task_content_digest: str
    repository_identity: str
    materialization_base_commit: str
    role_policy_path: str
    role_policy_commit: str
    role_policy_content_digest: str
    model_bindings_path: str
    model_bindings_content_digest: str
    expected_binding_schema: str
    expected_handoff_generation: int
    canonical_worktree_identity: str
    holder_instance_id: str
    report_path: str
    expected_head_commit: str
    expected_branch: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != PREPARATION_SCHEMA_VERSION:
            raise AdmissionPreparationInputError("admission_preparation:request_schema")
        _id(self.preparation_id, "preparation_id")
        _id(self.profile_id, "profile_id")
        for name in ("profile_content_digest", "materialized_task_content_digest", "repository_identity", "role_policy_content_digest", "model_bindings_content_digest", "canonical_worktree_identity", "content_digest"):
            _digest(getattr(self, name), name)
        _sha(self.materialization_base_commit, "materialization_base_commit")
        _sha(self.role_policy_commit, "role_policy_commit")
        _sha(self.expected_head_commit, "expected_head_commit")
        if self.role_policy_path != ROLE_POLICY_FILE.as_posix() or self.model_bindings_path != MODEL_BINDINGS_FILE.as_posix():
            raise AdmissionPreparationInputError("admission_preparation:evidence_path")
        if self.expected_binding_schema != MODEL_BINDINGS_SCHEMA_VERSION:
            raise AdmissionPreparationInputError("admission_preparation:binding_schema")
        _int(self.expected_handoff_generation, "expected_handoff_generation")
        _text(self.holder_instance_id, "holder_instance_id")
        _text(self.report_path, "report_path")
        _text(self.expected_branch, "expected_branch")


@dataclass(frozen=True, slots=True)
class AdmissionPreparationReceipt:
    schema_version: str
    receipt_id: str
    preparation_id: str
    profile_id: str
    profile_content_digest: str
    materialized_task_content_digest: str
    expected_task_attempt: int
    new_attempt: int
    assessment_id: str | None
    assessment_content_digest: str | None
    selected_difficulty: TaskDifficulty | None
    worker_kind: WorkerKind | None
    business_priority: BusinessPriority
    role_policy_commit: str
    role_policy_content_digest: str
    model_bindings_content_digest: str
    model_binding_id: str | None
    model_selection: ModelSelectionSnapshot | None
    admission_plan_template_id: str | None
    admission_plan_template_digest: str | None
    handoff_request_content_digest: str | None
    phase: AdmissionPreparationPhase
    outcome: AdmissionPreparationOutcome
    created_at: str
    assessed_at: str | None
    model_selected_at: str | None
    finalized_at: str | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != PREPARATION_SCHEMA_VERSION:
            raise AdmissionPreparationInputError("admission_preparation:receipt_schema")
        _id(self.receipt_id, "receipt_id", "APRCP-")
        _int(self.expected_task_attempt, "expected_task_attempt", 0)
        _int(self.new_attempt, "new_attempt", 1)
        if self.expected_task_attempt > 2 or self.new_attempt > 3 or self.new_attempt != self.expected_task_attempt + 1:
            raise AdmissionPreparationInputError("admission_preparation:receipt_attempt")
        if type(self.phase) is not AdmissionPreparationPhase or type(self.outcome) is not AdmissionPreparationOutcome:
            raise AdmissionPreparationInputError("admission_preparation:receipt_enum")
        _digest(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class AdmissionPreparationHandoffInputs:
    schema_version: str
    preparation_id: str
    receipt_id: str
    profile_content_digest: str
    materialized_task_content_digest: str
    expected_task_attempt: int
    new_attempt: int
    assessment_relative_path: str
    assessment_content_digest: str
    admission_plan_template: MaterializationAdmissionPlanTemplate
    materialization_admission_request: MaterializationAdmissionRequest
    created_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != PREPARATION_SCHEMA_VERSION:
            raise AdmissionPreparationInputError("admission_preparation:inputs_schema")
        _id(self.preparation_id, "preparation_id")
        _id(self.receipt_id, "receipt_id", "APRCP-")
        _digest(self.profile_content_digest, "profile_content_digest")
        _digest(self.materialized_task_content_digest, "materialized_task_content_digest")
        _int(self.expected_task_attempt, "expected_task_attempt", 0)
        _int(self.new_attempt, "new_attempt", 1)
        if self.expected_task_attempt > 2 or self.new_attempt > 3 or self.new_attempt != self.expected_task_attempt + 1:
            raise AdmissionPreparationInputError("admission_preparation:inputs_attempt")
        _text(self.assessment_relative_path, "assessment_relative_path")
        _digest(self.assessment_content_digest, "assessment_content_digest")
        if type(self.admission_plan_template) is not MaterializationAdmissionPlanTemplate or type(self.materialization_admission_request) is not MaterializationAdmissionRequest:
            raise AdmissionPreparationInputError("admission_preparation:inputs_nested_type")
        if (
            self.expected_task_attempt,
            self.new_attempt,
        ) != (
            self.admission_plan_template.expected_task_attempt,
            self.admission_plan_template.new_attempt,
        ):
            raise AdmissionPreparationInputError("admission_preparation:inputs_attempt_binding")
        _text(self.created_at, "created_at")
        _digest(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class AdmissionPreparationResult:
    receipt: AdmissionPreparationReceipt
    template: MaterializationAdmissionPlanTemplate
    handoff_request: MaterializationAdmissionRequest
    handoff_inputs: AdmissionPreparationHandoffInputs


def with_content_digest(value: object) -> object:
    return replace(value, content_digest=_content_digest(value))


def _inputs_mapping(value: AdmissionPreparationHandoffInputs, include_digest: bool = True) -> dict[str, object]:
    mapping: dict[str, object] = {
        "schema_version": value.schema_version,
        "preparation_id": value.preparation_id,
        "receipt_id": value.receipt_id,
        "profile_content_digest": value.profile_content_digest,
        "materialized_task_content_digest": value.materialized_task_content_digest,
        "expected_task_attempt": value.expected_task_attempt,
        "new_attempt": value.new_attempt,
        "assessment_relative_path": value.assessment_relative_path,
        "assessment_content_digest": value.assessment_content_digest,
        "admission_plan_template": _mapping(value.admission_plan_template),
        "materialization_admission_request": _mapping(value.materialization_admission_request),
        "created_at": value.created_at,
    }
    if include_digest:
        mapping["content_digest"] = value.content_digest
    return mapping


def _inputs_digest(value: AdmissionPreparationHandoffInputs) -> str:
    return "sha256:" + hashlib.sha256(_canonical(_inputs_mapping(value, False))).hexdigest()


def _model(snapshot: dict[str, object]) -> ModelSelectionSnapshot:
    return ModelSelectionSnapshot(
        required_model_tier=snapshot["required_model_tier"],
        required_model_capabilities=tuple(snapshot["required_model_capabilities"]),
        model_binding_id=snapshot["model_binding_id"],
        selected_model_provider=snapshot["selected_model_provider"],
        selected_model_id=snapshot["selected_model_id"],
        selected_model_tier=snapshot["selected_model_tier"],
        selected_deliberation_tier=snapshot["selected_deliberation_tier"],
        selected_context_window_tokens=snapshot["selected_context_window_tokens"],
        selected_model_capabilities=tuple(snapshot["selected_model_capabilities"]),
        model_degradation_approval_id=snapshot["model_degradation_approval_id"],
    )


_WORKER = {
    TaskDifficulty.BASIC: WorkerKind.BASIC_AGENT,
    TaskDifficulty.STANDARD: WorkerKind.STANDARD_AGENT,
    TaskDifficulty.ADVANCED: WorkerKind.ADVANCED_AGENT,
    TaskDifficulty.EXPERT: WorkerKind.EXPERT_AGENT,
}

# Task cards use the stable project role identity (R1), while the checked-in
# model policy names the corresponding capability role (DEV).  Keep the
# project identity on the handoff, but select against the policy role.
_POLICY_ROLE_BY_TASK_ROLE = {"R1": "DEV"}


def _policy_role_for_selection(policy: dict[str, object], task_role_id: str) -> str:
    roles = policy.get("roles")
    if isinstance(roles, dict) and task_role_id in roles:
        return task_role_id
    return _POLICY_ROLE_BY_TASK_ROLE.get(task_role_id, task_role_id)


def _git_command(project: Path, *arguments: str) -> list[str]:
    return ["git", "-c", f"safe.directory={project}", "-C", str(project), *arguments]


class _PreparationStore:
    def __init__(self, root: Path) -> None:
        self.root = root / ".agentdesk/runtime/admission-preparation"
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.root.resolve()), threading.Lock())

    def _save(self, path: Path, value: object) -> None:
        data = _canonical(_mapping(value))
        with self.lock:
            if path.exists():
                if path.read_bytes() == data:
                    return
                raise AdmissionPreparationConflictError("admission_preparation:divergent_replay")
            _atomic(path, data)

    def profile(self, value: PmTaskAdmissionProfile) -> None:
        self._save(self.root / "profiles" / f"{value.profile_id}.yaml", value)

    def receipt(self, value: AdmissionPreparationReceipt) -> None:
        path = self.root / "receipts" / f"{value.receipt_id}.yaml"
        data = _canonical(_mapping(value))
        with self.lock:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw["business_priority"] = BusinessPriority(raw["business_priority"])
                raw["selected_difficulty"] = None if raw["selected_difficulty"] is None else TaskDifficulty(raw["selected_difficulty"])
                raw["worker_kind"] = None if raw["worker_kind"] is None else WorkerKind(raw["worker_kind"])
                raw["phase"] = AdmissionPreparationPhase(raw["phase"])
                raw["outcome"] = AdmissionPreparationOutcome(raw["outcome"])
                if raw["model_selection"] is not None:
                    raw["model_selection"] = _model(raw["model_selection"])
                current = AdmissionPreparationReceipt(**raw)
                if current == value:
                    return
                static = (
                    "receipt_id", "preparation_id", "profile_id",
                    "profile_content_digest", "materialized_task_content_digest",
                    "expected_task_attempt", "new_attempt",
                    "business_priority", "role_policy_commit",
                    "role_policy_content_digest", "model_bindings_content_digest",
                )
                if any(getattr(current, name) != getattr(value, name) for name in static):
                    raise AdmissionPreparationConflictError("admission_preparation:receipt_identity")
                current_index = _PHASES.index(current.phase)
                new_index = _PHASES.index(value.phase)
                if new_index < current_index:
                    return
                if new_index != current_index + 1:
                    raise AdmissionPreparationConflictError("admission_preparation:phase")
            _atomic(path, data)

    def model_selection(self, preparation_id: str, value: ModelSelectionSnapshot) -> None:
        mapping = _mapping(value) if hasattr(value, "__dataclass_fields__") else {}
        data = _canonical(mapping)
        path = self.root / "model-selections" / f"{preparation_id}.yaml"
        with self.lock:
            if path.exists() and path.read_bytes() != data:
                raise AdmissionPreparationConflictError("admission_preparation:model_replay")
            if not path.exists():
                _atomic(path, data)

    def handoff_inputs(self, value: AdmissionPreparationHandoffInputs) -> None:
        path = self.root / "handoff-inputs" / f"{value.preparation_id}.yaml"
        data = _canonical(_inputs_mapping(value))
        with self.lock:
            if path.exists():
                if path.read_bytes() != data:
                    raise AdmissionPreparationConflictError("admission_preparation:handoff_inputs_replay")
                return
            _atomic(path, data)


class PmTaskAdmissionPreparationRuntime:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise AdmissionPreparationInputError("admission_preparation:project_root")
        self.root = project_root
        self.store = _PreparationStore(project_root)
        self.assessments = DifficultyAssessmentStore()

    def prepare(self, profile: PmTaskAdmissionProfile, request: AdmissionPreparationRequest, materialized: MaterializedTaskEvidence) -> AdmissionPreparationResult:
        self._validate(profile, request, materialized)
        policy, bindings, role_id, base_commit = self._load_sources(
            request, materialized
        )
        assessment_id = "ASM-" + hashlib.sha256(f"{profile.task_id}\0{profile.revision}\0{profile.content_digest}".encode()).hexdigest()[:24]
        receipt_id = "APRCP-" + hashlib.sha256(request.preparation_id.encode()).hexdigest()[:24]
        self.store.profile(profile)
        receipt = AdmissionPreparationReceipt(PREPARATION_SCHEMA_VERSION, receipt_id, request.preparation_id, profile.profile_id, profile.content_digest, materialized.content_digest, profile.expected_task_attempt, profile.new_attempt, None, None, None, None, profile.business_priority, request.role_policy_commit, request.role_policy_content_digest, request.model_bindings_content_digest, None, None, None, None, None, AdmissionPreparationPhase.PROFILE_BOUND, AdmissionPreparationOutcome.PREPARED, profile.prepared_at, None, None, None, "sha256:" + "0" * 64)
        receipt = replace(receipt, content_digest=_content_digest(receipt))
        self.store.receipt(receipt)
        assessment = assess_task_difficulty(assessment_id=assessment_id, task_id=profile.task_id, revision=profile.revision, rationale_keys=profile.difficulty_rationale_keys, selected_difficulty=profile.selected_difficulty, override_reason=profile.difficulty_override_reason, approval_id=profile.difficulty_approval_id)
        evidence = DifficultyAssessmentEvidence(
            assessment, request.materialization_base_commit
        )
        stored = self.assessments.write(DifficultyAssessmentStoreRequest(self.root, evidence, request.expected_head_commit))
        receipt = self._advance(receipt, AdmissionPreparationPhase.ASSESSMENT_COMMITTED, assessment_id=assessment_id, assessment_content_digest="sha256:" + stored.content_sha256, selected_difficulty=assessment.selected_difficulty, worker_kind=_WORKER[assessment.selected_difficulty], assessed_at=profile.prepared_at)
        selected = select_binding(
            policy,
            bindings,
            _policy_role_for_selection(policy, role_id),
            profile.risk,
            assessment.selected_difficulty.value,
            set(profile.task_capabilities),
            profile.degradation_approval_id,
        )
        model = _model(selected)
        self.store.model_selection(request.preparation_id, model)
        receipt = self._advance(receipt, AdmissionPreparationPhase.MODEL_SELECTION_COMMITTED, model_binding_id=model.model_binding_id, model_selection=model, model_selected_at=profile.prepared_at)
        generation = request.expected_handoff_generation
        stem = hashlib.sha256(f"{request.preparation_id}\0{generation}".encode()).hexdigest()[:20]
        template = MaterializationAdmissionPlanTemplate(HANDOFF_SCHEMA_VERSION, "APT-" + stem, profile.task_id, profile.revision, receipt.worker_kind, assessment_id, "DSP-" + stem, "EVT-" + stem, "MSG-" + stem, role_id, request.report_path, model, "ready", profile.expected_task_attempt, profile.new_attempt, SCHEMA_VERSION, request.holder_instance_id, request.canonical_worktree_identity, profile.prepared_at, "sha256:" + "0" * 64)
        template = replace(template, content_digest=_content_digest(template))
        assessment_digest = "sha256:" + stored.content_sha256
        handoff = MaterializationAdmissionRequest(HANDOFF_SCHEMA_VERSION, "HANDOFF-" + stem, profile.project_id, profile.plan_id, profile.task_id, profile.revision, materialized.content_digest, template.template_id, template.content_digest, profile.plan_revision, materialized.plan_content_digest, materialized.task_card_relative_path, materialized.task_card_content_digest, stored.relative_path, assessment_digest, request.repository_identity, request.expected_head_commit, base_commit, request.expected_branch, generation, profile.business_priority, receipt.worker_kind, assessment_id, SCHEMA_VERSION, profile.prepared_at, "sha256:" + "0" * 64)
        handoff = replace(handoff, content_digest=_content_digest(handoff))
        handoff_inputs = AdmissionPreparationHandoffInputs(PREPARATION_SCHEMA_VERSION, request.preparation_id, receipt.receipt_id, profile.content_digest, materialized.content_digest, profile.expected_task_attempt, profile.new_attempt, stored.relative_path, assessment_digest, template, handoff, profile.prepared_at, "sha256:" + "0" * 64)
        handoff_inputs = replace(handoff_inputs, content_digest=_inputs_digest(handoff_inputs))
        self.store.handoff_inputs(handoff_inputs)
        receipt = self._advance(receipt, AdmissionPreparationPhase.HANDOFF_INPUTS_BOUND, admission_plan_template_id=template.template_id, admission_plan_template_digest=template.content_digest, handoff_request_content_digest=handoff.content_digest)
        receipt = self._advance(receipt, AdmissionPreparationPhase.FINALIZED, outcome=AdmissionPreparationOutcome.FINALIZED, finalized_at=profile.prepared_at)
        return AdmissionPreparationResult(receipt, template, handoff, handoff_inputs)

    def _validate(self, profile: PmTaskAdmissionProfile, request: AdmissionPreparationRequest, materialized: MaterializedTaskEvidence) -> None:
        if type(profile) is not PmTaskAdmissionProfile or type(request) is not AdmissionPreparationRequest or type(materialized) is not MaterializedTaskEvidence:
            raise AdmissionPreparationInputError("admission_preparation:input_type")
        if profile.content_digest != _content_digest(profile) or request.content_digest != _content_digest(request) or materialized.content_digest != _content_digest(materialized):
            raise AdmissionPreparationConflictError("admission_preparation:digest")
        if request.profile_id != profile.profile_id or request.profile_content_digest != profile.content_digest or request.materialized_task_content_digest != materialized.content_digest:
            raise AdmissionPreparationConflictError("admission_preparation:binding")
        if (profile.project_id, profile.plan_id, profile.plan_revision, profile.task_id, profile.revision) != (materialized.project_id, materialized.plan_id, materialized.plan_revision, materialized.task_id, materialized.revision):
            raise AdmissionPreparationConflictError("admission_preparation:identity")
        if repository_identity(self.root) != request.repository_identity:
            raise AdmissionPreparationConflictError("admission_preparation:repository")
        head = subprocess.run(_git_command(self.root, "rev-parse", "HEAD"), check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        branch = subprocess.run(_git_command(self.root, "branch", "--show-current"), check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        if (
            head != request.expected_head_commit
            or request.materialization_base_commit != head
            or branch != request.expected_branch
        ):
            raise AdmissionPreparationConflictError("admission_preparation:git_cas")
        common_raw = Path(subprocess.run(_git_command(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"), check=True, capture_output=True, text=True, timeout=20).stdout.strip())
        common = str((common_raw if common_raw.is_absolute() else self.root / common_raw).resolve())
        generation = request.expected_handoff_generation
        dispatch_stem = hashlib.sha256(f"{request.preparation_id}\0{generation}".encode()).hexdigest()[:20]
        dispatch_id = "DSP-" + dispatch_stem
        dispatch_branch = expected_dispatch_branch(profile.task_id, profile.revision, profile.new_attempt, dispatch_id)
        worktree_id = derive_worktree_id(common, str(self.root), dispatch_branch, head, profile.task_id, profile.revision, profile.new_attempt, dispatch_id)
        managed_path = Path(expected_worktree_path(str(self.root), worktree_id))
        if request.canonical_worktree_identity != canonical_worktree_identity(managed_path):
            raise AdmissionPreparationConflictError("admission_preparation:worktree_identity")

    def _advance(self, receipt: AdmissionPreparationReceipt, phase: AdmissionPreparationPhase, **changes: object) -> AdmissionPreparationReceipt:
        value = replace(receipt, phase=phase, content_digest="sha256:" + "0" * 64, **changes)
        value = replace(value, content_digest=_content_digest(value))
        self.store.receipt(value)
        return value

    def _load_sources(
        self,
        request: AdmissionPreparationRequest,
        materialized: MaterializedTaskEvidence,
    ) -> tuple[dict[str, object], dict[str, object], str, str]:
        policy = _load_policy_at_commit(self.root, request.role_policy_commit)
        bindings = _load_worktree_json_object(
            self.root, MODEL_BINDINGS_FILE, "model bindings"
        )
        try:
            policy_bytes = subprocess.run(
                _git_command(self.root, "show", f"{request.role_policy_commit}:{ROLE_POLICY_FILE.as_posix()}"),
                check=True, capture_output=True, timeout=20,
            ).stdout
            card_path = self.root / PurePosixPath(
                materialized.task_card_relative_path
            )
            metadata = card_path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or bool(attributes & 0x400)
            ):
                raise AdmissionPreparationConflictError(
                    "admission_preparation:task_card_path"
                )
            card_bytes = card_path.read_bytes()
            binding_bytes = (self.root / MODEL_BINDINGS_FILE).read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdmissionPreparationConflictError(
                "admission_preparation:source_read"
            ) from exc
        observed = (
            "sha256:" + hashlib.sha256(policy_bytes).hexdigest(),
            "sha256:" + hashlib.sha256(binding_bytes).hexdigest(),
            "sha256:" + hashlib.sha256(card_bytes).hexdigest(),
        )
        expected = (
            request.role_policy_content_digest,
            request.model_bindings_content_digest,
            materialized.task_card_content_digest,
        )
        if observed != expected:
            raise AdmissionPreparationConflictError(
                "admission_preparation:source_digest"
            )
        blob = card_bytes.decode("utf-8")
        return (
            policy,
            bindings,
            self._field_from_blob(blob, "role_id"),
            _sha(self._field_from_blob(blob, "base_commit"), "base_commit"),
        )

    @staticmethod
    def _field_from_blob(blob: str, name: str) -> str:
        for line in blob.splitlines():
            if line.startswith(name + ":"):
                return _text(line.split(":", 1)[1].strip(), name)
        raise AdmissionPreparationConflictError(
            f"admission_preparation:task_{name}"
        )
