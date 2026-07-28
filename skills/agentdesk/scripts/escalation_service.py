"""EscalationService — pure WorkerKind tier-progression policy (TC-13.13b).

A deterministic, in-memory service that evaluates the escalation action
for a given ``WorkerKind``.  It carries no iteration loop, no child process,
no persistent state, and no side effects.

Frozen contract: §2.16 of ``001-mad-agentdesk-integration.md`` (TC-13.13a).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from core_types import WorkerKind

__all__ = [
    "EscalationAction",
    "EscalationRequest",
    "EscalationDecision",
    "evaluate_escalation",
]


@enum.unique
class EscalationAction(str, enum.Enum):
    """Escalation action — exactly two values.

    * ``ESCALATE`` — advance to the next ``WorkerKind`` tier.
    * ``REQUEST_USER_DECISION`` — end of chain; no further tier exists.
    """

    ESCALATE = "escalate"
    REQUEST_USER_DECISION = "request_user_decision"

    def __str__(self) -> str:
        return self.value


# -- private progression mapping (excluded from __all__) ---------------
_PROGRESSION: dict[WorkerKind, WorkerKind | None] = {
    WorkerKind.BASIC_AGENT: WorkerKind.STANDARD_AGENT,
    WorkerKind.STANDARD_AGENT: WorkerKind.ADVANCED_AGENT,
    WorkerKind.ADVANCED_AGENT: WorkerKind.EXPERT_AGENT,
    WorkerKind.EXPERT_AGENT: None,
}


# -- public data classes -----------------------------------------------


@dataclass(frozen=True, slots=True)
class EscalationRequest:
    """Immutable input carrying the current ``WorkerKind``.

    Exactly one field — no more, no less.
    """

    current_worker_kind: WorkerKind

    def __post_init__(self) -> None:
        if not isinstance(self.current_worker_kind, WorkerKind):
            raise TypeError(
                f"current_worker_kind must be a WorkerKind member, "
                f"got {type(self.current_worker_kind).__name__}"
            )
        # Defensive: the isinstance check above rejects bare strings,
        # bools, ints, None, and arbitrary objects.  Only genuine
        # WorkerKind members pass.


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """Immutable result of an escalation evaluation.

    Exactly three fields — no more, no less.

    * ``action`` — ``ESCALATE`` or ``REQUEST_USER_DECISION``.
    * ``current_worker_kind`` — echoed from the request.
    * ``next_worker_kind`` — the next ``WorkerKind`` when escalating,
      or ``None`` when the chain ends.
    """

    action: EscalationAction
    current_worker_kind: WorkerKind
    next_worker_kind: WorkerKind | None

    def __post_init__(self) -> None:
        # -- action type check -------------------------------------------------
        if not isinstance(self.action, EscalationAction):
            raise TypeError(
                f"action must be an EscalationAction member, "
                f"got {type(self.action).__name__}"
            )

        # -- current_worker_kind type check -----------------------------------
        if not isinstance(self.current_worker_kind, WorkerKind):
            raise TypeError(
                f"current_worker_kind must be a WorkerKind member, "
                f"got {type(self.current_worker_kind).__name__}"
            )

        # -- action / next_worker_kind consistency ----------------------------
        if self.action is EscalationAction.ESCALATE:
            if self.next_worker_kind is None:
                raise ValueError(
                    "next_worker_kind must not be None when action is ESCALATE"
                )
            if not isinstance(self.next_worker_kind, WorkerKind):
                raise TypeError(
                    f"next_worker_kind must be a WorkerKind member when "
                    f"action is ESCALATE, "
                    f"got {type(self.next_worker_kind).__name__}"
                )
        elif self.action is EscalationAction.REQUEST_USER_DECISION:
            if self.next_worker_kind is not None:
                raise ValueError(
                    "next_worker_kind must be None when action is "
                    "REQUEST_USER_DECISION"
                )
            if self.current_worker_kind is not WorkerKind.EXPERT_AGENT:
                raise ValueError(
                    "REQUEST_USER_DECISION is only valid when "
                    "current_worker_kind is EXPERT_AGENT"
                )
        else:
            # Defensive — unknown EscalationAction (should be unreachable
            # because the isinstance check above would catch non-members).
            raise ValueError(
                f"unknown EscalationAction: "
                f"{type(self.action).__name__}"
            )


# -- public function ---------------------------------------------------


def evaluate_escalation(
    request: EscalationRequest,
) -> EscalationDecision:
    """Evaluate the escalation action for *request*.

    Args:
        request: Frozen input carrying the current ``WorkerKind``.

    Returns:
        An ``EscalationDecision`` with the action and next ``WorkerKind``.

    Raises:
        TypeError: *request* is not an ``EscalationRequest``.
        ValueError: The ``current_worker_kind`` is not a valid
                    ``WorkerKind`` member.
    """
    if not isinstance(request, EscalationRequest):
        raise TypeError(
            f"request must be an EscalationRequest instance, "
            f"got {type(request).__name__}"
        )

    current: WorkerKind = request.current_worker_kind

    # -- safety: isinstance on WorkerKind already enforced by
    #    EscalationRequest.__post_init__, but verify anyway for
    #    defence-in-depth (e.g. if a frozen dataclass is constructed
    #    via object.__setattr__).
    if not isinstance(current, WorkerKind):
        raise TypeError(
            f"current_worker_kind must be a WorkerKind member, "
            f"got {type(current).__name__}"
        )

    next_kind: WorkerKind | None = _PROGRESSION.get(current, _SENTINEL)
    if next_kind is _SENTINEL:
        raise ValueError(
            f"unknown WorkerKind: {current.name}"
        )

    if next_kind is None:
        action = EscalationAction.REQUEST_USER_DECISION
    else:
        action = EscalationAction.ESCALATE

    return EscalationDecision(
        action=action,
        current_worker_kind=current,
        next_worker_kind=next_kind,
    )


# Sentinel for dict.get fallback; never exposed.
_SENTINEL = object()
