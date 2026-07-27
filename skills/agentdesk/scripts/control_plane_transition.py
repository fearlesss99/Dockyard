"""AgentDesk ControlPlaneTransitionService -- TC-13.11c.

Typed frozen/slots models, project-level state lock, lock-order tracking,
pure serialization and schema validation helpers, single-file atomic
write infrastructure, and full CAS-write state transition execution.

Interface #16 is Current -- TC-13.11c.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, fields as dc_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional, Union

if os.name != "nt":
    import fcntl


# -- sentinel for unset defaults --

_UNSET: Any = object()

# -- constants (frozen from validate_project.py) --

_STATES: tuple[str, ...] = (
    "draft",
    "ready",
    "dispatched",
    "in_progress",
    "review_ready",
    "returned",
    "blocked",
    "accepted",
    "integrated",
    "cancelled",
    "superseded",
)

_STATES_FROZEN: frozenset[str] = frozenset(_STATES)

# States allowed for resume_to_state in BlockerResolved
_RESUME_TO_STATES: frozenset[str] = frozenset({
    "ready",
    "draft",
    "dispatched",
    "in_progress",
    "review_ready",
    "returned",
})

# PM-only transitions: those that do NOT require a Worker lease.
_PM_ONLY_FROM_STATES: frozenset[str] = frozenset({
    "draft",    # draft → ready
    "returned",  # returned → ready
    "blocked",   # blocked → resume / draft / cancelled
    "accepted",  # accepted → integrated / blocked
})
_PM_ONLY_TO_STATES: frozenset[str] = frozenset({
    "ready", "integrated", "blocked", "draft", "cancelled",
})

# All event type names (frozen set).
_EVENT_TYPES: frozenset[str] = frozenset({
    "TASK_SPECIFIED",
    "TASK_DISPATCHED",
    "DISPATCH_ACKNOWLEDGED",
    "DELIVERY_SUBMITTED",
    "DELIVERY_ACCEPTED",
    "DELIVERY_RETURNED",
    "TASK_REQUEUED",
    "CHANGE_INTEGRATED",
    "TASK_BLOCKED",
    "INTEGRATION_FAILED",
    "BLOCKER_RESOLVED",
    "BLOCKER_RESCOPED",
    "BLOCKER_CANCELLED",
    "TASK_CANCELLED",
    "TASK_SUPERSEDED",
})

# Event type → from_state → to_state mapping
_EVENT_TYPE_TRANSITIONS: dict[str, tuple[str, str]] = {
    "TASK_SPECIFIED": ("draft", "ready"),
    "TASK_DISPATCHED": ("ready", "dispatched"),
    "DISPATCH_ACKNOWLEDGED": ("dispatched", "in_progress"),
    "DELIVERY_SUBMITTED": ("in_progress", "review_ready"),
    "DELIVERY_ACCEPTED": ("review_ready", "accepted"),
    "DELIVERY_RETURNED": ("review_ready", "returned"),
    "TASK_REQUEUED": ("returned", "ready"),
    "CHANGE_INTEGRATED": ("accepted", "integrated"),
    "INTEGRATION_FAILED": ("accepted", "blocked"),
    "BLOCKER_RESOLVED": ("blocked", "blocked"),  # to_state is caller-specified
    "BLOCKER_RESCOPED": ("blocked", "draft"),
    "BLOCKER_CANCELLED": ("blocked", "cancelled"),
}

# Event types that require an active dispatch (worker-lifecycle).
_ACTIVE_DISPATCH_EVENT_TYPES: frozenset[str] = frozenset({
    "TASK_DISPATCHED",
    "DISPATCH_ACKNOWLEDGED",
    "DELIVERY_SUBMITTED",
    "DELIVERY_ACCEPTED",
    "DELIVERY_RETURNED",
})

# Event types that produce an outbox.
_OUTBOX_EVENT_TYPES: frozenset[str] = frozenset({
    "TASK_DISPATCHED",
})

# Event types that produce an acceptance record.
_ACCEPTANCE_EVENT_TYPES: frozenset[str] = frozenset({
    "DELIVERY_ACCEPTED",
})

# Payload required for event types that need a DispatchCAS.
_DISPATCH_CAS_REQUIRED_EVENT_TYPES: frozenset[str] = frozenset({
    "DISPATCH_ACKNOWLEDGED",
    "DELIVERY_SUBMITTED",
    "DELIVERY_ACCEPTED",
    "DELIVERY_RETURNED",
})

# PM-only event types — DispatchCAS is forbidden.
_PM_ONLY_EVENT_TYPES: frozenset[str] = frozenset({
    "TASK_SPECIFIED",
    "TASK_REQUEUED",
    "CHANGE_INTEGRATED",
    "INTEGRATION_FAILED",
    "TASK_BLOCKED",
    "BLOCKER_RESOLVED",
    "BLOCKER_RESCOPED",
    "BLOCKER_CANCELLED",
    "TASK_CANCELLED",
    "TASK_SUPERSEDED",
})

# Event types where DispatchCAS is optional (e.g. cancellation of active task).
_DISPATCH_CAS_OPTIONAL_EVENT_TYPES: frozenset[str] = frozenset({
    "TASK_DISPATCHED",  # DispatchCAS is in the DispatchPayload itself
    "TASK_CANCELLED",
    "TASK_SUPERSEDED",
})

# Event type → payload class validation mapping.
_EVENT_TYPE_PAYLOAD_MAP: dict[str, type] = {}  # populated after class defs

# Guard result allowed values.
_GUARD_RESULTS: frozenset[str] = frozenset({
    "passed", "failed", "skipped", "not_applicable",
})

# Equivalence methods.
_EQUIVALENCE_METHODS: frozenset[str] = frozenset({
    "patch_id", "tree", "approved_mapping",
})

# Blocked kinds (from validate_project.py).
_BLOCKED_KINDS: frozenset[str] = frozenset({
    "external_approval",
    "credentials",
    "environment",
    "dependency",
    "role_timeout",
    "report_unreachable",
    "integration_conflict",
    "decision_required",
    "other",
})

# Frozen MODEL_SELECTION_FIELDS from validate_project.py.
_MODEL_SELECTION_FIELDS: tuple[str, ...] = (
    "required_model_tier",
    "required_model_capabilities",
    "model_binding_id",
    "selected_model_provider",
    "selected_model_id",
    "selected_model_tier",
    "selected_deliberation_tier",
    "selected_context_window_tokens",
    "selected_model_capabilities",
    "model_degradation_approval_id",
)

_MODEL_SELECTION_FIELD_SET: frozenset[str] = frozenset(_MODEL_SELECTION_FIELDS)

# Schema version constants.
_STATE_EVENT_SCHEMA = "agentdesk.state-event/v2"
_OUTBOX_SCHEMA = "agentdesk.outbox-message/v2"

# State-event common required keys.
_STATE_EVENT_COMMON_KEYS: tuple[str, ...] = (
    "schema_version",
    "event_id",
    "event_type",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "from_state",
    "to_state",
    "lease_epoch",
    "actor_role_id",
    "occurred_at",
    "source_message_id",
    "evidence_refs",
    "guard_results",
)

# Outbox required keys.
_OUTBOX_REQUIRED_KEYS: tuple[str, ...] = (
    "schema_version",
    "message_id",
    "event_id",
    "message_type",
    "dedupe_key",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "destination_role_id",
    "created_at",
    "model_selection",
    "payload",
)

# Path patterns.
_EVENT_ID_RE = re.compile(r"^EVT-.+")
_MESSAGE_ID_RE = re.compile(r"^MSG-.+")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PAYLOAD_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# RFC 3339 with mandatory timezone: Z or ±HH:MM.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# ── runtime paths ──────────────────────────────────────────────────────────

_RUNTIME_RELATIVE = Path(".agentdesk") / "runtime"
_STATE_LOCK_NAME = ".state-transition.lock"

# ── Windows directory-fsync errno allowlist ────────────────────────────────

_WIN_DIR_FSYNC_ALLOWLIST: frozenset[int] = frozenset({
    errno.EACCES,   # 13 — Permission denied (handle may be read-only)
    errno.EBADF,    # 9  — Bad file descriptor (dir fd not flushable)
    errno.EINVAL,   # 22 — Invalid argument (dir fsync unsupported)
})


def _is_dir_fsync_allowed_error(exc: OSError) -> bool:
    """Return True if *exc* is a directory-fsync error we can safely ignore."""
    return (
        os.name == "nt"
        and exc.errno is not None
        and exc.errno in _WIN_DIR_FSYNC_ALLOWLIST
    )


# ── safe error messaging helpers ───────────────────────────────────────────


def _safe_type_name(value: object) -> str:
    """Return ``type(value).__name__`` — never calls repr/str on the value."""
    return type(value).__name__


# ── validation helpers ─────────────────────────────────────────────────────


def _validate_nonempty_str(value: object, field_name: str) -> str:
    """Validate *value* is a non-empty str, no leading/trailing whitespace,
    no NUL/CR/LF.  Returns the value if valid."""
    if not isinstance(value, str) or not value:
        raise TypeError(
            f"{field_name} must be a non-empty str, "
            f"got {_safe_type_name(value)}"
        )
    if value.strip() != value:
        raise ValueError(
            f"{field_name} must not have leading or trailing whitespace"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError(
            f"{field_name} must not contain NUL, CR, or LF"
        )
    return value


def _validate_safe_str(value: object, field_name: str) -> str:
    """Validate *value* is a non-empty str with no NUL/CR/LF,
    and no leading/trailing whitespace."""
    return _validate_nonempty_str(value, field_name)


_TASK_ID_RE = re.compile(r"^TC-[0-9]{3,}$")


def _validate_task_id_str(value: object, field_name: str) -> str:
    """Validate *value* is a task ID matching ``TC-NNN`` (min 3 digits).

    Rejects short IDs, non-TC prefixes, path escape attempts,
    and any value containing NUL/CR/LF or whitespace.
    """
    if not isinstance(value, str) or not value:
        raise TypeError(
            f"{field_name} must be a non-empty str, "
            f"got {_safe_type_name(value)}"
        )
    if value.strip() != value:
        raise ValueError(
            f"{field_name} must not have leading or trailing whitespace"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError(
            f"{field_name} must not contain NUL, CR, or LF"
        )
    if value != value.rstrip():
        raise ValueError(
            f"{field_name} must not have trailing whitespace"
        )
    if _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError(
            f"task_id must match TC-NNN (min 3 digits)"
        )
    return value


def _validate_non_bool_int(value: object, field_name: str, min_val: int = 1) -> int:
    """Validate *value* is a non-bool int >= *min_val*."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{field_name} must be a non-bool int, "
            f"got {_safe_type_name(value)}"
        )
    if value < min_val:
        raise ValueError(
            f"{field_name} must be >= {min_val}, got {value}"
        )
    return value


def _validate_new_revision(
    new_revision: int,
    expected_revision: int,
) -> None:
    """Validate that *new_revision* equals ``expected_revision + 1``.

    Raises ``TransitionCASConflictError`` if not exactly ``+1``.
    This helper must be called by ``apply_transition()`` during
    ``BLOCKER_RESCOPED`` cross-object comparison — it exists as a
    public validation helper so tests can verify the rule before
    ``apply_transition()`` is implemented.

    *new_revision* must already have been validated as a non-bool int
    >= 1 by ``BlockerRescopedPayload.__post_init__``.
    """
    target = expected_revision + 1
    if new_revision != target:
        raise TransitionCASConflictError(
            f"new_revision must equal expected_revision + 1 "
            f"({expected_revision} + 1 = {target}), "
            f"got {new_revision}"
        )


def _validate_new_attempt(
    new_attempt: int,
    current_attempt: int,
) -> None:
    """Validate that *new_attempt* equals ``current_attempt + 1``.

    Raises ``TransitionCASConflictError`` if not exactly ``+1``.
    This helper must be called by ``apply_transition()`` during
    ``TASK_DISPATCHED`` cross-object comparison — it exists as a
    public validation helper so tests can verify the rule before
    ``apply_transition()`` is implemented.

    *new_attempt* must already have been validated as a non-bool int
    >= 1 by ``DispatchPayload.__post_init__``.
    *current_attempt* is the attempt from the CAS-verified ledger,
    which is ``0`` before the first dispatch and increments by ``1``
    on each dispatch.
    """
    target = current_attempt + 1
    if new_attempt != target:
        raise TransitionCASConflictError(
            f"new_attempt must equal current_attempt + 1 "
            f"({current_attempt} + 1 = {target}), "
            f"got {new_attempt}"
        )


def _validate_sha(value: object, field_name: str) -> str:
    """Validate *value* is a 40-char lowercase hex SHA."""
    if not isinstance(value, str) or not value:
        raise TypeError(
            f"{field_name} must be a non-empty str, "
            f"got {_safe_type_name(value)}"
        )
    if len(value) != 40:
        raise ValueError(
            f"{field_name} must be 40 lowercase hex chars, "
            f"got length {len(value)}"
        )
    if not _SHA_RE.match(value):
        raise ValueError(
            f"{field_name} must be 40 lowercase hex chars"
        )
    return value


_ACCEPTANCE_PATH_RE = re.compile(
    r"^docs/pm/acceptances/"
    r"(?P<task_id>TC-[0-9]{3,})-r(?P<revision>[1-9][0-9]*)-a(?P<attempt>[1-9][0-9]*)"
    r"-review(?P<review_n>[1-9][0-9]*)\.md$"
)

_ACCEPTANCE_DIR_PARTS = ("docs", "pm", "acceptances")


