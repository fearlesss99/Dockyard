"""StateProvider — read-only canonical project state snapshot (TC-13.17b).

Interface #21 production module.  Provides a single frozen snapshot of
all five canonical input sources with strict fail-closed validation.

Public API
----------
``StateProvider``       — read-only service boundary
``StateProvider.snapshot()`` — execute the multi-file consistency protocol
``StateSnapshot``       — frozen/slots dataclass with all parsed data
``TaskEntry``           — frozen task record (25 fields)
``TaskTimestamps``      — frozen timestamps sub-record (9 fields)
``DispatchInfo``        — frozen dispatch sub-record (8 fields)
``EventEntry``          — frozen event record (18 fields)
``GuardResult``         — frozen guard sub-record (5 fields)
``OutboxEntry``         — frozen outbox record (14 fields)
``OutboxPayload``       — frozen outbox payload sub-record (5 fields)
``AcceptanceEntry``     — frozen acceptance record (16 fields)
``MadRefEntry``         — frozen mad-ref record (10 fields, from mad_refs.py)
``ModelSelectionSnapshot`` — frozen model selection (10 fields)

Non-goals
---------
* File writes, lock acquisition, subprocess spawn, Git operations.
* Network, API, or model calls.
* Importing write-end modules (control_plane_transition, worker_adapter,
  approval_gate).
* Reading derived views, MAD archives, or runtime lease state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

__all__ = [
    "AcceptanceEntry",
    "DispatchInfo",
    "EventEntry",
    "GuardResult",
    "MadRefEntry",
    "ModelSelectionSnapshot",
    "OutboxEntry",
    "OutboxPayload",
    "StateProvider",
    "StateProviderError",
    "StateProviderInconsistentSnapshotError",
    "StateProviderInputError",
    "StateProviderNotFoundError",
    "StateProviderSchemaError",
    "StateProviderSnapshotChangedError",
    "StateSnapshot",
    "TaskEntry",
    "TaskTimestamps",
]

# ── constants ─────────────────────────────────────────────────────────────

_SCHEMA_TASKS = "agentdesk.tasks/v2"
_SCHEMA_EVENT = "agentdesk.state-event/v2"
_SCHEMA_OUTBOX = "agentdesk.outbox-message/v2"
_SCHEMA_ACCEPTANCE = "agentdesk.acceptance/v2"
_SCHEMA_MAD_REFS = "agentdesk.mad-refs/v1"

_STATES: frozenset[str] = frozenset({
    "draft", "ready", "dispatched", "in_progress",
    "review_ready", "returned", "blocked",
    "accepted", "integrated", "cancelled", "superseded",
})

_EVENT_TYPES: frozenset[str] = frozenset({
    "TASK_SPECIFIED", "TASK_DISPATCHED", "DISPATCH_ACKNOWLEDGED",
    "DELIVERY_SUBMITTED", "DELIVERY_ACCEPTED", "DELIVERY_RETURNED",
    "TASK_REQUEUED", "CHANGE_INTEGRATED", "INTEGRATION_FAILED",
    "TASK_BLOCKED", "BLOCKER_RESOLVED", "BLOCKER_RESCOPED",
    "BLOCKER_CANCELLED", "TASK_CANCELLED", "TASK_SUPERSEDED",
})

_ADOPTION_LEVELS: frozenset[str] = frozenset({"lite", "standard", "automated"})
_PM_MODES: frozenset[str] = frozenset({"manual", "timed"})
_DELIVERY_STATES: frozenset[str] = frozenset(
    {"none", "working", "submitted", "invalid", "accepted", "rejected"}
)
_INTEGRATION_STATES: frozenset[str] = frozenset(
    {"not_applicable", "pending", "integrated", "failed"}
)
_BLOCKED_KINDS: frozenset[str] = frozenset({
    "external_approval", "credentials", "environment", "dependency",
    "role_timeout", "report_unreachable", "integration_conflict",
    "decision_required", "other",
})
_TASK_TYPES: frozenset[str] = frozenset(
    {"implementation", "qa", "integration", "review",
     "architecture", "docs", "ops", "spike"}
)
_ACCEPTANCE_DECISIONS: frozenset[str] = frozenset(
    {"accepted", "returned", "blocked"}
)
_MAD_PURPOSES: frozenset[str] = frozenset({"planning", "audit"})
_MAD_DEPTHS: frozenset[str] = frozenset({"fast", "balanced", "deep"})

# Regex patterns
_TASK_ID_RE = re.compile(r"^TC-[0-9]{3,}$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^EVT-.+")
_MESSAGE_ID_RE = re.compile(r"^MSG-.+")
_EVENT_ID_FILENAME_RE = re.compile(r"^EVT-[A-Za-z0-9][-A-Za-z0-9._]*\.yaml$")
_ACCEPTANCE_FILENAME_RE = re.compile(
    r"^(TC-[0-9]{3,})-r([1-9][0-9]*)-a([1-9][0-9]*)-review([1-9][0-9]*)\.md$"
)
_OUTBOX_FILENAME_RE = re.compile(r"^MSG-[A-Za-z0-9][-A-Za-z0-9._]*\.yaml$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Canonical paths relative to project_root
_TASKS_PATH = Path("docs/pm/state/tasks.yaml")
_EVENTS_DIR = Path("docs/pm/events")
_OUTBOX_DIR = Path("docs/pm/outbox")
_ACCEPTANCES_DIR = Path("docs/pm/acceptances")
_MAD_REFS_PATH = Path(".agentdesk/runtime/mad-refs.yaml")

# Mad-ref field names (exactly matching mad_refs.py _REF_FIELD_NAMES)
_MAD_REF_FIELD_NAMES: tuple[str, ...] = (
    "task_id", "dispatch_id", "purpose", "deliberation_id", "depth",
    "stdout_sha256", "report_sha256", "status", "archive_path", "created_at",
)
_MAD_REF_FIELD_SET: frozenset[str] = frozenset(_MAD_REF_FIELD_NAMES)

# Mad-ref root field names
_MAD_REF_ROOT_FIELDS: frozenset[str] = frozenset(
    {"schema_version", "updated_at", "refs"}
)

# Tasks root required keys
_TASKS_ROOT_KEYS: frozenset[str] = frozenset(
    {"schema_version", "project_id", "adoption_level",
     "updated_at", "pm_control", "tasks"}
)

# Task fields (24, matching validate_project.py TASK_FIELDS)
_TASK_FIELDS: tuple[str, ...] = (
    "task_id", "revision", "task_card_path", "task_card_commit",
    "state", "attempt", "current_dispatch", "report_path",
    "granted_approval_ids", "delivery_state", "integration_state",
    "implementation_commit", "report_commit", "accepted_commit",
    "acceptance_path", "integrated_commit",
    "blocked_reason", "blocked_kind", "blocked_owner",
    "unblock_condition", "review_after", "blocked_attempt_valid",
    "resume_state", "timestamps",
)

# Timestamp fields (9)
_TIMESTAMP_FIELDS: tuple[str, ...] = (
    "created_at", "ready_at", "dispatched_at", "started_at",
    "delivered_at", "blocked_at", "accepted_at", "integrated_at",
    "updated_at",
)

# Dispatch info fields (8)
_DISPATCH_FIELDS: tuple[str, ...] = (
    "dispatch_id", "attempt_id", "role_id", "base_commit",
    "branch", "dispatched_at", "model_selection",
)

# Event common fields
_EVENT_COMMON_FIELDS: tuple[str, ...] = (
    "schema_version", "event_id", "event_type", "task_id",
    "revision", "attempt", "dispatch_id", "from_state", "to_state",
    "lease_epoch", "actor_role_id", "occurred_at",
    "source_message_id", "evidence_refs", "guard_results",
)

# Outbox fields
_OUTBOX_FIELDS: tuple[str, ...] = (
    "schema_version", "message_id", "event_id", "message_type",
    "dedupe_key", "task_id", "revision", "attempt", "dispatch_id",
    "destination_role_id", "created_at", "model_selection", "payload",
)

# Model selection fields (10)
_MODEL_SELECTION_FIELDS: tuple[str, ...] = (
    "required_model_tier", "required_model_capabilities",
    "model_binding_id", "selected_model_provider", "selected_model_id",
    "selected_model_tier", "selected_deliberation_tier",
    "selected_context_window_tokens", "selected_model_capabilities",
    "model_degradation_approval_id",
)

# Outbox payload fields
_OUTBOX_PAYLOAD_FIELDS: tuple[str, ...] = (
    "task_path", "task_card_commit", "base_commit", "branch", "report_path",
)

# Guard result fields
_GUARD_FIELDS: tuple[str, ...] = (
    "guard", "inputs", "result", "checked_at", "evidence_ref",
)

# Acceptance frontmatter fields (matching validate_project.py)
_ACCEPTANCE_FRONTMATTER_KEYS: frozenset[str] = frozenset({
    "schema_version", "task_id", "revision", "attempt",
    "implementation_commit", "report_commit", "base_commit",
    "decision", "accepted_commit", "reviewed_dispatch_id",
    "type", "owner_approval",
})

# CHANGE_INTEGRATED extra fields
_CHANGE_INTEGRATED_EXTRA_FIELDS: tuple[str, ...] = (
    "accepted_commit", "integrated_commit", "equivalence_method",
    "equivalence_result", "equivalence_evidence_ref",
)


# ── internal helpers ─────────────────────────────────────────────────────


def _is_int(value: Any) -> bool:
    """True if *value* is int and not bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bool(value: Any) -> bool:
    """True if *value* is strictly bool."""
    return isinstance(value, bool)


