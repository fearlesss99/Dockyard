# StateProvider — Read-Only Contract (Frozen — TC-13.17b)

Interface #21 frozen contract.  TC-13.17a froze the contract; TC-13.17b
produces the production module and this finalized revision.

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

### 3.2 Cross-File Consistency Rules

- Every event `event_id` referenced by an outbox `event_id` must exist
  in the events set.
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
- Digest parity is checked via raw canonical bytes: the `payload_digest`
  in a `TASK_DISPATCHED` event must match the SHA-256 of the outbox raw
  bytes for the matching outbox message.
- Partial transitions (event present but outbox missing, or vice versa,
  for the same dispatch) are rejected.
- Orphan evidence (event/outbox/acceptance/mad-ref referencing a
  non-existent task) is rejected.
- Duplicate records of any kind are rejected.

---
## 4. Input Schema — `tasks.yaml`

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
| 6 | `attempt` | `int \| null` | Non-bool, ≥ 1 when active dispatch; null otherwise |
| 7 | `current_dispatch` | `object \| null` | Dispatch identity object or null |
| 8 | `report_path` | `str \| null` | Project-relative report path |
| 9 | `granted_approval_ids` | `list[str] \| null` | Model-degradation approval IDs |
| 10 | `delivery_state` | `str \| null` | `none`, `working`, `submitted`, `invalid`, `accepted`, `rejected` |
| 11 | `integration_state` | `str \| null` | `not_applicable`, `pending`, `integrated`, `failed` |
| 12 | `implementation_commit` | `str \| null` | 40-char lowercase hex SHA |
| 13 | `report_commit` | `str \| null` | 40-char lowercase hex SHA |
| 14 | `accepted_commit` | `str \| null` | 40-char lowercase hex SHA |
| 15 | `acceptance_path` | `str \| null` | Project-relative acceptance path |
| 16 | `integrated_commit` | `str \| null` | 40-char lowercase hex SHA |
| 17 | `blocked_reason` | `str \| null` | |
| 18 | `blocked_kind` | `str \| null` | One of 9 frozen blocked kinds |
| 19 | `blocked_owner` | `str \| null` | |
| 20 | `unblock_condition` | `str \| null` | |
| 21 | `review_after` | `str \| null` | RFC 3339 UTC |
| 22 | `blocked_attempt_valid` | `bool \| null` | Non-int — strictly `bool` or `None` |
| 23 | `resume_state` | `str \| null` | Target state after unblock |
| 24 | `timestamps` | `object` | See §4.2 |

### 4.2 Task Timestamps

| Field | Type | Set when |
|-------|------|----------|
| `created_at` | `str` | Task created |
| `ready_at` | `str \| null` | `TASK_SPECIFIED` |
| `dispatched_at` | `str \| null` | `TASK_DISPATCHED` |
| `started_at` | `str \| null` | `DISPATCH_ACKNOWLEDGED` |
| `delivered_at` | `str \| null` | `DELIVERY_SUBMITTED` |
| `blocked_at` | `str \| null` | `TASK_BLOCKED` / `INTEGRATION_FAILED` |
| `accepted_at` | `str \| null` | `DELIVERY_ACCEPTED` |
| `integrated_at` | `str \| null` | `CHANGE_INTEGRATED` |
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

Schema: `agentdesk.state-event/v2`.

### 5.1 Common Fields

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `schema_version` | `str` | Must be `"agentdesk.state-event/v2"` |
| 2 | `event_id` | `str` | `^EVT-.+` |
| 3 | `event_type` | `str` | One of 15 frozen event types |
| 4 | `task_id` | `str` | `^TC-[0-9]{3,}$` |
| 5 | `revision` | `int` | Non-bool |
| 6 | `attempt` | `int \| null` | Non-bool when present |
| 7 | `dispatch_id` | `str \| null` | |
| 8 | `from_state` | `str` | One of 11 frozen states |
| 9 | `to_state` | `str` | One of 11 frozen states |
| 10 | `lease_epoch` | `int` | Non-bool, ≥ 1 |
| 11 | `actor_role_id` | `str` | `"PM"` or role ID |
| 12 | `occurred_at` | `str` | RFC 3339 UTC |
| 13 | `source_message_id` | `str \| null` | |
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

Schema: `agentdesk.outbox-message/v2`.

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

Based on the `agentdesk.acceptance/v2` writer and the frozen validator in
`validate_project.py` (`_validate_acceptance_record`), each acceptance
record exposes exactly 16 fields:

| # | Field | Type | Notes |
|---|-------|------|-------|
| 1 | `schema_version` | `str` | Must be `"agentdesk.acceptance/v2"` |
| 2 | `task_id` | `str` | `^TC-[0-9]{3,}$` |
| 3 | `revision` | `int` | Non-bool, ≥ 1 |
| 4 | `attempt` | `int` | Non-bool, ≥ 1 |
| 5 | `review_n` | `int` | Non-bool, ≥ 1 — from filename |
| 6 | `decision` | `str` | `accepted`, `returned`, or `blocked` |
| 7 | `implementation_commit` | `str` | 40-char lowercase hex SHA |
| 8 | `report_commit` | `str` | 40-char lowercase hex SHA |
| 9 | `base_commit` | `str \| null` | 40-char lowercase hex SHA; from task card frontmatter |
| 10 | `accepted_commit` | `str \| null` | 40-char lowercase hex SHA; set when decision=accepted |
| 11 | `reviewed_dispatch_id` | `str` | Non-empty dispatch ID |
| 12 | `type` | `str` | Task type (implementation, qa, integration, review, architecture, docs, ops, spike) |
| 13 | `owner_approval_gate` | `str` | `none` or owner approval gate value |
| 14 | `owner_approval_ids` | `tuple[str, ...]` | Owner approval IDs (may be empty tuple) |
| 15 | `raw_body` | `str` | Full markdown body after frontmatter delimiter |
| 16 | `filename` | `str` | Base filename within `docs/pm/acceptances/` |

