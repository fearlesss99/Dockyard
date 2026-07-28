# StateProvider — Read-Only Contract (Frozen — TC-13.17b)

Interface #21 frozen contract.  TC-13.17a froze the contract; TC-13.17b
produced the production module; TC-13.17b.1 aligned YAML format compatibility;
TC-13.17b.2 finalised this document against the current production API.

This document is the authoritative frozen specification.
Every field count, field name, field order, field type, and collection
declared below must match `skills/agentdesk/scripts/state_provider.py`
exactly.  Verification is enforced by `TC1317b2ContractFreezeTests`.

---
## 1. Purpose

`StateProvider` is the **read-only** service boundary for canonical
project state.  The Skill (PM/Worker runbooks) and the HTML Dashboard
(TC-13.20) both consume data through this interface.  Neither writes to
canonical state directly — all writes go through
`ControlPlaneTransitionService` (TC-13.11).

StateProvider does **not** acquire the control-plane state lock, does
**not** write lock files, does **not** spawn subprocesses, and does **not**
import write-end gateway, WorkerAdapter, or ControlPlaneTransitionService
execution entry points.

The production module parses events and outbox in the **canonical YAML**
format produced by `control_plane_transition._to_yaml_str()` — a
deterministic, sorted-key, 2-space-indent, LF-only YAML subset.
`tasks.yaml` and `mad-refs.yaml` are parsed as JSON (matching their
writer format).

---
## 2. Canonical Input Files

| # | File | Schema | Required | Description |
|---|------|--------|----------|-------------|
| 1 | `docs/pm/state/tasks.yaml` | `agentdesk.tasks/v2` | Yes | Task ledger: root object with `schema_version`, `project_id`, `adoption_level`, `updated_at`, `pm_control` (holder_id, lease_epoch, mode), `tasks` array |
| 2 | `docs/pm/events/*.yaml` | `agentdesk.state-event/v2` | Yes | Immutable state-transition event records |
| 3 | `docs/pm/outbox/*.yaml` | `agentdesk.outbox-message/v2` | Yes | Replayable outbox message records |
| 4 | `docs/pm/acceptances/*.md` | `agentdesk.acceptance/v2` | Yes | Acceptance records (filename: `{task_id}-r{revision}-a{attempt}-review{N}.md`) |
| 5 | `.agentdesk/runtime/mad-refs.yaml` | `agentdesk.mad-refs/v1` | No | Runtime MAD invocation reference records |

### 2.1 Explicit Exclusions

StateProvider does **not** read, parse, or depend on:

- Approval grant/revoke records (`docs/pm/approvals/`).  Approval
  authorization remains the responsibility of `ApprovalGate`
  (TC-13.12).
- Worker-slot lease files (`.agentdesk/runtime/.worker-slot-lease.lock`,
  `.agentdesk/runtime/worker-slot-lease.yaml`).
- PM lease files (`.agentdesk/runtime/pm-lease.yaml`,
  `.agentdesk/runtime/.pm-lease.lock`).
- Lock files, ownership tokens, or any runtime lease state.
- Transport receipts, process IDs, environment variables, or secrets.
- MAD archive internal files (`MAD_HOME/deliberations/*/`).
- Derived views: `BOARD.md`, `STATUS.md`, `DECISIONS.md`, `ROLES.md`,
  `CHECKS.yaml`, `ROLE-POLICIES.yaml`, `PM-PLAYBOOK.md`.
- `.agentdesk/runtime/model-bindings.yaml`, `.agentdesk/runtime/routes.yaml`,
  `.agentdesk/runtime/transport-receipts.yaml`.

`pm_control` within `tasks.yaml` is canonical read-only data and is
returned as-is; `.pm-lease`, `WorkerSlotLease`, lock files, and
ownership tokens are forbidden regardless of whether they overlap
conceptually with `pm_control` fields.

---
## 3. Multi-File Consistency Protocol (Fail-Closed)

