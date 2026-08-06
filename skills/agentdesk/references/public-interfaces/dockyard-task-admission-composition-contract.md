# Dockyard Task Admission Composition Contract

Status: **Contract Current — TC-13.29l.5d.3**
Runtime: **Current — TC-13.29l.5d.4**
Schema: `dockyard.task-admission-composition/v1`

This contract freezes the narrow bridge from a materialized Dockyard PM task
to the existing Admission Preparation and materialization-admission handoff
owners. It does not implement dispatch, Scheduler policy, lease management,
recovery, MAD, acceptance, or integration. TC-13.29l.5e adds only a narrow
post-admission composition into the existing Scheduler-to-Worker handoff and
PM external Worker owners; it does not duplicate either owner.

## 1. Ownership

- StateProvider/canonical task events are the sole dependency-state authority.
- PortfolioScheduler policy/store and WorkerSlotLease evidence are the sole
  admission-context authorities.
- Dockyard only creates a read-only context projection and a local progress
  receipt; it never invents capacity, conflicts, authorization, liveness, or
  canonical state.
- Admission Preparation remains the sole assessment/model-selection owner.
- MaterializationAdmissionRuntime remains the sole Git/canonical/queue/
  admission owner.
- WorktreeLifecycleManager remains the sole managed-worktree creation owner.
- Scheduler-to-Worker Handoff Runtime and PM External Worker Runtime remain the
  sole process, ACK, delivery, finalizer, and durable replay owners.
- WorkflowOrchestrator Interface #22 remains the sole task lifecycle owner.

## 2. Frozen public types

All types are `frozen=True, slots=True`. Public annotations contain no `Any`,
`object`, `dict`, `Mapping`, `list`, `set`, callback, provider/model client,
subprocess handle, or mutable collection.

### 2.1 `DockyardAdmissionContextSnapshot` — exactly 15 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `snapshot_id` | `str` |
| 3 | `project_id` | `str` |
| 4 | `plan_id` | `str` |
| 5 | `plan_revision` | `int` |
| 6 | `canonical_snapshot_commit` | `str` |
| 7 | `scheduler_policy_version` | `str` |
| 8 | `evaluated_at` | `str` |
| 9 | `active_hard_conflict_keys` | `tuple[str, ...]` |
| 10 | `advisory_conflict_keys` | `tuple[str, ...]` |
| 11 | `advisory_authorizations` | `tuple[AdvisoryAuthorization, ...]` |
| 12 | `available_worker_kinds` | `tuple[WorkerKind, ...]` |
| 13 | `worker_slot_evidence_digest` | `str` |
| 14 | `scheduler_evidence_digest` | `str` |
| 15 | `content_digest` | `str` |

The projection converts field-for-field into the existing `AdmissionContext`.
An empty conflict tuple means the authoritative source proved no active
conflict; absence, unreadable evidence, permission denial, stale generation,
or an unknown value fails closed. `available_worker_kinds` contains only kinds
proved available by current slot evidence; it is never the enum's full set by
default.

### 2.2 `DockyardTaskAdmissionProgress` — exactly 25 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `progress_id` | `str` |
| 3 | `project_id` | `str` |
| 4 | `plan_id` | `str` |
| 5 | `plan_revision` | `int` |
| 6 | `plan_content_digest` | `str` |
| 7 | `dispatch_authorization_content_digest` | `str` |
| 8 | `task_id` | `str` |
| 9 | `revision` | `int` |
| 10 | `dependency_task_ids` | `tuple[str, ...]` |
| 11 | `dependency_evidence_digest` | `str` |
| 12 | `context_snapshot_id` | `str` |
| 13 | `context_snapshot_content_digest` | `str` |
| 14 | `preparation_id` | `str` |
| 15 | `preparation_receipt_id` | `str | None` |
| 16 | `handoff_id` | `str` |
| 17 | `handoff_receipt_id` | `str | None` |
| 18 | `expected_task_attempt` | `int` |
| 19 | `new_attempt` | `int` |
| 20 | `phase` | `DockyardTaskAdmissionPhase` |
| 21 | `outcome` | `DockyardTaskAdmissionOutcome` |
| 22 | `created_at` | `str` |
| 23 | `updated_at` | `str` |
| 24 | `finalized_at` | `str | None` |
| 25 | `content_digest` | `str` |

The attempt pair is exact and bounded: `new_attempt == expected_task_attempt +
1`, `0 <= expected_task_attempt <= 2`, and `1 <= new_attempt <= 3`. Attempt
four is rejected before context/progress persistence or downstream calls.

### 2.3 Exact enums

```text
MATERIALIZED
DEPENDENCIES_READY
PREPARED
HANDOFF_STARTED
ADMITTED
FINALIZED
```

```text
RESERVED
WAITING_DEPENDENCIES
READY
PREPARED
ADMITTED
FINALIZED
RECOVERY_REQUIRED
REJECTED
```

