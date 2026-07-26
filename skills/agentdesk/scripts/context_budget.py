"""AgentDesk ContextBudgetPolicy — pure token-budget strategy (TC-13.5.1).

Computes a per-task context budget from ``TaskDifficulty`` and the model's
``context_window_tokens``.  The policy is a frozen arithmetic function:
no I/O, no provider knowledge, no WorkerAdapter mechanics.

The budget is the **smaller** of the percentage-floor result and a
per-difficulty hard cap — this prevents oversized budgets on very large
context windows while still reserving at least 35 % of the window.

Fixed percentages and caps (frozen by ADR #28):

============= ===== ==========
TaskDifficulty    %  Hard cap
============= ===== ==========
BASIC           20    64 000
STANDARD        35   128 000
ADVANCED        50   256 000
EXPERT          65   512 000
============= ===== ==========

At least **35 %** of the context window is always reserved for system
prompt, tool definitions, and overhead.

Input validation is fail-closed: only ``TaskDifficulty`` enum members
and positive non-bool ``int`` window sizes are accepted.  The computed
``budget_tokens`` must be ≥ 1 — callers get a ``ValueError`` for
windows too small to yield a usable budget at the requested difficulty.

All internal-invariant checks use explicit ``raise AssertionError``,
never bare ``assert`` — they must survive ``python -O``.

Non-goals (explicitly excluded from this module):

* WorkerAdapter, slot allocation, lease, or concurrency fencing
* Model selection, provider binding, or ``select_model.py`` logic
* Writing budget results to model-selection, dispatch, outbox, or report
* Any I/O, subprocess, filesystem, environment-variable, or network call
* Custom percentage or cap overrides or configuration files
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
    TaskDifficulty.BASIC: 20,
    TaskDifficulty.STANDARD: 35,
    TaskDifficulty.ADVANCED: 50,
    TaskDifficulty.EXPERT: 65,
}

# ── frozen hard-cap table (module-private) ──────────────────────────────
_BUDGET_CAP: dict[TaskDifficulty, int] = {
    TaskDifficulty.BASIC: 64_000,
    TaskDifficulty.STANDARD: 128_000,
    TaskDifficulty.ADVANCED: 256_000,
    TaskDifficulty.EXPERT: 512_000,
}


class BudgetResult(NamedTuple):
    """Immutable result of a ContextBudgetPolicy computation.

    All fields are read-only; the tuple is hashable and suitable for
    audit-trail snapshots.
    """

    context_window_tokens: int
    difficulty: TaskDifficulty
    budget_percent: int
    budget_cap_tokens: int
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
        ``BudgetResult`` with six frozen fields.

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

    # ── 3. Look up frozen percentage and cap ───────────────────────────
    percent = _BUDGET_PERCENT[difficulty]
    cap = _BUDGET_CAP[difficulty]

    # ── 3a. Validate percent (module-private table, but guard against
    #        corruption — must survive python -O) ───────────────────────
    if isinstance(percent, bool) or not isinstance(percent, int):
        raise AssertionError(
            f"budget percent for {difficulty.value} must be int, "
            f"got {type(percent).__name__}: {percent!r}"
        )
    if not (1 <= percent <= 65):
        raise AssertionError(
            f"budget percent for {difficulty.value} must be in [1, 65], "
            f"got {percent}"
        )

    # ── 3b. Validate cap (module-private table, but guard against
    #        corruption — must survive python -O) ───────────────────────
    if isinstance(cap, bool) or not isinstance(cap, int):
        raise AssertionError(
            f"budget cap for {difficulty.value} must be int, "
            f"got {type(cap).__name__}: {cap!r}"
        )
    if cap < 0:
        raise AssertionError(
            f"budget cap for {difficulty.value} must be >= 0, got {cap}"
        )

    # ── 4. Compute budget: min(floor percentage, hard cap) ─────────────
    percentage_budget = context_window_tokens * percent // 100
    budget_tokens = min(percentage_budget, cap)
    reserved_tokens = context_window_tokens - budget_tokens

    # ── 5. Guard: at least 1 budget token ──────────────────────────────
    if budget_tokens < 1:
        raise ValueError(
            f"context_window_tokens={context_window_tokens} is too small "
            f"for {difficulty.value} budget ({percent}%, cap={cap}): "
            f"budget would be {budget_tokens} token(s)"
        )

    # ── 6. Invariants (explicit raise — survive python -O) ─────────────

    # 6a. budget_tokens <= percentage_budget
    if budget_tokens > percentage_budget:
        raise AssertionError(
            f"invariant broken: budget_tokens={budget_tokens} > "
            f"percentage_budget={percentage_budget} "
            f"(window={context_window_tokens}, difficulty={difficulty.value}, "
            f"percent={percent}, cap={cap})"
        )

    # 6b. budget_tokens <= cap
    if budget_tokens > cap:
        raise AssertionError(
            f"invariant broken: budget_tokens={budget_tokens} > "
            f"cap={cap} "
            f"(window={context_window_tokens}, difficulty={difficulty.value})"
        )

    # 6c. budget_tokens + reserved_tokens == context_window_tokens
    if budget_tokens + reserved_tokens != context_window_tokens:
        raise AssertionError(
            f"invariant broken: budget_tokens={budget_tokens} + "
            f"reserved_tokens={reserved_tokens} != "
            f"context_window_tokens={context_window_tokens}"
        )

    # 6d. reserved_tokens >= ceil(35 %)
    minimum_reserved = (context_window_tokens * 35 + 99) // 100  # ceil(35%)
    if reserved_tokens < minimum_reserved:
        raise AssertionError(
            f"invariant broken: reserved={reserved_tokens} < "
            f"minimum_reserved={minimum_reserved} "
            f"(window={context_window_tokens}, difficulty={difficulty.value}, "
            f"percent={percent}, cap={cap})"
        )

    return BudgetResult(
        context_window_tokens=context_window_tokens,
        difficulty=difficulty,
        budget_percent=percent,
        budget_cap_tokens=cap,
        budget_tokens=budget_tokens,
        reserved_tokens=reserved_tokens,
    )