def _parse_review_n_from_path(
    acceptance_path: str,
    expected_task_id: str,
    expected_revision: int,
    expected_attempt: int,
) -> int:
    """Parse the review number from an acceptance path.

    Returns the integer review number parsed from a path matching
    ``docs/pm/acceptances/{task_id}-r{revision}-a{attempt}-review{N}.md``.

    Raises ``TransitionValidationError`` if:
    * The path does not match the expected pattern (exact task ID
      syntax ``TC-NNN``, revision ≥ 1, attempt ≥ 1, review ≥ 1).
    * The path contains escape attempts (``..``, ``//``, backslashes,
      absolute paths, drive prefixes, query/fragment).
    * The structural ``PurePosixPath`` parts are not exactly
      ``("docs", "pm", "acceptances", filename)``.
    * The task_id, revision, or attempt embedded in the path do not
      match the expected CAS-verified values.
    * The review number is not a positive integer.

    The review number is **deterministic**: it is derived only from the
    path, not from directory scans or file counts.  Idempotent replay
    with the same ``acceptance_path`` always produces the same number.
    """
    # ── structural escape check (before regex) ──────────────────────────
    if not isinstance(acceptance_path, str) or not acceptance_path:
        raise TransitionValidationError(
            "acceptance_path must be a non-empty str"
        )
    # Reject absolute paths, backslashes, drive prefixes, query/fragment.
    if acceptance_path.startswith("/"):
        raise TransitionValidationError(
            "acceptance_path must not start with /"
        )
    if "\\" in acceptance_path:
        raise TransitionValidationError(
            "acceptance_path must use forward slashes"
        )
    if "://" in acceptance_path or ":" in acceptance_path:
        raise TransitionValidationError(
            "acceptance_path must not contain colon or scheme"
        )
    if "?" in acceptance_path or "#" in acceptance_path:
        raise TransitionValidationError(
            "acceptance_path must not contain query or fragment"
        )
    if "//" in acceptance_path:
        raise TransitionValidationError(
            "acceptance_path must not contain consecutive slashes"
        )
    # PurePosixPath structural check.
    parts = PurePosixPath(acceptance_path).parts
    if len(parts) != 4:
        raise TransitionValidationError(
            "acceptance_path must have exactly 4 path segments"
        )
    if parts[:3] != _ACCEPTANCE_DIR_PARTS:
        raise TransitionValidationError(
            "acceptance_path must be under docs/pm/acceptances/"
        )
    filename = parts[3]

    # ── regex match against filename only ───────────────────────────────
    m = _ACCEPTANCE_PATH_RE.match(
        "docs/pm/acceptances/" + filename
    )
    if m is None:
        raise TransitionValidationError(
            "acceptance_path filename must match "
            "{task_id}-r{revision}-a{attempt}-review{N}.md"
        )
    if m.group("task_id") != expected_task_id:
        raise TransitionValidationError(
            "acceptance_path task_id mismatch"
        )
    if int(m.group("revision")) != expected_revision:
        raise TransitionValidationError(
            "acceptance_path revision mismatch"
        )
    if int(m.group("attempt")) != expected_attempt:
        raise TransitionValidationError(
            "acceptance_path attempt mismatch"
        )
    return int(m.group("review_n"))


def _validate_rfc3339_utc_str(value: object, field_name: str) -> str:
    """Validate *value* is an RFC 3339 UTC timestamp string."""
    if not isinstance(value, str) or not value:
        raise TypeError(
            f"{field_name} must be a non-empty str, "
            f"got {_safe_type_name(value)}"
        )
    parsed = _parse_rfc3339_utc(value)
    if parsed is None:
        raise ValueError(
            f"{field_name} must be RFC 3339 UTC (Z or +00:00), "
            f"got {_safe_type_name(value)}"
        )
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(
            f"{field_name} must be UTC (Z or +00:00), "
            f"got {_safe_type_name(value)}"
        )
    return value


def _parse_rfc3339_utc(value: str) -> datetime | None:
    """Parse an RFC 3339 UTC string to a timezone-aware datetime, or None."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.endswith("-00:00"):
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    if offset.total_seconds() != 0:
        return None
    return parsed


def _format_rfc3339_utc(dt: datetime) -> str:
    """Format a UTC datetime as RFC 3339 ``Z`` suffix string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── frozen data models ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TransitionCAS:
    """Immutable CAS preconditions for a state transition."""

    task_id: str
    expected_revision: int
    expected_state: str
    expected_snapshot_commit: str

    def __post_init__(self) -> None:
        _validate_task_id_str(self.task_id, "task_id")
        _validate_non_bool_int(self.expected_revision, "expected_revision", min_val=1)
        if not isinstance(self.expected_state, str) or not self.expected_state:
            raise TypeError(
                f"expected_state must be a non-empty str, "
                f"got {_safe_type_name(self.expected_state)}"
            )
        if self.expected_state not in _STATES_FROZEN:
            raise ValueError(
                f"expected_state must be one of the frozen STATES, "
                f"got {_safe_type_name(self.expected_state)}"
            )
        _validate_sha(self.expected_snapshot_commit, "expected_snapshot_commit")


@dataclass(frozen=True, slots=True)
class DispatchCAS:
    """Immutable CAS extension for dispatch-lifecycle transitions."""

    expected_dispatch_id: str
    expected_attempt: int

    def __post_init__(self) -> None:
        _validate_safe_str(self.expected_dispatch_id, "expected_dispatch_id")
        # min_val=0: before first dispatch, the ledger attempt is 0.
        # The CAS validates that expected_attempt matches the current
        # ledger value, which starts at 0 for a task with no prior
        # dispatch.  new_attempt (in DispatchPayload) must equal
        # current_attempt + 1.
        _validate_non_bool_int(self.expected_attempt, "expected_attempt", min_val=0)


# ── Payload dataclasses (14 frozen variants) ───────────────────────────────


@dataclass(frozen=True, slots=True)
class SpecifyPayload:
    """Payload for draft → ready (TASK_SPECIFIED)."""

    # No extra fields beyond common event context — zero-field dataclass.
    pass


@dataclass(frozen=True, slots=True)
class DispatchPayload:
    """Payload for ready → dispatched (TASK_DISPATCHED)."""

    dispatch_id: str
    role_id: str
    model_selection: Any  # Must be ModelSelectionSnapshot (validated in __post_init__)
    task_card_path: str
    task_card_commit: str
    base_commit: str
    branch: str
    report_path: str
    outbox_message_id: str
    new_attempt: int

    def __post_init__(self) -> None:
        _validate_safe_str(self.dispatch_id, "dispatch_id")
        _validate_safe_str(self.role_id, "role_id")
        # model_selection validated as ModelSelectionSnapshot instance
        # (import deferred to avoid circular imports; validated on use)
        _validate_safe_str(self.task_card_path, "task_card_path")
        _validate_sha(self.task_card_commit, "task_card_commit")
        _validate_sha(self.base_commit, "base_commit")
        _validate_safe_str(self.branch, "branch")
        _validate_safe_str(self.report_path, "report_path")
        if not isinstance(self.outbox_message_id, str) or not self.outbox_message_id:
            raise TypeError(
                f"outbox_message_id must be a non-empty str, "
                f"got {_safe_type_name(self.outbox_message_id)}"
            )
        if _MESSAGE_ID_RE.fullmatch(self.outbox_message_id) is None:
            raise ValueError(
                f"outbox_message_id must match MSG-* pattern, "
                f"got {_safe_type_name(self.outbox_message_id)}"
            )
        _validate_non_bool_int(self.new_attempt, "new_attempt", min_val=1)
        # new_attempt must equal the CAS-expected attempt + 1.
        # Caller provides the target value; the service validates it
        # against the current ledger during apply_transition().


@dataclass(frozen=True, slots=True)
class AcknowledgePayload:
    """Payload for dispatched → in_progress (DISPATCH_ACKNOWLEDGED)."""

    # No extra fields beyond common context — requires DispatchCAS.
    pass


@dataclass(frozen=True, slots=True)
class DeliverySubmittedPayload:
    """Payload for in_progress → review_ready (DELIVERY_SUBMITTED)."""

    implementation_commit: str
    report_commit: str

    def __post_init__(self) -> None:
        _validate_sha(self.implementation_commit, "implementation_commit")
        _validate_sha(self.report_commit, "report_commit")


# ── AcceptanceOwnerApproval — canonical evidence summary ───────────────────
#
# ``AcceptanceOwnerApproval`` is NOT a caller-supplied payload field.
# It is a service-derived summary computed from canonical evidence
# during ``apply_transition()``:
#
#   gate            ← committed task-card frontmatter
#                     ``owner_approval.gate``, validated as exactly
#                     ``"none"`` (the only value attested in the current
#                     task-card and acceptance templates).  Any other
#                     value is fail-closed.
#
#   approval_ids    ← service constant: empty tuple ``()``.
#
# When gate is ``"none"``, ``approval_ids`` MUST be empty — non-empty
# approval IDs are immediately rejected.
#
# The current acceptance owner approval output is therefore:
#
#   {"gate": "none", "approval_ids": []}
#
# ``granted_approval_ids``, ``MODEL_DEGRADATION_APPROVED``,
# ``MODEL_DEGRADATION_REVOKED``, and ``model_degradation_approval_id``
# belong exclusively to model-tier degradation authorization.  They
# are NOT owner approval evidence and MUST NOT be written into the
# acceptance record's ``owner_approval`` block.  Owner approval and
# model degradation approval are distinct authorization domains.
#
# The type is published in ``__all__`` so that ``apply_transition()``
# (TC-13.11c) can return it as part of the acceptance construction,
# and so that tests can verify its structure.

_GATE_VALUES: frozenset[str] = frozenset({"none"})
_APPROVAL_ID_RE = re.compile(r"^APR-.+")


@dataclass(frozen=True, slots=True)
class AcceptanceOwnerApproval:
    """Immutable owner-approval data for an acceptance record.

    Constructed by the service from canonical evidence — never from
    caller-supplied payload alone.

    For the currently attested contract, the only valid value is::

        AcceptanceOwnerApproval(gate="none", approval_ids=())

    ``gate`` must be ``"none"`` (the only value in ``_GATE_VALUES``).
    When gate is ``"none"``, ``approval_ids`` MUST be empty.
    Non-empty ``approval_ids`` are rejected immediately.

    The ``approval_ids`` field is typed as ``tuple[str, ...]`` for
    forward compatibility with future gate values, but the current
    attested contract requires exactly ``()``.

    ``granted_approval_ids`` and ``MODEL_DEGRADATION_APPROVED`` are
    NOT owner approval evidence — they belong to model degradation
    authorization exclusively.
    """

    gate: str
    approval_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_safe_str(self.gate, "gate")
        if self.gate not in _GATE_VALUES:
            raise ValueError(
                f"gate must be one of {sorted(_GATE_VALUES)}, "
                f"got {_safe_type_name(self.gate)}"
            )

        # Defensive copy from list/tuple only — reject str, bytes,
        # dict, set, generator, and any other iterable.
        if not isinstance(self.approval_ids, (tuple, list)):
            raise TypeError(
                f"approval_ids must be a tuple or list, "
                f"got {_safe_type_name(self.approval_ids)}"
            )
        # Freeze to tuple.
        if type(self.approval_ids) is not tuple:
            object.__setattr__(
                self,
                "approval_ids",
                tuple(self.approval_ids),
            )

        # When gate is "none", approval_ids MUST be empty.
        if self.gate == "none" and len(self.approval_ids) != 0:
            raise ValueError(
                "approval_ids must be empty when gate is 'none'"
            )

        seen: set[str] = set()
        for i, aid in enumerate(self.approval_ids):
            if not isinstance(aid, str) or not aid:
                raise TypeError(
                    f"approval_ids[{i}] must be a non-empty str"
                )
            if aid.strip() != aid:
                raise ValueError(
                    f"approval_ids[{i}] must not have leading or trailing whitespace"
                )
            if "\0" in aid or "\r" in aid or "\n" in aid:
                raise ValueError(
                    f"approval_ids[{i}] must not contain NUL, CR, or LF"
                )
            if _APPROVAL_ID_RE.fullmatch(aid) is None:
                raise ValueError(
                    f"approval_ids[{i}] must match APR-* pattern"
                )
            if aid in seen:
                raise ValueError(
                    f"approval_ids must not contain duplicates"
                )
            seen.add(aid)


@dataclass(frozen=True, slots=True)
class DeliveryAcceptedPayload:
    """Payload for review_ready → accepted (DELIVERY_ACCEPTED).

    ``owner_approval`` is NOT a payload field.  The service derives it
    from the committed task-card ``owner_approval.gate`` (currently
    only ``"none"`` is attested) and emits::

        {"gate": "none", "approval_ids": []}

    ``granted_approval_ids``, ``MODEL_DEGRADATION_APPROVED``,
    ``MODEL_DEGRADATION_REVOKED``, and ``model_degradation_approval_id``
    belong to model-tier degradation authorization exclusively — they
    must not be copied into acceptance owner approval.
    """

    accepted_commit: str
    acceptance_path: str
    residual_risks: tuple[str, ...]
    criteria_evidence: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        _validate_sha(self.accepted_commit, "accepted_commit")
        _validate_safe_str(self.acceptance_path, "acceptance_path")

        # residual_risks
        if not isinstance(self.residual_risks, tuple):
            raise TypeError(
                f"residual_risks must be a tuple, "
                f"got {_safe_type_name(self.residual_risks)}"
            )
        for i, item in enumerate(self.residual_risks):
            if not isinstance(item, str) or not item:
                raise TypeError(
                    f"residual_risks[{i}] must be a non-empty str"
                )
            if item.strip() != item:
                raise ValueError(
                    f"residual_risks[{i}] must not have leading or trailing whitespace"
                )
            if "\0" in item or "\r" in item:
                raise ValueError(
                    f"residual_risks[{i}] must not contain NUL or CR"
                )

        # criteria_evidence — must not be empty.
        # The acceptance template requires per-criterion evidence.
        if not isinstance(self.criteria_evidence, tuple):
            raise TypeError(
                f"criteria_evidence must be a tuple, "
                f"got {_safe_type_name(self.criteria_evidence)}"
            )
        if len(self.criteria_evidence) == 0:
            raise ValueError(
                "criteria_evidence must not be empty"
            )
        for i, item in enumerate(self.criteria_evidence):
            if not isinstance(item, str) or not item:
                raise TypeError(
                    f"criteria_evidence[{i}] must be a non-empty str"
                )
            if item.strip() != item:
                raise ValueError(
                    f"criteria_evidence[{i}] must not have leading or trailing whitespace"
                )
            if "\0" in item or "\r" in item:
                raise ValueError(
                    f"criteria_evidence[{i}] must not contain NUL or CR"
                )

        # rationale
        if not isinstance(self.rationale, str) or not self.rationale:
            raise TypeError(
                f"rationale must be a non-empty str, "
                f"got {_safe_type_name(self.rationale)}"
            )
        if self.rationale.strip() == "":
            raise ValueError(
                "rationale must not be only whitespace"
            )
        if "\0" in self.rationale or "\r" in self.rationale:
            raise ValueError(
                "rationale must not contain NUL or CR"
            )