StateProvider does **not** acquire the state lock, but detects
concurrent transitions during snapshot construction.  All five input
classes are read twice — not just `tasks.yaml`:

1. Read `docs/pm/state/tasks.yaml` raw bytes.
2. Read events directory in stable-sorted glob order → list of
   `(filename, raw_bytes)`.
3. Read outbox directory in stable-sorted glob order → list of
   `(filename, raw_bytes)`.
4. Read acceptances directory in stable-sorted glob order → list of
   `(filename, raw_bytes)`.
5. If `mad-refs.yaml` exists, read raw bytes; otherwise `None`.
6. Repeat steps 1–5, producing a second complete snapshot of all five
   input classes.
7. If **any** filename, file content, or file presence differs between
   the two passes → raise `StateProviderSnapshotChangedError`.
8. Validate cross-file referential integrity and digest parity
   across all loaded records.  Detect orphans, partial transitions,
   and reference inconsistencies.
9. If any inconsistency found → raise `StateProviderInconsistentSnapshotError`.
10. Only when steps 1–9 all pass: construct a frozen snapshot and return it.

StateProvider never skips validation — it does not assume the caller
has already verified the snapshot.

### 3.1 Stable Directory Sort

Directory entries are sorted with a locale-independent, byte-ordered key:

```python
sorted(entries, key=lambda p: p.name.encode("utf-8"))
```

Locale-dependent `sorted()`, `mtime`, or filesystem-returned order are
forbidden.

### 3.2 Symlink and Reparse Point Rejection

Symlinks, junctions, and reparse points in canonical directories are
rejected fail-closed.  StateProvider raises `StateProviderSchemaError`
if any file in `docs/pm/events/`, `docs/pm/outbox/`, or
`docs/pm/acceptances/` is a symlink or Windows reparse point.

### 3.3 Cross-File Consistency Rules

- Every event `event_id` referenced by an outbox `event_id` must exist
  in the events set.
- Every `TASK_DISPATCHED` event must have a corresponding outbox entry
  sharing the same `event_id`.
- Every `dispatch_id` in an active task's `current_dispatch` must have at
  least one corresponding event in the events set whose `task_id`,
  `revision`, `attempt`, and `dispatch_id` all match the task.
- Every `acceptance_path` in a task at state `accepted` or beyond must
  exist in the acceptances set.
- No two events may share the same `event_id`.
- No two outbox messages may share the same `message_id`.
- No two acceptance records may share the same filename.
- Every task `task_id` must be unique within `tasks.yaml`.
- Every `mad-ref` entry's `task_id` must appear in `tasks.yaml`.
- Every `mad-ref` entry's `dispatch_id` must correspond to at least
  one event in the events set.
- Digest parity: the `payload_digest` in a `TASK_DISPATCHED` event
  must match `sha256:<hex>` of the raw bytes of the matching outbox file.
- Partial transitions (event present but outbox missing for
  `TASK_DISPATCHED`) are rejected.
- Orphan evidence (event/outbox/acceptance/mad-ref referencing a
  non-existent task) is rejected.
- Duplicate records of any kind are rejected.

---
## 4. Input Schema — `tasks.yaml`

Schema: `agentdesk.tasks/v2`.  Serialised as JSON (`json.dumps` with
`sorted_keys=True`).

Root object keys:

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `schema_version` | `str` | Yes | Must be `"agentdesk.tasks/v2"` |
| `project_id` | `str` | Yes | Non-empty project identifier |
| `adoption_level` | `str` | Yes | One of `lite`, `standard`, `automated` |
| `updated_at` | `str` | Yes | RFC 3339 UTC timestamp |
| `pm_control` | `object` | Yes | `holder_id` (str), `lease_epoch` (int ≥ 1, non-bool), `mode` (`manual` or `timed`) |
| `tasks` | `array` | Yes | May be empty; task objects follow the schema below |

### 4.1 Task Object Schema

