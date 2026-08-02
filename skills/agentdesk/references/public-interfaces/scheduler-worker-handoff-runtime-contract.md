# Scheduler-to-Worker Handoff Runtime Contract

Status: **Contract Current — TC-13.26a**. Production runtime is
**Target — TC-13.26b** and end-to-end verification is **Target — TC-13.26c**.

## 1. Boundary

`start_admitted_dispatch(request, providers)` consumes an already committed
AdmissionPlan, ScheduleReceipt, canonical `TASK_DISPATCHED`, WorkerSlotLease,
managed worktree, durable handoff pair, and DispatchProcessReceipt lifecycle.
It never performs Admission, creates a lease, emits a second canonical event,
changes attempt, or invents dispatch/process identity. Provider validation
finishes before the first handoff write or process start.

The runtime may call only the explicit WorkflowOrchestrator
`start_admitted_dispatch_execution` adoption entry. It must never call the
private dispatch cycle or an entry that acquires a lease or applies
`TASK_DISPATCHED`.

## 2. Public frozen types

Every public value below is `@dataclass(frozen=True, slots=True)`. Public
fields contain no `Any`, bare `dict`, `Mapping`, `list`, `set`, callback,
subprocess object, exception text, stdout, stderr, argv, environment, token,
or provider credential.

### 2.1 AdmittedDispatchStartRequest — exactly 19 fields

| # | field | type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `project_root` | `str` |
| 4 | `queue_id` | `str` |
| 5 | `receipt_id` | `str` |
| 6 | `task_id` | `str` |
| 7 | `revision` | `int` |
| 8 | `attempt` | `int` |
| 9 | `dispatch_id` | `str` |
| 10 | `dispatch_event_id` | `str` |
| 11 | `outbox_message_id` | `str` |
| 12 | `selection_generation` | `int` |
| 13 | `plan_digest` | `str` |
| 14 | `handoff_id` | `str` |
| 15 | `worktree_id` | `str` |
| 16 | `lease_id` | `str` |
| 17 | `lease_epoch` | `int` |
| 18 | `holder_instance_id` | `str` |
| 19 | `requested_at` | `str` |

### 2.2 AdmittedDispatchStartResult — exactly 12 fields

| # | field | type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `task_id` | `str` |
| 4 | `revision` | `int` |
| 5 | `attempt` | `int` |
| 6 | `dispatch_id` | `str` |
| 7 | `handoff_id` | `str` |
| 8 | `generation_id` | `str` |
| 9 | `phase` | `ScheduledDispatchHandoffPhase` |
| 10 | `outcome` | `AdmittedDispatchStartOutcome` |
| 11 | `process_receipt_phase` | `str` |
| 12 | `content_digest` | `str` |

`AdmittedDispatchStartOutcome` has exactly `STARTED`, `REPLAYED`,
`RECOVERY_REQUIRED`, `REFUSED`, and `FAIL_CLOSED`. Schema version is
`agentdesk.scheduler-worker-handoff-runtime/v1`. Attempt is exactly 1..3;
attempt 4 fails before any write, lease operation, canonical transition, or
process start.

## 3. Exact binding and order

Before mutation, the runtime durably rereads and exactly binds queue/receipt,
AdmissionPlan, canonical `TASK_DISPATCHED`, lease id/epoch/holder/worktree,
READY managed-worktree record, handoff/receipt, provider binding, task,
revision, attempt, dispatch/event/outbox identities, generation, model
selection, assessment, branch, base commit, and Git HEAD. Missing, stale,
divergent, malformed, symlink/reparse, permission, or `UNKNOWN` evidence fails
closed.

The order is: validate typed request; reread all evidence; validate all
providers; reserve or byte-exact replay handoff; prove every Store/state lock
released; adopt the existing dispatch; start the real supervisor/Worker;
advance `SUPERVISOR_STARTED`, `WORKER_STARTED`, and `ACKNOWLEDGED`; persist
finalizer metadata and advance `FINALIZING`; require the exact finalizer
tombstone and advance `FINALIZED`.

Supervisor and Worker PID, creation time, boot identity, process group and
generation come only from the real DispatchProcessReceipt. The Orchestrator
PID is never substituted.

## 4. Seven forward-only phases and replay

The exact order remains `ADMISSION_COMMITTED`, `HANDOFF_RESERVED`,
`SUPERVISOR_STARTED`, `WORKER_STARTED`, `ACKNOWLEDGED`, `FINALIZING`,
`FINALIZED`. No skip, rollback, overwrite, second handoff, or second process
generation is allowed. Identical bytes replay the existing result; divergent
identity or bytes are a typed conflict.

## 5. Crash and ownership matrix

| Window/evidence | Required result |
|---|---|
| before handoff reservation | resume reservation or typed reject |
| HANDOFF_RESERVED before supervisor receipt | resume once; concurrent starters have one winner |
| supervisor receipt exists before phase advance | adopt exact receipt; never start a second process |
| before ACK | ALIVE waits/adopts; DEAD requests recovery; UNKNOWN fails closed |
| after ACK before finalizer | adopt exact active execution |
| finalizer metadata before tombstone | resume finalization only |
| tombstone before FINALIZED | verify exact tombstone then advance |
| FINALIZED replay | byte-exact no-op |
| owner loss or late success | delegate to frozen owner-loss/canonical ownership rules |
| cancellation or supersession | Interface #22 remains the sole canonical owner |

No lock is held across Worker, heartbeat, provider, model, API, network, or
process operations. Runtime never derives liveness from exception text, PID
absence, lease expiry, stdout, or stderr. Release of lease/worktree remains
owned by their frozen lifecycle components.

## 6. Status boundary

- Handoff Runtime Contract: **Contract Current — TC-13.26a**.
- Handoff Runtime: **Target — TC-13.26b**.
- Handoff E2E verification: **Target — TC-13.26c**.
- Interface #22 is unchanged and remains the canonical cancellation,
  supersession, ACK, delivery, and finalization owner.
- Interface #36 gains only this contract; complete Scheduler runtime remains
  **Target**.
