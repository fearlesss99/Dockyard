# PortfolioScheduler — Scheduler-to-Worker Durable Handoff Contract (Contract Current — TC-13.24b.2b.4c.1)

This document freezes the durable handoff contract between the
PortfolioScheduler admission boundary and the WorkflowOrchestrator /
dispatch supervisor Worker startup boundary.  The Scheduler has completed
durable plan reservation, `WorkerSlotLease`, canonical `TASK_DISPATCHED`,
and scheduler evidence alignment before any handoff may begin.  This
contract defines how that same dispatch identity is safely handed to the
existing WorkflowOrchestrator / dispatch supervisor to start the Worker.

This contract does **not** implement the handoff runtime entry,
supervisor, Worker startup, a second `WorkerSlotLease`, a second
`TASK_DISPATCHED`, or a second Admission.  Runtime implementation is
deferred to a later task card.

## 1. Core Boundary

The PortfolioScheduler runtime owns only this boundary:

```text
queued → selected → TASK_DISPATCHED admission
```

After `TASK_DISPATCHED`, the Scheduler has produced:

- A durable `AdmissionPlanReservation` (28 fields, Contract Current —
  TC-13.24b.2b.4a.2);
- A `ScheduleReceipt` advanced to `phase=dispatched`;
- A canonical `TASK_DISPATCHED` event;
- A `WorkerSlotLease` (acquired by the Scheduler);
- Scheduler evidence alignment (queue advanced to `dispatched`).

The handoff freezes the boundary where this evidence package is verified
and then passed to the existing WorkflowOrchestrator /
`start_dispatch_cycle()` / dispatch supervisor to start the Worker.

**Forbidden second operations**: The handoff must never create a second
`WorkerSlotLease`, a second `TASK_DISPATCHED` event, a second
`dispatch_id`, a second `event_id`, a second `attempt`, or a second
Admission.  The existing `run_dispatch_cycle()` /
`start_dispatch_cycle()` must not be called directly if it would
re-create `TASK_DISPATCHED` or re-acquire a lease.

## 2. Target Handoff Entry

The frozen entry name (adjustable before implementation) is:

```text
start_admitted_dispatch(handoff, providers)
```

It must:

1. Accept an existing `ScheduledDispatchHandoff` carrying the exact
   dispatch identity, event, attempt, lease, and admission evidence;
2. Verify the canonical `TASK_DISPATCHED` event is committed;
3. Verify the durable `AdmissionPlanReservation` matches the handoff
   identity exactly;
4. Verify the `ScheduleReceipt` is `phase=dispatched` with matching
   `dispatch_event_id`;
5. Verify the `WorkerSlotLease` epoch and holder identity;
6. **Not** re-execute Admission;
7. Release all global, state, and store locks before starting the
   supervisor or Worker;
8. Start the supervisor / Worker through the existing
   WorkflowOrchestrator dispatch-supervisor chain.

This contract does not implement `start_admitted_dispatch`.

## 3. Public Value Types

All public values are frozen, slotted, and typed.  No public field may
use `Any`, `dict`, `Mapping`, a mutable collection, `argv`, `env`,
`prompt`, `stdout`, `stderr`, callback, factory, provider runtime object,
exception text, or exit code.

### 3.1 Schema Version

The v1 schema version for all handoff values is exactly:

```text
agentdesk.portfolio-scheduler-worker-handoff/v1
```

### 3.2 ScheduledDispatchHandoffPhase

`ScheduledDispatchHandoffPhase` is a forward-only string enum with
exactly these seven values, in this exact order:

```text
ADMISSION_COMMITTED
HANDOFF_RESERVED
SUPERVISOR_STARTED
WORKER_STARTED
ACKNOWLEDGED
FINALIZING
FINALIZED
```

The only legal phase transitions are:

