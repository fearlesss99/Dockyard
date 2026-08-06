"""Compose Dockyard durable review evidence into the existing owner APIs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from control_plane_transition import (
    IntegrationPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
)
from dockyard_post_delivery_owner_projection import (
    DockyardOwnerPlanProjectionRequest,
    digest_owner_value,
    project_post_delivery_owner_plan,
)
from dockyard_post_delivery_review_store import (
    DockyardPostDeliveryReviewOutcome,
    DockyardPostDeliveryReviewPhase,
    DockyardPostDeliveryReviewReceipt,
    DockyardPostDeliveryReviewStore,
)
from git_integration_owner import (
    GitIntegrationOutcome,
    GitIntegrationOwner,
    GitIntegrationPhase,
    GitIntegrationReceipt,
)
from mad_gateway import MadGatewayConfig
from state_provider import StateProvider
from workflow_orchestrator import (
    AcceptanceCycleResult,
    BlockedAuditResult,
    DeliveryRemediationResult,
    DurableAcceptanceCycleRequest,
    DurableBlockedAuditRequest,
    DurableDeliveryRemediationRequest,
    DurableGitIntegrationEvidence,
    DurableIntegrationCompletionRequest,
    WorkflowOrchestrator,
)


class DockyardPostDeliveryCompositionError(ValueError):
    pass


class DockyardPostDeliveryCompositionOutcome(str, Enum):
    FINALIZED = "FINALIZED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True, slots=True)
class DockyardPostDeliveryCompositionTimes:
    review_reserved_at: str
    mad_started_at: str
    mad_completed_at: str
    review_applied_at: str
    integration_applied_at: str
    finalized_at: str


@dataclass(frozen=True, slots=True)
class DockyardPostDeliveryCompositionRequest:
    projection_request: DockyardOwnerPlanProjectionRequest
    audit_config: MadGatewayConfig
    times: DockyardPostDeliveryCompositionTimes


@dataclass(frozen=True, slots=True)
class DockyardPostDeliveryCompositionResult:
    review_receipt: DockyardPostDeliveryReviewReceipt
    outcome: DockyardPostDeliveryCompositionOutcome
    acceptance_result: AcceptanceCycleResult | None
    remediation_result: DeliveryRemediationResult | None
    blocked_result: BlockedAuditResult | None
    git_receipt: GitIntegrationReceipt | None
    integration_result: AcceptanceCycleResult | None
    replayed: bool


def _validate_timestamp(value: object, field: str) -> None:
    if type(value) is not str or not value.endswith("Z") or "T" not in value:
        raise DockyardPostDeliveryCompositionError(f"review_composition:{field}")


def _recovery(
    receipt: DockyardPostDeliveryReviewReceipt,
    *,
    replayed: bool = False,
) -> DockyardPostDeliveryCompositionResult:
    return DockyardPostDeliveryCompositionResult(
        receipt,
        (
            DockyardPostDeliveryCompositionOutcome.FINALIZED
            if receipt.phase is DockyardPostDeliveryReviewPhase.FINALIZED
            else DockyardPostDeliveryCompositionOutcome.RECOVERY_REQUIRED
        ),
        None,
        None,
        None,
        None,
        None,
        replayed,
    )


async def run_dockyard_post_delivery_composition(
    request: DockyardPostDeliveryCompositionRequest,
    orchestrator: WorkflowOrchestrator,
    git_owner: GitIntegrationOwner,
) -> DockyardPostDeliveryCompositionResult:
    """Run one normal post-delivery route without crossing owner boundaries."""
    if type(request) is not DockyardPostDeliveryCompositionRequest:
        raise DockyardPostDeliveryCompositionError("review_composition:request")
    if type(request.projection_request) is not DockyardOwnerPlanProjectionRequest:
        raise DockyardPostDeliveryCompositionError("review_composition:projection")
    if type(request.audit_config) is not MadGatewayConfig:
        raise DockyardPostDeliveryCompositionError("review_composition:audit_config")
    if type(request.times) is not DockyardPostDeliveryCompositionTimes:
        raise DockyardPostDeliveryCompositionError("review_composition:times")
    for field in request.times.__dataclass_fields__:
        _validate_timestamp(getattr(request.times, field), field)
    root = request.projection_request.project_root
    if type(orchestrator) is not WorkflowOrchestrator or orchestrator.project_root != root:
        raise DockyardPostDeliveryCompositionError("review_composition:orchestrator")
    if type(git_owner) is not GitIntegrationOwner or git_owner.root != root.resolve():
        raise DockyardPostDeliveryCompositionError("review_composition:git_owner")

    store = DockyardPostDeliveryReviewStore(root)
    observed = store.read_receipt(request.projection_request.review_id)
    if observed.phase is DockyardPostDeliveryReviewPhase.FINALIZED:
        return _recovery(observed, replayed=True)
    if observed.phase is not DockyardPostDeliveryReviewPhase.DELIVERY_BOUND:
        return _recovery(observed)

    plan = project_post_delivery_owner_plan(request.projection_request)
    receipt = store.advance(
        plan.review_id,
        DockyardPostDeliveryReviewPhase.REVIEW_RESERVED,
        DockyardPostDeliveryReviewOutcome.RESERVED,
        request.times.review_reserved_at,
    )
    receipt = store.advance(
        plan.review_id,
        DockyardPostDeliveryReviewPhase.MAD_STARTED,
        DockyardPostDeliveryReviewOutcome.RESERVED,
        request.times.mad_started_at,
    )

    acceptance = await orchestrator.run_durable_acceptance_cycle(
        DurableAcceptanceCycleRequest(
            plan.delivery_evidence,
            plan.audit_input,
            plan.acceptance_transition_request,
            None,
            plan.worker_kind,
            plan.holder_instance_id,
        ),
        request.audit_config,
    )
    verdict = acceptance.audit_result.verdict
    outcome_by_verdict = {
        "pass": DockyardPostDeliveryReviewOutcome.AUDIT_PASS,
        "fail": DockyardPostDeliveryReviewOutcome.AUDIT_FAIL,
        "blocked": DockyardPostDeliveryReviewOutcome.AUDIT_BLOCKED,
    }
    if verdict not in outcome_by_verdict:
        raise DockyardPostDeliveryCompositionError("review_composition:verdict")
    receipt = store.advance(
        plan.review_id,
        DockyardPostDeliveryReviewPhase.MAD_COMPLETED,
        outcome_by_verdict[verdict],
        request.times.mad_completed_at,
        audit_result_id=acceptance.audit_result.deliberation_id,
        audit_result_digest=digest_owner_value(acceptance.audit_result),
        audit_verdict=verdict,
    )

    remediation = None
    blocked = None
    git_receipt = None
    integration = None
    if verdict == "fail":
        if plan.return_transition_request is None or plan.requeue_transition_request is None:
            raise DockyardPostDeliveryCompositionError("review_composition:fail_route")
        remediation = await orchestrator.run_durable_delivery_remediation(
            DurableDeliveryRemediationRequest(
                acceptance,
                plan.delivery_evidence,
                plan.return_transition_request,
                plan.requeue_transition_request,
                plan.worker_kind,
                plan.holder_instance_id,
            )
        )
        receipt = store.advance(
            plan.review_id,
            DockyardPostDeliveryReviewPhase.REVIEW_APPLIED,
            DockyardPostDeliveryReviewOutcome.RETURNED,
            request.times.review_applied_at,
            applied_event_id=remediation.return_transition.event_id,
        )
    elif verdict == "blocked":
        if plan.block_transition_request is None:
            raise DockyardPostDeliveryCompositionError("review_composition:blocked_route")
        blocked = await orchestrator.run_durable_blocked_audit(
            DurableBlockedAuditRequest(
                acceptance,
                plan.delivery_evidence,
                plan.block_transition_request,
                plan.worker_kind,
            )
        )
        receipt = store.advance(
            plan.review_id,
            DockyardPostDeliveryReviewPhase.REVIEW_APPLIED,
            DockyardPostDeliveryReviewOutcome.BLOCKED,
            request.times.review_applied_at,
            applied_event_id=blocked.block_transition.event_id,
        )
    else:
        if acceptance.accept_transition is None:
            raise DockyardPostDeliveryCompositionError("review_composition:acceptance")
        receipt = store.advance(
            plan.review_id,
            DockyardPostDeliveryReviewPhase.REVIEW_APPLIED,
            DockyardPostDeliveryReviewOutcome.ACCEPTED,
            request.times.review_applied_at,
            applied_event_id=acceptance.accept_transition.event_id,
        )
        intent = plan.git_integration_intent
        if intent is not None:
            git_receipt = git_owner.integrate(intent.request)
            if (
                git_receipt.phase is not GitIntegrationPhase.FINALIZED
                or git_receipt.outcome is not GitIntegrationOutcome.FINALIZED
                or git_receipt.integrated_commit is None
                or git_receipt.integrated_tree is None
            ):
                return _recovery(receipt)
            git_evidence = DurableGitIntegrationEvidence(
                git_receipt.operation_id,
                git_receipt.request_content_digest.removeprefix("sha256:"),
                git_receipt.task_id,
                git_receipt.revision,
                git_receipt.attempt,
                git_receipt.dispatch_id,
                git_receipt.report_commit,
                git_receipt.target_branch,
                git_receipt.target_head_before,
                git_receipt.phase.value,
                git_receipt.method.value,
                git_receipt.outcome.value,
                git_receipt.integrated_commit,
                git_receipt.integrated_tree,
                git_receipt.content_digest.removeprefix("sha256:"),
            )
            snapshot = StateProvider(root).snapshot()
            transition = TransitionRequest(
                TransitionCAS(
                    git_receipt.task_id,
                    git_receipt.revision,
                    "accepted",
                    snapshot.read_hexsha,
                ),
                None,
                intent.event_id,
                "CHANGE_INTEGRATED",
                IntegrationPayload(git_receipt.integrated_commit, None, None),
                TransitionEventContext(
                    None,
                    (git_evidence.receipt_content_digest,),
                    (),
                ),
            )
            integration = await orchestrator.run_durable_integration_completion(
                DurableIntegrationCompletionRequest(
                    acceptance,
                    plan.delivery_evidence,
                    git_evidence,
                    transition,
                )
            )
            receipt = store.advance(
                plan.review_id,
                DockyardPostDeliveryReviewPhase.INTEGRATION_APPLIED,
                DockyardPostDeliveryReviewOutcome.INTEGRATED,
                request.times.integration_applied_at,
                applied_event_id=intent.event_id,
            )

    receipt = store.advance(
        plan.review_id,
        DockyardPostDeliveryReviewPhase.FINALIZED,
        DockyardPostDeliveryReviewOutcome.FINALIZED,
        request.times.finalized_at,
    )
    return DockyardPostDeliveryCompositionResult(
        receipt,
        DockyardPostDeliveryCompositionOutcome.FINALIZED,
        acceptance,
        remediation,
        blocked,
        git_receipt,
        integration,
        False,
    )


__all__ = [
    "DockyardPostDeliveryCompositionError",
    "DockyardPostDeliveryCompositionOutcome",
    "DockyardPostDeliveryCompositionTimes",
    "DockyardPostDeliveryCompositionRequest",
    "DockyardPostDeliveryCompositionResult",
    "run_dockyard_post_delivery_composition",
]