Phases are forward-only. A later observation never rewinds, reopens, or
overwrites an earlier generation.

## 3. Durable paths and replay

```text
.agentdesk/runtime/dockyard-admission/context/<snapshot_id>.yaml
.agentdesk/runtime/dockyard-admission/progress/<progress_id>.yaml
```

Both use canonical UTF-8 YAML, LF, one final LF, exact field order, atomic
same-directory replacement, and `sha256:` content digests excluding only the
digest field. Same identity plus identical bytes is byte-exact replay. Same
identity plus different bytes is a typed conflict. Two concurrent callers have
one winner and one identical replay or typed conflict; they never create two
preparation, handoff, queue, dispatch, event, or Worker identities.

The progress digest binds the exact confirmed plan-approval authorization.
Changing its confirmation or approval-operation identity is divergent replay,
including after finalization.

## 4. Dependency rule

A task with no dependencies is ready. A task with dependencies becomes ready
only when every exact `(task_id, revision)` has canonical state `integrated`,
an immutable `CHANGE_INTEGRATED` event with matching dispatch identity, and a
matching `integrated_commit`. Missing, accepted-only, blocked, cancelled,
superseded, stale-revision, divergent-event, or unreadable evidence yields
`WAITING_DEPENDENCIES` or fail-closed; it never dispatches.

The ordered dependency IDs come only from the durable `DockyardPlanTask`.
Task-card prose, UI state, queue order, timestamps, exception text, provider
output, and in-memory booleans are not dependency evidence.

## 5. Operation and lock order

1. validate materialized plan/task identity, attempt bounds, and digests;
2. read canonical dependency evidence through StateProvider;
3. independently read Scheduler conflict/authorization evidence;
4. independently read WorkerSlotLease capacity evidence;
5. release every owner lock and persist/replay the context snapshot;
6. under only the Dockyard progress-store lock, reserve/replay one progress;
7. release the progress lock;
8. if dependencies are ready, call Admission Preparation outside all locks;
9. persist/replay `PREPARED`, release the progress lock;
10. persist `HANDOFF_STARTED`, release the progress lock, then call the
    materialization-admission handoff outside all locks;
11. bind exact downstream receipt identities as `ADMITTED`, release the
    progress lock, and, when a configured post-admission owner is present,
    call it outside every owner lock;
12. finalize progress only after the existing Worker owner has published ACK,
    delivery, finalizer, and handoff evidence. A finalized external-Worker
    receipt is byte-exact replay and does not require an active lease.

No canonical-state, Scheduler, lease, preparation, handoff, Worker, provider,
model/API/network, process, Git, or StateProvider call occurs while the
Dockyard context/progress store lock is held. Owner locks are never nested.

## 6. Crash and decision matrix

| Window | Required disposition |
|---|---|
| before materialized validation | reject or resume validation; zero writes |
| attempt 4 | reject before all writes and owner calls |
| dependency evidence missing | waiting; zero preparation/handoff calls |
| dependency accepted but not integrated | waiting; zero downstream calls |
| dependency event/state divergent | fail-closed |
| context source UNKNOWN/unreadable | fail-closed; no empty-default context |
| context projection interrupted | fail-closed |
| context snapshot committed, progress absent | resume exact reservation |
| progress reservation interrupted | fail-closed |
| `MATERIALIZED`, dependencies later integrate | advance once to `DEPENDENCIES_READY` |
| `DEPENDENCIES_READY`, before preparation | resume same preparation identity |
| preparation writes, progress lags | adopt exact finalized preparation receipt |
| preparation divergent | reject before handoff |
| `PREPARED`, before handoff-start marker | persist marker, then call outside lock |
| `HANDOFF_STARTED`, handoff receipt absent | return `RECOVERY_REQUIRED`; never blind-repeat |
| handoff finalized, progress lags | adopt exact handoff receipt |
| `ADMITTED`, before finalization | finalize only |
| valid `FINALIZED` | byte-exact replay/no-op |
| two identical callers | one durable identity; identical replay |
| divergent generation/context/attempt | one winner; loser rejects |
| stale plan revision or content digest | reject |
| ALIVE/UNKNOWN/DEAD evidence present | defer unchanged to existing owner; never reinterpret |

## 7. Status

| Capability | Status |
|---|---|
| Dockyard task admission composition contract | **Contract Current — TC-13.29l.5d.3** |
| Dockyard task admission composition runtime | **Current — TC-13.29l.5d.4** |
| Dockyard post-admission Worker composition | **Current — TC-13.29l.5e** |
| Dockyard local closed-loop E2E | **Blocked — TC-13.29l** |

Interfaces #22, #36, #38, #40, #41, and #42 retain their existing owners and
status. This contract does not make the complete Scheduler runtime Current.
