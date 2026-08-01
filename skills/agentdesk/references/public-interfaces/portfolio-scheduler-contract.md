# PortfolioScheduler — Durable Admission Contract (Contract Current — TC-13.24a)

This document freezes the public contract for a future PortfolioScheduler.
TC-13.24a freezes types, durable evidence, deterministic admission order,
conflict-key semantics, lock boundaries, and crash recovery only.  It does
not implement a Scheduler, start a Worker, call a model, call a provider, or
perform network I/O.

## 1. Scope and current behavior

The current AgentDesk system is not a single-Worker architecture.  The
existing `WorkerSlotLease` contract provides eight stable slots, two for each
existing `WorkerKind`: `basic_agent`, `standard_agent`, `advanced_agent`, and
`expert_agent`.  Different worktrees and different available slots may be
dispatched concurrently by external callers.

There is currently no built-in queue and no PortfolioScheduler.  When the
requested `WorkerKind` has no available slot, the existing admission path
immediately raises `WorkerSlotCapacityError`; it does not enqueue or wait.
Each `ActiveDispatchExecution` has a private asyncio lock.  That lock is not
a cross-execution or cross-process global lock.

TC-13.24a's word **serial** means that one queue selection and reservation is
completed at a time.  It does not mean that the Worker lifetime is globally
serial or that the system can have only one active Worker.

The future Scheduler owns only this boundary:

```text
queued → selected → TASK_DISPATCHED admission
```

After `TASK_DISPATCHED`, ACK, Worker execution, delivery, acceptance,
integration, cancellation, supersession, retry, and owner-loss recovery
remain owned by `WorkflowOrchestrator` and
`ControlPlaneTransitionService`.  The Scheduler does not become a second
WorkflowOrchestrator state machine.

## 2. Independent concepts

### 2.1 BusinessPriority

`BusinessPriority` is an independent string enum with exactly these values and
this urgency order:

```text
P0, P1, P2, P3
```

It is not `TaskDifficulty`, `WorkerKind`, model tier, model price, provider,
or risk.  No value may be derived from another.

### 2.2 TaskDifficulty

The existing `basic | standard | advanced | expert` `TaskDifficulty` enum is
unchanged.  It may constrain model/WorkerKind capability, context/token
budget, and a future cost-aware fairness policy.  It is not a v1 scheduling
sort key and is never used to infer `BusinessPriority`.

### 2.3 Fairness dimensions

The following are separate fields and concepts:

- `business_priority` — business urgency;
- `enqueue_sequence` — durable FIFO identity;
- `aging_basis_at` — the timestamp from which aging promotion is calculated;
- `deadline` — not part of v1 and deferred with deadline-aware promotion.

The first three are frozen by this contract.  Deadline and deadline-aware
promotion require a later version.

## 3. Public value types

The public values are frozen/slotted and immutable.  Public annotations must
not contain `Any`, `dict`, `Mapping`, or mutable collections.  Collection
fields use tuples.

### 3.1 Schema and enums

The v1 schema version for both values is exactly:

```text
agentdesk.portfolio-scheduler/v1
```

`QueuePhase` has exactly four values:

```text
queued, selected, dispatched, retired
```

`ReceiptPhase` has exactly two values:

```text
selected, dispatched
```

`ConflictKeyClass` has exactly three values:

```text
hard_exclusive, advisory, unknown
```

`unknown` means fail-closed.  `ConflictKey` is a frozen/slotted value with
exactly three fields:

| Field | Type | Rule |
|---|---|---|
| `value` | `str` | Canonical non-empty key value; no raw prompt or provider text |
| `classification` | `ConflictKeyClass` | One of the three values above |
| `canonical` | `bool` | True only when validated by canonical evidence |

### 3.2 QueueEntry