Each task object in the `tasks` array has exactly 24 fields:

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `task_id` | `str` | `^TC-[0-9]{3,}$` |
| 2 | `revision` | `int` | Non-bool, ≥ 1 |
| 3 | `task_card_path` | `str` | Project-relative path to task card |
| 4 | `task_card_commit` | `str` | 40-char lowercase hex SHA |
| 5 | `state` | `str` | One of 11 frozen states |
| 6 | `attempt` | `int \| None` | Non-bool, ≥ 1 when active dispatch; null otherwise |
| 7 | `current_dispatch` | `object \| None` | Dispatch identity object or null |
| 8 | `report_path` | `str \| None` | Project-relative report path |
| 9 | `granted_approval_ids` | `list[str] \| None` | Model-degradation approval IDs |
| 10 | `delivery_state` | `str \| None` | `none`, `working`, `submitted`, `invalid`, `accepted`, `rejected` |
| 11 | `integration_state` | `str \| None` | `not_applicable`, `pending`, `integrated`, `failed` |
| 12 | `implementation_commit` | `str \| None` | 40-char lowercase hex SHA |
| 13 | `report_commit` | `str \| None` | 40-char lowercase hex SHA |
| 14 | `accepted_commit` | `str \| None` | 40-char lowercase hex SHA |
| 15 | `acceptance_path` | `str \| None` | Project-relative acceptance path |
| 16 | `integrated_commit` | `str \| None` | 40-char lowercase hex SHA |
| 17 | `blocked_reason` | `str \| None` | |
| 18 | `blocked_kind` | `str \| None` | One of 9 frozen blocked kinds |
| 19 | `blocked_owner` | `str \| None` | |
| 20 | `unblock_condition` | `str \| None` | |
| 21 | `review_after` | `str \| None` | RFC 3339 UTC |
| 22 | `blocked_attempt_valid` | `bool \| None` | Non-int — strictly `bool` or `None` |
| 23 | `resume_state` | `str \| None` | Target state after unblock |
| 24 | `timestamps` | `object` | See §4.2 |

### 4.2 Task Timestamps

| Field | Type | Set when |
|-------|------|----------|
| `created_at` | `str` | Task created |
| `ready_at` | `str \| None` | `TASK_SPECIFIED` |
| `dispatched_at` | `str \| None` | `TASK_DISPATCHED` |
| `started_at` | `str \| None` | `DISPATCH_ACKNOWLEDGED` |
| `delivered_at` | `str \| None` | `DELIVERY_SUBMITTED` |
| `blocked_at` | `str \| None` | `TASK_BLOCKED` / `INTEGRATION_FAILED` |
| `accepted_at` | `str \| None` | `DELIVERY_ACCEPTED` |
| `integrated_at` | `str \| None` | `CHANGE_INTEGRATED` |
| `updated_at` | `str` | Every transition |

### 4.3 Task States (Frozen Set)

```text
draft → ready → dispatched → in_progress → review_ready → accepted → integrated
                                                    ↘ returned → ready
Any → blocked → draft | ready | cancelled
Any → cancelled
Any → superseded
```

---
## 5. Input Schema — Events (`docs/pm/events/*.yaml`)

Schema: `agentdesk.state-event/v2`.  Serialised as canonical YAML
(`control_plane_transition._to_yaml_str`).

### 5.1 Common Fields

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `schema_version` | `str` | Must be `"agentdesk.state-event/v2"` |
| 2 | `event_id` | `str` | `^EVT-.+` |
| 3 | `event_type` | `str` | One of 15 frozen event types |
| 4 | `task_id` | `str` | `^TC-[0-9]{3,}$` |
| 5 | `revision` | `int` | Non-bool |
| 6 | `attempt` | `int \| None` | Non-bool when present |
| 7 | `dispatch_id` | `str \| None` | |
| 8 | `from_state` | `str` | One of 11 frozen states |
| 9 | `to_state` | `str` | One of 11 frozen states |
| 10 | `lease_epoch` | `int` | Non-bool, ≥ 1 |
| 11 | `actor_role_id` | `str` | `"PM"` or role ID |
| 12 | `occurred_at` | `str` | RFC 3339 UTC |
| 13 | `source_message_id` | `str \| None` | |
| 14 | `evidence_refs` | `list[str]` | |
| 15 | `guard_results` | `list[object]` | Each has `guard`, `inputs`, `result`, `checked_at`, `evidence_ref` |

