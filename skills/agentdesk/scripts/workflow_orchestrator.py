"""AgentDesk WorkflowOrchestrator — TC-13.18b / TC-13.18b.2 / TC-13.18c.1 / TC-13.18c.2.

Single dispatch-cycle execution: snapshot -> acquire lease ->
TASK_DISPATCHED -> heartbeat + run_worker_observed ->
(DispatchStarted → validate → DISPATCH_ACKNOWLEDGED) ->
Worker exit 0 → decode_worker_result → require_delivery_receipt →
DELIVERY_SUBMITTED → stop heartbeat -> release lease -> result.

Acceptance cycle (TC-13.18c.2): run_audit_gateway → verdict=pass →
acquire review lease → DELIVERY_ACCEPTED → release lease →
optional CHANGE_INTEGRATED → AcceptanceCycleResult.

Non-goals (explicitly excluded):
* DELIVERY_RETURNED, TASK_REQUEUED
* Escalation, retry, automatic blocked/fail remediation
* Codex output decoding (blocked until TC-13.9c.2)
* Parsing stdout/stderr manually, guessing commits from Git HEAD
* Git worktree lifecycle, subprocess invocation, file I/O
* ApprovalGate, hold_worker_slot_fence, .state-transition.lock
* Budget computation (delegated to WorkerAdapter.run_worker)
* ID generation (all identifiers are caller-supplied)
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from control_plane_transition import (
    AcknowledgePayload,
    ControlPlaneTransitionService,
    DeliveryAcceptedPayload,
    DeliverySubmittedPayload,
    DispatchCAS,
    DispatchPayload,
    IntegrationPayload,
    TransitionCAS,
    TransitionEventContext,
    TransitionRequest,
    TransitionResult,
)
from core_types import TaskDifficulty, WorkerKind
from dispatcher_gateway import (
    AgentCliProvider,
    DispatchRequest,
    DispatchStarted,
    DispatchStartedObserver,
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
    "DeliveryReceipt",
    "DispatchCycleRequest",
    "DispatchCycleResult",
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


# -- ACK Observer (internal, used within run_dispatch_cycle) ------------------


class _AckObserver:
    """Internal observer that validates DispatchStarted identity and
    applies DISPATCH_ACKNOWLEDGED under the same WorkerSlotLease.

    Must be callable only while the lease is still valid (before release)
    and heartbeat is active.
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
    ) -> None:
        self._project_root = project_root
        self._ack_tr = ack_transition_request
        self._lease = lease
        self._clock = clock
        self._dispatch_request = dispatch_request
        self._selected_model_provider = selected_model_provider
        self._selected_model_id = selected_model_id
        self._ack_result: TransitionResult | None = None

    @property
    def ack_result(self) -> TransitionResult | None:
        return self._ack_result

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
                "integration_transition_request event_type must be "
                f"CHANGE_INTEGRATED, got {itr.event_type!r}"
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
                "integration_transition_request expected_state must be "
                f"'accepted', got {itr.cas.expected_state!r}"
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
        """Execute a single dispatch cycle with ACK and DELIVERY_SUBMITTED.

        Execution order:
        1. Validate request/providers/Claude 2.1.214
        2. StateProvider.snapshot()
        3. acquire WorkerSlotLease
        4. apply TASK_DISPATCHED
        5. Start heartbeat + complete heartbeat_started Event handshake
        6. run_worker_observed()
        7. Process start → _AckObserver applies DISPATCH_ACKNOWLEDGED
        8. Worker exit 0 → WorkerResult
        9. heartbeat still running
        10. decode_worker_result(worker_result, "2.1.214")
        11. require_delivery_receipt(worker_output)
        12. Construct DELIVERY_SUBMITTED TransitionRequest
        13. Apply DELIVERY_SUBMITTED under same lease
        14. Stop and await heartbeat
        15. Release lease (exactly once)
        16. Return nine-field DispatchCycleResult
        """
        # -- 1. Validate request ----------------------------------------------
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

        # -- 2. Snapshot ------------------------------------------------------
        try:
            snapshot = StateProvider(self.project_root).snapshot()
        except StateProviderError:
            raise  # propagate as-is

        # -- 3. Verify target task --------------------------------------------
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

        # -- 4. Acquire worker slot -------------------------------------------
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
        acquired = True

        # Track the primary body exception for release-priority logic.
        body_error: BaseException | None = None
        dispatch_transition_result: TransitionResult | None = None
        ack_transition_result: TransitionResult | None = None
        delivery_transition_result: TransitionResult | None = None

        # Pre-declare task references so all exit paths can cancel + await.
        worker_task: asyncio.Task[WorkerResult] | None = None
        hb_task: asyncio.Task[None] | None = None

        # Build observer before any async operations that could fail.
        snapshot_sn = dr.model_selection
        ack_observer = _AckObserver(
            project_root=self.project_root,
            ack_transition_request=request.acknowledge_transition_request,
            lease=lease,
            clock=self.clock,
            dispatch_request=dr,
            selected_model_provider=snapshot_sn.selected_model_provider,
            selected_model_id=snapshot_sn.selected_model_id,
        )

        try:
            # -- 5. Apply dispatch transition ---------------------------------
            now_transition = self.clock.now()
            dispatch_transition_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(tr, lease, now_transition)

            # -- 6-7. Start heartbeat first, THEN worker_observed --------------

            heartbeat_started = asyncio.Event()

            async def _heartbeat_loop() -> None:
                """Heartbeat coroutine -- loop until cancelled or error."""
                heartbeat_started.set()
                while True:
                    await self.clock.sleep(self.heartbeat_interval_seconds)
                    now_hb = self.clock.now()
                    renew_worker_slot(self.project_root, lease, now_hb)

            hb_task = asyncio.ensure_future(_heartbeat_loop())

            # Wait for heartbeat to confirm it has entered its loop.
            await heartbeat_started.wait()

            # If heartbeat failed before worker started, propagate immediately.
            if hb_task.done():
                hb_exc = hb_task.exception()
                if hb_exc is not None:
                    if isinstance(hb_exc, WorkerSlotLeaseError):
                        raise hb_exc
                    raise WorkflowHeartbeatError(
                        "heartbeat task failed before worker started"
                    ) from hb_exc
                raise WorkflowHeartbeatError(
                    "heartbeat task terminated before worker started"
                )

            worker_task = asyncio.ensure_future(
                run_worker_observed(
                    dr,
                    request.worker_kind,
                    request.task_difficulty,
                    providers,
                    ack_observer,
                )
            )

            # -- 8. Wait for first completion ---------------------------------
            done, _pending = await asyncio.wait(
                [worker_task, hb_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Heartbeat failure ALWAYS takes priority over Worker success.
            if hb_task in done:
                # Cancel worker first, then examine heartbeat.
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass

                hb_exc = hb_task.exception()
                if hb_exc is not None:
                    if isinstance(hb_exc, WorkerSlotLeaseError):
                        raise hb_exc
                    raise WorkflowHeartbeatError(
                        "heartbeat task failed with unexpected exception"
                    ) from hb_exc

                # Heartbeat ended without exception -- should not happen.
                raise WorkflowHeartbeatError(
                    "heartbeat task terminated unexpectedly"
                )

            # Heartbeat is still running -- Worker completed first.

            # Worker result (may raise if worker failed).
            worker_result = worker_task.result()

            # Collect ACK result from observer.
            ack_transition_result = ack_observer.ack_result
            if ack_transition_result is None:
                raise WorkflowInvariantError(
                    "ACK transition was not applied — observer did not run"
                )

            # -- 9. Decode Worker output --------------------------------------
            worker_output = decode_worker_result(
                worker_result,
                request.provider_cli_version,
            )

            # -- 10. Require delivery receipt ----------------------------------
            delivery_receipt = require_delivery_receipt(worker_output)

            # -- 11. Construct DELIVERY_SUBMITTED transition -------------------
            ack_tr = request.acknowledge_transition_request
            delivery_tr = TransitionRequest(
                cas=TransitionCAS(
                    task_id=tr.cas.task_id,
                    expected_revision=ack_tr.cas.expected_revision,
                    expected_state="in_progress",
                    expected_snapshot_commit=tr.cas.expected_snapshot_commit,
                ),
                dispatch_cas=ack_tr.dispatch_cas,
                event_id=request.delivery_event_id,
                event_type="DELIVERY_SUBMITTED",
                payload=DeliverySubmittedPayload(
                    implementation_commit=delivery_receipt.implementation_commit,
                    report_commit=delivery_receipt.report_commit,
                ),
                event_context=request.delivery_event_context,
            )

            # -- 12. Apply DELIVERY_SUBMITTED under same lease -----------------
            now_delivery = self.clock.now()
            delivery_transition_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(delivery_tr, lease, now_delivery)

            # -- 13. Stop heartbeat --------------------------------------------
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                # Expected -- heartbeat was running, cancellation succeeded.
                pass
            except WorkerSlotLeaseError:
                # Heartbeat renewal was racing with cancellation and lost.
                raise
            except Exception as hb_exc:
                # Heartbeat failed for unexpected reason -- do NOT swallow.
                raise WorkflowHeartbeatError(
                    "heartbeat failed during shutdown"
                ) from hb_exc

            # -- 14. Compute and validate duration ----------------------------
            end_mono = self.clock.monotonic()
            duration = _validate_monotonic_delta(start_mono, end_mono)

            return DispatchCycleResult(
                worker_result=worker_result,
                worker_output=worker_output,
                delivery_receipt=delivery_receipt,
                dispatch_transition=dispatch_transition_result,
                acknowledge_transition=ack_transition_result,
                delivery_transition=delivery_transition_result,
                slot_id=lease.slot_id,
                lease_epoch=lease.lease_epoch,
                duration_seconds=duration,
            )

        except BaseException as exc:
            body_error = exc
            raise
        finally:
            # -- Cancel any still-running subtasks before release -------------
            if hb_task is not None and not hb_task.done():
                hb_task.cancel()
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()

            # Await both to avoid "Task exception was never retrieved".
            if hb_task is not None:
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass
            if worker_task is not None:
                try:
                    await worker_task
                except (asyncio.CancelledError, Exception):
                    pass

            # -- 15. Release (exactly once, if acquired) ----------------------
            if acquired:
                try:
                    now_release = self.clock.now()
                    release_worker_slot(self.project_root, lease, now_release)
                except BaseException as release_exc:
                    if body_error is None:
                        # Success-path release failure -> propagate.
                        raise
                    # Both body and release failed -- body is primary.
                    # Attach release error as __cause__ for diagnostics.
                    raise body_error from release_exc

        # body_error was already re-raised above -- unreachable.
        raise WorkflowInvariantError("unreachable")

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
            return AcceptanceCycleResult(
                task_id=dcr.delivery_receipt.identity.task_id,
                audit_result=audit_result,
                accept_transition=None,
                integrate_transition=None,
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