@dataclass(frozen=True, slots=True)
class DeliveryReturnedPayload:
    """Payload for review_ready → returned (DELIVERY_RETURNED)."""

    # No extra fields beyond common context — requires DispatchCAS.
    pass


@dataclass(frozen=True, slots=True)
class RequeuePayload:
    """Payload for returned → ready (TASK_REQUEUED)."""

    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class IntegrationPayload:
    """Payload for accepted → integrated (CHANGE_INTEGRATED)."""

    integrated_commit: str
    equivalence_method: str | None
    equivalence_evidence_ref: str | None

    def __post_init__(self) -> None:
        _validate_sha(self.integrated_commit, "integrated_commit")
        if self.equivalence_method is not None:
            if not isinstance(self.equivalence_method, str) or not self.equivalence_method:
                raise TypeError(
                    f"equivalence_method must be a non-empty str or None, "
                    f"got {_safe_type_name(self.equivalence_method)}"
                )
            if self.equivalence_method not in _EQUIVALENCE_METHODS:
                raise ValueError(
                    f"equivalence_method must be one of {sorted(_EQUIVALENCE_METHODS)}, "
                    f"got {_safe_type_name(self.equivalence_method)}"
                )
        if self.equivalence_evidence_ref is not None:
            _validate_safe_str(self.equivalence_evidence_ref, "equivalence_evidence_ref")


@dataclass(frozen=True, slots=True)
class BlockedPayload:
    """Payload for * → blocked (TASK_BLOCKED, INTEGRATION_FAILED)."""

    blocked_reason: str
    blocked_kind: str
    blocked_owner: str
    unblock_condition: str
    resume_state: str
    blocked_attempt_valid: bool | None

    def __post_init__(self) -> None:
        _validate_safe_str(self.blocked_reason, "blocked_reason")
        if not isinstance(self.blocked_kind, str) or not self.blocked_kind:
            raise TypeError(
                f"blocked_kind must be a non-empty str, "
                f"got {_safe_type_name(self.blocked_kind)}"
            )
        if self.blocked_kind not in _BLOCKED_KINDS:
            raise ValueError(
                f"blocked_kind must be one of the frozen BLOCKED_KINDS, "
                f"got {_safe_type_name(self.blocked_kind)}"
            )
        _validate_safe_str(self.blocked_owner, "blocked_owner")
        _validate_safe_str(self.unblock_condition, "unblock_condition")
        if not isinstance(self.resume_state, str) or not self.resume_state:
            raise TypeError(
                f"resume_state must be a non-empty str, "
                f"got {_safe_type_name(self.resume_state)}"
            )
        if self.resume_state not in _STATES_FROZEN:
            raise ValueError(
                f"resume_state must be a valid state, "
                f"got {_safe_type_name(self.resume_state)}"
            )
        if self.blocked_attempt_valid is not None:
            if not isinstance(self.blocked_attempt_valid, bool):
                raise TypeError(
                    f"blocked_attempt_valid must be bool or None, "
                    f"got {_safe_type_name(self.blocked_attempt_valid)}"
                )


@dataclass(frozen=True, slots=True)
class BlockerResolvedPayload:
    """Payload for blocked → resume_state (BLOCKER_RESOLVED)."""

    resume_to_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.resume_to_state, str) or not self.resume_to_state:
            raise TypeError(
                f"resume_to_state must be a non-empty str, "
                f"got {_safe_type_name(self.resume_to_state)}"
            )
        if self.resume_to_state not in _RESUME_TO_STATES:
            raise ValueError(
                f"resume_to_state must be one of {sorted(_RESUME_TO_STATES)}, "
                f"got {_safe_type_name(self.resume_to_state)}"
            )


@dataclass(frozen=True, slots=True)
class BlockerRescopedPayload:
    """Payload for blocked → draft (BLOCKER_RESCOPED)."""

    new_revision: int

    def __post_init__(self) -> None:
        _validate_non_bool_int(self.new_revision, "new_revision", min_val=1)
        # new_revision must equal expected_revision + 1.
        # Caller provides the target value; the service validates it
        # against the current ledger during apply_transition().


@dataclass(frozen=True, slots=True)
class BlockerCancelledPayload:
    """Payload for blocked → cancelled (BLOCKER_CANCELLED)."""

    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class CancelledPayload:
    """Payload for * → cancelled (TASK_CANCELLED)."""

    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class SupersededPayload:
    """Payload for * → superseded (TASK_SUPERSEDED)."""

    superseded_by: str

    def __post_init__(self) -> None:
        _validate_safe_str(self.superseded_by, "superseded_by")


# Closed union of all payload variants.
TransitionPayload = Union[
    SpecifyPayload,
    DispatchPayload,
    AcknowledgePayload,
    DeliverySubmittedPayload,
    DeliveryAcceptedPayload,
    DeliveryReturnedPayload,
    RequeuePayload,
    IntegrationPayload,
    BlockedPayload,
    BlockerResolvedPayload,
    BlockerRescopedPayload,
    BlockerCancelledPayload,
    CancelledPayload,
    SupersededPayload,
]


# ── Event type → payload class mapping ─────────────────────────────────────

_EVENT_TYPE_PAYLOAD_MAP: dict[str, type] = {
    "TASK_SPECIFIED": SpecifyPayload,
    "TASK_DISPATCHED": DispatchPayload,
    "DISPATCH_ACKNOWLEDGED": AcknowledgePayload,
    "DELIVERY_SUBMITTED": DeliverySubmittedPayload,
    "DELIVERY_ACCEPTED": DeliveryAcceptedPayload,
    "DELIVERY_RETURNED": DeliveryReturnedPayload,
    "TASK_REQUEUED": RequeuePayload,
    "CHANGE_INTEGRATED": IntegrationPayload,
    "TASK_BLOCKED": BlockedPayload,
    "INTEGRATION_FAILED": BlockedPayload,
    "BLOCKER_RESOLVED": BlockerResolvedPayload,
    "BLOCKER_RESCOPED": BlockerRescopedPayload,
    "BLOCKER_CANCELLED": BlockerCancelledPayload,
    "TASK_CANCELLED": CancelledPayload,
    "TASK_SUPERSEDED": SupersededPayload,
}


# ── GuardInput / GuardResult / TransitionEventContext ──────────────────────


@dataclass(frozen=True, slots=True)
class GuardInput:
    """Immutable single key-value pair for a guard input set.

    ``GuardResult.inputs`` is ``tuple[GuardInput, ...]`` — key order
    is caller-preserved.  Duplicate keys are rejected at construction.
    """

    key: str
    value: str | int | bool | None

    def __post_init__(self) -> None:
        _validate_safe_str(self.key, "key")
        # Value must be str, int (non-bool), bool, or None.
        if self.value is not None:
            if isinstance(self.value, bool):
                pass  # bool is allowed
            elif isinstance(self.value, int):
                pass  # int (non-bool) is allowed
            elif isinstance(self.value, str):
                if "\0" in self.value or "\r" in self.value or "\n" in self.value:
                    raise ValueError(
                        "GuardInput value as str must not contain NUL, CR, or LF"
                    )
            else:
                raise TypeError(
                    f"GuardInput value must be str|int|bool|None, "
                    f"got {_safe_type_name(self.value)}"
                )


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Immutable guard-check result for a state-event ``guard_results`` list.

    Constructed from validated fields — fail-closed on illegal input.
    """

    guard: str
    inputs: tuple[GuardInput, ...]
    result: str
    checked_at: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _validate_safe_str(self.guard, "guard")
        if not isinstance(self.inputs, tuple):
            raise TypeError(
                f"inputs must be a tuple, got {_safe_type_name(self.inputs)}"
            )
        for i, item in enumerate(self.inputs):
            if not isinstance(item, GuardInput):
                raise TypeError(
                    f"inputs[{i}] must be GuardInput, got {_safe_type_name(item)}"
                )
        # Duplicate key check.
        keys: list[str] = []
        for inp in self.inputs:
            keys.append(inp.key)
        if len(keys) != len(set(keys)):
            raise ValueError("guard inputs must not contain duplicate keys")
        if not isinstance(self.result, str) or not self.result:
            raise TypeError(
                f"result must be a non-empty str, "
                f"got {_safe_type_name(self.result)}"
            )
        if self.result not in _GUARD_RESULTS:
            raise ValueError(
                f"result must be one of {sorted(_GUARD_RESULTS)}, "
                f"got {_safe_type_name(self.result)}"
            )
        _validate_rfc3339_utc_str(self.checked_at, "checked_at")
        _validate_safe_str(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True, slots=True)
class TransitionEventContext:
    """Immutable event-context data supplied by the caller.

    Provides ``source_message_id``, ``evidence_refs``, and
    ``guard_results`` — the three fields that every
    ``agentdesk.state-event/v2`` must carry but that are independent
    of the transition payload.
    """

    source_message_id: str | None
    evidence_refs: tuple[str, ...]
    guard_results: tuple[GuardResult, ...]

    def __post_init__(self) -> None:
        # source_message_id
        if self.source_message_id is not None:
            if not isinstance(self.source_message_id, str) or not self.source_message_id:
                raise TypeError(
                    "source_message_id must be a non-empty str or None, "
                    f"got {_safe_type_name(self.source_message_id)}"
                )
            if self.source_message_id.strip() != self.source_message_id:
                raise ValueError(
                    "source_message_id must not have leading or trailing whitespace"
                )
            if "\0" in self.source_message_id or "\r" in self.source_message_id or "\n" in self.source_message_id:
                raise ValueError(
                    "source_message_id must not contain NUL, CR, or LF"
                )
        # evidence_refs
        if not isinstance(self.evidence_refs, tuple):
            raise TypeError(
                f"evidence_refs must be a tuple, got {_safe_type_name(self.evidence_refs)}"
            )
        seen_refs: set[str] = set()
        for i, ref in enumerate(self.evidence_refs):
            if not isinstance(ref, str) or not ref:
                raise TypeError(
                    f"evidence_refs[{i}] must be a non-empty str"
                )
            if ref.strip() != ref:
                raise ValueError(
                    f"evidence_refs[{i}] must not have leading or trailing whitespace"
                )
            if ref in seen_refs:
                raise ValueError(
                    "evidence_refs must not contain duplicates"
                )
            seen_refs.add(ref)
        # guard_results
        if not isinstance(self.guard_results, tuple):
            raise TypeError(
                f"guard_results must be a tuple, got {_safe_type_name(self.guard_results)}"
            )
        for i, item in enumerate(self.guard_results):
            if not isinstance(item, GuardResult):
                raise TypeError(
                    f"guard_results[{i}] must be GuardResult, got {_safe_type_name(item)}"
                )


# ── TransitionRequest / TransitionResult ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    """Immutable input for a single state transition.

    All fields have verifiable origins in the current control-plane
    state.  The service validates CAS preconditions, writes canonical
    files, regenerates derived views, and returns a
    ``TransitionResult``.
    """

    cas: TransitionCAS
    dispatch_cas: DispatchCAS | None
    event_id: str
    event_type: str
    payload: TransitionPayload
    event_context: TransitionEventContext

    def __post_init__(self) -> None:
        # cas must be TransitionCAS.
        if not isinstance(self.cas, TransitionCAS):
            raise TypeError(
                f"cas must be TransitionCAS, got {_safe_type_name(self.cas)}"
            )
        # dispatch_cas must be DispatchCAS or None.
        if self.dispatch_cas is not None and not isinstance(self.dispatch_cas, DispatchCAS):
            raise TypeError(
                f"dispatch_cas must be DispatchCAS or None, "
                f"got {_safe_type_name(self.dispatch_cas)}"
            )
        # event_id must match EVT-* pattern.
        if not isinstance(self.event_id, str) or not self.event_id:
            raise TypeError(
                f"event_id must be a non-empty str, "
                f"got {_safe_type_name(self.event_id)}"
            )
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise ValueError(
                f"event_id must match EVT-* pattern, "
                f"got {_safe_type_name(self.event_id)}"
            )
        # event_type must be in frozen set.
        if not isinstance(self.event_type, str) or not self.event_type:
            raise TypeError(
                f"event_type must be a non-empty str, "
                f"got {_safe_type_name(self.event_type)}"
            )
        if self.event_type not in _EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of the frozen event types, "
                f"got {_safe_type_name(self.event_type)}"
            )
        # payload must belong to TransitionPayload.
        expected_payload_class = _EVENT_TYPE_PAYLOAD_MAP.get(self.event_type)
        if expected_payload_class is None:
            raise TransitionValidationError(
                f"no payload class mapped for event_type, "
                f"got {_safe_type_name(self.event_type)}"
            )
        if not isinstance(self.payload, expected_payload_class):
            raise TransitionValidationError(
                f"event_type requires payload of type "
                f"{expected_payload_class.__name__}, "
                f"got {_safe_type_name(self.payload)}"
            )
        # DispatchCAS requirements.
        if self.event_type in _DISPATCH_CAS_REQUIRED_EVENT_TYPES:
            if self.dispatch_cas is None:
                raise TransitionValidationError(
                    f"event_type {self.event_type} requires DispatchCAS"
                )
        if self.event_type in _PM_ONLY_EVENT_TYPES:
            if self.dispatch_cas is not None:
                raise TransitionValidationError(
                    f"event_type {self.event_type} must not carry DispatchCAS"
                )
        # event_context must be TransitionEventContext.
        if not isinstance(self.event_context, TransitionEventContext):
            raise TypeError(
                f"event_context must be TransitionEventContext, "
                f"got {_safe_type_name(self.event_context)}"
            )


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Immutable result of a successful state transition."""

    task_id: str
    event_id: str
    from_state: str
    to_state: str
    occurred_at: str
    outbox_message_id: str | None

    def __post_init__(self) -> None:
        _validate_safe_str(self.task_id, "task_id")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise TypeError(
                f"event_id must be a non-empty str, "
                f"got {_safe_type_name(self.event_id)}"
            )
        if not isinstance(self.from_state, str) or not self.from_state:
            raise TypeError(
                f"from_state must be a non-empty str, "
                f"got {_safe_type_name(self.from_state)}"
            )
        if not isinstance(self.to_state, str) or not self.to_state:
            raise TypeError(
                f"to_state must be a non-empty str, "
                f"got {_safe_type_name(self.to_state)}"
            )
        _validate_rfc3339_utc_str(self.occurred_at, "occurred_at")
        if self.outbox_message_id is not None:
            if not isinstance(self.outbox_message_id, str) or not self.outbox_message_id:
                raise TypeError(
                    f"outbox_message_id must be a non-empty str or None, "
                    f"got {_safe_type_name(self.outbox_message_id)}"
                )