```text
ADMISSION_COMMITTED → HANDOFF_RESERVED
HANDOFF_RESERVED    → SUPERVISOR_STARTED
SUPERVISOR_STARTED  → WORKER_STARTED
WORKER_STARTED      → ACKNOWLEDGED
ACKNOWLEDGED        → FINALIZING
FINALIZING          → FINALIZED
```

Phases are forward-only.  Skipping a phase, moving backward, or
overwriting a phase for the same `handoff_id` is forbidden.  A write
with the same `handoff_id` and same phase is legal only as byte-exact
replay.  A phase or identity overwrite is rejected.

### 3.3 ScheduledDispatchHandoffOutcome

`ScheduledDispatchHandoffOutcome` is a string enum with exactly these
four values:

```text
HANDOFF_ACCEPTED
HANDOFF_REJECTED
HANDOFF_DEFERRED
HANDOFF_FAILED
```

- `HANDOFF_ACCEPTED`: all verifications passed, handoff reservation is
  durable, supervisor / Worker may start.
- `HANDOFF_REJECTED`: typed validation failure; zero handoff writes,
  zero supervisor, zero Worker.
- `HANDOFF_DEFERRED`: provider missing or capacity unavailable; zero
  handoff writes, zero supervisor, zero Worker, zero ACK.
- `HANDOFF_FAILED`: an unexpected error after reservation; handoff
  receipt may exist; no new Worker.

### 3.4 ScheduledDispatchHandoff — Exactly 25 Fields

`ScheduledDispatchHandoff` is a frozen, slotted value with exactly these
twenty-five fields, in this precise order:

| # | Field | Type | Rule |
|---:|---|---|---|
| 1 | `schema_version` | `str` | Exact `agentdesk.portfolio-scheduler-worker-handoff/v1` |
| 2 | `handoff_id` | `str` | Stable `HNDF-` identity |
| 3 | `queue_id` | `str` | Matches the sourcing `QueueEntry.queue_id` |
| 4 | `plan_digest` | `str` | `sha256:` + 64 lowercase hex; binds the exact `AdmissionPlanReservation` |
| 5 | `receipt_id` | `str` | Matches the sourcing `ScheduleReceipt.receipt_id` |
| 6 | `task_id` | `str` | `TC-*` identity from the canonical snapshot |
| 7 | `revision` | `int` | Non-bool positive integer; matches snapshot |
| 8 | `attempt` | `int` | Non-bool positive integer; 1..3 inclusive |
| 9 | `dispatch_id` | `str` | `DSP-*` identity from `AdmissionPlanReservation` |
| 10 | `dispatch_event_id` | `str` | `EVT-*` identity of the canonical `TASK_DISPATCHED` event |
| 11 | `outbox_message_id` | `str` | `MSG-*` identity from `AdmissionPlanReservation` |
| 12 | `lease_id` | `str` | The acquired `WorkerSlotLease` slot identity |
| 13 | `lease_epoch` | `int` | Non-bool positive integer; epoch at lease acquisition |
| 14 | `holder_instance_id` | `str` | Instance that acquired the lease |
| 15 | `worker_kind` | `WorkerKind` | From `AdmissionPlanReservation` |
| 16 | `assessment_id` | `str` | `ASM-*` identity bound to task and revision |
| 17 | `expected_snapshot_commit` | `str` | 40-char hex Git commit; from `AdmissionPlanReservation` |
| 18 | `provider_binding_id` | `str` | Provider identity from `ModelSelectionSnapshot.model_binding_id` |
| 19 | `phase` | `ScheduledDispatchHandoffPhase` | Current phase; starts at `ADMISSION_COMMITTED` |
| 20 | `reserved_at` | `str` | Canonical UTC timestamp of handoff reservation |
| 21 | `supervisor_started_at` | `str \| None` | `None` before `SUPERVISOR_STARTED` |
| 22 | `worker_started_at` | `str \| None` | `None` before `WORKER_STARTED` |
| 23 | `acknowledged_at` | `str \| None` | `None` before `ACKNOWLEDGED` |
| 24 | `finalized_at` | `str \| None` | `None` before `FINALIZED` |
| 25 | `content_digest` | `str` | `sha256:` + 64 lowercase hex over canonical bytes excluding this field |

