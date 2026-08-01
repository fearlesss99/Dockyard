# PortfolioScheduler Recovery Action Executor — Durable Contract

Contract Current — TC-13.24b.2b.4b.1

This document freezes the Scheduler-local Recovery Action Executor boundary.
It does not implement the executor runtime.  The executor consumes the
already frozen `RecoveryRequest`, `RecoveryDecision`, exact replay digest,
canonical `EventEntry.event_id` identity, and queue/receipt/generation CAS
evidence.  It does
not reclassify liveness, choose a recovery action, or infer a cause from
exception text.

## 1. Schema and public value rules

The schema version is exactly:

```text
agentdesk.portfolio-scheduler.recovery-execution/v1
```

Every public value in this contract is frozen, slotted, immutable, and typed.
Public annotations must not contain `Any`, `dict`, `Mapping`, or mutable
collections.  Collection values, when present, are tuples.  Free-form
exception text, provider output, PID data, lease expiry, and model/API
objects are not public evidence.

The executor accepts only a previously validated `RecoveryRequest` and its
corresponding `RecoveryDecision`.  It must recompute and compare the exact
decision replay digest, then compare the canonical event identity and all
queue/receipt/generation identities before applying an action.

## 2. Public types

### 2.1 RecoveryExecutionRequest

`RecoveryExecutionRequest` is frozen/slotted with exactly these fields, in
this order:

| # | Field | Type |
|---|---|---|
| 1 | `schema_version` | `str` |
| 2 | `execution_id` | `str` |
| 3 | `recovery_request` | `RecoveryRequest` |
| 4 | `recovery_decision` | `RecoveryDecision` |
| 5 | `decision_replay_digest` | `str` |
| 6 | `expected_queue_phase` | `QueuePhase` |
| 7 | `expected_receipt_phase` | `ReceiptPhase \| None` |
| 8 | `expected_selection_generation` | `int` |
| 9 | `expected_recovery_generation` | `int` |
| 10 | `expected_canonical_dispatch_event_id` | `str \| None` |
| 11 | `requested_at` | `str` |

The execution ID (`execution_id`) binds one execution reservation.
`recovery_request` and
`recovery_decision` must be the frozen values for the same queue, task,
revision, generation, action, reason, canonical event identity, and replay
digest.  `decision_replay_digest` must equal the digest in the decision and
the recomputed digest; otherwise execution fails closed.

### 2.2 RecoveryExecutionPhase

`RecoveryExecutionPhase` has exactly these values:

```text
RESERVED
VALIDATED
APPLYING
APPLIED
FINALIZED
```

The phase is forward-only.  Only the adjacent transitions
`RESERVED -> VALIDATED -> APPLYING -> APPLIED -> FINALIZED` are valid.  A
phase cannot be skipped, regressed, overwritten, or inferred from a file
timestamp.

### 2.3 RecoveryExecutionReceipt

`RecoveryExecutionReceipt` is frozen/slotted with exactly seventeen fields,
in this order:

| # | Field | Type |
|---|---|---|
| 1 | `schema_version` | `str` |
| 2 | `execution_id` | `str` |
| 3 | `queue_id` | `str` |
| 4 | `receipt_id` | `str \| None` |
| 5 | `task_id` | `str` |
| 6 | `revision` | `int` |
| 7 | `selection_generation` | `int` |
| 8 | `recovery_generation` | `int` |
| 9 | `recovery_action` | `RecoveryAction` |
| 10 | `recovery_reason` | `RecoveryReason` |
| 11 | `canonical_dispatch_event_id` | `str \| None` |
| 12 | `decision_replay_digest` | `str` |
| 13 | `phase` | `RecoveryExecutionPhase` |
| 14 | `reserved_at` | `str` |
| 15 | `applied_at` | `str \| None` |
| 16 | `finalized_at` | `str \| None` |
| 17 | `content_digest` | `str` |

`content_digest` is computed over the canonical receipt fields excluding
itself.  Receipt identity, all bound evidence, and phase bytes participate in
the digest.  A same-generation, same-content receipt is byte-exact replay;
any changed action, reason, digest, event identity, queue identity, task
identity, or generation is a typed conflict.

### 2.4 RecoveryExecutionOutcome

`RecoveryExecutionOutcome` is frozen/slotted with exactly these fields, in
this order:

| # | Field | Type |
|---|---|---|
| 1 | `schema_version` | `str` |
| 2 | `execution_id` | `str` |
| 3 | `phase` | `RecoveryExecutionPhase` |
| 4 | `result` | `str` |
| 5 | `recovery_action` | `RecoveryAction` |
| 6 | `receipt` | `RecoveryExecutionReceipt \| None` |

