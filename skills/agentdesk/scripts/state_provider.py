"""StateProvider — read-only canonical project state snapshot (TC-13.17b.1).

Interface #21 production module.  Provides a single frozen snapshot of
all five canonical input sources with strict fail-closed validation.

Parses the canonical YAML format produced by
``control_plane_transition._to_yaml_str()`` — a deterministic,
sorted-key, 2-space-indent, LF-only YAML subset with no third-party
dependency.

tasks.yaml is parsed as JSON (the writer uses ``json.dumps``).
events/*.yaml and outbox/*.yaml are parsed with a hand-written YAML
subset parser compatible with ``_to_yaml_str`` output.
acceptances/*.md use a markdown frontmatter parser.

Public API
----------
``StateProvider``       — read-only service boundary
``StateProvider.snapshot()`` — execute the multi-file consistency protocol
``StateSnapshot``       — frozen/slots dataclass with all parsed data
``TaskEntry``           — frozen task record (24 fields)
``TaskTimestamps``      — frozen timestamps sub-record (9 fields)
``DispatchInfo``        — frozen dispatch sub-record (7 fields)
``EventEntry``          — frozen event record (17 fields)
``GuardInput``          — frozen guard key-value pair (2 fields)
``GuardResult``         — frozen guard sub-record (5 fields)
``OutboxEntry``         — frozen outbox record (13 fields)
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
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AcceptanceEntry",
    "DispatchInfo",
    "EventEntry",
    "GuardInput",
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
_GUARD_RESULT_VALUES: frozenset[str] = frozenset(
    {"passed", "failed", "skipped", "not_applicable"}
)

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

# Acceptance frontmatter keys (matching validate_project.py)
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
    if not isinstance(value, str) or not value:
        raise StateProviderSchemaError("expected non-empty string field value")
    return value


def _require_int_ge(value: Any, minimum: int) -> int:
    if not _is_int(value) or value < minimum:
        raise StateProviderSchemaError("expected integer field value")
    return value


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _freeze(value: str) -> str:
    """Intern a string to reduce memory."""
    return sys.intern(value)


def _frozen_str(value: str | None) -> str | None:
    """Intern *value* if non-None, else None."""
    if value is None:
        return None
    return sys.intern(value)


def _stable_sort(entries: list[Path]) -> list[Path]:
    """Sort *entries* by name using locale-independent byte ordering."""
    return sorted(entries, key=lambda p: p.name.encode("utf-8"))


# ── YAML subset parser (compatible with control_plane_transition._to_yaml_str) ─


def _parse_yaml_mapping(raw_bytes: bytes, description: str) -> dict[str, object]:
    """Parse a YAML mapping produced by ``_to_yaml_str``.

    Handles the canonical YAML format: LF line endings, sorted keys,
    2-space indent, inline first-field for nested dicts, 4-space indent
    for nested dict fields, 8-space for nested list entries within
    guard_results.

    Raises StateProviderSchemaError on parse failure.
    """
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise StateProviderSchemaError("YAML file is not valid UTF-8") from None
    return _parse_canonical_yaml(text, description)


def _parse_canonical_yaml(raw: str, description: str) -> dict[str, object]:
    """Line-based canonical YAML parser.

    Canonical YAML format (from ``_to_yaml_str`` / ``_emit_yaml``):

    * Top-level keys at column 0, in sorted order.
    * Simple values inline after ``": "`` or on the same line.
    * Nested dicts: FIRST field inline at column 0 (after 2-space separator from
      parent key), remaining fields on subsequent lines at **2-space indent**.
    * Lists: ``- `` prefix at 2-space indent per nesting level.
    * ``guard_results``: list of dicts. First field inline, remaining at indent 4.
      ``inputs`` within a guard: inline first, subsequent at indent 8.
    """
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    annotated: list[tuple[int, str]] = []
    for line in lines:
        if not line or line.isspace():
            continue
        ind = _get_indent(line)
        if ind < 0:
            continue
        annotated.append((ind, line[ind:]))

    root: dict[str, object] = {}
    guard_list: list[dict[str, object]] | None = None
    # Track the last-created nested dict at indent 0 (for indent-2 lines)
    active_nested: dict[str, object] | None = None

    i = 0
    while i < len(annotated):
        indent, stripped = annotated[i]

        # ── list items ──
        if stripped.startswith("- "):
            item_val = _coerce_yaml_value(stripped[2:].strip())
            if indent == 2:
                ev = root.get("evidence_refs")
                if isinstance(ev, list):
                    ev.append(item_val)
            elif indent == 4 and active_nested is not None:
                for k, v in active_nested.items():
                    if isinstance(v, list) and k.endswith("s"):
                        v.append(item_val)
                        break
            i += 1
            continue

        if ":" not in stripped:
            i += 1
            continue

        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()

        # ── indent 0: root-level key ──
        if indent == 0:
            if key == "guard_results":
                guard_list = []
                root["guard_results"] = guard_list
                active_nested = None
                if val and val != "[]":
                    g = _parse_inline_dict_first_field(val)
                    guard_list.append(g)
                i += 1
                continue
            if val:
                coerce_val = _coerce_yaml_value(val)
                if isinstance(coerce_val, str) and _has_inline_field(val):
                    nested = _parse_inline_dict_first_field(val)
                    root[key] = nested
                    active_nested = nested
                else:
                    # Check for inline list: "- cg"
                    if isinstance(coerce_val, (str, list)):
                        if isinstance(coerce_val, str) and coerce_val.startswith("- "):
                            coerce_val = [coerce_val[2:]]
                    root[key] = coerce_val
                    active_nested = None
            else:
                root[key] = None
                active_nested = None
            i += 1
            continue

        # ── indent 2: nested-dict fields (model_selection, payload, pm_control) ──
        if indent == 2:
            if active_nested is not None:
                cv = _coerce_yaml_value(val) if val else None
                if isinstance(cv, str) and cv.startswith("- "):
                    cv = [cv[2:]]
                active_nested[key] = cv
            i += 1
            continue

        # ── indent 4: guard_result fields ──
        if indent == 4:
            if guard_list is not None and len(guard_list) > 0:
                cg = guard_list[-1]
                if key == "inputs":
                    il: list[dict[str, object]] = []
                    cg["inputs"] = il
                    if val:
                        inp = _parse_inline_input(val)
                        if inp:
                            il.append(inp)
                else:
                    cg[key] = _coerce_yaml_value(val) if val else None
            i += 1
            continue

        # ── indent 8: guard_result inputs entries ──
        if indent == 8 and guard_list is not None and len(guard_list) > 0:
            cg = guard_list[-1]
            il = cg.get("inputs")
            if isinstance(il, list):
                if key == "key":
                    il.append({"key": val})
                elif key == "value":
                    if il:
                        il[-1]["value"] = _coerce_yaml_value(val) if val else None
            i += 1
            continue

        i += 1

    return root


def _has_inline_field(val: str) -> bool:
    """Check if *val* contains an inline field (e.g. 'model_binding_id: ...')."""
    return ":" in val and not val.startswith('"') and not val.startswith("sha256:")


def _find_or_create_nested(
    root: dict[str, object], key: str, val: str,
    stack: list[tuple[int, dict[str, object], str | None]],
) -> dict[str, object]:
    """Find the correct nested dict for this key and add a field to it.

    The canonical YAML format arranges nested dicts as:
      parent_key:   first_field_key: first_field_value      # indent 0
      second_field_key: second_field_value                   # indent 2
      third_field_key: third_field_value                     # indent 2

    So the 'parent' is the LAST dict in root that was created
    with an inline first field and hasn't been superseded by a
    newer top-level key.
    """
    # The parent dict is the last entry on our stack with indent < current
    # Find the last nested dict (indent=2) on the stack, or the root
    for indent, d, _ in reversed(stack):
        if indent < 2:
            # Found the parent nested dict — it's the most recently
            # created nested dict that's still on the stack at indent 0.
            # Actually, the right approach: for indent-2 lines, the parent
            # is the dict that was pushed at indent 0 with inline first field.
            # In _parse_canonical_yaml, when we see a nested dict starter at
            # indent 0, we push it onto the stack at indent 2.
            # So the parent for subsequent indent-2 keys is simply the
            # head of the stack.
            pass

    # Simpler approach: the last-created nested dict is on top of stack
    parent = stack[-1][1]
    # But if parent is root (indent 0), we need to find the recently-created nested dict
    if parent is root:
        # Find the most recent nested dict among root's values
        # This is O(1) if we track it, but for now check the stack
        for _, d, _ in stack:
            if d is not root and indent >= 2:
                if val:
                    d[key] = _coerce_yaml_value(val) if val else None
                return d
    if val:
        parent[key] = _coerce_yaml_value(val) if val else None
    return parent


def _parse_inline_dict_first_field(val: str) -> dict[str, object]:
    """Parse 'model_binding_id: binding-001' from inline nested dict start."""
    d: dict[str, object] = {}
    if ":" in val:
        k, _, v = val.partition(":")
        k = k.strip()
        v = v.strip()
        d[k] = _coerce_yaml_value(v) if v else None
    return d


def _parse_inline_input(val: str) -> dict[str, object]:
    """Parse 'key: scope' from inline input field."""
    d: dict[str, object] = {}
    if ":" in val:
        k, _, v = val.partition(":")
        k = k.strip()
        v = v.strip()
        if k == "key":
            d["key"] = v
    return d


def _get_indent(line: str) -> int:
    """Return the count of leading spaces, or -1 if blank."""
    if not line or line.isspace():
        return -1
    count = 0
    for ch in line:
        if ch == " ":
            count += 1
        else:
            return count
    return count


def _parse_json_mapping(raw_bytes: bytes, description: str) -> dict[str, Any]:
    """Parse a JSON mapping (for tasks.yaml and mad-refs.yaml).

    The writer for tasks.yaml uses ``json.dumps``, so JSON is the
    authoritative parse path.  Raises StateProviderSchemaError.
    """
    try:
        value = json.loads(raw_bytes)
    except json.JSONDecodeError:
        raise StateProviderSchemaError(
            "file is not valid JSON"
        ) from None
    if not isinstance(value, dict):
        raise StateProviderSchemaError("root must be an object")
    return value


def _coerce_yaml_value(val: str) -> object:
    """Coerce a string from canonical YAML to proper Python type."""
    v = val.strip()
    if v == "null":
        return None
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
        return int(v)
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v == "[]":
        return []
    if v == "{}":
        return {}
    return v


def _validate_extra_keys(
    data: dict[str, object], allowed: frozenset[str], description: str
) -> None:
    extra = set(data.keys()) - allowed
    if extra:
        raise StateProviderSchemaError("has forbidden key(s)")


def _validate_missing_keys(
    data: dict[str, object], required: frozenset[str], description: str
) -> None:
    missing = required - set(data.keys())
    if missing:
        raise StateProviderSchemaError("is missing required key(s)")


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
class GuardInput:
    """Frozen key-value pair for a guard input set.

    Matches ``control_plane_transition.GuardInput``.
    """

    key: str
    value: str | int | bool | None


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Frozen guard result entry."""

    guard: str
    inputs: tuple[GuardInput, ...]
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
    """Frozen dispatch identity sub-record (7 fields)."""

    dispatch_id: str
    attempt_id: str
    role_id: str
    base_commit: str
    branch: str
    dispatched_at: str
    model_selection: ModelSelectionSnapshot


