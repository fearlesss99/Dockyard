"""Minimal production composition for the Dockyard local control plane.

This module owns HTTP-command composition only.  It does not reimplement the
PM plan lifecycle, Scheduler, WorkflowOrchestrator, or provider runtimes.
"""

from __future__ import annotations

import hashlib
import json
import threading
import asyncio
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Callable

from dockyard_control_api import (
    DockyardCommandEnvelope,
    DockyardCommandRejected,
    DockyardCommandReceipt,
    DockyardCommandRequest,
    DockyardJsonResponse,
    DockyardReadRequest,
)
from dockyard_active_execution import (
    DockyardActiveCancellationCommand,
    DockyardActiveExecutionCallbackError,
    DockyardActiveExecutionConflictError,
    DockyardActiveExecutionNotFoundError,
    DockyardActiveExecutionRegistry,
)
from dockyard_projection import (
    DockyardProjectionConflictError,
    DockyardProjectionRequest,
    DockyardProviderEvidence,
    DockyardRunEvidence,
    _termination_event_id,
    build_dockyard_projection,
)
import dispatch_supervisor_evidence as dispatch_evidence
from dockyard_pairing import (
    DockyardAuthenticateRequest,
    DockyardDeviceScope,
    DockyardPairingStore,
)
from dockyard_plan_store import (
    DockyardPlanConflictError,
    DockyardPlanPhase,
    DockyardPlanRecord,
    DockyardPlanTask,
)
from dockyard_pm_service import (
    DockyardPlanApprovalRequest,
    DockyardPlanDiscardRequest,
    DockyardPlanDraftRequest,
    DockyardPlanEditRequest,
    DockyardPlanMaterializeRequest,
    DockyardPlanTransitionRequest,
    DockyardPmService,
)
from dockyard_project_registry import (
    DockyardProjectCommand,
    DockyardProjectConflictError,
    DockyardProjectPhase,
    DockyardProjectRegistry,
    DockyardProjectRegistryError,
)
from dockyard_delivery_receipt_store import DockyardDeliveryReceiptStore
from dockyard_mad_result_store import DockyardMadResultStore
from dockyard_redispatch_receipt_store import (
    SCHEMA_VERSION as REDISPATCH_SCHEMA_VERSION,
    DockyardRedispatchReceipt,
    DockyardRedispatchReceiptStore,
    with_content_digest as with_redispatch_digest,
)
from dockyard_post_delivery_owner_projection import digest_owner_value
from dockyard_post_delivery_review_store import (
    DockyardPostDeliveryReviewReceipt,
    DockyardPostDeliveryReviewRequest,
    DockyardPostDeliveryReviewPhase,
    DockyardPostDeliveryReviewStore,
    DockyardReviewNotFoundError,
)
from dockyard_sse import DockyardSseHub
from dockyard_task_admission_composition import DockyardTaskAdmissionCompositionRuntime
from dockyard_terminal_owner_composition import (
    DockyardTerminalCompositionError,
    DockyardTerminalOwnerCompositionRuntime,
)
from pm_materialization_admission_handoff import (
    DISPATCH_AUTHORIZATION_SCHEMA_VERSION,
    DispatchApprovalAuthorization,
    HANDOFF_SCHEMA_VERSION,
    MaterializationAdmissionPhase,
    MaterializationAdmissionRuntime,
    MaterializedTaskEvidence,
    with_content_digest as with_handoff_digest,
)
from portfolio_scheduler_store import AdmissionPlanReservation, BusinessPriority
from portfolio_scheduler_store import PortfolioSchedulerStore
from portfolio_scheduler_worker_handoff_store import PortfolioSchedulerWorkerHandoffStore
from portfolio_scheduler_worker_handoff_store import (
    PortfolioSchedulerWorkerHandoffNotFoundError,
    ScheduledDispatchHandoff,
)
from difficulty_assessment_store import DifficultyAssessmentStore
from dispatcher_gateway import DispatchIdentity, DispatchRequest, ModelSelectionSnapshot
from agent_capability_registry import (
    AgentCapabilityRegistry,
)
from pm_codex_planner import PmCodexPlanner, PmCodexPlanningError
from mad_audit_gateway import MadAuditGatewayResult
from approval_gate import (
    ApprovalCheckRequest,
    ApprovalGate,
    ApprovalScope,
    ApprovalSubject,
    write_grant,
)
from control_plane_transition import (
    AcknowledgePayload,
    CancelledPayload,
    DispatchCAS,
    DispatchFailedPayload,
    DispatchPayload,
    TransitionCAS,
    TransitionCASConflictError,
    TransitionEventContext,
    TransitionLockContentionError,
    TransitionRequest,
    ControlPlaneTransitionService,
)
from workflow_orchestrator import (
    DispatchCycleRequest,
    TaskCancellationRequest,
    WorkflowInputError,
    WorkflowInvariantError,
    WorkflowOrchestrator,
)
import workflow_orchestrator as _workflow_orchestrator
from state_provider import StateProvider, StateSnapshot

__all__ = [
    "DockyardCompositionError",
    "DockyardPairingAuthorizer",
    "DockyardLocalReadService",
    "DockyardPlanApprovalProjection",
    "DockyardPlanTaskProjection",
    "DockyardPlanProjection",
    "DockyardReviewProjection",
    "DockyardPlanReadService",
    "DockyardCompositionGateway",
]