`result` is one of `NO_OP`, `APPLIED`, `REPLAYED`, `REJECTED`, or
`FAIL_CLOSED`.  The outcome never contains an untyped diagnostic payload.

## 3. Durable path and identity

The exact durable path is:

```text
docs/pm/portfolio-scheduler/recovery-actions/<queue_id>/g<recovery_generation>.yaml
```

The executor must not scan for the latest generation.  Queue ID,
selection-generation, recovery-generation, execution ID, receipt identity,
task/revision, action/reason, canonical event identity, and replay digest are
all exact bindings.  Same generation and same content must replay byte
exactly.  Divergent content is a typed conflict and is never overwritten.

## 4. Action mapping

| Recovery action | Executor behavior |
|---|---|
| `NO_OP` | Zero canonical and Scheduler evidence writes; return a typed no-op outcome. |
| `WAIT_FOR_EXECUTION_OWNER` | Zero writes; return a typed wait outcome. |
| `FAIL_CLOSED` | Zero writes; return a typed fail-closed outcome. |
| `REJECT` | Return a typed rejection; do not synthesize evidence. |
| `RESUME` | Do not modify canonical state; allow only later re-selection. |
| `REQUEUE_SELECTED` | Invoke only the existing selected-recovery operation. |
| `INVALIDATE_SELECTION` | Require verifiable receipt/tombstone evidence; otherwise fail closed without fabricated evidence. |
| `ADOPT_CANONICAL_DISPATCH` | Require the exact canonical `event_id`; align receipt and queue only in the Store-approved order. |
| `RETIRE_QUEUE_ENTRY` | Require canonical dispatch evidence; never infer it from Scheduler phase alone. |

The executor must never write canonical task, event, or outbox files.  The
executor must never acquire or clean a `WorkerSlotLease`, start a Worker, call
Admission or Policy, call owner-loss recovery, or classify by exception text,
provider, PID, or lease expiry.  It does not call WorkflowOrchestrator,
Worker, model, API, network, or provider code.

## 5. Atomicity and lock boundary

The only permitted execution sequence is:

1. Read a validated canonical snapshot without a lock.
2. Read the frozen `RecoveryRequest` and `RecoveryDecision`.
3. Recompute the decision and verify the byte-exact replay digest.
4. Acquire the queue-store lock and re-read queue, receipt, and execution receipt.
5. CAS-reserve the execution identity.
6. Apply exactly one Scheduler-local action.
7. Advance the execution receipt.
8. Release the queue-store lock.

WorkflowOrchestrator, Worker, model, API, network, and provider calls are
forbidden while the queue-store lock is held.  The lock is never held across
Worker lifetime or Worker startup because the executor does not own either.

## 6. Crash matrix

| Crash window | Required replay or decision |
|---|---|
| Decision created before executor reservation | Resume with the same execution identity or typed conflict. |
| Reservation write interrupted | Byte-exact replay or fail closed; never overwrite divergent bytes. |
| `RESERVED` before action | Revalidate all evidence, then resume or fail closed. |
| Receipt advanced while queue is not advanced | Re-read and CAS the same local action; divergent identity fails closed. |
| Queue advanced while execution receipt is not `APPLIED` | Replay the same action identity; never create a second action. |
| `APPLIED` before `FINALIZED` | Finalize the same receipt after exact validation. |
| Finalized replay | Return `REPLAYED` with no duplicate write or action. |
| Canonical event changed | Typed identity conflict or fail-closed outcome; never adopt a replacement event. |
| Stale generation | Typed stale-generation rejection with zero action. |
| Concurrent executor | Exactly one durable execution winner; other callers receive typed conflict or byte-exact replay. |
| Attempt 4 | Reject before reservation with zero plan, lease, event, or executor mutation. |
| Missing or divergent evidence | Typed rejection or fail-closed outcome; never infer or fabricate evidence. |

Each crash window has only one permitted class of result: resume the same
identity, byte-exact replay, no-op, typed rejection, or fail-closed.  No
fallback action is selected from timing, exception text, process state, or
lease expiry.

## 7. Status and non-goals

- Recovery Action Executor Contract: **Contract Current — TC-13.24b.2b.4b.1**.
- Recovery Action Executor Runtime: **Target — TC-13.24b.2b.4b.2**.
- Worker startup/complete Scheduler runtime: **Target**.
- Interface #22: unchanged, **Current — TC-13.18d.13b**.
- Interface #36: contract and previously verified Scheduler cores remain
  Current; Recovery Action Executor Runtime and complete Scheduler runtime
  remain Target.

This contract does not implement a production executor runtime or add
recovery action side
effects, Worker lifecycle behavior, or WorkflowOrchestrator wiring.
