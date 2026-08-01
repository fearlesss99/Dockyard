"""Deterministic PortfolioScheduler selection policy — pure function.

No I/O, no clock, no randomness, no mutable state, no model/API/provider calls.
Same input → byte-exact equal output.  Implements the frozen v1 selection
strategy from the PortfolioScheduler durable admission contract (TC-13.24a).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from portfolio_scheduler_store import (
    SCHEMA_VERSION,
    BusinessPriority,
    ConflictKey,
    ConflictKeyClass,
    QueueEntry,
    QueuePhase,
    ReceiptPhase,
    ScheduleReceipt,
    _finish,
    _receipt_digest,
    _receipt_lines,
)
from core_types import WorkerKind

__all__ = [
    "AdvisoryAuthorization",
    "AdmissionContext",
    "select_next",
    "PolicyError",
    "InvalidInputTypeError",
    "DuplicateQueueIdentityError",
    "DuplicateEnqueueSequenceError",
    "InvalidPhaseError",
    "UnknownConflictError",
    "InvalidAdvisoryAuthorizationError",
    "ForbiddenGlobalKeyError",
    "UnsupportedPolicyVersionError",
    "InconsistentOrderKeyInputError",
]


# ── typed errors (Section 10) ──────────────────────────────────────────────


class PolicyError(ValueError):
    """Base policy error — always raised with a typed subclass."""


class InvalidInputTypeError(PolicyError):
    """Snapshot or context is not the expected typed shape."""


class DuplicateQueueIdentityError(PolicyError):
    """Snapshot contains duplicate queue_id values."""


class DuplicateEnqueueSequenceError(PolicyError):
    """Snapshot contains duplicate enqueue_sequence values."""


class InvalidPhaseError(PolicyError):
    """Entry has a phase the policy cannot process."""


class UnknownConflictError(PolicyError):
    """A conflict key classification is unknown — fail-closed."""


class InvalidAdvisoryAuthorizationError(PolicyError):
    """Advisory authorization is malformed or does not match any candidate."""


class ForbiddenGlobalKeyError(PolicyError):
    """A GLOBAL conflict key is present — typed reject."""


class UnsupportedPolicyVersionError(PolicyError):
    """Policy version in admission context is not recognised."""


class InconsistentOrderKeyInputError(PolicyError):
    """Order-key fields are internally inconsistent."""


# ── frozen admission context (Section 5) ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class AdvisoryAuthorization:
    """Explicit advisory authorisation binding.

    Must match by queue identity or by task/revision pair — never a
    boolean global override.
    """

    queue_id: str
    task_id: str
    revision: int
    authorized_by: str

    def __post_init__(self) -> None:
        if not isinstance(self.queue_id, str) or not self.queue_id:
            raise InvalidInputTypeError(
                "portfolio_scheduler:advisory_auth_queue_id_type"
            )
        if not isinstance(self.task_id, str) or not self.task_id:
            raise InvalidInputTypeError(
                "portfolio_scheduler:advisory_auth_task_id_type"
            )
        if type(self.revision) is not int or self.revision < 1:
            raise InvalidInputTypeError(
                "portfolio_scheduler:advisory_auth_revision_type"
            )
        if not isinstance(self.authorized_by, str) or not self.authorized_by:
            raise InvalidInputTypeError(
                "portfolio_scheduler:advisory_auth_authorized_by_type"
            )


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    """Frozen, slotted admission context — only fields the contract requires.

    No deadline, changed paths, model, provider, or TaskDifficulty fields.
    """

    policy_version: str
    evaluated_at: str
    active_hard_conflict_keys: tuple[str, ...]
    advisory_conflict_keys: tuple[str, ...]
    advisory_authorizations: tuple[AdvisoryAuthorization, ...]
    available_worker_kinds: tuple[WorkerKind, ...]
    max_selections: int = 1

    def __post_init__(self) -> None:
        if type(self.policy_version) is not str or not self.policy_version:
            raise InvalidInputTypeError(
                "portfolio_scheduler:policy_version_type"
            )
        if type(self.evaluated_at) is not str or not self.evaluated_at:
            raise InvalidInputTypeError(
                "portfolio_scheduler:evaluated_at_type"
            )
        if type(self.active_hard_conflict_keys) is not tuple:
            raise InvalidInputTypeError(
                "portfolio_scheduler:active_hard_conflict_keys_type"
            )
        if type(self.advisory_conflict_keys) is not tuple:
            raise InvalidInputTypeError(
                "portfolio_scheduler:advisory_conflict_keys_type"
            )
        if type(self.advisory_authorizations) is not tuple:
            raise InvalidInputTypeError(
                "portfolio_scheduler:advisory_authorizations_type"
            )
        if type(self.available_worker_kinds) is not tuple:
            raise InvalidInputTypeError(
                "portfolio_scheduler:available_worker_kinds_type"
            )
        if type(self.max_selections) is not int or self.max_selections < 1:
            raise InvalidInputTypeError(
                "portfolio_scheduler:max_selections_type"
            )

        for idx, key in enumerate(self.active_hard_conflict_keys):
            if type(key) is not str or not key:
                raise InvalidInputTypeError(
                    "portfolio_scheduler:active_hard_conflict_key_type"
                )
        for idx, key in enumerate(self.advisory_conflict_keys):
            if type(key) is not str or not key:
                raise InvalidInputTypeError(
                    "portfolio_scheduler:advisory_conflict_key_type"
                )
        for idx, auth in enumerate(self.advisory_authorizations):
            if type(auth) is not AdvisoryAuthorization:
                raise InvalidInputTypeError(
                    "portfolio_scheduler:advisory_authorization_type"
                )
        for idx, wk in enumerate(self.available_worker_kinds):
            if type(wk) is not WorkerKind:
                raise InvalidInputTypeError(
                    "portfolio_scheduler:available_worker_kind_type"
                )


# ── frozen priority → rank mapping ─────────────────────────────────────────

_PRIORITY_RANK: dict[BusinessPriority, int] = {
    BusinessPriority.P0: 0,
    BusinessPriority.P1: 1,
    BusinessPriority.P2: 2,
    BusinessPriority.P3: 3,
}

_GLOBAL_KEY_CASEFOLD = "global"


# ── input validation ───────────────────────────────────────────────────────


def _validate_snapshot(snapshot: tuple[QueueEntry, ...]) -> None:
    """Fail-fast on malformed snapshot input."""
    if type(snapshot) is not tuple:
        raise InvalidInputTypeError("portfolio_scheduler:snapshot_type")

    seen_queue_ids: set[str] = set()
    seen_sequences: set[int] = set()

    for idx, entry in enumerate(snapshot):
        if type(entry) is not QueueEntry:
            raise InvalidInputTypeError("portfolio_scheduler:snapshot_entry_type")

        if entry.queue_id in seen_queue_ids:
            raise DuplicateQueueIdentityError(
                "portfolio_scheduler:duplicate_queue_id"
            )
        seen_queue_ids.add(entry.queue_id)

        if entry.enqueue_sequence in seen_sequences:
            raise DuplicateEnqueueSequenceError(
                "portfolio_scheduler:duplicate_enqueue_sequence"
            )
        seen_sequences.add(entry.enqueue_sequence)

        # Validate phase — only known phases pass; unknown fail-closed.
        if entry.state not in (
            QueuePhase.QUEUED,
            QueuePhase.SELECTED,
            QueuePhase.DISPATCHED,
            QueuePhase.RETIRED,
        ):
            raise InvalidPhaseError("portfolio_scheduler:unknown_phase")

        # Validate business_priority
        if type(entry.business_priority) is not BusinessPriority:
            raise InvalidInputTypeError(
                "portfolio_scheduler:business_priority_type"
            )

        # Validate no GLOBAL conflict keys
        for ck in entry.conflict_keys:
            if type(ck) is not ConflictKey:
                raise InvalidInputTypeError(
                    "portfolio_scheduler:conflict_key_type"
                )
            if ck.classification is ConflictKeyClass.UNKNOWN:
                raise UnknownConflictError(
                    "portfolio_scheduler:unknown_conflict_key"
                )
            if ck.value.casefold() == _GLOBAL_KEY_CASEFOLD:
                raise ForbiddenGlobalKeyError(
                    "portfolio_scheduler:global_key"
                )


def _validate_admission_context(ctx: AdmissionContext) -> None:
    """Fail on unsupported policy version."""
    if ctx.policy_version != SCHEMA_VERSION:
        raise UnsupportedPolicyVersionError(
            "portfolio_scheduler:unsupported_policy_version"
        )


# ── conflict adjudication ──────────────────────────────────────────────────


def _has_active_hard_conflict(
    entry: QueueEntry, active_hard_keys: frozenset[str]
) -> bool:
    """True when any entry hard_exclusive key is among active hard keys."""
    for ck in entry.conflict_keys:
        if ck.classification is not ConflictKeyClass.HARD_EXCLUSIVE:
            continue
        if ck.value.casefold() in active_hard_keys:
            return True
    return False


def _advisory_entry_keys(entry: QueueEntry) -> frozenset[str]:
    """Casefolded advisory conflict key values for this entry."""
    return frozenset(
        ck.value.casefold()
        for ck in entry.conflict_keys
        if ck.classification is ConflictKeyClass.ADVISORY
    )


def _advisory_authorized(
    entry: QueueEntry,
    advisory_keys: frozenset[str],
    authorizations: tuple[AdvisoryAuthorization, ...],
) -> bool:
    """True when an authorisation precisely matches this entry.

    Authorisation must bind to this entry's queue_id or (task_id, revision).
    No boolean global override.
    """
    for auth in authorizations:
        # Must match advisory key space
        if auth.authorized_by.casefold() not in advisory_keys:
            continue
        # Precise identity binding — queue or task/revision
        if auth.queue_id == entry.queue_id:
            return True
        if auth.task_id == entry.task_id and auth.revision == entry.revision:
            return True
    return False


def _check_advisory_blocked(
    entry: QueueEntry,
    ctx: AdmissionContext,
    active_advisory_keys: frozenset[str],
) -> bool:
    """True when entry is blocked by an unauthorised advisory conflict."""
    entry_advisory = _advisory_entry_keys(entry)
    if not entry_advisory:
        return False

    # Does this entry's advisory keys intersect active advisory keys?
    intersection = entry_advisory & active_advisory_keys
    if not intersection:
        return False

    # If intersection exists, explicit authorization is required
    if _advisory_authorized(entry, intersection, ctx.advisory_authorizations):
        return False

    return True


# ── aging normalisation ────────────────────────────────────────────────────


def _compute_aging_ranks(
    candidates: list[QueueEntry],
) -> dict[str, int]:
    """Compute per-priority aging promotion ranks.

    Within each priority band, entries are sorted by aging_basis_at
    ascending.  The oldest entry gets rank 0 (ordered first — smallest
    tuple value), and newer entries get higher ranks.

    This is the frozen v1 normalisation: pure ordinal within band,
    no cross-priority promotion, no threshold, no clock.
    """
    if not candidates:
        return {}

    # Collect by priority
    by_priority: dict[BusinessPriority, list[QueueEntry]] = {}
    for entry in candidates:
        by_priority.setdefault(entry.business_priority, []).append(entry)

    ranks: dict[str, int] = {}
    for _priority, entries in by_priority.items():
        # Sort by aging_basis_at ascending, then enqueue_sequence as
        # deterministic tie-break for equal timestamps.
        sorted_entries = sorted(
            entries, key=lambda e: (e.aging_basis_at, e.enqueue_sequence)
        )
        for ordinal, entry in enumerate(sorted_entries):
            ranks[entry.queue_id] = ordinal

    return ranks


# ── order key ──────────────────────────────────────────────────────────────


def _order_key(
    entry: QueueEntry,
    aging_rank: int,
) -> tuple[int, int, int, str]:
    """Build the deterministic v1 order key.

    Format: (business_priority_rank, aging_promotion_rank, enqueue_sequence, queue_id)

    Lower values come first in tuple comparison.
    """
    priority_rank = _PRIORITY_RANK[entry.business_priority]
    return (priority_rank, aging_rank, entry.enqueue_sequence, entry.queue_id)


# ── receipt construction ───────────────────────────────────────────────────


def _compute_receipt_digest(receipt: ScheduleReceipt) -> str:
    """Compute content digest for a ScheduleReceipt."""
    return "sha256:" + hashlib.sha256(
        _finish(_receipt_lines(receipt, False))
    ).hexdigest()


def _build_receipt(
    entry: QueueEntry,
    order_key_value: tuple[int, int, int, str],
    evaluated_at: str,
    selection_reason: str,
) -> ScheduleReceipt:
    """Build a deterministic ScheduleReceipt in phase=selected."""
    receipt_id = _deterministic_receipt_id(entry)
    provisional = ScheduleReceipt(
        schema_version=SCHEMA_VERSION,
        receipt_id=receipt_id,
        queue_id=entry.queue_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selected_at=evaluated_at,
        order_key=order_key_value,
        selection_reason=selection_reason,
        worker_kind=entry.worker_kind_request,
        dispatch_event_id=None,
        phase=ReceiptPhase.SELECTED,
        content_digest="sha256:" + "0" * 64,
        selection_generation=entry.selection_generation,
    )
    digest = _compute_receipt_digest(provisional)
    return ScheduleReceipt(
        schema_version=SCHEMA_VERSION,
        receipt_id=receipt_id,
        queue_id=entry.queue_id,
        task_id=entry.task_id,
        revision=entry.revision,
        enqueue_sequence=entry.enqueue_sequence,
        selected_at=evaluated_at,
        order_key=order_key_value,
        selection_reason=selection_reason,
        worker_kind=entry.worker_kind_request,
        dispatch_event_id=None,
        phase=ReceiptPhase.SELECTED,
        content_digest=digest,
        selection_generation=entry.selection_generation,
    )


def _deterministic_receipt_id(entry: QueueEntry) -> str:
    """Derive a deterministic receipt identity from the entry tuple.

    receipt_id is the durable identity for (queue_id, enqueue_sequence,
    selection_generation) per the contract.  This derivation is pure and
    reversible.
    """
    queue_part = entry.queue_id[2:]  # strip "Q-" prefix
    return f"SR-{queue_part}-{entry.enqueue_sequence}-g{entry.selection_generation}"


# ── selection reason ───────────────────────────────────────────────────────


def _build_selection_reason(
    entry: QueueEntry,
    aging_rank: int,
    priority_rank: int,
    is_advisory_authorized: bool,
) -> str:
    """Deterministic selection reason — stable across replays."""
    parts = [f"priority_{entry.business_priority.value}"]
    if aging_rank == 0:
        parts.append("oldest_in_band")
    if is_advisory_authorized:
        parts.append("advisory_authorized")
    parts.append("fifo")
    return ":".join(parts)


# ── core entry point ───────────────────────────────────────────────────────


def select_next(
    queue_snapshot: tuple[QueueEntry, ...],
    admission_context: AdmissionContext,
) -> ScheduleReceipt | None:
    """Deterministic v1 portfolio selection.

    Args:
        queue_snapshot: Immutable, verified tuple of QueueEntry values.
        admission_context: Frozen AdmissionContext with active conflicts,
            authorisations, available worker kinds, and policy version.

    Returns:
        A ScheduleReceipt in phase=selected if a candidate is chosen,
        or None when no eligible candidate exists.

    Same inputs → byte-exact equal output.  No I/O, no clock, no
    randomness, no mutable state, no model/API/provider calls.
    """
    # -- 1. validate inputs ---------------------------------------------------
    if type(admission_context) is not AdmissionContext:
        raise InvalidInputTypeError(
            "portfolio_scheduler:admission_context_type"
        )
    _validate_snapshot(queue_snapshot)
    _validate_admission_context(admission_context)

    # -- 2. filter to queued candidates only ----------------------------------
    candidates = [
        entry
        for entry in queue_snapshot
        if entry.state is QueuePhase.QUEUED
    ]
    if not candidates:
        return None

    # -- 3. pre-compute active key sets ---------------------------------------
    active_hard_keys = frozenset(
        k.casefold() for k in admission_context.active_hard_conflict_keys
    )
    active_advisory_keys = frozenset(
        k.casefold() for k in admission_context.advisory_conflict_keys
    )

    # Worker kind eligibility set
    available_kinds = frozenset(admission_context.available_worker_kinds)

    # -- 4. filter: hard conflicts --------------------------------------------
    eligible: list[QueueEntry] = []
    for entry in candidates:
        if _has_active_hard_conflict(entry, active_hard_keys):
            continue
        eligible.append(entry)

    if not eligible:
        return None

    # -- 5. filter: advisory conflicts ----------------------------------------
    advisory_authorized_entries: set[str] = set()
    blocked: list[QueueEntry] = []
    for entry in eligible:
        if _check_advisory_blocked(entry, admission_context, active_advisory_keys):
            blocked.append(entry)
            continue
        advisory_authorized_entries.add(entry.queue_id)

    eligible = [e for e in eligible if e.queue_id not in {b.queue_id for b in blocked}]

    if not eligible:
        return None

    # -- 6. filter: worker kind eligibility -----------------------------------
    eligible = [e for e in eligible if e.worker_kind_request in available_kinds]

    if not eligible:
        return None

    # -- 7. compute aging ranks -----------------------------------------------
    aging_ranks = _compute_aging_ranks(eligible)

    # -- 8. sort by deterministic order key -----------------------------------
    def sort_key(entry: QueueEntry) -> tuple[int, int, int, str]:
        aging_rank = aging_ranks[entry.queue_id]
        return _order_key(entry, aging_rank)

    eligible.sort(key=sort_key)

    # -- 9. take up to max_selections (v1 = 1) --------------------------------
    selected = eligible[: admission_context.max_selections]
    if not selected:
        return None

    chosen = selected[0]
    chosen_aging_rank = aging_ranks[chosen.queue_id]
    chosen_priority_rank = _PRIORITY_RANK[chosen.business_priority]
    was_advisory_authorized = chosen.queue_id in advisory_authorized_entries

    # -- 10. build receipt ----------------------------------------------------
    order_key_value = _order_key(chosen, chosen_aging_rank)
    selection_reason = _build_selection_reason(
        chosen, chosen_aging_rank, chosen_priority_rank, was_advisory_authorized
    )

    return _build_receipt(
        chosen,
        order_key_value,
        admission_context.evaluated_at,
        selection_reason,
    )