Identity and validation rules:

- `handoff_id` is a stable `HNDF-*` identity unique per handoff
  generation;
- `plan_digest` binds the exact `AdmissionPlanReservation` bytes; a
  divergent plan is rejected before any handoff write;
- `attempt` must be in 1..3; attempt 4 is forbidden at handoff
  reservation;
- `lease_id` and `lease_epoch` must match the current acquired lease;
- `provider_binding_id` is the `model_binding_id` from the
  `ModelSelectionSnapshot` embedded in the `AdmissionPlanReservation`;
- Timestamp fields are canonical UTC ISO-8601 with microsecond precision;
  a field not yet reached must be exactly `None`;
- `content_digest` is SHA-256 over the canonical UTF-8 bytes of fields
  1–24, with field 25 excluded; it binds the exact handoff identity and
  must match on replay.

### 3.5 ScheduledDispatchHandoffReceipt — Exactly 11 Fields

`ScheduledDispatchHandoffReceipt` is a separate frozen, slotted value
generated after the handoff is durably reserved.  It establishes a 1:1
generation binding with `DispatchProcessReceipt`.  A given `dispatch_id`
has at most one handoff receipt and at most one dispatch process receipt
with matching `generation_id`.

| # | Field | Type | Rule |
|---:|---|---|---|
| 1 | `schema_version` | `str` | Exact `agentdesk.portfolio-scheduler-worker-handoff/v1` |
| 2 | `handoff_id` | `str` | Matches `ScheduledDispatchHandoff.handoff_id` |
| 3 | `dispatch_id` | `str` | Matches the handoff `dispatch_id` |
| 4 | `generation_id` | `str` | Matches the `DispatchProcessReceipt.generation_id` |
| 5 | `task_id` | `str` | From the handoff |
| 6 | `revision` | `int` | From the handoff |
| 7 | `attempt` | `int` | From the handoff |
| 8 | `phase` | `ScheduledDispatchHandoffPhase` | Current handoff phase |
| 9 | `binding_digest` | `str` | `sha256:` + 64 lowercase hex binding the handoff + dispatch process receipt identity |
| 10 | `written_at` | `str` | Canonical UTC timestamp of this receipt write |
| 11 | `content_digest` | `str` | `sha256:` + 64 lowercase hex over canonical bytes excluding this field |

The 1:1 generation binding rule:

- One `dispatch_id` → at most one `generation_id` → at most one
  `DispatchProcessReceipt` + at most one `ScheduledDispatchHandoffReceipt`;
- The `generation_id` in the handoff receipt must match the
  `generation_id` in the `DispatchProcessReceipt` exactly;
- `binding_digest` is SHA-256 over the concatenation of the canonical
  `handoff_id` bytes and `generation_id` bytes, binding both identities
  into one immutable generation key;
- A dispatch without a matching handoff receipt has no valid handoff;
- A dispatch without a matching dispatch process receipt has no valid
  supervisor chain;
- The handoff receipt does **not** copy, duplicate, or replace the
  `DispatchProcessReceipt`; it references the same generation and proves
  the handoff side of the generation binding.

## 4. Durable Path

The sole v1 durable path for handoff evidence is:

```text
docs/pm/portfolio-scheduler/worker-handoffs/<dispatch_id>.yaml
```

`<dispatch_id>` must equal the embedded `dispatch_id` field.  At most one
handoff file exists per dispatch.

Canonical YAML rules (inherited from PortfolioScheduler):

- UTF-8 without BOM;
- LF line endings, one final LF;
- No tabs, anchors, aliases, or duplicate keys;
- Exact key ordering matching the frozen field order;
- Deterministic tuple/sequence order.

