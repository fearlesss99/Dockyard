# StateProvider — Read-Only Contract (Frozen — TC-13.17a)

Interface #21 frozen contract.  No production module is shipped under TC-13.17a
— this document is the deliverable.  TC-13.17b produces the production module.

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
- Worker-slot lease files, PM lease files, lock files.
- Transport receipts, process IDs, environment variables, or secrets.
- MAD archive internal files (`MAD_HOME/deliberations/*/`).
- Derived views: `BOARD.md`, `STATUS.md`, `DECISIONS.md`, `ROLES.md`,
  `CHECKS.yaml`, `ROLE-POLICIES.yaml`, `PM-PLAYBOOK.md`.
- `.agentdesk/runtime/model-bindings.yaml`, `.agentdesk/runtime/routes.yaml`,
  `.agentdesk/runtime/transport-receipts.yaml`, `.agentdesk/runtime/pm-lease.yaml`.

---
## 3. Multi-File Consistency Protocol (Fail-Closed)

StateProvider does **not** acquire the state lock, but must detect
concurrent transitions during snapshot construction.  The protocol is:

1. Read `docs/pm/state/tasks.yaml` raw bytes → **A**.
2. Read events directory in stable-sorted glob order → list of
   `(filename, raw_bytes)`.
3. Read outbox directory in stable-sorted glob order → list of
   `(filename, raw_bytes)`.
4. Read acceptances directory in stable-sorted glob order → list of
   `(filename, raw_bytes)`.
5. If `mad-refs.yaml` exists, read raw bytes; otherwise `None`.
6. Re-read `docs/pm/state/tasks.yaml` raw bytes → **B**.
7. If **A** ≠ **B** → raise `StateProviderSnapshotChangedError`.
8. Validate cross-file referential integrity and digest parity
   across all loaded records.  Detect orphans, partial transitions,
   and reference inconsistencies.
9. If any inconsistency found → raise `StateProviderInconsistentSnapshotError`.
10. Only when steps 1–9 all pass: construct a frozen snapshot and return it.

StateProvider must **never** skip validation — it must not assume
the caller has already verified the snapshot.

### 3.1 Cross-File Consistency Rules

- Every event `event_id` referenced by an outbox `event_id` must exist
  in the events set.
- Every `dispatch_id` in an active task's `current_dispatch` must have at
  least one corresponding event in the events set.
- Every `acceptance_path` in a task at state `accepted` or beyond must
  exist in the acceptances set.
- No two events may share the same `event_id`.
- No two outbox messages may share the same `message_id`.
- No two acceptance records may share the same filename.
- Every task `task_id` must be unique within `tasks.yaml`.

---
## 4. Input Schema — `tasks.yaml`

Root object keys:

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `schema_version` | `str` | Yes | Must be `"agentdesk.tasks/v2"` |
| `project_id` | `str` | Yes | Non-empty project identifier |
| `adoption_level` | `str` | Yes | One of `lite`, `standard`, `automated` |
| `updated_at` | `str` | Yes | RFC 3339 UTC timestamp |
| `pm_control` | `object` | Yes | `holder_id` (str), `lease_epoch` (int ≥ 1), `mode` (`manual` or `timed`) |
| `tasks` | `array` | Yes | May be empty; task objects follow the schema below |

### 4.1 Task Object Schema

Each task object in the `tasks` array has these fields:

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
| 22 | `blocked_attempt_valid` | `bool \| null` | |
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

---
## 8. Optional Runtime Input — `mad-refs.yaml`

Schema: `agentdesk.mad-refs/v1`.  Located at `.agentdesk/runtime/mad-refs.yaml`.

If the file does not exist, StateProvider proceeds without error — the
`mad_refs` field in the snapshot is `None`.

### 8.1 `MadRefEntry` Fields (10)

| # | Field | Type |
|---|-------|------|
| 1 | `deliberation_id` | `str` |
| 2 | `purpose` | `str` |
| 3 | `task_id` | `str` |
| 4 | `dispatch_id` | `str` |
| 5 | `task_card_commit` | `str` |
| 6 | `task_card_path` | `str` |
| 7 | `delivery_report_path` | `str \| None` |
| 8 | `report_commit` | `str \| None` |
| 9 | `archive_path` | `str` |
| 10 | `created_at` | `str` |

---
## 9. Output — Frozen Snapshot

The snapshot is returned as a frozen/slots dataclass.  All collections
are `tuple` — no `dict`, `list`, or `set` is exposed through the public API.

