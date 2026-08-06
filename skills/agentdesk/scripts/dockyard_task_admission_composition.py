"""Dockyard materialized-task to admission owner composition."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

from core_types import WorkerKind
from dockyard_plan_store import DockyardPlanRecord, DockyardPlanTask
from dockyard_post_admission_worker import DockyardPostAdmissionWorkerRuntime
from pm_materialization_admission_handoff import (
    DispatchApprovalAuthorization,
    MaterializationAdmissionPhase,
    MaterializationAdmissionReceipt,
    MaterializationAdmissionRuntime,
    MaterializedTaskEvidence,
    canonical_worktree_identity,
    repository_identity,
    with_content_digest as with_handoff_digest,
)
from pm_task_admission_preparation import (
    PREPARATION_SCHEMA_VERSION,
    AdmissionPreparationRequest,
    PmTaskAdmissionProfile,
    PmTaskAdmissionPreparationRuntime,
    with_content_digest,
)
from portfolio_scheduler_policy import AdmissionContext, AdvisoryAuthorization
from portfolio_scheduler_store import (
    SCHEMA_VERSION as SCHEDULER_SCHEMA_VERSION,
    ConflictKeyClass,
    PortfolioSchedulerStore,
    QueuePhase,
    encode_queue_entry,
)
from select_model import MODEL_BINDINGS_FILE, MODEL_BINDINGS_SCHEMA_VERSION, ROLE_POLICY_FILE
from state_provider import StateProvider
from worker_slot_lease import read_worker_slot_leases
from worktree_lifecycle_store import (
    derive_worktree_id,
    expected_branch as expected_dispatch_branch,
    expected_worktree_path,
)

__all__ = [
    "COMPOSITION_SCHEMA_VERSION",
    "DockyardAdmissionCompositionConflictError",
    "DockyardAdmissionCompositionError",
    "DockyardAdmissionCompositionInputError",
    "DockyardAdmissionCompositionRecoveryRequired",
    "DockyardAdmissionContextSnapshot",
    "DockyardTaskAdmissionCompositionRuntime",
    "DockyardTaskAdmissionOutcome",
    "DockyardTaskAdmissionPhase",
    "DockyardTaskAdmissionProgress",
]

COMPOSITION_SCHEMA_VERSION = "dockyard.task-admission-composition/v1"
_LOCKS: dict[str, threading.Lock] = {}
_EXEC_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()
_KINDS = tuple(WorkerKind)


class DockyardAdmissionCompositionError(Exception):
    pass


class DockyardAdmissionCompositionInputError(DockyardAdmissionCompositionError):
    pass


class DockyardAdmissionCompositionConflictError(DockyardAdmissionCompositionError):
    pass


class DockyardAdmissionCompositionRecoveryRequired(DockyardAdmissionCompositionError):
    pass


class DockyardTaskAdmissionPhase(str, Enum):
    MATERIALIZED = "MATERIALIZED"
    DEPENDENCIES_READY = "DEPENDENCIES_READY"
    PREPARED = "PREPARED"
    HANDOFF_STARTED = "HANDOFF_STARTED"
    ADMITTED = "ADMITTED"
    FINALIZED = "FINALIZED"


class DockyardTaskAdmissionOutcome(str, Enum):
    RESERVED = "RESERVED"
    WAITING_DEPENDENCIES = "WAITING_DEPENDENCIES"
    READY = "READY"
    PREPARED = "PREPARED"
    ADMITTED = "ADMITTED"
    FINALIZED = "FINALIZED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class DockyardAdmissionContextSnapshot:
    schema_version: str
    snapshot_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    canonical_snapshot_commit: str
    scheduler_policy_version: str
    evaluated_at: str
    active_hard_conflict_keys: tuple[str, ...]
    advisory_conflict_keys: tuple[str, ...]
    advisory_authorizations: tuple[AdvisoryAuthorization, ...]
    available_worker_kinds: tuple[WorkerKind, ...]
    worker_slot_evidence_digest: str
    scheduler_evidence_digest: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != COMPOSITION_SCHEMA_VERSION:
            raise DockyardAdmissionCompositionInputError("composition:context_schema")
        if type(self.plan_revision) is not int or self.plan_revision < 1:
            raise DockyardAdmissionCompositionInputError("composition:context_revision")
        if type(self.advisory_authorizations) is not tuple or any(type(item) is not AdvisoryAuthorization for item in self.advisory_authorizations):
            raise DockyardAdmissionCompositionInputError("composition:context_authorizations")
        if type(self.available_worker_kinds) is not tuple or any(type(item) is not WorkerKind for item in self.available_worker_kinds):
            raise DockyardAdmissionCompositionInputError("composition:context_worker_kinds")
        for values in (self.active_hard_conflict_keys, self.advisory_conflict_keys):
            if type(values) is not tuple or any(type(item) is not str or not item for item in values):
                raise DockyardAdmissionCompositionInputError("composition:context_conflicts")


@dataclass(frozen=True, slots=True)
class DockyardTaskAdmissionProgress:
    schema_version: str
    progress_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    plan_content_digest: str
    dispatch_authorization_content_digest: str
    task_id: str
    revision: int
    dependency_task_ids: tuple[str, ...]
    dependency_evidence_digest: str
    context_snapshot_id: str
    context_snapshot_content_digest: str
    preparation_id: str
    preparation_receipt_id: str | None
    handoff_id: str
    handoff_receipt_id: str | None
    expected_task_attempt: int
    new_attempt: int
    phase: DockyardTaskAdmissionPhase
    outcome: DockyardTaskAdmissionOutcome
    created_at: str
    updated_at: str
    finalized_at: str | None
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != COMPOSITION_SCHEMA_VERSION:
            raise DockyardAdmissionCompositionInputError("composition:progress_schema")
        if (
            type(self.expected_task_attempt) is not int
            or type(self.new_attempt) is not int
            or not 0 <= self.expected_task_attempt <= 2
            or self.new_attempt != self.expected_task_attempt + 1
            or self.new_attempt > 3
        ):
            raise DockyardAdmissionCompositionInputError("composition:attempt_limit")


_PHASES = tuple(DockyardTaskAdmissionPhase)


def _canonical(value: object) -> bytes:
    def default(item: object) -> object:
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return asdict(item)
        raise TypeError(type(item).__name__)
    return (json.dumps(value, default=default, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _with_digest(value: object) -> object:
    mapping = asdict(value)
    mapping.pop("content_digest")
    return replace(value, content_digest=_digest(mapping))


class _ProgressStore:
    def __init__(self, root: Path) -> None:
        self.root = root / ".agentdesk/runtime/dockyard-admission"
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.root.resolve()), threading.Lock())

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def save_context(self, value: DockyardAdmissionContextSnapshot) -> None:
        self._save(self.root / "context" / f"{value.snapshot_id}.yaml", value, False)

    def save_progress(self, value: DockyardTaskAdmissionProgress) -> None:
        self._save(self.root / "progress" / f"{value.progress_id}.yaml", value, True)

    def _save(self, path: Path, value: object, forward: bool) -> None:
        data = _canonical(value)
        with self.lock:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not forward:
                    if path.read_bytes() == data:
                        return
                    raise DockyardAdmissionCompositionConflictError("composition:context_replay")
                current = DockyardTaskAdmissionProgress(
                    **{**raw, "phase": DockyardTaskAdmissionPhase(raw["phase"]), "outcome": DockyardTaskAdmissionOutcome(raw["outcome"]), "dependency_task_ids": tuple(raw["dependency_task_ids"])}
                )
                if current == value:
                    return
                identity = ("progress_id", "project_id", "plan_id", "plan_revision", "plan_content_digest", "task_id", "revision", "dependency_task_ids", "preparation_id", "handoff_id", "expected_task_attempt", "new_attempt")
                if any(getattr(current, field) != getattr(value, field) for field in identity):
                    raise DockyardAdmissionCompositionConflictError("composition:progress_identity")
                if _PHASES.index(value.phase) != _PHASES.index(current.phase) + 1:
                    raise DockyardAdmissionCompositionConflictError("composition:progress_phase")
                context_changed = (
                    current.context_snapshot_id != value.context_snapshot_id
                    or current.context_snapshot_content_digest != value.context_snapshot_content_digest
                    or current.dependency_evidence_digest != value.dependency_evidence_digest
                )
                if context_changed and not (
                    current.phase is DockyardTaskAdmissionPhase.MATERIALIZED
                    and value.phase is DockyardTaskAdmissionPhase.DEPENDENCIES_READY
                ):
                    raise DockyardAdmissionCompositionConflictError("composition:progress_context")
            self._write(path, data)

    def read_progress(self, progress_id: str) -> DockyardTaskAdmissionProgress | None:
        path = self.root / "progress" / f"{progress_id}.yaml"
        with self.lock:
            if not path.exists():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        if set(raw) != {field.name for field in DockyardTaskAdmissionProgress.__dataclass_fields__.values()}:
            raise DockyardAdmissionCompositionConflictError("composition:progress_schema")
        value = DockyardTaskAdmissionProgress(**{**raw, "phase": DockyardTaskAdmissionPhase(raw["phase"]), "outcome": DockyardTaskAdmissionOutcome(raw["outcome"]), "dependency_task_ids": tuple(raw["dependency_task_ids"])})
        if value.content_digest != _with_digest(replace(value, content_digest="sha256:" + "0" * 64)).content_digest:
            raise DockyardAdmissionCompositionConflictError("composition:progress_digest")
        return value


class DockyardTaskAdmissionCompositionRuntime:
    def __init__(self, project_root: Path, *, preparation: PmTaskAdmissionPreparationRuntime | None = None, handoff: MaterializationAdmissionRuntime | None = None, post_admission: DockyardPostAdmissionWorkerRuntime | None = None) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardAdmissionCompositionInputError("composition:project_root")
        self.root = project_root
        self.preparation = preparation or PmTaskAdmissionPreparationRuntime(project_root)
        self.handoff = handoff or MaterializationAdmissionRuntime(project_root)
        if post_admission is not None and type(post_admission) is not DockyardPostAdmissionWorkerRuntime:
            raise DockyardAdmissionCompositionInputError("composition:post_admission")
        self.post_admission = post_admission
        self.scheduler_store = PortfolioSchedulerStore(project_root)
        self.store = _ProgressStore(project_root)

    def execute(self, plan: DockyardPlanRecord, task: DockyardPlanTask, evidence: MaterializedTaskEvidence, authorization: DispatchApprovalAuthorization) -> DockyardTaskAdmissionProgress:
        self._validate(plan, task, evidence)
        if type(authorization) is not DispatchApprovalAuthorization or authorization.content_digest != with_handoff_digest(replace(authorization, content_digest="sha256:" + "0" * 64)).content_digest:
            raise DockyardAdmissionCompositionInputError("composition:dispatch_authorization")
        if (authorization.plan_id, authorization.plan_revision, authorization.plan_digest, authorization.approved_at) != (plan.plan_id, plan.revision - 1, "sha256:" + plan.plan_digest, plan.approved_at):
            raise DockyardAdmissionCompositionConflictError("composition:dispatch_authorization_binding")
        key = f"{self.root.resolve()}\0{plan.plan_id}\0{plan.revision}\0{task.task_id}"
        with _LOCK_GUARD:
            execution_lock = _EXEC_LOCKS.setdefault(key, threading.Lock())
        with execution_lock:
            return self._execute(plan, task, evidence, authorization)

    def _execute(self, plan: DockyardPlanRecord, task: DockyardPlanTask, evidence: MaterializedTaskEvidence, authorization: DispatchApprovalAuthorization) -> DockyardTaskAdmissionProgress:
        context, state = self._context(plan, evidence.materialized_at)
        dependencies_ready, dependency_digest = self._dependencies(task, state)
        stem = hashlib.sha256(f"{plan.plan_id}\0{plan.revision}\0{task.task_id}".encode()).hexdigest()[:24]
        progress_id = "DAP-" + stem
        current = self.store.read_progress(progress_id)
        if current is not None and current.dispatch_authorization_content_digest != authorization.content_digest:
            raise DockyardAdmissionCompositionConflictError("composition:dispatch_authorization_replay")
        if current is not None and current.phase is DockyardTaskAdmissionPhase.FINALIZED:
            return current
        if current is not None and current.phase is DockyardTaskAdmissionPhase.HANDOFF_STARTED:
            recovered = self._recover_handoff(current, evidence.materialized_at)
            if recovered is not None:
                return recovered
            raise DockyardAdmissionCompositionRecoveryRequired("composition:handoff_recovery_required")
        progress = current or self._progress(plan, task, authorization, context, dependency_digest, progress_id, stem, evidence.materialized_at, dependencies_ready)
        if current is None:
            self.store.save_context(context)
            self.store.save_progress(progress)
        if not dependencies_ready:
            return progress
        if progress.phase is DockyardTaskAdmissionPhase.MATERIALIZED:
            self.store.save_context(context)
            progress = self._advance(
                progress, DockyardTaskAdmissionPhase.DEPENDENCIES_READY,
                DockyardTaskAdmissionOutcome.READY,
                dependency_evidence_digest=dependency_digest,
                context_snapshot_id=context.snapshot_id,
                context_snapshot_content_digest=context.content_digest,
            )
        profile, request = self._preparation_inputs(plan, task, evidence, context, stem)
        prepared = self.preparation.prepare(profile, request, evidence)
        if progress.phase is DockyardTaskAdmissionPhase.DEPENDENCIES_READY:
            progress = self._advance(progress, DockyardTaskAdmissionPhase.PREPARED, DockyardTaskAdmissionOutcome.PREPARED, preparation_receipt_id=prepared.receipt.receipt_id)
        progress = self._advance(progress, DockyardTaskAdmissionPhase.HANDOFF_STARTED, DockyardTaskAdmissionOutcome.PREPARED)
        admission_context = AdmissionContext(context.scheduler_policy_version, context.evaluated_at, context.active_hard_conflict_keys, context.advisory_conflict_keys, context.advisory_authorizations, context.available_worker_kinds, 1)
        worktree = self._worktree(prepared.template, prepared.handoff_request)
        receipt = self.handoff.execute(prepared.handoff_request, evidence, prepared.template, authorization, admission_context, worktree)
        progress = self._advance(progress, DockyardTaskAdmissionPhase.ADMITTED, DockyardTaskAdmissionOutcome.ADMITTED, handoff_receipt_id=receipt.receipt_id)
        if self.post_admission is not None:
            self.post_admission.execute(receipt)
        return self._advance(progress, DockyardTaskAdmissionPhase.FINALIZED, DockyardTaskAdmissionOutcome.FINALIZED, finalized_at=evidence.materialized_at)

    def _context(self, plan: DockyardPlanRecord, evaluated_at: str):
        scheduler_time = datetime.fromisoformat(
            evaluated_at.replace("Z", "+00:00")
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        state = StateProvider(self.root).snapshot()
        self.scheduler_store.initialize()
        entries = self.scheduler_store.enumerate_queue_snapshot()
        hard: set[str] = set()
        advisory: set[str] = set()
        for entry in entries:
            if entry.state not in (QueuePhase.SELECTED, QueuePhase.DISPATCHED):
                continue
            for key in entry.conflict_keys:
                if key.classification is ConflictKeyClass.UNKNOWN:
                    raise DockyardAdmissionCompositionConflictError("composition:unknown_conflict")
                (hard if key.classification is ConflictKeyClass.HARD_EXCLUSIVE else advisory).add(key.value)
        leases = read_worker_slot_leases(self.root)
        occupied = set(leases["leases"])
        available = tuple(kind for kind in _KINDS if any(slot.startswith(kind.value + "-") and slot not in occupied for slot in leases["slot_epochs"]))
        scheduler_digest = _digest([encode_queue_entry(entry).decode("utf-8") for entry in entries])
        lease_digest = _digest(leases)
        snapshot_id = "DACS-" + hashlib.sha256(f"{plan.plan_id}\0{plan.revision}\0{state.read_hexsha}\0{scheduler_digest}\0{lease_digest}".encode()).hexdigest()[:24]
        value = DockyardAdmissionContextSnapshot(COMPOSITION_SCHEMA_VERSION, snapshot_id, plan.project_id, plan.plan_id, plan.revision, state.read_hexsha, SCHEDULER_SCHEMA_VERSION, scheduler_time, tuple(sorted(hard)), tuple(sorted(advisory)), (), available, lease_digest, scheduler_digest, "sha256:" + "0" * 64)
        return _with_digest(value), state

    def _dependencies(self, task: DockyardPlanTask, state) -> tuple[bool, str]:
        evidence: list[str] = []
        for dependency in task.dependencies:
            matches = [item for item in state.tasks if item.task_id == dependency and item.revision == 1]
            if len(matches) != 1:
                return False, _digest((dependency, "missing"))
            item = matches[0]
            events = [event for event in state.events if event.task_id == dependency and event.revision == 1 and event.event_type == "CHANGE_INTEGRATED"]
            if item.state != "integrated" or item.integrated_commit is None or len(events) != 1:
                return False, _digest((dependency, item.state))
            if item.current_dispatch is None or events[0].dispatch_id != item.current_dispatch.dispatch_id:
                raise DockyardAdmissionCompositionConflictError("composition:dependency_divergence")
            evidence.extend((dependency, item.integrated_commit, events[0].event_id, events[0].dispatch_id or ""))
        return True, _digest(evidence)

    def _preparation_inputs(self, plan, task, evidence, context, stem):
        git = ["git", "-c", f"safe.directory={self.root}", "-C", str(self.root)]
        head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        branch = subprocess.run([*git, "branch", "--show-current"], check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        policy = subprocess.run([*git, "show", f"{head}:{ROLE_POLICY_FILE.as_posix()}"], check=True, capture_output=True, timeout=20).stdout
        bindings = (self.root / MODEL_BINDINGS_FILE).read_bytes()
        profile = PmTaskAdmissionProfile(PREPARATION_SCHEMA_VERSION, "PROFILE-" + stem, plan.project_id, plan.plan_id, plan.revision, task.task_id, 1, task.business_priority, task.difficulty_rationale_keys, task.selected_difficulty, task.difficulty_override_reason, task.difficulty_approval_id, task.risk, task.task_capabilities, task.degradation_approval_id, task.expected_task_attempt, task.new_attempt, evidence.materialized_at, "sha256:" + "0" * 64)
        profile = with_content_digest(profile)
        common_raw = Path(subprocess.run([*git, "rev-parse", "--path-format=absolute", "--git-common-dir"], check=True, capture_output=True, text=True, timeout=20).stdout.strip())
        common = str((common_raw if common_raw.is_absolute() else self.root / common_raw).resolve())
        dispatch_stem = hashlib.sha256(f"PREP-{stem}\0{1}".encode()).hexdigest()[:20]
        dispatch_id = "DSP-" + dispatch_stem
        dispatch_branch = expected_dispatch_branch(task.task_id, 1, task.new_attempt, dispatch_id)
        worktree_id = derive_worktree_id(common, str(self.root), dispatch_branch, head, task.task_id, 1, task.new_attempt, dispatch_id)
        worktree_path = Path(expected_worktree_path(str(self.root), worktree_id))
        request = AdmissionPreparationRequest(PREPARATION_SCHEMA_VERSION, "PREP-" + stem, profile.profile_id, profile.content_digest, evidence.content_digest, repository_identity(self.root), head, ROLE_POLICY_FILE.as_posix(), head, "sha256:" + hashlib.sha256(policy).hexdigest(), MODEL_BINDINGS_FILE.as_posix(), "sha256:" + hashlib.sha256(bindings).hexdigest(), MODEL_BINDINGS_SCHEMA_VERSION, 1, canonical_worktree_identity(worktree_path), "dockyard-" + plan.project_id, f"reports/{task.task_id}.md", head, branch, "sha256:" + "0" * 64)
        return profile, with_content_digest(request)

    def _worktree(self, template, handoff_request):
        common_raw = Path(subprocess.run(["git", "-c", f"safe.directory={self.root}", "-C", str(self.root), "rev-parse", "--path-format=absolute", "--git-common-dir"], check=True, capture_output=True, text=True, timeout=20).stdout.strip())
        common = str((common_raw if common_raw.is_absolute() else self.root / common_raw).resolve())
        branch = expected_dispatch_branch(handoff_request.task_id, handoff_request.revision, template.new_attempt, template.dispatch_id)
        worktree_id = derive_worktree_id(common, str(self.root), branch, handoff_request.expected_base_commit, handoff_request.task_id, handoff_request.revision, template.new_attempt, template.dispatch_id)
        path = expected_worktree_path(str(self.root), worktree_id)
        return Path(path)

    def _progress(self, plan, task, authorization, context, dependency_digest, progress_id, stem, now, ready):
        value = DockyardTaskAdmissionProgress(COMPOSITION_SCHEMA_VERSION, progress_id, plan.project_id, plan.plan_id, plan.revision, "sha256:" + plan.content_digest, authorization.content_digest, task.task_id, 1, task.dependencies, dependency_digest, context.snapshot_id, context.content_digest, "PREP-" + stem, None, "HANDOFF-" + hashlib.sha256(f"PREP-{stem}\0{1}".encode()).hexdigest()[:20], None, task.expected_task_attempt, task.new_attempt, DockyardTaskAdmissionPhase.MATERIALIZED, DockyardTaskAdmissionOutcome.READY if ready else DockyardTaskAdmissionOutcome.WAITING_DEPENDENCIES, now, now, None, "sha256:" + "0" * 64)
        return _with_digest(value)

    def _advance(self, progress, phase, outcome, **changes):
        value = replace(progress, phase=phase, outcome=outcome, updated_at=progress.created_at, content_digest="sha256:" + "0" * 64, **changes)
        value = _with_digest(value)
        self.store.save_progress(value)
        return value

    def _recover_handoff(self, progress, now):
        evidence_store = getattr(self.handoff, "evidence_store", None)
        reader = getattr(evidence_store, "read_receipt", None)
        if not callable(reader):
            return None
        receipt_id = "MAR-" + hashlib.sha256(progress.handoff_id.encode()).hexdigest()[:24]
        receipt = reader(receipt_id)
        if receipt is None:
            return None
        if (
            type(receipt) is not MaterializationAdmissionReceipt
            or receipt.phase is not MaterializationAdmissionPhase.FINALIZED
            or (receipt.handoff_id, receipt.project_id, receipt.plan_id, receipt.task_id, receipt.revision)
            != (progress.handoff_id, progress.project_id, progress.plan_id, progress.task_id, progress.revision)
        ):
            raise DockyardAdmissionCompositionConflictError("composition:handoff_receipt")
        admitted = self._advance(progress, DockyardTaskAdmissionPhase.ADMITTED, DockyardTaskAdmissionOutcome.ADMITTED, handoff_receipt_id=receipt.receipt_id)
        return self._advance(admitted, DockyardTaskAdmissionPhase.FINALIZED, DockyardTaskAdmissionOutcome.FINALIZED, finalized_at=now)

    def _validate(self, plan, task, evidence):
        if type(plan) is not DockyardPlanRecord or type(task) is not DockyardPlanTask or type(evidence) is not MaterializedTaskEvidence:
            raise DockyardAdmissionCompositionInputError("composition:input_type")
        if task not in plan.tasks or (plan.project_id, plan.plan_id, plan.revision, task.task_id) != (evidence.project_id, evidence.plan_id, evidence.plan_revision, evidence.task_id):
            raise DockyardAdmissionCompositionConflictError("composition:identity")
        if evidence.plan_content_digest != "sha256:" + plan.content_digest:
            raise DockyardAdmissionCompositionConflictError("composition:plan_digest")
        if task.new_attempt > 3:
            raise DockyardAdmissionCompositionInputError("composition:attempt_limit")
