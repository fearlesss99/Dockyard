"""AgentDesk WorkflowOrchestrator — TC-13.18b.

Single dispatch-cycle execution: snapshot → acquire lease →
TASK_DISPATCHED → heartbeat + run_worker → stop heartbeat →
release lease → result.

Non-goals (explicitly excluded):
* DISPATCH_ACKNOWLEDGED, DELIVERY_SUBMITTED, or any other transition
* ACK, audit, acceptance, integration, escalation, retry
* Parsing stdout/stderr, decoding provider output
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
    ControlPlaneTransitionService,
    DispatchPayload,
    TransitionRequest,
    TransitionResult,
)
from core_types import TaskDifficulty, WorkerKind
from dispatcher_gateway import AgentCliProvider, DispatchRequest
from state_provider import StateProvider, StateProviderError
from worker_adapter import WorkerResult, run_worker
from worker_slot_lease import (
    WorkerSlotLeaseError,
    acquire_worker_slot,
    release_worker_slot,
    renew_worker_slot,
)

__all__ = [
    "DispatchCycleRequest",
    "DispatchCycleResult",
    "WorkflowClock",
    "WorkflowHeartbeatError",
    "WorkflowInputError",
    "WorkflowInvariantError",
    "WorkflowOrchestrator",
    "WorkflowOrchestratorError",
]


# ── WorkflowClock Protocol ─────────────────────────────────────────────────


class WorkflowClock(Protocol):
    """Injectable clock — never use ``datetime.now()`` / ``time.sleep()``
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


# ── DispatchCycleRequest ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DispatchCycleRequest:
    """Immutable input for a single dispatch cycle.

    All identifiers are caller-supplied — the orchestrator generates none.
    """

    dispatch_request: DispatchRequest
    dispatch_transition_request: TransitionRequest
    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    holder_instance_id: str

    def __post_init__(self) -> None:
        # ── dispatch_request type ──────────────────────────────────────
        if not isinstance(self.dispatch_request, DispatchRequest):
            raise TypeError(
                f"dispatch_request must be DispatchRequest, "
                f"got {type(self.dispatch_request).__name__}"
            )

        # ── dispatch_transition_request type ───────────────────────────
        if not isinstance(self.dispatch_transition_request, TransitionRequest):
            raise TypeError(
                "dispatch_transition_request must be TransitionRequest, "
                f"got {type(self.dispatch_transition_request).__name__}"
            )

        tr = self.dispatch_transition_request

        # ── event_type must be TASK_DISPATCHED ─────────────────────────
        if tr.event_type != "TASK_DISPATCHED":
            raise ValueError(
                "event_type must be TASK_DISPATCHED, "
                f"got {tr.event_type!r}"
            )

        # ── payload must be DispatchPayload ────────────────────────────
        if not isinstance(tr.payload, DispatchPayload):
            raise TypeError(
                "payload must be DispatchPayload, "
                f"got {type(tr.payload).__name__}"
            )

        # ── worker_kind ────────────────────────────────────────────────
        if not isinstance(self.worker_kind, WorkerKind):
            raise TypeError(
                f"worker_kind must be WorkerKind, "
                f"got {type(self.worker_kind).__name__}"
            )

        # ── task_difficulty ────────────────────────────────────────────
        if not isinstance(self.task_difficulty, TaskDifficulty):
            raise TypeError(
                "task_difficulty must be TaskDifficulty, "
                f"got {type(self.task_difficulty).__name__}"
            )

        # ── holder_instance_id ─────────────────────────────────────────
        if not isinstance(self.holder_instance_id, str) or not self.holder_instance_id:
            raise TypeError(
                "holder_instance_id must be a non-empty str, "
                f"got {type(self.holder_instance_id).__name__}"
            )

        dr = self.dispatch_request
        payload = tr.payload

        # ── task_id consistency ────────────────────────────────────────
        if dr.identity.task_id != tr.cas.task_id:
            raise ValueError(
                "dispatch_request task_id must match transition task_id"
            )

        # ── dispatch_id consistency ────────────────────────────────────
        if dr.identity.dispatch_id != payload.dispatch_id:
            raise ValueError(
                "dispatch_request dispatch_id must match "
                "transition payload dispatch_id"
            )

        # ── model_selection identity (same object) ─────────────────────
        if dr.model_selection is not payload.model_selection:
            raise ValueError(
                "dispatch_request model_selection must be the same object "
                "as transition payload model_selection"
            )

        # ── workspace comes from DispatchRequest (validated by Gateway) ─


# ── DispatchCycleResult ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DispatchCycleResult:
    """Immutable result of a single dispatch cycle.

    Does NOT include:
    * delivery_transition
    * implementation_commit / report_commit
    * decoded output / audit result / acceptance result
    """

    worker_result: WorkerResult
    dispatch_transition: TransitionResult
    slot_id: str
    lease_epoch: int
    duration_seconds: float


# ── exception hierarchy ───────────────────────────────────────────────────


class WorkflowOrchestratorError(Exception):
    """Base for all WorkflowOrchestrator errors."""


class WorkflowInputError(WorkflowOrchestratorError):
    """Invalid argument types / values."""


class WorkflowHeartbeatError(WorkflowOrchestratorError):
    """Heartbeat task terminated unexpectedly without a WorkerSlotLeaseError."""


