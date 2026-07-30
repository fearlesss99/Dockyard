# Durable Dispatch Supervisor Evidence — Frozen Contract

## 0. Status

Frozen Contract (Contract Current — TC-13.18d.12a-pre1).

This contract freezes the durable evidence protocol that a future
owner-loss / pre-ACK recovery path must consult before it is ever
allowed to write `DISPATCH_FAILED` or start the next attempt.  The
contract is a direct consequence of the TC-13.18d.12a read-only
investigation, whose decisive conclusion is:

```text
Owner-loss recovery is not currently implementable fail-closed.
```

The investigation established that no creator identity, Worker PID,
process-group identity, incarnation token, or durable process receipt
is persisted anywhere in the current system.  `DispatchStarted` is
explicitly a "no PID" three-field receipt.  The only existing
`DISPATCH_FAILED` caller — `run_bounded_dispatch_retry` — requires
in-memory `_finalizer_metadata` from the same creator process and is
therefore unreachable after owner loss.

This card freezes the contract and contract-freeze tests only.  It does
**not** implement a supervisor, a process liveness probe, an
owner-loss recovery path, or any production runtime module.  Runtime
implementation is deferred to TC-13.18d.12a-pre2.

## 1. Purpose and Non-Goals

### 1.1 Purpose

Freeze a durable evidence protocol that lets a future Orchestrator
safely distinguish five mutually exclusive states for a dispatch that
was started by a creator process which may have disappeared:

- `creator alive`
- `creator lost but Worker alive`
- `creator lost and Worker dead`
- `cleanly finalized`
- `evidence unknown / inconsistent`

### 1.2 Non-Goals (explicit exclusions)

- No supervisor process is implemented by this card.
- No process liveness probe is implemented by this card.
- No `DISPATCH_FAILED` is written from canonical state alone by this
  card.
- No next attempt is started by this card.
- No real subprocess, process termination, model, API, or network call
  is exercised by this card's tests.
- This contract does **not** change the public shape of
  `StateSnapshot`.  Runtime process information must never be mixed
  into the existing business read-only view.
- Lease expiry alone is **not** proof of death and is **not** used as
  proof of death by this contract.

## 2. Supervisor Lifecycle Ordering — Core Principle

A future implementation must **not** let the current Orchestrator
start the provider Worker first and then retroactively write a PID
receipt.  That ordering leaves a "Worker already started but no
receipt" crash window that no later process can close.

To close that window, every dispatch must introduce a per-dispatch
supervisor lifecycle that persists its own identity **before** the
provider Worker is allowed to start:

```text
Orchestrator
  → start supervisor
  → supervisor durable-ready receipt
  → supervisor starts provider Worker
  → supervisor durable worker-start receipt
  → ACK may be written
```

The supervisor must durably persist its own identity before the Worker
is started.  The Worker may only be started after the supervisor
identity is durable.  This closes the "Worker started without
receipt" crash window.

## 3. Durable State Machine

### 3.1 Exact Phases

The durable receipt progresses through exactly five phases, in this
order:

```text
RESERVED
SUPERVISOR_READY
WORKER_STARTED
FINALIZING
FINALIZED
```

### 3.2 Allowed Transitions

Only the following forward transitions are permitted:

```text
RESERVED        → SUPERVISOR_READY
SUPERVISOR_READY → WORKER_STARTED
WORKER_STARTED  → FINALIZING
FINALIZING      → FINALIZED
```

### 3.3 Forbidden Transitions

Jumps, backward transitions, and overwrites that target any other
`generation_id` are forbidden.  Specifically:

- Skipping a phase is forbidden (no `RESERVED → WORKER_STARTED`).
- Backward transitions are forbidden (no `WORKER_STARTED →
  SUPERVISOR_READY`).
- A receipt for one generation may never be overwritten by a write
  targeting a different generation.
- A phase may only advance; it may never regress.

## 4. Receipt-to-Transition Coupling

The relationship between canonical control-plane transitions and
receipt phases is frozen as follows:

- `TASK_DISPATCHED` requires that at least a `RESERVED` receipt exists
  before the dispatch transition is applied.
- The provider Worker may only be started after the receipt has
  advanced to `SUPERVISOR_READY`.
