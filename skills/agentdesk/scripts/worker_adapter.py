"""AgentDesk WorkerAdapter Core — TC-13.9b.

Single-attempt execution orchestration: validates inputs, computes a
context budget from ``task_difficulty``, dispatches exactly one call
via the Gateway, and returns a frozen ``WorkerResult``.

WorkerKind and TaskDifficulty are independent inputs — budget is selected
by ``task_difficulty`` alone, never derived from ``worker_kind``.

Non-goals (explicitly excluded):
* Re-attempt, escalation, rate-limit, slot/lease, concurrency fencing
* Provider output decoding (Claude JSON / Codex JSONL)
* State persistence, events, outbox, reports, file I/O, version control
* ``executor_model`` construction
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from context_budget import BudgetResult, compute_budget
from core_types import TaskDifficulty, WorkerKind
from dispatcher_gateway import (
    AgentCliProvider,
    DispatchRequest,
    DispatchResult,
    DispatchStartedObserver,
    run_dispatch,
    run_dispatch_observed,
)
from dispatch_supervisor_runner import run_supervised_dispatch

__all__ = ["WorkerResult", "run_worker", "run_worker_observed"]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Immutable four-field result of a single Worker execution.

    Fields echo inputs and capture the budget and raw Gateway result.
    Output decoding is deferred to TC-13.9c.
    """

    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    budget: BudgetResult
    dispatch_result: DispatchResult


async def run_worker(
    request: DispatchRequest,
    worker_kind: WorkerKind,
    task_difficulty: TaskDifficulty,
    providers: Mapping[str, AgentCliProvider],
) -> WorkerResult:
    """Execute a single Worker dispatch.

    Args:
        request: Frozen dispatch input.
        worker_kind: Logical Worker tier label — independent of difficulty.
        task_difficulty: Task-intrinsic difficulty — drives budget selection.
        providers: Call-level explicit provider mapping (must be non-empty).

    Returns:
        ``WorkerResult`` on complete success.

    Raises:
        TypeError: If any input has the wrong type.
        ValueError: If ``providers`` is empty.
        DispatchGatewayError: Any Gateway error propagated as-is.
        asyncio.CancelledError: Propagated without wrapping.
    """
    # ── 1. Validate request type ─────────────────────────────────────
    if not isinstance(request, DispatchRequest):
        raise TypeError(
            f"request must be a DispatchRequest, "
            f"got {type(request).__name__}"
        )

    # ── 2. Validate worker_kind ──────────────────────────────────────
    if not isinstance(worker_kind, WorkerKind):
        raise TypeError(
            "worker_kind must be a WorkerKind member; "
            f"got type {type(worker_kind).__name__}"
        )

    # ── 3. Validate task_difficulty ───────────────────────────────────
    if not isinstance(task_difficulty, TaskDifficulty):
        raise TypeError(
            "task_difficulty must be a TaskDifficulty member; "
            f"got type {type(task_difficulty).__name__}"
        )

    # ── 4. Validate providers ────────────────────────────────────────
    if not isinstance(providers, Mapping):
        raise TypeError(
            f"providers must be a Mapping, "
            f"got {type(providers).__name__}"
        )
    if len(providers) == 0:
        raise ValueError("providers must not be empty")

    # ── 5. Compute budget from task_difficulty (not worker_kind) ─────
    budget = compute_budget(
        request.model_selection.selected_context_window_tokens,
        task_difficulty,
    )

    # ── 6. Dispatch via Gateway ──────────────────────────────────────
    dispatch_result = await run_dispatch(request, providers)

    # ── 7. Return frozen result ──────────────────────────────────────
    return WorkerResult(
        worker_kind=worker_kind,
        task_difficulty=task_difficulty,
        budget=budget,
        dispatch_result=dispatch_result,
    )


async def run_worker_observed(
    request: DispatchRequest,
    worker_kind: WorkerKind,
    task_difficulty: TaskDifficulty,
    providers: Mapping[str, AgentCliProvider],
    observer: DispatchStartedObserver,
) -> WorkerResult:
    """Execute a single Worker dispatch with process-start observation.

    Args:
        request: Frozen dispatch input.
        worker_kind: Logical Worker tier label.
        task_difficulty: Task-intrinsic difficulty.
        providers: Call-level explicit provider mapping.
        observer: Observer notified when the subprocess starts.

    Returns:
        ``WorkerResult`` on complete success.

    Raises:
        TypeError: If any input has the wrong type.
        ValueError: If ``providers`` is empty.
        DispatchGatewayError: Any Gateway error propagated as-is.
        asyncio.CancelledError: Propagated without wrapping.
        Observer exceptions: Propagated as-is, not captured or wrapped.
    """
    # ── 1. Validate request type ─────────────────────────────────────
    if not isinstance(request, DispatchRequest):
        raise TypeError(
            f"request must be a DispatchRequest, "
            f"got {type(request).__name__}"
        )

    # ── 2. Validate worker_kind ──────────────────────────────────────
    if not isinstance(worker_kind, WorkerKind):
        raise TypeError(
            "worker_kind must be a WorkerKind member; "
            f"got type {type(worker_kind).__name__}"
        )

    # ── 3. Validate task_difficulty ───────────────────────────────────
    if not isinstance(task_difficulty, TaskDifficulty):
        raise TypeError(
            "task_difficulty must be a TaskDifficulty member; "
            f"got type {type(task_difficulty).__name__}"
        )

    # ── 4. Validate providers ────────────────────────────────────────
    if not isinstance(providers, Mapping):
        raise TypeError(
            f"providers must be a Mapping, "
            f"got {type(providers).__name__}"
        )
    if len(providers) == 0:
        raise ValueError("providers must not be empty")

    # ── 5. Compute budget from task_difficulty (once) ────────────────
    budget = compute_budget(
        request.model_selection.selected_context_window_tokens,
        task_difficulty,
    )

    # ── 6. Dispatch via Gateway, or via the durable supervisor subprocess
    #       when the caller (Orchestrator) supplies a generation_id on the
    #       observer.  The supervised path closes the "Worker started
    #       without a durable receipt" crash window: the supervisor owns the
    #       provider Worker and writes SUPERVISOR_READY / WORKER_STARTED
    #       receipts before ACK may be applied.  All process launch, pipe
    #       communication, and output forwarding live in the supervisor
    #       runner module behind this typed API.
    if (
        getattr(observer, "generation_id", "")
        and getattr(observer, "project_root", None) is not None
    ):
        dispatch_result = await run_supervised_dispatch(request, providers, observer)
    else:
        dispatch_result = await run_dispatch_observed(request, providers, observer)

    # ── 7. Return frozen result ──────────────────────────────────────
    return WorkerResult(
        worker_kind=worker_kind,
        task_difficulty=task_difficulty,
        budget=budget,
        dispatch_result=dispatch_result,
    )