@dataclass(frozen=True, slots=True)
class TaskEntry:
    """Frozen task record (24 fields)."""

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


@dataclass(frozen=True, slots=True)
class EventEntry:
    """Frozen event record covering all 15 event types (17 fields)."""

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


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    """Frozen outbox record (13 fields)."""

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
    corrupt YAML/JSON."""


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
        root = self._project_root
        _ensure_required_exist(root)

        # ── Pass A ──
        a_tasks = _read_file_safe(root / _TASKS_PATH)
        a_events = _read_dir_entries_reject_symlinks(root / _EVENTS_DIR, ".yaml",
                                                      _EVENT_ID_FILENAME_RE)
        a_outbox = _read_dir_entries_reject_symlinks(root / _OUTBOX_DIR, ".yaml",
                                                      _OUTBOX_FILENAME_RE)
        a_acceptances = _read_dir_entries_reject_symlinks(root / _ACCEPTANCES_DIR, ".md", None)
        a_mad = _read_opt_bytes(root / _MAD_REFS_PATH)

        # ── Pass B ──
        b_tasks = _read_file_safe(root / _TASKS_PATH)
        b_events = _read_dir_entries_reject_symlinks(root / _EVENTS_DIR, ".yaml",
                                                      _EVENT_ID_FILENAME_RE)
        b_outbox = _read_dir_entries_reject_symlinks(root / _OUTBOX_DIR, ".yaml",
                                                      _OUTBOX_FILENAME_RE)
        b_acceptances = _read_dir_entries_reject_symlinks(root / _ACCEPTANCES_DIR, ".md", None)
        b_mad = _read_opt_bytes(root / _MAD_REFS_PATH)

        # ── Hash ──
        ah = _compute_pass_hash(a_tasks, a_events, a_outbox, a_acceptances, a_mad)
        bh = _compute_pass_hash(b_tasks, b_events, b_outbox, b_acceptances, b_mad)
        if ah != bh:
            raise StateProviderSnapshotChangedError(
                "snapshot changed between first and second read"
            )

        # ── Parse ──
        tasks_doc = _parse_json_mapping(a_tasks, "tasks.yaml")
        events_raw = [(fn, raw) for fn, raw in a_events]
        outbox_raw = [(fn, raw) for fn, raw in a_outbox]
        acceptances_raw = [(fn, raw) for fn, raw in a_acceptances]
        mad_doc: dict[str, Any] | None = None
        if a_mad is not None:
            mad_doc = _parse_json_mapping(a_mad, "mad-refs.yaml")

        # ── Build collections ──
        tasks = _build_tasks(tasks_doc)
        events = _build_events(events_raw)
        outbox = _build_outbox(outbox_raw)
        acceptances = _build_acceptances(acceptances_raw)
        mad_refs: tuple[MadRefEntry, ...] | None = None
        if mad_doc is not None:
            mad_refs = _build_mad_refs(mad_doc)

        # ── Cross-file consistency ──
        _validate_cross_consistency(
            tasks, events, outbox, acceptances, mad_refs,
            a_events, a_outbox, a_acceptances,
        )

        # ── Snapshot ──
        pm = tasks_doc["pm_control"]
        assert isinstance(pm, dict)
        return StateSnapshot(
            project_root=root,
            schema_version=_freeze(tasks_doc["schema_version"]),
            project_id=_freeze(tasks_doc["project_id"]),
            adoption_level=_freeze(tasks_doc["adoption_level"]),
            updated_at=_freeze(tasks_doc["updated_at"]),
            pm_holder_id=_freeze(pm["holder_id"]),
            pm_lease_epoch=_require_int_ge(pm["lease_epoch"], 1),
            pm_mode=_freeze(pm["mode"]),
            tasks=tasks,
            events=events,
            outbox=outbox,
            acceptances=acceptances,
            mad_refs=mad_refs,
            read_hexsha=ah,
        )


# ── filesystem helpers ────────────────────────────────────────────────────


def _ensure_required_exist(root: Path) -> None:
    tasks_path = root / _TASKS_PATH
    if not tasks_path.is_file():
        raise StateProviderNotFoundError("required canonical file not found")
    for dir_path in (_EVENTS_DIR, _OUTBOX_DIR, _ACCEPTANCES_DIR):
        full = root / dir_path
        if not full.is_dir():
            raise StateProviderNotFoundError("required canonical directory not found")


def _is_symlink_or_reparse(entry: Path) -> bool:
    """Return True if *entry* is a symlink (POSIX) or reparse point (Windows).

    Symlinks and reparse points are rejected — StateProvider only reads
    regular files in canonical directories.
    """
    if entry.is_symlink():
        return True
    # Windows reparse point / junction detection
    try:
        st = os.lstat(str(entry))
        if hasattr(st, "st_file_attributes") and hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
            if st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    except (AttributeError, OSError):
        pass
    return False


def _read_dir_entries_reject_symlinks(
    dir_path: Path, suffix: str, name_pattern: re.Pattern | None,
) -> list[tuple[str, bytes]]:
    """Read all regular files in *dir_path* matching *suffix*.

    - Rejects symlinks and reparse points (fail-closed).
    - Sorts by locale-independent byte order on filename.
    - Raises StateProviderNotFoundError on directory I/O error.
    - Raises StateProviderSchemaError on filename pattern mismatch.
    """
    try:
        candidates = list(dir_path.iterdir())
    except OSError:
        raise StateProviderNotFoundError("cannot read canonical directory") from None

    regular: list[Path] = []
    for p in candidates:
        if _is_symlink_or_reparse(p):
            raise StateProviderSchemaError("symlink or reparse point forbidden")
        if p.is_file() and p.suffix == suffix:
            regular.append(p)

    result: list[tuple[str, bytes]] = []
    for entry in _stable_sort(regular):
        if name_pattern is not None and not name_pattern.fullmatch(entry.name):
            raise StateProviderSchemaError("filename does not match expected pattern")
        try:
            result.append((entry.name, entry.read_bytes()))
        except FileNotFoundError:
            raise StateProviderSnapshotChangedError(
                "file disappeared during snapshot read"
            ) from None
        except OSError:
            raise StateProviderNotFoundError("cannot read canonical file") from None
    return result


def _read_opt_bytes(path: Path) -> bytes | None:
    """Read raw bytes from *path* if it exists, else None.

    On Windows, also reject the file if it is a reparse point.
    """
    try:
        if _is_symlink_or_reparse(path):
            raise StateProviderSchemaError("symlink or reparse point forbidden")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise StateProviderNotFoundError("cannot read optional runtime file") from None


def _read_file_safe(path: Path) -> bytes:
    """Read required file, converting I/O errors safely."""
    try:
        if _is_symlink_or_reparse(path):
            raise StateProviderSchemaError("symlink or reparse point forbidden")
        return path.read_bytes()
    except FileNotFoundError:
        raise StateProviderNotFoundError("required canonical file not found") from None
    except OSError:
        raise StateProviderNotFoundError("cannot read required file") from None


def _compute_pass_hash(
    tasks: bytes, events: list[tuple[str, bytes]],
    outbox: list[tuple[str, bytes]], acceptances: list[tuple[str, bytes]],
    mad: bytes | None,
) -> str:
    h = hashlib.sha256()
    h.update(tasks)
    for name, raw in events:
        h.update(name.encode("utf-8"))
        h.update(raw)
    for name, raw in outbox:
        h.update(name.encode("utf-8"))
        h.update(raw)
    for name, raw in acceptances:
        h.update(name.encode("utf-8"))
        h.update(raw)
    if mad is not None:
        h.update(b"mad-refs\x00")
        h.update(mad)
    else:
        h.update(b"mad-refs\x00none")
    return h.hexdigest()


# ── parse / build ─────────────────────────────────────────────────────────


def _build_tasks(doc: dict[str, Any]) -> tuple[TaskEntry, ...]:
    _validate_missing_keys(doc, _TASKS_ROOT_KEYS, "tasks.yaml root")
    _validate_extra_keys(doc, _TASKS_ROOT_KEYS, "tasks.yaml root")

    sv = doc["schema_version"]
    if sv != _SCHEMA_TASKS:
        raise StateProviderSchemaError("tasks.yaml schema_version is not agentdesk.tasks/v2")
    if not isinstance(doc["project_id"], str) or not doc["project_id"]:
        raise StateProviderSchemaError("tasks.yaml project_id must be non-empty")
    if doc["adoption_level"] not in _ADOPTION_LEVELS:
        raise StateProviderSchemaError("tasks.yaml adoption_level is invalid")
    if not isinstance(doc["updated_at"], str) or not doc["updated_at"]:
        raise StateProviderSchemaError("tasks.yaml updated_at must be non-empty")
    _validate_pm_control(doc["pm_control"])

    tasks_raw = doc.get("tasks")
    if not isinstance(tasks_raw, list):
        raise StateProviderSchemaError("tasks.yaml tasks must be an array")

    seen: set[str] = set()
    result: list[TaskEntry] = []
    for i, raw in enumerate(tasks_raw):
        if not isinstance(raw, dict):
            raise StateProviderSchemaError("task entry must be an object")
        result.append(_build_single_task(raw, seen))
    return tuple(result)


def _validate_pm_control(pm: Any) -> None:
    if not isinstance(pm, dict):
        raise StateProviderSchemaError("pm_control must be an object")
    allowed = frozenset({"holder_id", "lease_epoch", "mode"})
    _validate_extra_keys(pm, allowed, "pm_control")
    _validate_missing_keys(pm, allowed, "pm_control")
    if not isinstance(pm["holder_id"], str) or not pm["holder_id"]:
        raise StateProviderSchemaError("pm_control.holder_id must be non-empty")
    if not _is_int(pm["lease_epoch"]) or pm["lease_epoch"] < 1:
        raise StateProviderSchemaError("pm_control.lease_epoch must be an integer >= 1")
    if pm["mode"] not in _PM_MODES:
        raise StateProviderSchemaError("pm_control.mode is invalid")


def _build_single_task(raw: dict[str, Any], seen: set[str]) -> TaskEntry:
    allowed = frozenset(_TASK_FIELDS)
    _validate_extra_keys(raw, allowed, "task")
    _validate_missing_keys(raw, allowed, "task")

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
        raise StateProviderSchemaError("attempt must be set for active dispatch state")

    dispatch = _build_dispatch(raw.get("current_dispatch"))
    report_path = _frozen_str(_validate_str_or_none(raw.get("report_path")))

    granted = _build_str_list_or_none(raw.get("granted_approval_ids"))

    delivery_state = _frozen_str(_validate_str_or_none(raw.get("delivery_state")))
    if delivery_state is not None and delivery_state not in _DELIVERY_STATES:
        raise StateProviderSchemaError("delivery_state is invalid")
    integration_state = _frozen_str(_validate_str_or_none(raw.get("integration_state")))
    if integration_state is not None and integration_state not in _INTEGRATION_STATES:
        raise StateProviderSchemaError("integration_state is invalid")

    impl_commit = _validate_sha40(raw.get("implementation_commit"), required=False)
    report_commit_val = _validate_sha40(raw.get("report_commit"), required=False)
    accepted_commit = _validate_sha40(raw.get("accepted_commit"), required=False)
    acceptance_path = _frozen_str(_validate_str_or_none(raw.get("acceptance_path")))
    integrated_commit = _validate_sha40(raw.get("integrated_commit"), required=False)

    blocked_reason = _frozen_str(_validate_str_or_none(raw.get("blocked_reason")))
    blocked_kind = _frozen_str(_validate_str_or_none(raw.get("blocked_kind")))
    if blocked_kind is not None and blocked_kind not in _BLOCKED_KINDS:
        raise StateProviderSchemaError("blocked_kind is invalid")
    blocked_owner = _frozen_str(_validate_str_or_none(raw.get("blocked_owner")))
    unblock_condition = _frozen_str(_validate_str_or_none(raw.get("unblock_condition")))
    review_after = _frozen_str(_validate_str_or_none(raw.get("review_after")))

    bav = raw.get("blocked_attempt_valid")
    if bav is not None and not _is_bool(bav):
        raise StateProviderSchemaError("blocked_attempt_valid must be bool or null")

    resume_state = _frozen_str(_validate_str_or_none(raw.get("resume_state")))
    if resume_state is not None and resume_state not in _STATES:
        raise StateProviderSchemaError("resume_state is invalid")

    timestamps = _build_timestamps(raw.get("timestamps"))

    return TaskEntry(
        task_id=_freeze(task_id), revision=revision,
        task_card_path=_freeze(task_card_path),
        task_card_commit=task_card_commit,
        state=_freeze(state), attempt=attempt,
        current_dispatch=dispatch, report_path=report_path,
        granted_approval_ids=granted, delivery_state=delivery_state,
        integration_state=integration_state,
        implementation_commit=impl_commit, report_commit=report_commit_val,
        accepted_commit=accepted_commit, acceptance_path=acceptance_path,
        integrated_commit=integrated_commit,
        blocked_reason=blocked_reason, blocked_kind=blocked_kind,
        blocked_owner=blocked_owner, unblock_condition=unblock_condition,
        review_after=review_after, blocked_attempt_valid=bav,
        resume_state=resume_state, timestamps=timestamps,
    )


def _build_dispatch(raw: Any) -> DispatchInfo | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StateProviderSchemaError("current_dispatch must be an object or null")

    fields = frozenset(
        ("dispatch_id", "attempt_id", "role_id", "base_commit",
         "branch", "dispatched_at", "model_selection")
    )
    _validate_missing_keys(raw, fields, "current_dispatch")
    _validate_extra_keys(raw, fields, "current_dispatch")

    dispatch_id = _require_str(raw.get("dispatch_id"))
    attempt_id = _require_str(raw.get("attempt_id"))
    role_id = _require_str(raw.get("role_id"))
    base_commit = _validate_sha40(raw.get("base_commit"), required=True)
    branch = _require_str(raw.get("branch"))
    dispatched_at = _require_str(raw.get("dispatched_at"))

    ms_raw = raw.get("model_selection")
    if not isinstance(ms_raw, dict):
        raise StateProviderSchemaError("current_dispatch.model_selection must be an object")
    model_selection = _build_model_selection(ms_raw)

    return DispatchInfo(
        dispatch_id=_freeze(dispatch_id), attempt_id=_freeze(attempt_id),
        role_id=_freeze(role_id), base_commit=base_commit,
        branch=_freeze(branch), dispatched_at=_freeze(dispatched_at),
        model_selection=model_selection,
    )


def _build_model_selection(raw: dict[str, Any]) -> ModelSelectionSnapshot:
    ms_fields = frozenset(_MODEL_SELECTION_FIELDS)
    _validate_missing_keys(raw, ms_fields, "model_selection")
    _validate_extra_keys(raw, ms_fields, "model_selection")

    req_caps = _freeze_capabilities(raw.get("required_model_capabilities"))
    sel_caps = _freeze_capabilities(raw.get("selected_model_capabilities"))

    ctx = raw.get("selected_context_window_tokens")
    if not _is_int(ctx) or ctx < 1:
        raise StateProviderSchemaError("selected_context_window_tokens must be int >= 1")

    mda = raw.get("model_degradation_approval_id")
    if mda is not None and not isinstance(mda, str):
        raise StateProviderSchemaError("model_degradation_approval_id must be str or null")

    return ModelSelectionSnapshot(
        required_model_tier=_freeze(_require_str(raw.get("required_model_tier"))),
        required_model_capabilities=req_caps,
        model_binding_id=_freeze(_require_str(raw.get("model_binding_id"))),
        selected_model_provider=_freeze(_require_str(raw.get("selected_model_provider"))),
        selected_model_id=_freeze(_require_str(raw.get("selected_model_id"))),
        selected_model_tier=_freeze(_require_str(raw.get("selected_model_tier"))),
        selected_deliberation_tier=_freeze(_require_str(raw.get("selected_deliberation_tier"))),
        selected_context_window_tokens=ctx,
        selected_model_capabilities=sel_caps,
        model_degradation_approval_id=_frozen_str(mda) if mda else None,
    )


def _build_timestamps(raw: Any) -> TaskTimestamps:
    if not isinstance(raw, dict):
        raise StateProviderSchemaError("timestamps must be an object")
    allowed = frozenset(_TIMESTAMP_FIELDS)
    _validate_extra_keys(raw, allowed, "timestamps")
    _validate_missing_keys(raw, allowed, "timestamps")

    def _opt(key: str) -> str | None:
        v = raw.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            raise StateProviderSchemaError("timestamp must be str or null")
        return _freeze(v)

    return TaskTimestamps(
        created_at=_freeze(_require_str(raw.get("created_at"))),
        ready_at=_opt("ready_at"),
        dispatched_at=_opt("dispatched_at"),
        started_at=_opt("started_at"),
        delivered_at=_opt("delivered_at"),
        blocked_at=_opt("blocked_at"),
        accepted_at=_opt("accepted_at"),
        integrated_at=_opt("integrated_at"),
        updated_at=_freeze(_require_str(raw.get("updated_at"))),
    )


def _freeze_capabilities(raw: Any) -> tuple[str, ...]:
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


def _freeze_str_list(raw: Any) -> tuple[str, ...]:
    """Freeze a JSON list of strings into a tuple."""
    if not isinstance(raw, list):
        raise StateProviderSchemaError("expected a list")
    return tuple(sys.intern(str(s)) for s in raw)


def _build_str_list_or_none(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise StateProviderSchemaError("expected a list or null")
    for item in raw:
        if not isinstance(item, str) or not item:
            raise StateProviderSchemaError("list entries must be non-empty strings")
    return tuple(sys.intern(str(item)) for item in raw)


# ── events ────────────────────────────────────────────────────────────────


def _build_events(
    raw_list: list[tuple[str, bytes]],
) -> tuple[EventEntry, ...]:
    seen: set[str] = set()
    result: list[EventEntry] = []
    for fn, raw in raw_list:
        result.append(_build_single_event(raw, fn, seen))
    return tuple(result)


def _build_single_event(raw: bytes, filename: str, seen: set[str]) -> EventEntry:
    doc = _parse_yaml_mapping(raw, filename)
    return _build_event_from_dict(doc, filename, seen)


def _build_event_from_dict(doc: dict[str, object], filename: str, seen: set[str]) -> EventEntry:
    sv = doc.get("schema_version")
    if sv != _SCHEMA_EVENT:
        raise StateProviderSchemaError("event schema_version is not agentdesk.state-event/v2")

    event_type = doc.get("event_type")
    if event_type not in _EVENT_TYPES:
        raise StateProviderSchemaError("unknown event_type")

    extra_allowed: frozenset[str] = frozenset()
    if event_type == "TASK_DISPATCHED":
        extra_allowed = frozenset({"payload_digest"})
    elif event_type == "CHANGE_INTEGRATED":
        extra_allowed = frozenset(_CHANGE_INTEGRATED_EXTRA_FIELDS)

    allowed = frozenset(_EVENT_COMMON_FIELDS) | extra_allowed
    _validate_extra_keys(doc, allowed, "event")

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

    dispatch_id = _frozen_str(_validate_str_or_none(doc.get("dispatch_id")))
    from_state = _validate_state(doc.get("from_state"))
    to_state = _validate_state(doc.get("to_state"))
    lease_epoch = _require_int_ge(doc.get("lease_epoch"), 1)
    actor_role_id = _require_str(doc.get("actor_role_id"))
    occurred_at = _require_str(doc.get("occurred_at"))
    source_message_id = _frozen_str(_validate_str_or_none(doc.get("source_message_id")))

    evidence_refs = _freeze_str_list(doc.get("evidence_refs"))

    # guard_results — parse the real list[dict] format
    guard_raw = doc.get("guard_results")
    if guard_raw is not None:
        if not isinstance(guard_raw, list):
            raise StateProviderSchemaError("guard_results must be a list")
    else:
        guard_raw = []
    guard_results = tuple(_build_guard_result(g) for g in guard_raw)

    # Event-type-specific extras
    payload_digest = None
    if event_type == "TASK_DISPATCHED":
        pd = doc.get("payload_digest")
        if not isinstance(pd, str) or not _PAYLOAD_DIGEST_RE.match(pd):
            raise StateProviderSchemaError("TASK_DISPATCHED payload_digest is invalid")
        payload_digest = _freeze(pd)

    extra_fields: tuple[tuple[str, object], ...] | None = None
    if event_type == "CHANGE_INTEGRATED":
        pairs: list[tuple[str, object]] = []
        for key in _CHANGE_INTEGRATED_EXTRA_FIELDS:
            val = doc.get(key)
            if val is not None:
                pairs.append((key, val))
        for req_key in ("accepted_commit", "integrated_commit"):
            v = doc.get(req_key)
            if not isinstance(v, str) or not v:
                raise StateProviderSchemaError(f"CHANGE_INTEGRATED missing {req_key}")
        extra_fields = tuple(pairs)

    return EventEntry(
        schema_version=_freeze(sv),
        event_id=_freeze(event_id),
        event_type=_freeze(event_type),
        task_id=_freeze(task_id),
        revision=revision,
        attempt=attempt,
        dispatch_id=dispatch_id,
        from_state=_freeze(from_state),
        to_state=_freeze(to_state),
        lease_epoch=lease_epoch,
        actor_role_id=_freeze(actor_role_id),
        occurred_at=_freeze(occurred_at),
        source_message_id=source_message_id,
        evidence_refs=evidence_refs,
        guard_results=guard_results,
        payload_digest=payload_digest,
        extra_fields=extra_fields,
    )


def _build_guard_result(raw: Any) -> GuardResult:
    """Build a GuardResult from raw dict.

    The inputs field is ``list[{"key": ..., "value": ...}]``
    (matching ``control_plane_transition._serialize_guard_input``).
    """
    if not isinstance(raw, dict):
        raise StateProviderSchemaError("guard_result entry must be an object")
    allowed = frozenset(_GUARD_FIELDS)
    _validate_extra_keys(raw, allowed, "guard_result")
    _validate_missing_keys(raw, allowed, "guard_result")

    guard = _require_str(raw.get("guard"))

    inputs_raw = raw.get("inputs")
    if not isinstance(inputs_raw, list):
        raise StateProviderSchemaError("guard_result inputs must be a list")
    inputs_list: list[GuardInput] = []
    for inp in inputs_raw:
        if not isinstance(inp, dict):
            raise StateProviderSchemaError("guard_input must be an object")
        ik = inp.get("key")
        iv = inp.get("value")
        if not isinstance(ik, str) or not ik:
            raise StateProviderSchemaError("guard_input key must be non-empty string")
        # value: str, int, bool, or None
        if iv is not None and not isinstance(iv, (str, int, bool)):
            raise StateProviderSchemaError("guard_input value must be str, int, bool, or null")
        if isinstance(iv, int) and isinstance(iv, bool):
            raise StateProviderSchemaError("guard_input value must not be bool-as-int")
        inputs_list.append(GuardInput(key=_freeze(ik), value=iv))

    result_val = raw.get("result")
    if not isinstance(result_val, str) or result_val not in _GUARD_RESULT_VALUES:
        raise StateProviderSchemaError(
            "guard_result result must be one of passed/failed/skipped/not_applicable"
        )
    checked_at = _require_str(raw.get("checked_at"))
    evidence_ref = _require_str(raw.get("evidence_ref"))

    return GuardResult(
        guard=_freeze(guard),
        inputs=tuple(inputs_list),
        result=_freeze(result_val),
        checked_at=_freeze(checked_at),
        evidence_ref=_freeze(evidence_ref),
    )


# ── outbox ────────────────────────────────────────────────────────────────


def _build_outbox(
    raw_list: list[tuple[str, bytes]],
) -> tuple[OutboxEntry, ...]:
    seen: set[str] = set()
    result: list[OutboxEntry] = []
    for fn, raw in raw_list:
        result.append(_build_single_outbox(raw, fn, seen))
    return tuple(result)


def _build_single_outbox(raw: bytes, filename: str, seen: set[str]) -> OutboxEntry:
    doc = _parse_yaml_mapping(raw, filename)

    sv = doc.get("schema_version")
    if sv != _SCHEMA_OUTBOX:
        raise StateProviderSchemaError("outbox schema_version is not agentdesk.outbox-message/v2")

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

    return OutboxEntry(
        schema_version=_freeze(sv),
        message_id=_freeze(message_id),
        event_id=_freeze(event_id),
        message_type=_freeze(message_type),
        dedupe_key=_freeze(dedupe_key),
        task_id=_freeze(task_id),
        revision=revision,
        attempt=attempt,
        dispatch_id=_freeze(dispatch_id),
        destination_role_id=_freeze(destination_role_id),
        created_at=_freeze(created_at),
        model_selection=model_selection,
        payload=payload,
    )


def _build_outbox_payload(raw: dict[str, Any]) -> OutboxPayload:
    allowed = frozenset(_OUTBOX_PAYLOAD_FIELDS)
    _validate_extra_keys(raw, allowed, "outbox payload")
    _validate_missing_keys(raw, allowed, "outbox payload")

    return OutboxPayload(
        task_path=_freeze(_require_str(raw.get("task_path"))),
        task_card_commit=_freeze(_require_str(raw.get("task_card_commit"))),
        base_commit=_freeze(_require_str(raw.get("base_commit"))),
        branch=_freeze(_require_str(raw.get("branch"))),
        report_path=_freeze(_require_str(raw.get("report_path"))),
    )


# ── acceptances ───────────────────────────────────────────────────────────


def _parse_frontmatter(raw: str, description: str) -> dict[str, Any]:
    """Parse YAML frontmatter from markdown.

    Handles top-level scalars + one-level nested mappings
    (owner_approval.gate, owner_approval.approval_ids as inline []).
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise StateProviderSchemaError("must start with YAML frontmatter")
    try:
        closing = next(i for i, line in enumerate(lines[1:], start=1)
                       if line.strip() == "---")
    except StopIteration:
        raise StateProviderSchemaError("has no closing frontmatter delimiter")
    fm_lines = lines[1:closing]
    result: dict[str, Any] = {}
    current_map_key: str | None = None
    current_map: dict[str, Any] = {}

    for line in fm_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("  ") and not line.startswith("    "):
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if current_map_key is None:
                    current_map = {}
                if val:
                    current_map[key] = _parse_yaml_scalar(val)
                else:
                    current_map[key] = None
            continue

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

    if current_map_key is not None and current_map:
        result[current_map_key] = dict(current_map)

    return result


