"""Deterministic Dockyard projection into existing post-delivery owners."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path

from control_plane_transition import TransitionRequest
from core_types import WorkerKind
from dockyard_post_delivery_review_store import (
    DockyardPostDeliveryReviewPhase,
    DockyardPostDeliveryReviewStore,
)
from git_integration_owner import GitIntegrationRequest, with_request_digest
from mad_audit_gateway import MadAuditGatewayInput
from worker_output_decoder import DeliveryReceipt
from workflow_orchestrator import DurableDeliveryReviewEvidence


class DockyardOwnerProjectionError(ValueError):
    pass


class DockyardOwnerProjectionInputError(DockyardOwnerProjectionError):
    pass


class DockyardOwnerProjectionConflictError(DockyardOwnerProjectionError):
    pass


@dataclass(frozen=True, slots=True)
class DockyardOwnerPlanProjectionRequest:
    project_root: Path
    review_id: str
    delivery_receipt: DeliveryReceipt
    workspace: Path
    worker_kind: WorkerKind
    audit_input: MadAuditGatewayInput
    acceptance_transition_request: TransitionRequest
    git_integration_request: GitIntegrationRequest | None
    return_transition_request: TransitionRequest | None
    requeue_transition_request: TransitionRequest | None
    block_transition_request: TransitionRequest | None
    holder_instance_id: str


@dataclass(frozen=True, slots=True)
class DockyardGitIntegrationIntent:
    event_id: str
    request: GitIntegrationRequest

    def __post_init__(self) -> None:
        _text(self.event_id, "integration_event_id")
        if type(self.request) is not GitIntegrationRequest:
            raise DockyardOwnerProjectionInputError("owner_projection:git_request")


@dataclass(frozen=True, slots=True)
class DockyardPostDeliveryOwnerPlan:
    review_id: str
    delivery_evidence: DurableDeliveryReviewEvidence
    audit_input: MadAuditGatewayInput
    acceptance_transition_request: TransitionRequest
    git_integration_intent: DockyardGitIntegrationIntent | None
    return_transition_request: TransitionRequest | None
    requeue_transition_request: TransitionRequest | None
    block_transition_request: TransitionRequest | None
    worker_kind: WorkerKind
    holder_instance_id: str
    audit_config_id: str
    content_digest: str


def _plain(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__name__,
            "fields": [[field.name, _plain(getattr(value, field.name))] for field in fields(value)],
        }
    if value is None or type(value) in (str, int, bool):
        return value
    raise DockyardOwnerProjectionInputError("owner_projection:unsupported_value")


def digest_owner_value(value: object) -> str:
    """Return one type-sensitive canonical SHA-256 projection digest."""
    encoded = json.dumps(
        _plain(value), ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def digest_worktree_identity(
    worktree_id: str,
    workspace: Path,
    branch: str,
    base_commit: str,
) -> str:
    return digest_owner_value((worktree_id, workspace, branch, base_commit))


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DockyardOwnerProjectionInputError(f"owner_projection:{field}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DockyardOwnerProjectionInputError(f"owner_projection:{field}")
    return value


def _same(actual: object, expected: object, field: str) -> None:
    if actual != expected:
        raise DockyardOwnerProjectionConflictError(f"owner_projection:{field}")


def _plan_digest(plan: DockyardPostDeliveryOwnerPlan) -> str:
    return digest_owner_value(tuple(
        getattr(plan, field.name) for field in fields(plan) if field.name != "content_digest"
    ))


def project_post_delivery_owner_plan(
    projection: DockyardOwnerPlanProjectionRequest,
) -> DockyardPostDeliveryOwnerPlan:
    """Read canonical review evidence and return a side-effect-free owner plan."""
    if type(projection) is not DockyardOwnerPlanProjectionRequest:
        raise DockyardOwnerProjectionInputError("owner_projection:request")
    if not isinstance(projection.project_root, Path) or not projection.project_root.is_absolute():
        raise DockyardOwnerProjectionInputError("owner_projection:project_root")
    if not isinstance(projection.workspace, Path) or not projection.workspace.is_absolute():
        raise DockyardOwnerProjectionInputError("owner_projection:workspace")
    _text(projection.review_id, "review_id")
    _text(projection.holder_instance_id, "holder_instance_id")
    if type(projection.delivery_receipt) is not DeliveryReceipt:
        raise DockyardOwnerProjectionInputError("owner_projection:delivery_receipt")
    if type(projection.worker_kind) is not WorkerKind:
        raise DockyardOwnerProjectionInputError("owner_projection:worker_kind")
    if type(projection.audit_input) is not MadAuditGatewayInput:
        raise DockyardOwnerProjectionInputError("owner_projection:audit_input")
    if type(projection.acceptance_transition_request) is not TransitionRequest:
        raise DockyardOwnerProjectionInputError("owner_projection:acceptance_transition")
    for name in (
        "return_transition_request", "requeue_transition_request",
        "block_transition_request",
    ):
        value = getattr(projection, name)
        if value is not None and type(value) is not TransitionRequest:
            raise DockyardOwnerProjectionInputError(f"owner_projection:{name}")
    if (
        projection.git_integration_request is not None
        and type(projection.git_integration_request) is not GitIntegrationRequest
    ):
        raise DockyardOwnerProjectionInputError("owner_projection:git_request")

    store = DockyardPostDeliveryReviewStore(projection.project_root)
    durable = store.read_request(projection.review_id)
    receipt = store.read_receipt(projection.review_id)
    _same(receipt.review_id, projection.review_id, "review_id")
    _same(receipt.request_content_digest, durable.content_digest, "request_digest")
    if receipt.phase is DockyardPostDeliveryReviewPhase.FINALIZED:
        raise DockyardOwnerProjectionConflictError("owner_projection:finalized")

    delivery = projection.delivery_receipt
    identity = delivery.identity
    for actual, expected, field in (
        (identity.task_id, durable.task_id, "task_id"),
        (identity.revision, durable.revision, "revision"),
        (identity.attempt, durable.attempt, "attempt"),
        (identity.dispatch_id, durable.dispatch_id, "dispatch_id"),
        (delivery.implementation_commit, durable.implementation_commit, "implementation_commit"),
        (delivery.report_commit, durable.report_commit, "report_commit"),
        (digest_owner_value(delivery), durable.delivery_receipt_digest, "delivery_receipt_digest"),
        (digest_owner_value(projection.audit_input), durable.audit_input_digest, "audit_input_digest"),
        (digest_owner_value(projection.acceptance_transition_request), durable.acceptance_request_digest, "acceptance_digest"),
        (projection.acceptance_transition_request.event_id, durable.acceptance_event_id, "acceptance_event_id"),
        (projection.audit_input.task_id, durable.task_id, "audit_task_id"),
        (projection.audit_input.dispatch_id, durable.dispatch_id, "audit_dispatch_id"),
        (projection.audit_input.workspace, projection.workspace, "audit_workspace"),
        (projection.audit_input.implementation_commit, durable.implementation_commit, "audit_implementation_commit"),
        (projection.audit_input.report_commit, durable.report_commit, "audit_report_commit"),
    ):
        _same(actual, expected, field)

    _same(
        digest_worktree_identity(
            durable.worktree_id, projection.workspace, durable.branch, durable.base_commit
        ),
        durable.worktree_identity_digest,
        "worktree_identity_digest",
    )
    routing = (
        projection.return_transition_request,
        projection.requeue_transition_request,
        projection.block_transition_request,
    )
    _same(digest_owner_value(routing), durable.routing_digest, "routing_digest")
    _same(
        tuple(value is not None for value in routing),
        (
            durable.return_event_id is not None,
            durable.requeue_event_id is not None,
            durable.block_event_id is not None,
        ),
        "routing_shape",
    )
    for transition, event_id, field in zip(
        routing,
        (durable.return_event_id, durable.requeue_event_id, durable.block_event_id),
        ("return_event_id", "requeue_event_id", "block_event_id"),
    ):
        if transition is not None:
            _same(transition.event_id, event_id, field)

    git_request = projection.git_integration_request
    if durable.integration_event_id is None:
        if git_request is not None:
            raise DockyardOwnerProjectionConflictError("owner_projection:unexpected_git_request")
    else:
        if git_request is None:
            raise DockyardOwnerProjectionConflictError("owner_projection:missing_git_request")
        _same(
            with_request_digest(git_request).content_digest,
            git_request.content_digest,
            "git_request_content",
        )
        _same(git_request.content_digest, durable.integration_request_digest, "git_request_digest")
        for actual, expected, field in (
            (git_request.project_root, projection.project_root, "git_project_root"),
            (git_request.task_id, durable.task_id, "git_task_id"),
            (git_request.revision, durable.revision, "git_revision"),
            (git_request.attempt, durable.attempt, "git_attempt"),
            (git_request.dispatch_id, durable.dispatch_id, "git_dispatch_id"),
            (git_request.source_worktree, projection.workspace, "git_source_worktree"),
            (git_request.source_branch, durable.branch, "git_source_branch"),
            (git_request.base_commit, durable.base_commit, "git_base_commit"),
            (git_request.implementation_commit, durable.implementation_commit, "git_implementation_commit"),
            (git_request.report_commit, durable.report_commit, "git_report_commit"),
        ):
            _same(actual, expected, field)

    evidence = DurableDeliveryReviewEvidence(
        delivery_receipt=delivery,
        dispatch_generation_id=durable.dispatch_generation_id,
        external_worker_receipt_digest=durable.external_worker_receipt_digest.removeprefix("sha256:"),
        dispatch_event_id=durable.dispatch_event_id,
        acknowledge_event_id=durable.acknowledge_event_id,
        delivery_event_id=durable.delivery_event_id,
        delivery_event_digest=durable.delivery_event_digest.removeprefix("sha256:"),
        worktree_id=durable.worktree_id,
        workspace=projection.workspace,
        implementation_commit=durable.implementation_commit,
        report_commit=durable.report_commit,
        worker_kind=projection.worker_kind,
    )
    git_intent = (
        None
        if git_request is None
        else DockyardGitIntegrationIntent(durable.integration_event_id, git_request)
    )
    plan = DockyardPostDeliveryOwnerPlan(
        projection.review_id,
        evidence,
        projection.audit_input,
        projection.acceptance_transition_request,
        git_intent,
        projection.return_transition_request,
        projection.requeue_transition_request,
        projection.block_transition_request,
        projection.worker_kind,
        projection.holder_instance_id,
        durable.audit_config_id,
        "sha256:" + "0" * 64,
    )
    return replace(plan, content_digest=_plan_digest(plan))


__all__ = [
    "DockyardOwnerPlanProjectionRequest",
    "DockyardGitIntegrationIntent",
    "DockyardPostDeliveryOwnerPlan",
    "DockyardOwnerProjectionError",
    "DockyardOwnerProjectionInputError",
    "DockyardOwnerProjectionConflictError",
    "digest_owner_value",
    "digest_worktree_identity",
    "project_post_delivery_owner_plan",
]