def _require_str(value: Any) -> str:
    """Return *value* if it is a non-empty str, else raise."""
    if not isinstance(value, str) or not value:
        raise StateProviderSchemaError(
            "expected non-empty string field value"
        )
    return value


def _require_int_ge(value: Any, minimum: int) -> int:
    """Return *value* if it is a non-bool int >= *minimum*."""
    if not _is_int(value) or value < minimum:
        raise StateProviderSchemaError(
            "expected integer field value"
        )
    return value


def _sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _freeze_strings(value: str) -> str:
    """Intern a string to reduce memory."""
    return sys.intern(value)


def _make_immutable(obj: Any) -> Any:
    """Recursively convert lists to tuples and dicts to MappingProxyType."""
    if isinstance(obj, dict):
        return MappingProxyType(
            {k: _make_immutable(v) for k, v in obj.items()}
        )
    if isinstance(obj, list):
        return tuple(_make_immutable(v) for v in obj)
    if isinstance(obj, str):
        return sys.intern(obj)
    return obj


def _stable_sort(entries: list[Path]) -> list[Path]:
    """Sort *entries* by name using locale-independent byte ordering."""
    return sorted(entries, key=lambda p: p.name.encode("utf-8"))


def _read_bytes(path: Path) -> bytes:
    """Read raw bytes from *path*; raise OSError on failure."""
    return path.read_bytes()


def _read_text(path: Path) -> str:
    """Read UTF-8 text from *path*; raise on failure."""
    return path.read_text(encoding="utf-8")


def _parse_json(raw: bytes, description: str) -> dict[str, Any]:
    """Parse JSON-compatible YAML bytes, returning a dict.

    Raises StateProviderSchemaError on parse failure or non-dict root.
    """
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise StateProviderSchemaError(
            f"{description} is not valid JSON-compatible YAML"
        ) from None
    if not isinstance(value, dict):
        raise StateProviderSchemaError(
            f"{description} root must be an object"
        )
    return value


def _validate_extra_keys(
    data: dict[str, Any], allowed: frozenset[str], description: str
) -> None:
    """Reject unexpected keys."""
    extra = set(data.keys()) - allowed
    if extra:
        raise StateProviderSchemaError(
            f"{description} has forbidden key(s)"
        )


def _validate_missing_keys(
    data: dict[str, Any], required: frozenset[str], description: str
) -> None:
    """Reject missing required keys."""
    missing = required - set(data.keys())
    if missing:
        raise StateProviderSchemaError(
            f"{description} is missing required key(s)"
        )


# ── YAML subset frontmatter parser ────────────────────────────────────────


def _parse_frontmatter(raw: str, description: str) -> dict[str, Any]:
    """Parse a minimal YAML subset from markdown frontmatter.

    Handles:
    - Top-level scalar keys (string, int, bool, null)
    - One level of nested mapping (owner_approval: {gate: none, ...})
    - Inline lists (approval_ids: [])
    - Inline null / empty values

    This is intentionally a stripped-down parser — the file structure
    was already validated by the writer.  Malformed frontmatter raises
    StateProviderSchemaError.

    No import from validate_project.py — this is a self-contained
    read-only parser.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise StateProviderSchemaError(
            f"{description} must start with YAML frontmatter"
        )
    try:
        closing = next(
            i for i, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        raise StateProviderSchemaError(
            f"{description} has no closing frontmatter delimiter"
        )
    fm_lines = lines[1:closing]
    result: dict[str, Any] = {}
    current_map_key: str | None = None
    current_map: dict[str, Any] = {}

    for line in fm_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Indented line (2 spaces) — belongs to current nested mapping
        if line.startswith("  ") and not line.startswith("    "):
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if current_map_key is None:
                    # Start a new nested map
                    current_map = {}
                if val:
                    current_map[key] = _parse_yaml_scalar(val)
                else:
                    # Empty value — could be a list like "[]" on the next line
                    # or genuinely null/empty
                    current_map[key] = None
            continue

        # Top-level key: flush any pending nested map
        if current_map_key is not None and current_map:
            result[current_map_key] = dict(current_map)
            current_map_key = None
            current_map = {}

        if ":" in stripped:
            current_map_key, _, val = stripped.partition(":")
            current_map_key = current_map_key.strip()
            val = val.strip()
            current_map = {}
            if val:
                result[current_map_key] = _parse_yaml_scalar(val)
                current_map_key = None

    # Flush final pending nested map
    if current_map_key is not None and current_map:
        result[current_map_key] = dict(current_map)

    return result


def _parse_yaml_scalar(val: str) -> Any:
    """Parse a YAML scalar value — str, int, bool, None, or inline empty list."""
    if not val:
        return ""
    # Quoted string
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    # Empty list literal
    if val == "[]":
        return []
    # Null
    if val in ("null", "~", ""):
        return None
    # Bool
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    # Int
    try:
        return int(val)
    except ValueError:
        pass
    # String
    return val


# ── frozen dataclasses ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelSelectionSnapshot:
    """Frozen ten-field model selection (matching validate_project.py)."""

    required_model_tier: str
    required_model_capabilities: tuple[str, ...]
    model_binding_id: str
    selected_model_provider: str
    selected_model_id: str
    selected_model_tier: str
    selected_deliberation_tier: str
    selected_context_window_tokens: int
    selected_model_capabilities: tuple[str, ...]
    model_degradation_approval_id: str | None


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Frozen guard result entry."""

    guard: str
    inputs: tuple[tuple[str, object], ...]
    result: str
    checked_at: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class OutboxPayload:
    """Frozen outbox payload sub-record."""

    task_path: str
    task_card_commit: str
    base_commit: str
    branch: str
    report_path: str