Writes use a same-directory unique temporary file, file flush/fsync,
atomic replace, and directory fsync.  An identical request is byte-exact
replay; the same identity with different bytes is a conflict.

Missing, symlink/reparse, non-regular, extra, malformed, or
digest-inconsistent evidence fails closed.

## 5. Provider Boundary

The `providers` argument is an explicit non-empty typed mapping supplied
at the handoff entry.  It must be validated before any handoff phase
write or Worker start.

Frozen rules:

1. `providers` must be non-empty; empty mapping is rejected before any
   handoff write;
2. The type is a frozen mapping of `str` → typed provider descriptor
   (never `Any`, `dict`, or `Mapping`);
3. The required `provider_binding_id` must exist as a key in the mapping;
4. Missing, empty, wrong-type, or absent-selected-provider results in
   zero handoff writes, zero supervisor, zero Worker, zero ACK;
5. The handoff must not persist API keys, tokens, `argv`, `env`, or
   `prompt`;
6. The handoff must not infer retry eligibility, liveness
   classification, or failure classification from a provider name.

## 6. Atomic and Lock Order

The frozen handoff order is:

```text
1.  Read canonical snapshot (StateProvider.snapshot)
2.  Validate AdmissionPlanReservation, ScheduleReceipt, canonical
    TASK_DISPATCHED event, and WorkerSlotLease identity
3.  Validate providers mapping
4.  Acquire handoff store lock
5.  Under lock: read existing handoff evidence (if any)
6.  Under lock: reserve exact handoff (write ScheduledDispatchHandoff
    with phase=HANDOFF_RESERVED) or byte-exact replay
7.  Release handoff store lock
8.  Release all other store/state locks
9.  Start supervisor
10. Advance phase to SUPERVISOR_STARTED (under store lock, released)
11. Start Worker
12. Advance phase to WORKER_STARTED (under store lock, released)
13. After ACK: advance phase to ACKNOWLEDGED
14. Advance to FINALIZING
15. After finalizer/tombstone: advance to FINALIZED
```

The following must **never** be called inside any lock:

- `WorkerAdapter.run_worker()`;
- Starting a process or subprocess;
- Calling a model or provider;
- Waiting for heartbeat or completion;
- `asyncio.sleep()` or any blocking wait.

Worker execution remains protected by lease epoch, heartbeat, and
fencing.  Lock contention returns an immediate failure or `None`; it
never waits indefinitely.

## 7. Crash Recovery Matrix

Each window has exactly one required disposition: `resume`,
`byte-exact replay`, `no-op`, `reject`, or `fail-closed`.

| # | Crash or race window | Disposition |
|---:|---|---|
| 1 | Before Admission committed | Resume Scheduler tick; no handoff identity exists |
| 2 | After TASK_DISPATCHED, before handoff reservation | Resume: validate and reserve the handoff |
| 3 | Handoff reservation write interrupted | Fail-closed or byte-exact replay |
| 4 | After HANDOFF_RESERVED, before supervisor start | Resume: advance from the exact reserved identity; never create a second handoff |
| 5 | Supervisor started, receipt not advanced to SUPERVISOR_STARTED | Resume: byte-exact replay of the phase advance |
| 6 | Worker started, phase not yet WORKER_STARTED | Resume: byte-exact replay of the phase advance |
| 7 | After ACK, phase not yet ACKNOWLEDGED | Resume: byte-exact replay of ACKNOWLEDGED |
| 8 | Before finalizer | Resume: continue finalization |
| 9 | After finalizer, before FINALIZED | Byte-exact replay of FINALIZED phase advance |
| 10 | Tombstone and FINALIZED separated by crash | Fail-closed until both durable records agree |
| 11 | Concurrent handoff (two callers, same dispatch) | Exactly one winner by store-lock CAS; loser receives typed conflict |
| 12 | Stale generation | Reject; zero writes |
| 13 | PID reuse / boot-id change | Fail-closed (UNKNOWN is never downgraded) |
| 14 | Provider missing | Reject before any handoff write |
| 15 | Attempt 4 | Reject before reservation |