- `DISPATCH_ACKNOWLEDGED` may only be written after the receipt has
  advanced to `WORKER_STARTED`.

This ordering guarantees that an ACK can never exist without a durable
worker-start receipt, and a worker-start receipt can never exist
without a durable supervisor-ready receipt.

## 5. DispatchProcessReceipt — Frozen Fields

`DispatchProcessReceipt` is frozen with at least the following fields:

```python
class DispatchProcessReceipt:  # frozen contract reference, not production
    schema_version: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    lease_epoch: int
    holder_instance_id: str
    generation_id: str
    platform: str
    boot_id: str
    phase: str
    creator_pid: int | None
    creator_creation_time: str | None
    supervisor_pid: int | None
    supervisor_creation_time: str | None
    worker_pid: int | None
    worker_creation_time: str | None
    worker_process_group: int | None
    written_at: str
```

Rules:

- A field that belongs to a phase not yet reached must be exactly
  `None`.  No field may carry a forged or placeholder value to satisfy
  a non-null constraint.
- `generation_id` is an identity value.  It is **not** an API key or
  authorization secret.  It must never be written into a Git-tracked
  canonical file and must never appear in an exception message.
- `boot_id` is a per-boot identity used to detect PID reuse across a
  system restart.  A PID that exists under a different `boot_id` is
  treated as a reused PID, not as the original process.

## 6. DispatchFinalizerTombstone — Frozen Fields

`DispatchFinalizerTombstone` is a separate durable record, frozen with
exactly the following 12 fields:

```python
class DispatchFinalizerTombstone:  # frozen contract reference, not production
    schema_version: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    generation_id: str
    winner: str
    worker_done: bool
    heartbeat_done: bool
    release_completed: bool
    failure_kind: str | None
    finalized_at: str
```

Rules:

- A `FINALIZED` tombstone may only be written when the durable receipt
  is in the `FINALIZING` phase with matching `generation_id`,
  `task_id`, `revision`, `attempt`, and `dispatch_id`.
- The tombstone must be written atomically; immediately afterward the
  receipt is advanced from `FINALIZING` to `FINALIZED` under the same
  store lock.
- The tombstone write must precede the receipt advance.  If the process
  crashes after the tombstone is written but before the receipt is
  advanced, a subsequent byte-exact replay must finish the receipt
  advance and must not treat the replay as a divergence or a
  cross-generation overwrite.
- A receipt already `FINALIZED` but missing its tombstone is
  fail-closed: no tombstone may be silently manufactured.
- A `FINALIZED` tombstone may only be written after the Worker has
  ended, the heartbeat has ended, and the release has completed.
- The absence of a tombstone does **not** prove death.

## 7. Process Liveness Probe — Frozen Interface

```python
class ProcessLiveness(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"
```

The probe must separately test three subjects: `creator`, `supervisor`,
and `Worker`.  Each subject is compared on all of: PID, process
creation time / incarnation, and `boot_id`.

Rules:

- If the PID does not exist, the subject is `DEAD`.
- If the PID exists and every incarnation signal (PID, creation time,
  `boot_id`) matches, the subject is `ALIVE`.
- If the PID has been reused, `boot_id` differs, or the probe lacks
  permission to inspect the process, the subject is `UNKNOWN`, unless
  the contract separately and explicitly proves the original process
  cannot possibly still be alive.
- Any parse, permission, or platform error resolves to `UNKNOWN`.
- `UNKNOWN` must never be downgraded to `DEAD`.

## 8. Owner-Loss Safety Criteria

A future owner-loss recovery path may only be eligible to write
`DISPATCH_FAILED` and start the next attempt when **all** of the
following hold simultaneously:

- The receipt schema matches `DispatchCAS` exactly.
- The `generation_id` matches exactly.
- `creator` is `DEAD`.
- `supervisor` is `DEAD`.
- `Worker` is `DEAD`.
- No clean `FINALIZED` tombstone exists, or the tombstone explicitly
  indicates the finalization did not complete.
- The lease has been fenced, but lease expiry alone is **not**
  sufficient.