### 5.2 Event-Type-Specific Fields

| Event Type | Extra Fields |
|------------|-------------|
| `TASK_DISPATCHED` | `payload_digest` (`sha256:<64 hex>`) |
| `CHANGE_INTEGRATED` | `accepted_commit`, `integrated_commit`, `equivalence_method`, `equivalence_result`, `equivalence_evidence_ref` |

### 5.3 Frozen Event Types (15)

```text
TASK_SPECIFIED, TASK_DISPATCHED, DISPATCH_ACKNOWLEDGED,
DELIVERY_SUBMITTED, DELIVERY_ACCEPTED, DELIVERY_RETURNED,
TASK_REQUEUED, CHANGE_INTEGRATED, INTEGRATION_FAILED,
TASK_BLOCKED, BLOCKER_RESOLVED, BLOCKER_RESCOPED,
BLOCKER_CANCELLED, TASK_CANCELLED, TASK_SUPERSEDED
```

---
## 6. Input Schema — Outbox (`docs/pm/outbox/*.yaml`)

Schema: `agentdesk.outbox-message/v2`.  Serialised as canonical YAML
(`control_plane_transition._to_yaml_str`).

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `schema_version` | `str` | Must be `"agentdesk.outbox-message/v2"` |
| 2 | `message_id` | `str` | `^MSG-.+` |
| 3 | `event_id` | `str` | `^EVT-.+` |
| 4 | `message_type` | `str` | e.g. `task.dispatch` |
| 5 | `dedupe_key` | `str` | `{task_id}/r{revision}/a{attempt}/{dispatch_id}/{message_type}` |
| 6 | `task_id` | `str` | `^TC-[0-9]{3,}$` |
| 7 | `revision` | `int` | Non-bool |
| 8 | `attempt` | `int` | Non-bool |
| 9 | `dispatch_id` | `str` | |
| 10 | `destination_role_id` | `str` | |
| 11 | `created_at` | `str` | RFC 3339 UTC |
| 12 | `model_selection` | `object` | 10 frozen fields (§6.1) |
| 13 | `payload` | `object` | `task_path`, `task_card_commit`, `base_commit`, `branch`, `report_path` |

### 6.1 Model Selection Fields (Frozen 10)

```text
required_model_tier, required_model_capabilities, model_binding_id,
selected_model_provider, selected_model_id, selected_model_tier,
selected_deliberation_tier, selected_context_window_tokens,
selected_model_capabilities, model_degradation_approval_id
```

---
## 7. Input Schema — Acceptances (`docs/pm/acceptances/*.md`)

Schema: `agentdesk.acceptance/v2`.  Markdown files with YAML frontmatter
between `---` delimiters.

Filename pattern: `{task_id}-r{revision}-a{attempt}-review{N}.md`
(where N ≥ 1, `task_id` matches `TC-XXX`).

### 7.1 AcceptanceEntry Fields (Frozen 16)

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `schema_version` | `str` | Must be `"agentdesk.acceptance/v2"` |
| 2 | `task_id` | `str` | `^TC-[0-9]{3,}$` |
| 3 | `revision` | `int` | Non-bool, ≥ 1 |
| 4 | `attempt` | `int` | Non-bool, ≥ 1 |
| 5 | `review_n` | `int` | Non-bool, ≥ 1 — from filename |
| 6 | `decision` | `str` | `accepted`, `returned`, `blocked` |
| 7 | `implementation_commit` | `str` | 40-char lowercase hex SHA |
| 8 | `report_commit` | `str` | 40-char lowercase hex SHA |
| 9 | `base_commit` | `str \| None` | 40-char lowercase hex SHA |
| 10 | `accepted_commit` | `str \| None` | 40-char lowercase hex SHA; set when decision=accepted |
| 11 | `reviewed_dispatch_id` | `str` | Non-empty dispatch ID |
| 12 | `type` | `str` | Task type |
| 13 | `owner_approval_gate` | `str` | `none` or owner approval gate value |
| 14 | `owner_approval_ids` | `tuple[str, ...]` | Owner approval IDs (may be empty tuple) |
| 15 | `raw_body` | `str` | Full markdown body after frontmatter delimiter |
| 16 | `filename` | `str` | Base filename within `docs/pm/acceptances/` |