@dataclass(frozen=True, slots=True)
class TaskTimestamps:
    """Frozen task timestamps sub-record (9 fields)."""

    created_at: str
    ready_at: str | None
    dispatched_at: str | None
    started_at: str | None
    delivered_at: str | None
    blocked_at: str | None
    accepted_at: str | None
    integrated_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class DispatchInfo:
    """Frozen dispatch identity sub-record (8 fields)."""

    dispatch_id: str
    attempt_id: str
    role_id: str
    base_commit: str
    branch: str
    dispatched_at: str
    model_selection: ModelSelectionSnapshot
    raw_dispatch: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TaskEntry:
    """Frozen task record (25 fields)."""

    task_id: str
    revision: int
    task_card_path: str
    task_card_commit: str
    state: str
    attempt: int | None
    current_dispatch: DispatchInfo | None
    report_path: str | None
    granted_approval_ids: tuple[str, ...] | None
    delivery_state: str | None
    integration_state: str | None
    implementation_commit: str | None
    report_commit: str | None
    accepted_commit: str | None
    acceptance_path: str | None
    integrated_commit: str | None
    blocked_reason: str | None
    blocked_kind: str | None
    blocked_owner: str | None
    unblock_condition: str | None
    review_after: str | None
    blocked_attempt_valid: bool | None
    resume_state: str | None
    timestamps: TaskTimestamps
    raw_task: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EventEntry:
    """Frozen event record covering all 15 event types (18 fields)."""

    schema_version: str
    event_id: str
    event_type: str
    task_id: str
    revision: int
    attempt: int | None
    dispatch_id: str | None
    from_state: str
    to_state: str
    lease_epoch: int
    actor_role_id: str
    occurred_at: str
    source_message_id: str | None
    evidence_refs: tuple[str, ...]
    guard_results: tuple[GuardResult, ...]
    payload_digest: str | None           # TASK_DISPATCHED only
    extra_fields: tuple[tuple[str, object], ...] | None  # CHANGE_INTEGRATED only
    raw_event: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    """Frozen outbox record (14 fields)."""

    schema_version: str
    message_id: str
    event_id: str
    message_type: str
    dedupe_key: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    destination_role_id: str
    created_at: str
    model_selection: ModelSelectionSnapshot
    payload: OutboxPayload
    raw_outbox: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AcceptanceEntry:
    """Frozen acceptance record (16 fields)."""

    schema_version: str
    task_id: str
    revision: int
    attempt: int
    review_n: int
    decision: str
    implementation_commit: str
    report_commit: str
    base_commit: str | None
    accepted_commit: str | None
    reviewed_dispatch_id: str
    type: str
    owner_approval_gate: str
    owner_approval_ids: tuple[str, ...]
    raw_body: str
    filename: str


@dataclass(frozen=True, slots=True)
class MadRefEntry:
    """Frozen mad-ref record — exactly 10 fields (matching mad_refs.py)."""

    task_id: str
    dispatch_id: str
    purpose: str
    deliberation_id: str
    depth: str
    stdout_sha256: str
    report_sha256: str
    status: str
    archive_path: str
    created_at: str


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Frozen state snapshot with all parsed data (14 fields)."""

    project_root: Path
    schema_version: str
    project_id: str
    adoption_level: str
    updated_at: str
    pm_holder_id: str
    pm_lease_epoch: int
    pm_mode: str
    tasks: tuple[TaskEntry, ...]
    events: tuple[EventEntry, ...]
    outbox: tuple[OutboxEntry, ...]
    acceptances: tuple[AcceptanceEntry, ...]
    mad_refs: tuple[MadRefEntry, ...] | None
    read_hexsha: str


# ── exceptions ────────────────────────────────────────────────────────────


class StateProviderError(Exception):
    """Base for all StateProvider errors."""


class StateProviderInputError(StateProviderError):
    """Invalid project_root — not absolute or not a Path."""


class StateProviderNotFoundError(StateProviderError):
    """Required canonical file or directory missing."""


class StateProviderSchemaError(StateProviderError):
    """Schema violation — version mismatch, invalid type, extra/missing keys,
    corrupt JSON/YAML."""


class StateProviderSnapshotChangedError(StateProviderError):
    """Any input changed between the two snapshot passes."""


class StateProviderInconsistentSnapshotError(StateProviderError):
    """Cross-file referential integrity violation — orphan event,
    partial transition, duplicate ID, missing companion evidence."""


# ── StateProvider ─────────────────────────────────────────────────────────


class StateProvider:
    """Read-only service boundary for canonical project state.

    Usage::

        provider = StateProvider(project_root=Path("/abs/path"))
        snapshot = provider.snapshot()

    Construction validates only that *project_root* is an absolute Path;
    all file I/O and validation happens in :meth:`snapshot`.
    """

    __slots__ = ("_project_root",)

    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path):
            raise StateProviderInputError(
                "project_root must be a pathlib.Path"
            )
        if not project_root.is_absolute():
            raise StateProviderInputError(
                "project_root must be an absolute path"
            )
        self._project_root = project_root

    def snapshot(self) -> StateSnapshot:
        """Execute the five-source consistency protocol and return a frozen snapshot.

        Raises:
            StateProviderNotFoundError: required file/dir missing.
            StateProviderSchemaError: schema violation.
            StateProviderSnapshotChangedError: state changed between passes.
            StateProviderInconsistentSnapshotError: cross-file integrity failure.
        """
        root = self._project_root
        _ensure_required_exist(root)

        # ── Pass A: read all five sources ──
        a_tasks_bytes = _read_bytes(root / _TASKS_PATH)
        a_events = _read_dir_entries(root / _EVENTS_DIR, ".yaml",
                                     _EVENT_ID_FILENAME_RE)
        a_outbox = _read_dir_entries(root / _OUTBOX_DIR, ".yaml",
                                     _OUTBOX_FILENAME_RE)
        a_acceptances = _read_dir_entries(root / _ACCEPTANCES_DIR, ".md", None)
        a_mad_refs_bytes = _read_opt_bytes(root / _MAD_REFS_PATH)

        # ── Pass B: re-read all five sources ──
        b_tasks_bytes = _read_bytes(root / _TASKS_PATH)
        b_events = _read_dir_entries(root / _EVENTS_DIR, ".yaml",
                                     _EVENT_ID_FILENAME_RE)
        b_outbox = _read_dir_entries(root / _OUTBOX_DIR, ".yaml",
                                     _OUTBOX_FILENAME_RE)
        b_acceptances = _read_dir_entries(root / _ACCEPTANCES_DIR, ".md", None)
        b_mad_refs_bytes = _read_opt_bytes(root / _MAD_REFS_PATH)

        # ── Compute integrity hash over all sources ──
        a_hash = _compute_pass_hash(
            a_tasks_bytes, a_events, a_outbox, a_acceptances, a_mad_refs_bytes
        )
        b_hash = _compute_pass_hash(
            b_tasks_bytes, b_events, b_outbox, b_acceptances, b_mad_refs_bytes
        )

        if a_hash != b_hash:
            raise StateProviderSnapshotChangedError(
                "snapshot changed between first and second read"
            )

        # ── Parse all sources ──
        tasks_doc = _parse_json(a_tasks_bytes, "tasks.yaml")
        events_raw_list = [
            (fn, _parse_json(raw, fn))
            for fn, raw in a_events
        ]
        outbox_raw_list = [
            (fn, _parse_json(raw, fn))
            for fn, raw in a_outbox
        ]
        acceptance_raw_list = [
            (fn, _parse_acceptance(raw, fn))
            for fn, raw in a_acceptances
        ]
        mad_refs_doc = None
        mad_refs_raw_list: list[dict[str, Any]] = []
        if a_mad_refs_bytes is not None:
            mad_refs_doc = _parse_json(a_mad_refs_bytes, "mad-refs.yaml")
            mad_refs_raw_list = mad_refs_doc.get("refs", [])
            if not isinstance(mad_refs_raw_list, list):
                mad_refs_raw_list = []

        # ── Build parsed collections ──
        tasks_tuple = _build_tasks(tasks_doc)
        events_tuple = _build_events(events_raw_list)
        outbox_tuple = _build_outbox(outbox_raw_list)
        acceptances_tuple = _build_acceptances(acceptance_raw_list)
        mad_refs_tuple: tuple[MadRefEntry, ...] | None = None
        if mad_refs_doc is not None:
            mad_refs_tuple = _build_mad_refs(mad_refs_doc)

        # ── Cross-file consistency ──
        _validate_cross_consistency(
            tasks_tuple, events_tuple, outbox_tuple,
            acceptances_tuple, mad_refs_tuple,
            a_events, a_outbox, a_acceptances,
        )

        # ── Build frozen snapshot ──
        pm = tasks_doc["pm_control"]
        return StateSnapshot(
            project_root=root,
            schema_version=_freeze_strings(tasks_doc["schema_version"]),
            project_id=_freeze_strings(tasks_doc["project_id"]),
            adoption_level=_freeze_strings(tasks_doc["adoption_level"]),
            updated_at=_freeze_strings(tasks_doc["updated_at"]),
            pm_holder_id=_freeze_strings(pm["holder_id"]),
            pm_lease_epoch=_require_int_ge(pm["lease_epoch"], 1),
            pm_mode=_freeze_strings(pm["mode"]),
            tasks=tasks_tuple,
            events=events_tuple,
            outbox=outbox_tuple,
            acceptances=acceptances_tuple,
            mad_refs=mad_refs_tuple,
            read_hexsha=a_hash,
        )


# ── filesystem helpers ────────────────────────────────────────────────────


def _ensure_required_exist(root: Path) -> None:
    """Verify required files and directories exist.

    Raises StateProviderNotFoundError if any required path is missing.
    """
    tasks_path = root / _TASKS_PATH
    if not tasks_path.is_file():
        raise StateProviderNotFoundError(
            "required canonical file not found"
        )
    for dir_path in (_EVENTS_DIR, _OUTBOX_DIR, _ACCEPTANCES_DIR):
        full = root / dir_path
        if not full.is_dir():
            raise StateProviderNotFoundError(
                "required canonical directory not found"
            )


def _read_dir_entries(
    dir_path: Path, suffix: str, name_pattern: re.Pattern | None,
) -> list[tuple[str, bytes]]:
    """Read all files in *dir_path* matching *suffix* (and optionally
    *name_pattern*), returning a stable-sorted list of (filename, raw_bytes).

    Raises StateProviderSchemaError if any file doesn't match the pattern.
    """
    try:
        entries = [p for p in dir_path.iterdir()
                   if p.is_file() and not p.is_symlink()
                   and p.suffix == suffix]
    except OSError:
        raise StateProviderNotFoundError(
            "cannot read canonical directory"
        ) from None
    result: list[tuple[str, bytes]] = []
    for entry in _stable_sort(entries):
        if name_pattern is not None:
            if not name_pattern.fullmatch(entry.name):
                raise StateProviderSchemaError(
                    "filename does not match expected pattern"
                )
        try:
            raw = entry.read_bytes()
        except OSError:
            raise StateProviderNotFoundError(
                "cannot read canonical file"
            ) from None
        result.append((entry.name, raw))
    return result


def _read_opt_bytes(path: Path) -> bytes | None:
    """Read raw bytes from *path* if it exists, else None."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise StateProviderNotFoundError(
            "cannot read optional runtime file"
        ) from None