`QueueEntry` is a frozen/slotted value with exactly these fifteen fields, in
this order:

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `queue_id` | `str` |
| 3 | `task_id` | `str` |
| 4 | `revision` | `int` |
| 5 | `enqueue_sequence` | `int` |
| 6 | `enqueued_at` | `str` |
| 7 | `business_priority` | `BusinessPriority` |
| 8 | `aging_basis_at` | `str` |
| 9 | `worker_kind_request` | `WorkerKind` |
| 10 | `assessment_id` | `str` |
| 11 | `state` | `QueuePhase` |
| 12 | `conflict_keys` | `tuple[ConflictKey, ...]` |
| 13 | `retry_budget_used` | `int` |
| 14 | `content_digest` | `str` |
| 15 | `selection_generation` | `int` |

Identity and validation rules:

- `queue_id` is a stable `Q-` identity; `assessment_id` is an `ASM-*`
  identity bound to the exact `task_id` and `revision`;
- `revision`, `enqueue_sequence`, and `selection_generation` are non-bool
  integers; sequence and generation are positive, while
  `retry_budget_used` is non-negative;
- `enqueued_at` and `aging_basis_at` are canonical UTC timestamps;
- `conflict_keys` is an ordered immutable tuple and is frozen before
  selection;
- `content_digest` is `sha256:` followed by 64 lowercase hexadecimal
  characters and binds the canonical QueueEntry bytes excluding the digest
  field;
- `state` moves forward only: `queued → selected → dispatched → retired`.

Crash recovery may restore a `selected` entry to `queued` only through the
named recovery operation `recover_selected_without_dispatch`.  That operation
invalidates the old receipt and increments `selection_generation`; it is not a
normal reverse phase transition.

### 3.3 ScheduleReceipt

`ScheduleReceipt` is a separate frozen/slotted value with exactly these
fourteen fields, in this order:

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `receipt_id` | `str` |
| 3 | `queue_id` | `str` |
| 4 | `task_id` | `str` |
| 5 | `revision` | `int` |
| 6 | `enqueue_sequence` | `int` |
| 7 | `selected_at` | `str` |
| 8 | `order_key` | `tuple[int, int, int, str]` |
| 9 | `selection_reason` | `str` |
| 10 | `worker_kind` | `WorkerKind` |
| 11 | `dispatch_event_id` | `str \| None` |
| 12 | `phase` | `ReceiptPhase` |
| 13 | `content_digest` | `str` |
| 14 | `selection_generation` | `int` |

`receipt_id` is the durable identity for the tuple
`(queue_id, enqueue_sequence, selection_generation)`.  A legal re-selection
of one QueueEntry increments the generation and may create another receipt.
For one generation, the same request must return the same byte-exact receipt;
a divergent replay is rejected.

Before `TASK_DISPATCHED`, a receipt has `phase=selected` and
`dispatch_event_id=null`; it is a revocable selection intention.  After the
canonical event is committed, the receipt may be advanced to
`phase=dispatched` with the exact `dispatch_event_id`.  The receipt never
copies or rejudges lease, ACK, Worker, delivery, acceptance, or integration
state.

## 4. Canonical evidence and durable store

The queue is an independent authoritative store, not a derived view of
`tasks.yaml`.  The v1 reserved paths are:

```text
docs/pm/portfolio-scheduler/sequence.yaml
docs/pm/portfolio-scheduler/queue.yaml
docs/pm/portfolio-scheduler/receipts/<receipt_id>.yaml
docs/pm/portfolio-scheduler/reservations/<queue_id>/g<selection_generation>.yaml
```

The store must durably retain the last-issued enqueue sequence, enqueue and
aging timestamps, queue phase, retry-budget usage, frozen conflict keys,
selection receipt, selection generation, reservation identity, and replay
identity.  `sequence.yaml` is the sequence authority: a crash must never
reconstruct `max(existing enqueue_sequence) + 1`.

All evidence uses canonical UTF-8 YAML: no BOM, LF line endings, one final LF,
no tabs, no anchors or aliases, no duplicate keys, exact key order, and
deterministic tuple/sequence order.  `content_digest` is `sha256:` plus 64
lowercase hex over the exact canonical bytes excluding that field.  Writes
use a same-directory unique temporary file, file flush/fsync, atomic replace,
and directory fsync.  An identical request is byte-exact replay; the same
identity with different bytes is a conflict.