---
## 8. Optional Runtime Input — `mad-refs.yaml`

Schema: `agentdesk.mad-refs/v1`.  Located at `.agentdesk/runtime/mad-refs.yaml`.

If the file does not exist, StateProvider proceeds without error — the
`mad_refs` field in the snapshot is `None`.

### 8.1 `MadRefEntry` Fields (10 — matching `mad_refs.py`)

These fields exactly match the `MadRefEntry` dataclass in
`skills/agentdesk/scripts/mad_refs.py`:

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
are `tuple` — no `dict`, `list`, or `set` is exposed through the public API.

### 9.1 `StateSnapshot` — Exact Fields (14)

```text
StateSnapshot (frozen=True, slots=True)
├── project_root: Path              (absolute)
├── schema_version: str             ("agentdesk.tasks/v2")
├── project_id: str
├── adoption_level: str             ("lite" | "standard" | "automated")
├── updated_at: str                 (RFC 3339 UTC)
├── pm_holder_id: str
├── pm_lease_epoch: int             (≥ 1)
├── pm_mode: str                    ("manual" | "timed")
├── tasks: tuple[TaskEntry, ...]
├── events: tuple[EventEntry, ...]
├── outbox: tuple[OutboxEntry, ...]
├── acceptances: tuple[AcceptanceEntry, ...]
├── mad_refs: tuple[MadRefEntry, ...] | None
├── read_hexsha: str                (SHA-256 of raw bytes used for A/B comparison)
```

### 9.2 `TaskEntry` — Exact Fields (25)

Frozen/slots dataclass with 25 fields:

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
| 25 | `raw_task` | `Mapping[str, object]` (immutable proxy) |

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

### 9.4 `DispatchInfo` — Exact Fields (8)

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
| 8 | `raw_dispatch` | `Mapping[str, object]` (immutable proxy) |

### 9.5 `ModelSelectionSnapshot` — Exact Fields (10)

Frozen/slots dataclass with exactly the 10 model-selection fields:

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

### 9.6 `EventEntry` — Exact Fields (18)

A single frozen/slots dataclass representing all 15 event types.  Every
field is present on every instance; event-type-specific extra fields
are `None` when not applicable to that event type.

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
| 16 | `payload_digest` | `str \| None` — only for `TASK_DISPATCHED` |
| 17 | `extra_fields` | `tuple[tuple[str, str], ...] \| None` — event-type-specific extras as immutable pairs; only for `CHANGE_INTEGRATED` |
| 18 | `raw_event` | `Mapping[str, object]` (immutable proxy) |

### 9.7 `GuardResult` — Exact Fields (5)

Frozen/slots nested dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `guard` | `str` |
| 2 | `inputs` | `tuple[tuple[str, object], ...]` (immutable flattened key-value pairs) |
| 3 | `result` | `str` |
| 4 | `checked_at` | `str` |
| 5 | `evidence_ref` | `str` |

### 9.8 `OutboxEntry` — Exact Fields (14)

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
| 14 | `raw_outbox` | `Mapping[str, object]` (immutable proxy) |

### 9.9 `OutboxPayload` — Exact Fields (5)

Frozen/slots nested dataclass:

| # | Field | Type |
|---|-------|------|
| 1 | `task_path` | `str` |
| 2 | `task_card_commit` | `str` |
| 3 | `base_commit` | `str` |
| 4 | `branch` | `str` |
| 5 | `report_path` | `str` |

### 9.10 `AcceptanceEntry` — Exact Fields (16)

Frozen/slots dataclass as specified in §7.1.

### 9.11 `MadRefEntry` (from mad-refs) — Exact Fields (10)

Frozen/slots dataclass as specified in §8.1.

---
## 10. Public API

### 10.1 Construction

```python
StateProvider(project_root: Path)
```

- `project_root` must be an absolute `Path`.
- Construction does **not** read files — it only validates that
  `project_root` is absolute and is a `Path`.
- Raises `StateProviderInputError` if `project_root` is not absolute
  or is not a `Path`.

### 10.2 Snapshot Method

```python
def snapshot(self) -> StateSnapshot:
    ...
```

- Executes the multi-file consistency protocol (§3).
- Returns a frozen `StateSnapshot`.
- All errors raised from `snapshot()` — never from construction.

### 10.3 No Query Methods

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
├── StateProviderInputError              — invalid project_root, not absolute, not Path
├── StateProviderNotFoundError           — required file or directory missing
├── StateProviderSchemaError             — schema_version mismatch, invalid field type,
│                                          extra/missing keys, corrupt JSON/YAML
├── StateProviderSnapshotChangedError    — any input changed during snapshot construction
└── StateProviderInconsistentSnapshotError — orphan event, partial transition,
                                             cross-file reference mismatch,
                                             duplicate ID, missing companion evidence
```

No `PermissionDeniedError` — there is no real permissions system to
evidence such a distinction.

---
## 12. Design Constraints

### 12.1 Input/Output Types

- All input and output types are frozen/slots dataclasses.
- All collections are `tuple` — no public `dict`, `list`, or `set`.
- `project_root` is an absolute `Path`.
- `Mapping[str, object]` proxies are used internally to prevent mutation
  of raw parsed data; consumers receive only the frozen dataclass fields.

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

`state_provider.py` may reuse pure parse/validation helpers from
`validate_project.py` provided those helpers are read-only (no file
writes, no locks, no subprocesses).  Alternatively, `state_provider.py`
implements its own pure read-only strict parsers inline.

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
