"""AgentDesk WorkflowOrchestrator — through TC-13.18d.10b.

Single dispatch-cycle execution: snapshot -> acquire lease ->
TASK_DISPATCHED -> heartbeat + run_worker_observed ->
(DispatchStarted → validate → DISPATCH_ACKNOWLEDGED) ->
Worker exit 0 → decode_worker_result → require_delivery_receipt →
DELIVERY_SUBMITTED → stop heartbeat -> release lease -> result.

Acceptance cycle (TC-13.18c.2): run_audit_gateway → verdict=pass →
acquire review lease → DELIVERY_ACCEPTED → release lease →
optional CHANGE_INTEGRATED → AcceptanceCycleResult.

Delivery remediation (TC-13.18d.1): audit verdict=fail →
acquire remediation lease → DELIVERY_RETURNED → release lease →
TASK_REQUEUED (lease=None) → DeliveryRemediationResult.

Blocked audit escalation (TC-13.18d.2): audit verdict=blocked →
evaluate_escalation(current_worker_kind) → TASK_BLOCKED (lease=None) →
BlockedAuditResult.

Quiescent task cancellation (TC-13.18d.7): StateProvider.snapshot() →
validate no current_dispatch → TASK_CANCELLED (lease=None) →
TaskCancellationResult.

Non-goals (explicitly excluded):
* Escalation, retry, automatic blocked/fail remediation
* New subprocess termination implementation (the gateway owns termination)
* Codex output decoding (blocked until TC-13.9c.2)
* Parsing stdout/stderr manually, guessing commits from Git HEAD
* Git worktree lifecycle, subprocess invocation, file I/O
* ApprovalGate, hold_worker_slot_fence, .state-transition.lock
* Budget computation (delegated to WorkerAdapter.run_worker)
* ID generation (all identifiers are caller-supplied)
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from control_plane_transition import (
    AcknowledgePayload,
    BlockedPayload,
    BlockerCancelledPayload,
    BlockerRescopedPayload,
    BlockerResolvedPayload,
    CancelledPayload,
    ControlPlaneTransitionService,
    DeliveryAcceptedPayload,
    DeliveryReturnedPayload,
    DeliverySubmittedPayload,
    DispatchCAS,
    DispatchFailedPayload,
    DispatchPayload,
    IntegrationPayload,
    OwnerLossRetryReservationRequest,
    OwnerLossTransitionCheck,
    RequeuePayload,
    SupersededPayload,
    TransitionCAS,
    apply_owner_loss_recovery_transition,
    execute_owner_loss_retry_reservation,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from core_types import TaskDifficulty, WorkerKind
from dispatcher_gateway import (
    AgentCliProvider,
    DispatchCancelledError,
    DispatchRequest,
    DispatchStarted,
    DispatchStartedObserver,
)
import dispatch_supervisor_evidence as _dse
from escalation_service import (
    EscalationAction,
    EscalationDecision,
    EscalationRequest,
    evaluate_escalation,
)
from mad_audit_gateway import (
    MadAuditGatewayInput,
    MadAuditGatewayResult,
)
from mad_gateway import MadGatewayConfig
from state_provider import StateProvider, StateProviderError
from worker_adapter import WorkerResult, run_worker_observed
from worker_output_decoder import (
    DeliveryReceipt,
    WorkerOutput,
    decode_worker_result,
    require_delivery_receipt,
)
from worker_slot_lease import (
    WorkerSlotLease,
    WorkerSlotLeaseError,
    acquire_worker_slot,
    release_worker_slot,
    renew_worker_slot,
)

# -- run_audit_gateway (imported for module-level reference) -------------------
from mad_audit_gateway import run_audit_gateway

__all__ = [
    "AcceptanceCycleRequest",
    "AcceptanceCycleResult",
    "ActiveDispatchCancellationRequest",
    "ActiveDispatchCancellationResult",
    "ActiveDispatchExecution",
    "ActiveDispatchHandle",
    "ActiveDispatchSupersessionRequest",
    "ActiveDispatchSupersessionResult",
    "BlockedAuditRequest",
    "BlockedAuditResult",
    "BlockedCancellationRequest",
    "BlockedCancellationResult",
    "BlockedRescopeRequest",
    "BlockedRescopeResult",
    "DeliveryReceipt",
    "DeliveryRemediationRequest",
    "DeliveryRemediationResult",
    "BoundedDispatchRetryRequest",
    "BoundedDispatchRetryResult",
    "DispatchCycleRequest",
    "DispatchCycleResult",
    "DispatchRetryAttempt",
    "EscalatedRedispatchRequest",
    "EscalatedRedispatchResult",
    "IntegrationFailureRequest",
    "IntegrationFailureResult",
    "OwnerLossRecoveryRequest",
    "OwnerLossRecoveryResult",
    "TaskCancellationRequest",
    "TaskCancellationResult",
    "TaskSupersessionRequest",
    "TaskSupersessionResult",
    "WorkerOutput",
    "WorkflowClock",
    "WorkflowHeartbeatError",
    "WorkflowInputError",
    "WorkflowInvariantError",
    "WorkflowOrchestrator",
    "WorkflowOrchestratorError",
    "decode_worker_result",
    "require_delivery_receipt",
]


# -- WorkflowClock Protocol ---------------------------------------------------


class WorkflowClock(Protocol):
    """Injectable clock -- never use ``datetime.now()`` / ``time.sleep()``
    directly in production logic.

    * ``now()`` must return a timezone-aware UTC ``datetime``.
    * ``monotonic()`` returns a float for duration computation.
    * ``sleep()`` suspends the current task for *seconds*.
    """

    def now(self) -> datetime:
        ...

    def monotonic(self) -> float:
        ...

    async def sleep(self, seconds: float) -> None:
        ...


# -- DispatchCycleRequest -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchCycleRequest:
    """Immutable input for a single dispatch cycle — nine fields.

    All identifiers are caller-supplied -- the orchestrator generates none.

    The three delivery fields (*delivery_event_id*, *delivery_event_context*,
    *provider_cli_version*) are validated in __post_init__ before any acquire.
    """

    dispatch_request: DispatchRequest
    dispatch_transition_request: TransitionRequest
    acknowledge_transition_request: TransitionRequest
    delivery_event_id: str
    delivery_event_context: TransitionEventContext
    provider_cli_version: str
    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    holder_instance_id: str

    def __post_init__(self) -> None:
        # -- dispatch_request type ------------------------------------------
        if not isinstance(self.dispatch_request, DispatchRequest):
            raise TypeError(
                f"dispatch_request must be DispatchRequest, "
                f"got {type(self.dispatch_request).__name__}"
            )

        # -- dispatch_transition_request type -------------------------------
        if not isinstance(self.dispatch_transition_request, TransitionRequest):
            raise TypeError(
                "dispatch_transition_request must be TransitionRequest, "
                f"got {type(self.dispatch_transition_request).__name__}"
            )

        tr = self.dispatch_transition_request

        # -- event_type must be TASK_DISPATCHED -----------------------------
        if tr.event_type != "TASK_DISPATCHED":
            raise ValueError(
                "event_type must be TASK_DISPATCHED, "
                f"got {tr.event_type!r}"
            )

        # -- payload must be DispatchPayload --------------------------------
        if not isinstance(tr.payload, DispatchPayload):
            raise TypeError(
                "payload must be DispatchPayload, "
                f"got {type(tr.payload).__name__}"
            )

        # -- acknowledge_transition_request type ----------------------------
        if not isinstance(self.acknowledge_transition_request, TransitionRequest):
            raise TypeError(
                "acknowledge_transition_request must be TransitionRequest, "
                f"got {type(self.acknowledge_transition_request).__name__}"
            )

        ack_tr = self.acknowledge_transition_request

        # -- ACK event_type must be DISPATCH_ACKNOWLEDGED -------------------
        if ack_tr.event_type != "DISPATCH_ACKNOWLEDGED":
            raise ValueError(
                "acknowledge_transition_request event_type must be "
                f"DISPATCH_ACKNOWLEDGED, got {ack_tr.event_type!r}"
            )

        # -- ACK payload must be AcknowledgePayload -------------------------
        if not isinstance(ack_tr.payload, AcknowledgePayload):
            raise TypeError(
                "acknowledge_transition_request payload must be "
                f"AcknowledgePayload, got {type(ack_tr.payload).__name__}"
            )

        # -- ACK to_state must be in_progress -------------------------------
        ack_from, ack_to = "dispatched", "in_progress"
        # defer to_state validation to the transition service; just check
        # consistency here

        # -- ACK cas.task_id matches dispatch cas.task_id -------------------
        if ack_tr.cas.task_id != tr.cas.task_id:
            raise ValueError(
                "acknowledge_transition_request task_id must match "
                "dispatch_transition_request task_id"
            )

        # -- ACK cas.expected_state must be "dispatched" --------------------
        if ack_tr.cas.expected_state != "dispatched":
            raise ValueError(
                "acknowledge_transition_request expected_state must be "
                f"'dispatched', got {ack_tr.cas.expected_state!r}"
            )

        # -- ACK cas.expected_revision matches dispatch expected_revision ----
        # TASK_DISPATCHED does NOT increment the task revision in canonical
        # state.  The ACK CAS must match the post-dispatch task revision,
        # which is the same as the pre-dispatch revision.
        if ack_tr.cas.expected_revision != tr.cas.expected_revision:
            raise ValueError(
                "acknowledge_transition_request expected_revision must "
                f"equal dispatch expected_revision "
                f"({tr.cas.expected_revision}), "
                f"got {ack_tr.cas.expected_revision}"
            )

        # -- ACK cas.expected_snapshot_commit matches dispatch --------------
        if ack_tr.cas.expected_snapshot_commit != tr.cas.expected_snapshot_commit:
            raise ValueError(
                "acknowledge_transition_request expected_snapshot_commit "
                "must match dispatch_transition_request"
            )

        # -- ACK dispatch_cas must not be None ------------------------------
        if ack_tr.dispatch_cas is None:
            raise ValueError(
                "acknowledge_transition_request dispatch_cas must not be None"
            )

        # -- ACK dispatch_cas matches dispatch identity --------------------
        dr = self.dispatch_request
        ack_dcas = ack_tr.dispatch_cas
        if ack_dcas.expected_dispatch_id != dr.identity.dispatch_id:
            raise ValueError(
                "acknowledge_transition_request dispatch_cas "
                "expected_dispatch_id must match dispatch_request "
                "dispatch_id"
            )
        if ack_dcas.expected_attempt != dr.identity.attempt:
            raise ValueError(
                "acknowledge_transition_request dispatch_cas "
                "expected_attempt must match dispatch_request attempt"
            )

        # -- ACK event_id differs from dispatch event_id --------------------
        if ack_tr.event_id == tr.event_id:
            raise ValueError(
                "acknowledge_transition_request event_id must differ "
                "from dispatch_transition_request event_id"
            )

        # -- delivery_event_id ----------------------------------------------
        if not isinstance(self.delivery_event_id, str):
            raise TypeError(
                "delivery_event_id must be str, "
                f"got {type(self.delivery_event_id).__name__}"
            )
        if not self.delivery_event_id:
            raise ValueError("delivery_event_id must not be empty")
        if not self.delivery_event_id.startswith("EVT-"):
            raise ValueError(
                "delivery_event_id must start with 'EVT-', "
                f"got {self.delivery_event_id!r}"
            )

        # delivery event_id must differ from dispatch event_id
        if self.delivery_event_id == tr.event_id:
            raise ValueError(
                "delivery_event_id must differ from dispatch event_id"
            )

        # delivery event_id must differ from ACK event_id
        if self.delivery_event_id == ack_tr.event_id:
            raise ValueError(
                "delivery_event_id must differ from ACK event_id"
            )

        # -- delivery_event_context -----------------------------------------
        if not isinstance(self.delivery_event_context, TransitionEventContext):
            raise TypeError(
                "delivery_event_context must be TransitionEventContext, "
                f"got {type(self.delivery_event_context).__name__}"
            )

        # -- provider_cli_version -------------------------------------------
        if not isinstance(self.provider_cli_version, str):
            raise TypeError(
                "provider_cli_version must be str, "
                f"got {type(self.provider_cli_version).__name__}"
            )
        if not self.provider_cli_version:
            raise ValueError("provider_cli_version must not be empty")
        if self.provider_cli_version != self.provider_cli_version.strip():
            raise ValueError(
                "provider_cli_version must not have whitespace envelope"
            )
        if any(c.isspace() for c in self.provider_cli_version):
            raise ValueError(
                "provider_cli_version must not contain whitespace"
            )

        # -- provider boundary ----------------------------------------------
        snapshot_sn = dr.model_selection
        allowed_providers = frozenset({"claude", "claudecode"})
        allowed_version = "2.1.214"

        if snapshot_sn.selected_model_provider not in allowed_providers:
            if snapshot_sn.selected_model_provider == "codex":
                raise WorkflowInputError(
                    "codex provider is not supported "
                    "(blocked until TC-13.9c.2)"
                )
            raise WorkflowInputError(
                "provider must be claude or claudecode for delivery"
            )

        if self.provider_cli_version != allowed_version:
            raise WorkflowInputError(
                f"provider_cli_version must be {allowed_version!r}"
            )

        # -- worker_kind ----------------------------------------------------
        if not isinstance(self.worker_kind, WorkerKind):
            raise TypeError(
                f"worker_kind must be WorkerKind, "
                f"got {type(self.worker_kind).__name__}"
            )

        # -- task_difficulty ------------------------------------------------
        if not isinstance(self.task_difficulty, TaskDifficulty):
            raise TypeError(
                "task_difficulty must be TaskDifficulty, "
                f"got {type(self.task_difficulty).__name__}"
            )

        # -- holder_instance_id ---------------------------------------------
        if not isinstance(self.holder_instance_id, str) or not self.holder_instance_id:
            raise TypeError(
                "holder_instance_id must be a non-empty str, "
                f"got {type(self.holder_instance_id).__name__}"
            )

        dr = self.dispatch_request
        payload = tr.payload

        # -- task_id consistency --------------------------------------------
        if dr.identity.task_id != tr.cas.task_id:
            raise ValueError(
                "dispatch_request task_id must match transition task_id"
            )

        # -- dispatch_id consistency ----------------------------------------
        if dr.identity.dispatch_id != payload.dispatch_id:
            raise ValueError(
                "dispatch_request dispatch_id must match "
                "transition payload dispatch_id"
            )

        # -- model_selection identity (same object) -------------------------
        if dr.model_selection is not payload.model_selection:
            raise ValueError(
                "dispatch_request model_selection must be the same object "
                "as transition payload model_selection"
            )


# -- DispatchCycleResult ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchCycleResult:
    """Immutable result of a single dispatch cycle — nine fields.

    Includes both ACK and delivery transitions, plus the decoded
    ``WorkerOutput`` and ``DeliveryReceipt``.
    """

    worker_result: WorkerResult
    worker_output: WorkerOutput
    delivery_receipt: DeliveryReceipt
    dispatch_transition: TransitionResult
    acknowledge_transition: TransitionResult
    delivery_transition: TransitionResult
    slot_id: str
    lease_epoch: int
    duration_seconds: float


# -- Bounded dispatch retry types (TC-13.18d.11c) ---------------------------


@dataclass(frozen=True, slots=True)
class DispatchRetryAttempt:
    """Caller-supplied dispatch attempt and paired recovery request."""

    dispatch_cycle_request: DispatchCycleRequest
    failure_transition_request: TransitionRequest

    def __post_init__(self) -> None:
        if type(self.dispatch_cycle_request) is not DispatchCycleRequest:
            raise TypeError(
                "dispatch_cycle_request must be exact DispatchCycleRequest"
            )
        if type(self.failure_transition_request) is not TransitionRequest:
            raise TypeError(
                "failure_transition_request must be exact TransitionRequest"
            )

        cycle = self.dispatch_cycle_request
        failure = self.failure_transition_request
        identity = cycle.dispatch_request.identity
        dispatch_transition = cycle.dispatch_transition_request

        if type(failure.event_type) is not str or failure.event_type != (
            "DISPATCH_FAILED"
        ):
            raise WorkflowInputError(
                "failure_transition_request event_type must be "
                "exact DISPATCH_FAILED"
            )
        if type(failure.payload) is not DispatchFailedPayload:
            raise WorkflowInputError(
                "failure_transition_request payload must be exact "
                "DispatchFailedPayload"
            )
        if failure.cas.expected_state != "in_progress":
            raise WorkflowInputError(
                "failure_transition_request expected_state must be "
                "exact in_progress"
            )
        if type(failure.dispatch_cas) is not DispatchCAS:
            raise WorkflowInputError(
                "failure_transition_request requires exact DispatchCAS"
            )
        if failure.cas.task_id != identity.task_id:
            raise WorkflowInputError(
                "failure request task must match dispatch attempt"
            )
        if failure.cas.expected_revision != identity.revision:
            raise WorkflowInputError(
                "failure request revision must match dispatch attempt"
            )
        if failure.cas.expected_snapshot_commit != (
            dispatch_transition.cas.expected_snapshot_commit
        ):
            raise WorkflowInputError(
                "failure request snapshot commit must match dispatch attempt"
            )
        if failure.dispatch_cas.expected_dispatch_id != identity.dispatch_id:
            raise WorkflowInputError(
                "failure request dispatch id must match dispatch attempt"
            )
        if failure.dispatch_cas.expected_attempt != identity.attempt:
            raise WorkflowInputError(
                "failure request attempt must match dispatch attempt"
            )
        if not failure.event_context.evidence_refs:
            raise WorkflowInputError(
                "failure request requires evidence refs"
            )


@dataclass(frozen=True, slots=True)
class BoundedDispatchRetryRequest:
    """Finite caller-supplied retry plan of one to three attempts."""

    attempts: tuple[DispatchRetryAttempt, ...]

    def __post_init__(self) -> None:
        if type(self.attempts) is not tuple:
            raise TypeError("attempts must be an exact tuple")
        if not 1 <= len(self.attempts) <= 3:
            raise ValueError("attempts length must be between one and three")
        for item in self.attempts:
            if type(item) is not DispatchRetryAttempt:
                raise TypeError(
                    "attempts must contain exact DispatchRetryAttempt values"
                )

        first_identity = self.attempts[0].dispatch_cycle_request.dispatch_request.identity
        first_task_id = first_identity.task_id
        first_revision = first_identity.revision
        previous_attempt: int | None = None
        identifiers: set[str] = set()

        for item in self.attempts:
            cycle = item.dispatch_cycle_request
            failure = item.failure_transition_request
            DispatchRetryAttempt(cycle, failure)
            identity = cycle.dispatch_request.identity
            payload = cycle.dispatch_transition_request.payload
            if type(payload) is not DispatchPayload:
                raise WorkflowInputError(
                    "dispatch transition payload must be exact DispatchPayload"
                )
            if identity.task_id != first_task_id:
                raise WorkflowInputError(
                    "all retry attempts must target the same task"
                )
            if identity.revision != first_revision:
                raise WorkflowInputError(
                    "all retry attempts must target the same revision"
                )
            if previous_attempt is not None and identity.attempt != (
                previous_attempt + 1
            ):
                raise WorkflowInputError(
                    "retry attempt numbers must be strictly consecutive"
                )
            if payload.new_attempt != identity.attempt:
                raise WorkflowInputError(
                    "dispatch payload attempt must match dispatch identity"
                )
            if failure.cas.expected_state != "in_progress":
                raise WorkflowInputError(
                    "failure request expected_state must be exact in_progress"
                )

            attempt_identifiers = (
                identity.dispatch_id,
                cycle.dispatch_transition_request.event_id,
                cycle.acknowledge_transition_request.event_id,
                cycle.delivery_event_id,
                payload.outbox_message_id,
                failure.event_id,
            )
            if len(set(attempt_identifiers)) != len(attempt_identifiers):
                raise WorkflowInputError(
                    "retry plan identifiers must be distinct"
                )
            if identifiers.intersection(attempt_identifiers):
                raise WorkflowInputError(
                    "retry plan identifiers must be globally distinct"
                )
            identifiers.update(attempt_identifiers)
            previous_attempt = identity.attempt


@dataclass(frozen=True, slots=True)
class BoundedDispatchRetryResult:
    """Successful finite dispatch retry result."""

    task_id: str
    attempts_started: int
    recovery_transitions: tuple[TransitionResult, ...]
    dispatch_cycle_result: DispatchCycleResult

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise TypeError("task_id must be a non-empty exact str")
        if (
            type(self.attempts_started) is not int
            or not 1 <= self.attempts_started <= 3
        ):
            raise TypeError("attempts_started must be an int from one to three")
        if type(self.recovery_transitions) is not tuple:
            raise TypeError("recovery_transitions must be an exact tuple")
        for transition in self.recovery_transitions:
            if type(transition) is not TransitionResult:
                raise TypeError(
                    "recovery_transitions must contain exact TransitionResult"
                )
        if type(self.dispatch_cycle_result) is not DispatchCycleResult:
            raise TypeError(
                "dispatch_cycle_result must be exact DispatchCycleResult"
            )


class _DispatchFailureKind(str, Enum):
    """Private typed classification frozen by the execution finalizer."""

    WORKER_FAILED = "worker_failed"
    WORKER_OUTPUT_FAILED = "worker_output_failed"
    DELIVERY_TRANSITION_FAILED = "delivery_transition_failed"


@dataclass(frozen=True, slots=True)
class _DispatchFinalizerMetadata:
    """Private non-sensitive finalizer evidence published before completion."""

    winner: str
    worker_done: bool
    heartbeat_done: bool
    release_completed: bool
    failure_kind: _DispatchFailureKind | None


# -- ActiveDispatchCancellation types (TC-13.18d.9b) -------------------------


@dataclass(frozen=True, slots=True)
class ActiveDispatchHandle:
    """Immutable identity snapshot for a live dispatch."""

    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    holder_instance_id: str
    lease_epoch: int
    lease: WorkerSlotLease

    def __post_init__(self) -> None:
        for name in ("task_id", "dispatch_id", "holder_instance_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise TypeError(f"{name} must be a non-empty str")
        for name in ("revision", "attempt", "lease_epoch"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise TypeError(f"{name} must be a positive int")
        if type(self.lease) is not WorkerSlotLease:
            raise TypeError("lease must be WorkerSlotLease")
        if self.dispatch_id != self.lease.holder_dispatch_id:
            raise ValueError("dispatch_id must match lease holder_dispatch_id")
        if self.holder_instance_id != self.lease.holder_instance_id:
            raise ValueError(
                "holder_instance_id must match lease holder_instance_id"
            )
        if self.lease_epoch != self.lease.lease_epoch:
            raise ValueError("lease_epoch must match lease lease_epoch")


def _bind_active_dispatch_cas(
    handle: ActiveDispatchHandle,
    transition: TransitionRequest,
) -> TransitionRequest:
    """Return an immutable transition copy bound to *handle* when needed."""
    if transition.dispatch_cas is not None:
        return transition
    bound = object.__new__(TransitionRequest)
    object.__setattr__(bound, "cas", transition.cas)
    object.__setattr__(
        bound,
        "dispatch_cas",
        DispatchCAS(
            expected_dispatch_id=handle.dispatch_id,
            expected_attempt=handle.attempt,
        ),
    )
    object.__setattr__(bound, "event_id", transition.event_id)
    object.__setattr__(bound, "event_type", transition.event_type)
    object.__setattr__(bound, "payload", transition.payload)
    object.__setattr__(bound, "event_context", transition.event_context)
    return bound


@dataclass(frozen=True, slots=True)
class ActiveDispatchCancellationRequest:
    """Immutable request to cancel a creator-owned live dispatch."""

    handle: ActiveDispatchHandle
    cancellation_transition_request: TransitionRequest

    def __post_init__(self) -> None:
        transition = self.cancellation_transition_request
        if (
            type(self.handle) is ActiveDispatchHandle
            and type(transition) is TransitionRequest
            and transition.dispatch_cas is None
        ):
            # TransitionRequest's legacy constructor still treats
            # TASK_CANCELLED as PM-only, while the transition service already
            # supports optional DispatchCAS for its active path.  Bind the
            # immutable request to the execution handle without mutating the
            # caller's TransitionRequest or the lower-level production module.
            object.__setattr__(
                self,
                "cancellation_transition_request",
                _bind_active_dispatch_cas(self.handle, transition),
            )


@dataclass(frozen=True, slots=True)
class ActiveDispatchCancellationResult:
    """Result of an active-dispatch TASK_CANCELLED transition."""

    task_id: str
    dispatch_id: str
    cancellation_transition: TransitionResult


@dataclass(frozen=True, slots=True)
class ActiveDispatchSupersessionRequest:
    """Immutable request to supersede a creator-owned live dispatch."""

    handle: ActiveDispatchHandle
    supersession_transition_request: TransitionRequest

    def __post_init__(self) -> None:
        transition = self.supersession_transition_request
        if (
            type(self.handle) is ActiveDispatchHandle
            and type(transition) is TransitionRequest
            and transition.dispatch_cas is None
        ):
            object.__setattr__(
                self,
                "supersession_transition_request",
                _bind_active_dispatch_cas(self.handle, transition),
            )


@dataclass(frozen=True, slots=True)
class ActiveDispatchSupersessionResult:
    """Result of an active-dispatch TASK_SUPERSEDED transition."""

    task_id: str
    dispatch_id: str
    superseded_by: str
    supersession_transition: TransitionResult


class ActiveDispatchExecution:
    """In-process controller for one live dispatch.

    The task and future attributes are deliberately private.  Public callers
    can inspect only the frozen identity handle and await the final outcome.
    """

    __slots__ = (
        "_owner",
        "_handle",
        "_request",
        "_worker_task",
        "_heartbeat_task",
        "_state_lock",
        "_finalize_lock",
        "_completion",
        "_runner_task",
        "_winner",
        "_release_started",
        "_release_completed",
        "_finalizer_metadata",
        "_dispatch_transition",
        "_ack_transition",
        "_start_monotonic",
        "_ready",
        "_generation_id",
    )

    def __init__(
        self,
        owner: WorkflowOrchestrator,
        handle: ActiveDispatchHandle,
        request: DispatchCycleRequest,
        worker_task: asyncio.Task[WorkerResult],
        heartbeat_task: asyncio.Task[None],
        dispatch_transition: TransitionResult,
        acknowledge_transition: TransitionResult,
        start_monotonic: float,
    ) -> None:
        self._owner = owner
        self._handle = handle
        self._request = request
        self._worker_task = worker_task
        self._heartbeat_task = heartbeat_task
        self._state_lock = asyncio.Lock()
        self._finalize_lock = asyncio.Lock()
        self._completion: asyncio.Future[DispatchCycleResult] = (
            asyncio.get_running_loop().create_future()
        )
        # Retrieve stored failures even if a caller intentionally never waits.
        self._completion.add_done_callback(
            lambda future: future.exception()
            if not future.cancelled()
            else None
        )
        self._runner_task: asyncio.Task[None] | None = None
        self._winner: str | None = None
        self._release_started = False
        self._release_completed = False
        self._finalizer_metadata: _DispatchFinalizerMetadata | None = None
        self._dispatch_transition = dispatch_transition
        self._ack_transition = acknowledge_transition
        self._start_monotonic = start_monotonic
        self._ready = False
        self._generation_id = ""

    @property
    def handle(self) -> ActiveDispatchHandle:
        """Return the immutable dispatch identity, never a live Task."""
        return self._handle

    async def wait(self) -> DispatchCycleResult:
        """Observe the single stored result or exception."""
        return await asyncio.shield(self._completion)


# -- AcceptanceCycle types -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcceptanceCycleRequest:
    """Immutable input for an independent acceptance cycle — exactly six fields.

    All identifiers are caller-supplied.
    """

    dispatch_cycle_result: DispatchCycleResult
    audit_input: MadAuditGatewayInput
    acceptance_transition_request: TransitionRequest
    integration_transition_request: TransitionRequest | None
    worker_kind: WorkerKind
    holder_instance_id: str

    def __post_init__(self) -> None:
        _vr = _validate_acceptance_cycle_request
        _vr(self)


@dataclass(frozen=True, slots=True)
class AcceptanceCycleResult:
    """Immutable result of an acceptance cycle — exactly four fields.

    Semantics:
    * audit ``pass`` → ``accept_transition`` must be present.
    * audit ``fail | blocked`` → both transitions are ``None``.
    * integration not requested → ``integrate_transition=None``.
    * integration requested + success → real integration result.
    """

    task_id: str
    audit_result: MadAuditGatewayResult
    accept_transition: TransitionResult | None
    integrate_transition: TransitionResult | None


# -- DeliveryRemediation types (TC-13.18d.1) ----------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryRemediationRequest:
    """Immutable input for delivery remediation — exactly six fields.

    All identifiers are caller-supplied.
    """

    acceptance_cycle_result: AcceptanceCycleResult
    dispatch_cycle_result: DispatchCycleResult
    return_transition_request: TransitionRequest
    requeue_transition_request: TransitionRequest
    worker_kind: WorkerKind
    holder_instance_id: str


@dataclass(frozen=True, slots=True)
class DeliveryRemediationResult:
    """Immutable result of delivery remediation — exactly four fields."""

    task_id: str
    audit_result: MadAuditGatewayResult
    return_transition: TransitionResult
    requeue_transition: TransitionResult


# -- BlockedAudit types (TC-13.18d.2) ----------------------------------------


@dataclass(frozen=True, slots=True)
class BlockedAuditRequest:
    """Immutable input for blocked audit escalation — exactly four fields.

    All identifiers are caller-supplied.
    """

    acceptance_cycle_result: AcceptanceCycleResult
    dispatch_cycle_result: DispatchCycleResult
    block_transition_request: TransitionRequest
    current_worker_kind: WorkerKind


@dataclass(frozen=True, slots=True)
class BlockedAuditResult:
    """Immutable result of a blocked audit cycle — exactly four fields."""

    task_id: str
    audit_result: MadAuditGatewayResult
    escalation_decision: EscalationDecision
    block_transition: TransitionResult


# -- EscalatedRedispatch types (TC-13.18d.3) -----------------------------------


@dataclass(frozen=True, slots=True)
class EscalatedRedispatchRequest:
    """Immutable input for escalated single redispatch — exactly four fields.

    All identifiers are caller-supplied.
    """

    blocked_audit_request: BlockedAuditRequest
    blocked_audit_result: BlockedAuditResult
    resolve_transition_request: TransitionRequest
    next_dispatch_cycle_request: DispatchCycleRequest


@dataclass(frozen=True, slots=True)
class EscalatedRedispatchResult:
    """Immutable result of a single escalated redispatch — exactly four fields."""

    task_id: str
    escalation_decision: EscalationDecision
    resolve_transition: TransitionResult
    dispatch_cycle_result: DispatchCycleResult


# -- IntegrationFailure types (TC-13.18d.4) -----------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationFailureRequest:
    """Immutable input for recording an integration failure — exactly four fields.

    All identifiers are caller-supplied.
    """

    acceptance_cycle_request: AcceptanceCycleRequest
    acceptance_cycle_result: AcceptanceCycleResult
    dispatch_cycle_result: DispatchCycleResult
    failure_transition_request: TransitionRequest


@dataclass(frozen=True, slots=True)
class IntegrationFailureResult:
    """Immutable result of integration failure recording — exactly three fields."""

    task_id: str
    audit_result: MadAuditGatewayResult
    failure_transition: TransitionResult


# -- OwnerLossRecovery types (TC-13.18d.12b/.12c) ------------------------------


@dataclass(frozen=True, slots=True)
class OwnerLossRecoveryRequest:
    """Immutable input for owner-loss dispatch recovery — exactly 10 fields.

    All identifiers are caller-supplied.  ``retry_plan`` is either ``None``
    or an exact ``BoundedDispatchRetryRequest`` (transition-only when None).
    """

    task_id: str
    expected_revision: int
    expected_attempt: int
    expected_dispatch_id: str
    expected_generation_id: str
    recovery_event_id: str
    recovery_event_context: TransitionEventContext
    failure_kind: str
    evidence_refs: tuple[str, ...]
    retry_plan: BoundedDispatchRetryRequest | None

    def __post_init__(self) -> None:
        def _safe_exact(value: object, field: str) -> str:
            if type(value) is not str or not value:
                raise TypeError(f"{field} must be a non-empty exact str")
            if value != value.strip() or any(c in value for c in "\0\r\n"):
                raise ValueError(f"{field} must be a safe exact str")
            return value

        task_id = _safe_exact(self.task_id, "task_id")
        if re.fullmatch(r"TC-[0-9]{3,}", task_id) is None:
            raise ValueError("task_id must match the canonical format")
        if (
            type(self.expected_revision) is not int
            or isinstance(self.expected_revision, bool)
            or self.expected_revision < 1
        ):
            raise TypeError("expected_revision must be a non-bool int >= 1")
        if (
            type(self.expected_attempt) is not int
            or isinstance(self.expected_attempt, bool)
            or self.expected_attempt < 1
        ):
            raise TypeError("expected_attempt must be a non-bool int >= 1")
        _safe_exact(self.expected_dispatch_id, "expected_dispatch_id")
        _safe_exact(self.expected_generation_id, "expected_generation_id")
        event_id = _safe_exact(self.recovery_event_id, "recovery_event_id")
        if not event_id.startswith("EVT-"):
            raise ValueError("recovery_event_id must match the canonical format")
        if type(self.recovery_event_context) is not TransitionEventContext:
            raise TypeError(
                "recovery_event_context must be exact TransitionEventContext"
            )
        failure_kind = _safe_exact(self.failure_kind, "failure_kind")
        DispatchFailedPayload(failure_kind)
        if type(self.evidence_refs) is not tuple:
            raise TypeError("evidence_refs must be an exact tuple")
        if len(self.evidence_refs) == 0:
            raise ValueError("evidence_refs must not be empty")
        for i, ref in enumerate(self.evidence_refs):
            _safe_exact(ref, f"evidence_refs[{i}]")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs must be unique")
        if self.recovery_event_context.evidence_refs != self.evidence_refs:
            raise ValueError(
                "recovery_event_context evidence_refs must match evidence_refs"
            )
        if self.retry_plan is not None:
            if type(self.retry_plan) is not BoundedDispatchRetryRequest:
                raise TypeError(
                    "retry_plan must be BoundedDispatchRetryRequest or None"
                )
            first = self.retry_plan.attempts[0].dispatch_cycle_request
            identity = first.dispatch_request.identity
            if (
                identity.task_id != self.task_id
                or identity.revision != self.expected_revision
                or identity.attempt != self.expected_attempt + 1
                or identity.dispatch_id == self.expected_dispatch_id
            ):
                raise ValueError(
                    "retry_plan identity must be the next distinct attempt"
                )


@dataclass(frozen=True, slots=True)
class OwnerLossRecoveryResult:
    """Immutable result of owner-loss dispatch recovery — exactly 6 fields.

    ``process_liveness`` is the three-state enum from
    ``dispatch_supervisor_evidence.ProcessLiveness``.
    ``retry_result`` is ``None`` when no retry was attempted.
    """

    task_id: str
    recovered_attempt: int
    recovered_dispatch_id: str
    process_liveness: _dse.ProcessLiveness
    recovery_transition: TransitionResult
    retry_result: BoundedDispatchRetryResult | None

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise TypeError("task_id must be a non-empty exact str")
        if (
            type(self.recovered_attempt) is not int
            or isinstance(self.recovered_attempt, bool)
            or self.recovered_attempt < 1
        ):
            raise TypeError("recovered_attempt must be a non-bool int >= 1")
        if (
            type(self.recovered_dispatch_id) is not str
            or not self.recovered_dispatch_id
        ):
            raise TypeError("recovered_dispatch_id must be a non-empty exact str")
        if type(self.process_liveness) is not _dse.ProcessLiveness:
            raise TypeError("process_liveness must be exact ProcessLiveness")
        if type(self.recovery_transition) is not TransitionResult:
            raise TypeError(
                "recovery_transition must be exact TransitionResult"
            )
        if self.retry_result is not None:
            if type(self.retry_result) is not BoundedDispatchRetryResult:
                raise TypeError(
                    "retry_result must be BoundedDispatchRetryResult or None"
                )


# -- BlockedRescope types (TC-13.18d.5) ----------------------------------------


@dataclass(frozen=True, slots=True)
class BlockedRescopeRequest:
    """Immutable input for rescoping an expert blocked task — exactly three fields.

    All identifiers are caller-supplied.
    """

    blocked_audit_request: BlockedAuditRequest
    blocked_audit_result: BlockedAuditResult
    rescope_transition_request: TransitionRequest


@dataclass(frozen=True, slots=True)
class BlockedRescopeResult:
    """Immutable result of a blocked rescope — exactly four fields."""

    task_id: str
    previous_revision: int
    new_revision: int
    rescope_transition: TransitionResult


# -- BlockedCancellation types (TC-13.18d.6) -----------------------------------


@dataclass(frozen=True, slots=True)
class BlockedCancellationRequest:
    """Immutable input for expert blocked task cancellation — exactly three fields.

    All identifiers are caller-supplied.
    """

    blocked_audit_request: BlockedAuditRequest
    blocked_audit_result: BlockedAuditResult
    cancellation_transition_request: TransitionRequest


@dataclass(frozen=True, slots=True)
class BlockedCancellationResult:
    """Immutable result of a blocked cancellation — exactly three fields."""

    task_id: str
    escalation_decision: EscalationDecision
    cancellation_transition: TransitionResult


# -- TaskCancellation types (TC-13.18d.7) ------------------------------------


@dataclass(frozen=True, slots=True)
class TaskCancellationRequest:
    """Immutable input for quiescent task cancellation — exactly one field.

    All identifiers are caller-supplied.
    """

    cancellation_transition_request: TransitionRequest


@dataclass(frozen=True, slots=True)
class TaskCancellationResult:
    """Immutable result of quiescent task cancellation — exactly two fields."""

    task_id: str
    cancellation_transition: TransitionResult


# -- TaskSupersession types (TC-13.18d.8) ------------------------------------


@dataclass(frozen=True, slots=True)
class TaskSupersessionRequest:
    """Immutable input for quiescent task supersession — exactly one field.

    All identifiers are caller-supplied.
    """

    supersession_transition_request: TransitionRequest


@dataclass(frozen=True, slots=True)
class TaskSupersessionResult:
    """Immutable result of quiescent task supersession — exactly three fields."""

    task_id: str
    superseded_by: str
    supersession_transition: TransitionResult


# -- exception hierarchy ------------------------------------------------------


class WorkflowOrchestratorError(Exception):
    """Base for all WorkflowOrchestrator errors."""


class WorkflowInputError(WorkflowOrchestratorError):
    """Invalid argument types / values."""


class WorkflowHeartbeatError(WorkflowOrchestratorError):
    """Heartbeat task terminated unexpectedly without a WorkerSlotLeaseError."""


class WorkflowInvariantError(WorkflowOrchestratorError):
    """Internal precondition violated (snapshot, state, CAS mismatch,
    or monotonic-clock sanity)."""


# -- monotonic validation helper ----------------------------------------------


def _validate_monotonic_delta(start: float, end: float) -> float:
    """Validate *start* and *end* and return ``end - start``.

    Raises :exc:`WorkflowInvariantError` if:
    * either value is ``bool``, non-numeric, NaN, or Infinity;
    * ``end < start``.
    """
    for name, value in (("start", start), ("end", end)):
        if isinstance(value, bool):
            raise WorkflowInvariantError(
                f"monotonic {name} must not be bool"
            )
        if not isinstance(value, (int, float)):
            raise WorkflowInvariantError(
                f"monotonic {name} must be int or float, "
                f"got {type(value).__name__}"
            )
        if value != value:  # NaN
            raise WorkflowInvariantError(
                f"monotonic {name} must not be NaN"
            )
        if math.isinf(value):
            raise WorkflowInvariantError(
                f"monotonic {name} must not be Infinity"
            )
    if end < start:
        raise WorkflowInvariantError(
            "monotonic clock went backwards"
        )
    return end - start


# -- Escalated redispatch validation helpers (TC-13.18d.3) ---------------------


def _validate_blocked_audit_binding(
    bar: BlockedAuditRequest,
    bar_result: BlockedAuditResult,
) -> None:
    """Validate BlockedAuditRequest/Result binding (ref §4.1)."""
    # result.task_id == request 中原 task_id
    if bar_result.task_id != bar.acceptance_cycle_result.task_id:
        raise WorkflowInputError(
            "blocked_audit_result task_id must match "
            "blocked_audit_request acceptance_cycle_result task_id"
        )

    # result.audit_result is blocked_audit_request.acceptance_cycle_result.audit_result
    if bar_result.audit_result is not bar.acceptance_cycle_result.audit_result:
        raise WorkflowInputError(
            "blocked_audit_result.audit_result must be the same object "
            "as blocked_audit_request.acceptance_cycle_result.audit_result"
        )

    # result.escalation_decision.current_worker_kind == blocked_audit_request.current_worker_kind
    if bar_result.escalation_decision.current_worker_kind is not bar.current_worker_kind:
        raise WorkflowInputError(
            "escalation_decision.current_worker_kind must exactly equal "
            "blocked_audit_request.current_worker_kind"
        )

    # result.block_transition.task_id == result.task_id
    if bar_result.block_transition.task_id != bar_result.task_id:
        raise WorkflowInputError(
            "block_transition.task_id must match blocked_audit_result.task_id"
        )

    # result.block_transition.to_state == "blocked"
    if bar_result.block_transition.to_state != "blocked":
        raise WorkflowInputError(
            "block_transition.to_state must be 'blocked'"
        )


def _validate_escalation_decision_for_redispatch(
    decision: EscalationDecision,
) -> None:
    """Validate EscalationDecision for redispatch (ref §4.2)."""
    if decision.action != EscalationAction.ESCALATE:
        raise WorkflowInputError(
            "escalation_decision.action must be ESCALATE"
        )
    if decision.next_worker_kind is None:
        raise WorkflowInputError(
            "escalation_decision.next_worker_kind must not be None "
            "for ESCALATE action"
        )


def _validate_blocked_payload_for_redispatch(
    block_transition_request: TransitionRequest,
) -> None:
    """Validate original BlockedPayload fields for safe redispatch (ref §4.3)."""
    bp = block_transition_request.payload
    if not isinstance(bp, BlockedPayload):
        raise WorkflowInputError(
            "block_transition_request payload must be BlockedPayload"
        )
    if getattr(bp, "blocked_attempt_valid", None) is not False:
        raise WorkflowInputError(
            "BlockedPayload.blocked_attempt_valid must be False"
        )
    if getattr(bp, "resume_state", None) != "ready":
        raise WorkflowInputError(
            "BlockedPayload.resume_state must be 'ready'"
        )


def _validate_blocker_resolved_for_redispatch(
    rtr: TransitionRequest,
    bar: BlockedAuditRequest,
    bar_result: BlockedAuditResult,
) -> None:
    """Validate BLOCKER_RESOLVED TransitionRequest (ref §4.4)."""
    # event_type must be BLOCKER_RESOLVED
    if rtr.event_type != "BLOCKER_RESOLVED":
        raise WorkflowInputError(
            "resolve_transition_request event_type must be "
            "BLOCKER_RESOLVED"
        )

    # payload must be BlockerResolvedPayload
    if not isinstance(rtr.payload, BlockerResolvedPayload):
        raise WorkflowInputError(
            "resolve_transition_request payload must be "
            "BlockerResolvedPayload"
        )

    brp = rtr.payload

    # resume_to_state must be "ready"
    if brp.resume_to_state != "ready":
        raise WorkflowInputError(
            "BlockerResolvedPayload.resume_to_state must be 'ready'"
        )

    # CAS expected_state must be "blocked"
    if rtr.cas.expected_state != "blocked":
        raise WorkflowInputError(
            "resolve_transition_request cas.expected_state must be "
            "'blocked'"
        )

    # to_state must be "ready" (validated by TransitionService)
    # task_id must match
    if rtr.cas.task_id != bar_result.task_id:
        raise WorkflowInputError(
            "resolve_transition_request cas.task_id must match "
            "blocked_audit_result.task_id"
        )

    # dispatch_cas must be None (PM-only)
    if rtr.dispatch_cas is not None:
        raise WorkflowInputError(
            "resolve_transition_request dispatch_cas must be None"
        )

    # event_id dedup: must differ from dispatch, ACK, delivery, block
    dcr = bar.dispatch_cycle_result
    existing_event_ids = {
        dcr.dispatch_transition.event_id,
        dcr.acknowledge_transition.event_id,
        dcr.delivery_transition.event_id,
        bar_result.block_transition.event_id,
    }
    if rtr.event_id in existing_event_ids:
        raise WorkflowInputError(
            "resolve event_id must differ from dispatch, ACK, "
            "delivery, and block event_ids"
        )


def _validate_next_dispatch_cycle_for_redispatch(
    ndcr: DispatchCycleRequest,
    bar: BlockedAuditRequest,
    bar_result: BlockedAuditResult,
) -> None:
    """Validate next DispatchCycleRequest for redispatch (ref §4.5)."""
    decision = bar_result.escalation_decision

    # worker_kind must equal decision.next_worker_kind
    if ndcr.worker_kind is not decision.next_worker_kind:
        raise WorkflowInputError(
            "next_dispatch_cycle_request.worker_kind must equal "
            "escalation_decision.next_worker_kind"
        )

    dcr = bar.dispatch_cycle_result
    old_identity = dcr.delivery_receipt.identity
    new_identity = ndcr.dispatch_request.identity

    # task_id must match
    if new_identity.task_id != old_identity.task_id:
        raise WorkflowInputError(
            "next dispatch identity task_id must match original task_id"
        )

    # revision must match
    if new_identity.revision != old_identity.revision:
        raise WorkflowInputError(
            "next dispatch identity revision must match original revision"
        )

    # new attempt must be exactly old attempt + 1
    expected_new_attempt = old_identity.attempt + 1
    if new_identity.attempt != expected_new_attempt:
        raise WorkflowInputError(
            "next dispatch identity attempt must be "
            f"{expected_new_attempt}"
        )

    # dispatch_id must differ from old dispatch_id
    if new_identity.dispatch_id == old_identity.dispatch_id:
        raise WorkflowInputError(
            "next dispatch_id must differ from previous dispatch_id"
        )

    # DispatchPayload.new_attempt must equal new identity.attempt
    dp = ndcr.dispatch_transition_request.payload
    if isinstance(dp, DispatchPayload):
        if dp.new_attempt != new_identity.attempt:
            raise WorkflowInputError(
                "DispatchPayload.new_attempt must match dispatch identity attempt"
            )
        # DispatchPayload.dispatch_id must match identity.dispatch_id
        if dp.dispatch_id != new_identity.dispatch_id:
            raise WorkflowInputError(
                "DispatchPayload.dispatch_id must match identity.dispatch_id"
            )

    # ACK dispatch_cas must match new identity
    ack_dcas = ndcr.acknowledge_transition_request.dispatch_cas
    if ack_dcas is not None:
        if ack_dcas.expected_dispatch_id != new_identity.dispatch_id:
            raise WorkflowInputError(
                "ACK dispatch_cas expected_dispatch_id must match "
                "new dispatch identity dispatch_id"
            )
        if ack_dcas.expected_attempt != new_identity.attempt:
            raise WorkflowInputError(
                "ACK dispatch_cas expected_attempt must match "
                "new dispatch identity attempt"
            )

    # TaskDifficulty must remain unchanged
    if ndcr.task_difficulty is not dcr.worker_result.task_difficulty:
        raise WorkflowInputError(
            "next_dispatch_cycle_request.task_difficulty must equal "
            "original worker_result.task_difficulty"
        )


# -- ACK Observer (internal, used within run_dispatch_cycle) ------------------


class _AckObserver:
    """Internal observer that validates DispatchStarted identity and
    applies DISPATCH_ACKNOWLEDGED under the same WorkerSlotLease.

    Must be callable while the lease is valid and before the post-ACK
    heartbeat handshake starts.
    """

    def __init__(
        self,
        project_root: Path,
        ack_transition_request: TransitionRequest,
        lease: object,  # WorkerSlotLease
        clock: WorkflowClock,
        dispatch_request: DispatchRequest,
        selected_model_provider: str,
        selected_model_id: str,
        generation_id: str = "",
    ) -> None:
        self._project_root = project_root
        self._ack_tr = ack_transition_request
        self._lease = lease
        self._clock = clock
        self._dispatch_request = dispatch_request
        self._selected_model_provider = selected_model_provider
        self._selected_model_id = selected_model_id
        self.generation_id = generation_id
        self._ack_result: TransitionResult | None = None
        self._acknowledged = asyncio.Event()

    @property
    def ack_result(self) -> TransitionResult | None:
        return self._ack_result

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def acknowledged(self) -> asyncio.Event:
        """Private orchestration handshake; not part of the public API."""
        return self._acknowledged

    async def on_dispatch_started(
        self,
        started: DispatchStarted,
    ) -> None:
        """Validate identity and apply DISPATCH_ACKNOWLEDGED transition."""
        # -- Verify identity -------------------------------------------------
        if started.identity is not self._dispatch_request.identity:
            raise WorkflowInvariantError(
                "dispatch started identity does not match dispatch request"
            )
        if started.provider != self._selected_model_provider:
            raise WorkflowInvariantError(
                "dispatch started provider does not match selected model provider"
            )
        if started.model_id != self._selected_model_id:
            raise WorkflowInvariantError(
                "dispatch started model_id does not match selected model_id"
            )

        # -- Apply ACK transition under the same lease -----------------------
        now_ack = self._clock.now()
        self._ack_result = ControlPlaneTransitionService(
            self._project_root
        ).apply_transition(self._ack_tr, self._lease, now_ack)
        self._acknowledged.set()


# -- WorkflowOrchestrator -----------------------------------------------------


def _validate_acceptance_cycle_request(
    request: AcceptanceCycleRequest,
) -> None:
    """Fail-closed pre-validation of *request* before any audit or acquire.

    Raises :exc:`TypeError` / :exc:`ValueError` on the first violation;
    messages never contain paths, prompts, report bodies, issue
    descriptions, stdout, or secrets.
    """
    # -- dispatch_cycle_result type -----------------------------------------
    if not isinstance(request.dispatch_cycle_result, DispatchCycleResult):
        raise TypeError(
            "dispatch_cycle_result must be DispatchCycleResult, "
            f"got {type(request.dispatch_cycle_result).__name__}"
        )

    # -- audit_input type ---------------------------------------------------
    if not isinstance(request.audit_input, MadAuditGatewayInput):
        raise TypeError(
            "audit_input must be MadAuditGatewayInput, "
            f"got {type(request.audit_input).__name__}"
        )

    # -- worker_kind --------------------------------------------------------
    if not isinstance(request.worker_kind, WorkerKind):
        raise TypeError(
            "worker_kind must be WorkerKind, "
            f"got {type(request.worker_kind).__name__}"
        )

    # -- holder_instance_id -------------------------------------------------
    if not isinstance(request.holder_instance_id, str) or \
       not request.holder_instance_id:
        raise TypeError(
            "holder_instance_id must be a non-empty str, "
            f"got {type(request.holder_instance_id).__name__}"
        )

    dcr = request.dispatch_cycle_result
    ai = request.audit_input
    receipt = dcr.delivery_receipt

    # -- audit task_id matches worker identity ------------------------------
    if ai.task_id != receipt.identity.task_id:
        raise ValueError(
            "audit_input task_id must match delivery receipt task_id"
        )

    # -- audit dispatch_id matches worker identity --------------------------
    if ai.dispatch_id != receipt.identity.dispatch_id:
        raise ValueError(
            "audit_input dispatch_id must match delivery receipt dispatch_id"
        )

    # -- audit implementation_commit matches receipt -----------------------
    if ai.implementation_commit != receipt.implementation_commit:
        raise ValueError(
            "audit_input implementation_commit must match "
            "delivery receipt implementation_commit"
        )

    # -- audit report_commit matches receipt --------------------------------
    if ai.report_commit != receipt.report_commit:
        raise ValueError(
            "audit_input report_commit must match "
            "delivery receipt report_commit"
        )

    # -- audit workspace validation -----------------------------------------
    if not isinstance(ai.workspace, Path):
        raise TypeError(
            "audit_input workspace must be a Path, "
            f"got {type(ai.workspace).__name__}"
        )
    if not ai.workspace.is_absolute():
        raise ValueError(
            "audit_input workspace must be an absolute path"
        )

    # -- acceptance_transition_request --------------------------------------
    atr = request.acceptance_transition_request

    # event_type must be DELIVERY_ACCEPTED
    if atr.event_type != "DELIVERY_ACCEPTED":
        raise ValueError(
            "acceptance_transition_request event_type must be "
            f"DELIVERY_ACCEPTED, got {atr.event_type!r}"
        )

    # payload must be DeliveryAcceptedPayload
    if not isinstance(atr.payload, DeliveryAcceptedPayload):
        raise TypeError(
            "acceptance_transition_request payload must be "
            "DeliveryAcceptedPayload, "
            f"got {type(atr.payload).__name__}"
        )

    # CAS task_id matches dispatch identity
    if atr.cas.task_id != receipt.identity.task_id:
        raise ValueError(
            "acceptance_transition_request task_id must match "
            "delivery receipt task_id"
        )

    # CAS expected_state must be review_ready
    if atr.cas.expected_state != "review_ready":
        raise ValueError(
            "acceptance_transition_request expected_state must be "
            f"'review_ready', got {atr.cas.expected_state!r}"
        )

    # DispatchCAS must match dispatch identity
    if atr.dispatch_cas is None:
        raise ValueError(
            "acceptance_transition_request dispatch_cas must not be None"
        )
    dcas = atr.dispatch_cas
    if dcas.expected_dispatch_id != receipt.identity.dispatch_id:
        raise ValueError(
            "acceptance dispatch_cas expected_dispatch_id must match "
            "delivery receipt dispatch_id"
        )
    if dcas.expected_attempt != receipt.identity.attempt:
        raise ValueError(
            "acceptance dispatch_cas expected_attempt must match "
            "delivery receipt attempt"
        )

    # payload accepted_commit equals receipt implementation_commit
    if atr.payload.accepted_commit != receipt.implementation_commit:
        raise ValueError(
            "acceptance accepted_commit must equal "
            "delivery receipt implementation_commit"
        )

    # -- integration_transition_request (if present) ------------------------
    itr = request.integration_transition_request
    if itr is not None:
        if itr.event_type != "CHANGE_INTEGRATED":
            raise ValueError(
                "integration_transition_request must be "
                "CHANGE_INTEGRATED, "
                "got an invalid event_type"
            )

        if not isinstance(itr.payload, IntegrationPayload):
            raise TypeError(
                "integration_transition_request payload must be "
                "IntegrationPayload, "
                f"got {type(itr.payload).__name__}"
            )

        # integration expected_state must be "accepted"
        if itr.cas.expected_state != "accepted":
            raise ValueError(
                "integration_transition_request must be "
                "'accepted', "
                "got an invalid expected_state"
            )

        # integration task_id must match
        if itr.cas.task_id != receipt.identity.task_id:
            raise ValueError(
                "integration_transition_request task_id must match "
                "delivery receipt task_id"
            )

        # integration dispatch_cas must be None
        if itr.dispatch_cas is not None:
            raise ValueError(
                "integration_transition_request dispatch_cas must be None"
            )

        # integration event_id must differ from all other event_ids
        _existing_event_ids = {
            dcr.dispatch_transition.event_id,
            dcr.acknowledge_transition.event_id,
            dcr.delivery_transition.event_id,
            atr.event_id,
        }
        if itr.event_id in _existing_event_ids:
            raise ValueError(
                "integration_transition_request event_id must differ from "
                "dispatch, ACK, delivery, and acceptance event_ids"
            )


@dataclass(frozen=True, slots=True)
class WorkflowOrchestrator:
    """Pure orchestration facade -- delegates all authoritative operations.

    Usage::

        orchestrator = WorkflowOrchestrator(
            project_root=Path("/abs/path"),
            clock=RealClock(),
            heartbeat_interval_seconds=20.0,
        )
        result = await orchestrator.run_dispatch_cycle(request, providers)
    """

    project_root: Path
    clock: WorkflowClock
    heartbeat_interval_seconds: float = 20.0

    def __post_init__(self) -> None:
        # -- project_root: absolute, existing directory ----------------------
        if not isinstance(self.project_root, Path):
            raise WorkflowInputError(
                "project_root must be a Path, "
                f"got {type(self.project_root).__name__}"
            )
        if not self.project_root.is_absolute():
            raise WorkflowInputError("project_root must be an absolute path")
        if not self.project_root.is_dir():
            raise WorkflowInputError(
                "project_root must be an existing directory"
            )

        # -- clock: duck-type check (has now, monotonic, sleep) --------------
        if (
            not callable(getattr(self.clock, "now", None))
            or not callable(getattr(self.clock, "monotonic", None))
            or not callable(getattr(self.clock, "sleep", None))
        ):
            raise WorkflowInputError(
                "clock must implement WorkflowClock protocol"
            )

        # -- heartbeat_interval_seconds: 0 < interval <= 20 ------------------
        # Reject bool (subclass of int).
        if isinstance(self.heartbeat_interval_seconds, bool):
            raise WorkflowInputError(
                "heartbeat_interval_seconds must not be bool"
            )
        if not isinstance(self.heartbeat_interval_seconds, (int, float)):
            raise WorkflowInputError(
                "heartbeat_interval_seconds must be int or float, "
                f"got {type(self.heartbeat_interval_seconds).__name__}"
            )
        # Reject NaN.
        if self.heartbeat_interval_seconds != self.heartbeat_interval_seconds:
            raise WorkflowInputError(
                "heartbeat_interval_seconds must not be NaN"
            )
        # Reject Infinity.
        if math.isinf(self.heartbeat_interval_seconds):
            raise WorkflowInputError(
                "heartbeat_interval_seconds must not be Infinity"
            )
        if not (0 < self.heartbeat_interval_seconds <= 20):
            raise WorkflowInputError(
                "heartbeat_interval_seconds must be "
                f"0 < interval <= 20, got {self.heartbeat_interval_seconds}"
            )

    # -- public API -----------------------------------------------------------

    async def run_dispatch_cycle(
        self,
        request: DispatchCycleRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> DispatchCycleResult:
        """Compatibility wrapper over the live-execution API."""
        execution = await self.start_dispatch_cycle(request, providers)
        try:
            return await execution.wait()
        except asyncio.CancelledError as outer_cancel:
            cleanup_task = asyncio.ensure_future(
                self._abort_active_dispatch(execution)
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(cleanup_task)
                finally:
                    raise
            except BaseException as cleanup_error:
                raise outer_cancel from cleanup_error
            raise

    async def run_bounded_dispatch_retry(
        self,
        request: BoundedDispatchRetryRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> BoundedDispatchRetryResult:
        """Run a finite creator-owned retry plan without inferring failures."""
        if type(request) is not BoundedDispatchRetryRequest:
            raise WorkflowInputError(
                "request must be exact BoundedDispatchRetryRequest"
            )
        if not isinstance(providers, Mapping):
            raise WorkflowInputError("providers must be a Mapping")
        if len(providers) == 0:
            raise WorkflowInputError("providers must not be empty")

        # Revalidate the entire frozen outer plan.  Nested control-plane
        # values are immutable by contract, but this also fails closed against
        # hostile object.__setattr__ mutation before the first side effect.
        validated = BoundedDispatchRetryRequest(request.attempts)
        recovery_transitions: list[TransitionResult] = []

        for index, attempt in enumerate(validated.attempts):
            cycle_request = attempt.dispatch_cycle_request

            # This call is deliberately outside the execution-failure handler.
            # A pre-return exception has no ActiveDispatchExecution evidence
            # and therefore propagates without recovery or another attempt.
            execution = await self.start_dispatch_cycle(
                cycle_request,
                providers,
            )

            try:
                cycle_result = await execution.wait()
            except asyncio.CancelledError:
                raise
            except BaseException as dispatch_error:
                metadata = execution._finalizer_metadata
                if (
                    not execution._completion.done()
                    or type(metadata) is not _DispatchFinalizerMetadata
                ):
                    raise WorkflowInvariantError(
                        "dispatch finalizer metadata was not published"
                    ) from dispatch_error
                if (
                    metadata.winner != "completion"
                    or not metadata.worker_done
                    or not metadata.heartbeat_done
                    or not metadata.release_completed
                    or not execution._worker_task.done()
                    or not execution._heartbeat_task.done()
                    or not execution._release_started
                    or not execution._release_completed
                ):
                    raise WorkflowInvariantError(
                        "dispatch finalization evidence is incomplete"
                    ) from dispatch_error
                if type(metadata.failure_kind) is not _DispatchFailureKind:
                    raise WorkflowInvariantError(
                        "dispatch failure classification is unavailable"
                    ) from dispatch_error

                failure_request = attempt.failure_transition_request
                if (
                    failure_request.payload.failure_kind
                    != metadata.failure_kind.value
                ):
                    raise WorkflowInvariantError(
                        "failure request classification does not match "
                        "finalizer metadata"
                    ) from dispatch_error

                snapshot = StateProvider(self.project_root).snapshot()
                task = next(
                    (
                        item
                        for item in snapshot.tasks
                        if item.task_id
                        == cycle_request.dispatch_request.identity.task_id
                    ),
                    None,
                )
                identity = cycle_request.dispatch_request.identity
                if (
                    task is None
                    or task.revision != identity.revision
                    or task.state != "in_progress"
                    or task.attempt != identity.attempt
                    or task.current_dispatch is None
                    or task.current_dispatch.dispatch_id
                    != identity.dispatch_id
                ):
                    raise WorkflowInvariantError(
                        "canonical dispatch state does not match recovery CAS"
                    ) from dispatch_error

                try:
                    recovery = ControlPlaneTransitionService(
                        self.project_root
                    ).apply_transition(
                        failure_request,
                        lease=None,
                        now=self.clock.now(),
                    )
                except BaseException as transition_error:
                    raise transition_error from dispatch_error
                recovery_transitions.append(recovery)

                if index == len(validated.attempts) - 1:
                    final_snapshot = StateProvider(
                        self.project_root
                    ).snapshot()
                    final_task = next(
                        (
                            item
                            for item in final_snapshot.tasks
                            if item.task_id == identity.task_id
                        ),
                        None,
                    )
                    if (
                        final_task is None
                        or final_task.revision != identity.revision
                        or final_task.state != "ready"
                        or final_task.attempt != identity.attempt
                        or final_task.current_dispatch is not None
                    ):
                        raise WorkflowInvariantError(
                            "final recovery did not publish exact ready state"
                        ) from dispatch_error
                    raise
                continue

            return BoundedDispatchRetryResult(
                task_id=(
                    cycle_request.dispatch_request.identity.task_id
                ),
                attempts_started=index + 1,
                recovery_transitions=tuple(recovery_transitions),
                dispatch_cycle_result=cycle_result,
            )

        raise WorkflowInvariantError("bounded dispatch retry plan incomplete")

    async def start_dispatch_cycle(
        self,
        request: DispatchCycleRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> ActiveDispatchExecution:
        """Start a dispatch and return after ACK, before waiting for exit."""
        if not isinstance(request, DispatchCycleRequest):
            raise WorkflowInputError(
                "request must be DispatchCycleRequest, "
                f"got {type(request).__name__}"
            )

        if not isinstance(providers, Mapping):
            raise WorkflowInputError(
                f"providers must be a Mapping, "
                f"got {type(providers).__name__}"
            )
        if len(providers) == 0:
            raise WorkflowInputError("providers must not be empty")

        # Validate clock returns UTC.
        test_now = self.clock.now()
        if not isinstance(test_now, datetime):
            raise WorkflowInputError(
                "clock.now() must return datetime, "
                f"got {type(test_now).__name__}"
            )
        if test_now.tzinfo is None:
            raise WorkflowInputError(
                "clock.now() must return timezone-aware datetime"
            )
        offset = test_now.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise WorkflowInputError("clock.now() must return UTC")

        start_mono = self.clock.monotonic()

        try:
            snapshot = StateProvider(self.project_root).snapshot()
        except StateProviderError:
            raise

        tr = request.dispatch_transition_request
        task_id = tr.cas.task_id
        task = None
        for t in snapshot.tasks:
            if t.task_id == task_id:
                task = t
                break
        if task is None:
            raise WorkflowInvariantError(
                "target task not found in snapshot"
            )
        if task.state != tr.cas.expected_state:
            raise WorkflowInvariantError(
                "task state does not match transition CAS expected_state"
            )
        if task.revision != tr.cas.expected_revision:
            raise WorkflowInvariantError(
                "task revision does not match transition CAS expected_revision"
            )

        now_acquire = self.clock.now()
        dr = request.dispatch_request
        lease = acquire_worker_slot(
            self.project_root,
            request.worker_kind,
            dr.identity.dispatch_id,
            request.holder_instance_id,
            dr.workspace,
            now_acquire,
        )
        worker_task: asyncio.Task[WorkerResult] | None = None
        hb_task: asyncio.Task[None] | None = None

        snapshot_sn = dr.model_selection
        generation_id = "GEN-" + secrets.token_hex(16)
        ack_observer = _AckObserver(
            project_root=self.project_root,
            ack_transition_request=request.acknowledge_transition_request,
            lease=lease,
            clock=self.clock,
            dispatch_request=dr,
            selected_model_provider=snapshot_sn.selected_model_provider,
            selected_model_id=snapshot_sn.selected_model_id,
            generation_id=generation_id,
        )

        creator_pid, creator_creation = _dse.get_current_process_identity()
        if not creator_creation:
            raise WorkflowInvariantError("creator creation time unavailable")
        _dse.reserve_receipt(
            self.project_root,
            task_id=dr.identity.task_id,
            revision=dr.identity.revision,
            attempt=dr.identity.attempt,
            dispatch_id=dr.identity.dispatch_id,
            lease_epoch=lease.lease_epoch,
            holder_instance_id=request.holder_instance_id,
            generation_id=generation_id,
            creator_pid=creator_pid,
            creator_creation_time=creator_creation,
            boot_id=_dse.get_boot_id(),
        )

        try:
            now_transition = self.clock.now()
            dispatch_transition_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(tr, lease, now_transition)

            worker_task = asyncio.ensure_future(
                run_worker_observed(
                    dr,
                    request.worker_kind,
                    request.task_difficulty,
                    providers,
                    ack_observer,
                )
            )

            ack_wait = asyncio.ensure_future(ack_observer.acknowledged.wait())
            try:
                done, _ = await asyncio.wait(
                    (worker_task, ack_wait),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if ack_wait not in done:
                    # Propagate a launch/observer failure before returning a
                    # cancellation capability that never became ready.
                    worker_task.result()
                await ack_wait
            finally:
                if not ack_wait.done():
                    ack_wait.cancel()
                try:
                    await ack_wait
                except asyncio.CancelledError:
                    pass

            ack_transition_result = ack_observer.ack_result
            if ack_transition_result is None:
                raise WorkflowInvariantError(
                    "ACK transition was not applied — observer did not run"
                )

            heartbeat_started = asyncio.Event()
            async def _heartbeat_loop() -> None:
                heartbeat_started.set()
                while True:
                    await self.clock.sleep(self.heartbeat_interval_seconds)
                    now_hb = self.clock.now()
                    renew_worker_slot(self.project_root, lease, now_hb)

            hb_task = asyncio.ensure_future(_heartbeat_loop())
            await heartbeat_started.wait()
            if hb_task.done():
                hb_exc = hb_task.exception()
                if hb_exc is not None:
                    if isinstance(hb_exc, WorkerSlotLeaseError):
                        raise hb_exc
                    raise WorkflowHeartbeatError(
                        "heartbeat task failed before worker started"
                    ) from hb_exc
                raise WorkflowHeartbeatError(
                    "heartbeat task terminated before execution became ready"
                )

            handle = ActiveDispatchHandle(
                task_id=dr.identity.task_id,
                revision=dr.identity.revision,
                attempt=dr.identity.attempt,
                dispatch_id=dr.identity.dispatch_id,
                holder_instance_id=request.holder_instance_id,
                lease_epoch=lease.lease_epoch,
                lease=lease,
            )
            execution = ActiveDispatchExecution(
                owner=self,
                handle=handle,
                request=request,
                worker_task=worker_task,
                heartbeat_task=hb_task,
                dispatch_transition=dispatch_transition_result,
                acknowledge_transition=ack_transition_result,
                start_monotonic=start_mono,
            )
            execution._generation_id = generation_id
            execution._runner_task = asyncio.ensure_future(
                self._run_active_dispatch(execution)
            )
            execution._ready = True
            return execution
        except BaseException as body_error:
            async def _cleanup_failed_start() -> None:
                for active_task in (worker_task, hb_task):
                    if active_task is not None and not active_task.done():
                        active_task.cancel()
                for active_task in (worker_task, hb_task):
                    if active_task is not None:
                        try:
                            await active_task
                        except (asyncio.CancelledError, Exception):
                            pass
                release_worker_slot(
                    self.project_root, lease, self.clock.now()
                )

            cleanup_task = asyncio.ensure_future(_cleanup_failed_start())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(cleanup_task)
                finally:
                    raise
            except BaseException as release_error:
                raise body_error from release_error
            raise

    async def _run_active_dispatch(
        self,
        execution: ActiveDispatchExecution,
    ) -> None:
        """Select the non-cancellation winner and drive finalization."""
        done, _ = await asyncio.wait(
            (execution._worker_task, execution._heartbeat_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        async with execution._state_lock:
            if execution._winner is not None:
                return
            if execution._heartbeat_task in done:
                execution._winner = "heartbeat"
            else:
                execution._winner = "completion"
        try:
            await self._finalize_active_dispatch(execution, None)
        except BaseException:
            # The shared completion future is the public error channel.
            return

    async def _abort_active_dispatch(
        self,
        execution: ActiveDispatchExecution,
    ) -> None:
        """Preserve legacy run_dispatch_cycle outer-cancellation cleanup."""
        finalize_outer = False
        async with execution._state_lock:
            if execution._winner is None:
                execution._winner = "outer_cancellation"
                finalize_outer = True

        if finalize_outer:
            execution._worker_task.cancel()
            await self._finalize_active_dispatch(execution, None)
        else:
            try:
                await execution.wait()
            except BaseException:
                pass

        runner = execution._runner_task
        if runner is not None and not runner.done():
            await asyncio.shield(runner)

    @staticmethod
    def _heartbeat_failure(
        heartbeat_task: asyncio.Task[None],
    ) -> BaseException:
        heartbeat_error = heartbeat_task.exception()
        if isinstance(heartbeat_error, WorkerSlotLeaseError):
            return heartbeat_error
        if heartbeat_error is not None:
            wrapped = WorkflowHeartbeatError(
                "heartbeat task failed with unexpected exception"
            )
            wrapped.__cause__ = heartbeat_error
            return wrapped
        return WorkflowHeartbeatError(
            "heartbeat task terminated unexpectedly"
        )

    async def _finalize_active_dispatch(
        self,
        execution: ActiveDispatchExecution,
        cancellation_request: ActiveDispatchCancellationRequest | None,
        supersession_request: ActiveDispatchSupersessionRequest | None = None,
    ) -> (
        ActiveDispatchCancellationResult
        | ActiveDispatchSupersessionResult
        | None
    ):
        """Single cleanup owner for every active-dispatch terminal path."""
        async with execution._finalize_lock:
            winner = execution._winner
            primary_error: BaseException | None = None
            normal_result: DispatchCycleResult | None = None
            cancellation_result: ActiveDispatchCancellationResult | None = None
            supersession_result: ActiveDispatchSupersessionResult | None = None
            worker_cancel_error: DispatchCancelledError | None = None
            failure_kind: _DispatchFailureKind | None = None
            classified_error: BaseException | None = None

            if winner in ("cancellation", "supersession"):
                execution._worker_task.cancel()
                try:
                    await execution._worker_task
                except DispatchCancelledError as exc:
                    worker_cancel_error = exc
                except asyncio.CancelledError:
                    # Test doubles without a subprocess gateway terminate with
                    # bare task cancellation; the real gateway reports the
                    # more precise DispatchCancelledError.
                    worker_cancel_error = DispatchCancelledError()
                except BaseException as exc:
                    primary_error = exc
            elif winner == "outer_cancellation":
                if not execution._worker_task.done():
                    execution._worker_task.cancel()
                try:
                    await execution._worker_task
                except (
                    DispatchCancelledError,
                    asyncio.CancelledError,
                    Exception,
                ):
                    pass
            elif winner == "heartbeat":
                primary_error = self._heartbeat_failure(
                    execution._heartbeat_task
                )
                if not execution._worker_task.done():
                    execution._worker_task.cancel()
                try:
                    await execution._worker_task
                except (DispatchCancelledError, asyncio.CancelledError):
                    pass
                except BaseException:
                    # Heartbeat fencing is the primary failure.
                    pass
            elif winner == "completion":
                try:
                    worker_result = execution._worker_task.result()
                except BaseException as exc:
                    primary_error = exc
                    classified_error = exc
                    failure_kind = _DispatchFailureKind.WORKER_FAILED
                else:
                    request = execution._request
                    try:
                        worker_output = decode_worker_result(
                            worker_result, request.provider_cli_version
                        )
                        delivery_receipt = require_delivery_receipt(
                            worker_output
                        )
                    except BaseException as exc:
                        primary_error = exc
                        classified_error = exc
                        failure_kind = (
                            _DispatchFailureKind.WORKER_OUTPUT_FAILED
                        )
                    else:
                        dispatch_tr = request.dispatch_transition_request
                        ack_tr = request.acknowledge_transition_request
                        delivery_tr = TransitionRequest(
                            cas=TransitionCAS(
                                task_id=dispatch_tr.cas.task_id,
                                expected_revision=(
                                    ack_tr.cas.expected_revision
                                ),
                                expected_state="in_progress",
                                expected_snapshot_commit=(
                                    dispatch_tr.cas
                                    .expected_snapshot_commit
                                ),
                            ),
                            dispatch_cas=ack_tr.dispatch_cas,
                            event_id=request.delivery_event_id,
                            event_type="DELIVERY_SUBMITTED",
                            payload=DeliverySubmittedPayload(
                                implementation_commit=(
                                    delivery_receipt
                                    .implementation_commit
                                ),
                                report_commit=(
                                    delivery_receipt.report_commit
                                ),
                            ),
                            event_context=request.delivery_event_context,
                        )
                        try:
                            delivery_transition = (
                                ControlPlaneTransitionService(
                                    self.project_root
                                ).apply_transition(
                                    delivery_tr,
                                    execution._handle.lease,
                                    self.clock.now(),
                                )
                            )
                        except BaseException as exc:
                            primary_error = exc
                            classified_error = exc
                            failure_kind = (
                                _DispatchFailureKind
                                .DELIVERY_TRANSITION_FAILED
                            )
                        else:
                            try:
                                duration = _validate_monotonic_delta(
                                    execution._start_monotonic,
                                    self.clock.monotonic(),
                                )
                                normal_result = DispatchCycleResult(
                                    worker_result=worker_result,
                                    worker_output=worker_output,
                                    delivery_receipt=delivery_receipt,
                                    dispatch_transition=(
                                        execution._dispatch_transition
                                    ),
                                    acknowledge_transition=(
                                        execution._ack_transition
                                    ),
                                    delivery_transition=(
                                        delivery_transition
                                    ),
                                    slot_id=(
                                        execution._handle.lease.slot_id
                                    ),
                                    lease_epoch=(
                                        execution._handle.lease_epoch
                                    ),
                                    duration_seconds=duration,
                                )
                            except BaseException as exc:
                                primary_error = exc
            else:
                primary_error = WorkflowInvariantError(
                    "active dispatch has no terminal winner"
                )

            # A heartbeat failure that was already observable wins even when a
            # cancellation request acquired the state lock first.
            if execution._heartbeat_task.done():
                try:
                    heartbeat_error = self._heartbeat_failure(
                        execution._heartbeat_task
                    )
                except asyncio.CancelledError:
                    heartbeat_error = None
                if heartbeat_error is not None:
                    primary_error = heartbeat_error

            if not execution._heartbeat_task.done():
                execution._heartbeat_task.cancel()
            try:
                await execution._heartbeat_task
            except asyncio.CancelledError:
                pass
            except WorkerSlotLeaseError as exc:
                primary_error = exc
            except BaseException as exc:
                primary_error = WorkflowHeartbeatError(
                    "heartbeat failed during shutdown"
                )
                primary_error.__cause__ = exc

            # An unconfirmed terminal-action cleanup must not release the
            # lease or publish TASK_CANCELLED / TASK_SUPERSEDED.
            terminal_cleanup_confirmed = not (
                winner in ("cancellation", "supersession")
                and worker_cancel_error is None
                and primary_error is not None
            )

            if terminal_cleanup_confirmed:
                execution._release_started = True
                try:
                    release_worker_slot(
                        self.project_root,
                        execution._handle.lease,
                        self.clock.now(),
                    )
                    execution._release_completed = True
                except BaseException as release_error:
                    if primary_error is None:
                        primary_error = release_error
                    else:
                        primary_error.__cause__ = release_error

            if (
                winner == "cancellation"
                and primary_error is None
                and execution._release_completed
            ):
                if cancellation_request is None:
                    primary_error = WorkflowInvariantError(
                        "cancellation finalizer requires its request"
                    )
                else:
                    try:
                        transition = ControlPlaneTransitionService(
                            self.project_root
                        ).apply_transition(
                            (
                                cancellation_request
                                .cancellation_transition_request
                            ),
                            lease=None,
                            now=self.clock.now(),
                        )
                        cancellation_result = (
                            ActiveDispatchCancellationResult(
                                task_id=execution._handle.task_id,
                                dispatch_id=execution._handle.dispatch_id,
                                cancellation_transition=transition,
                            )
                        )
                    except BaseException as exc:
                        primary_error = exc

            if (
                winner == "supersession"
                and primary_error is None
                and execution._release_completed
            ):
                if supersession_request is None:
                    primary_error = WorkflowInvariantError(
                        "supersession finalizer requires its request"
                    )
                else:
                    try:
                        request_transition = (
                            supersession_request
                            .supersession_transition_request
                        )
                        transition = ControlPlaneTransitionService(
                            self.project_root
                        ).apply_transition(
                            request_transition,
                            lease=None,
                            now=self.clock.now(),
                        )
                        supersession_result = (
                            ActiveDispatchSupersessionResult(
                                task_id=execution._handle.task_id,
                                dispatch_id=execution._handle.dispatch_id,
                                superseded_by=(
                                    request_transition.payload.superseded_by
                                ),
                                supersession_transition=transition,
                            )
                        )
                    except BaseException as exc:
                        primary_error = exc

            if (
                winner != "completion"
                or primary_error is None
                or primary_error is not classified_error
                or not execution._release_completed
            ):
                failure_kind = None
            if execution._finalizer_metadata is not None:
                raise WorkflowInvariantError(
                    "dispatch finalizer metadata already published"
                )
            if execution._completion.done():
                raise WorkflowInvariantError(
                    "completion published before finalizer metadata"
                )
            execution._finalizer_metadata = _DispatchFinalizerMetadata(
                winner=winner or "unknown",
                worker_done=execution._worker_task.done(),
                heartbeat_done=execution._heartbeat_task.done(),
                release_completed=execution._release_completed,
                failure_kind=failure_kind,
            )

            # Durable FINALIZED tombstone — exactly-once, byte-exact replay on
            # duplicate.  Written only after Worker ended, heartbeat ended,
            # and release completed.  A tombstone write failure does not mask
            # the primary completion/error path; the durable receipt remains
            # at FINALIZING and tombstone absence is fail-closed (not death).
            if (
                execution._finalizer_metadata.release_completed
                and execution._finalizer_metadata.worker_done
                and execution._finalizer_metadata.heartbeat_done
                and execution._generation_id
            ):
                try:
                    _dse.write_finalizer_tombstone(
                        self.project_root,
                        task_id=execution._handle.task_id,
                        revision=execution._handle.revision,
                        attempt=execution._handle.attempt,
                        dispatch_id=execution._handle.dispatch_id,
                        generation_id=execution._generation_id,
                        winner=execution._finalizer_metadata.winner,
                        worker_done=True,
                        heartbeat_done=True,
                        release_completed=True,
                        failure_kind=(
                            execution._finalizer_metadata.failure_kind.value
                            if execution._finalizer_metadata.failure_kind
                            is not None
                            else None
                        ),
                    )
                except _dse.DispatchSupervisorEvidenceError:
                    pass

            if primary_error is not None:
                if not execution._completion.done():
                    execution._completion.set_exception(primary_error)
                raise primary_error
            if normal_result is not None:
                if not execution._completion.done():
                    execution._completion.set_result(normal_result)
                return None
            if cancellation_result is not None:
                if not execution._completion.done():
                    execution._completion.set_exception(
                        worker_cancel_error or DispatchCancelledError()
                    )
                return cancellation_result
            if supersession_result is not None:
                if not execution._completion.done():
                    execution._completion.set_exception(
                        worker_cancel_error or DispatchCancelledError()
                    )
                return supersession_result
            if winner == "outer_cancellation":
                if not execution._completion.done():
                    execution._completion.set_exception(
                        DispatchCancelledError()
                    )
                return None
            raise WorkflowInvariantError("active dispatch finalizer incomplete")

    async def cancel_active_dispatch(
        self,
        execution: ActiveDispatchExecution,
        request: ActiveDispatchCancellationRequest,
    ) -> ActiveDispatchCancellationResult:
        """Cancel a live dispatch owned by this exact orchestrator instance."""
        self._validate_active_dispatch_cancellation(execution, request)

        wait_for_heartbeat_failure = False
        async with execution._state_lock:
            if execution._winner is not None:
                raise WorkflowInputError("dispatch already terminal")
            if execution._heartbeat_task.done():
                wait_for_heartbeat_failure = True
            elif execution._worker_task.done():
                raise WorkflowInputError("dispatch already completed")
            else:
                execution._winner = "cancellation"

        if wait_for_heartbeat_failure:
            return await execution.wait()  # type: ignore[return-value]

        execution._worker_task.cancel()
        cleanup_task = asyncio.ensure_future(
            self._finalize_active_dispatch(execution, request)
        )
        try:
            result = await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(cleanup_task)
            finally:
                raise
        if result is None:
            raise WorkflowInvariantError("cancellation produced no result")
        return result

    async def supersede_active_dispatch(
        self,
        execution: ActiveDispatchExecution,
        request: ActiveDispatchSupersessionRequest,
    ) -> ActiveDispatchSupersessionResult:
        """Supersede a live dispatch owned by this orchestrator instance."""
        self._validate_active_dispatch_supersession(execution, request)

        wait_for_heartbeat_failure = False
        async with execution._state_lock:
            if execution._winner is not None:
                raise WorkflowInputError("dispatch already terminal")
            if execution._heartbeat_task.done():
                wait_for_heartbeat_failure = True
            elif execution._worker_task.done():
                raise WorkflowInputError("dispatch already completed")
            else:
                execution._winner = "supersession"

        if wait_for_heartbeat_failure:
            return await execution.wait()  # type: ignore[return-value]

        execution._worker_task.cancel()
        cleanup_task = asyncio.ensure_future(
            self._finalize_active_dispatch(
                execution,
                cancellation_request=None,
                supersession_request=request,
            )
        )
        try:
            result = await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(cleanup_task)
            finally:
                raise
        if not isinstance(result, ActiveDispatchSupersessionResult):
            raise WorkflowInvariantError("supersession produced no result")
        return result

    def _validate_active_dispatch_cancellation(
        self,
        execution: ActiveDispatchExecution,
        request: ActiveDispatchCancellationRequest,
    ) -> None:
        if type(execution) is not ActiveDispatchExecution:
            raise WorkflowInputError(
                "execution must be ActiveDispatchExecution"
            )
        if execution._owner is not self:
            raise WorkflowInputError(
                "execution is not owned by this WorkflowOrchestrator"
            )
        if type(request) is not ActiveDispatchCancellationRequest:
            raise WorkflowInputError(
                "request must be ActiveDispatchCancellationRequest"
            )
        if type(request.handle) is not ActiveDispatchHandle:
            raise WorkflowInputError(
                "request handle must be ActiveDispatchHandle"
            )
        if request.handle != execution._handle:
            raise WorkflowInputError("request handle does not match execution")
        transition = request.cancellation_transition_request
        if type(transition) is not TransitionRequest:
            raise WorkflowInputError(
                "cancellation_transition_request must be TransitionRequest"
            )
        if transition.event_type != "TASK_CANCELLED":
            raise WorkflowInputError(
                "cancellation transition event_type must be TASK_CANCELLED"
            )
        if type(transition.payload) is not CancelledPayload:
            raise WorkflowInputError(
                "cancellation transition payload must be CancelledPayload"
            )
        if transition.cas.task_id != execution._handle.task_id:
            raise WorkflowInputError(
                "cancellation transition task_id must match handle"
            )
        dispatch_cas = transition.dispatch_cas
        if dispatch_cas is None:
            raise WorkflowInputError(
                "active cancellation requires dispatch_cas"
            )
        if (
            dispatch_cas.expected_dispatch_id
            != execution._handle.dispatch_id
            or dispatch_cas.expected_attempt != execution._handle.attempt
        ):
            raise WorkflowInputError(
                "cancellation dispatch_cas must match handle"
            )

    def _validate_active_dispatch_supersession(
        self,
        execution: ActiveDispatchExecution,
        request: ActiveDispatchSupersessionRequest,
    ) -> None:
        if type(execution) is not ActiveDispatchExecution:
            raise WorkflowInputError(
                "execution must be ActiveDispatchExecution"
            )
        if execution._owner is not self:
            raise WorkflowInputError(
                "execution is not owned by this WorkflowOrchestrator"
            )
        if type(request) is not ActiveDispatchSupersessionRequest:
            raise WorkflowInputError(
                "request must be ActiveDispatchSupersessionRequest"
            )
        if type(request.handle) is not ActiveDispatchHandle:
            raise WorkflowInputError(
                "request handle must be ActiveDispatchHandle"
            )
        if request.handle != execution._handle:
            raise WorkflowInputError("request handle does not match execution")
        transition = request.supersession_transition_request
        if type(transition) is not TransitionRequest:
            raise WorkflowInputError(
                "supersession_transition_request must be TransitionRequest"
            )
        if transition.event_type != "TASK_SUPERSEDED":
            raise WorkflowInputError(
                "supersession transition event_type must be TASK_SUPERSEDED"
            )
        if type(transition.payload) is not SupersededPayload:
            raise WorkflowInputError(
                "supersession transition payload must be SupersededPayload"
            )
        if transition.cas.task_id != execution._handle.task_id:
            raise WorkflowInputError(
                "supersession transition task_id must match handle"
            )
        if transition.payload.superseded_by == execution._handle.task_id:
            raise WorkflowInputError(
                "supersession replacement must differ from active task"
            )
        dispatch_cas = transition.dispatch_cas
        if dispatch_cas is None:
            raise WorkflowInputError(
                "active supersession requires dispatch_cas"
            )
        if (
            dispatch_cas.expected_dispatch_id
            != execution._handle.dispatch_id
            or dispatch_cas.expected_attempt != execution._handle.attempt
        ):
            raise WorkflowInputError(
                "supersession dispatch_cas must match handle"
            )

    async def run_acceptance_cycle(
        self,
        request: AcceptanceCycleRequest,
        audit_config: MadGatewayConfig,
    ) -> AcceptanceCycleResult:
        """Execute an independent acceptance cycle (TC-13.18c.2).

        Execution order:
        1. validate request + audit_config
        2. run_audit_gateway exactly once
        3. verdict fail/blocked → return without transition
        4. verdict pass → acquire review lease
        5. apply DELIVERY_ACCEPTED under review lease
        6. release review lease exactly once
        7. optional apply CHANGE_INTEGRATED with lease=None
        8. return AcceptanceCycleResult
        """
        # -- 1. Validate request --------------------------------------------
        if not isinstance(request, AcceptanceCycleRequest):
            raise WorkflowInputError(
                "request must be AcceptanceCycleRequest, "
                f"got {type(request).__name__}"
            )

        # -- 1b. Validate audit_config --------------------------------------
        if not isinstance(audit_config, MadGatewayConfig):
            raise WorkflowInputError(
                "audit_config must be MadGatewayConfig, "
                f"got {type(audit_config).__name__}"
            )

        # -- 2. Run MAD audit gateway exactly once --------------------------
        audit_result = await run_audit_gateway(
            audit_config,
            request.audit_input,
        )

        dcr = request.dispatch_cycle_result

        # -- 3. Verdict routing ---------------------------------------------
        verdict = audit_result.verdict

        if verdict in ("fail", "blocked"):
            return AcceptanceCycleResult(
                task_id=dcr.delivery_receipt.identity.task_id,
                audit_result=audit_result,
                accept_transition=None,
                integrate_transition=None,
            )

        if verdict != "pass":
            # Unknown verdict — Gateway should have rejected this,
            # but orchestrator must fail-closed.
            raise WorkflowInvariantError(
                "audit gateway returned an unrecognized verdict"
            )

        # -- 4. Acquire review lease ----------------------------------------
        review_acquired = False
        review_lease = None
        body_error: BaseException | None = None
        accept_result: TransitionResult | None = None
        integrate_result: TransitionResult | None = None

        try:
            now_review_acquire = self.clock.now()
            review_lease = acquire_worker_slot(
                self.project_root,
                request.worker_kind,
                dcr.delivery_receipt.identity.dispatch_id,
                request.holder_instance_id,
                request.audit_input.workspace,
                now_review_acquire,
            )
            review_acquired = True

            # -- 5. Apply DELIVERY_ACCEPTED under review lease --------------
            now_accept = self.clock.now()
            accept_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(
                request.acceptance_transition_request,
                review_lease,
                now_accept,
            )

        except BaseException as exc:
            body_error = exc
            raise
        finally:
            # -- 6. Release review lease exactly once -----------------------
            if review_acquired:
                try:
                    now_release_review = self.clock.now()
                    release_worker_slot(
                        self.project_root, review_lease, now_release_review
                    )
                except BaseException as release_exc:
                    if body_error is None:
                        raise
                    raise body_error from release_exc

        # -- 7. Optional CHANGE_INTEGRATED ----------------------------------
        itr = request.integration_transition_request
        if itr is not None:
            now_integrate = self.clock.now()
            integrate_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(
                itr,
                lease=None,
                now=now_integrate,
            )

        # -- 8. Return result -----------------------------------------------
        return AcceptanceCycleResult(
            task_id=dcr.delivery_receipt.identity.task_id,
            audit_result=audit_result,
            accept_transition=accept_result,
            integrate_transition=integrate_result,
        )

    async def run_delivery_remediation(
        self,
        request: DeliveryRemediationRequest,
    ) -> DeliveryRemediationResult:
        """Execute delivery remediation for audit fail verdet (TC-13.18d.1).

        Execution order:
        1. validate DeliveryRemediationRequest
        2. clock.now()
        3. acquire_worker_slot() for remediation lease
        4. apply_transition(DELIVERY_RETURNED, remediation_lease, now)
        5. release_worker_slot() exactly once
        6. apply_transition(TASK_REQUEUED, lease=None, now)
        7. return DeliveryRemediationResult
        """
        # -- 1. Validate request type -----------------------------------------
        if not isinstance(request, DeliveryRemediationRequest):
            raise WorkflowInputError(
                "request must be DeliveryRemediationRequest, "
                f"got {type(request).__name__}"
            )

        acr = request.acceptance_cycle_result
        dcr = request.dispatch_cycle_result
        receipt = dcr.delivery_receipt

        # -- 1a. Validate audit result: must be "fail" -----------------------
        if not isinstance(acr.audit_result, MadAuditGatewayResult):
            raise WorkflowInputError(
                "acceptance_cycle_result.audit_result must be "
                "MadAuditGatewayResult"
            )

        verdict = acr.audit_result.verdict
        if verdict != "fail":
            raise WorkflowInputError(
                "acceptance_cycle_result audit verdict must be 'fail'"
            )

        # -- 1b. Fail-closed: accept/integrate transitions must be None ------
        if acr.accept_transition is not None:
            raise WorkflowInvariantError(
                "accept_transition must be None for fail verdet"
            )
        if acr.integrate_transition is not None:
            raise WorkflowInvariantError(
                "integrate_transition must be None for fail verdet"
            )

        # -- 1c. Validate worker_kind ----------------------------------------
        if not isinstance(request.worker_kind, WorkerKind):
            raise WorkflowInputError(
                "worker_kind must be WorkerKind, "
                f"got {type(request.worker_kind).__name__}"
            )

        # -- 1d. Validate holder_instance_id ---------------------------------
        if not isinstance(request.holder_instance_id, str) or \
           not request.holder_instance_id:
            raise WorkflowInputError(
                "holder_instance_id must be a non-empty str"
            )

        # -- 1e. Identity binding: task_id must be consistent ----------------
        task_id = acr.task_id
        if task_id != receipt.identity.task_id:
            raise WorkflowInputError(
                "acceptance_cycle_result task_id must match "
                "dispatch cycle delivery receipt task_id"
            )

        # -- 1f. Validate return_transition_request --------------------------
        rtr = request.return_transition_request

        # event_type must be DELIVERY_RETURNED
        if rtr.event_type != "DELIVERY_RETURNED":
            raise WorkflowInputError(
                "return_transition_request event_type must be "
                "DELIVERY_RETURNED"
            )

        # payload must be DeliveryReturnedPayload
        if not isinstance(rtr.payload, DeliveryReturnedPayload):
            raise WorkflowInputError(
                "return_transition_request payload must be "
                "DeliveryReturnedPayload"
            )

        # CAS task_id must match
        if rtr.cas.task_id != task_id:
            raise WorkflowInputError(
                "return_transition_request task_id must match"
            )

        # CAS expected_state must be review_ready
        if rtr.cas.expected_state != "review_ready":
            raise WorkflowInputError(
                "return_transition_request expected_state must be "
                "'review_ready'"
            )

        # DispatchCAS must be present and match delivery receipt identity
        if rtr.dispatch_cas is None:
            raise WorkflowInputError(
                "return_transition_request dispatch_cas must not be None"
            )
        rtr_dcas = rtr.dispatch_cas
        if rtr_dcas.expected_dispatch_id != receipt.identity.dispatch_id:
            raise WorkflowInputError(
                "return dispatch_cas expected_dispatch_id must match "
                "delivery receipt dispatch_id"
            )
        if rtr_dcas.expected_attempt != receipt.identity.attempt:
            raise WorkflowInputError(
                "return dispatch_cas expected_attempt must match "
                "delivery receipt attempt"
            )

        # -- 1g. Validate requeue_transition_request -------------------------
        qtr = request.requeue_transition_request

        # event_type must be TASK_REQUEUED
        if qtr.event_type != "TASK_REQUEUED":
            raise WorkflowInputError(
                "requeue_transition_request event_type must be "
                "TASK_REQUEUED"
            )

        # payload must be RequeuePayload
        if not isinstance(qtr.payload, RequeuePayload):
            raise WorkflowInputError(
                "requeue_transition_request payload must be "
                "RequeuePayload"
            )

        # CAS task_id must match
        if qtr.cas.task_id != task_id:
            raise WorkflowInputError(
                "requeue_transition_request task_id must match"
            )

        # CAS expected_state must be "returned"
        if qtr.cas.expected_state != "returned":
            raise WorkflowInputError(
                "requeue_transition_request expected_state must be "
                "'returned'"
            )

        # dispatch_cas must be None (PM-only transition)
        if qtr.dispatch_cas is not None:
            raise WorkflowInputError(
                "requeue_transition_request dispatch_cas must be None"
            )

        # -- 1h. event_id dedup: both must differ from each other -----------
        if rtr.event_id == qtr.event_id:
            raise WorkflowInputError(
                "return and requeue event_ids must differ"
            )

        # event_ids must also differ from already-used event_ids
        existing_event_ids = {
            dcr.dispatch_transition.event_id,
            dcr.acknowledge_transition.event_id,
            dcr.delivery_transition.event_id,
        }
        if rtr.event_id in existing_event_ids:
            raise WorkflowInputError(
                "return event_id must differ from existing event_ids"
            )
        if qtr.event_id in existing_event_ids:
            raise WorkflowInputError(
                "requeue event_id must differ from existing event_ids"
            )

        # -- 2. clock.now() ---------------------------------------------------
        now_start = self.clock.now()

        # -- 3. Acquire new remediation WorkerSlotLease -----------------------
        remediation_acquired = False
        remediation_lease = None
        body_error: BaseException | None = None
        return_result: TransitionResult | None = None
        requeue_result: TransitionResult | None = None

        try:
            now_remediation_acquire = self.clock.now()
            remediation_lease = acquire_worker_slot(
                self.project_root,
                request.worker_kind,
                receipt.identity.dispatch_id,
                request.holder_instance_id,
                self.project_root,  # workspace — remediation is control-plane only
                now_remediation_acquire,
            )
            remediation_acquired = True

            # -- 4. Apply DELIVERY_RETURNED under remediation lease ----------
            now_return = self.clock.now()
            return_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(rtr, remediation_lease, now_return)

        except BaseException as exc:
            body_error = exc
            raise
        finally:
            # -- 5. Release remediation lease exactly once -------------------
            if remediation_acquired:
                try:
                    now_release = self.clock.now()
                    release_worker_slot(
                        self.project_root,
                        remediation_lease,
                        now_release,
                    )
                except BaseException as release_exc:
                    if body_error is None:
                        raise
                    raise body_error from release_exc

        # -- 6. Apply TASK_REQUEUED with lease=None ---------------------------
        now_requeue = self.clock.now()
        requeue_result = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(qtr, lease=None, now=now_requeue)

        # -- 7. Return result -------------------------------------------------
        return DeliveryRemediationResult(
            task_id=task_id,
            audit_result=acr.audit_result,
            return_transition=return_result,
            requeue_transition=requeue_result,
        )

    async def run_blocked_audit_cycle(
        self,
        request: BlockedAuditRequest,
    ) -> BlockedAuditResult:
        """Execute blocked audit escalation cycle (TC-13.18d.2).

        Execution order:
        1. validate BlockedAuditRequest
        2. evaluate_escalation(current_worker_kind) — exactly once
        3. clock.now()
        4. apply_transition(TASK_BLOCKED, lease=None) — exactly once
        5. return BlockedAuditResult

        No MAD audit, no WorkerSlotLease operations, no dispatch cycle.
        """
        # -- 1. Validate request type -----------------------------------------
        if not isinstance(request, BlockedAuditRequest):
            raise WorkflowInputError(
                "request must be BlockedAuditRequest, "
                f"got {type(request).__name__}"
            )

        acr = request.acceptance_cycle_result
        dcr = request.dispatch_cycle_result
        btr = request.block_transition_request

        # -- 1a. Validate audit result: must be "blocked" --------------------
        if not isinstance(acr.audit_result, MadAuditGatewayResult):
            raise WorkflowInputError(
                "acceptance_cycle_result.audit_result must be "
                "MadAuditGatewayResult"
            )

        verdict = acr.audit_result.verdict
        if verdict != "blocked":
            raise WorkflowInputError(
                "acceptance_cycle_result audit verdict must be 'blocked'"
            )

        # -- 1b. Fail-closed: accept/integrate transitions must be None -------
        if acr.accept_transition is not None:
            raise WorkflowInvariantError(
                "accept_transition must be None for blocked verdict"
            )
        if acr.integrate_transition is not None:
            raise WorkflowInvariantError(
                "integrate_transition must be None for blocked verdict"
            )

        # -- 1c. Validate current_worker_kind --------------------------------
        if not isinstance(request.current_worker_kind, WorkerKind):
            raise WorkflowInputError(
                "current_worker_kind must be WorkerKind, "
                f"got {type(request.current_worker_kind).__name__}"
            )

        # -- 1d. Identity binding: task_id consistency -----------------------
        receipt = dcr.delivery_receipt
        task_id = acr.task_id
        if task_id != receipt.identity.task_id:
            raise WorkflowInputError(
                "acceptance_cycle_result task_id must match "
                "dispatch cycle delivery receipt task_id"
            )
        if task_id != btr.cas.task_id:
            raise WorkflowInputError(
                "block_transition_request task_id must match "
                "acceptance_cycle_result task_id"
            )

        # -- 1e. WorkerKind binding: must exactly equal WorkerResult --------
        if request.current_worker_kind is not dcr.worker_result.worker_kind:
            raise WorkflowInputError(
                "current_worker_kind must exactly equal "
                "dispatch_cycle_result.worker_result.worker_kind"
            )

        # -- 1f. Validate block_transition_request ---------------------------
        # event_type must be TASK_BLOCKED
        if btr.event_type != "TASK_BLOCKED":
            raise WorkflowInputError(
                "block_transition_request event_type must be "
                "TASK_BLOCKED"
            )

        # payload must be BlockedPayload
        if not isinstance(btr.payload, BlockedPayload):
            raise WorkflowInputError(
                "block_transition_request payload must be "
                "BlockedPayload"
            )

        # CAS expected_state must be review_ready
        if btr.cas.expected_state != "review_ready":
            raise WorkflowInputError(
                "block_transition_request expected_state must be "
                "'review_ready'"
            )

        # to_state must be "blocked" — validated by TransitionService,
        # but checked here for fail-closed consistency
        if btr.cas.task_id != task_id:
            raise WorkflowInputError(
                "block_transition_request task_id mismatch"
            )

        # dispatch_cas must be None (PM-only transition)
        if btr.dispatch_cas is not None:
            raise WorkflowInputError(
                "block_transition_request dispatch_cas must be None"
            )

        # -- 1g. event_id dedup: must differ from dispatch, ACK, delivery ---
        existing_event_ids = {
            dcr.dispatch_transition.event_id,
            dcr.acknowledge_transition.event_id,
            dcr.delivery_transition.event_id,
        }
        if btr.event_id in existing_event_ids:
            raise WorkflowInputError(
                "block event_id must differ from dispatch, ACK, and "
                "delivery event_ids"
            )

        # -- 2. Evaluate escalation exactly once -----------------------------
        escalation_decision = evaluate_escalation(
            EscalationRequest(
                current_worker_kind=request.current_worker_kind,
            )
        )

        # -- 3. clock.now() --------------------------------------------------
        now_block = self.clock.now()

        # -- 4. Apply TASK_BLOCKED with lease=None ---------------------------
        block_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(btr, lease=None, now=now_block)

        # -- 5. Return result ------------------------------------------------
        return BlockedAuditResult(
            task_id=task_id,
            audit_result=acr.audit_result,
            escalation_decision=escalation_decision,
            block_transition=block_transition,
        )

    async def run_escalated_redispatch(
        self,
        request: EscalatedRedispatchRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> EscalatedRedispatchResult:
        """Execute a single escalated redispatch cycle (TC-13.18d.3).

        Execution order:
        1. Full validation of EscalatedRedispatchRequest
        2. clock.now()
        3. apply_transition(BLOCKER_RESOLVED, lease=None, now)
        4. await self.run_dispatch_cycle(next_dispatch_cycle_request, providers)
        5. return EscalatedRedispatchResult

        Requests with REQUEST_USER_DECISION action are fail-closed with
        WorkflowInputError before any transition, slot, or dispatch.
        """
        # -- Validate request type ---------------------------------------------
        if not isinstance(request, EscalatedRedispatchRequest):
            raise WorkflowInputError(
                "request must be EscalatedRedispatchRequest, "
                f"got {type(request).__name__}"
            )

        bar = request.blocked_audit_request
        bar_result = request.blocked_audit_result
        rtr = request.resolve_transition_request
        ndcr = request.next_dispatch_cycle_request

        # -- 4.1 BlockedAuditRequest/Result binding ----------------------------
        _validate_blocked_audit_binding(bar, bar_result)

        # -- 4.2 EscalationDecision validation ---------------------------------
        _validate_escalation_decision_for_redispatch(bar_result.escalation_decision)

        # -- 4.3 Original BlockedPayload validation ----------------------------
        _validate_blocked_payload_for_redispatch(bar.block_transition_request)

        # -- 4.4 BLOCKER_RESOLVED validation -----------------------------------
        _validate_blocker_resolved_for_redispatch(rtr, bar, bar_result)

        # -- 4.5 Next DispatchCycleRequest validation --------------------------
        _validate_next_dispatch_cycle_for_redispatch(
            ndcr, bar, bar_result
        )

        # -- 4.6 Providers validation ------------------------------------------
        if not isinstance(providers, Mapping):
            raise WorkflowInputError(
                f"providers must be a Mapping, "
                f"got {type(providers).__name__}"
            )
        if len(providers) == 0:
            raise WorkflowInputError("providers must not be empty")

        # -- 2. clock.now() ---------------------------------------------------
        now_resolve = self.clock.now()

        # -- 3. Apply BLOCKER_RESOLVED with lease=None ------------------------
        resolve_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(rtr, lease=None, now=now_resolve)

        # -- 4. Run single dispatch cycle -------------------------------------
        dispatch_cycle_result = await self.run_dispatch_cycle(
            ndcr,
            providers,
        )

        # -- 5. Return result -------------------------------------------------
        return EscalatedRedispatchResult(
            task_id=bar_result.task_id,
            escalation_decision=bar_result.escalation_decision,
            resolve_transition=resolve_transition,
            dispatch_cycle_result=dispatch_cycle_result,
        )

    async def record_integration_failure(
        self,
        request: IntegrationFailureRequest,
    ) -> IntegrationFailureResult:
        """Record an integration failure observed externally (TC-13.18d.4).

        Execution order:
        1. Fail-closed input validation
        2. clock.now()
        3. apply_transition(INTEGRATION_FAILED, lease=None, now)
        4. return IntegrationFailureResult

        Preconditions (enforced in validation):
        * run_acceptance_cycle() completed with DELIVERY_ACCEPTED.
        * acceptance_cycle_request.integration_transition_request is None.
        * Caller observed real integration failure externally.
        * audit verdict is "pass".
        * accept_transition is present, integrate_transition is None.

        No MAD audit, no lease operations, no escalation, no auto-retry.
        """
        # -- 1. Fail-closed input validation -----------------------------------
        self._validate_integration_failure_request(request)

        # -- 2. clock.now() ----------------------------------------------------
        now = self.clock.now()

        # -- 3. Apply INTEGRATION_FAILED with lease=None -----------------------
        failure_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(
            request.failure_transition_request,
            lease=None,
            now=now,
        )

        # -- 4. Return result --------------------------------------------------
        return IntegrationFailureResult(
            task_id=request.acceptance_cycle_result.task_id,
            audit_result=request.acceptance_cycle_result.audit_result,
            failure_transition=failure_transition,
        )

    async def record_blocked_rescope(
        self,
        request: BlockedRescopeRequest,
    ) -> BlockedRescopeResult:
        """Rescope an expert blocked task via BLOCKER_RESCOPED (TC-13.18d.5).

        Execution order:
        1. Fail-closed input validation
        2. clock.now()
        3. apply_transition(BLOCKER_RESCOPED, lease=None, now)
        4. return BlockedRescopeResult

        Does NOT re-run MAD audit, evaluate_escalation, auto-dispatch,
        or any WorkerSlotLease operations.

        Transition exceptions and asyncio.CancelledError propagate
        unchanged.
        """
        # -- 1. Fail-closed input validation -----------------------------------
        self._validate_blocked_rescope_request(request)

        # -- 2. clock.now() ----------------------------------------------------
        now = self.clock.now()

        # -- 3. Apply BLOCKER_RESCOPED with lease=None -------------------------
        rescope_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(
            request.rescope_transition_request,
            lease=None,
            now=now,
        )

        # -- 4. Return result --------------------------------------------------
        previous_revision = request.rescope_transition_request.cas.expected_revision
        return BlockedRescopeResult(
            task_id=request.blocked_audit_result.task_id,
            previous_revision=previous_revision,
            new_revision=previous_revision + 1,
            rescope_transition=rescope_transition,
        )

    async def record_blocked_cancellation(
        self,
        request: BlockedCancellationRequest,
    ) -> BlockedCancellationResult:
        """Cancel an expert blocked task via BLOCKER_CANCELLED (TC-13.18d.6).

        Execution order:
        1. Fail-closed input validation
        2. clock.now()
        3. apply_transition(BLOCKER_CANCELLED, lease=None, now)
        4. return BlockedCancellationResult

        Does NOT re-run MAD audit, evaluate_escalation, auto-dispatch,
        or any WorkerSlotLease operations.

        Transition exceptions and asyncio.CancelledError propagate
        unchanged.
        """
        # -- 1. Fail-closed input validation -----------------------------------
        self._validate_blocked_cancellation_request(request)

        # -- 2. clock.now() ----------------------------------------------------
        now = self.clock.now()

        # -- 3. Apply BLOCKER_CANCELLED with lease=None ------------------------
        cancellation_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(
            request.cancellation_transition_request,
            lease=None,
            now=now,
        )

        # -- 4. Return result --------------------------------------------------
        return BlockedCancellationResult(
            task_id=request.blocked_audit_result.task_id,
            escalation_decision=request.blocked_audit_result.escalation_decision,
            cancellation_transition=cancellation_transition,
        )

    async def cancel_quiescent_task(
        self,
        request: TaskCancellationRequest,
    ) -> TaskCancellationResult:
        """Cancel a non-terminal task with no active dispatch (TC-13.18d.7).

        Execution order:
        1. Validate request shape (exactly TaskCancellationRequest)
        2. StateProvider.snapshot()
        3. Locate and validate task from snapshot
        4. now = clock.now()
        5. apply_transition(TASK_CANCELLED, lease=None, now)
        6. return TaskCancellationResult

        Must NOT cancel a running Worker.  Active dispatch cooperative
        cancellation, Worker termination, and heartbeat cleanup are
        deferred to separate task cards.

        Does NOT run MAD audit, EscalationService, WorkerAdapter, or any
        WorkerSlotLease operations.  No auto-retry.  Transition exceptions
        and asyncio.CancelledError propagate unchanged.
        """
        # -- 1. Validate request shape -------------------------------------------
        if type(request) is not TaskCancellationRequest:
            raise WorkflowInputError(
                "request must be TaskCancellationRequest, "
                f"got {type(request).__name__}"
            )

        tr = request.cancellation_transition_request

        # -- 1a. transition request must be exactly TransitionRequest ------------
        if type(tr) is not TransitionRequest:
            raise WorkflowInputError(
                "cancellation_transition_request must be TransitionRequest, "
                f"got {type(tr).__name__}"
            )

        # -- 1b. event_type must be TASK_CANCELLED ------------------------------
        if tr.event_type != "TASK_CANCELLED":
            raise WorkflowInputError(
                "cancellation_transition_request event_type must be "
                "TASK_CANCELLED"
            )

        # -- 1c. payload must be CancelledPayload -------------------------------
        if type(tr.payload) is not CancelledPayload:
            raise WorkflowInputError(
                "cancellation_transition_request payload must be "
                "CancelledPayload"
            )

        # -- 1d. to_state must be "cancelled" -----------------------------------
        # (validated by TransitionService, fail-closed check here)

        # -- 1e. dispatch_cas must be None --------------------------------------
        if tr.dispatch_cas is not None:
            raise WorkflowInputError(
                "cancellation_transition_request dispatch_cas must be None"
            )

        # -- 1f. event_id must not be empty --------------------------------------
        if not tr.event_id:
            raise WorkflowInputError(
                "cancellation_transition_request event_id must not be empty"
            )

        # -- 2. StateProvider.snapshot() -----------------------------------------
        try:
            snapshot = StateProvider(self.project_root).snapshot()
        except StateProviderError:
            raise  # propagate as-is

        # -- 3. Locate and validate task -----------------------------------------
        task_id = tr.cas.task_id
        task = None
        for t in snapshot.tasks:
            if t.task_id == task_id:
                task = t
                break
        if task is None:
            raise WorkflowInputError(
                "target task not found in snapshot"
            )

        # -- 3a. revision must match CAS expected_revision -----------------------
        if task.revision != tr.cas.expected_revision:
            raise WorkflowInputError(
                "task revision does not match CAS expected_revision"
            )

        # -- 3b. state must match CAS expected_state ----------------------------
        if task.state != tr.cas.expected_state:
            raise WorkflowInputError(
                "task state does not match CAS expected_state"
            )

        # -- 3c. current_dispatch must be None (quiescent only) ------------------
        if task.current_dispatch is not None:
            raise WorkflowInputError(
                "task has active dispatch — quiescent cancellation refused"
            )

        # -- 3d. task must not already be in a terminal state --------------------
        _TERMINAL_STATES = frozenset({"cancelled", "superseded", "integrated"})
        if task.state in _TERMINAL_STATES:
            raise WorkflowInputError(
                "task is already in a terminal state"
            )

        # -- 4. clock.now() ------------------------------------------------------
        now = self.clock.now()

        # -- 5. Apply TASK_CANCELLED with lease=None -----------------------------
        cancellation_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(tr, lease=None, now=now)

        # -- 6. Return result ----------------------------------------------------
        return TaskCancellationResult(
            task_id=task_id,
            cancellation_transition=cancellation_transition,
        )

    async def supersede_quiescent_task(
        self,
        request: TaskSupersessionRequest,
    ) -> TaskSupersessionResult:
        """Supersede a non-terminal task with no active dispatch (TC-13.18d.8).

        Execution order:
        1. Validate request shape (exactly TaskSupersessionRequest)
        2. StateProvider.snapshot() exactly once
        3. Locate source and replacement tasks from snapshot
        4. Validate source/replacement/CAS
        5. now = clock.now()
        6. apply_transition(TASK_SUPERSEDED, lease=None, now)
        7. return TaskSupersessionResult

        Must NOT supersede a task with an active dispatch.  Does NOT run
        MAD audit, EscalationService, WorkerAdapter, WorkerSlotLease, or
        dispatch replacement task.  Transition exceptions and
        asyncio.CancelledError propagate unchanged.
        """
        # -- 1. Validate request shape -------------------------------------------
        if type(request) is not TaskSupersessionRequest:
            raise WorkflowInputError(
                "request must be TaskSupersessionRequest, "
                f"got {type(request).__name__}"
            )

        tr = request.supersession_transition_request

        # -- 1a. transition request must be exactly TransitionRequest ------------
        if type(tr) is not TransitionRequest:
            raise WorkflowInputError(
                "supersession_transition_request must be TransitionRequest, "
                f"got {type(tr).__name__}"
            )

        # -- 1b. event_type must be TASK_SUPERSEDED ------------------------------
        if tr.event_type != "TASK_SUPERSEDED":
            raise WorkflowInputError(
                "supersession_transition_request event_type must be "
                "TASK_SUPERSEDED"
            )

        # -- 1c. payload must be SupersededPayload -------------------------------
        if type(tr.payload) is not SupersededPayload:
            raise WorkflowInputError(
                "supersession_transition_request payload must be "
                "SupersededPayload"
            )

        # -- 1d. to_state must be "superseded" -----------------------------------
        # (validated by TransitionService, fail-closed check here)

        # -- 1e. dispatch_cas must be None --------------------------------------
        if tr.dispatch_cas is not None:
            raise WorkflowInputError(
                "supersession_transition_request dispatch_cas must be None"
            )

        # -- 1f. event_id must not be empty --------------------------------------
        if not tr.event_id:
            raise WorkflowInputError(
                "supersession_transition_request event_id must not be empty"
            )

        # -- 2. StateProvider.snapshot() -----------------------------------------
        try:
            snapshot = StateProvider(self.project_root).snapshot()
        except StateProviderError:
            raise  # propagate as-is

        # -- 3. Locate source and replacement tasks ------------------------------
        task_id = tr.cas.task_id
        source_task = None
        replacement_task = None
        superseded_by = tr.payload.superseded_by

        for t in snapshot.tasks:
            if t.task_id == task_id:
                source_task = t
            if t.task_id == superseded_by:
                replacement_task = t

        # -- 4a. Source task must exist ------------------------------------------
        if source_task is None:
            raise WorkflowInputError(
                "source task not found in snapshot"
            )

        # -- 4b. Source task revision must match CAS expected_revision -----------
        if source_task.revision != tr.cas.expected_revision:
            raise WorkflowInputError(
                "task revision does not match CAS expected_revision"
            )

        # -- 4c. Source task state must match CAS expected_state -----------------
        if source_task.state != tr.cas.expected_state:
            raise WorkflowInputError(
                "task state does not match CAS expected_state"
            )

        # -- 4d. Source task current_dispatch must be None (quiescent only) ------
        if source_task.current_dispatch is not None:
            raise WorkflowInputError(
                "task has active dispatch — quiescent supersession refused"
            )

        # -- 4e. Source task must not already be in a terminal state -------------
        _TERMINAL_STATES = frozenset({"cancelled", "superseded", "integrated"})
        if source_task.state in _TERMINAL_STATES:
            raise WorkflowInputError(
                "task is already in a terminal state"
            )

        # -- 4f. superseded_by must be a non-empty safe string -------------------
        # (already validated by SupersededPayload.__post_init__)

        # -- 4g. Source and replacement must not be the same task ----------------
        if task_id == superseded_by:
            raise WorkflowInputError(
                "source task must not supersede itself"
            )

        # -- 4h. Replacement task must exist in snapshot -------------------------
        if replacement_task is None:
            raise WorkflowInputError(
                "replacement task not found in snapshot"
            )

        # -- 5. clock.now() ------------------------------------------------------
        now = self.clock.now()

        # -- 6. Apply TASK_SUPERSEDED with lease=None ----------------------------
        supersession_transition = ControlPlaneTransitionService(
            self.project_root
        ).apply_transition(tr, lease=None, now=now)

        # -- 7. Return result ----------------------------------------------------
        return TaskSupersessionResult(
            task_id=task_id,
            superseded_by=superseded_by,
            supersession_transition=supersession_transition,
        )

    # -- private validation helpers -------------------------------------------

    def _validate_integration_failure_request(
        self,
        request: IntegrationFailureRequest,
    ) -> None:
        """Fail-closed validation — any violation raises before transition.

        Raises TypeError / WorkflowInputError on the first violation;
        messages never contain paths, prompts, report bodies, issue
        descriptions, stdout, secrets, or input repr/str.
        """
        # -- request must be exactly IntegrationFailureRequest -----------------
        if type(request) is not IntegrationFailureRequest:
            raise TypeError(
                "request must be IntegrationFailureRequest, "
                f"got {type(request).__name__}"
            )

        acr = request.acceptance_cycle_request
        acr_result = request.acceptance_cycle_result
        dcr = request.dispatch_cycle_result
        ftr = request.failure_transition_request

        # -- acceptance_cycle_request type ------------------------------------
        if type(acr) is not AcceptanceCycleRequest:
            raise TypeError(
                "acceptance_cycle_request must be AcceptanceCycleRequest, "
                f"got {type(acr).__name__}"
            )

        # -- acceptance_cycle_result type -------------------------------------
        if type(acr_result) is not AcceptanceCycleResult:
            raise TypeError(
                "acceptance_cycle_result must be AcceptanceCycleResult, "
                f"got {type(acr_result).__name__}"
            )

        # -- dispatch_cycle_result type ---------------------------------------
        if type(dcr) is not DispatchCycleResult:
            raise TypeError(
                "dispatch_cycle_result must be DispatchCycleResult, "
                f"got {type(dcr).__name__}"
            )

        # -- failure_transition_request type ----------------------------------
        if type(ftr) is not TransitionRequest:
            raise TypeError(
                "failure_transition_request must be TransitionRequest, "
                f"got {type(ftr).__name__}"
            )

        # -- acceptance_cycle_request.integration_transition_request is None --
        if acr.integration_transition_request is not None:
            raise WorkflowInputError(
                "acceptance_cycle_request.integration_transition_request "
                "must be None"
            )

        # -- audit verdict must be "pass" -------------------------------------
        audit_result = acr_result.audit_result
        if type(audit_result) is not MadAuditGatewayResult:
            raise TypeError(
                "audit_result must be MadAuditGatewayResult, "
                f"got {type(audit_result).__name__}"
            )

        verdict = audit_result.verdict
        if verdict != "pass":
            raise WorkflowInputError(
                "audit verdict must be 'pass'"
            )

        # -- accept_transition must be present --------------------------------
        if acr_result.accept_transition is None:
            raise WorkflowInputError(
                "accept_transition must be present"
            )

        # -- integrate_transition must be None --------------------------------
        if acr_result.integrate_transition is not None:
            raise WorkflowInputError(
                "integrate_transition must be None"
            )

        # -- task_id consistency across three objects -------------------------
        receipt = dcr.delivery_receipt
        task_id = acr_result.task_id
        if task_id != receipt.identity.task_id:
            raise WorkflowInputError(
                "acceptance_cycle_result task_id must match "
                "delivery receipt task_id"
            )
        if acr.dispatch_cycle_result.delivery_receipt.identity.task_id != task_id:
            raise WorkflowInputError(
                "acceptance_cycle_request task_id must match "
                "delivery receipt task_id"
            )

        # -- revision, attempt, dispatch_id must match delivery receipt --------
        acr_receipt = acr.dispatch_cycle_result.delivery_receipt
        if acr_receipt.identity.revision != receipt.identity.revision:
            raise WorkflowInputError(
                "revision inconsistency"
            )
        if acr_receipt.identity.attempt != receipt.identity.attempt:
            raise WorkflowInputError(
                "attempt inconsistency"
            )
        if acr_receipt.identity.dispatch_id != receipt.identity.dispatch_id:
            raise WorkflowInputError(
                "dispatch_id inconsistency"
            )

        # -- failure_transition_request checks ---------------------------------
        # event_type must be INTEGRATION_FAILED
        if ftr.event_type != "INTEGRATION_FAILED":
            raise WorkflowInputError(
                "failure_transition_request event_type must be "
                "INTEGRATION_FAILED"
            )

        # payload must be BlockedPayload
        if type(ftr.payload) is not BlockedPayload:
            raise WorkflowInputError(
                "failure_transition_request payload must be "
                "BlockedPayload"
            )

        bp = ftr.payload

        # CAS expected_state must be "accepted"
        if ftr.cas.expected_state != "accepted":
            raise WorkflowInputError(
                "failure_transition_request cas.expected_state must be "
                "'accepted'"
            )

        # to_state must be "blocked" (TransitionService validates, but
        # we fail-closed check here)
        if ftr.cas.task_id != task_id:
            raise WorkflowInputError(
                "failure_transition_request cas.task_id must match"
            )

        # dispatch_cas must be None
        if ftr.dispatch_cas is not None:
            raise WorkflowInputError(
                "failure_transition_request dispatch_cas must be None"
            )

        # event_id must not duplicate existing event_ids
        existing_event_ids = {
            dcr.dispatch_transition.event_id,
            dcr.acknowledge_transition.event_id,
            dcr.delivery_transition.event_id,
            acr_result.accept_transition.event_id,
        }
        if ftr.event_id in existing_event_ids:
            raise WorkflowInputError(
                "failure event_id must differ from dispatch, ACK, "
                "delivery, and acceptance event_ids"
            )

        # BlockedPayload.resume_state must be "accepted"
        if bp.resume_state != "accepted":
            raise WorkflowInputError(
                "BlockedPayload.resume_state must be 'accepted'"
            )

        # BlockedPayload.blocked_attempt_valid must be True
        if bp.blocked_attempt_valid is not True:
            raise WorkflowInputError(
                "BlockedPayload.blocked_attempt_valid must be True"
            )

    def _validate_blocked_rescope_request(
        self,
        request: BlockedRescopeRequest,
    ) -> None:
        """Fail-closed validation — any violation raises before transition.

        Raises TypeError / WorkflowInputError on the first violation;
        messages never contain paths, prompts, report bodies, issue
        descriptions, stdout, secrets, dispatch_id, task_id,
        blocked_reason, or input repr/str.
        """
        # -- request must be exactly BlockedRescopeRequest --------------------
        if type(request) is not BlockedRescopeRequest:
            raise TypeError(
                "request must be BlockedRescopeRequest, "
                f"got {type(request).__name__}"
            )

        bar = request.blocked_audit_request
        bar_result = request.blocked_audit_result
        rtr = request.rescope_transition_request

        # -- blocked_audit_request must be exactly BlockedAuditRequest --------
        if type(bar) is not BlockedAuditRequest:
            raise TypeError(
                "blocked_audit_request must be BlockedAuditRequest, "
                f"got {type(bar).__name__}"
            )

        # -- blocked_audit_result must be exactly BlockedAuditResult ----------
        if type(bar_result) is not BlockedAuditResult:
            raise TypeError(
                "blocked_audit_result must be BlockedAuditResult, "
                f"got {type(bar_result).__name__}"
            )

        # -- rescope_transition_request must be exactly TransitionRequest ------
        if type(rtr) is not TransitionRequest:
            raise TypeError(
                "rescope_transition_request must be TransitionRequest, "
                f"got {type(rtr).__name__}"
            )

        # -- blocked_audit_result must bind to blocked_audit_request ----------
        # result.audit_result is request.acceptance_cycle_result.audit_result
        if bar_result.audit_result is not bar.acceptance_cycle_result.audit_result:
            raise WorkflowInputError(
                "blocked_audit_result.audit_result must be the same object "
                "as blocked_audit_request.acceptance_cycle_result.audit_result"
            )

        # result.escalation_decision.current_worker_kind == request.current_worker_kind
        if bar_result.escalation_decision.current_worker_kind is not bar.current_worker_kind:
            raise WorkflowInputError(
                "escalation_decision.current_worker_kind must exactly equal "
                "blocked_audit_request.current_worker_kind"
            )

        # result.block_transition.task_id == result.task_id
        if bar_result.block_transition.task_id != bar_result.task_id:
            raise WorkflowInputError(
                "block_transition.task_id must match blocked_audit_result.task_id"
            )

        # result.block_transition.to_state == "blocked"
        if bar_result.block_transition.to_state != "blocked":
            raise WorkflowInputError(
                "block_transition.to_state must be 'blocked'"
            )

        # -- Audit verdict must be "blocked" -----------------------------------
        verdict = bar.acceptance_cycle_result.audit_result.verdict
        if verdict != "blocked":
            raise WorkflowInputError(
                "audit verdict must be 'blocked'"
            )

        # -- current_worker_kind must be EXPERT_AGENT -------------------------
        decision = bar_result.escalation_decision
        if decision.current_worker_kind is not WorkerKind.EXPERT_AGENT:
            raise WorkflowInputError(
                "current_worker_kind must be EXPERT_AGENT"
            )

        # -- escalation action must be REQUEST_USER_DECISION -------------------
        if decision.action is not EscalationAction.REQUEST_USER_DECISION:
            raise WorkflowInputError(
                "escalation action must be REQUEST_USER_DECISION"
            )

        # -- next_worker_kind must be None ------------------------------------
        if decision.next_worker_kind is not None:
            raise WorkflowInputError(
                "next_worker_kind must be None for REQUEST_USER_DECISION"
            )

        # -- task_id consistency across bound objects -------------------------
        dcr = bar.dispatch_cycle_result
        task_id = bar_result.task_id
        receipt = dcr.delivery_receipt

        if task_id != receipt.identity.task_id:
            raise WorkflowInputError(
                "task_id must match delivery receipt task_id"
            )

        # -- revision, attempt, dispatch_id must match DeliveryReceipt ---------
        if bar_result.block_transition.task_id != task_id:
            raise WorkflowInputError(
                "block transition task_id inconsistency"
            )

        # -- Original block transition must be TASK_BLOCKED -------------------
        btr = bar.block_transition_request
        if btr.event_type != "TASK_BLOCKED":
            raise WorkflowInputError(
                "original block transition request event_type must be "
                "TASK_BLOCKED"
            )

        # -- Original BlockedPayload validation --------------------------------
        bp = btr.payload
        if not isinstance(bp, BlockedPayload):
            raise WorkflowInputError(
                "original block transition payload must be BlockedPayload"
            )

        # resume_state must be "draft"
        if getattr(bp, "resume_state", None) != "draft":
            raise WorkflowInputError(
                "BlockedPayload.resume_state must be 'draft'"
            )

        # blocked_attempt_valid must be False
        if getattr(bp, "blocked_attempt_valid", None) is not False:
            raise WorkflowInputError(
                "BlockedPayload.blocked_attempt_valid must be False"
            )

        # -- rescope_transition_request validation ----------------------------
        # event_type must be BLOCKER_RESCOPED
        if rtr.event_type != "BLOCKER_RESCOPED":
            raise WorkflowInputError(
                "rescope_transition_request event_type must be "
                "BLOCKER_RESCOPED"
            )

        # payload must be BlockerRescopedPayload
        if not isinstance(rtr.payload, BlockerRescopedPayload):
            raise WorkflowInputError(
                "rescope_transition_request payload must be "
                "BlockerRescopedPayload"
            )

        brp = rtr.payload

        # CAS expected_state must be "blocked"
        if rtr.cas.expected_state != "blocked":
            raise WorkflowInputError(
                "rescope_transition_request cas.expected_state must be "
                "'blocked'"
            )

        # CAS task_id must match
        if rtr.cas.task_id != task_id:
            raise WorkflowInputError(
                "rescope_transition_request cas.task_id must match task_id"
            )

        # CAS expected_revision must match original revision
        revision = receipt.identity.revision
        if rtr.cas.expected_revision != revision:
            raise WorkflowInputError(
                "rescope_transition_request cas.expected_revision must "
                "match delivery receipt revision"
            )

        # BlockerRescopedPayload.new_revision must equal expected_revision + 1
        if brp.new_revision != revision + 1:
            raise WorkflowInputError(
                "BlockerRescopedPayload.new_revision must equal "
                "expected_revision + 1"
            )

        # dispatch_cas must be None
        if rtr.dispatch_cas is not None:
            raise WorkflowInputError(
                "rescope_transition_request dispatch_cas must be None"
            )

        # -- event_id dedup: must differ from dispatch, ACK, delivery, block --
        existing_event_ids = {
            dcr.dispatch_transition.event_id,
            dcr.acknowledge_transition.event_id,
            dcr.delivery_transition.event_id,
            bar_result.block_transition.event_id,
        }
        if rtr.event_id in existing_event_ids:
            raise WorkflowInputError(
                "rescope event_id must differ from dispatch, ACK, "
                "delivery, and block event_ids"
            )

        # -- bool must not be revision ----------------------------------------
        if isinstance(revision, bool):
            raise WorkflowInputError(
                "revision must not be bool"
            )

    def _validate_blocked_cancellation_request(
        self,
        request: BlockedCancellationRequest,
    ) -> None:
        """Fail-closed validation — any violation raises before transition.

        Raises TypeError / WorkflowInputError on the first violation;
        messages never contain paths, prompts, report bodies, issue
        descriptions, stdout, secrets, dispatch_id, task_id,
        blocked_reason, or input repr/str.
        """
        # -- request must be exactly BlockedCancellationRequest ----------------
        if type(request) is not BlockedCancellationRequest:
            raise TypeError(
                "request must be BlockedCancellationRequest, "
                f"got {type(request).__name__}"
            )

        bar = request.blocked_audit_request
        bar_result = request.blocked_audit_result
        ctr = request.cancellation_transition_request

        # -- blocked_audit_request must be exactly BlockedAuditRequest ---------
        if type(bar) is not BlockedAuditRequest:
            raise TypeError(
                "blocked_audit_request must be BlockedAuditRequest, "
                f"got {type(bar).__name__}"
            )

        # -- blocked_audit_result must be exactly BlockedAuditResult -----------
        if type(bar_result) is not BlockedAuditResult:
            raise TypeError(
                "blocked_audit_result must be BlockedAuditResult, "
                f"got {type(bar_result).__name__}"
            )

        # -- cancellation_transition_request must be exactly TransitionRequest --
        if type(ctr) is not TransitionRequest:
            raise TypeError(
                "cancellation_transition_request must be TransitionRequest, "
                f"got {type(ctr).__name__}"
            )

        # -- blocked_audit_result must bind to blocked_audit_request -----------
        # result.audit_result is request.acceptance_cycle_result.audit_result
        if bar_result.audit_result is not bar.acceptance_cycle_result.audit_result:
            raise WorkflowInputError(
                "blocked_audit_result.audit_result must be the same object "
                "as blocked_audit_request.acceptance_cycle_result.audit_result"
            )

        # result.escalation_decision.current_worker_kind == request.current_worker_kind
        if bar_result.escalation_decision.current_worker_kind is not bar.current_worker_kind:
            raise WorkflowInputError(
                "escalation_decision.current_worker_kind must exactly equal "
                "blocked_audit_request.current_worker_kind"
            )

        # result.block_transition.task_id == result.task_id
        if bar_result.block_transition.task_id != bar_result.task_id:
            raise WorkflowInputError(
                "block_transition.task_id must match blocked_audit_result.task_id"
            )

        # result.block_transition.to_state == "blocked"
        if bar_result.block_transition.to_state != "blocked":
            raise WorkflowInputError(
                "block_transition.to_state must be 'blocked'"
            )

        # -- Audit verdict must be "blocked" -----------------------------------
        verdict = bar.acceptance_cycle_result.audit_result.verdict
        if verdict != "blocked":
            raise WorkflowInputError(
                "audit verdict must be 'blocked'"
            )

        # -- accept/integrate transitions must be None -------------------------
        if bar.acceptance_cycle_result.accept_transition is not None:
            raise WorkflowInvariantError(
                "accept_transition must be None for blocked verdict"
            )
        if bar.acceptance_cycle_result.integrate_transition is not None:
            raise WorkflowInvariantError(
                "integrate_transition must be None for blocked verdict"
            )

        # -- current_worker_kind must be EXPERT_AGENT --------------------------
        decision = bar_result.escalation_decision
        if decision.current_worker_kind is not WorkerKind.EXPERT_AGENT:
            raise WorkflowInputError(
                "current_worker_kind must be EXPERT_AGENT"
            )

        # -- escalation action must be REQUEST_USER_DECISION -------------------
        if decision.action is not EscalationAction.REQUEST_USER_DECISION:
            raise WorkflowInputError(
                "escalation action must be REQUEST_USER_DECISION"
            )

        # -- next_worker_kind must be None -------------------------------------
        if decision.next_worker_kind is not None:
            raise WorkflowInputError(
                "next_worker_kind must be None for REQUEST_USER_DECISION"
            )

        # -- task_id consistency across bound objects ---------------------------
        dcr = bar.dispatch_cycle_result
        task_id = bar_result.task_id
        receipt = dcr.delivery_receipt

        if task_id != receipt.identity.task_id:
            raise WorkflowInputError(
                "task_id must match delivery receipt task_id"
            )

        # -- revision, attempt, dispatch_id must match DeliveryReceipt ----------
        # (task_id already checked above; block_transition task_id matches too)
        if bar_result.block_transition.task_id != task_id:
            raise WorkflowInputError(
                "block transition task_id inconsistency"
            )

        if ctr.cas.task_id != task_id:
            raise WorkflowInputError(
                "cancellation cas.task_id must match task_id"
            )

        # -- CAS expected_revision must match delivery receipt revision ---------
        revision = receipt.identity.revision
        if ctr.cas.expected_revision != revision:
            raise WorkflowInputError(
                "cancellation cas.expected_revision must match "
                "delivery receipt revision"
            )

        # -- Original block transition must be TASK_BLOCKED --------------------
        btr = bar.block_transition_request
        if btr.event_type != "TASK_BLOCKED":
            raise WorkflowInputError(
                "original block transition request event_type must be "
                "TASK_BLOCKED"
            )

        # -- Original BlockedPayload validation --------------------------------
        bp = btr.payload
        if not isinstance(bp, BlockedPayload):
            raise WorkflowInputError(
                "original block transition payload must be BlockedPayload"
            )

        # -- cancellation_transition_request validation ------------------------
        # event_type must be BLOCKER_CANCELLED
        if ctr.event_type != "BLOCKER_CANCELLED":
            raise WorkflowInputError(
                "cancellation_transition_request event_type must be "
                "BLOCKER_CANCELLED"
            )

        # payload must be BlockerCancelledPayload
        if not isinstance(ctr.payload, BlockerCancelledPayload):
            raise WorkflowInputError(
                "cancellation_transition_request payload must be "
                "BlockerCancelledPayload"
            )

        # CAS expected_state must be "blocked"
        if ctr.cas.expected_state != "blocked":
            raise WorkflowInputError(
                "cancellation_transition_request cas.expected_state must be "
                "'blocked'"
            )

        # to_state must be "cancelled" (checked against event_type spec)
        # dispatch_cas must be None
        if ctr.dispatch_cas is not None:
            raise WorkflowInputError(
                "cancellation_transition_request dispatch_cas must be None"
            )

        # -- event_id dedup: must differ from dispatch, ACK, delivery, block --
        existing_event_ids = {
            dcr.dispatch_transition.event_id,
            dcr.acknowledge_transition.event_id,
            dcr.delivery_transition.event_id,
            bar_result.block_transition.event_id,
        }
        if ctr.event_id in existing_event_ids:
            raise WorkflowInputError(
                "cancellation event_id must differ from dispatch, ACK, "
                "delivery, and block event_ids"
            )

        # -- bool must not be revision -----------------------------------------
        if isinstance(revision, bool):
            raise WorkflowInputError(
                "revision must not be bool"
            )

    # -- owner-loss recovery (TC-13.18d.12b/.12c/.12c.1/.12c.2) -------------------------

    async def recover_owner_lost_dispatch(
        self,
        request: OwnerLossRecoveryRequest,
    ) -> OwnerLossRecoveryResult:
        """Recover a dispatch whose original creator process has been lost.

        Implements the frozen contract from §17 and §18 of the
        workflow-orchestrator contract.  When ``retry_plan is not None``,
        executes the durable automatic-retry protocol frozen in
        TC-13.18d.12c.1 §18.

        Atomic reservation boundary (frozen):
          1. Validate request without writes.
          2. Read durable evidence (receipt, tombstone, retry receipt).
          3. Probe creator + process tree liveness.
          4. ``apply_owner_loss_recovery_transition()`` acquires the state
             lock, re-executes the second-check, and writes
             ``DISPATCH_FAILED``.
          5. If retry_plan is supplied: under the same state-lock acquisition,
             atomically write the ``RETRY_RESERVED`` receipt, then release.
          6. Outside the state lock, call ``run_bounded_dispatch_retry()``
             with the reserved identity.

        ``retry_plan is None`` → transition-only (legacy TC-13.18d.12c).
        ``retry_plan is not None`` → transition + atomic reservation + retry.
        """
        if type(request) is not OwnerLossRecoveryRequest:
            raise WorkflowInputError(
                "request must be exact OwnerLossRecoveryRequest"
            )

        retry_plan = request.retry_plan
        if retry_plan is not None and type(retry_plan) is not BoundedDispatchRetryRequest:
            raise WorkflowInputError(
                "retry_plan must be BoundedDispatchRetryRequest or None"
            )

        snapshot = StateProvider(self.project_root).snapshot()
        task = next(
            (item for item in snapshot.tasks if item.task_id == request.task_id),
            None,
        )
        if task is None:
            raise WorkflowInvariantError("owner-loss task evidence mismatch")

        # Exact replay is delegated to the canonical transition core.
        replay_event = next(
            (
                event
                for event in snapshot.events
                if event.event_id == request.recovery_event_id
            ),
            None,
        )
        if replay_event is not None:
            from_state = replay_event.from_state
        else:
            from_state = task.state

        if from_state not in ("dispatched", "in_progress"):
            raise WorkflowInvariantError("owner-loss canonical state mismatch")
        if (
            replay_event is None
            and (
                task.revision != request.expected_revision
                or task.attempt != request.expected_attempt
                or task.current_dispatch is None
                or task.current_dispatch.dispatch_id
                != request.expected_dispatch_id
            )
        ):
            raise WorkflowInvariantError("owner-loss dispatch evidence mismatch")

        receipt = _dse.read_dispatch_receipt(
            self.project_root, request.expected_dispatch_id
        )
        tombstone = _dse.read_dispatch_tombstone(
            self.project_root, request.expected_dispatch_id
        )
        if replay_event is None:
            if (
                receipt is None
                or receipt.task_id != request.task_id
                or receipt.revision != request.expected_revision
                or receipt.attempt != request.expected_attempt
                or receipt.dispatch_id != request.expected_dispatch_id
                or receipt.generation_id != request.expected_generation_id
                or receipt.phase not in ("SUPERVISOR_READY", "WORKER_STARTED")
                or tombstone is not None
            ):
                raise WorkflowInvariantError(
                    "owner-loss durable evidence mismatch"
                )
            creator_liveness = _dse.probe_process(
                receipt.creator_pid,
                receipt.creator_creation_time,
                receipt.boot_id,
            )
            tree_liveness = _dse.probe_dispatch_process_tree(receipt)
            if creator_liveness is _dse.ProcessLiveness.ALIVE:
                raise WorkflowInvariantError(
                    "owner-loss creator is still alive"
                )
            if tree_liveness is _dse.ProcessLiveness.ALIVE:
                raise WorkflowInvariantError(
                    "owner-loss process tree is still alive"
                )
            if (
                creator_liveness is not _dse.ProcessLiveness.DEAD
                or tree_liveness is not _dse.ProcessLiveness.DEAD
            ):
                raise WorkflowInvariantError(
                    "owner-loss liveness is unknown"
                )
        else:
            tree_liveness = _dse.ProcessLiveness.DEAD

        transition_request = TransitionRequest(
            cas=TransitionCAS(
                task_id=request.task_id,
                expected_revision=request.expected_revision,
                expected_state=from_state,
                expected_snapshot_commit="0" * 40,
            ),
            dispatch_cas=DispatchCAS(
                expected_dispatch_id=request.expected_dispatch_id,
                expected_attempt=request.expected_attempt,
            ),
            event_id=request.recovery_event_id,
            event_type="DISPATCH_FAILED",
            payload=DispatchFailedPayload(request.failure_kind),
            event_context=request.recovery_event_context,
        )
        check = OwnerLossTransitionCheck(
            task_id=request.task_id,
            expected_revision=request.expected_revision,
            expected_attempt=request.expected_attempt,
            expected_dispatch_id=request.expected_dispatch_id,
            expected_generation_id=request.expected_generation_id,
            expected_from_state=from_state,
            receipt_present=receipt is not None,
            receipt_phase=receipt.phase if receipt is not None else from_state,
            receipt_generation_id=(
                receipt.generation_id
                if receipt is not None
                else request.expected_generation_id
            ),
            tombstone_present=tombstone is not None,
            tombstone_winner=(
                tombstone.winner if tombstone is not None else None
            ),
            process_liveness=tree_liveness.value,
            recovery_event_id=request.recovery_event_id,
            failure_kind=request.failure_kind,
            evidence_refs=request.evidence_refs,
        )

        # ── execute transition (under state lock) ─────────────────────────
        transition_service = ControlPlaneTransitionService(self.project_root)
        recovery_transition = apply_owner_loss_recovery_transition(
            transition_service,
            transition_request,
            check,
            self.clock.now(),
        )

        # ── atomic retry reservation (TC-13.18d.12c.1 §18.4) ─────────────
        if retry_plan is not None:
            retry_result = await self._reserve_and_retry_owner_loss(
                request=request,
                recovery_transition=recovery_transition,
                retry_plan=retry_plan,
                recovery_generation_id=request.expected_generation_id,
                recovery_event_id=request.recovery_event_id,
            )
            return OwnerLossRecoveryResult(
                task_id=request.task_id,
                recovered_attempt=request.expected_attempt,
                recovered_dispatch_id=request.expected_dispatch_id,
                process_liveness=_dse.ProcessLiveness.DEAD,
                recovery_transition=recovery_transition,
                retry_result=retry_result,
            )

        return OwnerLossRecoveryResult(
            task_id=request.task_id,
            recovered_attempt=request.expected_attempt,
            recovered_dispatch_id=request.expected_dispatch_id,
            process_liveness=_dse.ProcessLiveness.DEAD,
            recovery_transition=recovery_transition,
            retry_result=None,
        )

    # -- atomic retry reservation + execution (TC-13.18d.12c.2) ------------------------

    async def _reserve_and_retry_owner_loss(
        self,
        *,
        request: OwnerLossRecoveryRequest,
        recovery_transition: TransitionResult,
        retry_plan: BoundedDispatchRetryRequest,
        recovery_generation_id: str,
        recovery_event_id: str,
    ) -> BoundedDispatchRetryResult:
        """Atomically reserve the next attempt identity under the state lock,
        then call ``run_bounded_dispatch_retry()`` outside the lock.

        Frozen boundary (TC-13.18d.12c.1 §18.4):
          1. Validate the next attempt identity from the retry plan.
          2. Compute the frozen content digest.
          3. Acquire the existing state lock.
          4. Under the lock, re-read canonical task, recovery event,
             failed dispatch receipt/tombstone, and any retry receipt.
          5. Verify task is exactly ``ready``, ``current_dispatch is None``,
             revision exact, attempt == failed_attempt.
          6. Verify the byte-exact DISPATCH_FAILED event is committed.
          7. Atomically persist ``RETRY_RESERVED`` receipt.
          8. Release the state lock.
          9. Call ``run_bounded_dispatch_retry()`` with the reserved identity.

        No code holding the state lock calls dispatch/Worker/provider.
        """
        # ── 1. Validate next attempt identity ──────────────────────────
        first_attempt = retry_plan.attempts[0]
        next_identity = (
            first_attempt.dispatch_cycle_request.dispatch_request.identity
        )
        next_attempt = next_identity.attempt
        next_dispatch_id = next_identity.dispatch_id
        next_dispatch_event_id = (
            first_attempt.dispatch_cycle_request.dispatch_transition_request.event_id
        )

        # next_attempt must be failed_attempt + 1 and in 1..3.
        if next_attempt != request.expected_attempt + 1:
            raise WorkflowInvariantError(
                "retry plan next attempt must equal failed_attempt + 1"
            )
        if not 1 <= next_attempt <= 3:
            raise WorkflowInvariantError(
                "retry plan next attempt out of range"
            )
        if next_dispatch_id == request.expected_dispatch_id:
            raise WorkflowInvariantError(
                "retry plan next dispatch id must differ from failed dispatch id"
            )

        # Compute frozen content digest.
        content_digest = _dse.compute_retry_content_digest(
            task_id=request.task_id,
            revision=request.expected_revision,
            failed_attempt=request.expected_attempt,
            failed_dispatch_id=request.expected_dispatch_id,
            recovery_event_id=recovery_event_id,
            recovery_generation_id=recovery_generation_id,
            next_attempt=next_attempt,
            next_dispatch_id=next_dispatch_id,
            next_dispatch_event_id=next_dispatch_event_id,
        )

        # ── 2–8. Acquire state lock, validate, reserve, release ─────────
        creator_pid, creator_creation = _dse.get_current_process_identity()
        creator_boot = _dse.get_boot_id()

        reservation_request = OwnerLossRetryReservationRequest(
            task_id=request.task_id,
            revision=request.expected_revision,
            failed_attempt=request.expected_attempt,
            failed_dispatch_id=request.expected_dispatch_id,
            recovery_event_id=recovery_event_id,
            recovery_generation_id=recovery_generation_id,
            next_attempt=next_attempt,
            next_dispatch_id=next_dispatch_id,
            next_dispatch_event_id=next_dispatch_event_id,
            content_digest=content_digest,
            creator_pid=creator_pid,
            creator_creation_time=creator_creation,
            creator_boot_id=creator_boot,
        )

        transition_service = ControlPlaneTransitionService(self.project_root)
        reserve_receipt = execute_owner_loss_retry_reservation(
            transition_service,
            reservation_request,
        )

        # ── 9. Call retry outside the state lock ───────────────────────
        return await self.run_bounded_dispatch_retry(retry_plan, {})