Missing, damaged, duplicate, noncanonical, identity-mismatched, or
non-contiguous sequence evidence fails closed.  Recovery may read only the
queue, canonical task snapshot/events, and verified durable receipts.  Queue
phase must never be used to infer or overwrite canonical task state.

## 5. Selection strategy

The strategy boundary is:

```text
select_next(queue_snapshot, admission_context) -> ScheduleReceipt | None
```

`queue_snapshot` and `admission_context` are immutable verified inputs.  v1
does not freeze future weights.  Its deterministic order key is exactly:

```text
(business_priority_rank, aging_promotion_rank, enqueue_sequence, queue_id)
```

Lower `business_priority_rank` means more urgent (`P0` before `P1`, then
`P2`, then `P3`).  A higher aging promotion is ordered first by the frozen
normalization used to produce `aging_promotion_rank`.  `enqueue_sequence` is
the FIFO tie-break, and `queue_id` is the final stable tie-break.

The order key must not contain `TaskDifficulty`, model name, model tier,
provider, exception text, stdout, stderr, exit code, predicted changed paths,
or deadline.  The same snapshot and policy version must produce byte-exact
equal output.

Deadline-aware scheduling and any `max_concurrent > 1` selection policy are
not implemented in v1 and remain Target.

## 6. Conflict-key admission

- `hard_exclusive` conflicts apply only to currently active leases or
  executions; a queued task alone does not create a hard conflict.
- `advisory` conflicts may coexist, but forced admission requires explicit
  authorization attached to the admission evidence.
- `unknown` or unparseable keys fail closed.
- Caller-supplied contract/interface keys are untrusted and cannot become
  hard keys without canonical evidence cross-validation.
- Changed paths are post-execution advisory evidence only; they are never a
  pre-dispatch hard key.
- A `GLOBAL` conflict key is invalid and may not cover the entire Worker
  lifetime.

## 7. Lock and execution boundary

The only permitted acquisition order is:

```text
worker-slot lease coordination → state-transition lock → queue-store write
```

The existing lock contracts must be reviewed again by the runtime card before
implementation.  Selection and reservation are short critical sections.
The Scheduler must not start a Worker while holding the state-transition lock,
must not hold that lock across a Worker lifetime, and must not call
`run_dispatch_cycle()` while any global lock is held.  Worker execution remains
protected by lease epoch, heartbeat, and fencing.  Lock contention returns an
immediate failure or `None`; it never waits indefinitely.

`TASK_DISPATCHED` is the only dispatch fact boundary.  A Scheduler may create
selection and reservation evidence, but it may not manufacture a canonical
dispatch event or duplicate the Orchestrator state machine.

## 8. Crash recovery matrix

| Condition | Only permitted recovery |
|---|---|
| queued and not selected | Resume selection |
| selected with no `TASK_DISPATCHED` | Invalidate old receipt and requeue through the named recovery operation |
| receipt write interrupted | Fail closed or exact byte replay |
| reservation after slot coordination, before `TASK_DISPATCHED` | Reuse the durable reservation identity; never create a second identity |
| `TASK_DISPATCHED` committed | Retire QueueEntry; Orchestrator owns the lifecycle |
| canonical event committed but queue phase not advanced | Align to `dispatched`/`retired` from the canonical event |
| ACK not yet observed at Scheduler crash | Never dispatch again |
| Worker running at Scheduler crash | Worker execution continues under lease/fencing |
| receipt tombstone/phase boundary crash | Exact byte replay |
| two Schedulers choose one QueueEntry | CAS/lock leaves exactly one winner |
| stale queue generation/CAS | Reject the operation |
| PID reuse, boot-id change, or insufficient permission | `UNKNOWN` and fail closed |
| queue corruption or sequence discontinuity | Fail closed |