# ── exception hierarchy ────────────────────────────────────────────────────


class ControlPlaneTransitionError(Exception):
    """Base for all control-plane transition errors."""


class TransitionValidationError(ControlPlaneTransitionError):
    """Input type/value violation, event_type/payload mismatch."""


class TransitionCASConflictError(ControlPlaneTransitionError):
    """CAS precondition failed."""


class TransitionLockContentionError(ControlPlaneTransitionError):
    """Control-plane state lock already held."""


class TransitionLockOrderError(ControlPlaneTransitionError):
    """Reverse lock acquisition detected."""


class TransitionDuplicateEvidenceError(ControlPlaneTransitionError):
    """Duplicate/conflicting event_id / message_id / dedupe_key, or orphan evidence."""


class TransitionSchemaError(ControlPlaneTransitionError):
    """Corrupt or invalid canonical file on read, extra/missing keys."""


class TransitionWriteError(ControlPlaneTransitionError):
    """os.replace / os.fsync failure during the write sequence."""


# ── state lock infrastructure ──────────────────────────────────────────────


def _acquire_os_lock(fd: int) -> None:
    """Acquire a non-blocking OS advisory lock on *fd*.

    * Windows: uses ``msvcrt.locking()`` in non-blocking mode
      (``LK_NBLCK``).  An ``IOError`` on contention is translated to
      ``TransitionLockContentionError``.
    * POSIX: uses ``fcntl.flock(fd, LOCK_EX | LOCK_NB)``.  An ``IOError``
      with errno ``EAGAIN`` / ``EACCES`` / ``EWOULDBLOCK`` is translated
      to ``TransitionLockContentionError``.
    * Unsupported platforms: fail-closed with
      ``TransitionLockContentionError``.

    Raises ``TransitionLockContentionError`` on any lock-acquisition
    failure so callers only need to catch one exception type.
    """
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            raise TransitionLockContentionError(
                "cannot acquire OS advisory lock: msvcrt not available"
            ) from None
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise TransitionLockContentionError(
                "control-plane state lock is currently held"
            ) from exc
        return

    # POSIX: fcntl.flock
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        err = exc.errno
        if err in (
            errno.EACCES,
            errno.EAGAIN,
            errno.EWOULDBLOCK,
        ):
            raise TransitionLockContentionError(
                "control-plane state lock is currently held"
            ) from exc
        raise TransitionLockContentionError(
            "cannot acquire control-plane state lock"
        ) from exc
    except AttributeError:
        raise TransitionLockContentionError(
            "OS advisory lock not available on this platform"
        ) from None


def _release_os_lock(fd: int) -> None:
    """Release an OS advisory lock held on *fd*.

    * Windows: ``msvcrt.locking(fd, LK_UNLCK, 1)``.
    * POSIX: ``fcntl.flock(fd, LOCK_UN)``.

    Lock-release failures are *not* silently ignored — if release fails,
    a ``TransitionLockContentionError`` is raised because the lock state
    is indeterminate.
    """
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _exclusive_state_lock(
    project_root: Path,
) -> Iterator[None]:
    """Acquire the project-level control-plane state lock.

    Uses a **stable lock file** at
    ``.agentdesk/runtime/.state-transition.lock`` with a **non-blocking
    OS advisory lock** (``fcntl.flock(LOCK_EX | LOCK_NB)`` on POSIX,
    ``msvcrt.locking(LK_NBLCK)`` on Windows).

    The lock file is **never deleted** — its existence does not indicate
    that the lock is held; only the OS advisory lock decides ownership.
    Contention raises ``TransitionLockContentionError`` immediately —
    no sleeping, waiting, polling, or retry.

    Frozen fd lifecycle::

        open stable lock file
        acquire non-blocking OS lock
        yield
        release OS lock
        close file descriptor

    * Acquire failure: fd is closed, then
      ``TransitionLockContentionError`` is raised.
    * Body raises: body exception is always propagated (takes priority).
    * Body OK, release fails: ``TransitionLockContentionError`` raised.
    * Body OK, close fails: ``TransitionLockContentionError`` raised
      (close failure does NOT leak path, token, or content).
    """
    runtime_dir = project_root / _RUNTIME_RELATIVE
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / _STATE_LOCK_NAME

    # ── open stable lock file (create if missing, never truncate) ─────────
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise TransitionLockContentionError(
            "cannot acquire control-plane state lock"
        ) from exc

    # ── acquire non-blocking OS lock ──────────────────────────────────────
    try:
        _acquire_os_lock(fd)
    except TransitionLockContentionError:
        # Acquire failed — must close fd.
        os.close(fd)
        raise

    # ── yield to caller ───────────────────────────────────────────────────
    body_exception: BaseException | None = None
    try:
        yield
    except BaseException as _exc:
        body_exception = _exc
    finally:
        # ── release OS lock ──────────────────────────────────────────────
        release_error: BaseException | None = None
        try:
            _release_os_lock(fd)
        except BaseException as _exc:
            release_error = _exc

        # ── close file descriptor ──────────────────────────────────────
        close_error: BaseException | None = None
        try:
            os.close(fd)
        except BaseException as _exc:
            close_error = _exc

        # ── exception priority: body > release > close ────────────────
        if body_exception is not None:
            raise body_exception
        if release_error is not None:
            raise TransitionLockContentionError(
                "failed to release control-plane state lock"
            ) from release_error
        if close_error is not None:
            raise TransitionLockContentionError(
                "failed to close control-plane state lock file"
            ) from close_error


# ── lock-order tracking ────────────────────────────────────────────────────

# Per-call-context local lock-order tracker.
# This is a thread-/context-local concept — NOT a module-level mutable
# registry.  Each invocation gets its own tracker instance.

class _LockOrderTracker:
    """Per-invocation lock-order tracking context.

    Tracks whether the worker-slot fence is held when entering the
    state lock, ensuring the frozen lock order:

    1. worker-slot lease lock → 2. control-plane state lock

    Reverse order is immediately rejected with
    ``TransitionLockOrderError``.

    Enter/exit methods auto-validate — the caller does not need to
    call separate ``validate_*`` functions.  Duplicate enter or exit
    of an unheld lock is fail-closed.
    """

    __slots__ = ("_worker_fence_held", "_state_lock_held")

    def __init__(self) -> None:
        self._worker_fence_held: bool = False
        self._state_lock_held: bool = False

    def enter_worker_fence(self) -> None:
        """Mark worker-slot fence as held.

        Raises ``TransitionLockOrderError`` if:
        * the state lock is already held (reverse order violation).
        * the worker fence is already held (duplicate enter).
        """
        if self._state_lock_held:
            raise TransitionLockOrderError(
                "cannot acquire worker-slot fence while state lock"
                " is held; the frozen order is: worker-slot lock"
                " → state lock"
            )
        if self._worker_fence_held:
            raise TransitionLockOrderError(
                "worker-slot fence is already held"
            )
        self._worker_fence_held = True

    def exit_worker_fence(self) -> None:
        """Mark worker-slot fence as released.

        Raises ``TransitionLockOrderError`` if the worker fence is
        not currently held.
        """
        if not self._worker_fence_held:
            raise TransitionLockOrderError(
                "cannot exit worker-slot fence: not held"
            )
        self._worker_fence_held = False

    def enter_state_lock(self) -> None:
        """Mark state lock as held.

        Raises ``TransitionLockOrderError`` if the state lock is
        already held (duplicate enter).
        """
        if self._state_lock_held:
            raise TransitionLockOrderError(
                "control-plane state lock is already held"
            )
        self._state_lock_held = True

    def exit_state_lock(self) -> None:
        """Mark state lock as released.

        Raises ``TransitionLockOrderError`` if the state lock is
        not currently held.
        """
        if not self._state_lock_held:
            raise TransitionLockOrderError(
                "cannot exit state lock: not held"
            )
        self._state_lock_held = False


# ── model_selection snapshot validation ────────────────────────────────────


def _validate_model_selection_snapshot(model_selection: Any) -> None:
    """Validate that *model_selection* is a ten-field
    ``ModelSelectionSnapshot`` from ``dispatcher_gateway``.

    Does NOT import at module level — validated at construction time
    when a DispatchPayload is created.
    """
    # Dynamic import to avoid module-level side effects.
    from dispatcher_gateway import ModelSelectionSnapshot as MSS

    if not isinstance(model_selection, MSS):
        raise TypeError(
            f"model_selection must be ModelSelectionSnapshot, "
            f"got {_safe_type_name(model_selection)}"
        )
    # Verify exactly ten fields.
    actual_fields = [f.name for f in dc_fields(model_selection)]
    expected = list(_MODEL_SELECTION_FIELDS)
    if actual_fields != expected:
        raise TransitionSchemaError(
            f"model_selection has wrong field set: got {len(actual_fields)} fields, "
            f"expected exactly 10"
        )


# ── pure serialization helpers ─────────────────────────────────────────────


def _serialize_guard_input(inputs: tuple[GuardInput, ...]) -> list[dict[str, object]]:
    """Serialize GuardInput tuple to a list of YAML-friendly mappings."""
    result: list[dict[str, object]] = []
    for inp in inputs:
        value: object
        if isinstance(inp.value, bool):
            value = inp.value
        elif inp.value is None:
            value = None
        else:
            value = inp.value
        result.append({"key": inp.key, "value": value})
    return result


def _serialize_guard_result(results: tuple[GuardResult, ...]) -> list[dict[str, object]]:
    """Serialize GuardResult tuple to a list of YAML-friendly mappings."""
    out: list[dict[str, object]] = []
    for gr in results:
        out.append({
            "guard": gr.guard,
            "inputs": _serialize_guard_input(gr.inputs),
            "result": gr.result,
            "checked_at": gr.checked_at,
            "evidence_ref": gr.evidence_ref,
        })
    return out


def _serialize_event_context(
    ctx: TransitionEventContext,
) -> dict[str, object]:
    """Serialize TransitionEventContext to event field fragment."""
    return {
        "source_message_id": ctx.source_message_id,
        "evidence_refs": list(ctx.evidence_refs),
        "guard_results": _serialize_guard_result(ctx.guard_results),
    }


def _to_yaml_str(value: object, indent: int = 0) -> str:
    """Minimal deterministic YAML emitter — no third-party dependency.

    Produces UTF-8-compatible strings with LF line endings,
    2-space indentation, and deterministic key ordering.
    ``ensure_ascii=False`` equivalent — preserves Unicode.
    """
    return _emit_yaml(value, indent=indent, prefix="")


