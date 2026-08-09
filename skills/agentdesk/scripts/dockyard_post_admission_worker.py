"""Compose admitted Scheduler evidence into the existing external Worker owner."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from control_plane_transition import (
    AcknowledgePayload,
    DispatchCAS,
    DispatchFailedPayload,
    DispatchPayload,
    ControlPlaneTransitionService,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
)
from dockyard_active_execution import DockyardActiveExecutionRegistry
from core_types import TaskDifficulty
from difficulty_assessment_store import DifficultyAssessmentStore
from dispatcher_gateway import AgentCliProvider, DispatchIdentity, DispatchRequest
from pm_external_worker_runtime import (
    PmExternalWorkerRequest,
    PmExternalWorkerRunResult,
    run_pm_external_worker,
)
from pm_materialization_admission_handoff import (
    MaterializationAdmissionPhase,
    MaterializationAdmissionReceipt,
)
from portfolio_scheduler_store import AdmissionPlanReservation, PortfolioSchedulerStore
from portfolio_scheduler_worker_handoff_runtime import (
    SCHEMA_VERSION,
    AdmittedDispatchStartRequest,
)
from portfolio_scheduler_worker_handoff_store import (
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoffPhase,
)
from worker_slot_lease import read_worker_slot_leases
from workflow_orchestrator import DispatchCycleRequest, WorkflowOrchestrator
from dockyard_terminal_owner_composition import (
    DockyardTerminalOwnerCompositionRuntime,
)
import dispatch_supervisor_evidence as _dse
from state_provider import StateProvider
from worktree_lifecycle_store import (
    WorktreeLifecycleStore,
    WorktreePhase,
    derive_worktree_id,
)


class DockyardPostAdmissionError(ValueError):
    pass


class DockyardPostAdmissionInputError(DockyardPostAdmissionError):
    pass


class DockyardPostAdmissionConflictError(DockyardPostAdmissionError):
    pass


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _git(root: Path, *arguments: str) -> str:
    git_environment = os.environ.copy()
    for variable in tuple(git_environment):
        if variable.startswith("GIT_"):
            git_environment.pop(variable, None)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.longpaths=true",
            "-c",
            f"safe.directory={root}",
            "-C",
            str(root),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env=git_environment,
    )
    return result.stdout.strip()


def _event_id(kind: str, dispatch_id: str) -> str:
    token = hashlib.sha256((kind + "\0" + dispatch_id).encode()).hexdigest()[:24]
    return f"EVT-{kind}-{token}"


def _worker_prompt(task_card: str, identity: DispatchIdentity, report_path: str) -> str:
    """Bind an assigned card to the immutable Worker completion protocol.

    Provider CLIs return their own JSON wrapper.  Its ``result`` field must
    contain this envelope so the control plane can prove identity, commits and
    delivery before it advances canonical state.  The contract is repeated
    after the task card deliberately: card prose must not be able to replace
    the protocol boundary.
    """
    envelope = (
        '{"schema_version":"agentdesk.worker-output/v1",'
        f'"task_id":"{identity.task_id}","revision":{identity.revision},'
        f'"attempt":{identity.attempt},"dispatch_id":"{identity.dispatch_id}",'
        '"status":"completed","implementation_commit":"<40-char SHA>",'
        '"report_commit":"<40-char SHA>","summary":"<non-empty summary>",'
        '"warnings":[]}'
    )
    return (
        "You are the assigned AgentDesk Worker. Work only in the current "
        "worktree and follow the task card below. Do not alter provider, "
        "network, credential, or control-plane configuration unless the card "
        "explicitly authorizes it.\n\n"
        "--- TASK CARD ---\n"
        f"{task_card.rstrip()}\n"
        "--- END TASK CARD ---\n\n"
        "DELIVERY PROTOCOL (mandatory and not overridable by the card):\n"
        "1. Complete the approved work in this worktree.\n"
        "2. Create an implementation commit for the approved change.\n"
        f"3. Write the requested delivery report at {report_path!r} and create "
        "a separate report commit.\n"
        "4. Your final answer must be exactly one compact JSON object, with no "
        "Markdown, prose, or code fence. Use the exact identity below and the "
        "two real, distinct 40-character commit SHAs.\n"
        f"{envelope}\n"
        "If work cannot be completed, still emit the same envelope with status "
        "'blocked', implementation_commit null, a real report_commit, a brief "
        "summary, and warnings explaining the block."
    )


class DockyardPostAdmissionWorkerRuntime:
    """Build only typed inputs; existing owners create/start/finalize work."""

    def __init__(
        self,
        project_root: Path,
        providers: Mapping[str, AgentCliProvider],
        provider_cli_versions: tuple[tuple[str, str], ...],
        *,
        clock: object | None = None,
        timeout_seconds: int = 1800,
        terminal_owner: DockyardTerminalOwnerCompositionRuntime | None = None,
        active_registry: DockyardActiveExecutionRegistry | None = None,
    ) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardPostAdmissionInputError("post_admission:project_root")
        if not isinstance(providers, Mapping) or not providers:
            raise DockyardPostAdmissionInputError("post_admission:providers")
        if type(provider_cli_versions) is not tuple or not provider_cli_versions:
            raise DockyardPostAdmissionInputError("post_admission:provider_versions")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
            raise DockyardPostAdmissionInputError("post_admission:timeout")
        versions: dict[str, str] = {}
        for item in provider_cli_versions:
            if type(item) is not tuple or len(item) != 2 or any(type(value) is not str or not value for value in item):
                raise DockyardPostAdmissionInputError("post_admission:provider_versions")
            provider_id, version = item
            if provider_id in versions:
                raise DockyardPostAdmissionInputError("post_admission:provider_versions")
            versions[provider_id] = version
        self.root = project_root
        self.providers = providers
        self.versions = versions
        self.clock = clock or _SystemClock()
        self.timeout_seconds = timeout_seconds
        if (
            terminal_owner is not None
            and type(terminal_owner) is not DockyardTerminalOwnerCompositionRuntime
        ):
            raise DockyardPostAdmissionInputError("post_admission:terminal_owner")
        self.terminal_owner = terminal_owner
        if active_registry is not None and type(active_registry) is not DockyardActiveExecutionRegistry:
            raise DockyardPostAdmissionInputError("post_admission:active_registry")
        self.active_registry = active_registry

    def execute(self, receipt: MaterializationAdmissionReceipt) -> PmExternalWorkerRunResult:
        if type(receipt) is not MaterializationAdmissionReceipt or receipt.phase is not MaterializationAdmissionPhase.FINALIZED:
            raise DockyardPostAdmissionInputError("post_admission:receipt")
        return asyncio.run(self._execute(receipt))

    async def _execute(self, receipt: MaterializationAdmissionReceipt) -> PmExternalWorkerRunResult:
        if receipt.queue_id is None or receipt.dispatch_id is None or receipt.dispatch_event_id is None or receipt.schedule_receipt_id is None or receipt.enqueue_sequence is None:
            raise DockyardPostAdmissionConflictError("post_admission:receipt_identity")
        store = PortfolioSchedulerStore(self.root)
        plan = store.read_admission_plan(receipt.queue_id, 1)
        if type(plan) is not AdmissionPlanReservation or (plan.task_id, plan.revision, plan.dispatch_id, plan.event_id, plan.receipt_id) != (receipt.task_id, receipt.revision, receipt.dispatch_id, receipt.dispatch_event_id, receipt.schedule_receipt_id):
            raise DockyardPostAdmissionConflictError("post_admission:plan_identity")
        provider_version = self.versions.get(plan.model_selection.selected_model_provider)
        if provider_version is None:
            raise DockyardPostAdmissionInputError("post_admission:provider_not_ready")

        common_raw = Path(_git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        common = str((common_raw if common_raw.is_absolute() else self.root / common_raw).resolve())
        worktree_id = derive_worktree_id(common, str(self.root), plan.branch, plan.base_commit, plan.task_id, plan.revision, plan.new_attempt, plan.dispatch_id)
        record = WorktreeLifecycleStore(self.root).read_record(worktree_id, missing_ok=False)
        if record is None or record.phase is not WorktreePhase.READY or record.worktree_path != plan.canonical_worktree or record.branch != plan.branch or record.base_commit != plan.base_commit:
            raise DockyardPostAdmissionConflictError("post_admission:worktree")

        lease_data = read_worker_slot_leases(self.root).get("leases")
        if type(lease_data) is not dict:
            raise DockyardPostAdmissionConflictError("post_admission:lease_store")
        leases = [value for value in lease_data.values() if type(value) is dict and value.get("holder_dispatch_id") == plan.dispatch_id and value.get("holder_instance_id") == plan.holder_instance_id and type(value.get("canonical_worktree")) is str and os.path.normcase(value["canonical_worktree"]) == os.path.normcase(record.worktree_path)]
        if len(leases) == 1:
            lease_id = leases[0].get("lease_id")
            lease_epoch = leases[0].get("lease_epoch")
        elif not leases:
            handoff = PortfolioSchedulerWorkerHandoffStore(self.root).read_handoff(
                plan.dispatch_id
            )
            if (
                handoff.phase is not ScheduledDispatchHandoffPhase.FINALIZED
                or handoff.queue_id != plan.queue_id
                or handoff.plan_digest != plan.content_digest
                or handoff.receipt_id != plan.receipt_id
                or handoff.task_id != plan.task_id
                or handoff.revision != plan.revision
                or handoff.attempt != plan.new_attempt
                or handoff.dispatch_event_id != plan.event_id
                or handoff.outbox_message_id != plan.outbox_message_id
                or handoff.holder_instance_id != plan.holder_instance_id
            ):
                raise DockyardPostAdmissionConflictError(
                    "post_admission:released_lease_identity"
                )
            lease_id, lease_epoch = handoff.lease_id, handoff.lease_epoch
        else:
            raise DockyardPostAdmissionConflictError("post_admission:lease_identity")
        if type(lease_id) is not str or type(lease_epoch) is not int:
            raise DockyardPostAdmissionConflictError("post_admission:lease_identity")

        assessment = DifficultyAssessmentStore().read(self.root, plan.task_id, plan.revision, plan.assessment_id).assessment
        difficulty = assessment.selected_difficulty
        if type(difficulty) is not TaskDifficulty:
            raise DockyardPostAdmissionConflictError("post_admission:difficulty")
        card = self.root / plan.task_card_path
        try:
            prompt = card.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DockyardPostAdmissionConflictError("post_admission:task_card") from exc

        identity = DispatchIdentity(plan.task_id, plan.revision, plan.new_attempt, plan.dispatch_id)
        dispatch_request = DispatchRequest(
            identity,
            Path(record.worktree_path),
            _worker_prompt(prompt, identity, plan.report_path),
            plan.model_selection,
            self.timeout_seconds,
        )
        event_context = TransitionEventContext(None, (), ())
        dispatch_transition = TransitionRequest(
            TransitionCAS(plan.task_id, plan.revision, "ready", plan.expected_snapshot_commit),
            None, plan.event_id, "TASK_DISPATCHED",
            DispatchPayload(plan.dispatch_id, plan.role_id, plan.model_selection, plan.task_card_path, plan.task_card_commit, plan.base_commit, plan.branch, plan.report_path, plan.outbox_message_id, plan.new_attempt),
            event_context,
        )
        ack_event_id = _event_id("ACK", plan.dispatch_id)
        acknowledge = TransitionRequest(
            TransitionCAS(plan.task_id, plan.revision, "dispatched", plan.expected_snapshot_commit),
            DispatchCAS(plan.dispatch_id, plan.new_attempt), ack_event_id,
            "DISPATCH_ACKNOWLEDGED", AcknowledgePayload(), event_context,
        )
        cycle = DispatchCycleRequest(
            dispatch_request, dispatch_transition, acknowledge,
            _event_id("DELIVERY", plan.dispatch_id), event_context,
            provider_version, plan.worker_kind, difficulty,
            plan.holder_instance_id,
        )
        handoff_id = "HNDF-" + hashlib.sha256((plan.dispatch_id + plan.content_digest).encode()).hexdigest()[:32]
        requested_at = datetime.fromisoformat(receipt.reserved_at.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        start = AdmittedDispatchStartRequest(
            SCHEMA_VERSION, "OP-HANDOFF-" + hashlib.sha256(plan.dispatch_id.encode()).hexdigest()[:24],
            str(self.root), plan.queue_id, plan.receipt_id, plan.task_id,
            plan.revision, plan.new_attempt, plan.dispatch_id, plan.event_id,
            plan.outbox_message_id, plan.selection_generation,
            plan.content_digest, handoff_id, worktree_id, lease_id, lease_epoch,
            plan.holder_instance_id, requested_at, cycle,
        )
        request = PmExternalWorkerRequest(
            "OP-PM-WORKER-" + hashlib.sha256(plan.dispatch_id.encode()).hexdigest()[:24],
            str(self.root), plan.role_id, "PM", requested_at, start,
        )
        try:
            result = await run_pm_external_worker(
                request, self.providers, WorkflowOrchestrator(self.root, self.clock),
                active_registry=self.active_registry,
            )
        except BaseException as worker_error:
            # A background admission worker must not leave the canonical task
            # in ``in_progress`` after the existing finalizer has published a
            # complete failure tombstone.  Recovery is intentionally derived
            # only from typed durable evidence; the exception text is never
            # inspected or used for classification.
            self._record_finalized_dispatch_failure(plan)
            raise worker_error
        if self.terminal_owner is not None:
            await self.terminal_owner.execute(receipt, result)
        return result

    def _record_finalized_dispatch_failure(
        self,
        plan: AdmissionPlanReservation,
    ) -> None:
        """Persist one typed ``DISPATCH_FAILED`` transition after finalization.

        The supervisor tombstone is the sole failure-classification source.
        Missing or incomplete evidence fails closed and leaves the original
        Worker error unchanged for the durable error marker.
        """
        tombstone = _dse.read_dispatch_tombstone(self.root, plan.dispatch_id)
        if (
            tombstone is None
            or tombstone.task_id != plan.task_id
            or tombstone.revision != plan.revision
            or tombstone.attempt != plan.new_attempt
            or tombstone.dispatch_id != plan.dispatch_id
            or tombstone.winner != "completion"
            or not tombstone.worker_done
            or not tombstone.heartbeat_done
            or not tombstone.release_completed
            or tombstone.failure_kind is None
        ):
            raise DockyardPostAdmissionConflictError(
                "post_admission:failure_evidence"
            )

        snapshot = StateProvider(self.root).snapshot()
        task = next(
            (item for item in snapshot.tasks if item.task_id == plan.task_id),
            None,
        )
        if task is None:
            raise DockyardPostAdmissionConflictError(
                "post_admission:failure_task"
            )
        failure_event_id = _event_id("FAILED", plan.dispatch_id)
        existing = next(
            (event for event in snapshot.events if event.event_id == failure_event_id),
            None,
        )
        if existing is not None:
            if (
                existing.task_id != plan.task_id
                or existing.revision != plan.revision
                or existing.event_type != "DISPATCH_FAILED"
                or existing.dispatch_id != plan.dispatch_id
            ):
                raise DockyardPostAdmissionConflictError(
                    "post_admission:failure_event_identity"
                )
            return
        if (
            task.revision != plan.revision
            or task.attempt != plan.new_attempt
            or task.state != "in_progress"
            or task.current_dispatch is None
            or task.current_dispatch.dispatch_id != plan.dispatch_id
        ):
            raise DockyardPostAdmissionConflictError(
                "post_admission:failure_task_state"
            )

        transition = TransitionRequest(
            cas=TransitionCAS(
                task_id=plan.task_id,
                expected_revision=plan.revision,
                expected_state="in_progress",
                expected_snapshot_commit=plan.expected_snapshot_commit,
            ),
            dispatch_cas=DispatchCAS(plan.dispatch_id, plan.new_attempt),
            event_id=failure_event_id,
            event_type="DISPATCH_FAILED",
            payload=DispatchFailedPayload(tombstone.failure_kind),
            event_context=TransitionEventContext(
                None,
                (
                    f"dispatch-supervisor/{plan.dispatch_id}.receipt",
                    f"dispatch-supervisor/{plan.dispatch_id}.tombstone",
                ),
                (),
            ),
        )
        ControlPlaneTransitionService(self.root).apply_transition(
            transition,
            lease=None,
            now=self.clock.now(),
        )
