"""Adopt an already admitted scheduler dispatch and drive durable handoff."""

from __future__ import annotations

import hashlib
import asyncio
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import dispatch_supervisor_evidence as dse
from control_plane_transition import (
    CancelledPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from dispatcher_gateway import AgentCliProvider, resolve_invocation
from portfolio_scheduler_store import PortfolioSchedulerStore, QueuePhase, ReceiptPhase
from portfolio_scheduler_worker_handoff_store import (
    HANDOFF_SCHEMA_VERSION,
    PortfolioSchedulerWorkerHandoffNotFoundError,
    PortfolioSchedulerWorkerHandoffStore,
    ScheduledDispatchHandoff,
    ScheduledDispatchHandoffPhase,
)
from state_provider import StateProvider
from worker_slot_lease import WorkerSlotLease, read_worker_slot_leases
from workflow_orchestrator import (
    ActiveDispatchCancellationRequest,
    ActiveDispatchCancellationResult,
    DispatchCycleRequest,
    DispatchCycleResult,
    WorkflowOrchestrator,
)
from dockyard_active_execution import (
    DockyardActiveCancellationCommand,
    DockyardActiveCancellationOutcome,
    DockyardActiveExecutionRegistry,
    make_active_execution_identity,
)
from worktree_lifecycle_store import WorktreeLifecycleStore, WorktreePhase

SCHEMA_VERSION = "agentdesk.scheduler-worker-handoff-runtime/v1"


class AdmittedDispatchRuntimeError(ValueError):
    pass


class AdmittedDispatchInputError(AdmittedDispatchRuntimeError):
    pass


class AdmittedDispatchConflictError(AdmittedDispatchRuntimeError):
    pass


class AdmittedDispatchStartOutcome(str, Enum):
    STARTED = "STARTED"
    REPLAYED = "REPLAYED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REFUSED = "REFUSED"
    FAIL_CLOSED = "FAIL_CLOSED"


def _fail(error: type[AdmittedDispatchRuntimeError], code: str) -> None:
    raise error("scheduler_handoff_runtime:" + code)


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value or value != value.strip() or any(c in value for c in "\0\r\n"):
        _fail(AdmittedDispatchInputError, code)
    return value


def _positive(value: object, code: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        _fail(AdmittedDispatchInputError, code)
    return value


@dataclass(frozen=True, slots=True)
class AdmittedDispatchStartRequest:
    schema_version: str
    operation_id: str
    project_root: str
    queue_id: str
    receipt_id: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    dispatch_event_id: str
    outbox_message_id: str
    selection_generation: int
    plan_digest: str
    handoff_id: str
    worktree_id: str
    lease_id: str
    lease_epoch: int
    holder_instance_id: str
    requested_at: str
    dispatch_cycle_request: DispatchCycleRequest

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(AdmittedDispatchInputError, "schema")
        for name in ("operation_id", "project_root", "queue_id", "receipt_id", "task_id", "dispatch_id", "dispatch_event_id", "outbox_message_id", "plan_digest", "handoff_id", "worktree_id", "lease_id", "holder_instance_id", "requested_at"):
            _text(getattr(self, name), name)
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt", 3)
        _positive(self.selection_generation, "selection_generation")
        _positive(self.lease_epoch, "lease_epoch")
        if type(self.dispatch_cycle_request) is not DispatchCycleRequest:
            _fail(AdmittedDispatchInputError, "dispatch_cycle_request")


@dataclass(frozen=True, slots=True)
class AdmittedDispatchStartResult:
    schema_version: str
    operation_id: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    handoff_id: str
    generation_id: str
    phase: ScheduledDispatchHandoffPhase
    outcome: AdmittedDispatchStartOutcome
    process_receipt_phase: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class AdmittedDispatchCompletionResult:
    """Handoff result plus fresh delivery evidence when this call starts it."""

    start_result: AdmittedDispatchStartResult
    dispatch_cycle_result: DispatchCycleResult | None


def _result(request: AdmittedDispatchStartRequest, generation: str, phase: ScheduledDispatchHandoffPhase, outcome: AdmittedDispatchStartOutcome, receipt_phase: str) -> AdmittedDispatchStartResult:
    body = "\0".join((request.operation_id, request.dispatch_id, request.handoff_id, generation, phase.value, outcome.value, receipt_phase))
    return AdmittedDispatchStartResult(SCHEMA_VERSION, request.operation_id, request.task_id, request.revision, request.attempt, request.dispatch_id, request.handoff_id, generation, phase, outcome, receipt_phase, "sha256:" + hashlib.sha256(body.encode()).hexdigest())


def _lease(project_root: Path, request: AdmittedDispatchStartRequest) -> WorkerSlotLease:
    try:
        leases = read_worker_slot_leases(project_root)["leases"]
        if type(leases) is not dict:
            raise TypeError
        matches = [raw for raw in leases.values() if type(raw) is dict and raw.get("lease_id") == request.lease_id]
        if len(matches) != 1:
            raise ValueError
        raw = dict(matches[0])
        from core_types import WorkerKind
        raw["worker_kind"] = WorkerKind(raw["worker_kind"])
        lease = WorkerSlotLease(**raw)
    except Exception:
        _fail(AdmittedDispatchConflictError, "lease_evidence")
    if lease.lease_epoch != request.lease_epoch or lease.holder_dispatch_id != request.dispatch_id or lease.holder_instance_id != request.holder_instance_id:
        _fail(AdmittedDispatchConflictError, "lease_identity")
    return lease


def _same_worktree(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _handoff_timestamp(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _verify_git_worktree(path: Path, expected_head: str, expected_branch: str) -> None:
    if not path.is_absolute() or not path.is_dir():
        _fail(AdmittedDispatchConflictError, "git_worktree_path")
    try:
        def run(*arguments: str) -> bytes:
            completed = subprocess.run(
                ["git", "-c", "core.longpaths=true", *arguments],
                cwd=path, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                check=False, timeout=15,
            )
            if completed.returncode != 0:
                _fail(AdmittedDispatchConflictError, "git_worktree_read")
            return bytes(completed.stdout)
        head = run("rev-parse", "HEAD").decode("ascii").strip()
        branch = run("branch", "--show-current").decode("utf-8").strip()
        clean = run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    except AdmittedDispatchRuntimeError:
        raise
    except Exception:
        _fail(AdmittedDispatchConflictError, "git_worktree_unknown")
    if head != expected_head or branch != expected_branch or clean != b"":
        _fail(AdmittedDispatchConflictError, "git_worktree_identity")


async def _start_admitted_dispatch(
    request: AdmittedDispatchStartRequest,
    providers: Mapping[str, AgentCliProvider],
    orchestrator: WorkflowOrchestrator,
    completion: list[DispatchCycleResult] | None,
    active_registry: DockyardActiveExecutionRegistry | None = None,
) -> AdmittedDispatchStartResult:
    if type(request) is not AdmittedDispatchStartRequest:
        _fail(AdmittedDispatchInputError, "request")
    if not isinstance(providers, Mapping) or not providers:
        _fail(AdmittedDispatchInputError, "providers")
    if type(orchestrator) is not WorkflowOrchestrator:
        _fail(AdmittedDispatchInputError, "orchestrator")
    if active_registry is not None and type(active_registry) is not DockyardActiveExecutionRegistry:
        _fail(AdmittedDispatchInputError, "active_registry")
    root = Path(request.project_root)
    if not root.is_absolute() or root != orchestrator.project_root:
        _fail(AdmittedDispatchInputError, "project_root")
    cycle = request.dispatch_cycle_request
    dr = cycle.dispatch_request
    if (dr.identity.task_id, dr.identity.revision, dr.identity.attempt, dr.identity.dispatch_id, cycle.holder_instance_id) != (request.task_id, request.revision, request.attempt, request.dispatch_id, request.holder_instance_id):
        _fail(AdmittedDispatchConflictError, "cycle_identity")

    # Provider resolution is pure validation and must precede every write.
    resolve_invocation(dr, providers)
    store = PortfolioSchedulerStore(root)
    plan = store.read_admission_plan(request.queue_id, request.selection_generation)
    entry, receipt = store.read_queue_entry(request.queue_id), store.read_schedule_receipt(request.receipt_id)
    if entry.state is not QueuePhase.DISPATCHED or receipt.phase is not ReceiptPhase.DISPATCHED:
        _fail(AdmittedDispatchConflictError, "admission_phase")
    if (plan.receipt_id, plan.task_id, plan.revision, plan.dispatch_id, plan.event_id, plan.outbox_message_id, plan.selection_generation, plan.content_digest) != (request.receipt_id, request.task_id, request.revision, request.dispatch_id, request.dispatch_event_id, request.outbox_message_id, request.selection_generation, request.plan_digest):
        _fail(AdmittedDispatchConflictError, "plan_identity")
    lease = _lease(root, request)
    if not _same_worktree(lease.canonical_worktree, str(dr.workspace)):
        _fail(AdmittedDispatchConflictError, "worktree_lease")
    record = WorktreeLifecycleStore(root).read_record(request.worktree_id, missing_ok=False)
    if (
        record is None
        or record.phase is not WorktreePhase.READY
        or record.worktree_path != str(dr.workspace)
        or record.dispatch_id != request.dispatch_id
        or record.task_id != request.task_id
        or record.revision != request.revision
        or record.attempt != request.attempt
        or record.base_commit != plan.base_commit
        or record.branch != plan.branch
        or record.head_commit != plan.base_commit
    ):
        _fail(AdmittedDispatchConflictError, "worktree_record")
    _verify_git_worktree(Path(record.worktree_path), record.head_commit, record.branch)
    snapshot = StateProvider(root).snapshot()
    event = next((e for e in snapshot.events if e.event_id == request.dispatch_event_id), None)
    outbox = next((o for o in snapshot.outbox if o.message_id == request.outbox_message_id), None)
    task = next((t for t in snapshot.tasks if t.task_id == request.task_id), None)
    if event is None or event.event_type != "TASK_DISPATCHED" or event.dispatch_id != request.dispatch_id or outbox is None or outbox.event_id != event.event_id or task is None or task.current_dispatch is None or task.current_dispatch.dispatch_id != request.dispatch_id:
        _fail(AdmittedDispatchConflictError, "canonical_dispatch")

    existing = dse.read_dispatch_receipt(root, request.dispatch_id)
    handoff_store = PortfolioSchedulerWorkerHandoffStore(root)
    existing_handoff = None
    try:
        existing_handoff = handoff_store.read_handoff(request.dispatch_id)
    except PortfolioSchedulerWorkerHandoffNotFoundError:
        pass
    if existing is not None and existing.phase != dse.DispatchReceiptPhase.RESERVED.value:
        if existing.generation_id == "":
            _fail(AdmittedDispatchConflictError, "process_generation")
        if existing_handoff is not None:
            if (
                existing_handoff.dispatch_id != request.dispatch_id
                or existing_handoff.handoff_id != request.handoff_id
                or existing_handoff.plan_digest != request.plan_digest
            ):
                _fail(AdmittedDispatchConflictError, "handoff_replay_identity")
            if existing_handoff.phase is ScheduledDispatchHandoffPhase.FINALIZED:
                tombstone = dse.read_dispatch_tombstone(root, request.dispatch_id)
                if tombstone is None or tombstone.generation_id != existing.generation_id:
                    _fail(AdmittedDispatchConflictError, "finalized_tombstone")
                return _result(
                    request, existing.generation_id, existing_handoff.phase,
                    AdmittedDispatchStartOutcome.REPLAYED, existing.phase,
                )
            tombstone = dse.read_dispatch_tombstone(root, request.dispatch_id)
            if tombstone is not None:
                if tombstone.generation_id != existing.generation_id:
                    _fail(AdmittedDispatchConflictError, "tombstone_generation")
                resumed = existing_handoff
                if resumed.phase is ScheduledDispatchHandoffPhase.ACKNOWLEDGED:
                    resumed, _ = handoff_store.advance_phase(
                        request.dispatch_id,
                        ScheduledDispatchHandoffPhase.FINALIZING,
                        _handoff_timestamp(existing.written_at),
                        existing,
                    )
                if resumed.phase is ScheduledDispatchHandoffPhase.FINALIZING:
                    resumed, _ = handoff_store.advance_phase(
                        request.dispatch_id,
                        ScheduledDispatchHandoffPhase.FINALIZED,
                        _handoff_timestamp(tombstone.finalized_at),
                        existing,
                    )
                    return _result(
                        request, existing.generation_id, resumed.phase,
                        AdmittedDispatchStartOutcome.REPLAYED, existing.phase,
                    )
        liveness = dse.probe_dispatch_process_tree(existing)
        if liveness is dse.ProcessLiveness.UNKNOWN:
            _fail(AdmittedDispatchConflictError, "process_liveness_unknown")
        phase = (
            existing_handoff.phase if existing_handoff is not None
            else ScheduledDispatchHandoffPhase.ADMISSION_COMMITTED
        )
        return _result(
            request, existing.generation_id, phase,
            AdmittedDispatchStartOutcome.RECOVERY_REQUIRED, existing.phase,
        )
    if existing is None:
        creator_pid, creator_creation = dse.get_current_process_identity()
        if not creator_creation:
            _fail(AdmittedDispatchConflictError, "creator_identity")
        generation = "GEN-" + hashlib.sha256((request.dispatch_id + request.handoff_id + request.plan_digest).encode()).hexdigest()[:32]
        existing = dse.reserve_receipt(root, task_id=request.task_id, revision=request.revision, attempt=request.attempt, dispatch_id=request.dispatch_id, lease_epoch=request.lease_epoch, holder_instance_id=request.holder_instance_id, generation_id=generation, creator_pid=creator_pid, creator_creation_time=creator_creation, boot_id=dse.get_boot_id())
    generation = existing.generation_id
    handoff = ScheduledDispatchHandoff(HANDOFF_SCHEMA_VERSION, request.handoff_id, request.queue_id, request.plan_digest, request.receipt_id, request.task_id, request.revision, request.attempt, request.dispatch_id, request.dispatch_event_id, request.outbox_message_id, request.lease_id, request.lease_epoch, request.holder_instance_id, plan.worker_kind, plan.assessment_id, plan.expected_snapshot_commit, plan.model_selection.model_binding_id, ScheduledDispatchHandoffPhase.ADMISSION_COMMITTED, request.requested_at, None, None, None, None, "sha256:" + "0" * 64)
    reserved, _ = handoff_store.reserve_handoff(handoff, existing, request.requested_at)
    dispatch_transition = TransitionResult(request.task_id, event.event_id, event.from_state, event.to_state, event.occurred_at, request.outbox_message_id)
    execution = await orchestrator.start_admitted_dispatch_execution(cycle, providers, lease, generation, dispatch_transition)
    current_receipt = dse.read_dispatch_receipt(root, request.dispatch_id)
    if current_receipt is None:
        _fail(AdmittedDispatchConflictError, "process_receipt_missing")
    handoff_store.advance_phase(request.dispatch_id, ScheduledDispatchHandoffPhase.SUPERVISOR_STARTED, _handoff_timestamp(current_receipt.written_at), current_receipt)
    handoff_store.advance_phase(request.dispatch_id, ScheduledDispatchHandoffPhase.WORKER_STARTED, _handoff_timestamp(current_receipt.written_at), current_receipt)
    handoff_store.advance_phase(request.dispatch_id, ScheduledDispatchHandoffPhase.ACKNOWLEDGED, request.requested_at, current_receipt)
    if active_registry is not None:
        owner_loop = asyncio.get_running_loop()

        def cancel_callback(
            command: DockyardActiveCancellationCommand,
        ) -> DockyardActiveCancellationOutcome:
            if owner_loop.is_closed():
                raise RuntimeError("owner event loop is closed")
            cancellation_request = ActiveDispatchCancellationRequest(
                execution.handle,
                TransitionRequest(
                    TransitionCAS(
                        command.task_id,
                        command.revision,
                        "in_progress",
                        command.expected_snapshot_commit,
                    ),
                    None,
                    command.event_id,
                    "TASK_CANCELLED",
                    CancelledPayload(),
                    TransitionEventContext(None, (), ()),
                ),
            )
            future = asyncio.run_coroutine_threadsafe(
                orchestrator.cancel_active_dispatch(execution, cancellation_request),
                owner_loop,
            )
            try:
                result = future.result(timeout=120)
            except BaseException:
                future.cancel()
                raise
            if type(result) is not ActiveDispatchCancellationResult:
                raise RuntimeError("active cancellation returned an invalid result")
            return DockyardActiveCancellationOutcome(
                command.task_id,
                command.dispatch_id,
                result.cancellation_transition.event_id,
                result.cancellation_transition.occurred_at,
            )

        identity = make_active_execution_identity(
            request.task_id,
            request.revision,
            request.attempt,
            request.dispatch_id,
            generation,
            request.requested_at,
        )
        active_registry.register(identity, cancel_callback)
    try:
        dispatch_cycle_result = await execution.wait()
    finally:
        if active_registry is not None:
            active_registry.complete(request.dispatch_id, generation)
    if completion is not None:
        completion.append(dispatch_cycle_result)
    final_receipt = dse.read_dispatch_receipt(root, request.dispatch_id)
    tombstone = dse.read_dispatch_tombstone(root, request.dispatch_id)
    if final_receipt is None or tombstone is None or tombstone.generation_id != generation:
        _fail(AdmittedDispatchConflictError, "final_evidence")
    handoff_store.advance_phase(request.dispatch_id, ScheduledDispatchHandoffPhase.FINALIZING, _handoff_timestamp(final_receipt.written_at), final_receipt)
    finalized, _ = handoff_store.advance_phase(request.dispatch_id, ScheduledDispatchHandoffPhase.FINALIZED, _handoff_timestamp(tombstone.finalized_at), final_receipt)
    return _result(request, generation, finalized.phase, AdmittedDispatchStartOutcome.STARTED, final_receipt.phase)


async def start_admitted_dispatch(
    request: AdmittedDispatchStartRequest,
    providers: Mapping[str, AgentCliProvider],
    orchestrator: WorkflowOrchestrator,
) -> AdmittedDispatchStartResult:
    """Preserve the existing start-only API."""
    return await _start_admitted_dispatch(request, providers, orchestrator, None)


async def start_admitted_dispatch_completion(
    request: AdmittedDispatchStartRequest,
    providers: Mapping[str, AgentCliProvider],
    orchestrator: WorkflowOrchestrator,
    *,
    active_registry: DockyardActiveExecutionRegistry | None = None,
) -> AdmittedDispatchCompletionResult:
    """Return fresh delivery evidence without changing replay behavior."""
    completion: list[DispatchCycleResult] = []
    start_result = await _start_admitted_dispatch(
        request, providers, orchestrator, completion, active_registry
    )
    if len(completion) > 1:
        _fail(AdmittedDispatchConflictError, "completion_count")
    return AdmittedDispatchCompletionResult(
        start_result=start_result,
        dispatch_cycle_result=completion[0] if completion else None,
    )