def _parse_yaml_scalar(val: str) -> Any:
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    if val == "[]":
        return []
    if val in ("null", "~", ""):
        return None
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    return val


def _parse_acceptance(raw: bytes, filename: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StateProviderSchemaError("acceptance file is not valid UTF-8") from None
    m = _ACCEPTANCE_FILENAME_RE.match(filename)
    if not m:
        raise StateProviderSchemaError("acceptance filename does not match expected pattern")
    review_n = int(m.group(4))
    fm = _parse_frontmatter(text, filename)
    body_start = _find_body_start(text)
    body = text[body_start:] if body_start < len(text) else ""
    return {**fm, "_review_n": review_n, "_body": body, "_filename": filename}


def _find_body_start(text: str) -> int:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return 0
    try:
        closing = next(i for i, line in enumerate(lines[1:], start=1)
                       if line.strip() == "---")
    except StopIteration:
        return 0
    return sum(len(l) + 1 for l in lines[:closing + 1])


def _build_acceptances(
    raw_list: list[tuple[str, bytes]],
) -> tuple[AcceptanceEntry, ...]:
    seen: set[str] = set()
    result: list[AcceptanceEntry] = []
    for fn, raw in raw_list:
        fm = _parse_acceptance(raw, fn)
        result.append(_build_single_acceptance(fm, fn, seen))
    return tuple(result)


def _build_single_acceptance(
    fm: dict[str, Any], filename: str, seen: set[str]
) -> AcceptanceEntry:
    if filename in seen:
        raise StateProviderSchemaError("duplicate acceptance filename")
    seen.add(filename)

    sv = fm.get("schema_version")
    if sv != _SCHEMA_ACCEPTANCE:
        raise StateProviderSchemaError("acceptance schema_version is not agentdesk.acceptance/v2")

    for key in _ACCEPTANCE_FRONTMATTER_KEYS:
        if key not in fm:
            raise StateProviderSchemaError("acceptance frontmatter missing required key")

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
    accepted_commit_val = _validate_sha40(fm.get("accepted_commit"), required=False)

    reviewed_dispatch_id = _require_str(fm.get("reviewed_dispatch_id"))
    acc_type = fm.get("type")
    if acc_type not in _TASK_TYPES:
        raise StateProviderSchemaError("acceptance type is invalid")

    oa = fm.get("owner_approval")
    if not isinstance(oa, dict):
        raise StateProviderSchemaError("acceptance owner_approval must be a mapping")
    gate = oa.get("gate")
    if gate != "none":
        raise StateProviderSchemaError("acceptance owner_approval.gate must be 'none'")
    approval_ids_raw = oa.get("approval_ids")
    if not isinstance(approval_ids_raw, list):
        raise StateProviderSchemaError("acceptance owner_approval.approval_ids must be a list")
    approval_ids: tuple[str, ...] = tuple(sys.intern(str(aid)) for aid in approval_ids_raw)

    body = fm.get("_body", "")
    if not isinstance(body, str):
        body = ""

    return AcceptanceEntry(
        schema_version=_freeze(sv),
        task_id=_freeze(task_id),
        revision=revision,
        attempt=attempt,
        review_n=review_n,
        decision=_freeze(decision),
        implementation_commit=impl_commit,
        report_commit=report_commit_val,
        base_commit=base_commit,
        accepted_commit=accepted_commit_val,
        reviewed_dispatch_id=_freeze(reviewed_dispatch_id),
        type=_freeze(acc_type),
        owner_approval_gate=_freeze(gate),
        owner_approval_ids=approval_ids,
        raw_body=body,
        filename=_freeze(filename),
    )


# ── mad-refs ──────────────────────────────────────────────────────────────


def _build_mad_refs(doc: dict[str, Any]) -> tuple[MadRefEntry, ...]:
    sv = doc.get("schema_version")
    if sv != _SCHEMA_MAD_REFS:
        raise StateProviderSchemaError("mad-refs schema_version is not agentdesk.mad-refs/v1")
    _validate_missing_keys(doc, _MAD_REF_ROOT_FIELDS, "mad-refs root")
    _validate_extra_keys(doc, _MAD_REF_ROOT_FIELDS, "mad-refs root")

    ua = doc.get("updated_at")
    if not isinstance(ua, str) or not ua:
        raise StateProviderSchemaError("mad-refs updated_at must be non-empty")

    refs_raw = doc.get("refs")
    if not isinstance(refs_raw, list):
        raise StateProviderSchemaError("mad-refs refs must be an array")

    seen_pairs: set[tuple[str, str]] = set()
    result: list[MadRefEntry] = []
    for i, ref in enumerate(refs_raw):
        if not isinstance(ref, dict):
            raise StateProviderSchemaError("mad-refs entry must be an object")
        actual_keys = set(ref.keys())
        missing = _MAD_REF_FIELD_SET - actual_keys
        extra = actual_keys - _MAD_REF_FIELD_SET
        if missing:
            raise StateProviderSchemaError("mad-refs entry missing field(s)")
        if extra:
            raise StateProviderSchemaError("mad-refs entry has forbidden field(s)")

        task_id = _require_str(ref["task_id"])
        dispatch_id = _require_str(ref["dispatch_id"])
        purpose = ref["purpose"]
        if purpose not in _MAD_PURPOSES:
            raise StateProviderSchemaError("mad-refs purpose is invalid")
        deliberation_id = _require_str(ref["deliberation_id"])
        depth = ref["depth"]
        if depth not in _MAD_DEPTHS:
            raise StateProviderSchemaError("mad-refs depth is invalid")
        stdout_sha = ref["stdout_sha256"]
        if not isinstance(stdout_sha, str) or not _SHA256_RE.fullmatch(stdout_sha):
            raise StateProviderSchemaError("mad-refs stdout_sha256 must be 64 lowercase hex")
        report_sha = ref["report_sha256"]
        if not isinstance(report_sha, str) or not _SHA256_RE.fullmatch(report_sha):
            raise StateProviderSchemaError("mad-refs report_sha256 must be 64 lowercase hex")
        status = _require_str(ref["status"])
        archive_path = _require_str(ref["archive_path"])
        if not Path(archive_path).is_absolute():
            raise StateProviderSchemaError("mad-refs archive_path must be absolute")
        created_at = _require_str(ref["created_at"])

        pair = (dispatch_id, purpose)
        if pair in seen_pairs:
            raise StateProviderSchemaError("duplicate mad-refs entry")
        seen_pairs.add(pair)

        result.append(MadRefEntry(
            task_id=_freeze(task_id), dispatch_id=_freeze(dispatch_id),
            purpose=_freeze(purpose), deliberation_id=_freeze(deliberation_id),
            depth=_freeze(depth), stdout_sha256=stdout_sha,
            report_sha256=report_sha, status=_freeze(status),
            archive_path=_freeze(archive_path), created_at=_freeze(created_at),
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
    task_ids = {t.task_id for t in tasks}
    event_ids = {e.event_id for e in events}
    event_index = {e.event_id: e for e in events}
    acceptance_filenames = {a.filename for a in acceptances}

    events_by_task: dict[str, list[EventEntry]] = {}
    for e in events:
        events_by_task.setdefault(e.task_id, []).append(e)

    # Outbox raw bytes indexed by event_id
    outbox_raw_by_event: dict[str, bytes] = {}
    for (_, raw), o in zip(a_outbox_raw, outbox):
        outbox_raw_by_event[o.event_id] = raw

    # ── Outbox event_id must exist ──
    for o in outbox:
        if o.event_id not in event_ids:
            raise StateProviderInconsistentSnapshotError("outbox references non-existent event")

    # ── TASK_DISPATCHED event must have a corresponding outbox ──
    for e in events:
        if e.event_type == "TASK_DISPATCHED":
            if e.event_id not in outbox_raw_by_event:
                raise StateProviderInconsistentSnapshotError(
                    "TASK_DISPATCHED event has no corresponding outbox"
                )

    # ── Dispatch → event evidence ──
    for t in tasks:
        if t.current_dispatch is not None and t.state in (
            "dispatched", "in_progress", "review_ready"
        ):
            did = t.current_dispatch.dispatch_id
            task_events = events_by_task.get(t.task_id, [])
            if not any(
                e.dispatch_id == did and e.revision == t.revision and e.attempt == t.attempt
                for e in task_events
            ):
                raise StateProviderInconsistentSnapshotError(
                    "current_dispatch has no matching event evidence"
                )

    # ── Acceptance path → file existence ──
    for t in tasks:
        if t.state in ("accepted", "integrated") and t.acceptance_path:
            acc_filename = Path(t.acceptance_path).name
            if acc_filename not in acceptance_filenames:
                raise StateProviderInconsistentSnapshotError(
                    "acceptance_path references non-existent file"
                )

    # ── Digest parity ──
    for e in events:
        if e.event_type == "TASK_DISPATCHED" and e.payload_digest:
            ob_raw = outbox_raw_by_event.get(e.event_id)
            if ob_raw is not None:
                expected = f"sha256:{_sha256_hex(ob_raw)}"
                if e.payload_digest != expected:
                    raise StateProviderInconsistentSnapshotError(
                        "TASK_DISPATCHED payload_digest does not match outbox"
                    )

    # ── Orphan: outbox/event/acceptance/mad-ref → non-existent task ──
    for o in outbox:
        if o.task_id not in task_ids:
            raise StateProviderInconsistentSnapshotError("outbox references non-existent task")
    for e in events:
        if e.task_id not in task_ids:
            raise StateProviderInconsistentSnapshotError("event references non-existent task")
    for a in acceptances:
        if a.task_id not in task_ids:
            raise StateProviderInconsistentSnapshotError("acceptance references non-existent task")

    # ── Outbox task_id matches event task_id ──
    for o in outbox:
        ev = event_index.get(o.event_id)
        if ev is not None and ev.task_id != o.task_id:
            raise StateProviderInconsistentSnapshotError("outbox event_id task mismatch")

    # ── Mad-ref cross-ref ──
    if mad_refs is not None:
        for mr in mad_refs:
            if mr.task_id not in task_ids:
                raise StateProviderInconsistentSnapshotError("mad-ref references non-existent task")
            if not any(e.dispatch_id == mr.dispatch_id and e.task_id == mr.task_id
                       for e in events):
                raise StateProviderInconsistentSnapshotError(
                    "mad-ref dispatch_id has no matching event"
                )


# ── validation helpers ────────────────────────────────────────────────────


def _validate_task_id(raw: Any) -> str:
    if not isinstance(raw, str) or not _TASK_ID_RE.match(raw):
        raise StateProviderSchemaError("invalid task_id")
    return raw


def _validate_state(raw: Any) -> str:
    if not isinstance(raw, str) or raw not in _STATES:
        raise StateProviderSchemaError("invalid state")
    return raw


def _validate_sha40(raw: Any, required: bool) -> str | None:
    if raw is None:
        if required:
            raise StateProviderSchemaError("required SHA field is null")
        return None
    if not isinstance(raw, str) or not _SHA40_RE.match(raw):
        raise StateProviderSchemaError("invalid SHA field")
    return raw


def _validate_str_or_none(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise StateProviderSchemaError("expected string or null")
    return raw
