"""Dockyard single-PM requirement and versioned plan-draft runtime."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from dockyard_plan_store import (
    DockyardPlanConflictError,
    DockyardPlanInputError,
    DockyardPlanPhase,
    DockyardPlanRecord,
    DockyardPlanStore,
    DockyardPlanTask,
)

__all__ = [
    "DockyardPlanDraftRequest",
    "DockyardPlanEditRequest",
    "DockyardPlanTransitionRequest",
    "DockyardPlanDiscardRequest",
    "DockyardPlanApprovalRequest",
    "DockyardPlanMaterializeRequest",
    "DockyardPlanMaterializeResult",
    "DockyardPmService",
]

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _text(value: object, field: str, maximum: int = 32768) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise DockyardPlanInputError(f"{field} is invalid")
    if "\x00" in value:
        raise DockyardPlanInputError(f"{field} contains NUL")
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field, 128)
    if any(not (char.isalnum() or char in "._:-") for char in text):
        raise DockyardPlanInputError(f"{field} is invalid")
    return text


def _digest(parts: tuple[str, ...]) -> str:
    encoded = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _task_digest(task: DockyardPlanTask) -> str:
    return _digest((
        task.task_id,
        task.title,
        task.description,
        *task.dependencies,
        task.execution_mode,
        task.role_id,
        task.task_type,
        task.provider_id,
        task.model_id,
        task.tier,
        str(task.budget_tokens),
        str(task.max_attempts),
        task.base_commit,
        task.business_priority.value,
        *task.difficulty_rationale_keys,
        "" if task.selected_difficulty is None else task.selected_difficulty.value,
        task.difficulty_override_reason or "",
        task.difficulty_approval_id or "",
        task.risk,
        *task.task_capabilities,
        task.degradation_approval_id or "",
        str(task.expected_task_attempt),
        str(task.new_attempt),
    ))


def _plan_digest(requirement: str, tasks: tuple[DockyardPlanTask, ...]) -> str:
    return _digest((hashlib.sha256(requirement.encode("utf-8")).hexdigest(), *tuple(
        _task_digest(task) for task in tasks
    )))


def _validate_graph(tasks: tuple[DockyardPlanTask, ...]) -> None:
    if not isinstance(tasks, tuple) or not tasks:
        raise DockyardPlanInputError("tasks must be a non-empty tuple")
    by_id = {task.task_id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise DockyardPlanInputError("task IDs must be unique")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise DockyardPlanInputError("task dependency cycle detected")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].dependencies:
            if dependency not in by_id:
                raise DockyardPlanInputError("task dependency is unknown")
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)


@dataclass(frozen=True, slots=True)
class DockyardPlanDraftRequest:
    plan_id: str
    project_id: str
    requirement: str
    tasks: tuple[DockyardPlanTask, ...]
    pm_owner_id: str
    created_at: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class DockyardPlanEditRequest:
    plan_id: str
    expected_revision: int
    expected_content_digest: str
    requirement: str
    tasks: tuple[DockyardPlanTask, ...]
    edited_at: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class DockyardPlanTransitionRequest:
    plan_id: str
    expected_revision: int
    expected_content_digest: str
    transitioned_at: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class DockyardPlanDiscardRequest:
    plan_id: str
    expected_revision: int
    expected_content_digest: str
    discarded_at: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class DockyardPlanApprovalRequest:
    plan_id: str
    expected_revision: int
    expected_content_digest: str
    expected_plan_digest: str
    expected_task_count: int
    approved_at: str
    confirmation_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class DockyardPlanMaterializeRequest:
    plan_id: str
    expected_revision: int
    expected_content_digest: str
    expected_plan_digest: str
    materialized_at: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class DockyardPlanMaterializeResult:
    plan: DockyardPlanRecord
    task_card_paths: tuple[str, ...]
    replayed: bool


def _request_digest(action: str, values: tuple[str, ...]) -> str:
    return _digest((action, *values))


def _empty_record_digest() -> str:
    return "0" * 64


def _reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


class DockyardPmService:
    def __init__(self, store: DockyardPlanStore, task_cards_root: Path) -> None:
        if not isinstance(store, DockyardPlanStore):
            raise DockyardPlanInputError("store has an invalid type")
        if not isinstance(task_cards_root, Path) or not task_cards_root.is_absolute():
            raise DockyardPlanInputError("task_cards_root must be absolute")
        task_cards_root.mkdir(parents=True, exist_ok=True)
        if not task_cards_root.is_dir() or _reparse(task_cards_root):
            raise DockyardPlanInputError("task_cards_root is not plain")
        self._store = store
        self._task_cards_root = task_cards_root

    def _latest(
        self,
        plan_id: str,
        expected_revision: int,
        expected_digest: str,
    ) -> DockyardPlanRecord:
        latest = self._store.latest(_identifier(plan_id, "plan_id"))
        if latest is None:
            raise DockyardPlanConflictError("plan does not exist")
        if latest.revision != expected_revision or latest.content_digest != expected_digest:
            raise DockyardPlanConflictError("plan CAS failed")
        return latest

    def latest(self, plan_id: str) -> DockyardPlanRecord | None:
        """Return the current immutable plan revision without changing it."""
        _identifier(plan_id, "plan_id")
        return self._store.latest(plan_id)

    def draft(self, request: DockyardPlanDraftRequest) -> DockyardPlanRecord:
        if not isinstance(request, DockyardPlanDraftRequest):
            raise DockyardPlanInputError("request has an invalid type")
        plan_id = _identifier(request.plan_id, "plan_id")
        project_id = _identifier(request.project_id, "project_id")
        requirement = _text(request.requirement, "requirement")
        pm_owner = _identifier(request.pm_owner_id, "pm_owner_id")
        operation = _identifier(request.operation_id, "operation_id")
        _validate_graph(request.tasks)
        requirement_digest = hashlib.sha256(requirement.encode("utf-8")).hexdigest()
        plan_digest = _plan_digest(requirement, request.tasks)
        request_digest = _request_digest(
            "draft", (plan_id, project_id, requirement_digest, plan_digest)
        )
        existing = self._store.latest(plan_id)
        if existing is not None:
            if existing.last_operation_id == operation and existing.request_digest == request_digest:
                return existing
            raise DockyardPlanConflictError("plan already exists")
        record = DockyardPlanRecord(
            "dockyard.pm-plan/v1",
            plan_id,
            project_id,
            1,
            DockyardPlanPhase.DRAFTED,
            requirement,
            requirement_digest,
            request.tasks,
            plan_digest,
            None,
            pm_owner,
            request.created_at,
            request.created_at,
            None,
            None,
            operation,
            request_digest,
            _empty_record_digest(),
        )
        return self._store.save(record)

    def edit(self, request: DockyardPlanEditRequest) -> DockyardPlanRecord:
        if not isinstance(request, DockyardPlanEditRequest):
            raise DockyardPlanInputError("request has an invalid type")
        requirement = _text(request.requirement, "requirement")
        _validate_graph(request.tasks)
        requirement_digest = hashlib.sha256(requirement.encode("utf-8")).hexdigest()
        plan_digest = _plan_digest(requirement, request.tasks)
        operation = _identifier(request.operation_id, "operation_id")
        request_digest = _request_digest("edit", (
            request.plan_id, str(request.expected_revision), requirement_digest, plan_digest
        ))
        observed = self._store.latest(request.plan_id)
        if observed is not None and observed.last_operation_id == operation:
            if observed.request_digest == request_digest:
                return observed
            raise DockyardPlanConflictError("divergent edit replay")
        latest = self._latest(request.plan_id, request.expected_revision, request.expected_content_digest)
        if latest.phase in {DockyardPlanPhase.APPROVAL_PENDING, DockyardPlanPhase.MATERIALIZED}:
            raise DockyardPlanConflictError("plan phase cannot be edited")
        record = replace(
            latest,
            revision=latest.revision + 1,
            phase=DockyardPlanPhase.EDITED,
            requirement=requirement,
            requirement_digest=requirement_digest,
            tasks=request.tasks,
            plan_digest=plan_digest,
            previous_revision_digest=latest.content_digest,
            updated_at=request.edited_at,
            approved_at=None,
            materialized_at=None,
            last_operation_id=operation,
            request_digest=request_digest,
            content_digest=_empty_record_digest(),
        )
        return self._store.save(record)

    def request_approval(self, request: DockyardPlanTransitionRequest) -> DockyardPlanRecord:
        if not isinstance(request, DockyardPlanTransitionRequest):
            raise DockyardPlanInputError("request has an invalid type")
        operation = _identifier(request.operation_id, "operation_id")
        request_digest = _request_digest("approval_pending", (
            request.plan_id, request.expected_content_digest, str(request.expected_revision)
        ))
        observed = self._store.latest(request.plan_id)
        if observed is not None and observed.last_operation_id == operation:
            if observed.request_digest == request_digest:
                return observed
            raise DockyardPlanConflictError("divergent approval-pending replay")
        latest = self._latest(request.plan_id, request.expected_revision, request.expected_content_digest)
        if latest.phase not in {DockyardPlanPhase.DRAFTED, DockyardPlanPhase.EDITED}:
            raise DockyardPlanConflictError("plan cannot enter approval pending")
        return self._store.save(replace(
            latest,
            revision=latest.revision + 1,
            phase=DockyardPlanPhase.APPROVAL_PENDING,
            previous_revision_digest=latest.content_digest,
            updated_at=request.transitioned_at,
            last_operation_id=operation,
            request_digest=request_digest,
            content_digest=_empty_record_digest(),
        ))

    def discard(self, request: DockyardPlanDiscardRequest) -> DockyardPlanRecord:
        """Terminally discard an unapproved draft without deleting its evidence."""
        if not isinstance(request, DockyardPlanDiscardRequest):
            raise DockyardPlanInputError("request has an invalid type")
        operation = _identifier(request.operation_id, "operation_id")
        request_digest = _request_digest("discard", (
            request.plan_id, str(request.expected_revision), request.expected_content_digest,
        ))
        observed = self._store.latest(request.plan_id)
        if observed is not None and observed.last_operation_id == operation:
            if observed.request_digest == request_digest:
                return observed
            raise DockyardPlanConflictError("divergent discard replay")
        latest = self._latest(
            request.plan_id, request.expected_revision, request.expected_content_digest,
        )
        if latest.phase not in {DockyardPlanPhase.DRAFTED, DockyardPlanPhase.EDITED}:
            raise DockyardPlanConflictError("plan cannot be discarded")
        return self._store.save(replace(
            latest,
            revision=latest.revision + 1,
            phase=DockyardPlanPhase.DISCARDED,
            previous_revision_digest=latest.content_digest,
            updated_at=request.discarded_at,
            approved_at=None,
            materialized_at=None,
            last_operation_id=operation,
            request_digest=request_digest,
            content_digest=_empty_record_digest(),
        ))

    def approve(self, request: DockyardPlanApprovalRequest) -> DockyardPlanRecord:
        if not isinstance(request, DockyardPlanApprovalRequest):
            raise DockyardPlanInputError("request has an invalid type")
        confirmation = _identifier(request.confirmation_id, "confirmation_id")
        operation = _identifier(request.operation_id, "operation_id")
        request_digest = _request_digest("approve", (
            request.plan_id,
            request.expected_plan_digest,
            str(request.expected_task_count),
            confirmation,
        ))
        observed = self._store.latest(request.plan_id)
        if observed is not None and observed.last_operation_id == operation:
            if observed.request_digest == request_digest:
                return observed
            raise DockyardPlanConflictError("divergent approval replay")
        latest = self._latest(request.plan_id, request.expected_revision, request.expected_content_digest)
        if latest.phase is not DockyardPlanPhase.APPROVAL_PENDING:
            raise DockyardPlanConflictError("plan is not approval pending")
        if latest.plan_digest != request.expected_plan_digest or len(latest.tasks) != request.expected_task_count:
            raise DockyardPlanConflictError("approval confirmation diverged")
        return self._store.save(replace(
            latest,
            revision=latest.revision + 1,
            phase=DockyardPlanPhase.APPROVED,
            previous_revision_digest=latest.content_digest,
            updated_at=request.approved_at,
            approved_at=request.approved_at,
            last_operation_id=operation,
            request_digest=request_digest,
            content_digest=_empty_record_digest(),
        ))

    def _task_card(self, task: DockyardPlanTask) -> bytes:
        dependencies = ", ".join(task.dependencies) if task.dependencies else "none"
        text = (
            "---\n"
            "schema_version: agentdesk.task-card/v2\n"
            f"task_id: {task.task_id}\n"
            "revision: 1\n"
            f"type: {task.task_type}\n"
            f"role_id: {task.role_id}\n"
            f"base_commit: {task.base_commit}\n"
            "owner_approval:\n  gate: none\n"
            "---\n\n"
            f"# {task.title}\n\n"
            f"{task.description}\n\n"
            f"- dependencies: {dependencies}\n"
            f"- execution_mode: {task.execution_mode}\n"
            f"- provider/model/tier: {task.provider_id} / {task.model_id} / {task.tier}\n"
            f"- budget_tokens: {task.budget_tokens}\n"
            f"- max_attempts: {task.max_attempts}\n"
            f"- business_priority: {task.business_priority.value}\n"
            "- difficulty_rationale_keys: "
            f"{', '.join(task.difficulty_rationale_keys)}\n"
            "- selected_difficulty: "
            f"{task.selected_difficulty.value if task.selected_difficulty is not None else 'none'}\n"
            "- difficulty_override_reason: "
            f"{task.difficulty_override_reason or 'none'}\n"
            f"- difficulty_approval_id: {task.difficulty_approval_id or 'none'}\n"
            f"- risk: {task.risk}\n"
            f"- task_capabilities: {', '.join(task.task_capabilities)}\n"
            f"- degradation_approval_id: {task.degradation_approval_id or 'none'}\n"
            f"- expected_task_attempt: {task.expected_task_attempt}\n"
            f"- new_attempt: {task.new_attempt}\n"
        )
        return text.encode("utf-8")

    def _write_card(self, task: DockyardPlanTask) -> str:
        filename = f"{task.task_id}-r1-dockyard.md"
        path = self._task_cards_root / filename
        data = self._task_card(task)
        if path.exists():
            if not path.is_file() or _reparse(path):
                raise DockyardPlanConflictError("task card path is not plain")
            if path.read_bytes() == data:
                return filename
            raise DockyardPlanConflictError("task card content diverged")
        temporary = path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except FileExistsError as exc:
            raise DockyardPlanConflictError("task card temp exists") from exc
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        return filename

    def materialize(self, request: DockyardPlanMaterializeRequest) -> DockyardPlanMaterializeResult:
        if not isinstance(request, DockyardPlanMaterializeRequest):
            raise DockyardPlanInputError("request has an invalid type")
        operation = _identifier(request.operation_id, "operation_id")
        observed = self._store.latest(request.plan_id)
        paths = () if observed is None else tuple(
            f"{task.task_id}-r1-dockyard.md" for task in observed.tasks
        )
        request_digest = _request_digest(
            "materialize", (request.plan_id, request.expected_plan_digest, *paths)
        )
        if observed is not None and observed.last_operation_id == operation:
            if observed.request_digest == request_digest and observed.phase is DockyardPlanPhase.MATERIALIZED:
                return DockyardPlanMaterializeResult(observed, paths, True)
            raise DockyardPlanConflictError("divergent materialize replay")
        latest = self._latest(request.plan_id, request.expected_revision, request.expected_content_digest)
        if latest.phase is not DockyardPlanPhase.APPROVED:
            raise DockyardPlanConflictError("plan is not approved")
        if latest.plan_digest != request.expected_plan_digest:
            raise DockyardPlanConflictError("materialize digest diverged")
        paths = tuple(self._write_card(task) for task in latest.tasks)
        request_digest = _request_digest("materialize", (
            latest.plan_id, latest.plan_digest, *paths
        ))
        materialized = self._store.save(replace(
            latest,
            revision=latest.revision + 1,
            phase=DockyardPlanPhase.MATERIALIZED,
            previous_revision_digest=latest.content_digest,
            updated_at=request.materialized_at,
            materialized_at=request.materialized_at,
            last_operation_id=operation,
            request_digest=request_digest,
            content_digest=_empty_record_digest(),
        ))
        return DockyardPlanMaterializeResult(materialized, paths, False)