def _compute_pass_hash(
    tasks_bytes: bytes,
    events: list[tuple[str, bytes]],
    outbox: list[tuple[str, bytes]],
    acceptances: list[tuple[str, bytes]],
    mad_refs_bytes: bytes | None,
) -> str:
    """Compute a SHA-256 hash over all five-source raw data."""
    h = hashlib.sha256()
    h.update(tasks_bytes)
    for name, raw in events:
        h.update(name.encode("utf-8"))
        h.update(raw)
    for name, raw in outbox:
        h.update(name.encode("utf-8"))
        h.update(raw)
    for name, raw in acceptances:
        h.update(name.encode("utf-8"))
        h.update(raw)
    if mad_refs_bytes is not None:
        h.update(b"mad-refs\x00")
        h.update(mad_refs_bytes)
    else:
        h.update(b"mad-refs\x00none")
    return h.hexdigest()


# ── parse / build ─────────────────────────────────────────────────────────


def _build_tasks(doc: dict[str, Any]) -> tuple[TaskEntry, ...]:
    """Parse and validate tasks.yaml, returning a tuple of TaskEntry."""
    # Validate root
    _validate_missing_keys(doc, _TASKS_ROOT_KEYS, "tasks.yaml root")
    _validate_extra_keys(doc, _TASKS_ROOT_KEYS, "tasks.yaml root")

    sv = doc["schema_version"]
    if sv != _SCHEMA_TASKS:
        raise StateProviderSchemaError(
            "tasks.yaml schema_version is not agentdesk.tasks/v2"
        )
    if not isinstance(doc["project_id"], str) or not doc["project_id"]:
        raise StateProviderSchemaError("tasks.yaml project_id must be non-empty")
    if doc["adoption_level"] not in _ADOPTION_LEVELS:
        raise StateProviderSchemaError(
            "tasks.yaml adoption_level is invalid"
        )
    if not isinstance(doc["updated_at"], str) or not doc["updated_at"]:
        raise StateProviderSchemaError("tasks.yaml updated_at must be non-empty")
    _validate_pm_control(doc["pm_control"])

    tasks_raw = doc.get("tasks")
    if not isinstance(tasks_raw, list):
        raise StateProviderSchemaError("tasks.yaml tasks must be an array")

    seen_task_ids: set[str] = set()
    result: list[TaskEntry] = []
    for i, raw_task in enumerate(tasks_raw):
        if not isinstance(raw_task, dict):
            raise StateProviderSchemaError(
                f"tasks[{i}] must be an object"
            )
        task_entry = _build_single_task(raw_task, i, seen_task_ids)
        result.append(task_entry)
    return tuple(result)


def _validate_pm_control(pm: Any) -> None:
    """Validate pm_control object."""
    if not isinstance(pm, dict):
        raise StateProviderSchemaError("pm_control must be an object")
    allowed = frozenset({"holder_id", "lease_epoch", "mode"})
    _validate_extra_keys(pm, allowed, "pm_control")
    _validate_missing_keys(pm, allowed, "pm_control")
    if not isinstance(pm["holder_id"], str) or not pm["holder_id"]:
        raise StateProviderSchemaError("pm_control.holder_id must be non-empty")
    if not _is_int(pm["lease_epoch"]) or pm["lease_epoch"] < 1:
        raise StateProviderSchemaError(
            "pm_control.lease_epoch must be an integer >= 1"
        )
    if pm["mode"] not in _PM_MODES:
        raise StateProviderSchemaError("pm_control.mode is invalid")