---
## 8. Optional Runtime Input — `mad-refs.yaml`

Schema: `agentdesk.mad-refs/v1`.  Located at `.agentdesk/runtime/mad-refs.yaml`.
Serialised as JSON (matching `mad_refs.py`).

If the file does not exist, StateProvider proceeds without error — the
`mad_refs` field in the snapshot is `None`.

### 8.1 `MadRefEntry` Fields (10 — matching `mad_refs.py`)

| # | Field | Type |
|---|-------|------|
| 1 | `task_id` | `str` |
| 2 | `dispatch_id` | `str` |
| 3 | `purpose` | `str` — `planning` or `audit` |
| 4 | `deliberation_id` | `str` |
| 5 | `depth` | `str` — `fast`, `balanced`, or `deep` |
| 6 | `stdout_sha256` | `str` — 64 lowercase hex |
| 7 | `report_sha256` | `str` — 64 lowercase hex |
| 8 | `status` | `str` |
| 9 | `archive_path` | `str` — absolute path |
| 10 | `created_at` | `str` — RFC 3339 UTC |

---
## 9. Output — Frozen Snapshot

The snapshot is returned as a frozen/slots dataclass.  All collections
are `tuple`.  No `dict`, `list`, `set`, `Mapping`, or `MappingProxyType`
is exposed on any public dataclass field.

### 9.1 `StateSnapshot` — Exact Fields (14)

```
StateSnapshot (frozen=True, slots=True)
├── project_root: Path
├── schema_version: str
├── project_id: str
├── adoption_level: str
├── updated_at: str
├── pm_holder_id: str
├── pm_lease_epoch: int
├── pm_mode: str
├── tasks: tuple[TaskEntry, ...]
├── events: tuple[EventEntry, ...]
├── outbox: tuple[OutboxEntry, ...]
├── acceptances: tuple[AcceptanceEntry, ...]
├── mad_refs: tuple[MadRefEntry, ...] | None
└── read_hexsha: str
```

### 9.2 `TaskEntry` — Exact Fields (24)

Frozen/slots dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `task_id` | `str` |
| 2 | `revision` | `int` |
| 3 | `task_card_path` | `str` |
| 4 | `task_card_commit` | `str` |
| 5 | `state` | `str` |
| 6 | `attempt` | `int \| None` |
| 7 | `current_dispatch` | `DispatchInfo \| None` |
| 8 | `report_path` | `str \| None` |
| 9 | `granted_approval_ids` | `tuple[str, ...] \| None` |
| 10 | `delivery_state` | `str \| None` |
| 11 | `integration_state` | `str \| None` |
| 12 | `implementation_commit` | `str \| None` |
| 13 | `report_commit` | `str \| None` |
| 14 | `accepted_commit` | `str \| None` |
| 15 | `acceptance_path` | `str \| None` |
| 16 | `integrated_commit` | `str \| None` |
| 17 | `blocked_reason` | `str \| None` |
| 18 | `blocked_kind` | `str \| None` |
| 19 | `blocked_owner` | `str \| None` |
| 20 | `unblock_condition` | `str \| None` |
| 21 | `review_after` | `str \| None` |
| 22 | `blocked_attempt_valid` | `bool \| None` |
| 23 | `resume_state` | `str \| None` |
| 24 | `timestamps` | `TaskTimestamps` |