### 9.1 `StateSnapshot`

```text
StateSnapshot
├── project_root: Path          (absolute)
├── schema_version: str         ("agentdesk.tasks/v2")
├── project_id: str
├── adoption_level: str         ("lite" | "standard" | "automated")
├── updated_at: str             (RFC 3339 UTC)
├── pm_holder_id: str
├── pm_lease_epoch: int         (≥ 1)
├── pm_mode: str                ("manual" | "timed")
├── tasks: tuple[TaskEntry, ...]
├── events: tuple[EventEntry, ...]
├── outbox: tuple[OutboxEntry, ...]
├── acceptances: tuple[AcceptanceEntry, ...]
└── mad_refs: tuple[MadRefEntry, ...] | None
```

### 9.2 `TaskEntry`

All 24 task fields from §4.1 mapped to a frozen/slots dataclass.
Timestamps are a nested frozen/slots `TaskTimestamps` dataclass.
Active dispatch is a nested frozen/slots `DispatchInfo` dataclass.

### 9.3 `EventEntry`

All common fields from §5.1 plus event-type-specific extra fields.
A closed `Union` of event-type-specific frozen/slots dataclasses
is acceptable as long as the union is exhaustive (all 15 types covered).

### 9.4 `OutboxEntry`

All 13 fields from §6 mapped to a frozen/slots dataclass.
`model_selection` is a nested frozen/slots `ModelSelectionSnapshot`.

### 9.5 `AcceptanceEntry`

Frozen/slots dataclass with fields derived from the acceptance
frontmatter and body.  Exact field set to be finalized in TC-13.17b
based on the `agentdesk.acceptance/v2` template.

### 9.6 `MadRefEntry` (from mad-refs)

All 10 fields from §8.1 mapped to a frozen/slots dataclass.
If `mad-refs.yaml` is absent, `mad_refs` is `None`.

---
## 10. Public API

### 10.1 Construction

```python
StateProvider(project_root: Path)
```

- `project_root` must be an absolute `Path`.
- Construction does **not** read files — it only validates that
  `project_root` is absolute.
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

### 10.3 Query Methods (Optional — TC-13.17b Decision)

The following query methods **may** be provided on the snapshot
or the provider, but are **not** frozen in TC-13.17a:

- `task_by_id(task_id: str) -> TaskEntry | None`
- `events_by_task(task_id: str) -> tuple[EventEntry, ...]`
- `outbox_by_task(task_id: str) -> tuple[OutboxEntry, ...]`
- `tasks_by_state(state: str) -> tuple[TaskEntry, ...]`

No query method that lacks existing consumption evidence may be
added in TC-13.17b.  If Dashboard or WorkflowOrchestrator require
a specific query pattern, the requirement must cite the consuming
code.

---
## 11. Exception Hierarchy

```text
StateProviderError (Exception)
├── StateProviderInputError          — invalid project_root, not absolute, not Path
├── StateProviderNotFoundError       — required file or directory missing
├── StateProviderSchemaError         — schema_version mismatch, invalid field type,
│                                      extra/missing keys, corrupt JSON/YAML
├── StateProviderSnapshotChangedError — tasks.yaml changed during snapshot construction
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

### 12.2 Determinism

- Directory listing sort order must be deterministic (locale-aware
  `sorted()` on filenames).
- Time-related methods receive an explicit UTC `datetime` parameter;
  no implicit `now` unless a consuming API contract requires it.
  (No consuming API currently does — leave `now` out of TC-13.17a.)

### 12.3 Error Message Safety

Error messages must **never** contain:
- File paths
- `dispatch_id` values
- Task content or task IDs
- File content
- Secrets

### 12.4 Import Boundaries

StateProvider must **not** import:
- Write-end gateways (`control_plane_transition`, `approval_gate` write helpers)
- `WorkerAdapter`
- `ControlPlaneTransitionService` execution entry points (`apply_transition`)

StateProvider **may** reuse pure parse/validation helpers from
`validate_project.py` and `control_plane_transition.py`, provided
those helpers do not write files, acquire locks, or spawn subprocesses.

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

- Interface #21 remains **Target** — TC-13.17a.
- This document (§1–§13) is the frozen contract for TC-13.17a.
- TC-13.17b (production module) is **not** started.
- TC-13.18 (WorkflowOrchestrator) and TC-13.20 (HTML Dashboard)
  remain **Target**.
- ADR §2.7 references this contract.