def _build_single_task(
    raw: dict[str, Any], index: int, seen: set[str]
) -> TaskEntry:
    """Build a single TaskEntry from raw task dict."""
    allowed = frozenset(_TASK_FIELDS)
    _validate_extra_keys(raw, allowed, f"task at index {index}")
    _validate_missing_keys(raw, allowed, f"task at index {index}")

    task_id = _validate_task_id(raw.get("task_id"))
    if task_id in seen:
        raise StateProviderSchemaError("duplicate task_id")
    seen.add(task_id)

    revision = _require_int_ge(raw.get("revision"), 1)

    task_card_path = _require_str(raw.get("task_card_path"))
    task_card_commit = _validate_sha40(raw.get("task_card_commit"), required=True)
    state = _validate_state(raw.get("state"))

    attempt_raw = raw.get("attempt")
    attempt: int | None = None
    if attempt_raw is not None:
        attempt = _require_int_ge(attempt_raw, 1)
    elif state in ("dispatched", "in_progress", "review_ready"):
        raise StateProviderSchemaError(
            "attempt must be set for active dispatch state"
        )

    dispatch = _build_dispatch(raw.get("current_dispatch"), state)

    report_path = _validate_str_or_none(raw.get("report_path"))

    granted_raw = raw.get("granted_approval_ids")
    granted: tuple[str, ...] | None = None
    if granted_raw is not None:
        if not isinstance(granted_raw, list):
            raise StateProviderSchemaError(
                "granted_approval_ids must be a list"
            )
        for gid in granted_raw:
            if not isinstance(gid, str) or not gid:
                raise StateProviderSchemaError(
                    "granted_approval_ids entries must be non-empty strings"
                )
        granted = tuple(sys.intern(str(g)) for g in granted_raw)

    delivery_state = _validate_str_or_none(raw.get("delivery_state"))
    if delivery_state is not None and delivery_state not in _DELIVERY_STATES:
        raise StateProviderSchemaError("delivery_state is invalid")
    integration_state = _validate_str_or_none(raw.get("integration_state"))
    if integration_state is not None and integration_state not in _INTEGRATION_STATES:
        raise StateProviderSchemaError("integration_state is invalid")

    impl_commit = _validate_sha40(raw.get("implementation_commit"), required=False)
    report_commit = _validate_sha40(raw.get("report_commit"), required=False)
    accepted_commit = _validate_sha40(raw.get("accepted_commit"), required=False)
    acceptance_path = _validate_str_or_none(raw.get("acceptance_path"))
    integrated_commit = _validate_sha40(raw.get("integrated_commit"), required=False)

    blocked_reason = _validate_str_or_none(raw.get("blocked_reason"))
    blocked_kind = _validate_str_or_none(raw.get("blocked_kind"))
    if blocked_kind is not None and blocked_kind not in _BLOCKED_KINDS:
        raise StateProviderSchemaError("blocked_kind is invalid")
    blocked_owner = _validate_str_or_none(raw.get("blocked_owner"))
    unblock_condition = _validate_str_or_none(raw.get("unblock_condition"))
    review_after = _validate_str_or_none(raw.get("review_after"))

    bav = raw.get("blocked_attempt_valid")
    if bav is not None and not _is_bool(bav):
        raise StateProviderSchemaError(
            "blocked_attempt_valid must be bool or null"
        )

    resume_state = _validate_str_or_none(raw.get("resume_state"))
    if resume_state is not None and resume_state not in _STATES:
        raise StateProviderSchemaError("resume_state is invalid")

    timestamps = _build_timestamps(raw.get("timestamps"))

    raw_immutable = _make_immutable(raw)

    return TaskEntry(
        task_id=_freeze_strings(task_id),
        revision=revision,
        task_card_path=_freeze_strings(task_card_path),
        task_card_commit=task_card_commit,
        state=_freeze_strings(state),
        attempt=attempt,
        current_dispatch=dispatch,
        report_path=_freeze_strings(report_path) if report_path else None,
        granted_approval_ids=granted,
        delivery_state=_freeze_strings(delivery_state) if delivery_state else None,
        integration_state=_freeze_strings(integration_state) if integration_state else None,
        implementation_commit=impl_commit,
        report_commit=report_commit,
        accepted_commit=accepted_commit,
        acceptance_path=_freeze_strings(acceptance_path) if acceptance_path else None,
        integrated_commit=integrated_commit,
        blocked_reason=_freeze_strings(blocked_reason) if blocked_reason else None,
        blocked_kind=_freeze_strings(blocked_kind) if blocked_kind else None,
        blocked_owner=_freeze_strings(blocked_owner) if blocked_owner else None,
        unblock_condition=_freeze_strings(unblock_condition) if unblock_condition else None,
        review_after=_freeze_strings(review_after) if review_after else None,
        blocked_attempt_valid=bav,
        resume_state=_freeze_strings(resume_state) if resume_state else None,
        timestamps=timestamps,
        raw_task=raw_immutable,
    )


