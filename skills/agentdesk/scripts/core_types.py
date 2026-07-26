"""AgentDesk shared core integration types (TC-13.4).

Three independent string enumerations used across the MAD–AgentDesk
integration.  Each enum uses ``str, enum.Enum`` so members are both
enum values and Python strings.  Serialised values are lowercase.

Rationale — why three separate enums instead of one combined type:

* ``TaskDifficulty`` describes task-intrinsic complexity, not model
  capability.  It must be decoupled from model-tier labels so that
  future revisions can diverge the two sets without breaking dispatch,
  outbox, or report evidence.
* ``MadDeliberationDepth`` belongs to the ``mad.*`` semantic space
  and uses MAD's public depth values (``fast`` / ``balanced`` /
  ``deep``).  AgentDesk ``deliberation_tier`` (``efficient`` /
  ``balanced`` / ``deep``) maps to provider-specific reasoning
  controls and lives in the model-bindings layer — the two enums are
  separate by design.
* ``WorkerKind`` is a logical slot label.  It carries no provider,
  model revision, thread ID, worktree path, or lease.  Mapping a
  ``WorkerKind`` to a concrete runtime binding is the
  responsibility of WorkerAdapter (TC-13.9) and WorkerSlotLease
  (TC-13.10).

All three use lowercase strings as stable serialisation values.
Unknown values MUST be treated as fail-closed — consumers must not
silently fall back to a default or guess a meaning.

Non-goals (explicitly excluded from this module):

* Budget calculation, context-window arithmetic, or token budgeting
  (→ TC-13.5 ContextBudgetPolicy)
* Model selection, provider binding, or ``select_model.py`` logic
* Any subprocess invocation, CLI call, or filesystem write
* Worker scheduling, slot allocation, lease acquisition, or
  concurrency fencing (→ TC-13.9, TC-13.10)
* Retry, escalation, or rate-limit logic (→ TC-13.13, TC-13.14)
* Cross-enum mapping (TaskDifficulty → WorkerKind, etc.)
* CLI argument parsing or subprocess orchestration
"""

from __future__ import annotations

import enum

__all__ = [
    "TaskDifficulty",
    "MadDeliberationDepth",
    "WorkerKind",
]


class _CoreEnum(str, enum.Enum):
    """Base for strict-str enums: ``str(member) == member.value`` and
    ``isinstance(member, str)`` are both True.
    """

    def __str__(self) -> str:
        return self.value


class TaskDifficulty(_CoreEnum):
    """Task-intrinsic difficulty, independent of model tier or binding.

    Enum values (stable lowercase strings):

    * ``basic`` — low-risk, well-bounded, mechanical or informational
    * ``standard`` — routine single-module implementation, testing,
      documentation, known-pattern fixes
    * ``advanced`` — cross-module implementation, complex debugging,
      ambiguous constraints, security/compat-sensitive work
    * ``expert`` — architecture, critical migrations, high-risk review,
      complex decision support with costly failure

    ``TaskDifficulty`` is **not** a model tier — even though the four
    string values coincide with AgentDesk model-tier labels today, the
    two concepts may diverge in future revisions.
    """

    BASIC = "basic"
    STANDARD = "standard"
    ADVANCED = "advanced"
    EXPERT = "expert"


class MadDeliberationDepth(_CoreEnum):
    """Public deliberation depth recognised by MAD.

    Enum values (stable lowercase strings):

    * ``fast`` — fast, cost-minimising deliberation
    * ``balanced`` — standard engineering-tradeoff deliberation
    * ``deep`` — multi-constraint, long-chain, high-stakes deliberation

    This is **not** an AgentDesk ``deliberation_tier``.  The AgentDesk
    enum is ``efficient / balanced / deep`` and maps to provider-specific
    reasoning controls.  ``fast`` ≠ ``efficient`` — the two enums are
    separate by design.
    """

    FAST = "fast"
    BALANCED = "balanced"
    DEEP = "deep"


class WorkerKind(_CoreEnum):
    """Logical Worker type for dispatch — no provider, model, or lease.

    Enum values (stable lowercase strings):

    * ``basic_agent`` — basic-tier Worker
    * ``standard_agent`` — standard-tier Worker
    * ``advanced_agent`` — advanced-tier Worker
    * ``expert_agent`` — expert-tier Worker

    ``WorkerKind`` is a **logical slot label**, not a concrete runtime
    binding.
    """

    BASIC_AGENT = "basic_agent"
    STANDARD_AGENT = "standard_agent"
    ADVANCED_AGENT = "advanced_agent"
    EXPERT_AGENT = "expert_agent"