### 9.3 `TaskTimestamps` — Exact Fields (9)

Frozen/slots nested dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `created_at` | `str` |
| 2 | `ready_at` | `str \| None` |
| 3 | `dispatched_at` | `str \| None` |
| 4 | `started_at` | `str \| None` |
| 5 | `delivered_at` | `str \| None` |
| 6 | `blocked_at` | `str \| None` |
| 7 | `accepted_at` | `str \| None` |
| 8 | `integrated_at` | `str \| None` |
| 9 | `updated_at` | `str` |

### 9.4 `DispatchInfo` — Exact Fields (7)

Frozen/slots nested dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `dispatch_id` | `str` |
| 2 | `attempt_id` | `str` |
| 3 | `role_id` | `str` |
| 4 | `base_commit` | `str` |
| 5 | `branch` | `str` |
| 6 | `dispatched_at` | `str` |
| 7 | `model_selection` | `ModelSelectionSnapshot` |

### 9.5 `ModelSelectionSnapshot` — Exact Fields (10)

| # | Field | Type |
|---|-------|------|
| 1 | `required_model_tier` | `str` |
| 2 | `required_model_capabilities` | `tuple[str, ...]` |
| 3 | `model_binding_id` | `str` |
| 4 | `selected_model_provider` | `str` |
| 5 | `selected_model_id` | `str` |
| 6 | `selected_model_tier` | `str` |
| 7 | `selected_deliberation_tier` | `str` |
| 8 | `selected_context_window_tokens` | `int` |
| 9 | `selected_model_capabilities` | `tuple[str, ...]` |
| 10 | `model_degradation_approval_id` | `str \| None` |

### 9.6 `EventEntry` — Exact Fields (17)

A single frozen/slots dataclass.  Event-type-specific extra fields
are `None` when not applicable.

| # | Field | Type |
|---|-------|------|
| 1 | `schema_version` | `str` |
| 2 | `event_id` | `str` |
| 3 | `event_type` | `str` |
| 4 | `task_id` | `str` |
| 5 | `revision` | `int` |
| 6 | `attempt` | `int \| None` |
| 7 | `dispatch_id` | `str \| None` |
| 8 | `from_state` | `str` |
| 9 | `to_state` | `str` |
| 10 | `lease_epoch` | `int` |
| 11 | `actor_role_id` | `str` |
| 12 | `occurred_at` | `str` |
| 13 | `source_message_id` | `str \| None` |
| 14 | `evidence_refs` | `tuple[str, ...]` |
| 15 | `guard_results` | `tuple[GuardResult, ...]` |
| 16 | `payload_digest` | `str \| None` |
| 17 | `extra_fields` | `tuple[tuple[str, object], ...] \| None` |

### 9.7 `GuardInput` — Exact Fields (2)

Frozen/slots dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `key` | `str` |
| 2 | `value` | `str \| int \| bool \| None` |

### 9.8 `GuardResult` — Exact Fields (5)

Frozen/slots nested dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `guard` | `str` |
| 2 | `inputs` | `tuple[GuardInput, ...]` |
| 3 | `result` | `str` |
| 4 | `checked_at` | `str` |
| 5 | `evidence_ref` | `str` |

### 9.9 `OutboxEntry` — Exact Fields (13)

Frozen/slots dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `schema_version` | `str` |
| 2 | `message_id` | `str` |
| 3 | `event_id` | `str` |
| 4 | `message_type` | `str` |
| 5 | `dedupe_key` | `str` |
| 6 | `task_id` | `str` |
| 7 | `revision` | `int` |
| 8 | `attempt` | `int` |
| 9 | `dispatch_id` | `str` |
| 10 | `destination_role_id` | `str` |
| 11 | `created_at` | `str` |
| 12 | `model_selection` | `ModelSelectionSnapshot` |
| 13 | `payload` | `OutboxPayload` |

### 9.10 `OutboxPayload` — Exact Fields (5)