def _build_dispatch(raw: Any, state: str) -> DispatchInfo | None:
    """Build DispatchInfo from raw current_dispatch, or None."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StateProviderSchemaError("current_dispatch must be an object or null")

    dispatch_fields = frozenset(
        ("dispatch_id", "attempt_id", "role_id", "base_commit",
         "branch", "dispatched_at", "model_selection")
    )
    _validate_missing_keys(raw, dispatch_fields, "current_dispatch")
    _validate_extra_keys(raw, dispatch_fields, "current_dispatch")

    dispatch_id = _require_str(raw.get("dispatch_id"))
    attempt_id = _require_str(raw.get("attempt_id"))
    role_id = _require_str(raw.get("role_id"))
    base_commit = _validate_sha40(raw.get("base_commit"), required=True)
    branch = _require_str(raw.get("branch"))
    dispatched_at = _require_str(raw.get("dispatched_at"))

    ms_raw = raw.get("model_selection")
    if not isinstance(ms_raw, dict):
        raise StateProviderSchemaError(
            "current_dispatch.model_selection must be an object"
        )
    model_selection = _build_model_selection(ms_raw)

    raw_immutable = _make_immutable(raw)

    return DispatchInfo(
        dispatch_id=_freeze_strings(dispatch_id),
        attempt_id=_freeze_strings(attempt_id),
        role_id=_freeze_strings(role_id),
        base_commit=base_commit,
        branch=_freeze_strings(branch),
        dispatched_at=_freeze_strings(dispatched_at),
        model_selection=model_selection,
        raw_dispatch=raw_immutable,
    )


def _build_model_selection(raw: dict[str, Any]) -> ModelSelectionSnapshot:
    """Build ModelSelectionSnapshot from raw dict."""
    ms_fields = frozenset(_MODEL_SELECTION_FIELDS)
    _validate_missing_keys(raw, ms_fields, "model_selection")
    _validate_extra_keys(raw, ms_fields, "model_selection")

    req_caps = _freeze_capabilities(raw.get("required_model_capabilities"))
    sel_caps = _freeze_capabilities(raw.get("selected_model_capabilities"))

    ctx_tokens = raw.get("selected_context_window_tokens")
    if not _is_int(ctx_tokens) or ctx_tokens < 1:
        raise StateProviderSchemaError(
            "selected_context_window_tokens must be int >= 1"
        )

    mda_id = raw.get("model_degradation_approval_id")
    if mda_id is not None and not isinstance(mda_id, str):
        raise StateProviderSchemaError(
            "model_degradation_approval_id must be str or null"
        )

    return ModelSelectionSnapshot(
        required_model_tier=_freeze_strings(_require_str(raw.get("required_model_tier"))),
        required_model_capabilities=req_caps,
        model_binding_id=_freeze_strings(_require_str(raw.get("model_binding_id"))),
        selected_model_provider=_freeze_strings(_require_str(raw.get("selected_model_provider"))),
        selected_model_id=_freeze_strings(_require_str(raw.get("selected_model_id"))),
        selected_model_tier=_freeze_strings(_require_str(raw.get("selected_model_tier"))),
        selected_deliberation_tier=_freeze_strings(_require_str(raw.get("selected_deliberation_tier"))),
        selected_context_window_tokens=ctx_tokens,
        selected_model_capabilities=sel_caps,
        model_degradation_approval_id=_freeze_strings(mda_id) if mda_id else None,
    )


def _build_timestamps(raw: Any) -> TaskTimestamps:
    """Build TaskTimestamps from raw dict."""
    if not isinstance(raw, dict):
        raise StateProviderSchemaError("timestamps must be an object")
    allowed = frozenset(_TIMESTAMP_FIELDS)
    _validate_extra_keys(raw, allowed, "timestamps")
    _validate_missing_keys(raw, allowed, "timestamps")

    return TaskTimestamps(
        created_at=_freeze_strings(_require_str(raw.get("created_at"))),
        ready_at=_freeze_strings(_validate_str_or_none(raw.get("ready_at"))) if raw.get("ready_at") else None,
        dispatched_at=_freeze_strings(_validate_str_or_none(raw.get("dispatched_at"))) if raw.get("dispatched_at") else None,
        started_at=_freeze_strings(_validate_str_or_none(raw.get("started_at"))) if raw.get("started_at") else None,
        delivered_at=_freeze_strings(_validate_str_or_none(raw.get("delivered_at"))) if raw.get("delivered_at") else None,
        blocked_at=_freeze_strings(_validate_str_or_none(raw.get("blocked_at"))) if raw.get("blocked_at") else None,
        accepted_at=_freeze_strings(_validate_str_or_none(raw.get("accepted_at"))) if raw.get("accepted_at") else None,
        integrated_at=_freeze_strings(_validate_str_or_none(raw.get("integrated_at"))) if raw.get("integrated_at") else None,
        updated_at=_freeze_strings(_require_str(raw.get("updated_at"))),
    )


def _freeze_capabilities(raw: Any) -> tuple[str, ...]:
    """Convert a list of capability strings to a tuple."""
    if not isinstance(raw, list):
        raise StateProviderSchemaError("capabilities must be an array")
    result: list[str] = []
    for cap in raw:
        if not isinstance(cap, str) or not cap.strip():
            raise StateProviderSchemaError("capability must be non-empty string")
        result.append(sys.intern(cap.strip()))
    if len(result) != len(set(result)):
        raise StateProviderSchemaError("capabilities must not contain duplicates")
    return tuple(result)


def _build_events(
    raw_list: list[tuple[str, dict[str, Any]]],
) -> tuple[EventEntry, ...]:
    """Parse and validate events, returning a tuple of EventEntry."""
    seen_event_ids: set[str] = set()
    result: list[EventEntry] = []
    for filename, doc in raw_list:
        entry = _build_single_event(doc, filename, seen_event_ids)
        result.append(entry)
    return tuple(result)


def _build_single_event(
    doc: dict[str, Any], filename: str, seen: set[str]
) -> EventEntry:
    """Build a single EventEntry from raw event dict."""
    # Validate schema_version
    sv = doc.get("schema_version")
    if sv != _SCHEMA_EVENT:
        raise StateProviderSchemaError(
            "event schema_version is not agentdesk.state-event/v2"
        )

    # Validate exact keys: common fields + possible event-type extras
    common_allowed = frozenset(_EVENT_COMMON_FIELDS)
    event_type = doc.get("event_type")
    if event_type not in _EVENT_TYPES:
        raise StateProviderSchemaError("unknown event_type")

    extra_allowed: frozenset[str] = frozenset()
    if event_type == "TASK_DISPATCHED":
        extra_allowed = frozenset({"payload_digest"})
    elif event_type == "CHANGE_INTEGRATED":
        extra_allowed = frozenset(_CHANGE_INTEGRATED_EXTRA_FIELDS)

    # Check event_type-specific extra keys: they may be present
    allowed = common_allowed | extra_allowed
    extra_keys = set(doc.keys()) - allowed
    if extra_keys:
        raise StateProviderSchemaError("event has forbidden key(s)")

    # Common field extraction
    event_id = _require_str(doc.get("event_id"))
    if event_id in seen:
        raise StateProviderSchemaError("duplicate event_id")
    seen.add(event_id)

    if not _EVENT_ID_RE.match(event_id):
        raise StateProviderSchemaError("event_id does not match pattern")

    task_id = _validate_task_id(doc.get("task_id"))
    revision = _require_int_ge(doc.get("revision"), 1)
    attempt_raw = doc.get("attempt")
    attempt: int | None = None
    if attempt_raw is not None:
        attempt = _require_int_ge(attempt_raw, 1)

    dispatch_id = _validate_str_or_none(doc.get("dispatch_id"))
    from_state = _validate_state(doc.get("from_state"))
    to_state = _validate_state(doc.get("to_state"))
    lease_epoch = _require_int_ge(doc.get("lease_epoch"), 1)
    actor_role_id = _require_str(doc.get("actor_role_id"))
    occurred_at = _require_str(doc.get("occurred_at"))
    source_message_id = _validate_str_or_none(doc.get("source_message_id"))

    # evidence_refs
    evidence_refs_raw = doc.get("evidence_refs")
    if not isinstance(evidence_refs_raw, list):
        raise StateProviderSchemaError("evidence_refs must be a list")
    evidence_refs: tuple[str, ...] = tuple(
        sys.intern(str(ref)) for ref in evidence_refs_raw
    )

    # guard_results
    guard_raw = doc.get("guard_results")
    if not isinstance(guard_raw, list):
        raise StateProviderSchemaError("guard_results must be a list")
    guard_results = tuple(_build_guard_result(g) for g in guard_raw)

    # Event-type-specific extras
    payload_digest: str | None = None
    if event_type == "TASK_DISPATCHED":
        pd = doc.get("payload_digest")
        if not isinstance(pd, str) or not _PAYLOAD_DIGEST_RE.match(pd):
            raise StateProviderSchemaError(
                "TASK_DISPATCHED payload_digest is invalid"
            )
        payload_digest = _freeze_strings(pd)

    extra_fields: tuple[tuple[str, object], ...] | None = None
    if event_type == "CHANGE_INTEGRATED":
        pairs: list[tuple[str, object]] = []
        for key in _CHANGE_INTEGRATED_EXTRA_FIELDS:
            val = doc.get(key)
            if val is not None:
                pairs.append((key, val))
        # Require at least accepted_commit and integrated_commit
        for req_key in ("accepted_commit", "integrated_commit"):
            v = doc.get(req_key)
            if not isinstance(v, str) or not v:
                raise StateProviderSchemaError(
                    f"CHANGE_INTEGRATED missing {req_key}"
                )
        extra_fields = tuple(pairs)

    raw_immutable = _make_immutable(doc)

    return EventEntry(
        schema_version=_freeze_strings(sv),
        event_id=_freeze_strings(event_id),
        event_type=_freeze_strings(event_type),
        task_id=_freeze_strings(task_id),
        revision=revision,
        attempt=attempt,
        dispatch_id=_freeze_strings(dispatch_id) if dispatch_id else None,
        from_state=_freeze_strings(from_state),
        to_state=_freeze_strings(to_state),
        lease_epoch=lease_epoch,
        actor_role_id=_freeze_strings(actor_role_id),
        occurred_at=_freeze_strings(occurred_at),
        source_message_id=_freeze_strings(source_message_id) if source_message_id else None,
        evidence_refs=evidence_refs,
        guard_results=guard_results,
        payload_digest=payload_digest,
        extra_fields=extra_fields,
        raw_event=raw_immutable,
    )


def _build_guard_result(raw: Any) -> GuardResult:
    """Build a GuardResult from raw dict."""
    if not isinstance(raw, dict):
        raise StateProviderSchemaError("guard_results entry must be an object")
    allowed = frozenset(_GUARD_FIELDS)
    _validate_extra_keys(raw, allowed, "guard_results entry")
    _validate_missing_keys(raw, allowed, "guard_results entry")

    guard = _require_str(raw.get("guard"))
    inputs_raw = raw.get("inputs")
    if not isinstance(inputs_raw, dict):
        raise StateProviderSchemaError("guard inputs must be an object")
    inputs: tuple[tuple[str, object], ...] = tuple(
        (sys.intern(str(k)), v)
        for k, v in inputs_raw.items()
    )
    result_val = _require_str(raw.get("result"))
    checked_at = _require_str(raw.get("checked_at"))
    evidence_ref = _require_str(raw.get("evidence_ref"))

    return GuardResult(
        guard=_freeze_strings(guard),
        inputs=inputs,
        result=_freeze_strings(result_val),
        checked_at=_freeze_strings(checked_at),
        evidence_ref=_freeze_strings(evidence_ref),
    )


def _build_outbox(
    raw_list: list[tuple[str, dict[str, Any]]],
) -> tuple[OutboxEntry, ...]:
    """Parse and validate outbox, returning a tuple of OutboxEntry."""
    seen_message_ids: set[str] = set()
    result: list[OutboxEntry] = []
    for filename, doc in raw_list:
        entry = _build_single_outbox(doc, filename, seen_message_ids)
        result.append(entry)
    return tuple(result)


def _build_single_outbox(
    doc: dict[str, Any], filename: str, seen: set[str]
) -> OutboxEntry:
    """Build a single OutboxEntry."""
    sv = doc.get("schema_version")
    if sv != _SCHEMA_OUTBOX:
        raise StateProviderSchemaError(
            "outbox schema_version is not agentdesk.outbox-message/v2"
        )

    allowed = frozenset(_OUTBOX_FIELDS)
    _validate_extra_keys(doc, allowed, "outbox")
    _validate_missing_keys(doc, allowed, "outbox")

    message_id = _require_str(doc.get("message_id"))
    if message_id in seen:
        raise StateProviderSchemaError("duplicate message_id")
    seen.add(message_id)
    if not _MESSAGE_ID_RE.match(message_id):
        raise StateProviderSchemaError("message_id does not match pattern")

    event_id = _require_str(doc.get("event_id"))
    message_type = _require_str(doc.get("message_type"))
    dedupe_key = _require_str(doc.get("dedupe_key"))
    task_id = _validate_task_id(doc.get("task_id"))
    revision = _require_int_ge(doc.get("revision"), 1)
    attempt = _require_int_ge(doc.get("attempt"), 1)
    dispatch_id = _require_str(doc.get("dispatch_id"))
    destination_role_id = _require_str(doc.get("destination_role_id"))
    created_at = _require_str(doc.get("created_at"))

    ms_raw = doc.get("model_selection")
    if not isinstance(ms_raw, dict):
        raise StateProviderSchemaError("outbox model_selection must be an object")
    model_selection = _build_model_selection(ms_raw)

    payload_raw = doc.get("payload")
    if not isinstance(payload_raw, dict):
        raise StateProviderSchemaError("outbox payload must be an object")
    payload = _build_outbox_payload(payload_raw)

    raw_immutable = _make_immutable(doc)

    return OutboxEntry(
        schema_version=_freeze_strings(sv),
        message_id=_freeze_strings(message_id),
        event_id=_freeze_strings(event_id),
        message_type=_freeze_strings(message_type),
        dedupe_key=_freeze_strings(dedupe_key),
        task_id=_freeze_strings(task_id),
        revision=revision,
        attempt=attempt,
        dispatch_id=_freeze_strings(dispatch_id),
        destination_role_id=_freeze_strings(destination_role_id),
        created_at=_freeze_strings(created_at),
        model_selection=model_selection,
        payload=payload,
        raw_outbox=raw_immutable,
    )


def _build_outbox_payload(raw: dict[str, Any]) -> OutboxPayload:
    """Build OutboxPayload from raw dict."""
    allowed = frozenset(_OUTBOX_PAYLOAD_FIELDS)
    _validate_extra_keys(raw, allowed, "outbox payload")
    _validate_missing_keys(raw, allowed, "outbox payload")

    return OutboxPayload(
        task_path=_freeze_strings(_require_str(raw.get("task_path"))),
        task_card_commit=_freeze_strings(_require_str(raw.get("task_card_commit"))),
        base_commit=_freeze_strings(_require_str(raw.get("base_commit"))),
        branch=_freeze_strings(_require_str(raw.get("branch"))),
        report_path=_freeze_strings(_require_str(raw.get("report_path"))),
    )


def _parse_acceptance(raw: bytes, filename: str) -> dict[str, Any]:
    """Parse acceptance markdown file, returning dict with frontmatter + body."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateProviderSchemaError(
            "acceptance file is not valid UTF-8"
        ) from None
    # Validate filename pattern
    m = _ACCEPTANCE_FILENAME_RE.match(filename)
    if not m:
        raise StateProviderSchemaError(
            "acceptance filename does not match expected pattern"
        )
    review_n = int(m.group(4))
    fm = _parse_frontmatter(text, filename)
    body_start = _find_body_start(text)
    body = text[body_start:] if body_start < len(text) else ""

    return {
        **fm,
        "_review_n": review_n,
        "_body": body,
        "_filename": filename,
    }