class _DockyardWorkflowClock:
    """Adapt the gateway's UTC string clock to WorkflowOrchestrator's clock."""

    def __init__(self, now: Callable[[], str]) -> None:
        self._now = now

    def now(self) -> datetime:
        return datetime.fromisoformat(self._now().replace("Z", "+00:00"))

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class DockyardCompositionError(Exception):
    """Base error for bounded Dockyard production composition."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: object, field: str, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    if any(ord(character) < 32 for character in value):
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    return value


def _integer(value: object, field: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    return value


def _document_text(value: object, field: str, maximum: int = 32768) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    return value


def _sha(value: object) -> str:
    text = _text(value, "digest", 64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    return text


def _commit(value: object) -> str:
    text = _text(value, "commit", 40)
    if len(text) != 40 or any(character not in "0123456789abcdef" for character in text):
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    return text


def _payload(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input") from exc
    if type(value) is not dict:
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    return value


def _returned_delivery_prompt(
    task_card: str,
    audit_result: MadAuditGatewayResult,
) -> str:
    if type(task_card) is not str or not task_card.strip():
        raise ValueError("returned task card is invalid")
    if type(audit_result) is not MadAuditGatewayResult or audit_result.verdict != "fail":
        raise ValueError("returned audit result is invalid")
    issue_lines: list[str] = []
    for issue in audit_result.issues:
        issue_lines.extend((
            f"- [{issue.severity}] {issue.title}",
            f"  - 问题：{issue.description}",
            f"  - 建议：{issue.recommendation}",
            f"  - 位置：{issue.location.file}"
            + ("" if issue.location.line is None else f":{issue.location.line}"),
        ))
    issues = "\n".join(issue_lines) if issue_lines else "- 无单独 issue；以审议报告为准。"
    return (
        task_card.rstrip()
        + "\n\n---\n\n"
        + "# Dockyard MAD 返修反馈（durable evidence）\n\n"
        + audit_result.report.strip()
        + "\n\n## 结构化问题\n\n"
        + issues
        + "\n"
    )


def _returned_admission_context(
    project_root: Path,
    review: DockyardPostDeliveryReviewReceipt,
    durable: DockyardPostDeliveryReviewRequest,
) -> tuple[ScheduledDispatchHandoff, AdmissionPlanReservation, bool]:
    """Resolve the original Scheduler context for a returned attempt."""
    handoff_store = PortfolioSchedulerWorkerHandoffStore(project_root)
    plan_store = PortfolioSchedulerStore(project_root)
    try:
        handoff = handoff_store.read_handoff(review.dispatch_id)
    except PortfolioSchedulerWorkerHandoffNotFoundError:
        redispatch = DockyardRedispatchReceiptStore(project_root).read(
            review.dispatch_id
        )
        review_store = DockyardPostDeliveryReviewStore(project_root)
        source_receipt = review_store.read_receipt(redispatch.source_review_id)
        source_request = review_store.read_request(redispatch.source_review_id)
        if (
            redispatch.content_digest != durable.external_worker_receipt_digest
            or redispatch.task_id != review.task_id
            or redispatch.revision != review.revision
            or redispatch.attempt != review.attempt
            or redispatch.dispatch_id != review.dispatch_id
            or redispatch.generation_id != review.dispatch_generation_id
            or source_receipt.phase is not DockyardPostDeliveryReviewPhase.FINALIZED
            or source_receipt.audit_verdict != "fail"
            or source_receipt.content_digest
            != redispatch.source_review_receipt_digest
            or source_receipt.task_id != review.task_id
            or source_receipt.revision != review.revision
            or source_receipt.attempt + 1 != review.attempt
            or source_request.queue_id != durable.queue_id
            or source_request.schedule_receipt_id != durable.schedule_receipt_id
            or source_request.admission_plan_digest
            != durable.admission_plan_digest
            or source_request.handoff_id != durable.handoff_id
            or source_request.worktree_id != durable.worktree_id
        ):
            raise ValueError("returned redispatch source identity")
        handoff, _, _ = _returned_admission_context(
            project_root, source_receipt, source_request
        )
        source_dispatch_id = handoff.dispatch_id
        source_attempt = handoff.attempt
        redispatched = True
    else:
        source_dispatch_id = review.dispatch_id
        source_attempt = review.attempt
        redispatched = False
    plan = plan_store.read_admission_plan(handoff.queue_id, 1)
    if (
        handoff.task_id != review.task_id
        or handoff.revision != review.revision
        or handoff.attempt != source_attempt
        or handoff.dispatch_id != source_dispatch_id
        or handoff.phase.value != "FINALIZED"
        or plan.task_id != review.task_id
        or plan.revision != review.revision
        or plan.new_attempt != source_attempt
        or plan.dispatch_id != source_dispatch_id
        or plan.receipt_id != handoff.receipt_id
        or plan.content_digest != handoff.plan_digest
        or plan.receipt_id != durable.schedule_receipt_id
        or plan.content_digest != durable.admission_plan_digest
        or handoff.handoff_id != durable.handoff_id
    ):
        raise ValueError("returned scheduler source identity")
    return handoff, plan, redispatched


def _receipt_digest(
    envelope: DockyardCommandEnvelope,
    outcome: str,
    snapshot_commit: str,
    completed_at: str,
) -> str:
    values = (
        envelope.command_id,
        envelope.project_id,
        envelope.command_type,
        outcome,
        snapshot_commit,
        completed_at,
    )
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _materialize_operation_id(command_id: str) -> str:
    return "MAT-" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:60]


def _approval_pending_operation_id(command_id: str) -> str:
    return "PEND-" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:59]


class DockyardPairingAuthorizer:
    """Authorize one paired local device without exposing provider credentials."""

    def __init__(
        self,
        pairing_store: DockyardPairingStore,
        csrf_value: str,
        *,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        if type(pairing_store) is not DockyardPairingStore:
            raise TypeError("pairing_store must be DockyardPairingStore")
        self._pairing_store = pairing_store
        self._csrf_value = _text(csrf_value, "csrf_value", 192)
        self._now = now

    def authorize(
        self,
        *,
        device_id: str,
        bearer_token: str,
        csrf_value: str,
        required_scope: DockyardDeviceScope,
        operation_id: str,
    ) -> None:
        if csrf_value != self._csrf_value:
            raise DockyardCommandRejected(403, "CSRF_FORBIDDEN", "authentication")
        try:
            device = self._pairing_store.read_device(device_id)
            authenticated_at = self._now()
            if (
                device is not None
                and device.last_operation_id == operation_id
                and device.last_used_at is not None
            ):
                authenticated_at = device.last_used_at
            self._pairing_store.authenticate(DockyardAuthenticateRequest(
                device_id=device_id,
                device_token=bearer_token,
                required_scope=required_scope,
                authenticated_at=authenticated_at,
                operation_id=operation_id,
            ))
        except Exception as exc:
            raise DockyardCommandRejected(403, "AUTH_FORBIDDEN", "authentication") from exc


class DockyardLocalReadService:
    """Small safe projection used until the richer read projection is composed."""

    def __init__(self, snapshot_commit: Callable[[], str]) -> None:
        self._snapshot_commit = snapshot_commit

    def read(self, request: DockyardReadRequest) -> DockyardJsonResponse:
        snapshot = self._snapshot_commit()
        if request.endpoint == "health":
            body = {"schema_version": "dockyard.health/v1", "status": "ready", "snapshot_commit": snapshot}
        else:
            body = {
                "schema_version": "dockyard.read/v1",
                "endpoint": request.endpoint,
                "project_id": request.project_id,
                "resource_id": request.resource_id,
                "snapshot_commit": snapshot,
            }
        return DockyardJsonResponse(
            200,
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            f'"{snapshot}"',
        )


@dataclass(frozen=True, slots=True)
class DockyardPlanApprovalProjection:
    schema_version: str
    project_id: str
    snapshot_commit: str
    project_generation: int
    plan_id: str
    revision: int
    plan_digest: str
    task_count: int
    phase: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.plan-approval-projection/v1":
            raise ValueError("projection schema is invalid")
        _text(self.project_id, "project_id", 128)
        if len(self.snapshot_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.snapshot_commit
        ):
            raise ValueError("projection snapshot is invalid")
        _integer(self.project_generation, "project_generation")
        _text(self.plan_id, "plan_id", 128)
        _integer(self.revision, "revision")
        _sha(self.plan_digest)
        _integer(self.task_count, "task_count")
        if self.phase != DockyardPlanPhase.APPROVAL_PENDING.value:
            raise ValueError("projection phase is invalid")
        _sha(self.content_digest)


@dataclass(frozen=True, slots=True)
class DockyardPlanTaskProjection:
    task_id: str
    title: str
    description: str
    dependencies: tuple[str, ...]
    execution_mode: str
    role_id: str
    provider_id: str
    model_id: str
    budget_tokens: int
    max_attempts: int

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id", 128)
        _text(self.title, "title", 256)
        _document_text(self.description, "description", 32768)
        if type(self.dependencies) is not tuple:
            raise ValueError("projection dependencies are invalid")
        for dependency in self.dependencies:
            _text(dependency, "dependency", 128)
        if self.execution_mode not in {"parallel", "serial"}:
            raise ValueError("projection execution mode is invalid")
        _text(self.role_id, "role_id", 128)
        if self.provider_id not in {"claude", "codex", "reasonix"}:
            raise ValueError("projection provider is invalid")
        _text(self.model_id, "model_id", 256)
        _integer(self.budget_tokens, "budget_tokens")
        if self.max_attempts not in {1, 2, 3}:
            raise ValueError("projection max attempts are invalid")


@dataclass(frozen=True, slots=True)
class DockyardPlanProjection:
    schema_version: str
    project_id: str
    snapshot_commit: str
    project_generation: int
    plan_id: str
    revision: int
    phase: str
    requirement: str
    tasks: tuple[DockyardPlanTaskProjection, ...]
    plan_digest: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.plan-projection/v1":
            raise ValueError("plan projection schema is invalid")
        _text(self.project_id, "project_id", 128)
        if len(self.snapshot_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.snapshot_commit
        ):
            raise ValueError("plan projection snapshot is invalid")
        _integer(self.project_generation, "project_generation")
        _text(self.plan_id, "plan_id", 128)
        _integer(self.revision, "revision")
        if self.phase not in {
            DockyardPlanPhase.DRAFTED.value,
            DockyardPlanPhase.EDITED.value,
            DockyardPlanPhase.APPROVAL_PENDING.value,
        }:
            raise ValueError("plan projection phase is invalid")
        _document_text(self.requirement, "requirement", 32768)
        if type(self.tasks) is not tuple or not self.tasks:
            raise ValueError("plan projection tasks are invalid")
        if any(type(task) is not DockyardPlanTaskProjection for task in self.tasks):
            raise ValueError("plan projection task is invalid")
        _sha(self.plan_digest)
        _sha(self.content_digest)


@dataclass(frozen=True, slots=True)
class DockyardReviewProjection:
    schema_version: str
    project_id: str
    snapshot_commit: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    implementation_commit: str
    report_commit: str
    audit_verdict: str
    review_phase: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.review-projection/v1":
            raise ValueError("review projection schema is invalid")
        for value in (self.project_id, self.task_id, self.dispatch_id):
            _text(value, "review_identity", 128)
        _commit(self.snapshot_commit)
        _integer(self.revision, "revision")
        _integer(self.attempt, "attempt")
        _commit(self.implementation_commit)
        _commit(self.report_commit)
        if self.audit_verdict not in {"pass", "fail", "blocked"}:
            raise ValueError("review verdict is invalid")
        if self.review_phase != "MAD_COMPLETED":
            raise ValueError("review phase is invalid")
        _sha(self.content_digest)


def _task_projection(task: DockyardPlanTask) -> DockyardPlanTaskProjection:
    title = " ".join(task.title.split())
    if len(title) > 256:
        title = title[:253].rstrip() + "…"
    return DockyardPlanTaskProjection(
        task.task_id,
        title,
        task.description,
        task.dependencies,
        task.execution_mode,
        task.role_id,
        task.provider_id,
        task.model_id,
        task.budget_tokens,
        task.max_attempts,
    )


def _plan_projection_digest(
    project_id: str,
    snapshot_commit: str,
    generation: int,
    plan: DockyardPlanRecord,
) -> str:
    values = [
        project_id,
        snapshot_commit,
        str(generation),
        plan.plan_id,
        str(plan.revision),
        plan.phase.value,
        plan.requirement,
    ]
    for task in plan.tasks:
        title = " ".join(task.title.split())
        if len(title) > 256:
            title = title[:253].rstrip() + "…"
        values.extend((
            task.task_id,
            title,
            task.description,
            *task.dependencies,
            task.execution_mode,
            task.role_id,
            task.provider_id,
            task.model_id,
            str(task.budget_tokens),
            str(task.max_attempts),
        ))
    values.append(plan.plan_digest)
    return _projection_digest(tuple(values))


def _projection_digest(values: tuple[str, ...]) -> str:
    encoded = "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DockyardPlanReadService:
    """Expose the exact safe evidence required for the current approval."""

    def __init__(
        self,
        snapshot_commit: Callable[[], str],
        project_registry: DockyardProjectRegistry,
        pm_service: DockyardPmService,
        project_id: str,
        plan_id: str,
        terminal_owner: DockyardTerminalOwnerCompositionRuntime | None = None,
        project_root: Path | None = None,
        provider_evidence: tuple[DockyardProviderEvidence, ...] = (),
        active_registry: DockyardActiveExecutionRegistry | None = None,
        retry_provider_ids: tuple[str, ...] = (),
    ) -> None:
        if type(project_registry) is not DockyardProjectRegistry:
            raise TypeError("project_registry must be DockyardProjectRegistry")
        if type(pm_service) is not DockyardPmService:
            raise TypeError("pm_service must be DockyardPmService")
        self._snapshot_commit = snapshot_commit
        self._project_registry = project_registry
        self._pm_service = pm_service
        self._project_id = _text(project_id, "project_id", 128)
        self._plan_id = _text(plan_id, "plan_id", 128)
        if terminal_owner is not None and type(terminal_owner) is not DockyardTerminalOwnerCompositionRuntime:
            raise TypeError("terminal_owner must be DockyardTerminalOwnerCompositionRuntime or None")
        self._terminal_owner = terminal_owner
        if project_root is not None and not isinstance(project_root, Path):
            raise TypeError("project_root must be Path or None")
        self._project_root = project_root
        if type(provider_evidence) is not tuple or any(
            type(item) is not DockyardProviderEvidence for item in provider_evidence
        ):
            raise TypeError("provider_evidence must be tuple[DockyardProviderEvidence, ...]")
        self._provider_evidence = provider_evidence
        if active_registry is not None and type(active_registry) is not DockyardActiveExecutionRegistry:
            raise TypeError("active_registry must be DockyardActiveExecutionRegistry or None")
        self._active_registry = active_registry
        if type(retry_provider_ids) is not tuple:
            raise TypeError("retry_provider_ids must be tuple[str, ...]")
        normalized_retry_ids = tuple(
            _text(provider_id, "retry_provider_id", 128)
            for provider_id in retry_provider_ids
        )
        if len(set(normalized_retry_ids)) != len(normalized_retry_ids):
            raise TypeError("retry_provider_ids must not contain duplicates")
        self._retry_provider_ids = frozenset(normalized_retry_ids)

    def set_current_plan_id(self, plan_id: str) -> None:
        """Follow the PM's current immutable plan session."""
        self._plan_id = _text(plan_id, "plan_id", 128)

    def read(self, request: DockyardReadRequest) -> DockyardJsonResponse:
        snapshot = self._snapshot_commit()
        if request.endpoint == "health":
            body = {"schema_version": "dockyard.health/v1", "status": "ready", "snapshot_commit": snapshot}
            return DockyardJsonResponse(
                200,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                f'"{snapshot}"',
            )
        project = self._project_registry.read(self._project_id)
        if project is None:
            return self._not_ready("PROJECT_NOT_REGISTERED", snapshot, 409)
        if project.phase is not DockyardProjectPhase.REGISTERED:
            return self._not_ready("PROJECT_NOT_ACTIVE", snapshot, 409)
        if (
            self._project_root is not None
            and request.endpoint in {"overview", "tasks", "task", "runs", "approvals", "providers"}
        ):
            return self._read_operational(request, project.project_id, snapshot)
        if request.endpoint == "project" and request.project_id == self._project_id:
            body = {
                "schema_version": "dockyard.project-planning/v1",
                "project_id": project.project_id,
                "snapshot_commit": snapshot,
                "project_generation": project.generation,
                "plan_id": self._plan_id,
            }
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return DockyardJsonResponse(200, encoded, f'"{snapshot}"')
        if (
            request.endpoint == "plan"
            and request.project_id == self._project_id
            and request.resource_id == self._plan_id
        ):
            plan = self._pm_service.latest(self._plan_id)
            if plan is None:
                return self._not_ready("PLAN_NOT_FOUND", snapshot, 404)
            if plan.project_id != project.project_id:
                return self._not_ready("PLAN_PROJECT_DIVERGENCE", snapshot, 409)
            if plan.phase not in {
                DockyardPlanPhase.DRAFTED,
                DockyardPlanPhase.EDITED,
                DockyardPlanPhase.APPROVAL_PENDING,
            }:
                return self._not_ready("PLAN_NOT_EDITABLE", snapshot, 409)
            projection = DockyardPlanProjection(
                "dockyard.plan-projection/v1",
                project.project_id,
                snapshot,
                project.generation,
                plan.plan_id,
                plan.revision,
                plan.phase.value,
                plan.requirement,
                tuple(_task_projection(task) for task in plan.tasks),
                plan.plan_digest,
                _plan_projection_digest(project.project_id, snapshot, project.generation, plan),
            )
            body = json.dumps(asdict(projection), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return DockyardJsonResponse(200, body, f'"{projection.content_digest}"')
        if request.endpoint == "tasks" and request.project_id == self._project_id:
            if self._terminal_owner is None:
                return self._not_ready("REVIEW_NOT_READY", snapshot, 404)
            projections = []
            for receipt in self._terminal_owner.pending_reviews():
                durable = self._terminal_owner.review_request(receipt.review_id)
                values = (
                    project.project_id, snapshot, receipt.task_id, str(receipt.revision),
                    str(receipt.attempt), receipt.dispatch_id,
                    durable.implementation_commit, durable.report_commit,
                    receipt.audit_verdict or "", receipt.phase.value,
                )
                projections.append(DockyardReviewProjection(
                    "dockyard.review-projection/v1", project.project_id, snapshot,
                    receipt.task_id, receipt.revision, receipt.attempt,
                    receipt.dispatch_id, durable.implementation_commit,
                    durable.report_commit, receipt.audit_verdict or "",
                    receipt.phase.value, _projection_digest(values),
                ))
            body = {
                "schema_version": "dockyard.review-list/v1",
                "project_id": project.project_id,
                "snapshot_commit": snapshot,
                "reviews": [asdict(item) for item in projections],
            }
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return DockyardJsonResponse(200, encoded, f'"{snapshot}"')
        if request.endpoint == "task" and request.project_id == self._project_id:
            if self._terminal_owner is None or request.resource_id is None:
                return self._not_ready("REVIEW_NOT_READY", snapshot, 404)
            receipt = self._terminal_owner.pending_review(request.resource_id)
            if receipt is None:
                return self._not_ready("REVIEW_NOT_READY", snapshot, 404)
            durable = self._terminal_owner.review_request(receipt.review_id)
            values = (
                project.project_id, snapshot, receipt.task_id, str(receipt.revision),
                str(receipt.attempt), receipt.dispatch_id,
                durable.implementation_commit, durable.report_commit,
                receipt.audit_verdict or "", receipt.phase.value,
            )
            projection = DockyardReviewProjection(
                "dockyard.review-projection/v1",
                project.project_id,
                snapshot,
                receipt.task_id,
                receipt.revision,
                receipt.attempt,
                receipt.dispatch_id,
                durable.implementation_commit,
                durable.report_commit,
                receipt.audit_verdict or "",
                receipt.phase.value,
                _projection_digest(values),
            )
            body = json.dumps(asdict(projection), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return DockyardJsonResponse(200, body, f'"{projection.content_digest}"')
        if request.endpoint != "overview" or request.project_id != self._project_id:
            return self._not_ready("PROJECTION_UNAVAILABLE", snapshot, 404)
        plan = self._pm_service.latest(self._plan_id)
        if plan is None:
            return self._not_ready("PLAN_NOT_FOUND", snapshot, 409)
        if plan.project_id != project.project_id:
            return self._not_ready("PLAN_PROJECT_DIVERGENCE", snapshot, 409)
        if plan.phase is not DockyardPlanPhase.APPROVAL_PENDING:
            return self._not_ready("PLAN_NOT_APPROVAL_PENDING", snapshot, 409)
        values = (
            project.project_id,
            snapshot,
            str(project.generation),
            plan.plan_id,
            str(plan.revision),
            plan.plan_digest,
            str(len(plan.tasks)),
            plan.phase.value,
        )
        content_digest = _projection_digest(values)
        projection = DockyardPlanApprovalProjection(
            "dockyard.plan-approval-projection/v1",
            project.project_id,
            snapshot,
            project.generation,
            plan.plan_id,
            plan.revision,
            plan.plan_digest,
            len(plan.tasks),
            plan.phase.value,
            content_digest,
        )
        body = json.dumps(asdict(projection), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return DockyardJsonResponse(200, body, f'"{content_digest}"')

    def _read_operational(
        self,
        request: DockyardReadRequest,
        project_id: str,
        snapshot_commit: str,
    ) -> DockyardJsonResponse:
        assert self._project_root is not None
        snapshot = StateProvider(self._project_root).snapshot()
        if snapshot.project_id != project_id:
            return self._not_ready("SNAPSHOT_DIVERGENCE", snapshot_commit, 409)
        bundle = build_dockyard_projection(DockyardProjectionRequest(
            snapshot,
            self._run_evidence(snapshot),
            self._provider_evidence,
            (),
            snapshot.updated_at,
        ))
        reviews = self._review_projections(project_id, snapshot_commit)
        if request.endpoint == "overview":
            approval = self._approval_projection(project_id, snapshot_commit)
            body = {
                "schema_version": "dockyard.operational-overview/v1",
                "project_id": project_id,
                "snapshot_commit": snapshot_commit,
                "overview": asdict(bundle.overview),
                "plan_approval": None if approval is None else asdict(approval),
                "content_digest": bundle.content_digest,
            }
        elif request.endpoint == "tasks":
            body = {
                "schema_version": "dockyard.task-list/v1",
                "project_id": project_id,
                "snapshot_commit": snapshot_commit,
                "tasks": [asdict(item) for item in bundle.tasks],
                "reviews": [asdict(item) for item in reviews],
                "content_digest": bundle.content_digest,
            }
        elif request.endpoint == "task":
            detail = next(
                (item for item in bundle.task_details if item.summary.task_id == request.resource_id),
                None,
            )
            if detail is None:
                return self._not_ready("TASK_NOT_FOUND", snapshot_commit, 404)
            review = next(
                (item for item in reviews if item.task_id == request.resource_id),
                None,
            )
            body = {
                "schema_version": "dockyard.task-detail/v1",
                "project_id": project_id,
                "snapshot_commit": snapshot_commit,
                "task": asdict(detail),
                "review": None if review is None else asdict(review),
                "content_digest": detail.content_digest,
            }
        else:
            items = {
                "runs": bundle.runs,
                "approvals": bundle.approvals,
                "providers": bundle.providers,
            }[request.endpoint]
            body = {
                "schema_version": f"dockyard.{request.endpoint[:-1]}-list/v1",
                "project_id": project_id,
                "snapshot_commit": snapshot_commit,
                request.endpoint: [asdict(item) for item in items],
                "content_digest": bundle.content_digest,
            }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return DockyardJsonResponse(200, encoded, f'"{bundle.content_digest}"')

    def _run_evidence(self, snapshot: StateSnapshot) -> tuple[DockyardRunEvidence, ...]:
        assert self._project_root is not None
        result: list[DockyardRunEvidence] = []
        current_dispatch_ids: set[str] = set()
        for task in snapshot.tasks:
            dispatch = task.current_dispatch
            if dispatch is None or task.attempt is None:
                continue
            current_dispatch_ids.add(dispatch.dispatch_id)
            receipt = dispatch_evidence.read_dispatch_receipt(
                self._project_root,
                dispatch.dispatch_id,
            )
            if receipt is None:
                continue
            if (
                receipt.task_id != task.task_id
                or receipt.revision != task.revision
                or receipt.attempt != task.attempt
                or receipt.dispatch_id != dispatch.dispatch_id
            ):
                raise DockyardProjectionConflictError("run_receipt_identity")
            tombstone = dispatch_evidence.read_dispatch_tombstone(
                self._project_root,
                dispatch.dispatch_id,
            )
            if tombstone is not None and (
                tombstone.task_id != task.task_id
                or tombstone.revision != task.revision
                or tombstone.attempt != task.attempt
                or tombstone.dispatch_id != dispatch.dispatch_id
                or tombstone.generation_id != receipt.generation_id
            ):
                raise DockyardProjectionConflictError("run_tombstone_identity")
            finalized = tombstone is not None
            if finalized != (receipt.phase == dispatch_evidence.DispatchReceiptPhase.FINALIZED.value):
                raise DockyardProjectionConflictError("run_finalizer_divergence")
            acknowledgements = tuple(
                event for event in snapshot.events
                if event.event_type == "DISPATCH_ACKNOWLEDGED"
                and event.task_id == task.task_id
                and event.revision == task.revision
                and event.attempt == task.attempt
                and event.dispatch_id == dispatch.dispatch_id
            )
            if len(acknowledgements) > 1:
                raise DockyardProjectionConflictError("run_ack_duplicate")
            phase = receipt.phase
            if (
                phase == dispatch_evidence.DispatchReceiptPhase.WORKER_STARTED.value
                and acknowledgements
            ):
                phase = "ACKNOWLEDGED"
            active = None
            if self._active_registry is not None and not finalized and task.state == "in_progress":
                active = next(
                    (
                        identity for identity in self._active_registry.snapshot()
                        if identity.task_id == task.task_id
                        and identity.revision == task.revision
                        and identity.attempt == task.attempt
                        and identity.dispatch_id == dispatch.dispatch_id
                        and identity.generation_id == receipt.generation_id
                    ),
                    None,
                )
            process_state = "NOT_APPLICABLE"
            if not finalized and receipt.phase != dispatch_evidence.DispatchReceiptPhase.RESERVED.value:
                creator_liveness = dispatch_evidence.probe_process(
                    receipt.creator_pid,
                    receipt.creator_creation_time,
                    receipt.boot_id,
                )
                tree_liveness = dispatch_evidence.probe_dispatch_process_tree(receipt)
                if active is not None or (
                    creator_liveness is dispatch_evidence.ProcessLiveness.ALIVE
                    or tree_liveness is dispatch_evidence.ProcessLiveness.ALIVE
                ):
                    process_state = "ALIVE"
                elif (
                    creator_liveness is dispatch_evidence.ProcessLiveness.DEAD
                    and tree_liveness is dispatch_evidence.ProcessLiveness.DEAD
                ):
                    process_state = "DEAD"
                    phase = "RECOVERY_REQUIRED"
                else:
                    process_state = "UNKNOWN"
                    phase = "RECOVERY_REQUIRED"
            supervisor_state = process_state
            worker_state = (
                "NOT_APPLICABLE"
                if receipt.phase == dispatch_evidence.DispatchReceiptPhase.SUPERVISOR_READY.value
                or finalized
                else process_state
            )
            result.append(DockyardRunEvidence(
                task.task_id,
                task.revision,
                task.attempt,
                dispatch.dispatch_id,
                phase,
                supervisor_state,
                worker_state,
                "DONE" if finalized else "UNKNOWN",
                "RELEASED" if finalized else "UNKNOWN",
                task.timestamps.started_at,
                tombstone.finalized_at if tombstone is not None else receipt.written_at,
                max(0, task.attempt - 1),
                receipt.generation_id,
                active is not None,
                None if active is None else _termination_event_id(dispatch.dispatch_id),
            ))
        try:
            review_receipts = DockyardPostDeliveryReviewStore(
                self._project_root
            ).enumerate_receipts()
        except DockyardReviewNotFoundError:
            review_receipts = ()
        for task in snapshot.tasks:
            if task.current_dispatch is not None or task.attempt is None:
                continue
            failed_events = tuple(
                event for event in snapshot.events
                if event.event_type == "DISPATCH_FAILED"
                and event.task_id == task.task_id
                and event.revision == task.revision
                and event.attempt == task.attempt
                and event.dispatch_id is not None
                and event.to_state == "ready"
            )
            for failed_event in failed_events:
                assert failed_event.dispatch_id is not None
                if failed_event.dispatch_id in current_dispatch_ids:
                    continue
                outboxes = tuple(
                    item for item in snapshot.outbox
                    if item.task_id == task.task_id
                    and item.revision == task.revision
                    and item.attempt == task.attempt
                    and item.dispatch_id == failed_event.dispatch_id
                )
                if len(outboxes) != 1:
                    raise DockyardProjectionConflictError("retry_outbox_identity")
                receipt = dispatch_evidence.read_dispatch_receipt(
                    self._project_root, failed_event.dispatch_id
                )
                tombstone = dispatch_evidence.read_dispatch_tombstone(
                    self._project_root, failed_event.dispatch_id
                )
                if (
                    receipt is None
                    or tombstone is None
                    or receipt.task_id != task.task_id
                    or receipt.revision != task.revision
                    or receipt.attempt != task.attempt
                    or receipt.dispatch_id != failed_event.dispatch_id
                    or tombstone.task_id != task.task_id
                    or tombstone.revision != task.revision
                    or tombstone.attempt != task.attempt
                    or tombstone.dispatch_id != failed_event.dispatch_id
                    or tombstone.generation_id != receipt.generation_id
                    or tombstone.failure_kind is None
                ):
                    raise DockyardProjectionConflictError("retry_evidence_identity")
                outbox = outboxes[0]
                provider_id = outbox.model_selection.selected_model_provider
                model_id = outbox.model_selection.selected_model_id
                retry_available = (
                    task.attempt < 3
                    and receipt.phase == dispatch_evidence.DispatchReceiptPhase.FINALIZED.value
                    and provider_id in self._retry_provider_ids
                )
                result.append(DockyardRunEvidence(
                    task.task_id,
                    task.revision,
                    task.attempt,
                    failed_event.dispatch_id,
                    "FAILED",
                    "DEAD",
                    "DEAD",
                    "DONE",
                    "RELEASED",
                    task.timestamps.started_at,
                    failed_event.occurred_at,
                    max(0, task.attempt - 1),
                    receipt.generation_id,
                    False,
                    None,
                    retry_available,
                    failed_event.event_id if retry_available else None,
                    tombstone.failure_kind if retry_available else None,
                    provider_id if retry_available else None,
                    model_id if retry_available else None,
                ))
        review_store = DockyardPostDeliveryReviewStore(self._project_root)
        for task in snapshot.tasks:
            if (
                task.state != "ready"
                or task.current_dispatch is not None
                or task.attempt is None
            ):
                continue
            matches = tuple(
                receipt for receipt in review_receipts
                if receipt.task_id == task.task_id
                and receipt.revision == task.revision
                and receipt.attempt == task.attempt
                and receipt.phase is DockyardPostDeliveryReviewPhase.FINALIZED
                and receipt.audit_verdict == "fail"
                and receipt.return_event_id is not None
                and receipt.requeue_event_id is not None
            )
            if len(matches) > 1:
                raise DockyardProjectionConflictError("returned_review_duplicate")
            if not matches:
                continue
            review = matches[0]
            durable = review_store.read_request(review.review_id)
            return_events = tuple(
                event for event in snapshot.events
                if event.event_id == review.return_event_id
                and event.event_type == "DELIVERY_RETURNED"
                and event.task_id == task.task_id
                and event.revision == task.revision
                and event.attempt == task.attempt
                and event.dispatch_id == review.dispatch_id
                and event.from_state == "review_ready"
                and event.to_state == "returned"
            )
            requeue_events = tuple(
                event for event in snapshot.events
                if event.event_id == review.requeue_event_id
                and event.event_type == "TASK_REQUEUED"
                and event.task_id == task.task_id
                and event.revision == task.revision
                and event.attempt == task.attempt
                and event.dispatch_id is None
                and event.from_state == "returned"
                and event.to_state == "ready"
            )
            if (
                len(return_events) != 1
                or len(requeue_events) != 1
                or return_events[0].occurred_at >= requeue_events[0].occurred_at
                or durable.dispatch_id != review.dispatch_id
                or durable.return_event_id != review.return_event_id
                or durable.requeue_event_id != review.requeue_event_id
            ):
                raise DockyardProjectionConflictError("returned_event_identity")
            delivery = DockyardDeliveryReceiptStore(self._project_root).read(
                review.dispatch_id
            )
            receipt = dispatch_evidence.read_dispatch_receipt(
                self._project_root, review.dispatch_id
            )
            tombstone = dispatch_evidence.read_dispatch_tombstone(
                self._project_root, review.dispatch_id
            )
            try:
                _returned_admission_context(
                    self._project_root, review, durable
                )
            except Exception as exc:
                raise DockyardProjectionConflictError(
                    "returned_admission_identity"
                ) from exc
            outboxes = tuple(
                item for item in snapshot.outbox
                if item.task_id == task.task_id
                and item.revision == task.revision
                and item.attempt == task.attempt
                and item.dispatch_id == review.dispatch_id
            )
            if (
                delivery.identity.task_id != task.task_id
                or delivery.identity.revision != task.revision
                or delivery.identity.attempt != task.attempt
                or delivery.identity.dispatch_id != review.dispatch_id
                or digest_owner_value(delivery) != durable.delivery_receipt_digest
                or receipt is None
                or tombstone is None
                or receipt.task_id != task.task_id
                or receipt.revision != task.revision
                or receipt.attempt != task.attempt
                or receipt.dispatch_id != review.dispatch_id
                or receipt.phase != dispatch_evidence.DispatchReceiptPhase.FINALIZED.value
                or tombstone.generation_id != receipt.generation_id
                or tombstone.winner != "completion"
                or not tombstone.worker_done
                or not tombstone.heartbeat_done
                or not tombstone.release_completed
                or tombstone.failure_kind is not None
                or len(outboxes) != 1
            ):
                raise DockyardProjectionConflictError("returned_evidence_identity")
            outbox = outboxes[0]
            provider_id = outbox.model_selection.selected_model_provider
            model_id = outbox.model_selection.selected_model_id
            retry_available = (
                task.attempt < 3
                and provider_id in self._retry_provider_ids
            )
            result.append(DockyardRunEvidence(
                task.task_id,
                task.revision,
                task.attempt,
                review.dispatch_id,
                "RETURNED",
                "NOT_APPLICABLE",
                "NOT_APPLICABLE",
                "DONE",
                "RELEASED",
                task.timestamps.started_at,
                requeue_events[0].occurred_at,
                max(0, task.attempt - 1),
                receipt.generation_id,
                False,
                None,
                retry_available,
                review.requeue_event_id if retry_available else None,
                "delivery_returned" if retry_available else None,
                provider_id if retry_available else None,
                model_id if retry_available else None,
            ))
        return tuple(result)

    def _review_projections(
        self,
        project_id: str,
        snapshot_commit: str,
    ) -> tuple[DockyardReviewProjection, ...]:
        if self._terminal_owner is None:
            return ()
        result = []
        try:
            receipts = self._terminal_owner.pending_reviews()
        except DockyardReviewNotFoundError:
            return ()
        for receipt in receipts:
            durable = self._terminal_owner.review_request(receipt.review_id)
            values = (
                project_id, snapshot_commit, receipt.task_id, str(receipt.revision),
                str(receipt.attempt), receipt.dispatch_id,
                durable.implementation_commit, durable.report_commit,
                receipt.audit_verdict or "", receipt.phase.value,
            )
            result.append(DockyardReviewProjection(
                "dockyard.review-projection/v1", project_id, snapshot_commit,
                receipt.task_id, receipt.revision, receipt.attempt,
                receipt.dispatch_id, durable.implementation_commit,
                durable.report_commit, receipt.audit_verdict or "",
                receipt.phase.value, _projection_digest(values),
            ))
        return tuple(result)

    def _approval_projection(
        self,
        project_id: str,
        snapshot_commit: str,
    ) -> DockyardPlanApprovalProjection | None:
        plan = self._pm_service.latest(self._plan_id)
        if (
            plan is None
            or plan.project_id != project_id
            or plan.phase is not DockyardPlanPhase.APPROVAL_PENDING
        ):
            return None
        values = (
            project_id,
            snapshot_commit,
            str(self._project_registry.read(project_id).generation),
            plan.plan_id,
            str(plan.revision),
            plan.plan_digest,
            str(len(plan.tasks)),
            plan.phase.value,
        )
        return DockyardPlanApprovalProjection(
            "dockyard.plan-approval-projection/v1",
            project_id,
            snapshot_commit,
            self._project_registry.read(project_id).generation,
            plan.plan_id,
            plan.revision,
            plan.plan_digest,
            len(plan.tasks),
            plan.phase.value,
            _projection_digest(values),
        )

    def _not_ready(self, reason_code: str, snapshot: str, status: int) -> DockyardJsonResponse:
        body = {
            "schema_version": "dockyard.read-not-ready/v1",
            "project_id": self._project_id,
            "snapshot_commit": snapshot,
            "reason_code": reason_code,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return DockyardJsonResponse(status, encoded, None)


_EDITABLE_TASK_FIELDS = frozenset({
    "task_id",
    "title",
    "description",
    "dependencies",
    "execution_mode",
    "role_id",
    "provider_id",
    "model_id",
    "budget_tokens",
    "max_attempts",
})


def _pm_codex_failure_code(error: PmCodexPlanningError) -> str:
    """Map private planner failures to stable, non-sensitive API codes."""
    reason = str(error)
    if reason == "pm_codex:proxy_certificate_untrusted":
        return "PM_CODEX_PROXY_CERTIFICATE_UNTRUSTED"
    if reason == "pm_codex:dispatch_failed":
        return "PM_CODEX_DISPATCH_FAILED"
    if reason == "pm_codex:local_policy_rejected":
        return "PM_CODEX_PLAN_REJECTED"
    return "PM_CODEX_OUTPUT_INVALID"


def _edited_tasks(raw: object, current: tuple[DockyardPlanTask, ...]) -> tuple[DockyardPlanTask, ...]:
    if type(raw) is not list or len(raw) != len(current):
        raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
    current_by_id = {task.task_id: task for task in current}
    if len(current_by_id) != len(current):
        raise DockyardCommandRejected(500, "PLAN_TASK_IDENTITY_INVALID", "internal")
    result: list[DockyardPlanTask] = []
    seen: set[str] = set()
    for item in raw:
        if type(item) is not dict or set(item) != _EDITABLE_TASK_FIELDS:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        task_id = _text(item["task_id"], "task_id", 128)
        if task_id in seen or task_id not in current_by_id:
            raise DockyardCommandRejected(409, "PLAN_TASK_IDENTITY_DIVERGENCE", "conflict")
        seen.add(task_id)
        dependencies = item["dependencies"]
        if type(dependencies) is not list:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        dependency_tuple = tuple(_text(value, "dependency", 128) for value in dependencies)
        execution_mode = _text(item["execution_mode"], "execution_mode", 16)
        provider_id = _text(item["provider_id"], "provider_id", 32)
        max_attempts = _integer(item["max_attempts"], "max_attempts")
        if execution_mode not in {"parallel", "serial"} or provider_id not in {
            "claude", "codex", "reasonix"
        } or max_attempts > 3:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        result.append(replace(
            current_by_id[task_id],
            task_type="qa" if current_by_id[task_id].task_type == "validation" else current_by_id[task_id].task_type,
            title=_text(item["title"], "title", 256),
            description=_document_text(item["description"], "description"),
            dependencies=dependency_tuple,
            execution_mode=execution_mode,
            role_id=_text(item["role_id"], "role_id", 128),
            provider_id=provider_id,
            model_id=_text(item["model_id"], "model_id", 256),
            budget_tokens=_integer(item["budget_tokens"], "budget_tokens"),
            max_attempts=max_attempts,
        ))
    if seen != set(current_by_id):
        raise DockyardCommandRejected(409, "PLAN_TASK_IDENTITY_DIVERGENCE", "conflict")
    return tuple(result)


class DockyardCompositionGateway:
    """Route accepted Control API commands to already-owned production logic."""

    def __init__(
        self,
        pm_service: DockyardPmService,
        snapshot_commit: Callable[[], str],
        sse_hub: DockyardSseHub,
        project_registry: DockyardProjectRegistry | None = None,
        task_admission: DockyardTaskAdmissionCompositionRuntime | None = None,
        terminal_owner: DockyardTerminalOwnerCompositionRuntime | None = None,
        *,
        project_root: Path | None = None,
        active_registry: DockyardActiveExecutionRegistry | None = None,
        plan_id: str | None = None,
        ready_provider_ids: tuple[str, ...] | None = None,
        providers: Mapping[str, object] | None = None,
        provider_cli_versions: tuple[tuple[str, str], ...] | None = None,
        agent_registry: AgentCapabilityRegistry | None = None,
        pm_planner: PmCodexPlanner | None = None,
        plan_id_changed: Callable[[str], None] | None = None,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        if type(pm_service) is not DockyardPmService:
            raise TypeError("pm_service must be DockyardPmService")
        if type(sse_hub) is not DockyardSseHub:
            raise TypeError("sse_hub must be DockyardSseHub")
        if project_registry is not None and type(project_registry) is not DockyardProjectRegistry:
            raise TypeError("project_registry must be DockyardProjectRegistry or None")
        if task_admission is not None and type(task_admission) is not DockyardTaskAdmissionCompositionRuntime:
            raise TypeError("task_admission must be DockyardTaskAdmissionCompositionRuntime or None")
        self._pm_service = pm_service
        self._snapshot_commit = snapshot_commit
        self._sse_hub = sse_hub
        self._project_registry = project_registry
        self._task_admission = task_admission
        if terminal_owner is not None and type(terminal_owner) is not DockyardTerminalOwnerCompositionRuntime:
            raise TypeError("terminal_owner must be DockyardTerminalOwnerCompositionRuntime or None")
        self._terminal_owner = terminal_owner
        if project_root is not None and (not isinstance(project_root, Path) or not project_root.is_absolute()):
            raise TypeError("project_root must be an absolute Path or None")
        if active_registry is not None and type(active_registry) is not DockyardActiveExecutionRegistry:
            raise TypeError("active_registry must be DockyardActiveExecutionRegistry or None")
        self._project_root = project_root
        self._active_registry = active_registry
        self._terminate_receipts: dict[str, tuple[str, DockyardCommandReceipt]] = {}
        self._terminate_receipts_lock = threading.Lock()
        self._cancel_receipts: dict[str, tuple[str, DockyardCommandReceipt]] = {}
        self._cancel_receipts_lock = threading.Lock()
        self._plan_id = None if plan_id is None else _text(plan_id, "plan_id", 128)
        if plan_id_changed is not None and not callable(plan_id_changed):
            raise TypeError("plan_id_changed must be callable or None")
        self._plan_id_changed = plan_id_changed
        if ready_provider_ids is not None:
            if type(ready_provider_ids) is not tuple:
                raise TypeError("ready_provider_ids must be tuple or None")
            normalized = tuple(_text(value, "provider_id", 128) for value in ready_provider_ids)
            if len(set(normalized)) != len(normalized):
                raise ValueError("ready_provider_ids must be unique")
            self._ready_provider_ids: frozenset[str] | None = frozenset(normalized)
        else:
            self._ready_provider_ids = None
        if providers is not None and (not isinstance(providers, Mapping) or not providers):
            raise TypeError("providers must be a non-empty Mapping or None")
        if provider_cli_versions is not None:
            if type(provider_cli_versions) is not tuple or not provider_cli_versions:
                raise TypeError("provider_cli_versions must be a non-empty tuple or None")
            for item in provider_cli_versions:
                if type(item) is not tuple or len(item) != 2 or any(type(value) is not str or not value for value in item):
                    raise TypeError("provider_cli_versions must contain string pairs")
        self._providers = providers
        self._provider_cli_versions = dict(provider_cli_versions or ())
        if agent_registry is not None and type(agent_registry) is not AgentCapabilityRegistry:
            raise TypeError("agent_registry must be AgentCapabilityRegistry or None")
        self._agent_registry = agent_registry
        if pm_planner is not None and type(pm_planner) is not PmCodexPlanner:
            raise TypeError("pm_planner must be PmCodexPlanner or None")
        self._pm_planner = pm_planner
        if self._providers is not None and self._ready_provider_ids is not None:
            missing = set(self._providers) - set(self._ready_provider_ids)
            if missing:
                raise ValueError("providers must be represented by ready_provider_ids")
        self._retry_receipts: dict[str, tuple[str, DockyardCommandReceipt]] = {}
        self._retry_receipts_lock = threading.Lock()
        self._now = now

    def execute(self, request: DockyardCommandRequest) -> DockyardCommandReceipt:
        if type(request) is not DockyardCommandRequest:
            raise DockyardCommandRejected(400, "COMMAND_REQUEST_INVALID", "input")
        current_snapshot = self._snapshot_commit()
        if request.envelope.expected_snapshot_commit != current_snapshot:
            raise DockyardCommandRejected(409, "SNAPSHOT_STALE", "conflict")
        if request.envelope.command_type == "plan.create":
            return self._create_plan(request, current_snapshot)
        if request.envelope.command_type == "plan.discard":
            return self._discard_plan(request, current_snapshot)
        if request.envelope.command_type == "plan.update":
            return self._update_plan(request, current_snapshot)
        if request.envelope.command_type == "plan.approve":
            return self._approve_plan(request, current_snapshot)
        if request.envelope.command_type == "delivery.accept":
            return self._accept_delivery(request, current_snapshot)
        if request.envelope.command_type == "delivery.return":
            return self._return_delivery(request, current_snapshot)
        if request.envelope.command_type == "dispatch.terminate":
            return self._terminate_dispatch(request, current_snapshot)
        if request.envelope.command_type == "task.cancel":
            return self._cancel_task(request, current_snapshot)
        if request.envelope.command_type == "task.retry":
            return self._retry_task(request, current_snapshot)
        if request.envelope.command_type == "project.remove":
            return self._remove_project(request, current_snapshot)
        raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")

    def _cancel_task(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        """Soft-delete one quiescent task through the canonical transition owner.

        A task with an active dispatch is deliberately rejected here; the UI
        must use the separate exact-generation dispatch termination command.
        The canonical TASK_CANCELLED event and task state remain durable.
        """
        if self._project_root is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"task_id", "event_id"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        task_id = _text(payload["task_id"], "task_id", 128)
        event_id = _text(payload["event_id"], "event_id", 128)
        if request.resource_id != task_id or envelope.confirmation_id is None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        with self._cancel_receipts_lock:
            existing = self._cancel_receipts.get(envelope.command_id)
        if existing is not None:
            payload_digest, receipt = existing
            if payload_digest != envelope.payload_digest:
                raise DockyardCommandRejected(409, "COMMAND_ID_DIVERGENCE", "conflict")
            return replace(receipt, replayed=True)
        snapshot = StateProvider(self._project_root).snapshot()
        task = next((value for value in snapshot.tasks if value.task_id == task_id), None)
        if task is None:
            raise DockyardCommandRejected(404, "TASK_NOT_FOUND", "not_found")
        if task.revision != envelope.expected_revision:
            raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
        if task.current_dispatch is not None:
            raise DockyardCommandRejected(409, "ACTIVE_DISPATCH_REQUIRES_TERMINATE", "conflict")
        if task.state in {"cancelled", "superseded", "integrated"}:
            raise DockyardCommandRejected(409, "TASK_ALREADY_TERMINAL", "conflict")
        if any(value.event_id == event_id for value in snapshot.events):
            raise DockyardCommandRejected(409, "EVENT_ID_CONFLICT", "conflict")
        transition = TransitionRequest(
            cas=TransitionCAS(
                task_id=task_id,
                expected_revision=task.revision,
                expected_state=task.state,
                expected_snapshot_commit=envelope.expected_snapshot_commit,
            ),
            dispatch_cas=None,
            event_id=event_id,
            event_type="TASK_CANCELLED",
            payload=CancelledPayload(),
            event_context=TransitionEventContext(
                source_message_id=envelope.command_id,
                evidence_refs=(),
                guard_results=(),
            ),
        )
        try:
            result = asyncio.run(WorkflowOrchestrator(
                self._project_root,
                _DockyardWorkflowClock(self._now),
            ).cancel_quiescent_task(TaskCancellationRequest(transition)))
        except WorkflowInputError as exc:
            raise DockyardCommandRejected(409, "TASK_CANCEL_CONFLICT", "conflict") from exc
        except TransitionCASConflictError as exc:
            raise DockyardCommandRejected(409, "TASK_CANCEL_CONFLICT", "conflict") from exc
        except TransitionLockContentionError as exc:
            raise DockyardCommandRejected(409, "STATE_LOCK_CONTENTION", "conflict") from exc
        completed_at = self._now()
        committed_snapshot = self._snapshot_commit()
        receipt = DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "cancelled",
            result.cancellation_transition.event_id,
            committed_snapshot,
            False,
            completed_at,
            _receipt_digest(envelope, "cancelled", committed_snapshot, completed_at),
        )
        with self._cancel_receipts_lock:
            self._cancel_receipts[envelope.command_id] = (envelope.payload_digest, receipt)
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="task.changed",
            snapshot_commit=committed_snapshot,
            resource_id=task_id,
            occurred_at=completed_at,
        )
        return receipt

    def _retry_task(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        """Start one user-approved retry through the canonical workflow owner."""
        if self._project_root is None or self._providers is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        expected = {
            "task_id", "failed_attempt", "dispatch_id", "generation_id",
            "event_id", "provider_id", "model_id", "upgrade",
        }
        if set(payload) != expected:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        task_id = _text(payload["task_id"], "task_id", 128)
        failed_attempt = _integer(payload["failed_attempt"], "failed_attempt")
        failed_dispatch_id = _text(payload["dispatch_id"], "dispatch_id", 128)
        generation_id = _text(payload["generation_id"], "generation_id", 128)
        failed_event_id = _text(payload["event_id"], "event_id", 128)
        provider_id = _text(payload["provider_id"], "provider_id", 64)
        model_id = _text(payload["model_id"], "model_id", 256)
        if type(payload["upgrade"]) is not bool:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        if request.resource_id != task_id or envelope.confirmation_id is None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        with self._retry_receipts_lock:
            existing = self._retry_receipts.get(envelope.command_id)
        if existing is not None:
            old_digest, receipt = existing
            if old_digest != envelope.payload_digest:
                raise DockyardCommandRejected(409, "COMMAND_ID_DIVERGENCE", "conflict")
            return replace(receipt, replayed=True)
        if self._ready_provider_ids is None or provider_id not in self._ready_provider_ids:
            raise DockyardCommandRejected(409, "PROVIDER_NOT_READY", "not_ready")
        if provider_id not in self._providers or provider_id not in self._provider_cli_versions:
            raise DockyardCommandRejected(409, "PROVIDER_NOT_READY", "not_ready")

        snapshot = StateProvider(self._project_root).snapshot()
        task = next((item for item in snapshot.tasks if item.task_id == task_id), None)
        if (
            task is None
            or task.revision != envelope.expected_revision
            or task.state != "ready"
            or task.current_dispatch is not None
            or task.attempt != failed_attempt
            or failed_attempt >= 3
        ):
            raise DockyardCommandRejected(409, "RETRY_CAS_CONFLICT", "conflict")
        failed_events = tuple(
            event for event in snapshot.events
            if event.event_type == "DISPATCH_FAILED"
            and event.event_id == failed_event_id
            and event.task_id == task_id
            and event.revision == task.revision
            and event.attempt == failed_attempt
            and event.dispatch_id == failed_dispatch_id
            and event.to_state == "ready"
        )
        requeue_events = tuple(
            event for event in snapshot.events
            if event.event_type == "TASK_REQUEUED"
            and event.event_id == failed_event_id
            and event.task_id == task_id
            and event.revision == task.revision
            and event.attempt == failed_attempt
            and event.dispatch_id is None
            and event.from_state == "returned"
            and event.to_state == "ready"
        )
        if len(failed_events) + len(requeue_events) != 1:
            raise DockyardCommandRejected(409, "RETRY_EVIDENCE_UNAVAILABLE", "not_ready")
        returned_route = len(requeue_events) == 1
        retry_event = requeue_events[0] if returned_route else failed_events[0]
        returned_review = None
        returned_request = None
        audit_result = None
        delivery = None
        if returned_route:
            if self._terminal_owner is None:
                raise DockyardCommandRejected(
                    409, "COMMAND_OWNER_UNAVAILABLE", "not_ready"
                )
            try:
                review_store = DockyardPostDeliveryReviewStore(self._project_root)
                returned_reviews = tuple(
                    receipt for receipt in review_store.enumerate_receipts()
                    if receipt.task_id == task_id
                    and receipt.revision == task.revision
                    and receipt.attempt == failed_attempt
                    and receipt.dispatch_id == failed_dispatch_id
                    and receipt.phase is DockyardPostDeliveryReviewPhase.FINALIZED
                    and receipt.audit_verdict == "fail"
                    and receipt.requeue_event_id == retry_event.event_id
                    and receipt.return_event_id is not None
                )
                if len(returned_reviews) != 1:
                    raise ValueError("returned review identity")
                returned_review = returned_reviews[0]
                returned_request = review_store.read_request(returned_review.review_id)
                audit_result = DockyardMadResultStore(self._project_root).read(
                    returned_review.review_id
                )
                delivery = DockyardDeliveryReceiptStore(self._project_root).read(
                    failed_dispatch_id
                )
                return_events = tuple(
                    event for event in snapshot.events
                    if event.event_id == returned_review.return_event_id
                    and event.event_type == "DELIVERY_RETURNED"
                    and event.task_id == task_id
                    and event.revision == task.revision
                    and event.attempt == failed_attempt
                    and event.dispatch_id == failed_dispatch_id
                    and event.from_state == "review_ready"
                    and event.to_state == "returned"
                )
                if (
                    len(return_events) != 1
                    or return_events[0].occurred_at >= retry_event.occurred_at
                    or returned_request.dispatch_id != failed_dispatch_id
                    or returned_request.return_event_id != returned_review.return_event_id
                    or returned_request.requeue_event_id != returned_review.requeue_event_id
                    or returned_request.delivery_receipt_digest != digest_owner_value(delivery)
                    or returned_review.audit_result_digest != digest_owner_value(audit_result)
                    or audit_result.verdict != "fail"
                ):
                    raise ValueError("returned evidence binding")
            except Exception as exc:
                raise DockyardCommandRejected(
                    409, "RETRY_EVIDENCE_UNAVAILABLE", "not_ready"
                ) from exc
        try:
            process_receipt = dispatch_evidence.read_dispatch_receipt(
                self._project_root, failed_dispatch_id
            )
            tombstone = dispatch_evidence.read_dispatch_tombstone(
                self._project_root, failed_dispatch_id
            )
            if returned_route:
                assert returned_review is not None and returned_request is not None
                handoff, admission_plan, _ = _returned_admission_context(
                    self._project_root, returned_review, returned_request
                )
            else:
                handoff = PortfolioSchedulerWorkerHandoffStore(
                    self._project_root
                ).read_handoff(failed_dispatch_id)
                admission_plan = PortfolioSchedulerStore(
                    self._project_root
                ).read_admission_plan(handoff.queue_id, 1)
        except Exception as exc:
            raise DockyardCommandRejected(409, "RETRY_EVIDENCE_UNAVAILABLE", "not_ready") from exc
        if (
            process_receipt is None
            or tombstone is None
            or (returned_route and tombstone.failure_kind is not None)
            or (not returned_route and tombstone.failure_kind is None)
            or process_receipt.task_id != task_id
            or process_receipt.revision != task.revision
            or process_receipt.attempt != failed_attempt
            or process_receipt.dispatch_id != failed_dispatch_id
            or process_receipt.generation_id != generation_id
            or process_receipt.phase != dispatch_evidence.DispatchReceiptPhase.FINALIZED.value
            or tombstone.task_id != task_id
            or tombstone.revision != task.revision
            or tombstone.attempt != failed_attempt
            or tombstone.dispatch_id != failed_dispatch_id
            or tombstone.generation_id != generation_id
            or tombstone.winner != "completion"
            or not tombstone.worker_done
            or not tombstone.heartbeat_done
            or not tombstone.release_completed
            or handoff.task_id != task_id
            or handoff.revision != task.revision
            or handoff.phase.value != "FINALIZED"
            or admission_plan.task_id != task_id
            or admission_plan.revision != task.revision
            or admission_plan.receipt_id != handoff.receipt_id
            or admission_plan.content_digest != handoff.plan_digest
            or (
                not returned_route
                and (
                    handoff.attempt != failed_attempt
                    or handoff.dispatch_id != failed_dispatch_id
                    or admission_plan.new_attempt != failed_attempt
                    or admission_plan.dispatch_id != failed_dispatch_id
                )
            )
            or (
                returned_route
                and (
                    delivery is None
                    or delivery.identity.task_id != task_id
                    or delivery.identity.revision != task.revision
                    or delivery.identity.attempt != failed_attempt
                    or delivery.identity.dispatch_id != failed_dispatch_id
                )
            )
            or (
                returned_route
                and (
                    returned_request is None
                    or returned_request.queue_id != handoff.queue_id
                    or returned_request.schedule_receipt_id != handoff.receipt_id
                    or returned_request.admission_plan_digest != admission_plan.content_digest
                    or returned_request.handoff_id != handoff.handoff_id
                    or returned_request.dispatch_generation_id != generation_id
                )
            )
        ):
            raise DockyardCommandRejected(409, "RETRY_EVIDENCE_CONFLICT", "conflict")

        outboxes = tuple(
            item for item in snapshot.outbox
            if item.task_id == task_id
            and item.revision == task.revision
            and item.attempt == failed_attempt
            and item.dispatch_id == failed_dispatch_id
        )
        if len(outboxes) != 1 or outboxes[0].model_selection.selected_model_provider != provider_id or outboxes[0].model_selection.selected_model_id != model_id:
            raise DockyardCommandRejected(409, "RETRY_EVIDENCE_CONFLICT", "conflict")
        outbox = outboxes[0]
        next_attempt = failed_attempt + 1
        suffix = hashlib.sha256(("dockyard-retry\0" + envelope.command_id).encode("utf-8")).hexdigest()[:32]
        next_dispatch_id = "DSP-RETRY-" + suffix
        next_event_id = "EVT-RETRY-DISPATCH-" + suffix
        next_outbox_id = "MSG-RETRY-" + suffix
        next_ack_id = "EVT-RETRY-ACK-" + suffix
        next_delivery_id = "EVT-RETRY-DELIVERY-" + suffix
        next_failure_id = "EVT-RETRY-FAILED-" + suffix
        existing_dispatch = next(
            (
                event for event in snapshot.events
                if event.event_type == "TASK_DISPATCHED"
                and event.task_id == task_id
                and event.revision == task.revision
                and event.attempt == next_attempt
                and event.dispatch_id == next_dispatch_id
            ),
            None,
        )
        existing_failure = next(
            (
                event for event in snapshot.events
                if event.event_type == "DISPATCH_FAILED"
                and event.task_id == task_id
                and event.revision == task.revision
                and event.attempt == next_attempt
                and event.dispatch_id == next_dispatch_id
            ),
            None,
        )
        if existing_dispatch is not None:
            outcome = "retry_failed" if existing_failure is not None else "retry_started"
            replay_completed_at = self._now()
            replay_snapshot = self._snapshot_commit()
            replay = DockyardCommandReceipt(
                "dockyard.command-receipt/v1", envelope.command_id, envelope.project_id,
                envelope.command_type, outcome,
                existing_dispatch.event_id, replay_snapshot, True, replay_completed_at,
                _receipt_digest(envelope, outcome, replay_snapshot, replay_completed_at),
            )
            with self._retry_receipts_lock:
                self._retry_receipts[envelope.command_id] = (envelope.payload_digest, replay)
            return replay

        try:
            assessment = DifficultyAssessmentStore().read(
                self._project_root, task_id, task.revision, handoff.assessment_id
            ).assessment
            model_selection = ModelSelectionSnapshot(
                outbox.model_selection.required_model_tier,
                outbox.model_selection.required_model_capabilities,
                outbox.model_selection.model_binding_id,
                outbox.model_selection.selected_model_provider,
                outbox.model_selection.selected_model_id,
                outbox.model_selection.selected_model_tier,
                outbox.model_selection.selected_deliberation_tier,
                outbox.model_selection.selected_context_window_tokens,
                outbox.model_selection.selected_model_capabilities,
                outbox.model_selection.model_degradation_approval_id,
            )
            worktree = Path(admission_plan.canonical_worktree).resolve(strict=True)
            project_root = self._project_root.resolve(strict=True)
            worktree.relative_to(project_root.parent)
            if returned_route:
                if returned_review is None or audit_result is None:
                    raise ValueError("returned review is unavailable")
                evidence_store = MaterializationAdmissionRuntime(
                    self._project_root
                ).evidence_store
                receipts_root = (
                    self._project_root
                    / ".agentdesk/runtime/materialization-admission/receipts"
                )
                candidates = []
                for path in receipts_root.iterdir():
                    if path.is_symlink() or not path.is_file() or path.suffix != ".yaml":
                        raise ValueError("materialization receipt entry is invalid")
                    candidate = evidence_store.read_receipt(path.stem)
                    if (
                        candidate is not None
                        and candidate.task_id == task_id
                        and candidate.revision == task.revision
                        and candidate.queue_id == handoff.queue_id
                        and candidate.schedule_receipt_id == handoff.receipt_id
                        and candidate.dispatch_id == admission_plan.dispatch_id
                    ):
                        candidates.append(candidate)
                if len(candidates) != 1:
                    raise ValueError("materialization receipt identity is invalid")
                materialized = candidates[0]
                if (
                    materialized.phase is not MaterializationAdmissionPhase.FINALIZED
                    or materialized.task_card_relative_path != outbox.payload.task_path
                    or materialized.task_card_relative_path != admission_plan.task_card_path
                    or materialized.task_card_git_commit != admission_plan.task_card_commit
                    or materialized.base_commit != admission_plan.base_commit
                ):
                    raise ValueError("materialized task card binding is invalid")
                card = (project_root / materialized.task_card_relative_path).resolve(
                    strict=True
                )
                card.relative_to(project_root)
                card_bytes = card.read_bytes()
                if (
                    "sha256:" + hashlib.sha256(card_bytes).hexdigest()
                    != materialized.task_card_content_digest
                ):
                    raise ValueError("materialized task card digest is invalid")
                prompt = _returned_delivery_prompt(
                    card_bytes.decode("utf-8", errors="strict"), audit_result
                )
            else:
                card = (worktree / outbox.payload.task_path).resolve(strict=True)
                card.relative_to(worktree)
                prompt = card.read_text(encoding="utf-8")
            dispatch_request = DispatchRequest(
                DispatchIdentity(task_id, task.revision, next_attempt, next_dispatch_id),
                worktree,
                prompt,
                model_selection,
                1800,
            )
        except Exception as exc:
            raise DockyardCommandRejected(409, "RETRY_EVIDENCE_UNAVAILABLE", "not_ready") from exc
        if returned_route:
            approval_subject = ApprovalSubject(
                task_id,
                task.revision,
                next_attempt,
                next_dispatch_id,
                None,
            )
            approval_request = ApprovalCheckRequest(
                ApprovalScope.DISPATCH,
                approval_subject,
                snapshot_commit,
            )
            approval_gate = ApprovalGate(self._project_root)
            approval_id = "APR-RETRY-" + suffix
            approval_event_id = "EVT-RETRY-APPROVAL-" + suffix
            approval_reason = "Dockyard confirmed returned-delivery redispatch"
            try:
                checked = approval_gate.check(
                    approval_request,
                    datetime.fromisoformat(self._now().replace("Z", "+00:00")),
                )
                if not checked.passed:
                    if checked.failure_code not in {
                        "not_found", "wrong_scope", "wrong_subject"
                    }:
                        raise ValueError("redispatch approval is unavailable")
                    try:
                        write_grant(
                            self._project_root,
                            approval_id,
                            approval_event_id,
                            ApprovalScope.DISPATCH,
                            approval_subject,
                            snapshot.pm_lease_epoch,
                            datetime.fromisoformat(
                                self._now().replace("Z", "+00:00")
                            ),
                            approval_reason,
                            None,
                            snapshot_commit,
                        )
                    except Exception:
                        replay_check = approval_gate.check(
                            approval_request,
                            datetime.fromisoformat(
                                self._now().replace("Z", "+00:00")
                            ),
                        )
                        if not replay_check.passed:
                            raise
            except Exception as exc:
                raise DockyardCommandRejected(
                    409, "RETRY_EVIDENCE_CONFLICT", "conflict"
                ) from exc
        context_refs = retry_event.evidence_refs
        if returned_route:
            assert returned_review is not None and audit_result is not None
            context_refs = tuple(dict.fromkeys((
                *context_refs,
                returned_review.content_digest.removeprefix("sha256:"),
                digest_owner_value(audit_result).removeprefix("sha256:"),
            )))
        context = TransitionEventContext(
            envelope.command_id,
            context_refs,
            retry_event.guard_results,
        )
        dispatch_transition = TransitionRequest(
            TransitionCAS(task_id, task.revision, "ready", snapshot_commit),
            None,
            next_event_id,
            "TASK_DISPATCHED",
            DispatchPayload(
                next_dispatch_id,
                outbox.destination_role_id,
                model_selection,
                outbox.payload.task_path,
                outbox.payload.task_card_commit,
                outbox.payload.base_commit,
                outbox.payload.branch,
                outbox.payload.report_path,
                next_outbox_id,
                next_attempt,
            ),
            context,
        )
        acknowledge = TransitionRequest(
            TransitionCAS(task_id, task.revision, "dispatched", snapshot_commit),
            DispatchCAS(next_dispatch_id, next_attempt),
            next_ack_id,
            "DISPATCH_ACKNOWLEDGED",
            AcknowledgePayload(),
            context,
        )
        cycle = DispatchCycleRequest(
            dispatch_request,
            dispatch_transition,
            acknowledge,
            next_delivery_id,
            context,
            self._provider_cli_versions[provider_id],
            handoff.worker_kind,
            assessment.selected_difficulty,
            handoff.holder_instance_id,
        )

        async def run_cycle():
            orchestrator = WorkflowOrchestrator(
                self._project_root,
                _DockyardWorkflowClock(self._now),
            )
            execution = await orchestrator.start_dispatch_cycle(cycle, self._providers)
            try:
                return await execution.wait(), None
            except BaseException as dispatch_error:
                metadata = execution._finalizer_metadata
                if (
                    type(metadata) is not _workflow_orchestrator._DispatchFinalizerMetadata
                    or metadata.winner != "completion"
                    or not metadata.worker_done
                    or not metadata.heartbeat_done
                    or not metadata.release_completed
                ):
                    raise WorkflowInvariantError("retry finalization evidence is incomplete") from dispatch_error
                failure_request = TransitionRequest(
                    TransitionCAS(task_id, task.revision, "in_progress", snapshot_commit),
                    DispatchCAS(next_dispatch_id, next_attempt),
                    next_failure_id,
                    "DISPATCH_FAILED",
                    DispatchFailedPayload(metadata.failure_kind.value),
                    context,
                )
                ControlPlaneTransitionService(self._project_root).apply_transition(
                    failure_request,
                    lease=None,
                    now=datetime.fromisoformat(self._now().replace("Z", "+00:00")),
                )
                raise

        try:
            result, _ = asyncio.run(run_cycle())
        except DockyardCommandRejected:
            raise
        except BaseException as exc:
            raise DockyardCommandRejected(409, "RETRY_FAILED", "conflict") from exc
        if returned_route:
            assert returned_review is not None
            assert self._terminal_owner is not None
            try:
                completed_snapshot = StateProvider(self._project_root).snapshot()

                def completed_event(event_id: str, event_type: str):
                    matches = tuple(
                        event for event in completed_snapshot.events
                        if event.event_id == event_id
                        and event.event_type == event_type
                        and event.task_id == task_id
                        and event.revision == task.revision
                        and event.attempt == next_attempt
                        and event.dispatch_id == next_dispatch_id
                    )
                    if len(matches) != 1:
                        raise ValueError("redispatch canonical event")
                    return matches[0]

                dispatch_event = completed_event(
                    result.dispatch_transition.event_id, "TASK_DISPATCHED"
                )
                acknowledge_event = completed_event(
                    result.acknowledge_transition.event_id,
                    "DISPATCH_ACKNOWLEDGED",
                )
                delivery_event = completed_event(
                    result.delivery_transition.event_id, "DELIVERY_SUBMITTED"
                )
                process_receipt = dispatch_evidence.read_dispatch_receipt(
                    self._project_root, next_dispatch_id
                )
                tombstone = dispatch_evidence.read_dispatch_tombstone(
                    self._project_root, next_dispatch_id
                )
                if (
                    process_receipt is None
                    or tombstone is None
                    or process_receipt.task_id != task_id
                    or process_receipt.revision != task.revision
                    or process_receipt.attempt != next_attempt
                    or process_receipt.dispatch_id != next_dispatch_id
                    or process_receipt.phase
                    != dispatch_evidence.DispatchReceiptPhase.FINALIZED.value
                    or tombstone.generation_id != process_receipt.generation_id
                    or tombstone.winner != "completion"
                    or not tombstone.worker_done
                    or not tombstone.heartbeat_done
                    or not tombstone.release_completed
                    or tombstone.failure_kind is not None
                ):
                    raise ValueError("redispatch finalizer evidence")
                DockyardDeliveryReceiptStore(self._project_root).save(
                    result.delivery_receipt
                )
                redispatch = with_redispatch_digest(DockyardRedispatchReceipt(
                    REDISPATCH_SCHEMA_VERSION,
                    envelope.command_id,
                    returned_review.review_id,
                    returned_review.content_digest,
                    task_id,
                    task.revision,
                    next_attempt,
                    next_dispatch_id,
                    process_receipt.generation_id,
                    dispatch_event.event_id,
                    digest_owner_value(dispatch_event),
                    acknowledge_event.event_id,
                    digest_owner_value(acknowledge_event),
                    delivery_event.event_id,
                    digest_owner_value(delivery_event),
                    digest_owner_value(result.delivery_receipt),
                    "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    tombstone.finalized_at,
                    "sha256:" + "0" * 64,
                ))
                redispatch = DockyardRedispatchReceiptStore(
                    self._project_root
                ).save(redispatch)
                asyncio.run(self._terminal_owner.execute_redispatch(
                    returned_review.review_id,
                    redispatch,
                    result,
                ))
            except DockyardCommandRejected:
                raise
            except BaseException as exc:
                raise DockyardCommandRejected(
                    409, "REDISPATCH_REVIEW_FAILED", "conflict"
                ) from exc
        completed_at = self._now()
        current = self._snapshot_commit()
        receipt = DockyardCommandReceipt(
            "dockyard.command-receipt/v1", envelope.command_id, envelope.project_id,
            envelope.command_type, "retry_started", result.dispatch_transition.event_id,
            current, False, completed_at, _receipt_digest(envelope, "retry_started", current, completed_at),
        )
        with self._retry_receipts_lock:
            self._retry_receipts[envelope.command_id] = (envelope.payload_digest, receipt)
        self._sse_hub.publish(project_id=envelope.project_id, event_type="task.changed", snapshot_commit=current, resource_id=task_id, occurred_at=completed_at)
        self._sse_hub.publish(project_id=envelope.project_id, event_type="run.changed", snapshot_commit=current, resource_id=next_dispatch_id, occurred_at=completed_at)
        return receipt

    def _terminate_dispatch(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._project_root is None or self._active_registry is None:
            raise DockyardCommandRejected(409, "RECOVERY_REQUIRED", "not_ready")
        envelope = request.envelope
        with self._terminate_receipts_lock:
            existing = self._terminate_receipts.get(envelope.command_id)
        if existing is not None:
            payload_digest, receipt = existing
            if payload_digest != envelope.payload_digest:
                raise DockyardCommandRejected(409, "COMMAND_ID_DIVERGENCE", "conflict")
            return replace(receipt, replayed=True)
        payload = _payload(request.payload_json)
        if set(payload) != {"task_id", "attempt", "dispatch_id", "generation_id", "event_id"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        task_id = _text(payload["task_id"], "task_id", 128)
        attempt = _integer(payload["attempt"], "attempt")
        dispatch_id = _text(payload["dispatch_id"], "dispatch_id", 128)
        generation_id = _text(payload["generation_id"], "generation_id", 128)
        event_id = _text(payload["event_id"], "event_id", 128)
        if request.resource_id != dispatch_id or envelope.confirmation_id is None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        snapshot = StateProvider(self._project_root).snapshot()
        task = next((value for value in snapshot.tasks if value.task_id == task_id), None)
        if (
            task is None
            or task.revision != envelope.expected_revision
            or task.attempt != attempt
            or task.state != "in_progress"
            or task.current_dispatch is None
            or task.current_dispatch.dispatch_id != dispatch_id
        ):
            raise DockyardCommandRejected(409, "DISPATCH_CAS_CONFLICT", "conflict")
        if any(value.event_id == event_id for value in snapshot.events):
            raise DockyardCommandRejected(409, "EVENT_ID_CONFLICT", "conflict")
        try:
            outcome = self._active_registry.cancel(DockyardActiveCancellationCommand(
                envelope.command_id,
                task_id,
                envelope.expected_revision,
                attempt,
                dispatch_id,
                generation_id,
                envelope.expected_snapshot_commit,
                event_id,
                self._now(),
            ))
        except DockyardActiveExecutionNotFoundError as exc:
            raise DockyardCommandRejected(409, "RECOVERY_REQUIRED", "not_ready") from exc
        except DockyardActiveExecutionConflictError as exc:
            raise DockyardCommandRejected(409, "DISPATCH_TERMINATE_CONFLICT", "conflict") from exc
        except DockyardActiveExecutionCallbackError as exc:
            raise DockyardCommandRejected(409, "DISPATCH_TERMINATE_FAILED", "conflict") from exc
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="task.changed",
            snapshot_commit=self._snapshot_commit(),
            resource_id=task_id,
            occurred_at=outcome.completed_at,
        )
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="run.changed",
            snapshot_commit=self._snapshot_commit(),
            resource_id=dispatch_id,
            occurred_at=outcome.completed_at,
        )
        receipt = DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "cancelled",
            outcome.event_id,
            self._snapshot_commit(),
            False,
            outcome.completed_at,
            _receipt_digest(envelope, "cancelled", self._snapshot_commit(), outcome.completed_at),
        )
        with self._terminate_receipts_lock:
            self._terminate_receipts[envelope.command_id] = (envelope.payload_digest, receipt)
        return receipt

    def _accept_delivery(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._terminal_owner is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"task_id", "implementation_commit", "report_commit"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        task_id = _text(payload["task_id"], "task_id", 128)
        implementation_commit = _commit(payload["implementation_commit"])
        report_commit = _commit(payload["report_commit"])
        if request.resource_id != task_id or envelope.confirmation_id is None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        pending_review = self._terminal_owner.pending_review(task_id)
        if (
            envelope.expected_revision < 1
            or pending_review is None
            or pending_review.revision != envelope.expected_revision
        ):
            raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
        try:
            result = self._terminal_owner.accept(
                task_id,
                implementation_commit,
                report_commit,
                snapshot_commit,
                envelope.command_id,
                envelope.confirmation_id,
            )
        except DockyardTerminalCompositionError as exc:
            raise DockyardCommandRejected(409, "DELIVERY_ACCEPT_CONFLICT", "conflict") from exc
        completed_at = self._now()
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="review.changed",
            snapshot_commit=self._snapshot_commit(),
            resource_id=task_id,
            occurred_at=completed_at,
        )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "integrated",
            None,
            self._snapshot_commit(),
            result.replayed,
            completed_at,
            _receipt_digest(envelope, "integrated", self._snapshot_commit(), completed_at),
        )

    def _return_delivery(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._terminal_owner is None or self._project_root is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"task_id", "implementation_commit", "report_commit"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        task_id = _text(payload["task_id"], "task_id", 128)
        implementation_commit = _commit(payload["implementation_commit"])
        report_commit = _commit(payload["report_commit"])
        if request.resource_id != task_id or envelope.confirmation_id is None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        pending_review = self._terminal_owner.pending_review(task_id)
        if (
            envelope.expected_revision < 1
            or pending_review is None
            or pending_review.revision != envelope.expected_revision
        ):
            raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
        if pending_review.audit_verdict != "fail":
            raise DockyardCommandRejected(
                409, "DELIVERY_RETURN_CONFLICT", "conflict"
            )
        try:
            result = self._terminal_owner.return_delivery(
                task_id,
                implementation_commit,
                report_commit,
                snapshot_commit,
                envelope.command_id,
                envelope.confirmation_id,
            )
        except DockyardTerminalCompositionError as exc:
            raise DockyardCommandRejected(
                409, "DELIVERY_RETURN_CONFLICT", "conflict"
            ) from exc
        snapshot = StateProvider(self._project_root).snapshot()
        returned_events = tuple(
            event for event in snapshot.events
            if event.event_type == "DELIVERY_RETURNED"
            and event.task_id == task_id
            and event.revision == pending_review.revision
            and event.attempt == pending_review.attempt
            and event.dispatch_id == pending_review.dispatch_id
        )
        if len(returned_events) != 1:
            raise DockyardCommandRejected(
                409, "DELIVERY_RETURN_EVIDENCE_CONFLICT", "conflict"
            )
        completed_at = self._now()
        committed_snapshot = self._snapshot_commit()
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="review.changed",
            snapshot_commit=committed_snapshot,
            resource_id=task_id,
            occurred_at=completed_at,
        )
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="task.changed",
            snapshot_commit=committed_snapshot,
            resource_id=task_id,
            occurred_at=completed_at,
        )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "returned",
            returned_events[0].event_id,
            committed_snapshot,
            result.replayed,
            completed_at,
            _receipt_digest(
                envelope, "returned", committed_snapshot, completed_at
            ),
        )

    def _create_plan(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._project_registry is None or self._plan_id is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"plan_id", "requirement"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        if request.resource_id is not None or envelope.confirmation_id is not None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        requested_plan_id = _text(payload["plan_id"], "plan_id", 128)
        if requested_plan_id != self._plan_id:
            raise DockyardCommandRejected(409, "PLAN_ID_DIVERGENCE", "conflict")
        requirement = _document_text(payload["requirement"], "requirement")
        project = self._project_registry.read(envelope.project_id)
        if project is None or project.phase is not DockyardProjectPhase.REGISTERED:
            raise DockyardCommandRejected(409, "PROJECT_NOT_ACTIVE", "not_ready")
        if envelope.expected_revision != project.generation:
            raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
        existing = self._pm_service.latest(requested_plan_id)
        plan_id = requested_plan_id
        if existing is not None and existing.phase in {
            DockyardPlanPhase.MATERIALIZED,
            DockyardPlanPhase.DISCARDED,
        }:
            # A materialized plan is immutable.  Start a fresh PM session with
            # a deterministic successor identity while retaining all prior
            # revisions and task evidence under the old plan ID.
            plan_id = f"{requested_plan_id}-r{existing.revision + 1}"
            existing = None
        replayed = existing is not None and existing.last_operation_id == envelope.command_id
        if replayed and existing is not None:
            tasks = existing.tasks
        else:
            # A Dockyard plan is a PM-agent artefact.  Local control-plane code
            # may validate, bind and persist the result, but must never invent a
            # one-card substitute when PM-Codex is unavailable.
            if self._pm_planner is None or self._agent_registry is None:
                raise DockyardCommandRejected(
                    409, "PM_AGENT_UNAVAILABLE", "not_ready"
                )
            try:
                decomposition = self._pm_planner.plan(
                    plan_id=plan_id,
                    requirement=requirement,
                    snapshot_commit=snapshot_commit,
                    command_id=envelope.command_id,
                    registry=self._agent_registry,
                )
            except PmCodexPlanningError as exc:
                raise DockyardCommandRejected(
                    502, _pm_codex_failure_code(exc), "provider"
                ) from exc
            tasks = tuple(item.plan_task for item in decomposition.tasks)
        try:
            plan = self._pm_service.draft(DockyardPlanDraftRequest(
                plan_id,
                envelope.project_id,
                requirement,
                tasks,
                "PM-DOCKYARD",
                existing.created_at if replayed else self._now(),
                envelope.command_id,
            ))
        except DockyardPlanConflictError as exc:
            raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict") from exc
        if plan_id != self._plan_id:
            self._plan_id = plan_id
            if self._plan_id_changed is not None:
                self._plan_id_changed(plan_id)
        if not replayed:
            self._sse_hub.publish(
                project_id=envelope.project_id,
                event_type="project.changed",
                snapshot_commit=snapshot_commit,
                resource_id=plan_id,
                occurred_at=plan.updated_at,
            )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "drafted",
            None,
            snapshot_commit,
            replayed,
            plan.updated_at,
            _receipt_digest(envelope, "drafted", snapshot_commit, plan.updated_at),
        )

    def _discard_plan(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._plan_id is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"plan_id", "plan_digest"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        plan_id = _text(payload["plan_id"], "plan_id", 128)
        plan_digest = _sha(payload["plan_digest"])
        if plan_id != self._plan_id or request.resource_id != plan_id:
            raise DockyardCommandRejected(409, "PLAN_ID_DIVERGENCE", "conflict")
        if envelope.confirmation_id is None:
            raise DockyardCommandRejected(409, "CONFIRMATION_REQUIRED", "conflict")
        latest = self._latest(plan_id)
        if latest.project_id != envelope.project_id:
            raise DockyardCommandRejected(409, "PROJECT_ID_DIVERGENCE", "conflict")
        if envelope.expected_revision != latest.revision or latest.plan_digest != plan_digest:
            raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict")
        try:
            discarded = self._pm_service.discard(DockyardPlanDiscardRequest(
                plan_id,
                latest.revision,
                latest.content_digest,
                self._now(),
                envelope.command_id,
            ))
        except DockyardPlanConflictError as exc:
            raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict") from exc
        self._sse_hub.publish(
            project_id=envelope.project_id,
            event_type="project.changed",
            snapshot_commit=snapshot_commit,
            resource_id=plan_id,
            occurred_at=discarded.updated_at,
        )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "discarded",
            None,
            snapshot_commit,
            False,
            discarded.updated_at,
            _receipt_digest(envelope, "discarded", snapshot_commit, discarded.updated_at),
        )

    def _update_plan(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._plan_id is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"plan_id", "requirement", "tasks", "submit_for_approval"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        plan_id = _text(payload["plan_id"], "plan_id", 128)
        if plan_id != self._plan_id:
            raise DockyardCommandRejected(409, "PLAN_ID_DIVERGENCE", "conflict")
        if request.resource_id != plan_id or envelope.confirmation_id is not None:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        if payload["submit_for_approval"] is not True:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        requirement = _document_text(payload["requirement"], "requirement")
        latest = self._latest(plan_id)
        if latest.project_id != envelope.project_id:
            raise DockyardCommandRejected(409, "PROJECT_ID_DIVERGENCE", "conflict")
        pending_operation = _approval_pending_operation_id(envelope.command_id)
        finalized_replay = (
            latest.phase is DockyardPlanPhase.APPROVAL_PENDING
            and latest.last_operation_id == pending_operation
        )
        if finalized_replay:
            if envelope.expected_revision != latest.revision - 2:
                raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
            merged = _edited_tasks(payload["tasks"], latest.tasks)
            if latest.requirement != requirement or merged != latest.tasks:
                raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict")
            pending = latest
        else:
            edit_replay = (
                latest.phase is DockyardPlanPhase.EDITED
                and latest.last_operation_id == envelope.command_id
            )
            if edit_replay:
                if envelope.expected_revision != latest.revision - 1:
                    raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
                merged = _edited_tasks(payload["tasks"], latest.tasks)
                if latest.requirement != requirement or merged != latest.tasks:
                    raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict")
                edited = latest
            else:
                if envelope.expected_revision != latest.revision:
                    raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
                merged = _edited_tasks(payload["tasks"], latest.tasks)
                try:
                    edited = self._pm_service.edit(DockyardPlanEditRequest(
                        plan_id,
                        latest.revision,
                        latest.content_digest,
                        requirement,
                        merged,
                        self._now(),
                        envelope.command_id,
                    ))
                except DockyardPlanConflictError as exc:
                    raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict") from exc
            try:
                pending = self._pm_service.request_approval(DockyardPlanTransitionRequest(
                    plan_id,
                    edited.revision,
                    edited.content_digest,
                    self._now(),
                    pending_operation,
                ))
            except DockyardPlanConflictError as exc:
                raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict") from exc
        if not finalized_replay:
            self._sse_hub.publish(
                project_id=envelope.project_id,
                event_type="approval.changed",
                snapshot_commit=snapshot_commit,
                resource_id=plan_id,
                occurred_at=pending.updated_at,
            )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "approval_pending",
            None,
            snapshot_commit,
            finalized_replay,
            pending.updated_at,
            _receipt_digest(envelope, "approval_pending", snapshot_commit, pending.updated_at),
        )

    def _approve_plan(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"plan_id", "plan_digest", "task_count"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        plan_id = _text(payload["plan_id"], "plan_id", 128)
        if request.resource_id != plan_id:
            raise DockyardCommandRejected(409, "RESOURCE_ID_DIVERGENCE", "conflict")
        plan_digest = _sha(payload["plan_digest"])
        task_count = _integer(payload["task_count"], "task_count")
        if envelope.confirmation_id is None:
            raise DockyardCommandRejected(400, "CONFIRMATION_REQUIRED", "input")
        latest = self._latest(plan_id)
        if latest.project_id != envelope.project_id:
            raise DockyardCommandRejected(409, "PROJECT_ID_DIVERGENCE", "conflict")
        materialize_operation = _materialize_operation_id(envelope.command_id)
        finalized_replay = (
            latest.phase is DockyardPlanPhase.MATERIALIZED
            and latest.last_operation_id == materialize_operation
        )
        providers_ready = (
            self._ready_provider_ids is None
            or all(task.provider_id in self._ready_provider_ids for task in latest.tasks)
        )
        if not providers_ready and not finalized_replay:
            raise DockyardCommandRejected(409, "PROVIDER_NOT_READY", "not_ready")
        if finalized_replay:
            if (
                envelope.expected_revision != latest.revision - 2
                or latest.plan_digest != plan_digest
                or len(latest.tasks) != task_count
            ):
                raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict")
            materialized = latest
        else:
            approval_replay = (
                latest.phase is DockyardPlanPhase.APPROVED
                and latest.last_operation_id == envelope.command_id
            )
            if approval_replay:
                if envelope.expected_revision != latest.revision - 1:
                    raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
                approved = latest
            else:
                if latest.phase is not DockyardPlanPhase.APPROVAL_PENDING:
                    raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict")
                if envelope.expected_revision != latest.revision:
                    raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
                try:
                    approved = self._pm_service.approve(DockyardPlanApprovalRequest(
                        plan_id=plan_id,
                        expected_revision=latest.revision,
                        expected_content_digest=latest.content_digest,
                        expected_plan_digest=plan_digest,
                        expected_task_count=task_count,
                        approved_at=self._now(),
                        confirmation_id=envelope.confirmation_id,
                        operation_id=envelope.command_id,
                    ))
                except DockyardPlanConflictError as exc:
                    raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict") from exc
            try:
                materialized = self._pm_service.materialize(DockyardPlanMaterializeRequest(
                    plan_id=plan_id,
                    expected_revision=approved.revision,
                    expected_content_digest=approved.content_digest,
                    expected_plan_digest=plan_digest,
                    materialized_at=self._now(),
                    operation_id=materialize_operation,
                )).plan
            except DockyardPlanConflictError as exc:
                raise DockyardCommandRejected(409, "PLAN_CONFLICT", "conflict") from exc
        completed_at = materialized.materialized_at
        if completed_at is None:
            raise DockyardCommandRejected(500, "MATERIALIZATION_TIMESTAMP_MISSING", "internal")
        if materialized.approved_at is None or envelope.confirmation_id is None:
            raise DockyardCommandRejected(500, "PLAN_APPROVAL_EVIDENCE_MISSING", "internal")
        authorization = with_handoff_digest(DispatchApprovalAuthorization(
            DISPATCH_AUTHORIZATION_SCHEMA_VERSION,
            materialized.plan_id,
            materialized.revision - 1,
            "sha256:" + materialized.plan_digest,
            envelope.confirmation_id,
            envelope.command_id,
            materialized.approved_at,
            "sha256:" + "0" * 64,
        ))
        assert isinstance(authorization, DispatchApprovalAuthorization)
        if self._task_admission is not None and providers_ready:
            for task in materialized.tasks:
                relative_path = f"docs/pm/tasks/{task.task_id}-r1-dockyard.md"
                card = self._task_admission.root / relative_path
                try:
                    card_digest = "sha256:" + hashlib.sha256(card.read_bytes()).hexdigest()
                except OSError as exc:
                    raise DockyardCommandRejected(500, "MATERIALIZED_CARD_UNAVAILABLE", "internal") from exc
                evidence = MaterializedTaskEvidence(
                    HANDOFF_SCHEMA_VERSION, materialized.project_id,
                    materialized.plan_id, materialized.revision,
                    "sha256:" + materialized.content_digest,
                    materialize_operation, task.task_id, 1, relative_path,
                    card_digest, materialized.pm_owner_id, completed_at,
                    "sha256:" + "0" * 64,
                )
                self._task_admission.execute(
                    materialized, task, with_handoff_digest(evidence), authorization
                )
        if not finalized_replay:
            self._sse_hub.publish(
                project_id=envelope.project_id,
                event_type="approval.changed",
                snapshot_commit=snapshot_commit,
                resource_id=plan_id,
                occurred_at=completed_at,
            )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "materialized",
            None,
            snapshot_commit,
            finalized_replay,
            completed_at,
            _receipt_digest(envelope, "materialized", snapshot_commit, completed_at),
        )

    def _remove_project(
        self,
        request: DockyardCommandRequest,
        snapshot_commit: str,
    ) -> DockyardCommandReceipt:
        if self._project_registry is None:
            raise DockyardCommandRejected(409, "COMMAND_OWNER_UNAVAILABLE", "not_ready")
        envelope = request.envelope
        payload = _payload(request.payload_json)
        if set(payload) != {"project_id", "acknowledgement"}:
            raise DockyardCommandRejected(400, "COMMAND_PAYLOAD_INVALID", "input")
        project_id = _text(payload["project_id"], "project_id", 128)
        _text(payload["acknowledgement"], "acknowledgement", 256)
        if request.resource_id is not None or project_id != envelope.project_id:
            raise DockyardCommandRejected(409, "PROJECT_ID_DIVERGENCE", "conflict")
        if envelope.confirmation_id is None:
            raise DockyardCommandRejected(400, "CONFIRMATION_REQUIRED", "input")
        current = self._project_registry.read(project_id)
        if current is None:
            raise DockyardCommandRejected(404, "PROJECT_NOT_FOUND", "routing")
        replayed = current.last_operation_id == envelope.command_id
        if not replayed and envelope.expected_revision != current.generation:
            raise DockyardCommandRejected(409, "REVISION_STALE", "conflict")
        operation_time = current.updated_at if replayed else self._now()
        try:
            result = self._project_registry.request_removal(DockyardProjectCommand(
                project_id=project_id,
                expected_repository_identity=current.repository_identity,
                expected_generation=envelope.expected_revision,
                operation_id=envelope.command_id,
                occurred_at=operation_time,
            ))
        except DockyardProjectConflictError as exc:
            raise DockyardCommandRejected(409, "PROJECT_CONFLICT", "conflict") from exc
        except DockyardProjectRegistryError as exc:
            raise DockyardCommandRejected(500, "PROJECT_STORE_FAILED", "internal") from exc
        completed_at = result.record.updated_at
        self._sse_hub.publish(
            project_id=project_id,
            event_type="project.changed",
            snapshot_commit=snapshot_commit,
            resource_id=project_id,
            occurred_at=completed_at,
        )
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            project_id,
            envelope.command_type,
            "removal_pending",
            None,
            snapshot_commit,
            result.replayed,
            completed_at,
            _receipt_digest(envelope, "removal_pending", snapshot_commit, completed_at),
        )

    def _latest(self, plan_id: str) -> DockyardPlanRecord:
        latest = self._pm_service.latest(plan_id)
        if latest is None:
            raise DockyardCommandRejected(404, "PLAN_NOT_FOUND", "routing")
        return latest