- The canonical state is still `dispatched` or `in_progress`.
- `current_dispatch` matches exactly.
- No delivery, acceptance, or integration advanced evidence exists.

Any `ALIVE` or `UNKNOWN` result for any subject forbids both
`DISPATCH_FAILED` and the next attempt.

## 9. Process-Tree Boundary

The contract must separately record and prove the entire process tree,
not just the parent Worker PID:

- On POSIX, the process group / session identity is recorded and
  probed.
- On Windows, the process-tree / Job Object identity is recorded and
  probed.
- When the whole process tree cannot be proven dead, the result is
  `UNKNOWN`.

The contract must **not** probe only the parent Worker PID and then
assume all descendants are dead.

## 10. Storage Boundary

Receipts and tombstones must live under `.agentdesk/runtime/` and must
not enter Git-tracked canonical files.  The storage rules are:

- Atomic replace is required.
- The state lock or a dedicated, documented lock order is required.
- Symlinks and Windows reparse points are fail-closed.
- The path is fixed.
- The schema is exact.
- Temporary files are cleaned up.
- Error messages must not leak PID, path, `generation_id`, or command
  arguments.

This contract does not change the existing `StateSnapshot` public
shape.  A future `DispatchRecoveryEvidenceProvider` may be added as a
dedicated read-only boundary; runtime process information must not be
mixed into the existing business read-only view.

## 11. Crash-Window Matrix

For each window the contract must yield exactly one conclusion:
`safe recovery` or `UNKNOWN / fail-closed` (the `unless exact supervisor
identity probes ALIVE` escape is the only permitted relaxation, and only
for the `SUPERVISOR_READY → Worker starts` window).

| Window | Conclusion |
|--------|------------|
| Crash before `RESERVED` is written | safe recovery |
| Crash after `RESERVED`, before supervisor starts | UNKNOWN / fail-closed |
| Crash after supervisor starts, before `SUPERVISOR_READY` is written | UNKNOWN / fail-closed |
| Crash after `SUPERVISOR_READY`, before Worker starts | UNKNOWN / fail-closed unless exact supervisor identity probes ALIVE |
| Crash after Worker starts, before `WORKER_STARTED` is written | UNKNOWN / fail-closed |
| Crash after `WORKER_STARTED`, before ACK | UNKNOWN / fail-closed |
| Crash after ACK | UNKNOWN / fail-closed |
| Crash during finalizer phases | UNKNOWN / fail-closed |
| Receipt partial write | UNKNOWN / fail-closed |
| System restart | UNKNOWN / fail-closed |
| PID reuse | UNKNOWN / fail-closed |
| Insufficient permissions | UNKNOWN / fail-closed |

## 12. Status and Sequencing

- TC-13.18d.11a/b/c: Current.
- TC-13.18d.12a: investigation complete — owner-loss recovery is not
  currently implementable fail-closed.
- Durable dispatch supervisor evidence:
  `Current — TC-13.18d.12a-pre2.1` (real supervisor chain, durable
  receipt/tombstone store, and three-state liveness probing all
  implemented and covered by targeted tests).
- Windows Job Object process-tree containment:
  `Current — TC-13.18d.12a-pre2.2` (named Windows Job Object per
  dispatch generation with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`;
  supervisor assigned before `SUPERVISOR_READY`; Worker and descendants
  inherit the Job through parent-child association).
- Windows process-tree liveness evidence:
  `Current — TC-13.18d.12a-pre2.2` (typed `probe_dispatch_process_tree`
  producing strict ALIVE / DEAD / UNKNOWN from durable receipt and named
  Job Object evidence; never conflates permission failures with death).
- Public API boundary closure:
  `Current — TC-13.18d.12a-pre2.2.1` (raw Windows Job handle helpers
  removed from public `__all__`; private `_DispatchJobOwner` owns handle
  lifecycle; only `probe_dispatch_process_tree` is the public
  process-tree probe entry point).
- Owner-loss recovery: continues Target; must remain fail-closed until
  the above Current capabilities land and the §8 criteria are
  enforceable from durable evidence alone.
- Interface #22: Target.

This card does not flip any Current status except as recorded above.
Production implementation (TC-13.18d.12a-pre2) is generated only after
this contract passes.