def _find_body_start(text: str) -> int:
    """Find the start of the markdown body after frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return 0
    try:
        closing = next(
            i for i, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return 0
    # Body starts after the closing --- line
    return sum(len(line) + 1 for line in lines[:closing + 1])


def _build_acceptances(
    raw_list: list[tuple[str, dict[str, Any]]],
) -> tuple[AcceptanceEntry, ...]:
    """Parse and validate acceptances, returning a tuple of AcceptanceEntry."""
    seen_filenames: set[str] = set()
    result: list[AcceptanceEntry] = []
    for filename, fm in raw_list:
        entry = _build_single_acceptance(fm, filename, seen_filenames)
        result.append(entry)
    return tuple(result)


def _build_single_acceptance(
    fm: dict[str, Any], filename: str, seen: set[str]
) -> AcceptanceEntry:
    """Build a single AcceptanceEntry from parsed frontmatter."""
    if filename in seen:
        raise StateProviderSchemaError("duplicate acceptance filename")
    seen.add(filename)

    # Validate frontmatter against expected schema
    sv = fm.get("schema_version")
    if sv != _SCHEMA_ACCEPTANCE:
        raise StateProviderSchemaError(
            "acceptance schema_version is not agentdesk.acceptance/v2"
        )

    # Validate required frontmatter keys
    for key in _ACCEPTANCE_FRONTMATTER_KEYS:
        if key not in fm:
            raise StateProviderSchemaError(
                "acceptance frontmatter missing required key"
            )

    task_id = _validate_task_id(fm.get("task_id"))
    revision = _require_int_ge(fm.get("revision"), 1)
    attempt = _require_int_ge(fm.get("attempt"), 1)
    review_n = fm.get("_review_n", 1)
    if not _is_int(review_n) or review_n < 1:
        raise StateProviderSchemaError("acceptance review_n is invalid")

    decision = fm.get("decision")
    if decision not in _ACCEPTANCE_DECISIONS:
        raise StateProviderSchemaError("acceptance decision is invalid")

    impl_commit = _validate_sha40(fm.get("implementation_commit"), required=True)
    report_commit_val = _validate_sha40(fm.get("report_commit"), required=True)
    base_commit = _validate_sha40(fm.get("base_commit"), required=False)
    accepted_commit = _validate_sha40(fm.get("accepted_commit"), required=False)

    reviewed_dispatch_id = _require_str(fm.get("reviewed_dispatch_id"))
    acc_type = fm.get("type")
    if acc_type not in _TASK_TYPES:
        raise StateProviderSchemaError("acceptance type is invalid")

    # owner_approval
    oa = fm.get("owner_approval")
    if not isinstance(oa, dict):
        raise StateProviderSchemaError(
            "acceptance owner_approval must be a mapping"
        )
    gate = oa.get("gate")
    if gate != "none":
        raise StateProviderSchemaError(
            "acceptance owner_approval.gate must be 'none'"
        )
    approval_ids_raw = oa.get("approval_ids")
    if not isinstance(approval_ids_raw, list):
        raise StateProviderSchemaError(
            "acceptance owner_approval.approval_ids must be a list"
        )
    approval_ids: tuple[str, ...] = tuple(
        sys.intern(str(aid)) for aid in approval_ids_raw
    )

    body = fm.get("_body", "")
    if not isinstance(body, str):
        body = ""

    return AcceptanceEntry(
        schema_version=_freeze_strings(sv),
        task_id=_freeze_strings(task_id),
        revision=revision,
        attempt=attempt,
        review_n=review_n,
        decision=_freeze_strings(decision),
        implementation_commit=impl_commit,
        report_commit=report_commit_val,
        base_commit=base_commit,
        accepted_commit=accepted_commit,
        reviewed_dispatch_id=_freeze_strings(reviewed_dispatch_id),
        type=_freeze_strings(acc_type),
        owner_approval_gate=_freeze_strings(gate),
        owner_approval_ids=approval_ids,
        raw_body=body,
        filename=_freeze_strings(filename),
    )


def _build_mad_refs(
    doc: dict[str, Any],
) -> tuple[MadRefEntry, ...]:
    """Parse and validate mad-refs.yaml, returning tuple of MadRefEntry."""
    sv = doc.get("schema_version")
    if sv != _SCHEMA_MAD_REFS:
        raise StateProviderSchemaError(
            "mad-refs schema_version is not agentdesk.mad-refs/v1"
        )
    _validate_missing_keys(doc, _MAD_REF_ROOT_FIELDS, "mad-refs root")
    _validate_extra_keys(doc, _MAD_REF_ROOT_FIELDS, "mad-refs root")

    ua = doc.get("updated_at")
    if not isinstance(ua, str) or not ua:
        raise StateProviderSchemaError(
            "mad-refs updated_at must be non-empty"
        )

    refs_raw = doc.get("refs")
    if not isinstance(refs_raw, list):
        raise StateProviderSchemaError("mad-refs refs must be an array")

    seen_pairs: set[tuple[str, str]] = set()
    result: list[MadRefEntry] = []
    for i, ref in enumerate(refs_raw):
        if not isinstance(ref, dict):
            raise StateProviderSchemaError(
                f"mad-refs entry {i} must be an object"
            )
        # Validate exact 10 keys
        actual_keys = set(ref.keys())
        missing = _MAD_REF_FIELD_SET - actual_keys
        extra = actual_keys - _MAD_REF_FIELD_SET
        if missing:
            raise StateProviderSchemaError(
                "mad-refs entry missing field(s)"
            )
        if extra:
            raise StateProviderSchemaError(
                "mad-refs entry has forbidden field(s)"
            )

        # Validate each field
        task_id = _require_str(ref["task_id"])
        dispatch_id = _require_str(ref["dispatch_id"])
        purpose = ref["purpose"]
        if purpose not in _MAD_PURPOSES:
            raise StateProviderSchemaError(
                "mad-refs purpose is invalid"
            )
        deliberation_id = _require_str(ref["deliberation_id"])
        depth = ref["depth"]
        if depth not in _MAD_DEPTHS:
            raise StateProviderSchemaError(
                "mad-refs depth is invalid"
            )
        stdout_sha = ref["stdout_sha256"]
        if not isinstance(stdout_sha, str) or not _SHA256_RE.fullmatch(stdout_sha):
            raise StateProviderSchemaError(
                "mad-refs stdout_sha256 must be 64 lowercase hex"
            )
        report_sha = ref["report_sha256"]
        if not isinstance(report_sha, str) or not _SHA256_RE.fullmatch(report_sha):
            raise StateProviderSchemaError(
                "mad-refs report_sha256 must be 64 lowercase hex"
            )
        status = _require_str(ref["status"])
        archive_path = _require_str(ref["archive_path"])
        if not Path(archive_path).is_absolute():
            raise StateProviderSchemaError(
                "mad-refs archive_path must be absolute"
            )
        created_at = _require_str(ref["created_at"])

        # Duplicate check
        pair = (dispatch_id, purpose)
        if pair in seen_pairs:
            raise StateProviderSchemaError(
                "duplicate mad-refs entry"
            )
        seen_pairs.add(pair)

        result.append(MadRefEntry(
            task_id=_freeze_strings(task_id),
            dispatch_id=_freeze_strings(dispatch_id),
            purpose=_freeze_strings(purpose),
            deliberation_id=_freeze_strings(deliberation_id),
            depth=_freeze_strings(depth),
            stdout_sha256=stdout_sha,
            report_sha256=report_sha,
            status=_freeze_strings(status),
            archive_path=_freeze_strings(archive_path),
            created_at=_freeze_strings(created_at),
        ))

    return tuple(result)


# ── cross-file consistency ────────────────────────────────────────────────


def _validate_cross_consistency(
    tasks: tuple[TaskEntry, ...],
    events: tuple[EventEntry, ...],
    outbox: tuple[OutboxEntry, ...],
    acceptances: tuple[AcceptanceEntry, ...],
    mad_refs: tuple[MadRefEntry, ...] | None,
    a_events_raw: list[tuple[str, bytes]],
    a_outbox_raw: list[tuple[str, bytes]],
    a_acceptances_raw: list[tuple[str, bytes]],
) -> None:
    """Validate cross-file referential integrity."""

    # Build index maps
    task_ids = {t.task_id for t in tasks}
    event_ids = {e.event_id for e in events}
    event_index = {e.event_id: e for e in events}
    outbox_event_refs = {o.event_id for o in outbox}
    acceptance_filenames = {a.filename for a in acceptances}

    # Events indexed by task
    events_by_task: dict[str, list[EventEntry]] = {}
    for e in events:
        events_by_task.setdefault(e.task_id, []).append(e)

    # Outbox raw bytes indexed by event_id for digest parity
    outbox_raw_by_event: dict[str, bytes] = {}
    for (fn, raw), o in zip(a_outbox_raw, outbox):
        outbox_raw_by_event[o.event_id] = raw

    # Acceptance raw bytes by filename
    acceptance_raw_by_filename: dict[str, bytes] = {}
    for (fn, raw), a in zip(a_acceptances_raw, acceptances):
        acceptance_raw_by_filename[a.filename] = raw

    # ── Outbox → Event referential check ──
    for o in outbox:
        if o.event_id not in event_ids:
            raise StateProviderInconsistentSnapshotError(
                "outbox references non-existent event"
            )

    # ── Dispatch → Event referential check ──
    for t in tasks:
        if t.current_dispatch is not None and t.state in (
            "dispatched", "in_progress", "review_ready"
        ):
            did = t.current_dispatch.dispatch_id
            task_events = events_by_task.get(t.task_id, [])
            found = any(
                e.dispatch_id == did
                and e.revision == t.revision
                and e.attempt == t.attempt
                for e in task_events
            )
            if not found:
                raise StateProviderInconsistentSnapshotError(
                    "current_dispatch has no matching event evidence"
                )

    # ── Acceptance path → file existence ──
    for t in tasks:
        if t.state in ("accepted", "integrated") and t.acceptance_path:
            # acceptance_path refers to a file in docs/pm/acceptances/
            # The filename should exist in our acceptance set
            acc_filename = Path(t.acceptance_path).name
            if acc_filename not in acceptance_filenames:
                raise StateProviderInconsistentSnapshotError(
                    "acceptance_path references non-existent file"
                )

    # ── Digest parity: TASK_DISPATCHED payload_digest must match outbox sha256 ──
    for e in events:
        if e.event_type == "TASK_DISPATCHED" and e.payload_digest:
            ob_raw = outbox_raw_by_event.get(e.event_id)
            if ob_raw is not None:
                expected = f"sha256:{_sha256_hex(ob_raw)}"
                if e.payload_digest != expected:
                    raise StateProviderInconsistentSnapshotError(
                        "TASK_DISPATCHED payload_digest does not match outbox"
                    )

    # ── Orphan detection: outbox referencing non-existent task ──
    for o in outbox:
        if o.task_id not in task_ids:
            raise StateProviderInconsistentSnapshotError(
                "outbox references non-existent task"
            )

    # ── Orphan detection: event referencing non-existent task ──
    for e in events:
        if e.task_id not in task_ids:
            raise StateProviderInconsistentSnapshotError(
                "event references non-existent task"
            )

    # ── Orphan detection: acceptance referencing non-existent task ──
    for a in acceptances:
        if a.task_id not in task_ids:
            raise StateProviderInconsistentSnapshotError(
                "acceptance references non-existent task"
            )

    # ── Partial transition detection: outbox with no matching event ──
    for o in outbox:
        ev = event_index.get(o.event_id)
        if ev is None:
            continue
        if ev.task_id != o.task_id:
            raise StateProviderInconsistentSnapshotError(
                "outbox event_id task mismatch"
            )

    # ── Mad-ref cross-file: task_id must exist ──
    if mad_refs is not None:
        for mr in mad_refs:
            if mr.task_id not in task_ids:
                raise StateProviderInconsistentSnapshotError(
                    "mad-ref references non-existent task"
                )
            # dispatch_id must have event evidence
            found = any(
                e.dispatch_id == mr.dispatch_id
                and e.task_id == mr.task_id
                for e in events
            )
            if not found:
                raise StateProviderInconsistentSnapshotError(
                    "mad-ref dispatch_id has no matching event"
                )


# ── validation helpers ────────────────────────────────────────────────────


def _validate_task_id(raw: Any) -> str:
    """Validate task_id format."""
    if not isinstance(raw, str) or not _TASK_ID_RE.match(raw):
        raise StateProviderSchemaError("invalid task_id")
    return raw


def _validate_state(raw: Any) -> str:
    """Validate state is one of the 11 frozen states."""
    if not isinstance(raw, str) or raw not in _STATES:
        raise StateProviderSchemaError("invalid state")
    return raw


def _validate_sha40(raw: Any, required: bool) -> str | None:
    """Validate a 40-char lowercase hex SHA field."""
    if raw is None:
        if required:
            raise StateProviderSchemaError("required SHA field is null")
        return None
    if not isinstance(raw, str) or not _SHA40_RE.match(raw):
        raise StateProviderSchemaError("invalid SHA field")
    return raw


def _validate_str_or_none(raw: Any) -> str | None:
    """Validate a string-or-null field."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise StateProviderSchemaError("expected string or null")
    return raw