class WorkflowInvariantError(WorkflowOrchestratorError):
    """Internal precondition violated (snapshot, state, or CAS mismatch)."""


# ── WorkflowOrchestrator ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkflowOrchestrator:
    """Pure orchestration facade — delegates all authoritative operations.

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
        # ── project_root: absolute, existing directory ──────────────────
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

        # ── clock: duck-type check (has now, monotonic, sleep) ──────────
        if (
            not callable(getattr(self.clock, "now", None))
            or not callable(getattr(self.clock, "monotonic", None))
            or not callable(getattr(self.clock, "sleep", None))
        ):
            raise WorkflowInputError(
                "clock must implement WorkflowClock protocol"
            )

        # ── heartbeat_interval_seconds: 0 < interval <= 20 ──────────────
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

    # ── public API ────────────────────────────────────────────────────

    async def run_dispatch_cycle(
        self,
        request: DispatchCycleRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> DispatchCycleResult:
        """Execute a single dispatch cycle.

        Execution order:
        1. Validate inputs
        2. Snapshot via StateProvider
        3. Verify target task in snapshot
        4. Acquire worker slot
        5. Apply TASK_DISPATCHED transition
        6-7. Start heartbeat + worker in parallel
        8. Wait for first completion
        9-10. Clean up the other task
        11. Release slot (exactly once on all paths)
        12. Compute duration
        13. Return DispatchCycleResult on success
        """
        # ── 1. Validate request ───────────────────────────────────────
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

        # ── 2. Snapshot ────────────────────────────────────────────────
        try:
            snapshot = StateProvider(self.project_root).snapshot()
        except StateProviderError:
            raise  # propagate as-is

        # ── 3. Verify target task ──────────────────────────────────────
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

        # ── 4. Acquire worker slot ─────────────────────────────────────
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

        try:
            # ── 5. Apply transition ──────────────────────────────────
            now_transition = self.clock.now()
            transition_result = ControlPlaneTransitionService(
                self.project_root
            ).apply_transition(tr, lease, now_transition)

            # ── 6-7. Start heartbeat + worker ────────────────────────

            async def _heartbeat_loop() -> None:
                """Heartbeat coroutine — loop until cancelled or error."""
                try:
                    while True:
                        await self.clock.sleep(self.heartbeat_interval_seconds)
                        now_hb = self.clock.now()
                        # renew_worker_slot is sync (fast file-lock I/O).
                        # WorkerSlotLeaseError propagates as-is.
                        renew_worker_slot(self.project_root, lease, now_hb)
                except asyncio.CancelledError:
                    raise

            worker_coro = run_worker(
                dr,
                request.worker_kind,
                request.task_difficulty,
                providers,
            )

            worker_task = asyncio.ensure_future(worker_coro)
            hb_task = asyncio.ensure_future(_heartbeat_loop())

            # ── 8. Wait for first completion ─────────────────────────
            done, _pending = await asyncio.wait(
                [worker_task, hb_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # ── 9. Worker succeeded first ────────────────────────────
            if worker_task in done:
                # Cancel heartbeat and await it.
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Heartbeat raised non-cancel exception during
                    # shutdown — worker already succeeded, so log-like
                    # absorb.  The next renewal would have failed
                    # anyway, and we're about to release.
                    pass

                # Worker result (may raise if worker failed).
                worker_result = worker_task.result()

                duration = self.clock.monotonic() - start_mono

                return DispatchCycleResult(
                    worker_result=worker_result,
                    dispatch_transition=transition_result,
                    slot_id=lease.slot_id,
                    lease_epoch=lease.lease_epoch,
                    duration_seconds=duration,
                )

            # ── 10. Heartbeat failed first ───────────────────────────
            else:
                # Cancel worker and await it.
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Worker raised during cancellation — heartbeat
                    # failure takes priority.
                    pass

                # Get heartbeat exception (must be non-None since
                # heartbeat completed first and we ruled out success).
                hb_exc = hb_task.exception()
                if hb_exc is not None:
                    if isinstance(hb_exc, WorkerSlotLeaseError):
                        raise hb_exc
                    raise WorkflowHeartbeatError(
                        "heartbeat task failed with unexpected exception"
                    ) from hb_exc

                # Heartbeat ended without exception — should not happen
                # (the loop only exits via exception or cancellation,
                # and cancellation is only triggered by worker success).
                raise WorkflowHeartbeatError(
                    "heartbeat task terminated unexpectedly"
                )

        except BaseException:
            body_error = (
                _current_exception()
            )  # Capture before finally may overwrite.
            raise
        finally:
            # ── 11. Release (exactly once, if acquired) ──────────────
            if acquired:
                try:
                    now_release = self.clock.now()
                    release_worker_slot(self.project_root, lease, now_release)
                except BaseException as release_exc:
                    if body_error is None:
                        # Success-path release failure → propagate.
                        raise
                    # Both body and release failed — keep body as
                    # primary.  Suppress release chain for safety
                    # (WorkerSlotLeaseError messages are already safe).
                    raise body_error from None

        # body_error was already re-raised above — unreachable.
        raise WorkflowInvariantError("unreachable")


def _current_exception() -> BaseException | None:
    """Return the current exception, or None."""
    import sys

    return sys.exc_info()[1]