Recovery never promotes a queue phase over a canonical task state and never
uses an exception message as evidence.

## 9. Retry and unchanged interfaces

The Scheduler does not define a second attempt limit.  `retry_budget_used`
records only retry facts authorized by the existing retry contract.  The
Scheduler never infers retry from provider output or exception text.  Attempt
identity remains owned by the existing Orchestrator durable retry contract.
Attempt 3 is the maximum: an attempt-4 reservation or dispatch is forbidden.
Retry re-enqueue must reference the existing failure or recovery event
identity.

The following remain unchanged and Current: `TaskDifficulty`, `WorkerKind`,
`TransitionCAS`, `DispatchCAS`, existing `TaskEntry` fields, `DispatchRequest`,
`WorkerSlotLease`, the WorkflowOrchestrator delivery/acceptance/integration
state machine, and the ControlPlaneTransitionService canonical single-writer
boundary.  Any field or semantic change requires a versioned task card.

## 9.1 Durable Admission Plan Binding (Contract Current - TC-13.24b.2b.4a.2)

The selected queue reservation needs a separate, immutable admission-plan
identity.  `ScheduleReceipt` remains unchanged and is not expanded to carry
dispatch or provider data.  The plan binding closes the gap between a
selected reservation and the later lease/transition boundary: a restarted
Scheduler must not submit a divergent plan for the same queue generation.

The public value is named `AdmissionPlanReservation`.  It is a frozen,
slotted, typed value with exactly these twenty-eight fields, in this order:

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `queue_id` | `str` |
| 3 | `receipt_id` | `str` |
| 4 | `task_id` | `str` |
| 5 | `revision` | `int` |
| 6 | `enqueue_sequence` | `int` |
| 7 | `selection_generation` | `int` |
| 8 | `worker_kind` | `WorkerKind` |
| 9 | `assessment_id` | `str` |
| 10 | `dispatch_id` | `str` |
| 11 | `event_id` | `str` |
| 12 | `outbox_message_id` | `str` |
| 13 | `role_id` | `str` |
| 14 | `task_card_path` | `str` |
| 15 | `task_card_commit` | `str` |
| 16 | `base_commit` | `str` |
| 17 | `branch` | `str` |
| 18 | `report_path` | `str` |
| 19 | `model_selection` | `ModelSelectionSnapshot` |
| 20 | `expected_task_state` | `str` |
| 21 | `expected_task_attempt` | `int` |
| 22 | `new_attempt` | `int` |
| 23 | `expected_snapshot_commit` | `str` |
| 24 | `policy_version` | `str` |
| 25 | `holder_instance_id` | `str` |
| 26 | `canonical_worktree` | `str` |
| 27 | `reserved_at` | `str` |
| 28 | `content_digest` | `str` |

`model_selection` is the existing ten-field frozen typed
`ModelSelectionSnapshot`; it is never a `dict` or `Mapping`.  No public plan
field may use `Any`, `object`, `dict`, `Mapping`, a mutable collection,
callback/factory, provider runtime object, exception text, stdout, stderr, or
exit code.  `reserved_at` is the first reservation timestamp only.  It is not
plan identity and must not change during byte-exact replay; an operation's
`now` value is not durable plan identity.

The sole v1 durable path is:

```text
docs/pm/portfolio-scheduler/admission-plans/<queue_id>/g<selection_generation>.yaml
```

The path components must equal the embedded `queue_id` and
`selection_generation`.  A generation has at most one plan reservation.
Missing, symlink/reparse, non-regular, extra, malformed, out-of-order, or
digest-inconsistent evidence fails closed.  v1 never scans a directory to
select a "latest" plan and never deletes or overwrites an existing plan.

Plan bytes use the existing PortfolioScheduler canonical YAML rules: UTF-8
without BOM, LF line endings, one final LF, no tabs, anchors, aliases, or
duplicate keys, exact field order, and deterministic tuple order.  The
`content_digest` is SHA-256 over the canonical bytes with the digest field
excluded.  Identical path and bytes are byte-exact replay.  The same
queue/generation with any differing field is a typed divergent replay and is
rejected before lease acquisition, canonical transition, or Scheduler-phase
write.  Digest construction must not use `str()` or `repr()`.