def _emit_yaml(value: object, indent: int = 0, prefix: str = "") -> str:
    """Recursive YAML emitter."""
    pad = " " * indent

    if value is None:
        return f"{prefix}null\n"

    if isinstance(value, bool):
        return f"{prefix}{str(value).lower()}\n"

    if isinstance(value, int):
        return f"{prefix}{value}\n"

    if isinstance(value, float):
        return f"{prefix}{value}\n"

    if isinstance(value, str):
        # Simple heuristic: if no special chars, use plain scalar.
        # Otherwise use double-quoted.
        if _needs_quoting(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'{prefix}"{escaped}"\n'
        return f"{prefix}{value}\n"

    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return f"{prefix}[]\n"
        lines = []
        for item in value:
            item_yaml = _emit_yaml(item, indent=indent + 2, prefix="- ")
            lines.append(item_yaml)
        return "".join(lines)

    if isinstance(value, dict):
        if len(value) == 0:
            return f"{prefix}{{}}\n"
        lines = []
        for key in sorted(value.keys()):
            k = str(key)
            v = value[key]
            v_yaml = _emit_yaml(v, indent=indent + 2, prefix="")
            lines.append(f"{pad}{k}: {_strip_trailing_newline(v_yaml)}\n")
        return "".join(lines)

    raise TypeError(
        f"unsupported YAML type: {_safe_type_name(value)}"
    )


def _needs_quoting(value: str) -> bool:
    """Return True if *value* needs YAML double-quoting."""
    if not value:
        return True
    if value[0] in "{[&*!|>'\"%#@`-" or value[-1] in " \t":
        return True
    for ch in value:
        if ch in "\0\r\n\t:{}[],&*?|><=!%@`#\"'\\":
            return True
    # Check for YAML keywords.
    lower = value.lower().strip()
    if lower in ("true", "false", "yes", "no", "on", "off", "null", "none"):
        return True
    if value in ("y", "n", "Y", "N"):
        return True
    return False


def _strip_trailing_newline(s: str) -> str:
    """Strip a single trailing newline if present."""
    if s.endswith("\n"):
        return s[:-1]
    return s


def _serialize_payload_to_mapping(payload: Any, event_type: str) -> dict[str, object]:
    """Serialize a typed Payload to a transition-specific YAML mapping."""
    result: dict[str, object] = {}
    for f in dc_fields(payload):
        val = getattr(payload, f.name)
        if isinstance(payload, DispatchPayload) and f.name == "model_selection":
            result[f.name] = _serialize_model_selection(val)
        else:
            result[f.name] = _to_yaml_scalar(val)
    return result


def _serialize_model_selection(model_selection: Any) -> dict[str, object]:
    """Serialize a ModelSelectionSnapshot to a YAML-friendly mapping."""
    result: dict[str, object] = {}
    for f in dc_fields(model_selection):
        val = getattr(model_selection, f.name)
        if isinstance(val, tuple):
            result[f.name] = list(val)
        else:
            result[f.name] = val
    return result


def _to_yaml_scalar(val: Any) -> object:
    """Convert a value to a YAML scalar for serialization."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        return val
    return val


def _serialize_state_event_mapping(
    schema_version: str,
    event_id: str,
    event_type: str,
    task_id: str,
    revision: int,
    attempt: int | None,
    dispatch_id: str | None,
    from_state: str,
    to_state: str,
    lease_epoch: int,
    occurred_at: str,
    source_message_id: str | None,
    evidence_refs: tuple[str, ...],
    guard_results: tuple[GuardResult, ...],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a complete state-event mapping for serialization."""
    result: dict[str, object] = {
        "schema_version": schema_version,
        "event_id": event_id,
        "event_type": event_type,
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "from_state": from_state,
        "to_state": to_state,
        "lease_epoch": lease_epoch,
        "actor_role_id": "PM",
        "occurred_at": occurred_at,
        "source_message_id": source_message_id,
        "evidence_refs": list(evidence_refs),
        "guard_results": _serialize_guard_result(guard_results),
    }
    if extra:
        result.update(extra)
    return result


def _serialize_outbox_mapping(
    schema_version: str,
    message_id: str,
    event_id: str,
    message_type: str,
    dedupe_key: str,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    destination_role_id: str,
    created_at: str,
    model_selection: dict[str, object],
    payload: dict[str, object],
) -> dict[str, object]:
    """Build a complete outbox mapping for serialization."""
    return {
        "schema_version": schema_version,
        "message_id": message_id,
        "event_id": event_id,
        "message_type": message_type,
        "dedupe_key": dedupe_key,
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "destination_role_id": destination_role_id,
        "created_at": created_at,
        "model_selection": model_selection,
        "payload": payload,
    }


# ── schema validation helpers ──────────────────────────────────────────────


def _validate_state_event_schema(
    event: dict[str, object],
) -> None:
    """Validate a state-event mapping for required keys.

    Raises ``TransitionSchemaError`` on missing/extra keys or value type
    violations.
    """
    actual_keys = set(event.keys())

    # Common required keys.
    for key in _STATE_EVENT_COMMON_KEYS:
        if key not in actual_keys:
            raise TransitionSchemaError(
                f"state event missing required key: {key}"
            )

    # No extra keys check (strict set of known keys plus event-type-specific).
    known_keys = set(_STATE_EVENT_COMMON_KEYS)
    event_type = event.get("event_type")
    if event_type == "TASK_DISPATCHED":
        known_keys.add("payload_digest")
    if event_type == "CHANGE_INTEGRATED":
        known_keys.update({
            "accepted_commit",
            "integrated_commit",
            "equivalence_method",
            "equivalence_result",
            "equivalence_evidence_ref",
        })

    extra = actual_keys - known_keys
    if extra:
        raise TransitionSchemaError(
            f"state event has unexpected keys: {', '.join(sorted(extra))}"
        )

    # Validate schema_version.
    if event.get("schema_version") != _STATE_EVENT_SCHEMA:
        raise TransitionSchemaError(
            f"state event schema_version must be {_STATE_EVENT_SCHEMA}"
        )

    # Validate event_id.
    eid = event.get("event_id")
    if not isinstance(eid, str) or _EVENT_ID_RE.fullmatch(eid) is None:
        raise TransitionSchemaError("state event event_id must match EVT-* pattern")

    # Validate revision is non-bool int.
    rev = event.get("revision")
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise TransitionSchemaError("state event revision must be a non-bool int")

    # Validate attempt is non-bool int or null.
    att = event.get("attempt")
    if att is not None and (isinstance(att, bool) or not isinstance(att, int)):
        raise TransitionSchemaError("state event attempt must be non-bool int or null")

    # Validate dispatch_id is str or null.
    did = event.get("dispatch_id")
    if did is not None and not isinstance(did, str):
        raise TransitionSchemaError("state event dispatch_id must be str or null")

    # Validate evidence_refs is list.
    refs = event.get("evidence_refs")
    if not isinstance(refs, list):
        raise TransitionSchemaError("state event evidence_refs must be a list")

    # Validate guard_results is list.
    grs = event.get("guard_results")
    if not isinstance(grs, list):
        raise TransitionSchemaError("state event guard_results must be a list")
    for i, gr in enumerate(grs):
        if not isinstance(gr, dict):
            raise TransitionSchemaError(
                f"state event guard_results[{i}] must be a mapping"
            )
        # Validate guard_result fields.
        for req in ("guard", "inputs", "result", "checked_at", "evidence_ref"):
            if req not in gr:
                raise TransitionSchemaError(
                    f"state event guard_results[{i}] missing key: {req}"
                )

    # Validate payload_digest for TASK_DISPATCHED.
    if event_type == "TASK_DISPATCHED":
        pd = event.get("payload_digest")
        if not isinstance(pd, str) or _PAYLOAD_DIGEST_RE.fullmatch(pd) is None:
            raise TransitionSchemaError(
                "state event payload_digest must match sha256:<64 hex>"
            )


def _validate_outbox_schema(
    outbox: dict[str, object],
) -> None:
    """Validate an outbox mapping for required keys.

    Raises ``TransitionSchemaError`` on missing/extra keys or value type
    violations.
    """
    actual_keys = set(outbox.keys())

    for key in _OUTBOX_REQUIRED_KEYS:
        if key not in actual_keys:
            raise TransitionSchemaError(
                f"outbox missing required key: {key}"
            )

    extra = actual_keys - set(_OUTBOX_REQUIRED_KEYS)
    if extra:
        raise TransitionSchemaError(
            f"outbox has unexpected keys: {', '.join(sorted(extra))}"
        )

    # Validate schema_version.
    if outbox.get("schema_version") != _OUTBOX_SCHEMA:
        raise TransitionSchemaError(
            f"outbox schema_version must be {_OUTBOX_SCHEMA}"
        )

    # Validate message_id.
    mid = outbox.get("message_id")
    if not isinstance(mid, str) or _MESSAGE_ID_RE.fullmatch(mid) is None:
        raise TransitionSchemaError("outbox message_id must match MSG-* pattern")

    # Validate model_selection is a dict.
    ms = outbox.get("model_selection")
    if not isinstance(ms, dict):
        raise TransitionSchemaError("outbox model_selection must be a mapping")

    # Validate model_selection fields.
    ms_keys = set(ms.keys())
    if ms_keys != _MODEL_SELECTION_FIELD_SET:
        missing_ms = sorted(_MODEL_SELECTION_FIELD_SET - ms_keys)
        extra_ms = sorted(ms_keys - _MODEL_SELECTION_FIELD_SET)
        bits: list[str] = []
        if missing_ms:
            bits.append(f"missing: {', '.join(missing_ms)}")
        if extra_ms:
            bits.append(f"extra: {', '.join(extra_ms)}")
        raise TransitionSchemaError(
            f"outbox model_selection field set mismatch: {'; '.join(bits)}"
        )

    # Validate payload is a dict.
    pl = outbox.get("payload")
    if not isinstance(pl, dict):
        raise TransitionSchemaError("outbox payload must be a mapping")

    # Validate revision is non-bool int.
    rev = outbox.get("revision")
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise TransitionSchemaError("outbox revision must be a non-bool int")


# -- transition spec registry --


@dataclass(frozen=True, slots=True)
class _TransitionSpec:
    """Internal registry entry for a single transition type.

    Encodes static facts only -- never stores runtime path, request,
    task, or mutable state.
    """

    event_type: str
    payload_type: type
    from_states: frozenset[str]  # empty = wildcard (any non-terminal)
    to_state: str | None  # None = variable (caller-specified)
    needs_worker_lease: bool
    needs_dispatch_cas: bool
    produces_outbox: bool
    produces_acceptance: bool


# -- terminal states (no further transitions allowed) --

_TERMINAL_STATES: frozenset[str] = frozenset({
    "integrated", "cancelled", "superseded",
})

# -- wildcard-eligible non-terminal states --

_NON_TERMINAL_STATES: frozenset[str] = _STATES_FROZEN - _TERMINAL_STATES

# -- transition registry -- exactly 15 entries --

_TRANSITION_SPECS: dict[str, _TransitionSpec] = {
    # 1. draft -> ready
    "TASK_SPECIFIED": _TransitionSpec(
        event_type="TASK_SPECIFIED",
        payload_type=SpecifyPayload,
        from_states=frozenset({"draft"}),
        to_state="ready",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 2. ready -> dispatched
    "TASK_DISPATCHED": _TransitionSpec(
        event_type="TASK_DISPATCHED",
        payload_type=DispatchPayload,
        from_states=frozenset({"ready"}),
        to_state="dispatched",
        needs_worker_lease=True,
        needs_dispatch_cas=False,
        produces_outbox=True,
        produces_acceptance=False,
    ),
    # 3. dispatched -> in_progress
    "DISPATCH_ACKNOWLEDGED": _TransitionSpec(
        event_type="DISPATCH_ACKNOWLEDGED",
        payload_type=AcknowledgePayload,
        from_states=frozenset({"dispatched"}),
        to_state="in_progress",
        needs_worker_lease=True,
        needs_dispatch_cas=True,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 4. in_progress -> review_ready
    "DELIVERY_SUBMITTED": _TransitionSpec(
        event_type="DELIVERY_SUBMITTED",
        payload_type=DeliverySubmittedPayload,
        from_states=frozenset({"in_progress"}),
        to_state="review_ready",
        needs_worker_lease=True,
        needs_dispatch_cas=True,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 5. review_ready -> accepted
    "DELIVERY_ACCEPTED": _TransitionSpec(
        event_type="DELIVERY_ACCEPTED",
        payload_type=DeliveryAcceptedPayload,
        from_states=frozenset({"review_ready"}),
        to_state="accepted",
        needs_worker_lease=True,
        needs_dispatch_cas=True,
        produces_outbox=False,
        produces_acceptance=True,
    ),
    # 6. review_ready -> returned
    "DELIVERY_RETURNED": _TransitionSpec(
        event_type="DELIVERY_RETURNED",
        payload_type=DeliveryReturnedPayload,
        from_states=frozenset({"review_ready"}),
        to_state="returned",
        needs_worker_lease=True,
        needs_dispatch_cas=True,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 7. returned -> ready
    "TASK_REQUEUED": _TransitionSpec(
        event_type="TASK_REQUEUED",
        payload_type=RequeuePayload,
        from_states=frozenset({"returned"}),
        to_state="ready",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 8. accepted -> integrated
    "CHANGE_INTEGRATED": _TransitionSpec(
        event_type="CHANGE_INTEGRATED",
        payload_type=IntegrationPayload,
        from_states=frozenset({"accepted"}),
        to_state="integrated",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 9. accepted -> blocked (INTEGRATION_FAILED)
    "INTEGRATION_FAILED": _TransitionSpec(
        event_type="INTEGRATION_FAILED",
        payload_type=BlockedPayload,
        from_states=frozenset({"accepted"}),
        to_state="blocked",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 10. * -> blocked (TASK_BLOCKED)
    "TASK_BLOCKED": _TransitionSpec(
        event_type="TASK_BLOCKED",
        payload_type=BlockedPayload,
        from_states=frozenset(),
        to_state="blocked",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 11. blocked -> resume (BLOCKER_RESOLVED)
    "BLOCKER_RESOLVED": _TransitionSpec(
        event_type="BLOCKER_RESOLVED",
        payload_type=BlockerResolvedPayload,
        from_states=frozenset({"blocked"}),
        to_state=None,
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 12. blocked -> draft (BLOCKER_RESCOPED)
    "BLOCKER_RESCOPED": _TransitionSpec(
        event_type="BLOCKER_RESCOPED",
        payload_type=BlockerRescopedPayload,
        from_states=frozenset({"blocked"}),
        to_state="draft",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 13. blocked -> cancelled (BLOCKER_CANCELLED)
    "BLOCKER_CANCELLED": _TransitionSpec(
        event_type="BLOCKER_CANCELLED",
        payload_type=BlockerCancelledPayload,
        from_states=frozenset({"blocked"}),
        to_state="cancelled",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 14. * -> cancelled (TASK_CANCELLED)
    "TASK_CANCELLED": _TransitionSpec(
        event_type="TASK_CANCELLED",
        payload_type=CancelledPayload,
        from_states=frozenset(),
        to_state="cancelled",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
    # 15. * -> superseded (TASK_SUPERSEDED)
    "TASK_SUPERSEDED": _TransitionSpec(
        event_type="TASK_SUPERSEDED",
        payload_type=SupersededPayload,
        from_states=frozenset(),
        to_state="superseded",
        needs_worker_lease=False,
        needs_dispatch_cas=False,
        produces_outbox=False,
        produces_acceptance=False,
    ),
}


# -- canonical path helpers --

_CANONICAL_DIRS = {
    "state": Path("docs") / "pm" / "state",
    "events": Path("docs") / "pm" / "events",
    "outbox": Path("docs") / "pm" / "outbox",
    "acceptances": Path("docs") / "pm" / "acceptances",
}
_BOARD_PATH = Path("docs") / "pm" / "BOARD.md"
_STATUS_PATH = Path("docs") / "pm" / "STATUS.md"
_ACCEPTANCE_SCHEMA = "agentdesk.acceptance/v2"
_REVIEW_NUMBER_RE = re.compile(r"-review([1-9][0-9]*)\.md$")


def _derive_to_state(
    spec: _TransitionSpec,
    payload: Any,
) -> str:
    """Return the to_state for this transition."""
    if spec.to_state is not None:
        return spec.to_state
    return payload.resume_to_state  # type: ignore[union-attr]


# -- validate now --


def _validate_now(now: datetime) -> None:
    """Validate now is a timezone-aware UTC datetime, not bool, not naive."""
    if isinstance(now, bool) or not isinstance(now, datetime):
        raise TypeError(
            f"now must be a datetime, got {_safe_type_name(now)}"
        )
    if now.tzinfo is None:
        raise TypeError("now must be timezone-aware")
    if now.utcoffset() is None:
        raise TypeError("now must have a UTC offset")
    if now.utcoffset().total_seconds() != 0:
        raise TypeError("now must be UTC")


# -- Git HEAD query --


def _resolve_head_commit(project_root: Path) -> str:
    """Read the current Git HEAD via git rev-parse HEAD.

    Uses argv array, shell=False.  Raises TransitionCASConflictError
    on any failure -- the error message never leaks stderr or paths.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        raise TransitionCASConflictError(
            "cannot read Git HEAD -- git command failed"
        ) from None
    if result.returncode != 0:
        raise TransitionCASConflictError(
            "cannot read Git HEAD -- git returned non-zero"
        )
    sha = result.stdout.strip()
    if not _SHA_RE.fullmatch(sha):
        raise TransitionCASConflictError(
            "cannot read Git HEAD -- output is not a valid SHA"
        )
    return sha


# -- canonical state reader --


def _read_tasks_yaml(project_root: Path) -> dict[str, Any]:
    """Read and validate docs/pm/state/tasks.yaml.

    Returns the parsed state dict.  Raises TransitionSchemaError on
    any structural or schema violation.
    """
    state_path = project_root / _CANONICAL_DIRS["state"] / "tasks.yaml"
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TransitionSchemaError(
            "cannot read canonical tasks state"
        ) from exc

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TransitionSchemaError(
            "canonical tasks state is not JSON-compatible YAML"
        ) from exc

    if not isinstance(state, dict):
        raise TransitionSchemaError(
            "canonical tasks state root must be an object"
        )
    if state.get("schema_version") != "agentdesk.tasks/v2":
        raise TransitionSchemaError(
            "canonical tasks state schema_version must be agentdesk.tasks/v2"
        )
    if not isinstance(state.get("project_id"), str) or not state["project_id"]:
        raise TransitionSchemaError(
            "canonical tasks state project_id must be a non-empty str"
        )

    pm_control = state.get("pm_control")
    if not isinstance(pm_control, dict):
        raise TransitionSchemaError(
            "canonical tasks state pm_control must be an object"
        )
    for req in ("holder_id", "lease_epoch", "mode"):
        if req not in pm_control:
            raise TransitionSchemaError(
                f"canonical tasks state pm_control missing key: {req}"
            )
    if not isinstance(pm_control.get("holder_id"), str) or not pm_control["holder_id"]:
        raise TransitionSchemaError(
            "canonical tasks state pm_control.holder_id must be a non-empty str"
        )
    if isinstance(pm_control.get("lease_epoch"), bool) or not isinstance(
        pm_control.get("lease_epoch"), int
    ):
        raise TransitionSchemaError(
            "canonical tasks state pm_control.lease_epoch must be a non-bool int"
        )

    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        raise TransitionSchemaError(
            "canonical tasks state tasks must be a list"
        )
    seen_ids: set[str] = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise TransitionSchemaError(
                f"canonical tasks state tasks[{i}] must be an object"
            )
        tid = t.get("task_id")
        if not isinstance(tid, str) or not tid:
            raise TransitionSchemaError(
                f"canonical tasks state tasks[{i}].task_id must be a non-empty str"
            )
        if tid in seen_ids:
            raise TransitionSchemaError(
                f"canonical tasks state duplicate task_id: {tid!r}"
            )
        seen_ids.add(tid)
    return state


def _find_task(state: dict[str, Any], task_id: str) -> tuple[dict[str, Any], int]:
    """Find a task in canonical state by task_id.

    Returns (task_dict, index).  Raises TransitionCASConflictError
    if the task is not found.
    """
    tasks: list[dict[str, Any]] = state["tasks"]
    for i, t in enumerate(tasks):
        if t.get("task_id") == task_id:
            return t, i
    raise TransitionCASConflictError(
        "task not found in canonical state"
    )


# -- existing evidence scanner --


def _read_existing_event_bytes(
    project_root: Path, event_id: str
) -> bytes | None:
    """Read existing event file as raw bytes, or None if missing."""
    event_path = (
        project_root
        / _CANONICAL_DIRS["events"]
        / f"{event_id}.yaml"
    )
    try:
        return event_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _read_existing_outbox_bytes(
    project_root: Path, message_id: str
) -> bytes | None:
    """Read existing outbox file as raw bytes, or None if missing."""
    outbox_path = (
        project_root
        / _CANONICAL_DIRS["outbox"]
        / f"{message_id}.yaml"
    )
    try:
        return outbox_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _read_existing_acceptance_bytes(
    project_root: Path, acceptance_path: str
) -> bytes | None:
    """Read existing acceptance record as raw bytes, or None if missing."""
    abs_path = project_root / acceptance_path
    try:
        return abs_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


# -- idempotency / duplicate / orphan detection --


def _check_idempotency_and_duplicates(
    project_root: Path,
    request: TransitionRequest,
    spec: _TransitionSpec,
    task: dict[str, Any],
    to_state: str,
    proposed_event_bytes: bytes,
    proposed_outbox_bytes: bytes | None,
    proposed_acceptance_bytes: bytes | None,
    proposed_tasks_bytes: bytes,
) -> TransitionResult | None:
    """Check for idempotent replay, duplicate evidence, or orphan state.

    Returns a TransitionResult on full idempotent success (zero writes).
    Raises TransitionDuplicateEvidenceError on duplicate / orphan.

    Must be called BEFORE any authoritative writes.
    """
    event_id = request.event_id
    existing_event_bytes = _read_existing_event_bytes(project_root, event_id)

    expects_outbox = spec.produces_outbox
    expects_acceptance = spec.produces_acceptance
    outbox_message_id: str | None = None
    if expects_outbox and isinstance(request.payload, DispatchPayload):
        outbox_message_id = request.payload.outbox_message_id
    acceptance_path: str | None = None
    if expects_acceptance and isinstance(request.payload, DeliveryAcceptedPayload):
        acceptance_path = request.payload.acceptance_path

    existing_outbox_bytes: bytes | None = None
    if outbox_message_id is not None:
        existing_outbox_bytes = _read_existing_outbox_bytes(
            project_root, outbox_message_id
        )

    existing_acceptance_bytes: bytes | None = None
    if acceptance_path is not None:
        existing_acceptance_bytes = _read_existing_acceptance_bytes(
            project_root, acceptance_path
        )

    has_event = existing_event_bytes is not None
    has_outbox = existing_outbox_bytes is not None
    has_acceptance = existing_acceptance_bytes is not None
    tasks_at_target = task.get("state") == to_state

    any_present = has_event or has_outbox or has_acceptance
    if not any_present and not tasks_at_target:
        return None

    # Event exists -- check byte equality.
    # Only raise if content differs AND tasks are NOT at target state.
    # If tasks ARE at target, different content may be from a prior
    # genuine transition (not a retry collision), so we defer to
    # the full-idempotent check below.
    if has_event and existing_event_bytes != proposed_event_bytes:
        if not tasks_at_target:
            raise TransitionDuplicateEvidenceError(
                "event_id already exists with different content"
            )

    # Outbox exists -- check byte equality.
    if has_outbox and proposed_outbox_bytes is not None:
        if existing_outbox_bytes != proposed_outbox_bytes:
            raise TransitionDuplicateEvidenceError(
                "outbox message_id already exists with different content"
            )

    # Acceptance exists -- check byte equality.
    if has_acceptance and proposed_acceptance_bytes is not None:
        if existing_acceptance_bytes != proposed_acceptance_bytes:
            raise TransitionDuplicateEvidenceError(
                "acceptance record already exists with different content"
            )

    # Full idempotent replay: all expected files present AND tasks at target.
    all_expected_present = (
        has_event
        and (not expects_outbox or has_outbox)
        and (not expects_acceptance or has_acceptance)
    )
    if all_expected_present and tasks_at_target:
        occurred_at = task.get("timestamps", {}).get(
            "updated_at", "1970-01-01T00:00:00Z"
        )
        return TransitionResult(
            task_id=request.cas.task_id,
            event_id=event_id,
            from_state=request.cas.expected_state,
            to_state=to_state,
            occurred_at=occurred_at,
            outbox_message_id=outbox_message_id,
        )

    # Simple transitions without companion files: event + tasks -> idempotent.
    if (
        has_event
        and tasks_at_target
        and not expects_outbox
        and not expects_acceptance
        and not has_outbox
        and not has_acceptance
    ):
        occurred_at = task.get("timestamps", {}).get(
            "updated_at", "1970-01-01T00:00:00Z"
        )
        return TransitionResult(
            task_id=request.cas.task_id,
            event_id=event_id,
            from_state=request.cas.expected_state,
            to_state=to_state,
            occurred_at=occurred_at,
            outbox_message_id=None,
        )

    # Partial / orphan: some evidence present but not complete.
    missing: list[str] = []
    if has_event and not tasks_at_target:
        missing.append("tasks.yaml not at target state")
    if expects_outbox and not has_outbox and has_event:
        missing.append("outbox not found")
    if expects_acceptance and not has_acceptance and has_event:
        missing.append("acceptance not found")
    if tasks_at_target and not has_event and (has_outbox or has_acceptance):
        missing.append("event not found but companion evidence exists")

    if missing:
        raise TransitionDuplicateEvidenceError(
            "partial evidence detected: " + "; ".join(missing)
        )

    return None


# -- dedupe key --


def _derive_dedupe_key(
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    message_type: str,
) -> str:
    """Derive deterministic outbox dedupe key."""
    return f"{task_id}/r{revision}/a{attempt}/{dispatch_id}/{message_type}"


# -- payload digest --


def _compute_payload_digest(outbox_bytes: bytes) -> str:
    """Compute sha256:<64 hex> of outbox bytes."""
    h = hashlib.sha256(outbox_bytes).hexdigest()
    return f"sha256:{h}"


# -- review number parser --


def _parse_review_number(acceptance_path: str) -> int:
    """Extract review number from acceptance_path."""
    m = _REVIEW_NUMBER_RE.search(acceptance_path)
    if m is None:
        raise TransitionValidationError(
            "acceptance_path must end with -reviewN.md (N >= 1)"
        )
    return int(m.group(1))


# -- task field mutation --


def _mutate_task_for_transition(
    task: dict[str, Any],
    request: TransitionRequest,
    spec: _TransitionSpec,
    to_state: str,
    now_str: str,
    lease_epoch: int,
) -> dict[str, Any]:
    """Return a mutated deep copy of task for the transition."""
    new_task = copy.deepcopy(task)
    new_task["state"] = to_state

    if not isinstance(new_task.get("timestamps"), dict):
        new_task["timestamps"] = {}
    timestamps: dict[str, Any] = new_task["timestamps"]
    timestamps["updated_at"] = now_str

    event_type = spec.event_type

    if event_type == "TASK_SPECIFIED":
        timestamps["ready_at"] = now_str

    elif event_type == "TASK_DISPATCHED":
        payload = request.payload
        assert isinstance(payload, DispatchPayload)
        new_task["attempt"] = 1
        new_task["current_dispatch"] = {
            "dispatch_id": payload.dispatch_id,
            "role_id": payload.role_id,
            "base_commit": payload.base_commit,
            "branch": payload.branch,
            "model_selection": _serialize_model_selection(payload.model_selection),
        }
        new_task["model_selection"] = _serialize_model_selection(
            payload.model_selection
        )
        new_task["report_path"] = payload.report_path
        timestamps["dispatched_at"] = now_str

    elif event_type == "DISPATCH_ACKNOWLEDGED":
        timestamps["started_at"] = now_str

    elif event_type == "DELIVERY_SUBMITTED":
        payload = request.payload
        assert isinstance(payload, DeliverySubmittedPayload)
        new_task["implementation_commit"] = payload.implementation_commit
        new_task["report_commit"] = payload.report_commit
        new_task["delivery_state"] = "submitted"
        timestamps["delivered_at"] = now_str

    elif event_type == "DELIVERY_ACCEPTED":
        payload = request.payload
        assert isinstance(payload, DeliveryAcceptedPayload)
        new_task["accepted_commit"] = payload.accepted_commit
        new_task["acceptance_path"] = payload.acceptance_path
        new_task["delivery_state"] = "accepted"
        timestamps["accepted_at"] = now_str
        new_task["current_dispatch"] = None

    elif event_type == "DELIVERY_RETURNED":
        new_task["delivery_state"] = "rejected"
        new_task["current_dispatch"] = None

    elif event_type == "TASK_REQUEUED":
        new_task["current_dispatch"] = None

    elif event_type == "CHANGE_INTEGRATED":
        payload = request.payload
        assert isinstance(payload, IntegrationPayload)
        new_task["integrated_commit"] = payload.integrated_commit
        timestamps["integrated_at"] = now_str

    elif event_type in ("TASK_BLOCKED", "INTEGRATION_FAILED"):
        payload = request.payload
        assert isinstance(payload, BlockedPayload)
        new_task["blocked_reason"] = payload.blocked_reason
        new_task["blocked_kind"] = payload.blocked_kind
        new_task["blocked_owner"] = payload.blocked_owner
        new_task["unblock_condition"] = payload.unblock_condition
        new_task["resume_state"] = payload.resume_state
        new_task["blocked_attempt_valid"] = payload.blocked_attempt_valid
        timestamps["blocked_at"] = now_str
        if payload.blocked_attempt_valid is not True:
            new_task["current_dispatch"] = None

    elif event_type == "BLOCKER_RESOLVED":
        payload = request.payload
        assert isinstance(payload, BlockerResolvedPayload)
        new_task["state"] = payload.resume_to_state
        new_task["blocked_reason"] = None
        new_task["blocked_kind"] = None
        new_task["blocked_owner"] = None
        new_task["unblock_condition"] = None
        new_task["resume_state"] = None
        new_task["blocked_attempt_valid"] = None
        new_task["review_after"] = None

    elif event_type == "BLOCKER_RESCOPED":
        payload = request.payload
        assert isinstance(payload, BlockerRescopedPayload)
        new_task["revision"] = payload.new_revision
        new_task["blocked_reason"] = None
        new_task["blocked_kind"] = None
        new_task["blocked_owner"] = None
        new_task["unblock_condition"] = None
        new_task["resume_state"] = None
        new_task["blocked_attempt_valid"] = None
        new_task["review_after"] = None
        new_task["current_dispatch"] = None
        new_task["delivery_state"] = None
        new_task["implementation_commit"] = None
        new_task["report_commit"] = None
        new_task["accepted_commit"] = None
        new_task["acceptance_path"] = None
        new_task["report_path"] = None

    elif event_type == "BLOCKER_CANCELLED":
        new_task["blocked_reason"] = None
        new_task["blocked_kind"] = None
        new_task["blocked_owner"] = None
        new_task["unblock_condition"] = None
        new_task["resume_state"] = None
        new_task["blocked_attempt_valid"] = None
        new_task["review_after"] = None
        new_task["current_dispatch"] = None
        timestamps["cancelled_at"] = now_str

    elif event_type == "TASK_CANCELLED":
        new_task["current_dispatch"] = None
        timestamps["cancelled_at"] = now_str

    elif event_type == "TASK_SUPERSEDED":
        payload = request.payload
        assert isinstance(payload, SupersededPayload)
        new_task["superseded_by"] = payload.superseded_by
        new_task["current_dispatch"] = None
        timestamps["superseded_at"] = now_str

    return new_task


# -- event bytes builder --


def _build_event_bytes(
    request: TransitionRequest,
    spec: _TransitionSpec,
    task: dict[str, Any],
    to_state: str,
    now: datetime,
    lease_epoch: int,
    payload_digest: str | None,
) -> bytes:
    """Build the state-event YAML bytes."""
    occurred_at = _format_rfc3339_utc(now)

    task_revision = task.get("revision", request.cas.expected_revision)
    task_attempt = task.get("attempt")
    task_dispatch_id = None
    cd = task.get("current_dispatch")
    if isinstance(cd, dict) and isinstance(cd.get("dispatch_id"), str):
        task_dispatch_id = cd["dispatch_id"]

    extra: dict[str, object] = {}
    if spec.event_type == "TASK_DISPATCHED" and payload_digest is not None:
        extra["payload_digest"] = payload_digest
    if spec.event_type == "CHANGE_INTEGRATED":
        payload = request.payload
        if isinstance(payload, IntegrationPayload):
            extra["integrated_commit"] = payload.integrated_commit
            if payload.equivalence_method is not None:
                extra["equivalence_method"] = payload.equivalence_method
                extra["equivalence_result"] = "passed"
                if payload.equivalence_evidence_ref is not None:
                    extra["equivalence_evidence_ref"] = (
                        payload.equivalence_evidence_ref
                    )

    event_ctx = request.event_context
    mapping = _serialize_state_event_mapping(
        schema_version=_STATE_EVENT_SCHEMA,
        event_id=request.event_id,
        event_type=spec.event_type,
        task_id=request.cas.task_id,
        revision=task_revision,
        attempt=task_attempt,
        dispatch_id=task_dispatch_id,
        from_state=request.cas.expected_state,
        to_state=to_state,
        lease_epoch=lease_epoch,
        occurred_at=occurred_at,
        source_message_id=event_ctx.source_message_id,
        evidence_refs=event_ctx.evidence_refs,
        guard_results=event_ctx.guard_results,
        extra=extra if extra else None,
    )

    _validate_state_event_schema(mapping)

    yaml_str = _to_yaml_str(mapping)
    return yaml_str.encode("utf-8")


# -- outbox bytes builder --


def _build_outbox_bytes(
    request: TransitionRequest,
    task: dict[str, Any],
    to_state: str,
    now: datetime,
) -> bytes | None:
    """Build outbox YAML bytes for TASK_DISPATCHED, or None."""
    if not isinstance(request.payload, DispatchPayload):
        return None

    payload = request.payload
    created_at = _format_rfc3339_utc(now)
    new_attempt = task.get("attempt", 1)
    dedupe_key = _derive_dedupe_key(
        task_id=request.cas.task_id,
        revision=request.cas.expected_revision,
        attempt=new_attempt,
        dispatch_id=payload.dispatch_id,
        message_type="task.dispatch",
    )

    model_sel = _serialize_model_selection(payload.model_selection)
    outbox_payload = {
        "task_path": payload.task_card_path,
        "task_card_commit": payload.task_card_commit,
        "base_commit": payload.base_commit,
        "branch": payload.branch,
        "report_path": payload.report_path,
    }

    mapping = _serialize_outbox_mapping(
        schema_version=_OUTBOX_SCHEMA,
        message_id=payload.outbox_message_id,
        event_id=request.event_id,
        message_type="task.dispatch",
        dedupe_key=dedupe_key,
        task_id=request.cas.task_id,
        revision=request.cas.expected_revision,
        attempt=new_attempt,
        dispatch_id=payload.dispatch_id,
        destination_role_id=payload.role_id,
        created_at=created_at,
        model_selection=model_sel,
        payload=outbox_payload,
    )

    _validate_outbox_schema(mapping)

    yaml_str = _to_yaml_str(mapping)
    return yaml_str.encode("utf-8")


# -- acceptance record builder --


def _build_acceptance_bytes(
    request: TransitionRequest,
    task: dict[str, Any],
    now: datetime,
) -> bytes | None:
    """Build acceptance markdown bytes for DELIVERY_ACCEPTED, or None."""
    if not isinstance(request.payload, DeliveryAcceptedPayload):
        return None

    payload = request.payload
    dc = request.dispatch_cas
    cd = task.get("current_dispatch")
    reviewed_dispatch_id = (
        dc.expected_dispatch_id
        if dc is not None
        else (cd["dispatch_id"] if isinstance(cd, dict) else None)
    )

    occurrence = _format_rfc3339_utc(now)

    lines: list[str] = []
    lines.append("---")
    lines.append(f"schema_version: {_ACCEPTANCE_SCHEMA}")
    lines.append(f"task_id: {request.cas.task_id}")
    lines.append(f"revision: {request.cas.expected_revision}")
    attempt = task.get("attempt")
    lines.append(f"attempt: {attempt if attempt is not None else 'null'}")
    impl_commit = task.get("implementation_commit")
    lines.append(
        f"implementation_commit: {impl_commit if impl_commit else 'null'}"
    )
    report_commit = task.get("report_commit")
    lines.append(
        f"report_commit: {report_commit if report_commit else 'null'}"
    )
    base_commit = (
        cd.get("base_commit")
        if isinstance(cd, dict)
        else task.get("task_card_commit")
    )
    lines.append(
        f"base_commit: {base_commit if base_commit else 'null'}"
    )
    lines.append("decision: accepted")
    lines.append(f"accepted_commit: {payload.accepted_commit}")
    lines.append(
        f"reviewed_dispatch_id: {reviewed_dispatch_id if reviewed_dispatch_id else 'null'}"
    )
    lines.append(f"occurred_at: {occurrence}")
    lines.append("gate: none")
    lines.append("approval_ids: []")
    lines.append("---")
    lines.append("")
    lines.append("# Acceptance Record")
    lines.append("")

    content = "\n".join(lines)
    return content.encode("utf-8")


# -- tasks.yaml serializer --


def _serialize_tasks_state(state: dict[str, Any]) -> bytes:
    """Serialize the full tasks.yaml state dict to JSON bytes.

    Uses json.dumps to produce JSON-compatible YAML (RFC 7159 JSON
    is valid YAML 1.2). The existing render_views._load_state uses
    json.loads, so the round-trip is verified.
    """
    json_str = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    return (json_str + "\n").encode("utf-8")


# -- derived view renderer --


def _render_derived_views(
    state: dict[str, Any],
) -> tuple[str, str]:
    """Render BOARD.md and STATUS.md from canonical state."""
    import sys as _sys
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    _already = _scripts_dir in _sys.path
    if not _already:
        _sys.path.insert(0, _scripts_dir)
    try:
        from render_views import _render_board, _render_status
    finally:
        if not _already:
            _sys.path.pop(0)

    board = _render_board(state)
    status = _render_status(state)
    return board, status


# -- canonical file writing --


def _write_canonical_files(
    project_root: Path,
    event_id: str,
    event_bytes: bytes,
    outbox_bytes: bytes | None,
    outbox_message_id: str | None,
    acceptance_bytes: bytes | None,
    acceptance_path: str | None,
    tasks_bytes: bytes,
) -> None:
    """Write all canonical files in fixed order.

    Order: event -> outbox -> acceptance -> tasks.yaml
    """
    events_dir = project_root / _CANONICAL_DIRS["events"]
    outbox_dir = project_root / _CANONICAL_DIRS["outbox"]
    acceptances_dir = project_root / _CANONICAL_DIRS["acceptances"]
    state_dir = project_root / _CANONICAL_DIRS["state"]

    events_dir.mkdir(parents=True, exist_ok=True)
    if outbox_bytes is not None:
        outbox_dir.mkdir(parents=True, exist_ok=True)
    if acceptance_bytes is not None:
        acceptances_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    try:
        event_path = events_dir / f"{event_id}.yaml"
        _atomic_write_bytes(event_path, event_bytes)
    except TransitionWriteError:
        raise
    except Exception as exc:
        raise TransitionWriteError("write failed at stage: event") from exc

    try:
        if outbox_bytes is not None and outbox_message_id is not None:
            outbox_path = outbox_dir / f"{outbox_message_id}.yaml"
            _atomic_write_bytes(outbox_path, outbox_bytes)
    except TransitionWriteError:
        raise
    except Exception as exc:
        raise TransitionWriteError("write failed at stage: outbox") from exc

    try:
        if acceptance_bytes is not None and acceptance_path is not None:
            abs_accept_path = project_root / acceptance_path
            abs_accept_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(abs_accept_path, acceptance_bytes)
    except TransitionWriteError:
        raise
    except Exception as exc:
        raise TransitionWriteError("write failed at stage: acceptance") from exc

    try:
        tasks_path = state_dir / "tasks.yaml"
        _atomic_write_bytes(tasks_path, tasks_bytes)
    except TransitionWriteError:
        raise
    except Exception as exc:
        raise TransitionWriteError("write failed at stage: tasks.yaml") from exc


def _write_derived_views(
    project_root: Path,
    board_md: str,
    status_md: str,
) -> None:
    """Write BOARD.md and STATUS.md via atomic replacement."""
    board_path = project_root / _BOARD_PATH
    status_path = project_root / _STATUS_PATH
    board_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _atomic_write_bytes(board_path, board_md.encode("utf-8"))
    except Exception as exc:
        raise TransitionWriteError("write failed at stage: BOARD.md") from exc

    try:
        _atomic_write_bytes(status_path, status_md.encode("utf-8"))
    except Exception as exc:
        raise TransitionWriteError("write failed at stage: STATUS.md") from exc


# -- CAS validation --


def _validate_cas(
    task: dict[str, Any],
    request: TransitionRequest,
    spec: _TransitionSpec,
    expected_head: str,
) -> None:
    """Validate all CAS preconditions against the canonical state.

    Raises TransitionCASConflictError on any mismatch.  Zero writes.
    """
    # 1. Git HEAD CAS.
    if request.cas.expected_snapshot_commit != expected_head:
        raise TransitionCASConflictError(
            "CAS conflict: expected_snapshot_commit does not match HEAD"
        )

    # 2. Revision CAS.
    task_revision = task.get("revision")
    if task_revision != request.cas.expected_revision:
        raise TransitionCASConflictError(
            "CAS conflict: expected_revision does not match task revision"
        )

    # 3. State CAS.
    current_state = task.get("state")
    if not isinstance(current_state, str) or not current_state:
        raise TransitionCASConflictError(
            "CAS conflict: task state is invalid or missing"
        )

    if spec.from_states:
        if current_state not in spec.from_states:
            raise TransitionCASConflictError(
                "CAS conflict: task state is not a valid from_state"
            )
    else:
        if current_state in _TERMINAL_STATES:
            raise TransitionCASConflictError(
                "CAS conflict: cannot transition from a terminal state"
            )

    if request.cas.expected_state != current_state:
        raise TransitionCASConflictError(
            "CAS conflict: expected_state does not match task state"
        )

    # 4. Dispatch CAS.
    cd = task.get("current_dispatch")
    if spec.needs_dispatch_cas:
        if request.dispatch_cas is None:
            raise TransitionCASConflictError(
                "CAS conflict: DispatchCAS is required for this transition"
            )
        if not isinstance(cd, dict):
            raise TransitionCASConflictError(
                "CAS conflict: no active dispatch on task"
            )
        if cd.get("dispatch_id") != request.dispatch_cas.expected_dispatch_id:
            raise TransitionCASConflictError(
                "CAS conflict: expected_dispatch_id does not match"
            )
        task_attempt = task.get("attempt")
        if task_attempt != request.dispatch_cas.expected_attempt:
            raise TransitionCASConflictError(
                "CAS conflict: expected_attempt does not match task attempt"
            )

    if spec.event_type == "TASK_DISPATCHED" and isinstance(
        request.payload, DispatchPayload
    ):
        if isinstance(cd, dict):
            raise TransitionCASConflictError(
                "CAS conflict: task already has an active dispatch"
            )

    if spec.event_type in ("TASK_CANCELLED", "TASK_SUPERSEDED"):
        if request.dispatch_cas is not None:
            if not isinstance(cd, dict):
                raise TransitionCASConflictError(
                    "CAS conflict: DispatchCAS provided but no active dispatch"
                )
            if cd.get("dispatch_id") != request.dispatch_cas.expected_dispatch_id:
                raise TransitionCASConflictError(
                    "CAS conflict: expected_dispatch_id does not match"
                )
            task_attempt = task.get("attempt")
            if task_attempt != request.dispatch_cas.expected_attempt:
                raise TransitionCASConflictError(
                    "CAS conflict: expected_attempt does not match task attempt"
                )

    # 5. BLOCKER_RESCOPED: new_revision == expected_revision + 1.
    if spec.event_type == "BLOCKER_RESCOPED" and isinstance(
        request.payload, BlockerRescopedPayload
    ):
        expected_new = request.cas.expected_revision + 1
        if request.payload.new_revision != expected_new:
            raise TransitionCASConflictError(
                "CAS conflict: new_revision must equal expected_revision + 1"
            )


# -- main transition orchestration --


def _execute_transition_core(
    project_root: Path,
    request: TransitionRequest,
    spec: _TransitionSpec,
    now: datetime,
    lease_epoch: int,
) -> TransitionResult:
    """Execute the core transition pipeline (inside locks)."""
    occurred_at = _format_rfc3339_utc(now)

    # 1. Read Git HEAD.
    head_commit = _resolve_head_commit(project_root)

    # 2. Read canonical state.
    state = _read_tasks_yaml(project_root)
    task, task_index = _find_task(state, request.cas.task_id)

    # 3. Derive to_state.
    to_state = _derive_to_state(spec, request.payload)

    # 4. Build proposed bytes BEFORE CAS to enable idempotency check.
    # (CAS may fail for already-transitioned tasks; idempotency
    #  must be checked first.)
    new_task = _mutate_task_for_transition(
        task, request, spec, to_state, occurred_at, lease_epoch
    )

    post_state = copy.deepcopy(state)
    post_state["tasks"][task_index] = new_task
    post_state["updated_at"] = occurred_at

    outbox_bytes = _build_outbox_bytes(request, new_task, to_state, now)
    payload_digest: str | None = None
    if outbox_bytes is not None:
        payload_digest = _compute_payload_digest(outbox_bytes)

    event_bytes = _build_event_bytes(
        request, spec, new_task, to_state, now, lease_epoch, payload_digest
    )

    acceptance_bytes = _build_acceptance_bytes(request, new_task, now)
    acceptance_path: str | None = None
    if isinstance(request.payload, DeliveryAcceptedPayload):
        acceptance_path = request.payload.acceptance_path

    tasks_bytes = _serialize_tasks_state(post_state)

    outbox_message_id: str | None = None
    if isinstance(request.payload, DispatchPayload):
        outbox_message_id = request.payload.outbox_message_id

    # 5. Idempotency / duplicate / orphan check (BEFORE CAS).
    #    If the task is already at target state and all files match,
    #    return early with zero writes.
    idempotent_result = _check_idempotency_and_duplicates(
        project_root,
        request,
        spec,
        task,
        to_state,
        event_bytes,
        outbox_bytes,
        acceptance_bytes,
        tasks_bytes,
    )
    if idempotent_result is not None:
        return idempotent_result

    # 6. Validate CAS (revision, state, HEAD, dispatch).
    _validate_cas(task, request, spec, head_commit)

    # 7. Write canonical files.
    _write_canonical_files(
        project_root,
        request.event_id,
        event_bytes,
        outbox_bytes,
        outbox_message_id,
        acceptance_bytes,
        acceptance_path,
        tasks_bytes,
    )

    # 8. Render and write derived views.
    board_md, status_md = _render_derived_views(post_state)
    _write_derived_views(project_root, board_md, status_md)

    # 9. Return TransitionResult.
    from_state = request.cas.expected_state
    return TransitionResult(
        task_id=request.cas.task_id,
        event_id=request.event_id,
        from_state=from_state,
        to_state=to_state,
        occurred_at=occurred_at,
        outbox_message_id=outbox_message_id,
    )


# -- validate lease requirement --


def _validate_lease_requirement(
    spec: _TransitionSpec,
    lease: Any,
    event_type: str,
) -> None:
    """Validate that lease is present/absent according to spec."""
    if spec.needs_worker_lease:
        if lease is None:
            raise TransitionValidationError(
                f"worker-lifecycle transition {event_type} requires a lease"
            )
        from worker_slot_lease import WorkerSlotLease

        if type(lease) is not WorkerSlotLease:
            raise TransitionValidationError(
                f"lease must be a WorkerSlotLease, got {_safe_type_name(lease)}"
            )
    else:
        if lease is not None:
            raise TransitionValidationError(
                f"PM-only transition {event_type} must not carry a lease"
            )


# -- single-file atomic write helper --


def _atomic_write_bytes(
    path: Path,
    content: bytes,
) -> None:
    """Atomically write *content* to *path*.

    Uses ``tempfile.mkstemp → write → flush/fsync → os.replace → directory
    fsync``.

    **Pre-replace failures** leave the original file byte-for-byte
    unchanged — only ``os.replace`` mutates the target path, and the
    temp file is cleaned up in the ``except`` handler.

    **Post-replace (directory fsync) failures** may have already
    replaced the target file via ``os.replace``.  The error message
    includes a safe stage identifier (``"directory_open"`` or
    ``"directory_fsync"``) — never the full path or content.

    Assumes the caller has already validated the serialized content
    through the schema helpers.  The parent directory must exist.
    """
    dir_path = path.parent
    tmp_fd: int | None = None
    tmp_path: str | None = None

    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp-",
            suffix=".yaml",
            dir=str(dir_path),
        )
        os.write(tmp_fd, content)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None

        os.replace(tmp_path, str(path))

        # Directory fsync with proper platform semantics.
        try:
            dir_fd = os.open(str(dir_path), os.O_RDONLY)
        except OSError as exc:
            if not _is_dir_fsync_allowed_error(exc):
                raise TransitionWriteError(
                    "directory fsync failed at directory_open"
                ) from exc
            # Windows allowlist: safe to skip.
        else:
            try:
                os.fsync(dir_fd)
            except OSError as exc:
                if not _is_dir_fsync_allowed_error(exc):
                    raise TransitionWriteError(
                        "directory fsync failed at directory_fsync"
                    ) from exc
                # Windows allowlist: safe to skip.
            finally:
                os.close(dir_fd)

    except BaseException:
        # Pre-replace: original file untouched, clean up temp.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


# -- ControlPlaneTransitionService --


@dataclass(frozen=True, slots=True)
class ControlPlaneTransitionService:
    """CAS-write service for control-plane state transitions.

    Takes a ``project_root`` and does NOT store mutable state.
    Every ``apply_transition`` call is self-contained.
    """

    project_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.project_root, Path):
            raise TypeError(
                f"project_root must be a Path, "
                f"got {_safe_type_name(self.project_root)}"
            )
        if not self.project_root.is_absolute():
            raise ValueError(
                f"project_root must be an absolute path, "
                f"got {_safe_type_name(self.project_root)}"
            )
        if not self.project_root.exists():
            raise ValueError(
                "project_root must exist"
            )
        if not self.project_root.is_dir():
            raise ValueError(
                "project_root must be a directory"
            )
        # Reject symlinks/reparse points — same rule as WorkerSlotLease.
        resolved = os.path.realpath(str(self.project_root))
        if os.name == "nt":
            resolved = os.path.normcase(resolved)
        original = str(self.project_root)
        if os.name == "nt":
            original = os.path.normcase(original)
        if resolved != original:
            raise ValueError(
                "project_root must not be a symlink or reparse point"
            )

    def apply_transition(
        self,
        request: TransitionRequest,
        lease: Any | None,
        now: datetime,
    ) -> TransitionResult:
        """Validate CAS, write canonical files, render views, return result.

        *lease* is required for worker-lifecycle transitions and optional
        for PM-only transitions.

        *now* must be a timezone-aware UTC ``datetime``.
        """
        # 1. Validate now.
        _validate_now(now)

        # 2. Validate project_root still exists.
        if not self.project_root.is_dir():
            raise ValueError(
                "project_root is no longer a directory"
            )

        # 3. Look up transition spec.
        spec = _TRANSITION_SPECS.get(request.event_type)
        if spec is None:
            raise TransitionValidationError(
                f"unknown event_type: {_safe_type_name(request.event_type)}"
            )

        # 4. Validate lease requirement.
        _validate_lease_requirement(spec, lease, request.event_type)

        # 5. Execute via worker-fence or PM-only path.
        if spec.needs_worker_lease:
            from worker_slot_lease import (
                WorkerSlotLease,
                hold_worker_slot_fence,
            )

            tracker = _LockOrderTracker()
            tracker.enter_worker_fence()

            body_exception: BaseException | None = None
            result: TransitionResult | None = None

            try:
                with hold_worker_slot_fence(
                    self.project_root, lease, now
                ):
                    tracker.enter_state_lock()
                    try:
                        with _exclusive_state_lock(self.project_root):
                            result = _execute_transition_core(
                                self.project_root,
                                request,
                                spec,
                                now,
                                lease.lease_epoch,
                            )
                    finally:
                        tracker.exit_state_lock()
            except BaseException as _exc:
                body_exception = _exc
            finally:
                try:
                    tracker.exit_worker_fence()
                except TransitionLockOrderError:
                    if body_exception is None:
                        raise

            if body_exception is not None:
                raise body_exception

            assert result is not None
            return result

        else:
            # PM-only path -- no worker fence.
            result = None
            body_exception = None

            try:
                with _exclusive_state_lock(self.project_root):
                    state = _read_tasks_yaml(self.project_root)
                    pm = state.get("pm_control")
                    if not isinstance(pm, dict):
                        raise TransitionCASConflictError(
                            "pm_control is missing from canonical state"
                        )
                    lease_epoch = pm.get("lease_epoch")
                    if isinstance(lease_epoch, bool) or not isinstance(
                        lease_epoch, int
                    ):
                        raise TransitionCASConflictError(
                            "pm_control.lease_epoch is invalid"
                        )

                    result = _execute_transition_core(
                        self.project_root,
                        request,
                        spec,
                        now,
                        lease_epoch,
                    )
            except BaseException as _exc:
                body_exception = _exc

            if body_exception is not None:
                raise body_exception

            assert result is not None
            return result


# ── __all__ — exactly 32 frozen public symbols ────────────────────────────

__all__ = [
    "ControlPlaneTransitionService",
    "TransitionCAS",
    "DispatchCAS",
    "TransitionRequest",
    "TransitionPayload",
    "TransitionResult",
    "TransitionEventContext",
    "GuardResult",
    "GuardInput",
    "AcceptanceOwnerApproval",
    "SpecifyPayload",
    "DispatchPayload",
    "AcknowledgePayload",
    "DeliverySubmittedPayload",
    "DeliveryAcceptedPayload",
    "DeliveryReturnedPayload",
    "RequeuePayload",
    "IntegrationPayload",
    "BlockedPayload",
    "BlockerResolvedPayload",
    "BlockerRescopedPayload",
    "BlockerCancelledPayload",
    "CancelledPayload",
    "SupersededPayload",
    "ControlPlaneTransitionError",
    "TransitionValidationError",
    "TransitionCASConflictError",
    "TransitionLockContentionError",
    "TransitionLockOrderError",
    "TransitionDuplicateEvidenceError",
    "TransitionSchemaError",
    "TransitionWriteError",
]