`resume` means continue with the exact durably reserved identity at the
permitted phase; never allocate a new identity.  `fail-closed` performs
zero new writes, zero supervisor, zero Worker.

## 8. Invariant Interfaces

The following remain unchanged and Current:

- **Interface #22** core orchestration semantics (TC-13.18d.13b);
- **ControlPlaneTransitionService** is the canonical single-writer for
  all control-plane transitions;
- Existing bounded retry is not re-implemented by the Scheduler;
- Owner-loss recovery is not re-implemented by the handoff;
- `TaskDifficulty` does not enter the handoff start order;
- The complete Worker runtime remains **Target**.

The handoff freezes only a new entry point.  It does not modify
`DispatchRequest`, `DispatchCAS`, `WorkerSlotLease`,
`run_dispatch_cycle()`, `start_dispatch_cycle()`, or the
WorkflowOrchestrator delivery/acceptance/integration state machine.

## 9. Relationship to Existing Contracts

| Contract | Relationship |
|---|---|
| `AdmissionPlanReservation` (TC-13.24b.2b.4a.2) | Handoff reads and validates; `plan_digest` binds the exact plan bytes |
| `ScheduleReceipt` (TC-13.24a) | Handoff reads and validates `phase=dispatched`; does not copy or replace |
| `WorkerSlotLease` (TC-13.10c) | Scheduler-acquired lease is verified and reused; handoff never acquires a second lease |
| `DispatchProcessReceipt` (TC-13.18d.12a-pre1) | 1:1 generation binding via `ScheduledDispatchHandoffReceipt` |
| `DispatchFinalizerTombstone` (TC-13.18d.12a-pre1) | Finalizer/tombstone unchanged; handoff advances to FINALIZED after tombstone |
| `ControlPlaneTransitionService` (TC-13.11c) | Unchanged; canonical single-writer |
| `WorkflowOrchestrator` Interface #22 (TC-13.18d.13b) | Unchanged; handoff calls into existing dispatch-supervisor chain |
| `run_dispatch_cycle()` / `start_dispatch_cycle()` (TC-13.18b) | Not called directly if they re-create TASK_DISPATCHED or lease; handoff supplies pre-admitted dispatch |

## 10. Status and Task Split

| Capability | Status |
|---|---|
| PortfolioScheduler durable admission contract | **Contract Current — TC-13.24a** |
| PortfolioScheduler durable evidence store | **Current — TC-13.24b.1** |
| PortfolioScheduler deterministic selection policy | **Current — TC-13.24b.2a** |
| AdmissionPlan durable binding contract | **Contract Current — TC-13.24b.2b.4a.2** |
| Durable AdmissionPlan Store/runtime | **Current — TC-13.24b.2b.4a.3** |
| Selection-to-Admission runtime | **Current — TC-13.24b.2b.4a.4** |
| Recovery canonical event identity | **Current — TC-13.24b.2b.2c** |
| Recovery adversarial verification | **Verified — TC-13.24b.2b.2d** |
| Scheduler-to-Worker durable handoff contract | **Contract Current — TC-13.24b.2b.4c.1** |
| `start_admitted_dispatch` runtime implementation | **Target** |
| Complete handoff runtime | **Target** |
| Interface #22 core orchestration | **Current — TC-13.18d.13b** |
| Dispatch supervisor evidence | **Current — TC-13.18d.12a-pre2.1/2.2/2.2.1** |
| Owner-loss recovery | **Current — TC-13.18d.12c/12c.1/12c.2** |

This document stops before `start_admitted_dispatch` runtime
implementation, supervisor startup, Worker startup, model/provider/API
calls, network access, and any change to the existing Orchestrator or
ControlPlaneTransitionService production modules.