The selection-to-admission write order is frozen:

1. Read a validated queue snapshot and run the existing pure policy selection.
2. Under the queue-store lock, read the current plan, receipt, and reservation.
3. Write or byte-exact replay the `AdmissionPlanReservation`.
4. Write or byte-exact replay the `ScheduleReceipt`.
5. Advance `QueueEntry` from `queued` to `selected`.
6. Release the queue-store lock.
7. Re-read and validate the plan reservation byte-exactly against the
   in-memory plan.
8. Only then acquire `WorkerSlotLease`.
9. Only after the lease succeeds may the existing canonical
   `TASK_DISPATCHED` transition be attempted.

The queue-store lock is never held while acquiring a lease or calling
`ControlPlaneTransitionService`.  The plan is revalidated immediately before
lease acquisition, so a divergent restart is rejected with zero lease calls.

The frozen crash boundaries are:

| Boundary | Required outcome |
|---|---|
| Before plan write | Resume selection; no plan identity exists |
| After plan write, before receipt | Reuse the exact plan and complete the receipt; never create a second plan |
| After receipt, before queue `selected` | Reuse the plan and receipt, then advance the queue by CAS |
| After queue `selected`, before lease | Re-read and validate the exact plan; reject stale or divergent identity |
| After lease, before `TASK_DISPATCHED` | Use existing lease release/fencing semantics; do not synthesize an event |
| After canonical event, before Scheduler evidence alignment | Byte-exact replay and canonical-event alignment; never repeat dispatch |
| Missing plan | `RECOVERY_REQUIRED` or typed fail-closed rejection; admission is forbidden |
| Divergent plan | Typed conflict with zero lease acquisition |
| Two Schedulers on one generation | One durable plan winner; the other receives a typed conflict |
| Attempt 4 | Reject before plan reservation |

The three identity substitutions that are always rejected before lease
acquisition are `dispatch_id`, `event_id`, and `outbox_message_id`.
Changing any `ModelSelectionSnapshot` field, task-card/base/branch/report
field, expected Git HEAD, holder identity, canonical worktree, attempt, or
selection generation is also a typed divergent-plan rejection.  ApprovalGate
is not a plan-identity validator.  This contract freezes the evidence and
ordering only; durable plan Store/runtime remains Target - TC-13.24b.4a.3,
and selection-to-admission runtime remains Target with the known defect open.

AdmissionPlan durable binding contract: **Contract Current - TC-13.24b.2b.4a.2**.
Selection-to-Admission runtime: **Target - defect open**.
Durable plan Store/runtime: **Target - TC-13.24b.4a.3**.
Recovery event identity repair status is unchanged by this contract card.

## 10. Status and task split

- PortfolioScheduler durable admission contract: **Contract Current — TC-13.24a**.
- PortfolioScheduler durable evidence store: **Current — TC-13.24b.1**.
- PortfolioScheduler deterministic selection policy: **Current — TC-13.24b.2a**.
- PortfolioScheduler admission reservation core: **Current —
  TC-13.24b.2b.1**.
- PortfolioScheduler crash reconciliation decision core: **Current —
  TC-13.24b.2b.2**.
- Admission orchestration/runtime wiring: **Target — TC-13.24b.2b.4**.
- Complete PortfolioScheduler runtime and Worker startup/dispatch integration:
  **Target**.
- WorktreeLifecycleManager: **Target** and independent.
- TaskDifficulty dispatch/lifecycle wiring: **Current — TC-13.22b.3a**.
- Interface #22 core orchestration: unchanged, **Current — TC-13.18d.13b**;
  Codex/provider-429 extensions remain deferred.

This document stops before queue reservation/admission runtime, Worker
startup, model/provider/API calls, network access, and any change to the
existing Orchestrator or ControlPlane production modules.
