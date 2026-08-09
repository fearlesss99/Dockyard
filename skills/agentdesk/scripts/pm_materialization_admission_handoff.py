"""Durable PM-materialization to PortfolioScheduler admission handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol

from approval_gate import (
    ApprovalCheckRequest,
    ApprovalGate,
    ApprovalScope,
    ApprovalSubject,
    write_grant,
)
from core_types import WorkerKind
from dispatcher_gateway import ModelSelectionSnapshot
from portfolio_scheduler_policy import AdmissionContext
from portfolio_scheduler_runtime import (
    PortfolioAdmissionPlan,
    PortfolioSchedulerRuntime,
    PortfolioSchedulerTickOutcomeKind,
    PortfolioSchedulerTickRequest,
)
from portfolio_scheduler_store import (
    SCHEMA_VERSION as SCHEDULER_SCHEMA_VERSION,
    BusinessPriority,
    PortfolioSchedulerStore,
    QueueEntry,
    QueuePhase,
    _entry_digest,
)
from worktree_lifecycle_manager import WorktreeLifecycleManager
from worktree_lifecycle_store import (
    SCHEMA_VERSION as WORKTREE_SCHEMA_VERSION,
    WorktreeLifecycleAction,
    WorktreeLifecycleRequest,
    derive_worktree_id,
    expected_branch as expected_dispatch_branch,
    expected_worktree_path,
)

__all__ = [
    "DISPATCH_AUTHORIZATION_SCHEMA_VERSION",
    "HANDOFF_SCHEMA_VERSION",
    "DispatchApprovalAuthorization",
    "MaterializationAdmissionConflictError",
    "MaterializationAdmissionError",
    "MaterializationAdmissionFilesystemError",
    "MaterializationAdmissionInputError",
    "MaterializationAdmissionOutcome",
    "MaterializationAdmissionPhase",
    "MaterializationAdmissionPlanTemplate",
    "MaterializationAdmissionReceipt",
    "MaterializationAdmissionRequest",
    "MaterializationAdmissionRuntime",
    "MaterializedTaskEvidence",
]

HANDOFF_SCHEMA_VERSION = "agentdesk.pm-materialization-admission-handoff/v1"
DISPATCH_AUTHORIZATION_SCHEMA_VERSION = "dockyard.dispatch-approval-authorization/v1"
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_DOCKYARD_UNTRACKED_TASK = re.compile(
    r"docs/pm/tasks/TC-[0-9]{20}-r[1-9][0-9]*-dockyard\.md\Z"
)
_DOCKYARD_UNTRACKED_ASSESSMENT = re.compile(
    r"docs/pm/assessments/TC-[0-9]{20}/r[1-9][0-9]*/ASM-[0-9a-f]{24}\.yaml\Z"
)
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class MaterializationAdmissionError(Exception):
    pass


class MaterializationAdmissionInputError(MaterializationAdmissionError):
    pass


class MaterializationAdmissionConflictError(MaterializationAdmissionError):
    pass


class MaterializationAdmissionFilesystemError(MaterializationAdmissionError):
    pass


class MaterializationAdmissionPhase(str, Enum):
    MATERIALIZED = "MATERIALIZED"
    GIT_EVIDENCE_COMMITTED = "GIT_EVIDENCE_COMMITTED"
    CANONICAL_TASK_REGISTERED = "CANONICAL_TASK_REGISTERED"
    QUEUE_RESERVED = "QUEUE_RESERVED"
    ADMISSION_READY = "ADMISSION_READY"
    ADMISSION_SUBMITTED = "ADMISSION_SUBMITTED"
    FINALIZED = "FINALIZED"


class MaterializationAdmissionOutcome(str, Enum):
    RESERVED = "RESERVED"
    REPLAYED = "REPLAYED"
    FINALIZED = "FINALIZED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class DispatchApprovalAuthorization:
    schema_version: str
    plan_id: str
    plan_revision: int
    plan_digest: str
    confirmation_id: str
    approval_operation_id: str
    approved_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != DISPATCH_AUTHORIZATION_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:dispatch_authorization_schema")
        _identifier(self.plan_id, "plan_id")
        _integer(self.plan_revision, "plan_revision", 1)
        _digest_value(self.plan_digest, "plan_digest")
        _identifier(self.confirmation_id, "confirmation_id")
        _identifier(self.approval_operation_id, "approval_operation_id")
        _timestamp(self.approved_at, "approved_at")
        _digest_value(self.content_digest, "content_digest")
_PHASES = tuple(MaterializationAdmissionPhase)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    if any(char in value for char in ("\0", "\r", "\n")):
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    return value


def _identifier(value: object, field: str, prefix: str = "") -> str:
    text = _text(value, field)
    if _ID.fullmatch(text) is None or (prefix and not text.startswith(prefix)):
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    return text


def _integer(value: object, field: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    return value


def _digest_value(value: object, field: str) -> str:
    text = _text(value, field)
    if _DIGEST.fullmatch(text) is None:
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    return text


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if _SHA40.fullmatch(text) is None:
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    return text


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaterializationAdmissionInputError(f"handoff:{field}") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise MaterializationAdmissionInputError(f"handoff:{field}")
    return text


def _scheduler_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative_card(value: object) -> str:
    text = _text(value, "task_card_relative_path")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or path.parts[:3] != ("docs", "pm", "tasks"):
        raise MaterializationAdmissionInputError("handoff:task_card_relative_path")
    return text


def _relative_assessment(value: object) -> str:
    text = _text(value, "assessment_relative_path")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[:3] != ("docs", "pm", "assessments")
        or path.suffix != ".yaml"
    ):
        raise MaterializationAdmissionInputError("handoff:assessment_relative_path")
    return text


def _model_mapping(value: ModelSelectionSnapshot) -> dict[str, object]:
    if type(value) is not ModelSelectionSnapshot:
        raise MaterializationAdmissionInputError("handoff:model_selection")
    return {
        "required_model_tier": value.required_model_tier,
        "required_model_capabilities": list(value.required_model_capabilities),
        "model_binding_id": value.model_binding_id,
        "selected_model_provider": value.selected_model_provider,
        "selected_model_id": value.selected_model_id,
        "selected_model_tier": value.selected_model_tier,
        "selected_deliberation_tier": value.selected_deliberation_tier,
        "selected_context_window_tokens": value.selected_context_window_tokens,
        "selected_model_capabilities": list(value.selected_model_capabilities),
        "model_degradation_approval_id": value.model_degradation_approval_id,
    }


@dataclass(frozen=True, slots=True)
class MaterializedTaskEvidence:
    schema_version: str
    project_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    materialization_operation_id: str
    task_id: str
    revision: int
    task_card_relative_path: str
    task_card_content_digest: str
    pm_owner_id: str
    materialized_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != HANDOFF_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:schema_version")
        for name in ("project_id", "plan_id", "materialization_operation_id", "task_id", "pm_owner_id"):
            _identifier(getattr(self, name), name)
        _integer(self.plan_revision, "plan_revision", 1)
        _integer(self.revision, "revision", 1)
        _digest_value(self.plan_content_digest, "plan_content_digest")
        _relative_card(self.task_card_relative_path)
        _digest_value(self.task_card_content_digest, "task_card_content_digest")
        _timestamp(self.materialized_at, "materialized_at")
        _digest_value(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class MaterializationAdmissionPlanTemplate:
    schema_version: str
    template_id: str
    task_id: str
    revision: int
    worker_kind: WorkerKind
    assessment_id: str
    dispatch_id: str
    event_id: str
    outbox_message_id: str
    role_id: str
    report_path: str
    model_selection: ModelSelectionSnapshot
    expected_task_state: str
    expected_task_attempt: int
    new_attempt: int
    policy_version: str
    holder_instance_id: str
    canonical_worktree_identity: str
    created_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != HANDOFF_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:template_schema")
        _identifier(self.template_id, "template_id", "APT-")
        _identifier(self.task_id, "task_id")
        _integer(self.revision, "revision", 1)
        if type(self.worker_kind) is not WorkerKind:
            raise MaterializationAdmissionInputError("handoff:worker_kind")
        _identifier(self.assessment_id, "assessment_id", "ASM-")
        _identifier(self.dispatch_id, "dispatch_id", "DSP-")
        _identifier(self.event_id, "event_id", "EVT-")
        _identifier(self.outbox_message_id, "outbox_message_id", "MSG-")
        for name in ("role_id", "report_path", "holder_instance_id", "canonical_worktree_identity"):
            _text(getattr(self, name), name)
        _model_mapping(self.model_selection)
        if self.expected_task_state != "ready":
            raise MaterializationAdmissionInputError("handoff:expected_task_state")
        _integer(self.expected_task_attempt, "expected_task_attempt", 0)
        _integer(self.new_attempt, "new_attempt", 1)
        if self.new_attempt != self.expected_task_attempt + 1 or self.new_attempt > 3:
            raise MaterializationAdmissionInputError("handoff:attempt_limit")
        if self.policy_version != SCHEDULER_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:policy_version")
        _timestamp(self.created_at, "created_at")
        _digest_value(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class MaterializationAdmissionRequest:
    schema_version: str
    handoff_id: str
    project_id: str
    plan_id: str
    task_id: str
    revision: int
    materialized_task_content_digest: str
    admission_plan_template_id: str
    admission_plan_template_digest: str
    expected_plan_revision: int
    expected_plan_content_digest: str
    expected_task_card_relative_path: str
    expected_task_card_content_digest: str
    expected_assessment_relative_path: str
    expected_assessment_content_digest: str
    repository_identity: str
    expected_head_commit: str
    expected_base_commit: str
    expected_branch: str
    expected_canonical_generation: int
    business_priority: BusinessPriority
    worker_kind_request: WorkerKind
    assessment_id: str
    policy_version: str
    requested_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != HANDOFF_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:request_schema")
        for name in ("handoff_id", "project_id", "plan_id", "task_id"):
            _identifier(getattr(self, name), name)
        _integer(self.revision, "revision", 1)
        _digest_value(self.materialized_task_content_digest, "materialized_task_content_digest")
        _identifier(self.admission_plan_template_id, "admission_plan_template_id", "APT-")
        _digest_value(self.admission_plan_template_digest, "admission_plan_template_digest")
        _integer(self.expected_plan_revision, "expected_plan_revision", 1)
        _digest_value(self.expected_plan_content_digest, "expected_plan_content_digest")
        _relative_card(self.expected_task_card_relative_path)
        _digest_value(self.expected_task_card_content_digest, "expected_task_card_content_digest")
        _relative_assessment(self.expected_assessment_relative_path)
        _digest_value(self.expected_assessment_content_digest, "expected_assessment_content_digest")
        _digest_value(self.repository_identity, "repository_identity")
        _sha(self.expected_head_commit, "expected_head_commit")
        _sha(self.expected_base_commit, "expected_base_commit")
        _text(self.expected_branch, "expected_branch")
        _integer(self.expected_canonical_generation, "expected_canonical_generation", 1)
        if type(self.business_priority) is not BusinessPriority:
            raise MaterializationAdmissionInputError("handoff:business_priority")
        if type(self.worker_kind_request) is not WorkerKind:
            raise MaterializationAdmissionInputError("handoff:worker_kind_request")
        _identifier(self.assessment_id, "assessment_id", "ASM-")
        if self.policy_version != SCHEDULER_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:policy_version")
        _timestamp(self.requested_at, "requested_at")
        _digest_value(self.content_digest, "content_digest")


@dataclass(frozen=True, slots=True)
class MaterializationAdmissionReceipt:
    schema_version: str
    receipt_id: str
    handoff_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    materialization_operation_id: str
    materialized_task_content_digest: str
    admission_plan_template_id: str
    admission_plan_template_digest: str
    pm_owner_id: str
    task_id: str
    revision: int
    task_card_relative_path: str
    task_card_content_digest: str
    assessment_relative_path: str
    assessment_content_digest: str
    repository_identity: str
    base_commit: str
    branch: str
    task_card_git_commit: str | None
    canonical_task_state: str | None
    canonical_task_generation: int | None
    queue_id: str | None
    enqueue_sequence: int | None
    policy_version: str
    business_priority: BusinessPriority
    worker_kind_request: WorkerKind
    assessment_id: str
    admission_plan_content_digest: str | None
    schedule_receipt_id: str | None
    admission_plan_receipt_id: str | None
    dispatch_id: str | None
    dispatch_event_id: str | None
    phase: MaterializationAdmissionPhase
    outcome: MaterializationAdmissionOutcome
    reserved_at: str
    git_evidence_committed_at: str | None
    canonical_registered_at: str | None
    queue_reserved_at: str | None
    admission_submitted_at: str | None
    finalized_at: str | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != HANDOFF_SCHEMA_VERSION:
            raise MaterializationAdmissionInputError("handoff:receipt_schema")
        _identifier(self.receipt_id, "receipt_id", "MAR-")
        _identifier(self.handoff_id, "handoff_id")
        _identifier(self.task_id, "task_id")
        _integer(self.revision, "revision", 1)
        _relative_card(self.task_card_relative_path)
        _digest_value(self.task_card_content_digest, "task_card_content_digest")
        _relative_assessment(self.assessment_relative_path)
        _digest_value(self.assessment_content_digest, "assessment_content_digest")
        if type(self.phase) is not MaterializationAdmissionPhase:
            raise MaterializationAdmissionInputError("handoff:phase")
        if type(self.outcome) is not MaterializationAdmissionOutcome:
            raise MaterializationAdmissionInputError("handoff:outcome")
        _digest_value(self.content_digest, "content_digest")


def _value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, ModelSelectionSnapshot):
        return _model_mapping(value)
    return value


def _mapping(instance: object, include_digest: bool = True) -> dict[str, object]:
    result: dict[str, object] = {}
    for field_name in instance.__dataclass_fields__:  # type: ignore[attr-defined]
        if not include_digest and field_name == "content_digest":
            continue
        result[field_name] = _value(getattr(instance, field_name))
    return result


def _canonical(mapping: dict[str, object]) -> bytes:
    return (json.dumps(mapping, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")


def _content_digest(instance: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(_mapping(instance, False))).hexdigest()


def with_content_digest(instance: object) -> object:
    return replace(instance, content_digest=_content_digest(instance))


def repository_identity(project_root: Path) -> str:
    normalized = str(project_root.resolve()).replace("\\", "/")
    if os.name == "nt":
        normalized = normalized.casefold()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonical_worktree_identity(path: Path) -> str:
    return repository_identity(path)


def _plain(path: Path) -> bool:
    metadata = path.lstat()
    return not stat.S_ISLNK(metadata.st_mode) and not bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _plain(path.parent):
        raise MaterializationAdmissionFilesystemError("handoff:parent_reparse")
    temporary = path.with_name(path.name + ".tmp-" + hashlib.sha256(data).hexdigest()[:16])
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except FileExistsError as exc:
        raise MaterializationAdmissionConflictError("handoff:temporary_exists") from exc
    except OSError as exc:
        raise MaterializationAdmissionFilesystemError("handoff:atomic_write") from exc


class _EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root / ".agentdesk" / "runtime" / "materialization-admission"
        key = str(self.root.resolve())
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(key, threading.Lock())

    def _save(self, path: Path, value: object) -> object:
        data = _canonical(_mapping(value))
        with self.lock:
            if path.exists():
                if not path.is_file() or not _plain(path):
                    raise MaterializationAdmissionFilesystemError("handoff:evidence_not_plain")
                if path.read_bytes() == data:
                    return value
                raise MaterializationAdmissionConflictError("handoff:divergent_replay")
            _atomic(path, data)
        return value

    def template_path(self, template_id: str) -> Path:
        return self.root / "templates" / f"{template_id}.yaml"

    def receipt_path(self, receipt_id: str) -> Path:
        return self.root / "receipts" / f"{receipt_id}.yaml"

    def plan_path(self, handoff_id: str) -> Path:
        return self.root / "admission-plans" / f"{handoff_id}.yaml"

    def save_template(self, template: MaterializationAdmissionPlanTemplate) -> None:
        self._save(self.template_path(template.template_id), template)

    def read_receipt(self, receipt_id: str) -> MaterializationAdmissionReceipt | None:
        path = self.receipt_path(receipt_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["business_priority"] = BusinessPriority(raw["business_priority"])
        raw["worker_kind_request"] = WorkerKind(raw["worker_kind_request"])
        raw["phase"] = MaterializationAdmissionPhase(raw["phase"])
        raw["outcome"] = MaterializationAdmissionOutcome(raw["outcome"])
        receipt = MaterializationAdmissionReceipt(**raw)
        if _canonical(_mapping(receipt)) != path.read_bytes() or receipt.content_digest != _content_digest(receipt):
            raise MaterializationAdmissionFilesystemError("handoff:receipt_noncanonical")
        return receipt

    def save_receipt(self, receipt: MaterializationAdmissionReceipt) -> None:
        path = self.receipt_path(receipt.receipt_id)
        data = _canonical(_mapping(receipt))
        with self.lock:
            if path.exists():
                current = self.read_receipt(receipt.receipt_id)
                assert current is not None
                if current == receipt:
                    return
                old_index = _PHASES.index(current.phase)
                new_index = _PHASES.index(receipt.phase)
                if new_index != old_index + 1:
                    raise MaterializationAdmissionConflictError("handoff:phase_transition")
                static = tuple(MaterializationAdmissionReceipt.__dataclass_fields__)[:19]
                if any(getattr(current, name) != getattr(receipt, name) for name in static):
                    raise MaterializationAdmissionConflictError("handoff:receipt_identity")
            _atomic(path, data)

    def save_plan(self, handoff_id: str, plan: PortfolioAdmissionPlan) -> str:
        mapping = _plan_mapping(plan)
        digest = "sha256:" + hashlib.sha256(_canonical(mapping)).hexdigest()
        wrapper = {"plan": mapping, "content_digest": digest}
        path = self.plan_path(handoff_id)
        data = _canonical(wrapper)
        with self.lock:
            if path.exists():
                if not path.is_file() or not _plain(path):
                    raise MaterializationAdmissionFilesystemError(
                        "handoff:plan_not_plain"
                    )
                if path.read_bytes() != data:
                    raise MaterializationAdmissionConflictError(
                        "handoff:plan_divergent_replay"
                    )
            else:
                _atomic(path, data)
        return digest


def _plan_mapping(plan: PortfolioAdmissionPlan) -> dict[str, object]:
    result = asdict(plan)
    result["worker_kind"] = plan.worker_kind.value
    result["model_selection"] = _model_mapping(plan.model_selection)
    result["now"] = plan.now.isoformat().replace("+00:00", "Z")
    return result


class _SchedulerEvidence:
    __slots__ = ("schedule_receipt_id", "admission_plan_receipt_id", "dispatch_id", "event_id")

    def __init__(self, schedule_receipt_id: str, admission_plan_receipt_id: str, dispatch_id: str, event_id: str) -> None:
        self.schedule_receipt_id = schedule_receipt_id
        self.admission_plan_receipt_id = admission_plan_receipt_id
        self.dispatch_id = dispatch_id
        self.event_id = event_id


class _SchedulerGateway(Protocol):
    def submit(self, plan: PortfolioAdmissionPlan, context: AdmissionContext) -> _SchedulerEvidence: ...


class _DefaultSchedulerGateway:
    def __init__(self, root: Path, store: PortfolioSchedulerStore) -> None:
        self.runtime = PortfolioSchedulerRuntime(root, store=store)
        self.root = root

    def submit(self, plan: PortfolioAdmissionPlan, context: AdmissionContext) -> _SchedulerEvidence:
        result = self.runtime.tick(PortfolioSchedulerTickRequest(self.root, plan, context))
        if result.outcome.kind is not PortfolioSchedulerTickOutcomeKind.ADMITTED or result.admission_result is None or result.outcome.receipt is None:
            raise MaterializationAdmissionConflictError("handoff:scheduler_not_admitted")
        admission = result.admission_result
        return _SchedulerEvidence(result.outcome.receipt.receipt_id, plan.receipt_id, admission.dispatch_id, admission.event_id)


class _GitOwner:
    def _run(self, root: Path, *args: str) -> bytes:
        try:
            completed = subprocess.run(["git", "-c", f"safe.directory={root}", "-C", str(root), *args], check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MaterializationAdmissionConflictError("handoff:git") from exc
        return completed.stdout

    def bind(self, root: Path, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence) -> str:
        if repository_identity(root) != request.repository_identity:
            raise MaterializationAdmissionConflictError("handoff:repository_identity")
        branch = self._run(root, "branch", "--show-current").decode().strip()
        head = self._run(root, "rev-parse", "HEAD").decode().strip()
        if branch != request.expected_branch:
            raise MaterializationAdmissionConflictError("handoff:branch")
        self._run(root, "merge-base", "--is-ancestor", request.expected_base_commit, head)
        card = root / PurePosixPath(evidence.task_card_relative_path)
        if not card.is_file() or not _plain(card):
            raise MaterializationAdmissionConflictError("handoff:card_path")
        data = card.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != evidence.task_card_content_digest:
            raise MaterializationAdmissionConflictError("handoff:card_digest")
        assessment = root / PurePosixPath(request.expected_assessment_relative_path)
        expected_assessment_path = PurePosixPath(
            "docs", "pm", "assessments", request.task_id,
            f"r{request.revision}", f"{request.assessment_id}.yaml",
        ).as_posix()
        if request.expected_assessment_relative_path != expected_assessment_path:
            raise MaterializationAdmissionConflictError("handoff:assessment_identity")
        if not assessment.is_file() or not _plain(assessment):
            raise MaterializationAdmissionConflictError("handoff:assessment_path")
        assessment_data = assessment.read_bytes()
        if "sha256:" + hashlib.sha256(assessment_data).hexdigest() != request.expected_assessment_content_digest:
            raise MaterializationAdmissionConflictError("handoff:assessment_digest")
        if head == request.expected_head_commit:
            status = self._run(root, "status", "--porcelain", "--untracked-files=all").decode().splitlines()
            status = [
                line for line in status
                if not line[3:].replace("\\", "/").startswith(".agentdesk/runtime/")
            ]
            allowed = {evidence.task_card_relative_path, request.expected_assessment_relative_path}
            current: set[str] = set()
            unsafe: list[str] = []
            for line in status:
                path = line[3:].replace("\\", "/")
                if path in allowed:
                    current.add(path)
                    continue
                is_prior_dockyard_evidence = (
                    line[:2] == "??"
                    and (
                        _DOCKYARD_UNTRACKED_TASK.fullmatch(path) is not None
                        or _DOCKYARD_UNTRACKED_ASSESSMENT.fullmatch(path) is not None
                    )
                )
                if not is_prior_dockyard_evidence:
                    unsafe.append(line)
            if current != allowed or unsafe:
                raise MaterializationAdmissionConflictError("handoff:dirty_worktree")
            self._run(root, "add", "--", evidence.task_card_relative_path, request.expected_assessment_relative_path)
            self._run(root, "commit", "-m", f"docs: materialize {evidence.task_id} task evidence")
            head = self._run(root, "rev-parse", "HEAD").decode().strip()
        else:
            self._run(root, "merge-base", "--is-ancestor", request.expected_head_commit, head)
        committed = self._run(root, "show", f"{head}:{evidence.task_card_relative_path}")
        if committed != data:
            raise MaterializationAdmissionConflictError("handoff:committed_card")
        committed_assessment = self._run(root, "show", f"{head}:{request.expected_assessment_relative_path}")
        if committed_assessment != assessment_data:
            raise MaterializationAdmissionConflictError("handoff:committed_assessment")
        return _sha(head, "task_card_git_commit")


class _CanonicalRegistry:
    def register(self, root: Path, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence, commit: str) -> int:
        from control_plane_transition import (
            _atomic_write_bytes,
            _exclusive_state_lock,
            _render_derived_views,
            _serialize_tasks_state,
            _write_derived_views,
        )
        path = root / "docs" / "pm" / "state" / "tasks.yaml"
        with _exclusive_state_lock(root):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise MaterializationAdmissionFilesystemError("handoff:canonical_state") from exc
            tasks = state.get("tasks")
            if type(tasks) is not list:
                raise MaterializationAdmissionFilesystemError("handoff:canonical_tasks")
            expected = self._task(evidence, commit, request.requested_at)
            for current in tasks:
                if type(current) is dict and current.get("task_id") == evidence.task_id:
                    if current == expected:
                        return request.expected_canonical_generation
                    raise MaterializationAdmissionConflictError("handoff:canonical_divergence")
            tasks.append(expected)
            state["updated_at"] = request.requested_at
            encoded = _serialize_tasks_state(state)
            board, status = _render_derived_views(state)
            _atomic_write_bytes(path, encoded)
            _write_derived_views(root, board, status)
        return request.expected_canonical_generation

    @staticmethod
    def _task(evidence: MaterializedTaskEvidence, commit: str, now: str) -> dict[str, object]:
        return {
            "acceptance_path": None, "accepted_commit": None, "attempt": None,
            "blocked_attempt_valid": None, "blocked_kind": None, "blocked_owner": None,
            "blocked_reason": None, "current_dispatch": None, "delivery_state": None,
            "granted_approval_ids": None, "implementation_commit": None,
            "integrated_commit": None, "integration_state": None, "report_commit": None,
            "report_path": None, "resume_state": None, "review_after": None,
            "revision": evidence.revision, "state": "ready",
            "task_card_commit": commit, "task_card_path": evidence.task_card_relative_path,
            "task_id": evidence.task_id,
            "timestamps": {
                "accepted_at": None, "blocked_at": None, "created_at": now,
                "delivered_at": None, "dispatched_at": None, "integrated_at": None,
                "ready_at": now, "started_at": None, "updated_at": now,
            },
            "unblock_condition": None,
        }


class MaterializationAdmissionRuntime:
    def __init__(self, project_root: Path, scheduler: _SchedulerGateway | None = None) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise MaterializationAdmissionInputError("handoff:project_root")
        self.root = project_root
        self.evidence_store = _EvidenceStore(project_root)
        self.queue_store = PortfolioSchedulerStore(project_root)
        self.scheduler = scheduler or _DefaultSchedulerGateway(project_root, self.queue_store)
        self.git = _GitOwner()
        self.registry = _CanonicalRegistry()

    def execute(self, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence, template: MaterializationAdmissionPlanTemplate, authorization: DispatchApprovalAuthorization, context: AdmissionContext, canonical_worktree: Path) -> MaterializationAdmissionReceipt:
        self._validate(request, evidence, template, authorization, context, canonical_worktree)
        receipt_id = "MAR-" + hashlib.sha256(request.handoff_id.encode()).hexdigest()[:24]
        receipt = self.evidence_store.read_receipt(receipt_id)
        if receipt is None:
            self.evidence_store.save_template(template)
            receipt = self._initial(receipt_id, request, evidence, template)
            self.evidence_store.save_receipt(receipt)
        else:
            self._validate_receipt(receipt, request, evidence, template)

        if _PHASES.index(receipt.phase) < 1:
            commit = self.git.bind(self.root, request, evidence)
            receipt = self._advance(receipt, MaterializationAdmissionPhase.GIT_EVIDENCE_COMMITTED, task_card_git_commit=commit, git_evidence_committed_at=request.requested_at)
        commit = receipt.task_card_git_commit
        assert commit is not None
        self._ensure_dispatch_approval(request, template, authorization, commit)
        self._ensure_worktree(request, template, canonical_worktree)
        if receipt.phase is MaterializationAdmissionPhase.FINALIZED:
            return receipt
        if _PHASES.index(receipt.phase) < 2:
            generation = self.registry.register(self.root, request, evidence, commit)
            receipt = self._advance(receipt, MaterializationAdmissionPhase.CANONICAL_TASK_REGISTERED, canonical_task_state="ready", canonical_task_generation=generation, canonical_registered_at=request.requested_at)
        if _PHASES.index(receipt.phase) < 3:
            queue_id = "Q-" + hashlib.sha256(request.handoff_id.encode()).hexdigest()[:24]
            sequence = self.queue_store.reserve_enqueue_sequence(self.root)
            scheduler_time = _scheduler_timestamp(request.requested_at)
            provisional = QueueEntry(SCHEDULER_SCHEMA_VERSION, queue_id, request.task_id, request.revision, sequence, scheduler_time, request.business_priority, scheduler_time, request.worker_kind_request, request.assessment_id, QueuePhase.QUEUED, (), 0, "sha256:" + "0" * 64, 1)
            entry = replace(provisional, content_digest=_entry_digest(provisional))
            self.queue_store.create_queue_entry(entry, self.root)
            receipt = self._advance(receipt, MaterializationAdmissionPhase.QUEUE_RESERVED, queue_id=queue_id, enqueue_sequence=sequence, queue_reserved_at=request.requested_at)
        assert receipt.queue_id is not None and receipt.enqueue_sequence is not None
        plan = self._plan(receipt, request, evidence, template, canonical_worktree)
        plan_digest = self.evidence_store.save_plan(request.handoff_id, plan)
        if _PHASES.index(receipt.phase) < 4:
            receipt = self._advance(receipt, MaterializationAdmissionPhase.ADMISSION_READY, admission_plan_content_digest=plan_digest)
        elif receipt.admission_plan_content_digest != plan_digest:
            raise MaterializationAdmissionConflictError("handoff:plan_digest")
        result = self.scheduler.submit(plan, context)
        if _PHASES.index(receipt.phase) < 5:
            receipt = self._advance(receipt, MaterializationAdmissionPhase.ADMISSION_SUBMITTED, schedule_receipt_id=result.schedule_receipt_id, admission_plan_receipt_id=result.admission_plan_receipt_id, dispatch_id=result.dispatch_id, dispatch_event_id=result.event_id, admission_submitted_at=request.requested_at)
        if receipt.dispatch_id != result.dispatch_id or receipt.dispatch_event_id != result.event_id:
            raise MaterializationAdmissionConflictError("handoff:scheduler_divergence")
        if _PHASES.index(receipt.phase) < 6:
            receipt = self._advance(receipt, MaterializationAdmissionPhase.FINALIZED, outcome=MaterializationAdmissionOutcome.FINALIZED, finalized_at=request.requested_at)
        return receipt

    def _validate(self, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence, template: MaterializationAdmissionPlanTemplate, authorization: DispatchApprovalAuthorization, context: AdmissionContext, worktree: Path) -> None:
        if type(request) is not MaterializationAdmissionRequest or type(evidence) is not MaterializedTaskEvidence or type(template) is not MaterializationAdmissionPlanTemplate or type(authorization) is not DispatchApprovalAuthorization or type(context) is not AdmissionContext:
            raise MaterializationAdmissionInputError("handoff:input_type")
        if template.new_attempt > 3:
            raise MaterializationAdmissionInputError("handoff:attempt_limit")
        if request.materialized_task_content_digest != evidence.content_digest or request.admission_plan_template_id != template.template_id or request.admission_plan_template_digest != template.content_digest:
            raise MaterializationAdmissionConflictError("handoff:evidence_binding")
        if (request.project_id, request.plan_id, request.task_id, request.revision) != (evidence.project_id, evidence.plan_id, evidence.task_id, evidence.revision):
            raise MaterializationAdmissionConflictError("handoff:identity")
        if (request.expected_plan_revision, request.expected_plan_content_digest, request.expected_task_card_relative_path, request.expected_task_card_content_digest) != (evidence.plan_revision, evidence.plan_content_digest, evidence.task_card_relative_path, evidence.task_card_content_digest):
            raise MaterializationAdmissionConflictError("handoff:materialization")
        if (template.task_id, template.revision, template.worker_kind, template.assessment_id, template.policy_version) != (request.task_id, request.revision, request.worker_kind_request, request.assessment_id, request.policy_version):
            raise MaterializationAdmissionConflictError("handoff:template_binding")
        if authorization.plan_id != request.plan_id or authorization.plan_revision != request.expected_plan_revision - 1:
            raise MaterializationAdmissionConflictError("handoff:dispatch_authorization_plan")
        if canonical_worktree_identity(worktree) != template.canonical_worktree_identity:
            raise MaterializationAdmissionConflictError("handoff:worktree_identity")
        if request.content_digest != _content_digest(request) or evidence.content_digest != _content_digest(evidence) or template.content_digest != _content_digest(template) or authorization.content_digest != _content_digest(authorization):
            raise MaterializationAdmissionConflictError("handoff:content_digest")

    def _ensure_dispatch_approval(self, request: MaterializationAdmissionRequest, template: MaterializationAdmissionPlanTemplate, authorization: DispatchApprovalAuthorization, snapshot_commit: str) -> None:
        identity = "\x1f".join((authorization.content_digest, request.task_id, str(request.revision), str(template.new_attempt), template.dispatch_id))
        token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        approval_id = "APR-" + token
        event_id = "EVT-APR-" + token
        reason = "Dockyard confirmed plan approval"
        subject = ApprovalSubject(request.task_id, request.revision, template.new_attempt, template.dispatch_id, None)
        gate = ApprovalGate(self.root)
        checked = gate.check(ApprovalCheckRequest(ApprovalScope.DISPATCH, subject, snapshot_commit), datetime.fromisoformat(request.requested_at.replace("Z", "+00:00")))
        if checked.passed:
            existing = checked.matched_evidence
            if existing is None or (existing.approval_id, existing.event_id, existing.scope, existing.subject, existing.lease_epoch, existing.granted_at, existing.reason, existing.snapshot_commit) != (approval_id, event_id, ApprovalScope.DISPATCH, subject, request.expected_canonical_generation, authorization.approved_at, reason, snapshot_commit):
                raise MaterializationAdmissionConflictError("handoff:dispatch_approval_divergence")
            return
        if checked.failure_code != "not_found":
            raise MaterializationAdmissionConflictError("handoff:dispatch_approval_unavailable")
        try:
            write_grant(self.root, approval_id, event_id, ApprovalScope.DISPATCH, subject, request.expected_canonical_generation, datetime.fromisoformat(authorization.approved_at.replace("Z", "+00:00")), reason, None, snapshot_commit)
        except Exception as exc:
            raise MaterializationAdmissionConflictError("handoff:dispatch_approval_write") from exc

    def _ensure_worktree(self, request: MaterializationAdmissionRequest, template: MaterializationAdmissionPlanTemplate, worktree: Path) -> None:
        raw = Path(self.git._run(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
        common = str((raw if raw.is_absolute() else self.root / raw).resolve())
        branch = expected_dispatch_branch(request.task_id, request.revision, template.new_attempt, template.dispatch_id)
        worktree_id = derive_worktree_id(common, str(self.root), branch, request.expected_base_commit, request.task_id, request.revision, template.new_attempt, template.dispatch_id)
        expected_path = expected_worktree_path(str(self.root), worktree_id)
        if str(worktree) != expected_path or canonical_worktree_identity(worktree) != template.canonical_worktree_identity:
            raise MaterializationAdmissionConflictError("handoff:worktree_binding")
        timestamp = datetime.fromisoformat(request.requested_at.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        worktree_request = WorktreeLifecycleRequest(
            WORKTREE_SCHEMA_VERSION,
            "OP-WORKTREE-" + hashlib.sha256(template.dispatch_id.encode()).hexdigest()[:24],
            WorktreeLifecycleAction.CREATE, common, str(self.root), expected_path,
            branch, request.expected_base_commit, request.task_id,
            request.revision, template.new_attempt, template.dispatch_id,
            worktree_id, timestamp,
        )
        try:
            WorktreeLifecycleManager(self.root).create(worktree_request)
        except Exception as exc:
            raise MaterializationAdmissionConflictError("handoff:worktree_create") from exc

    def _initial(self, receipt_id: str, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence, template: MaterializationAdmissionPlanTemplate) -> MaterializationAdmissionReceipt:
        receipt = MaterializationAdmissionReceipt(HANDOFF_SCHEMA_VERSION, receipt_id, request.handoff_id, request.project_id, request.plan_id, evidence.plan_revision, evidence.plan_content_digest, evidence.materialization_operation_id, evidence.content_digest, template.template_id, template.content_digest, evidence.pm_owner_id, evidence.task_id, evidence.revision, evidence.task_card_relative_path, evidence.task_card_content_digest, request.expected_assessment_relative_path, request.expected_assessment_content_digest, request.repository_identity, request.expected_base_commit, request.expected_branch, None, None, None, None, None, request.policy_version, request.business_priority, request.worker_kind_request, request.assessment_id, None, None, None, None, None, MaterializationAdmissionPhase.MATERIALIZED, MaterializationAdmissionOutcome.RESERVED, request.requested_at, None, None, None, None, None, "sha256:" + "0" * 64)
        return replace(receipt, content_digest=_content_digest(receipt))

    def _validate_receipt(self, receipt: MaterializationAdmissionReceipt, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence, template: MaterializationAdmissionPlanTemplate) -> None:
        if receipt.content_digest != _content_digest(receipt) or (receipt.handoff_id, receipt.project_id, receipt.plan_id, receipt.task_id, receipt.revision, receipt.materialized_task_content_digest, receipt.admission_plan_template_id, receipt.admission_plan_template_digest, receipt.assessment_relative_path, receipt.assessment_content_digest) != (request.handoff_id, request.project_id, request.plan_id, request.task_id, request.revision, evidence.content_digest, template.template_id, template.content_digest, request.expected_assessment_relative_path, request.expected_assessment_content_digest):
            raise MaterializationAdmissionConflictError("handoff:receipt_binding")

    def _advance(self, receipt: MaterializationAdmissionReceipt, phase: MaterializationAdmissionPhase, **changes: object) -> MaterializationAdmissionReceipt:
        updated = replace(receipt, phase=phase, content_digest="sha256:" + "0" * 64, **changes)
        updated = replace(updated, content_digest=_content_digest(updated))
        self.evidence_store.save_receipt(updated)
        return updated

    def _plan(self, receipt: MaterializationAdmissionReceipt, request: MaterializationAdmissionRequest, evidence: MaterializedTaskEvidence, template: MaterializationAdmissionPlanTemplate, worktree: Path) -> PortfolioAdmissionPlan:
        assert receipt.queue_id is not None and receipt.enqueue_sequence is not None and receipt.task_card_git_commit is not None
        schedule_receipt_id = f"SR-{receipt.queue_id[2:]}-{receipt.enqueue_sequence}-g1"
        dispatch_branch = expected_dispatch_branch(evidence.task_id, evidence.revision, template.new_attempt, template.dispatch_id)
        return PortfolioAdmissionPlan(SCHEDULER_SCHEMA_VERSION, receipt.queue_id, schedule_receipt_id, evidence.task_id, evidence.revision, receipt.enqueue_sequence, 1, template.worker_kind, template.assessment_id, template.dispatch_id, template.event_id, template.outbox_message_id, template.role_id, evidence.task_card_relative_path, receipt.task_card_git_commit, request.expected_base_commit, dispatch_branch, template.report_path, template.model_selection, template.expected_task_state, template.expected_task_attempt, template.new_attempt, receipt.task_card_git_commit, template.policy_version, datetime.fromisoformat(template.created_at.replace("Z", "+00:00")), template.holder_instance_id, str(worktree))
