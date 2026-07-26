"""AgentDesk ContextBudgetPolicy — pure token-budget strategy (TC-13.5).

Computes a per-task context budget from ``TaskDifficulty`` and the model's
``context_window_tokens``.  The policy is a frozen arithmetic function:
no I/O, no provider knowledge, no WorkerAdapter mechanics.

Fixed percentages (frozen by ADR #28):

============= =====
TaskDifficulty  %
============= =====
BASIC          15
STANDARD       30
ADVANCED       50
EXPERT         65
============= =====

At least **35 %** of the context window is always reserved for system
prompt, tool definitions, and overhead.

Input validation is fail-closed: only ``TaskDifficulty`` enum members
and positive non-bool ``int`` window sizes are accepted.  The computed
``budget_tokens`` must be ≥ 1 — callers get a ``ValueError`` for
windows too small to yield a usable budget at the requested difficulty.

Non-goals (explicitly excluded from this module):

* WorkerAdapter, slot allocation, lease, or concurrency fencing
* Model selection, provider binding, or ``select_model.py`` logic
* Writing budget results to model-selection, dispatch, outbox, or report
* Any I/O, subprocess, filesystem, environment-variable, or network call
* Custom percentage overrides or configuration files
* Cross-enum mapping (TaskDifficulty → WorkerKind, etc.)
"""

from __future__ import annotations

from typing import NamedTuple

from core_types import TaskDifficulty

__all__ = [
    "BudgetResult",
    "compute_budget",
]

# ── frozen percentage table (module-private) ────────────────────────────
_BUDGET_PERCENT: dict[TaskDifficulty, int] = {
    TaskDifficulty.BASIC: 15,
    TaskDifficulty.STANDARD: 30,
    TaskDifficulty.ADVANCED: 50,
    TaskDifficulty.EXPERT: 65,
}


class BudgetResult(NamedTuple):
    """Immutable result of a ContextBudgetPolicy computation.

    All fields are read-only; the tuple is hashable and suitable for
    audit-trail snapshots.
    """

    context_window_tokens: int
    difficulty: TaskDifficulty
    budget_percent: int
    budget_tokens: int
    reserved_tokens: int


def compute_budget(
    context_window_tokens: int,
    difficulty: TaskDifficulty,
) -> BudgetResult:
    """Compute a token budget for *difficulty* given the model's window size.

    Args:
        context_window_tokens: Model context-window size from a validated
            ``model-bindings/v2`` binding.  Must be a positive (≥1) non-bool
            ``int``.
        difficulty: A ``TaskDifficulty`` enum member.

    Returns:
        ``BudgetResult`` with five frozen fields.

    Raises:
        TypeError: If *context_window_tokens* is not an ``int`` (or is
            ``bool``), or if *difficulty* is not a ``TaskDifficulty``
            instance.
        ValueError: If *context_window_tokens* ≤ 0, or if the computed
            ``budget_tokens`` rounds to 0 (window too small for the
            requested difficulty).
    """
    # ── 1. Validate difficulty ─────────────────────────────────────────
    if not isinstance(difficulty, TaskDifficulty):
        raise TypeError(
            f"difficulty must be a TaskDifficulty member, "
            f"got {type(difficulty).__name__}: {difficulty!r}"
        )

    # ── 2. Validate context_window_tokens ──────────────────────────────
    # bool is a subclass of int — reject it explicitly first.
    if isinstance(context_window_tokens, bool):
        raise TypeError(
            "context_window_tokens must be a positive integer, got bool"
        )
    if not isinstance(context_window_tokens, int):
        raise TypeError(
            "context_window_tokens must be a positive integer, "
            f"got {type(context_window_tokens).__name__}: "
            f"{context_window_tokens!r}"
        )
    if context_window_tokens <= 0:
        raise ValueError(
            f"context_window_tokens must be >= 1, "
            f"got {context_window_tokens}"
        )

    # ── 3. Compute budget ──────────────────────────────────────────────
    percent = _BUDGET_PERCENT[difficulty]
    budget_tokens = context_window_tokens * percent // 100
    reserved_tokens = context_window_tokens - budget_tokens

    # ── 4. Guard: at least 1 budget token ──────────────────────────────
    if budget_tokens < 1:
        raise ValueError(
            f"context_window_tokens={context_window_tokens} is too small "
            f"for {difficulty.value} budget ({percent}%): "
            f"budget would be {budget_tokens} token(s)"
        )

    # ── 5. Invariant: at least 35 % reserved ───────────────────────────
    minimum_reserved = (context_window_tokens * 35 + 99) // 100  # ceil(35%)
    # This is an assertion, not a branch — the arithmetic guarantees it
    # for all four frozen percentages, so a failure here indicates a bug.
    assert reserved_tokens >= minimum_reserved, (
        f"invariant broken: reserved={reserved_tokens} < "
        f"minimum_reserved={minimum_reserved} "
        f"(window={context_window_tokens}, difficulty={difficulty.value})"
    )

    return BudgetResult(
        context_window_tokens=context_window_tokens,
        difficulty=difficulty,
        budget_percent=percent,
        budget_tokens=budget_tokens,
        reserved_tokens=reserved_tokens,
    )
