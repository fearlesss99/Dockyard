"""Compose one completed Dockyard Worker into review and integration owners."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from control_plane_transition import (
    DeliveryAcceptedPayload,
    DeliveryReturnedPayload,
    DispatchCAS,
    IntegrationPayload,
    RequeuePayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
)
from approval_gate import (
    ApprovalCheckRequest,
    ApprovalGate,
    ApprovalNotFoundError,
    ApprovalScope,
    ApprovalSubject,
    write_grant,
)
from core_types import MadDeliberationDepth
from dockyard_post_delivery_composition import (
    DockyardPostDeliveryCompositionOutcome,
)
from dockyard_delivery_receipt_store import DockyardDeliveryReceiptStore
from dockyard_mad_result_store import DockyardMadResultStore
from dockyard_redispatch_receipt_store import (
    DockyardRedispatchReceipt,
    DockyardRedispatchReceiptStore,
    DockyardRedispatchStoreError,
)
from dockyard_post_delivery_owner_projection import (
    DockyardOwnerPlanProjectionRequest,
    digest_owner_value,
    digest_worktree_identity,
    project_post_delivery_owner_plan,
)
from dockyard_post_delivery_review_store import (
    SCHEMA_VERSION as REVIEW_SCHEMA_VERSION,
    DockyardPostDeliveryReviewOutcome,
    DockyardPostDeliveryReviewPhase,
    DockyardPostDeliveryReviewReceipt,
    DockyardPostDeliveryReviewRequest,
    DockyardPostDeliveryReviewStore,
    DockyardReviewNotFoundError,
    with_request_digest,
)
from git_integration_owner import (
    SCHEMA_VERSION as GIT_SCHEMA_VERSION,
    GitIntegrationMethod,
    GitIntegrationOutcome,
    GitIntegrationOwner,
    GitIntegrationPhase,
    GitIntegrationRequest,
    with_request_digest as with_git_request_digest,
)
from mad_audit_gateway import MadAuditGatewayInput
from mad_gateway import MadGatewayConfig
from pm_external_worker_runtime import PmExternalWorkerRunResult
from pm_materialization_admission_handoff import (
    MaterializationAdmissionPhase,
    MaterializationAdmissionReceipt,
)
from portfolio_scheduler_store import AdmissionPlanReservation, PortfolioSchedulerStore
from portfolio_scheduler_worker_handoff_store import (
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoffPhase,
)
from state_provider import EventEntry, StateProvider
from workflow_orchestrator import (
    AcceptanceCycleResult,
    DurableAcceptanceCycleRequest,
    DurableDeliveryRemediationRequest,
    DurableGitIntegrationEvidence,
    DurableIntegrationCompletionRequest,
    DispatchCycleResult,
    WorkflowOrchestrator,
)
from worktree_lifecycle_store import (
    WorktreeLifecycleStore,
    WorktreePhase,
    derive_worktree_id,
)


class DockyardTerminalCompositionError(ValueError):
    pass


class DockyardTerminalCompositionInputError(DockyardTerminalCompositionError):
    pass


class DockyardTerminalCompositionConflictError(DockyardTerminalCompositionError):
    pass


class DockyardTerminalCompositionRecoveryRequired(DockyardTerminalCompositionError):
    pass


@dataclass(frozen=True, slots=True)
class DockyardTerminalCompositionResult:
    review_id: str
    outcome: DockyardPostDeliveryCompositionOutcome
    review_phase: DockyardPostDeliveryReviewPhase
    replayed: bool


@dataclass(frozen=True, slots=True)
class _TerminalReviewContext:
    plan: AdmissionPlanReservation
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    worktree_id: str
    workspace: Path


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _git(root: Path, *arguments: str) -> str:
    try:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise DockyardTerminalCompositionConflictError(
            "terminal:git_evidence"
        ) from exc


def _event_id(kind: str, dispatch_id: str) -> str:
    return "EVT-" + kind + "-" + hashlib.sha256(
        (kind + "\0" + dispatch_id).encode("utf-8")
    ).hexdigest()[:24]


def _operation_id(kind: str, dispatch_id: str) -> str:
    return "OP-" + kind + "-" + hashlib.sha256(
        (kind + "\0" + dispatch_id).encode("utf-8")
    ).hexdigest()[:24]


def _timestamp(clock: object) -> str:
    value = getattr(clock, "now")()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DockyardTerminalCompositionInputError("terminal:clock")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if type(value) is str and value.endswith("Z") and "T" in value:
        return value
    raise DockyardTerminalCompositionInputError("terminal:clock")


def _config_id(config: MadGatewayConfig) -> str:
    values = (
        config.mad_executable,
        config.mad_home,
        str(config.timeout_seconds),
        *config.audit_agent_ids,
        config.audit_report_agent_id,
    )
    return "MADCFG-" + hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:24]


def _event(snapshot: object, event_id: str, event_type: str, dispatch_id: str) -> EventEntry:
    events = getattr(snapshot, "events", ())
    matches = [
        item for item in events
        if type(item) is EventEntry and item.event_id == event_id
    ]
    if len(matches) != 1:
        raise DockyardTerminalCompositionConflictError("terminal:event_identity")
    event = matches[0]
    if event.event_type != event_type or event.dispatch_id != dispatch_id:
        raise DockyardTerminalCompositionConflictError("terminal:event_binding")
    return event


def _remediation_transitions(
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    snapshot_commit: str,
    event_context: TransitionEventContext,
) -> tuple[TransitionRequest, TransitionRequest]:
    """Reserve deterministic return/requeue identities before MAD starts."""
    return (
        TransitionRequest(
            TransitionCAS(task_id, revision, "review_ready", snapshot_commit),
            DispatchCAS(dispatch_id, attempt),
            _event_id("RETURN", dispatch_id),
            "DELIVERY_RETURNED",
            DeliveryReturnedPayload(),
            event_context,
        ),
        TransitionRequest(
            TransitionCAS(task_id, revision, "returned", snapshot_commit),
            None,
            _event_id("REQUEUE", dispatch_id),
            "TASK_REQUEUED",
            RequeuePayload(),
            event_context,
        ),
    )


class DockyardTerminalOwnerCompositionRuntime:
    """Use typed durable evidence to enter MAD, acceptance and Git owners."""

    def __init__(
        self,
        project_root: Path,
        audit_config: MadGatewayConfig,
        integration_target_branch: str,
        *,
        clock: object | None = None,
    ) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardTerminalCompositionInputError("terminal:project_root")
        if type(audit_config) is not MadGatewayConfig:
            raise DockyardTerminalCompositionInputError("terminal:audit_config")
        if (
            type(integration_target_branch) is not str
            or not integration_target_branch
            or integration_target_branch != integration_target_branch.strip()
            or any(character in integration_target_branch for character in "\0\r\n")
        ):
            raise DockyardTerminalCompositionInputError("terminal:target_branch")
        self.root = project_root.resolve()
        self.audit_config = audit_config
        self.target_branch = integration_target_branch
        self.clock = clock or _SystemClock()

    async def execute(
        self,
        admission_receipt: MaterializationAdmissionReceipt,
        worker_run: PmExternalWorkerRunResult,
    ) -> DockyardTerminalCompositionResult:
        if (
            type(admission_receipt) is not MaterializationAdmissionReceipt
            or admission_receipt.phase is not MaterializationAdmissionPhase.FINALIZED
        ):
            raise DockyardTerminalCompositionInputError("terminal:admission_receipt")
        if type(worker_run) is not PmExternalWorkerRunResult:
            raise DockyardTerminalCompositionInputError("terminal:worker_run")
        if (
            worker_run.receipt.task_id,
            worker_run.receipt.revision,
            worker_run.receipt.dispatch_id,
        ) != (
            admission_receipt.task_id,
            admission_receipt.revision,
            admission_receipt.dispatch_id,
        ):
            raise DockyardTerminalCompositionConflictError("terminal:worker_identity")
        dispatch_id = worker_run.receipt.dispatch_id
        review_id = "REV-" + hashlib.sha256(
            (dispatch_id + "\0" + worker_run.receipt.content_digest).encode("utf-8")
        ).hexdigest()[:24]
        review_store = DockyardPostDeliveryReviewStore(self.root)
        try:
            observed = review_store.read_receipt(review_id)
        except DockyardReviewNotFoundError:
            observed = None
        if observed is not None:
            if (
                observed.task_id != worker_run.receipt.task_id
                or observed.revision != worker_run.receipt.revision
                or observed.attempt != worker_run.receipt.attempt
                or observed.dispatch_id != dispatch_id
            ):
                raise DockyardTerminalCompositionConflictError("terminal:review_replay")
            if observed.phase is DockyardPostDeliveryReviewPhase.FINALIZED:
                return DockyardTerminalCompositionResult(
                    review_id,
                    DockyardPostDeliveryCompositionOutcome.FINALIZED,
                    observed.phase,
                    True,
                )
            if worker_run.dispatch_cycle_result is None:
                raise DockyardTerminalCompositionRecoveryRequired(
                    "terminal:review_resume_required"
                )
        cycle = worker_run.dispatch_cycle_result
        if cycle is None:
            raise DockyardTerminalCompositionRecoveryRequired(
                "terminal:delivery_replay_requires_recovery"
            )
        delivery = cycle.delivery_receipt
        if delivery.identity.dispatch_id != dispatch_id:
            raise DockyardTerminalCompositionConflictError("terminal:delivery_identity")
        if admission_receipt.queue_id is None:
            raise DockyardTerminalCompositionConflictError("terminal:queue_identity")
        DockyardDeliveryReceiptStore(self.root).save(delivery)
        plan = PortfolioSchedulerStore(self.root).read_admission_plan(
            admission_receipt.queue_id, 1
        )
        if type(plan) is not AdmissionPlanReservation:
            raise DockyardTerminalCompositionConflictError("terminal:admission_plan")
        if (
            plan.task_id,
            plan.revision,
            plan.new_attempt,
            plan.dispatch_id,
            plan.receipt_id,
        ) != (
            delivery.identity.task_id,
            delivery.identity.revision,
            delivery.identity.attempt,
            delivery.identity.dispatch_id,
            admission_receipt.schedule_receipt_id,
        ):
            raise DockyardTerminalCompositionConflictError("terminal:plan_binding")

        handoff_store = PortfolioSchedulerWorkerHandoffStore(self.root)
        handoff = handoff_store.read_handoff(dispatch_id)
        handoff_receipt = handoff_store.read_handoff_receipt(dispatch_id)
        if (
            handoff.phase is not ScheduledDispatchHandoffPhase.FINALIZED
            or handoff_receipt.phase is not ScheduledDispatchHandoffPhase.FINALIZED
            or handoff.handoff_id != handoff_receipt.handoff_id
            or handoff.dispatch_id != dispatch_id
            or handoff_receipt.dispatch_id != dispatch_id
        ):
            raise DockyardTerminalCompositionConflictError("terminal:handoff_binding")

        common_raw = Path(_git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        common = str((common_raw if common_raw.is_absolute() else self.root / common_raw).resolve())
        worktree_id = derive_worktree_id(
            common,
            str(self.root),
            plan.branch,
            plan.base_commit,
            plan.task_id,
            plan.revision,
            plan.new_attempt,
            plan.dispatch_id,
        )
        record = WorktreeLifecycleStore(self.root).read_record(worktree_id)
        if (
            record is None
            or record.phase is not WorktreePhase.READY
            or record.worktree_path != plan.canonical_worktree
            or record.branch != plan.branch
            or record.base_commit != plan.base_commit
        ):
            raise DockyardTerminalCompositionConflictError("terminal:worktree_binding")
        workspace = Path(record.worktree_path)

        snapshot = StateProvider(self.root).snapshot()
        tasks = [
            item for item in snapshot.tasks
            if item.task_id == plan.task_id and item.revision == plan.revision
        ]
        if len(tasks) != 1 or tasks[0].state != "review_ready":
            raise DockyardTerminalCompositionConflictError("terminal:canonical_task")
        task = tasks[0]
        if task.current_dispatch is None or task.current_dispatch.dispatch_id != dispatch_id:
            raise DockyardTerminalCompositionConflictError("terminal:canonical_dispatch")
        dispatch_event = _event(snapshot, cycle.dispatch_transition.event_id, "TASK_DISPATCHED", dispatch_id)
        acknowledge_event = _event(snapshot, cycle.acknowledge_transition.event_id, "DISPATCH_ACKNOWLEDGED", dispatch_id)
        delivery_event = _event(snapshot, cycle.delivery_transition.event_id, "DELIVERY_SUBMITTED", dispatch_id)
        canonical_head = _git(self.root, "rev-parse", "HEAD")

        acceptance_event_id = _event_id("ACCEPT", dispatch_id)
        integration_event_id = _event_id("INTEGRATE", dispatch_id)
        event_context = TransitionEventContext(
            None,
            (worker_run.receipt.content_digest.removeprefix("sha256:"),),
            (),
        )
        acceptance = TransitionRequest(
            TransitionCAS(plan.task_id, plan.revision, "review_ready", canonical_head),
            DispatchCAS(dispatch_id, plan.new_attempt),
            acceptance_event_id,
            "DELIVERY_ACCEPTED",
            DeliveryAcceptedPayload(
                delivery.implementation_commit,
                f"docs/pm/acceptances/{plan.task_id}-r{plan.revision}-a{plan.new_attempt}-review1.md",
                (),
                ("durable Worker delivery receipt", "MAD audit verdict"),
                "Dockyard accepts only after the configured MAD audit passes.",
            ),
            event_context,
        )
        return_transition, requeue_transition = _remediation_transitions(
            plan.task_id,
            plan.revision,
            plan.new_attempt,
            dispatch_id,
            canonical_head,
            event_context,
        )
        audit_input = MadAuditGatewayInput(
            self.root,
            plan.task_id,
            dispatch_id,
            f"审议 {plan.task_id} 的实现与交付报告是否满足任务卡。",
            workspace,
            plan.task_card_commit,
            plan.task_card_path,
            plan.report_path,
            delivery.report_commit,
            plan.base_commit,
            delivery.implementation_commit,
            MadDeliberationDepth.BALANCED,
        )
        reserved_at = _timestamp(self.clock)
        target_head = _git(self.root, "rev-parse", f"refs/heads/{self.target_branch}")
        git_request = with_git_request_digest(GitIntegrationRequest(
            GIT_SCHEMA_VERSION,
            _operation_id("GIT", dispatch_id),
            self.root,
            plan.task_id,
            plan.revision,
            plan.new_attempt,
            dispatch_id,
            workspace,
            plan.branch,
            plan.base_commit,
            delivery.implementation_commit,
            delivery.report_commit,
            self.target_branch,
            target_head,
            GitIntegrationMethod.MERGE_TREE,
            f"Integrate {plan.task_id} via Dockyard",
            reserved_at,
            "sha256:" + "0" * 64,
        ))
        assert isinstance(git_request, GitIntegrationRequest)
        review_request = with_request_digest(DockyardPostDeliveryReviewRequest(
            REVIEW_SCHEMA_VERSION,
            _operation_id("REVIEW", dispatch_id),
            admission_receipt.project_id,
            plan.task_id,
            plan.revision,
            plan.new_attempt,
            dispatch_id,
            handoff_receipt.generation_id,
            plan.queue_id,
            plan.receipt_id,
            plan.content_digest,
            handoff.handoff_id,
            worker_run.receipt.content_digest,
            dispatch_event.event_id,
            digest_owner_value(dispatch_event),
            acknowledge_event.event_id,
            digest_owner_value(acknowledge_event),
            delivery_event.event_id,
            digest_owner_value(delivery_event),
            digest_owner_value(delivery),
            delivery.implementation_commit,
            delivery.report_commit,
            worktree_id,
            digest_worktree_identity(worktree_id, workspace, plan.branch, plan.base_commit),
            plan.branch,
            plan.base_commit,
            digest_owner_value(audit_input),
            _config_id(self.audit_config),
            acceptance_event_id,
            digest_owner_value(acceptance),
            integration_event_id,
            git_request.content_digest,
            return_transition.event_id,
            requeue_transition.event_id,
            None,
            digest_owner_value((return_transition, requeue_transition, None)),
            reserved_at,
            "sha256:" + "0" * 64,
        ))
        review_receipt = review_store.reserve(review_id, review_request)
        projection = DockyardOwnerPlanProjectionRequest(
            self.root,
            review_id,
            delivery,
            workspace,
            plan.worker_kind,
            audit_input,
            acceptance,
            git_request,
            return_transition,
            requeue_transition,
            None,
            plan.holder_instance_id,
        )
        owner_plan = project_post_delivery_owner_plan(projection)
        orchestrator = WorkflowOrchestrator(self.root, self.clock)
        acceptance_request = DurableAcceptanceCycleRequest(
            owner_plan.delivery_evidence,
            owner_plan.audit_input,
            owner_plan.acceptance_transition_request,
            None,
            owner_plan.worker_kind,
            owner_plan.holder_instance_id,
        )
        if review_receipt.phase is DockyardPostDeliveryReviewPhase.DELIVERY_BOUND:
            review_receipt = review_store.advance(
                review_id,
                DockyardPostDeliveryReviewPhase.REVIEW_RESERVED,
                DockyardPostDeliveryReviewOutcome.RESERVED,
                _timestamp(self.clock),
            )
            review_receipt = review_store.advance(
                review_id,
                DockyardPostDeliveryReviewPhase.MAD_STARTED,
                DockyardPostDeliveryReviewOutcome.RESERVED,
                _timestamp(self.clock),
            )
            audit_result = await orchestrator.run_durable_audit_cycle(
                acceptance_request, self.audit_config
            )
            DockyardMadResultStore(self.root).save(review_id, audit_result)
            outcomes = {
                "pass": DockyardPostDeliveryReviewOutcome.AUDIT_PASS,
                "fail": DockyardPostDeliveryReviewOutcome.AUDIT_FAIL,
                "blocked": DockyardPostDeliveryReviewOutcome.AUDIT_BLOCKED,
            }
            outcome = outcomes.get(audit_result.verdict)
            if outcome is None:
                raise DockyardTerminalCompositionConflictError(
                    "terminal:audit_verdict"
                )
            review_receipt = review_store.advance(
                review_id,
                DockyardPostDeliveryReviewPhase.MAD_COMPLETED,
                outcome,
                _timestamp(self.clock),
                audit_result_id=audit_result.deliberation_id,
                audit_result_digest=digest_owner_value(audit_result),
                audit_verdict=audit_result.verdict,
            )
        return DockyardTerminalCompositionResult(
            review_id,
            DockyardPostDeliveryCompositionOutcome.RECOVERY_REQUIRED,
            review_receipt.phase,
            observed is not None,
        )

    async def execute_redispatch(
        self,
        source_review_id: str,
        redispatch: DockyardRedispatchReceipt,
        cycle: DispatchCycleResult,
    ) -> DockyardTerminalCompositionResult:
        """Enter the existing review owner from durable returned-retry evidence."""
        if type(source_review_id) is not str or not source_review_id:
            raise DockyardTerminalCompositionInputError("terminal:source_review_id")
        if type(redispatch) is not DockyardRedispatchReceipt:
            raise DockyardTerminalCompositionInputError("terminal:redispatch_receipt")
        if type(cycle) is not DispatchCycleResult:
            raise DockyardTerminalCompositionInputError("terminal:redispatch_cycle")
        durable_redispatch = DockyardRedispatchReceiptStore(self.root).read(
            redispatch.dispatch_id
        )
        if durable_redispatch != redispatch or redispatch.source_review_id != source_review_id:
            raise DockyardTerminalCompositionConflictError("terminal:redispatch_replay")

        review_store = DockyardPostDeliveryReviewStore(self.root)
        source_receipt = review_store.read_receipt(source_review_id)
        source_request = review_store.read_request(source_review_id)
        if (
            source_receipt.phase is not DockyardPostDeliveryReviewPhase.FINALIZED
            or source_receipt.audit_verdict != "fail"
            or source_receipt.content_digest != redispatch.source_review_receipt_digest
            or source_receipt.task_id != redispatch.task_id
            or source_receipt.revision != redispatch.revision
            or source_receipt.attempt + 1 != redispatch.attempt
            or source_receipt.dispatch_id == redispatch.dispatch_id
            or source_request.content_digest != source_receipt.request_content_digest
        ):
            raise DockyardTerminalCompositionConflictError("terminal:redispatch_source")
        delivery = cycle.delivery_receipt
        if (
            delivery.identity.task_id != redispatch.task_id
            or delivery.identity.revision != redispatch.revision
            or delivery.identity.attempt != redispatch.attempt
            or delivery.identity.dispatch_id != redispatch.dispatch_id
            or digest_owner_value(delivery) != redispatch.delivery_receipt_digest
        ):
            raise DockyardTerminalCompositionConflictError("terminal:redispatch_delivery")

        source_context = self._resolve_review_context(
            source_receipt, source_request
        )
        plan = source_context.plan
        if (
            source_context.task_id != redispatch.task_id
            or source_context.revision != redispatch.revision
            or source_context.attempt != source_receipt.attempt
            or source_context.dispatch_id != source_receipt.dispatch_id
        ):
            raise DockyardTerminalCompositionConflictError("terminal:redispatch_plan")
        workspace = source_context.workspace

        snapshot = StateProvider(self.root).snapshot()
        task = next(
            (
                item for item in snapshot.tasks
                if item.task_id == redispatch.task_id
                and item.revision == redispatch.revision
            ),
            None,
        )
        if (
            task is None
            or task.state != "review_ready"
            or task.attempt != redispatch.attempt
            or task.current_dispatch is None
            or task.current_dispatch.dispatch_id != redispatch.dispatch_id
        ):
            raise DockyardTerminalCompositionConflictError("terminal:redispatch_task")
        dispatch_event = _event(
            snapshot, redispatch.dispatch_event_id, "TASK_DISPATCHED",
            redispatch.dispatch_id,
        )
        acknowledge_event = _event(
            snapshot, redispatch.acknowledge_event_id, "DISPATCH_ACKNOWLEDGED",
            redispatch.dispatch_id,
        )
        delivery_event = _event(
            snapshot, redispatch.delivery_event_id, "DELIVERY_SUBMITTED",
            redispatch.dispatch_id,
        )
        if (
            digest_owner_value(dispatch_event) != redispatch.dispatch_event_digest
            or digest_owner_value(acknowledge_event) != redispatch.acknowledge_event_digest
            or digest_owner_value(delivery_event) != redispatch.delivery_event_digest
        ):
            raise DockyardTerminalCompositionConflictError("terminal:redispatch_events")
        DockyardDeliveryReceiptStore(self.root).save(delivery)

        review_id = "REV-" + hashlib.sha256(
            (redispatch.dispatch_id + "\0" + redispatch.content_digest).encode("utf-8")
        ).hexdigest()[:24]
        try:
            observed = review_store.read_receipt(review_id)
        except DockyardReviewNotFoundError:
            observed = None
        if observed is not None:
            if (
                observed.task_id != redispatch.task_id
                or observed.revision != redispatch.revision
                or observed.attempt != redispatch.attempt
                or observed.dispatch_id != redispatch.dispatch_id
            ):
                raise DockyardTerminalCompositionConflictError("terminal:review_replay")
            if observed.phase is DockyardPostDeliveryReviewPhase.FINALIZED:
                return DockyardTerminalCompositionResult(
                    review_id,
                    DockyardPostDeliveryCompositionOutcome.FINALIZED,
                    observed.phase,
                    True,
                )

        canonical_head = _git(self.root, "rev-parse", "HEAD")
        event_context = TransitionEventContext(
            None, (redispatch.content_digest.removeprefix("sha256:"),), ()
        )
        acceptance_event_id = _event_id("ACCEPT", redispatch.dispatch_id)
        integration_event_id = _event_id("INTEGRATE", redispatch.dispatch_id)
        acceptance = TransitionRequest(
            TransitionCAS(redispatch.task_id, redispatch.revision, "review_ready", canonical_head),
            DispatchCAS(redispatch.dispatch_id, redispatch.attempt),
            acceptance_event_id,
            "DELIVERY_ACCEPTED",
            DeliveryAcceptedPayload(
                delivery.implementation_commit,
                f"docs/pm/acceptances/{redispatch.task_id}-r{redispatch.revision}-a{redispatch.attempt}-review1.md",
                (),
                ("durable Worker delivery receipt", "MAD audit verdict"),
                "Dockyard accepts only after the configured MAD audit passes.",
            ),
            event_context,
        )
        return_transition, requeue_transition = _remediation_transitions(
            redispatch.task_id,
            redispatch.revision,
            redispatch.attempt,
            redispatch.dispatch_id,
            canonical_head,
            event_context,
        )
        audit_input = MadAuditGatewayInput(
            self.root,
            redispatch.task_id,
            redispatch.dispatch_id,
            f"审议 {redispatch.task_id} 的返修实现与交付报告是否满足任务卡。",
            workspace,
            plan.task_card_commit,
            plan.task_card_path,
            plan.report_path,
            delivery.report_commit,
            plan.base_commit,
            delivery.implementation_commit,
            MadDeliberationDepth.BALANCED,
        )
        reserved_at = _timestamp(self.clock)
        target_head = _git(self.root, "rev-parse", f"refs/heads/{self.target_branch}")
        git_request = with_git_request_digest(GitIntegrationRequest(
            GIT_SCHEMA_VERSION,
            _operation_id("GIT", redispatch.dispatch_id),
            self.root,
            redispatch.task_id,
            redispatch.revision,
            redispatch.attempt,
            redispatch.dispatch_id,
            workspace,
            plan.branch,
            plan.base_commit,
            delivery.implementation_commit,
            delivery.report_commit,
            self.target_branch,
            target_head,
            GitIntegrationMethod.MERGE_TREE,
            f"Integrate {redispatch.task_id} via Dockyard",
            reserved_at,
            "sha256:" + "0" * 64,
        ))
        assert isinstance(git_request, GitIntegrationRequest)
        review_request = with_request_digest(DockyardPostDeliveryReviewRequest(
            REVIEW_SCHEMA_VERSION,
            _operation_id("REVIEW", redispatch.dispatch_id),
            source_request.project_id,
            redispatch.task_id,
            redispatch.revision,
            redispatch.attempt,
            redispatch.dispatch_id,
            redispatch.generation_id,
            source_request.queue_id,
            source_request.schedule_receipt_id,
            source_request.admission_plan_digest,
            source_request.handoff_id,
            redispatch.content_digest,
            dispatch_event.event_id,
            digest_owner_value(dispatch_event),
            acknowledge_event.event_id,
            digest_owner_value(acknowledge_event),
            delivery_event.event_id,
            digest_owner_value(delivery_event),
            digest_owner_value(delivery),
            delivery.implementation_commit,
            delivery.report_commit,
            source_request.worktree_id,
            source_request.worktree_identity_digest,
            plan.branch,
            plan.base_commit,
            digest_owner_value(audit_input),
            _config_id(self.audit_config),
            acceptance_event_id,
            digest_owner_value(acceptance),
            integration_event_id,
            git_request.content_digest,
            return_transition.event_id,
            requeue_transition.event_id,
            None,
            digest_owner_value((return_transition, requeue_transition, None)),
            reserved_at,
            "sha256:" + "0" * 64,
        ))
        review_receipt = review_store.reserve(review_id, review_request)
        owner_plan = project_post_delivery_owner_plan(
            DockyardOwnerPlanProjectionRequest(
                self.root,
                review_id,
                delivery,
                workspace,
                plan.worker_kind,
                audit_input,
                acceptance,
                git_request,
                return_transition,
                requeue_transition,
                None,
                plan.holder_instance_id,
            )
        )
        acceptance_request = DurableAcceptanceCycleRequest(
            owner_plan.delivery_evidence,
            owner_plan.audit_input,
            owner_plan.acceptance_transition_request,
            None,
            owner_plan.worker_kind,
            owner_plan.holder_instance_id,
        )
        if review_receipt.phase is DockyardPostDeliveryReviewPhase.DELIVERY_BOUND:
            review_store.advance(
                review_id,
                DockyardPostDeliveryReviewPhase.REVIEW_RESERVED,
                DockyardPostDeliveryReviewOutcome.RESERVED,
                _timestamp(self.clock),
            )
            review_store.advance(
                review_id,
                DockyardPostDeliveryReviewPhase.MAD_STARTED,
                DockyardPostDeliveryReviewOutcome.RESERVED,
                _timestamp(self.clock),
            )
            audit_result = await WorkflowOrchestrator(
                self.root, self.clock
            ).run_durable_audit_cycle(acceptance_request, self.audit_config)
            DockyardMadResultStore(self.root).save(review_id, audit_result)
            outcomes = {
                "pass": DockyardPostDeliveryReviewOutcome.AUDIT_PASS,
                "fail": DockyardPostDeliveryReviewOutcome.AUDIT_FAIL,
                "blocked": DockyardPostDeliveryReviewOutcome.AUDIT_BLOCKED,
            }
            outcome = outcomes.get(audit_result.verdict)
            if outcome is None:
                raise DockyardTerminalCompositionConflictError("terminal:audit_verdict")
            review_receipt = review_store.advance(
                review_id,
                DockyardPostDeliveryReviewPhase.MAD_COMPLETED,
                outcome,
                _timestamp(self.clock),
                audit_result_id=audit_result.deliberation_id,
                audit_result_digest=digest_owner_value(audit_result),
                audit_verdict=audit_result.verdict,
            )
        return DockyardTerminalCompositionResult(
            review_id,
            DockyardPostDeliveryCompositionOutcome.RECOVERY_REQUIRED,
            review_receipt.phase,
            observed is not None,
        )

    def _resolve_review_context(
        self,
        receipt: DockyardPostDeliveryReviewReceipt,
        durable: DockyardPostDeliveryReviewRequest,
    ) -> _TerminalReviewContext:
        plan = PortfolioSchedulerStore(self.root).read_admission_plan(
            durable.queue_id, 1
        )
        if (
            type(plan) is not AdmissionPlanReservation
            or plan.task_id != receipt.task_id
            or plan.revision != receipt.revision
            or plan.receipt_id != durable.schedule_receipt_id
            or plan.content_digest != durable.admission_plan_digest
        ):
            raise DockyardTerminalCompositionConflictError("terminal:plan_binding")
        if plan.new_attempt == receipt.attempt and plan.dispatch_id == receipt.dispatch_id:
            common_raw = Path(_git(
                self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ))
            common = str(
                (common_raw if common_raw.is_absolute() else self.root / common_raw).resolve()
            )
            worktree_id = derive_worktree_id(
                common,
                str(self.root),
                plan.branch,
                plan.base_commit,
                plan.task_id,
                plan.revision,
                plan.new_attempt,
                plan.dispatch_id,
            )
        else:
            try:
                redispatch = DockyardRedispatchReceiptStore(self.root).read(
                    receipt.dispatch_id
                )
            except DockyardRedispatchStoreError as exc:
                raise DockyardTerminalCompositionConflictError(
                    "terminal:redispatch_receipt"
                ) from exc
            source_receipt = DockyardPostDeliveryReviewStore(self.root).read_receipt(
                redispatch.source_review_id
            )
            source_request = DockyardPostDeliveryReviewStore(self.root).read_request(
                redispatch.source_review_id
            )
            source_context = self._resolve_review_context(
                source_receipt, source_request
            )
            if (
                redispatch.content_digest != durable.external_worker_receipt_digest
                or redispatch.task_id != receipt.task_id
                or redispatch.revision != receipt.revision
                or redispatch.attempt != receipt.attempt
                or redispatch.dispatch_id != receipt.dispatch_id
                or redispatch.generation_id != receipt.dispatch_generation_id
                or source_receipt.phase is not DockyardPostDeliveryReviewPhase.FINALIZED
                or source_receipt.audit_verdict != "fail"
                or source_receipt.content_digest
                != redispatch.source_review_receipt_digest
                or source_receipt.attempt + 1 != receipt.attempt
                or source_context.plan != plan
                or source_context.task_id != source_receipt.task_id
                or source_context.revision != source_receipt.revision
                or source_context.attempt != source_receipt.attempt
                or source_context.dispatch_id != source_receipt.dispatch_id
                or source_request.queue_id != durable.queue_id
                or source_request.schedule_receipt_id
                != durable.schedule_receipt_id
                or source_request.admission_plan_digest
                != durable.admission_plan_digest
                or source_request.handoff_id != durable.handoff_id
                or source_request.worktree_id != durable.worktree_id
                or source_request.worktree_identity_digest
                != durable.worktree_identity_digest
            ):
                raise DockyardTerminalCompositionConflictError(
                    "terminal:redispatch_plan_binding"
                )
            worktree_id = durable.worktree_id
        if worktree_id != durable.worktree_id:
            raise DockyardTerminalCompositionConflictError("terminal:worktree_identity")
        record = WorktreeLifecycleStore(self.root).read_record(worktree_id)
        if (
            record is None
            or record.phase is not WorktreePhase.READY
            or record.worktree_path != plan.canonical_worktree
            or record.branch != plan.branch
            or record.base_commit != plan.base_commit
            or digest_worktree_identity(
                worktree_id,
                Path(record.worktree_path),
                plan.branch,
                plan.base_commit,
            ) != durable.worktree_identity_digest
        ):
            raise DockyardTerminalCompositionConflictError("terminal:worktree_binding")
        return _TerminalReviewContext(
            plan,
            receipt.task_id,
            receipt.revision,
            receipt.attempt,
            receipt.dispatch_id,
            worktree_id,
            Path(record.worktree_path),
        )

    def pending_review(self, task_id: str):
        """Return the one user-actionable review for a task, if present."""
        if type(task_id) is not str or not task_id:
            raise DockyardTerminalCompositionInputError("terminal:task_id")
        matches = [
            receipt
            for receipt in DockyardPostDeliveryReviewStore(self.root).enumerate_receipts()
            if receipt.task_id == task_id
            and receipt.phase is DockyardPostDeliveryReviewPhase.MAD_COMPLETED
        ]
        if len(matches) > 1:
            raise DockyardTerminalCompositionConflictError("terminal:multiple_reviews")
        return None if not matches else matches[0]

    def pending_reviews(self):
        return tuple(
            receipt
            for receipt in DockyardPostDeliveryReviewStore(self.root).enumerate_receipts()
            if receipt.phase is DockyardPostDeliveryReviewPhase.MAD_COMPLETED
        )

    def review_request(self, review_id: str) -> DockyardPostDeliveryReviewRequest:
        """Return the validated safe request projection for one review."""
        return DockyardPostDeliveryReviewStore(self.root).read_request(review_id)

    def accept(
        self,
        task_id: str,
        implementation_commit: str,
        report_commit: str,
        expected_snapshot_commit: str,
        command_id: str,
        confirmation_id: str,
    ) -> DockyardTerminalCompositionResult:
        """Apply one explicit user acceptance without rerunning Worker or MAD."""
        import asyncio

        return asyncio.run(self._accept(
            task_id,
            implementation_commit,
            report_commit,
            expected_snapshot_commit,
            command_id,
            confirmation_id,
        ))

    async def _accept(
        self,
        task_id: str,
        implementation_commit: str,
        report_commit: str,
        expected_snapshot_commit: str,
        command_id: str,
        confirmation_id: str,
    ) -> DockyardTerminalCompositionResult:
        for value, field in (
            (task_id, "task_id"),
            (command_id, "command_id"),
            (confirmation_id, "confirmation_id"),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise DockyardTerminalCompositionInputError(f"terminal:{field}")
        for value, field in (
            (implementation_commit, "implementation_commit"),
            (report_commit, "report_commit"),
            (expected_snapshot_commit, "expected_snapshot_commit"),
        ):
            if (
                type(value) is not str
                or len(value) != 40
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise DockyardTerminalCompositionInputError(f"terminal:{field}")
        current_head = _git(self.root, "rev-parse", "HEAD")
        if current_head != expected_snapshot_commit:
            raise DockyardTerminalCompositionConflictError("terminal:snapshot_stale")
        receipt = self.pending_review(task_id)
        if receipt is None or receipt.audit_verdict != "pass":
            raise DockyardTerminalCompositionConflictError("terminal:review_not_acceptable")
        review_store = DockyardPostDeliveryReviewStore(self.root)
        durable = review_store.read_request(receipt.review_id)
        delivery = DockyardDeliveryReceiptStore(self.root).read(receipt.dispatch_id)
        if (
            delivery.identity.task_id != task_id
            or delivery.implementation_commit != implementation_commit
            or delivery.report_commit != report_commit
            or durable.delivery_receipt_digest != digest_owner_value(delivery)
        ):
            raise DockyardTerminalCompositionConflictError("terminal:delivery_binding")
        context = self._resolve_review_context(receipt, durable)
        plan = context.plan
        workspace = context.workspace
        audit_objective = (
            f"审议 {context.task_id} 的返修实现与交付报告是否满足任务卡。"
            if context.dispatch_id != plan.dispatch_id
            else f"审议 {context.task_id} 的实现与交付报告是否满足任务卡。"
        )
        audit_input = MadAuditGatewayInput(
            self.root,
            context.task_id,
            context.dispatch_id,
            audit_objective,
            workspace,
            plan.task_card_commit,
            plan.task_card_path,
            plan.report_path,
            delivery.report_commit,
            plan.base_commit,
            delivery.implementation_commit,
            MadDeliberationDepth.BALANCED,
        )
        acceptance_event_id = durable.acceptance_event_id
        acceptance = TransitionRequest(
            TransitionCAS(context.task_id, context.revision, "review_ready", current_head),
            DispatchCAS(context.dispatch_id, context.attempt),
            acceptance_event_id,
            "DELIVERY_ACCEPTED",
            DeliveryAcceptedPayload(
                delivery.implementation_commit,
                f"docs/pm/acceptances/{context.task_id}-r{context.revision}-a{context.attempt}-review1.md",
                (),
                ("durable Worker delivery receipt", "MAD audit verdict"),
                "Dockyard accepts only after the configured MAD audit passes.",
            ),
            TransitionEventContext(
                None,
                (durable.external_worker_receipt_digest.removeprefix("sha256:"),),
                (),
            ),
        )
        return_transition, requeue_transition = _remediation_transitions(
            context.task_id,
            context.revision,
            context.attempt,
            context.dispatch_id,
            current_head,
            acceptance.event_context,
        )
        target_head = _git(self.root, "rev-parse", f"refs/heads/{self.target_branch}")
        git_request = with_git_request_digest(GitIntegrationRequest(
            GIT_SCHEMA_VERSION,
            _operation_id("GIT", context.dispatch_id),
            self.root,
            context.task_id,
            context.revision,
            context.attempt,
            context.dispatch_id,
            workspace,
            plan.branch,
            plan.base_commit,
            delivery.implementation_commit,
            delivery.report_commit,
            self.target_branch,
            target_head,
            GitIntegrationMethod.MERGE_TREE,
            f"Integrate {context.task_id} via Dockyard",
            durable.reserved_at,
            "sha256:" + "0" * 64,
        ))
        projection = DockyardOwnerPlanProjectionRequest(
            self.root,
            receipt.review_id,
            delivery,
            workspace,
            plan.worker_kind,
            audit_input,
            acceptance,
            git_request,
            return_transition,
            requeue_transition,
            None,
            plan.holder_instance_id,
        )
        owner_plan = project_post_delivery_owner_plan(projection)
        audit_result = DockyardMadResultStore(self.root).read(receipt.review_id)
        if digest_owner_value(audit_result) != receipt.audit_result_digest:
            raise DockyardTerminalCompositionConflictError("terminal:audit_result_binding")
        acceptance_request = DurableAcceptanceCycleRequest(
            owner_plan.delivery_evidence,
            owner_plan.audit_input,
            owner_plan.acceptance_transition_request,
            None,
            owner_plan.worker_kind,
            owner_plan.holder_instance_id,
        )

        subject = ApprovalSubject(
            context.task_id,
            context.revision,
            context.attempt,
            context.dispatch_id,
            None,
        )
        approval_check = ApprovalCheckRequest(
            ApprovalScope.ACCEPT, subject, current_head
        )
        try:
            ApprovalGate(self.root).require(approval_check, self.clock.now())
        except ApprovalNotFoundError:
            approval_token = hashlib.sha256(
                (command_id + "\0" + confirmation_id).encode("utf-8")
            ).hexdigest()[:24]
            write_grant(
                self.root,
                "APR-DOCKYARD-" + approval_token,
                "EVT-APPROVAL-" + approval_token,
                ApprovalScope.ACCEPT,
                subject,
                context.attempt,
                self.clock.now(),
                "Dockyard explicit delivery acceptance",
                None,
                current_head,
            )

        orchestrator = WorkflowOrchestrator(self.root, self.clock)
        acceptance_result = await orchestrator.resume_durable_acceptance_cycle(
            acceptance_request, audit_result
        )
        if acceptance_result.accept_transition is None:
            raise DockyardTerminalCompositionConflictError("terminal:acceptance_missing")
        receipt = review_store.advance(
            receipt.review_id,
            DockyardPostDeliveryReviewPhase.REVIEW_APPLIED,
            DockyardPostDeliveryReviewOutcome.ACCEPTED,
            _timestamp(self.clock),
            applied_event_id=acceptance_result.accept_transition.event_id,
        )
        git_receipt = GitIntegrationOwner(self.root).integrate(git_request)
        if (
            git_receipt.phase is not GitIntegrationPhase.FINALIZED
            or git_receipt.outcome is not GitIntegrationOutcome.FINALIZED
            or git_receipt.integrated_commit is None
            or git_receipt.integrated_tree is None
        ):
            raise DockyardTerminalCompositionRecoveryRequired("terminal:git_recovery")
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
        integrated_head = _git(self.root, "rev-parse", "HEAD")
        integration_subject = ApprovalSubject(
            context.task_id,
            context.revision,
            context.attempt,
            context.dispatch_id,
            delivery.implementation_commit,
        )
        integration_approval_token = hashlib.sha256(
            (command_id + "\0" + confirmation_id + "\0integrate").encode("utf-8")
        ).hexdigest()[:24]
        write_grant(
            self.root,
            "APR-DOCKYARD-" + integration_approval_token,
            "EVT-APPROVAL-" + integration_approval_token,
            ApprovalScope.INTEGRATE,
            integration_subject,
            context.attempt,
            self.clock.now(),
            "Dockyard accepted delivery integration",
            None,
            integrated_head,
        )
        integration_transition = TransitionRequest(
            TransitionCAS(context.task_id, context.revision, "accepted", integrated_head),
            None,
            durable.integration_event_id,
            "CHANGE_INTEGRATED",
            IntegrationPayload(
                git_receipt.integrated_commit,
                "tree",
                git_evidence.receipt_content_digest,
            ),
            TransitionEventContext(
                None,
                (git_evidence.receipt_content_digest,),
                (),
            ),
        )
        await orchestrator.run_durable_integration_completion(
            DurableIntegrationCompletionRequest(
                acceptance_result,
                owner_plan.delivery_evidence,
                git_evidence,
                integration_transition,
            )
        )
        receipt = review_store.advance(
            receipt.review_id,
            DockyardPostDeliveryReviewPhase.INTEGRATION_APPLIED,
            DockyardPostDeliveryReviewOutcome.INTEGRATED,
            _timestamp(self.clock),
            applied_event_id=durable.integration_event_id,
        )
        receipt = review_store.advance(
            receipt.review_id,
            DockyardPostDeliveryReviewPhase.FINALIZED,
            DockyardPostDeliveryReviewOutcome.FINALIZED,
            _timestamp(self.clock),
        )
        return DockyardTerminalCompositionResult(
            receipt.review_id,
            DockyardPostDeliveryCompositionOutcome.FINALIZED,
            receipt.phase,
            False,
        )

    def return_delivery(
        self,
        task_id: str,
        implementation_commit: str,
        report_commit: str,
        expected_snapshot_commit: str,
        command_id: str,
        confirmation_id: str,
    ) -> DockyardTerminalCompositionResult:
        """Apply one explicit fail-verdict return without starting a Worker."""
        import asyncio

        return asyncio.run(self._return_delivery(
            task_id,
            implementation_commit,
            report_commit,
            expected_snapshot_commit,
            command_id,
            confirmation_id,
        ))

    async def _return_delivery(
        self,
        task_id: str,
        implementation_commit: str,
        report_commit: str,
        expected_snapshot_commit: str,
        command_id: str,
        confirmation_id: str,
    ) -> DockyardTerminalCompositionResult:
        for value, field in (
            (task_id, "task_id"),
            (command_id, "command_id"),
            (confirmation_id, "confirmation_id"),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise DockyardTerminalCompositionInputError(f"terminal:{field}")
        for value, field in (
            (implementation_commit, "implementation_commit"),
            (report_commit, "report_commit"),
            (expected_snapshot_commit, "expected_snapshot_commit"),
        ):
            if (
                type(value) is not str
                or len(value) != 40
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise DockyardTerminalCompositionInputError(f"terminal:{field}")
        current_head = _git(self.root, "rev-parse", "HEAD")
        if current_head != expected_snapshot_commit:
            raise DockyardTerminalCompositionConflictError("terminal:snapshot_stale")
        receipt = self.pending_review(task_id)
        if receipt is None or receipt.audit_verdict != "fail":
            raise DockyardTerminalCompositionConflictError("terminal:review_not_returnable")

        review_store = DockyardPostDeliveryReviewStore(self.root)
        durable = review_store.read_request(receipt.review_id)
        delivery = DockyardDeliveryReceiptStore(self.root).read(receipt.dispatch_id)
        if (
            delivery.identity.task_id != task_id
            or delivery.identity.revision != receipt.revision
            or delivery.identity.attempt != receipt.attempt
            or delivery.identity.dispatch_id != receipt.dispatch_id
            or delivery.implementation_commit != implementation_commit
            or delivery.report_commit != report_commit
            or durable.delivery_receipt_digest != digest_owner_value(delivery)
        ):
            raise DockyardTerminalCompositionConflictError("terminal:delivery_binding")
        context = self._resolve_review_context(receipt, durable)
        plan = context.plan
        workspace = context.workspace
        audit_objective = (
            f"审议 {context.task_id} 的返修实现与交付报告是否满足任务卡。"
            if context.dispatch_id != plan.dispatch_id
            else f"审议 {context.task_id} 的实现与交付报告是否满足任务卡。"
        )
        audit_input = MadAuditGatewayInput(
            self.root,
            context.task_id,
            context.dispatch_id,
            audit_objective,
            workspace,
            plan.task_card_commit,
            plan.task_card_path,
            plan.report_path,
            delivery.report_commit,
            plan.base_commit,
            delivery.implementation_commit,
            MadDeliberationDepth.BALANCED,
        )
        event_context = TransitionEventContext(
            None,
            (durable.external_worker_receipt_digest.removeprefix("sha256:"),),
            (),
        )
        acceptance = TransitionRequest(
            TransitionCAS(context.task_id, context.revision, "review_ready", current_head),
            DispatchCAS(context.dispatch_id, context.attempt),
            durable.acceptance_event_id,
            "DELIVERY_ACCEPTED",
            DeliveryAcceptedPayload(
                delivery.implementation_commit,
                f"docs/pm/acceptances/{context.task_id}-r{context.revision}-a{context.attempt}-review1.md",
                (),
                ("durable Worker delivery receipt", "MAD audit verdict"),
                "Dockyard accepts only after the configured MAD audit passes.",
            ),
            event_context,
        )
        target_head = _git(self.root, "rev-parse", f"refs/heads/{self.target_branch}")
        git_request = with_git_request_digest(GitIntegrationRequest(
            GIT_SCHEMA_VERSION,
            _operation_id("GIT", context.dispatch_id),
            self.root,
            context.task_id,
            context.revision,
            context.attempt,
            context.dispatch_id,
            workspace,
            plan.branch,
            plan.base_commit,
            delivery.implementation_commit,
            delivery.report_commit,
            self.target_branch,
            target_head,
            GitIntegrationMethod.MERGE_TREE,
            f"Integrate {context.task_id} via Dockyard",
            durable.reserved_at,
            "sha256:" + "0" * 64,
        ))
        return_transition, requeue_transition = _remediation_transitions(
            context.task_id,
            context.revision,
            context.attempt,
            context.dispatch_id,
            current_head,
            event_context,
        )
        owner_plan = project_post_delivery_owner_plan(
            DockyardOwnerPlanProjectionRequest(
                self.root,
                receipt.review_id,
                delivery,
                workspace,
                plan.worker_kind,
                audit_input,
                acceptance,
                git_request,
                return_transition,
                requeue_transition,
                None,
                plan.holder_instance_id,
            )
        )
        if (
            owner_plan.return_transition_request is None
            or owner_plan.requeue_transition_request is None
        ):
            raise DockyardTerminalCompositionConflictError("terminal:remediation_missing")
        audit_result = DockyardMadResultStore(self.root).read(receipt.review_id)
        if (
            audit_result.verdict != "fail"
            or digest_owner_value(audit_result) != receipt.audit_result_digest
        ):
            raise DockyardTerminalCompositionConflictError("terminal:audit_result_binding")
        remediation = await WorkflowOrchestrator(
            self.root, self.clock
        ).run_durable_delivery_remediation(DurableDeliveryRemediationRequest(
            AcceptanceCycleResult(task_id, audit_result, None, None),
            owner_plan.delivery_evidence,
            owner_plan.return_transition_request,
            owner_plan.requeue_transition_request,
            owner_plan.worker_kind,
            owner_plan.holder_instance_id,
        ))
        if (
            remediation.return_transition.event_id != durable.return_event_id
            or remediation.requeue_transition.event_id != durable.requeue_event_id
        ):
            raise DockyardTerminalCompositionConflictError("terminal:remediation_identity")
        receipt = review_store.advance(
            receipt.review_id,
            DockyardPostDeliveryReviewPhase.REVIEW_APPLIED,
            DockyardPostDeliveryReviewOutcome.RETURNED,
            _timestamp(self.clock),
            applied_event_id=remediation.return_transition.event_id,
        )
        receipt = review_store.advance(
            receipt.review_id,
            DockyardPostDeliveryReviewPhase.FINALIZED,
            DockyardPostDeliveryReviewOutcome.FINALIZED,
            _timestamp(self.clock),
        )
        return DockyardTerminalCompositionResult(
            receipt.review_id,
            DockyardPostDeliveryCompositionOutcome.FINALIZED,
            receipt.phase,
            False,
        )


__all__ = [
    "DockyardTerminalCompositionError",
    "DockyardTerminalCompositionInputError",
    "DockyardTerminalCompositionConflictError",
    "DockyardTerminalCompositionRecoveryRequired",
    "DockyardTerminalCompositionResult",
    "DockyardTerminalOwnerCompositionRuntime",
]