Frozen/slots nested dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `task_path` | `str` |
| 2 | `task_card_commit` | `str` |
| 3 | `base_commit` | `str` |
| 4 | `branch` | `str` |
| 5 | `report_path` | `str` |

### 9.11 `AcceptanceEntry` — Exact Fields (16)

Frozen/slots dataclass as specified in §7.1.

### 9.12 `MadRefEntry` — Exact Fields (10)

Frozen/slots dataclass as specified in §8.1.

---
## 10. Public API

### 10.1 `__all__` (19 symbols)

```python
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
```

### 10.2 Construction

```python
StateProvider(project_root: Path)
```

- `project_root` must be an absolute `Path`.
- Construction does **not** read files — it only validates that
  `project_root` is absolute and is a `Path`.
- Raises `StateProviderInputError` if `project_root` is not absolute
  or is not a `Path`.

### 10.3 Snapshot Method

```python
def snapshot(self) -> StateSnapshot:
    ...
```

- Executes the multi-file consistency protocol (§3).
- Returns a frozen `StateSnapshot`.
- All errors raised from `snapshot()` — never from construction.

### 10.4 No Query Methods

TC-13.17b does not add optional query methods.  The only public read
entry points are:

```python
StateProvider(project_root: Path)
StateProvider.snapshot() -> StateSnapshot
```

Consumers filter on the immutable `tuple` collections returned in
`StateSnapshot` using standard Python iteration, comprehension, or
lookup patterns.

---
## 11. Exception Hierarchy

```text
StateProviderError (Exception)
├── StateProviderInputError
├── StateProviderNotFoundError
├── StateProviderSchemaError
├── StateProviderSnapshotChangedError
└── StateProviderInconsistentSnapshotError
```

No `PermissionDeniedError` — there is no real permissions system to
evidence such a distinction.

---
## 12. Design Constraints

### 12.1 Input/Output Types

- All input and output types are frozen/slots dataclasses.
- All collections are `tuple` — no public `dict`, `list`, or `set`.
- No public field has type `Mapping`, `MappingProxyType`, or `dict`.
- `project_root` is an absolute `Path`.

### 12.2 Determinism

- Directory listing sort order: `sorted(entries, key=lambda p: p.name.encode("utf-8"))`.
  Locale-dependent `sorted()`, `mtime`, or filesystem-returned order are
  forbidden.
- No implicit `now` — the snapshot reflects the disk state at read time.

### 12.3 Error Message Safety

Error messages never contain:
- File paths
- `task_id` values
- `dispatch_id` values
- Task content or task IDs
- File content
- Secrets

### 12.4 Import Boundaries

`state_provider.py` does not import:
- `control_plane_transition.py` or any other write-end module
- `worker_adapter.py`
- `approval_gate.py`
- Any module that writes files, acquires locks, or spawns subprocesses

`state_provider.py` implements its own pure read-only strict parsers inline.

### 12.5 Security

The production module guarantees:
- Zero file writes
- Zero lock acquisition
- Zero Git operations
- Zero subprocess spawn
- Zero network/API/model calls
- No reads from derived views
- No reads from MAD archive internals
- No modification of working directory, environment variables, or input objects
- `repr()` is never called on untrusted objects in error paths

---
## 13. Non-Goals (Explicitly Excluded)

- Approval authorization → `ApprovalGate` (TC-13.12)
- Worker-slot lease, PM lease, lock ownership tokens
- Transport receipts, process IDs, environment variables, secrets
- MAD archive internal file reading
- `BOARD.md`, `STATUS.md` and other derived views as authoritative input
- Writing any file
- Acquiring any lock
- Spawning subprocesses

---
## 14. Status

- Interface #21 is **Current** — TC-13.17b.
- This document (§1–§13) is the frozen contract implemented by TC-13.17b.
- TC-13.17b (production module) is **complete**.
- TC-13.18 (WorkflowOrchestrator) and TC-13.20 (HTML Dashboard)
  remain **Target**.
- ADR §2.7 references this contract.
