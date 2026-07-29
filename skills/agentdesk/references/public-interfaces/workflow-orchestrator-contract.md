# WorkflowOrchestrator — Implementable Contract (Current — TC-13.18d.8; E2E Evidence — Current — TC-13.19j; Active Cancellation Contract — TC-13.18d.9a)

Interface #22 frozen contract.  TC-13.18b implements the production
WorkflowOrchestrator module.  TC-13.18c.1 extends it with
DELIVERY_SUBMITTED.  TC-13.18c.2 extends it with MAD audit,
DELIVERY_ACCEPTED, and optional CHANGE_INTEGRATED.
TC-13.18d.1 extends it with DELIVERY_RETURNED and TASK_REQUEUED.
TC-13.18d.2 extends it with TASK_BLOCKED (audit blocked → escalation).
TC-13.18d.3 extends it with BLOCKER_RESOLVED (escalation resume → single redispatch).
TC-13.18d.4 extends it with INTEGRATION_FAILED (accepted → blocked for external integration failure).
TC-13.18d.5 extends it with BLOCKER_RESCOPED (expert blocked task rescope to draft).
TC-13.18d.6 extends it with BLOCKER_CANCELLED (expert blocked task cancellation).
TC-13.18d.7 extends it with TASK_CANCELLED (quiescent path — no active dispatch).
TC-13.18d.8 extends it with TASK_SUPERSEDED (quiescent path — no active dispatch).
TC-13.18d.9a freezes the active-dispatch cancellation contract.
Production implementation is TC-13.18d.9b.

## Status

**Current** as of TC-13.18d.8; E2E evidence program closed as of TC-13.19j.
Active-dispatch cancellation contract frozen as of TC-13.18d.9a; production
implementation is TC-13.18d.9b (Runtime Target).
TASK_DISPATCHED → heartbeat + run_worker →
DISPATCH_ACKNOWLEDGED → decode_worker_result →
require_delivery_receipt → DELIVERY_SUBMITTED → stop heartbeat → release
→ result), delivery remediation (audit fail → acquire remediation lease →
DELIVERY_RETURNED → release lease → TASK_REQUEUED (lease=None) →
DeliveryRemediationResult), blocked audit escalation (audit blocked →
evaluate_escalation → TASK_BLOCKED (lease=None) → BlockedAuditResult),
escalated single redispatch (BLOCKER_RESOLVED (lease=None) → single
run_dispatch_cycle with next_worker_kind → EscalatedRedispatchResult),
integration failure recording (INTEGRATION_FAILED (lease=None) →
IntegrationFailureResult),
and expert blocked task rescope (BLOCKER_RESCOPED (lease=None) →
BlockedRescopeResult),
and expert blocked task cancellation (BLOCKER_CANCELLED (lease=None) →
BlockedCancellationResult),
and quiescent task cancellation (StateProvider.snapshot() → validate no
active dispatch → TASK_CANCELLED (lease=None) → TaskCancellationResult),
and quiescent task supersession (StateProvider.snapshot() → validate no
active dispatch → TASK_SUPERSEDED (lease=None) → TaskSupersessionResult)
are implemented and callable.  The E2E evidence program (TC-13.19a–j)
validates all Current paths.  Active-dispatch cancellation, active-dispatch
supersession, Codex decoding, Codex rate-limit classification, and
retry-loop fault recovery remain Target.  Active-dispatch cancellation
contract is frozen (Contract Current — TC-13.18d.9a); production
implementation is TC-13.18d.9b (Runtime Target).  See
`reports/tc-13.19-final-delivery-report.md` and `test_release_smoke.py`
class `TC1319jE2EProgramClosureTests`.  TC-13.20 (HTML Dashboard) is Target.

This document is the authoritative frozen specification for the
WorkflowOrchestrator public API, ownership boundaries, hard dependencies,
ACK semantics, clock/heartbeat model, ID/workspace ownership, audit strategy,
and exception hierarchy.

The ADR contract is in `001-mad-agentdesk-integration.md` §2.19;
this document expands per-interface details.

---

## 1. Purpose

`WorkflowOrchestrator` is a **pure orchestration facade** that connects all
existing Current services into a complete closed loop.  It owns zero canonical
state writes, zero lock primitives, and zero subprocess execution.  Every
authoritative operation delegates to an existing service.

---

## 2. Delegation Matrix

| Responsibility | Delegated to | Method |
|---------------|-------------|--------|
| Read task state | `StateProvider` | `snapshot()` |
| Compute budget | `ContextBudgetPolicy` | `compute_budget()` |
| Acquire / release / renew slot | `WorkerSlotLease` | `acquire_worker_slot` / `release_worker_slot` / `renew_worker_slot` |
| Execute Worker dispatch | `WorkerAdapter` | `run_worker()` |
| Write canonical state / events / outbox / acceptances | `ControlPlaneTransitionService` | `apply_transition()` |
| Check / grant / revoke approval | `ApprovalGate` | `check()` / `require()` / `write_grant()` / `write_revoke()` |
| Evaluate escalation | `EscalationService` | `evaluate_escalation()` |
| Invoke MAD deliberation | `MadGateway` | `run_gateway()` |
| Invoke MAD audit | `MadAuditGateway` | `run_audit_gateway()` |

---

## 3. Ownership Boundary

### 3.1 TransitionService-Exclusive Responsibilities

The **TransitionService** is the sole writer of canonical state.  It:

- Acquires `hold_worker_slot_fence()` internally when a `WorkerSlotLease` is
  supplied as the `lease` parameter.
- Acquires `.state-transition.lock` internally (project-level state lock).
- Calls `ApprovalGate.require()` internally for the three gated transitions
  inside the state lock:
  - `TASK_DISPATCHED`
  - `DELIVERY_ACCEPTED`
  - `CHANGE_INTEGRATED`

**Frozen rules for WorkflowOrchestrator:**

1. **Must not** call `hold_worker_slot_fence()` directly.
2. **Must not** acquire `.state-transition.lock` directly.
3. **Must not** call `ApprovalGate.require()` before or after transition
   requests for the three gated transitions.
4. **Must not** fabricate `approval_gate` GuardResult entries — the
   TransitionService constructs them from the actual ApprovalGate outcome
   (§2.15.13 of the ADR).

### 3.2 Orchestrator-Exclusive Responsibilities

WorkflowOrchestrator alone is responsible for:

1. Obtaining a read-only snapshot via `StateProvider.snapshot()`.
2. Calling `compute_budget()`.
3. Acquiring, renewing, and releasing `WorkerSlotLease` (direct calls to
   `acquire_worker_slot`, `renew_worker_slot`, `release_worker_slot`).
4. Calling `WorkerAdapter.run_worker()`.
5. Constructing typed `TransitionRequest` objects and passing them to
   `ControlPlaneTransitionService.apply_transition()` in the correct order.
6. Calling `MadAuditGateway.run_audit_gateway()`.
7. Calling `EscalationService.evaluate_escalation()`.
8. Bounded cleanup on failure paths (slot release, heartbeat cancellation).

### 3.3 What the Orchestrator Must Never Do

- Directly serialize or write any file under `docs/pm/` or `.agentdesk/runtime/`.
- Directly start CLI subprocesses (claude, codex, mad).
- Parse Claude JSON or Codex JSONL stdout.
- Create, remove, or switch Git worktrees.
- Call `git` commands.
- Access the network or handle API keys.
- Bypass audit or approval gates.

---

## 4. TC-13.9c Hard Dependency — Opaque Output Boundary

`WorkerResult.dispatch_result.stdout` and `.stderr` are opaque `bytes`.

TC-13.9c.1 (Claude 2.1.214 decoder) is now **Current**.  The
WorkflowOrchestrator must delegate output decoding to
``decode_worker_result()`` and ``require_delivery_receipt()`` from
``worker_output_decoder``.

**The WorkflowOrchestrator must never:**

| Forbidden action | Correct delegation |
|-----------------|-------------------|
| Parse Claude JSON or Codex JSONL | → `worker_output_decoder.decode_worker_result()` |
| Guess `implementation_commit` from stdout | → `WorkerOutput.implementation_commit` |
| Guess `report_commit` from stdout | → `WorkerOutput.report_commit` |
| Decode bytes to text and extract report content | → TC-13.9c |
| Use current Git HEAD as Worker commit | → Not a valid substitute |
| Silently decode with fallback character sets | → Fail-closed |

**Impact on TC-13.18 v1:**

- The orchestrator **can** complete: scheduling, lease acquisition, Worker
  execution, and result return.
- The orchestrator **can** complete ``DELIVERY_SUBMITTED`` via
  ``decode_worker_result()`` → ``require_delivery_receipt()`` for
  Claude/claudecode 2.1.214.
- Codex deliveries remain blocked until TC-13.9c.2.
- This dependency is **hard**: any path that claims ``DELIVERY_SUBMITTED``
  completion without TC-13.9c must document exactly which mechanism supplies
  the two commit SHAs.

---

## 5. DISPATCH_ACKNOWLEDGED — ACK Semantics

The true point at which a CLI subprocess has started and is ready to receive
input is observable via the `DispatcherAgentGateway.run_dispatch_observed()` API
added in TC-13.18b.2.  The Gateway calls a `DispatchStartedObserver` Protocol
callback after `asyncio.create_subprocess_exec()` succeeds and before
`process.communicate()` sends stdin.  The `WorkflowOrchestrator` holds an
internal `_AckObserver` that validates the `DispatchStarted` identity against
the `DispatchRequest` and applies `DISPATCH_ACKNOWLEDGED` under the same
`WorkerSlotLease` while the heartbeat is still active.

### Process-Start Receipt Timing

```text
create_subprocess_exec → process returned
    ↓
observer.on_dispatch_started(DispatchStarted(identity, provider, model_id))
    ↓  (observer validates identity, applies DISPATCH_ACKNOWLEDGED in same lease)
    ↓
observer returns (ACK is now committed to canonical state)
    ↓
process.communicate(input=stdin)  ← stdin is only sent AFTER ACK succeeds
```

`DispatchStarted` carries exactly three fields: `identity` (the original
`DispatchIdentity`), `provider` (the selected provider mapping key), and
`model_id` (the `selected_model_id` from the snapshot).  It carries no PID,
argv, env, workspace, prompt, stdin, secret, timestamp, or process handle.

### Decision — TC-13.18b.2

**DISPATCH_ACKNOWLEDGED → Current — TC-13.18b.2.**

- The `DISPATCH_ACKNOWLEDGED` transition is now produced automatically
  by the `WorkflowOrchestrator` via an internal `DispatchStartedObserver`
  wired into `run_worker_observed()` → `run_dispatch_observed()`.
- The `DispatchCycleRequest` carries a 6th field `acknowledge_transition_request`
  (a `TransitionRequest` with `event_type == "DISPATCH_ACKNOWLEDGED"` and
  `AcknowledgePayload`).
- `DispatchCycleResult` carries a 6th field `acknowledge_transition`
  (the `TransitionResult` from the ACK write).
- The ACK is applied under the same `WorkerSlotLease` and while the
  heartbeat is active.

### Prohibited Fake ACK Patterns

| Pattern | Why forbidden |
|---------|--------------|
| Claim CLI started before calling `run_worker()` | No evidence subprocess launched |
| Transition to `in_progress` after Worker completed | Semantically wrong — Worker is done |
| Create asyncio task then immediately ACK | Task creation ≠ process start |

---

## 6. Clock and Heartbeat

### 6.1 WorkflowClock Protocol

```python
from typing import Protocol
from datetime import datetime


class WorkflowClock(Protocol):
    def now(self) -> datetime:
        """Return a timezone-aware UTC datetime."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the current task for *seconds*."""
        ...
```

### 6.2 Frozen Rules

1. `now()` returns timezone-aware UTC.  Naive or non-UTC datetimes are
   rejected by downstream services (§2.5.5, §2.14.4 of the ADR).
2. Heartbeat interval must not exceed 20 s
   (`WorkerSlotLease.MAX_HEARTBEAT_INTERVAL_SECONDS`).
3. A heartbeat background `asyncio.Task` must be started before `run_worker()`
   and cancelled (stopped) on Worker completion, failure, or cancellation.
4. Heartbeat failure (`renew_worker_slot` raises `WorkerSlotFencingError`)
   must cancel the dispatch and enter the cleanup path.  Subsequent
   transitions must **not** proceed with an expired lease.
5. Tests inject a fake clock; production injects a real asyncio clock.
6. A single frozen `now` value must **not** be reused across multiple
   time-dependent operations (acquire, dispatch, transition, release each
   get a fresh `now()` call).
7. `WorkflowOrchestrator` must **not** call `datetime.now()` or `time.sleep()`
   directly in production logic.

---

## 7. ID and Workspace Ownership

### 7.1 Caller-Supplied Identifiers

All authoritative identifiers in TC-13.18 v1 are **caller-supplied**:

| Identity | Pattern | Supplied by | Validated by |
|----------|--------|------------|-------------|
| `task_id` | `TC-*` | Caller | TransitionService (CAS) |
| `dispatch_id` | `DSP-*` | Caller | TransitionService (CAS) |
| `event_id` | `EVT-*` | Caller | TransitionService (uniqueness) |
| `outbox_message_id` | `MSG-*` | Caller | TransitionService (uniqueness) |
| `holder_instance_id` | freeform | Caller | WorkerSlotLease |
| `holder_dispatch_id` | freeform | Caller | WorkerSlotLease |

The WorkflowOrchestrator does **not** generate any of these.  It does **not**
hide ID generation behind private helpers, UUID calls, or timestamp
derivations.

### 7.2 Workspace

`workspace` is an existing absolute directory supplied by the caller.  The
WorkflowOrchestrator does **not**:

- Create or remove Git worktrees.
- Execute `git checkout`, `git worktree add`, or `git worktree remove`.
- Derive the workspace from environment variables.

Worktree lifecycle management is deferred to a future task card with a
separate interface contract.

---

## 8. MAD Audit — Fail-Closed

### 8.1 Mandatory — No Skip Flag

In TC-13.18 v1, MAD audit is **mandatory** in the automated acceptance path.
The skip-audit policy flag **must not** exist:

```python
# FORBIDDEN:
skip_audit: bool
```

### 8.2 Verdict Routing

| Condition | Action |
|-----------|--------|
| `verdict == "pass"` | Proceed to acceptance request |
| `verdict == "fail"` | Reject automatic acceptance → enter return/escalation |
| `verdict == "blocked"` | Pause for PM / user decision |
| Gateway exception | Must **not** be treated as `"pass"`; enters blocked/escalation |
| No audit result (audit not invoked) | Automatic acceptance **not** permitted |

### 8.3 Future Exemption Policy

Future exemption from mandatory audit must use a separate, typed, auditable
policy evidence object — never a bare boolean flag.  Example of acceptable
future API:

```python
# Future (not in v1):
audit_policy: AuditPolicy  # typed dataclass with evidence_ref
```

---

## 9. Transition Coverage

The WorkflowOrchestrator must be aware of **all 15** transition types
defined by `ControlPlaneTransitionService` (§2.14.8 of the ADR):

| # | Event type | Covered by task card |
|---|-----------|---------------------|
| 1 | `TASK_SPECIFIED` | TC-13.18b (PM gate) |
| 2 | `TASK_DISPATCHED` | TC-13.18b (dispatch path) |
| 3 | `DISPATCH_ACKNOWLEDGED` | Current — TC-13.18b.2 (§5 of this doc) |
| 4 | `DELIVERY_SUBMITTED` | Current — TC-13.18c.1 (§13 of this doc) |
| 5 | `DELIVERY_ACCEPTED` | Current — TC-13.18c.2 (acceptance path) |
| 6 | `DELIVERY_RETURNED` | Current — TC-13.18d.1 (return path) |
| 7 | `TASK_REQUEUED` | Current — TC-13.18d.1 (requeue path) |
| 8 | `CHANGE_INTEGRATED` | Current — TC-13.18c.2 (integration path) |
| 9 | `INTEGRATION_FAILED` | Current — TC-13.18d.4 (blocked path) |
| 10 | `TASK_BLOCKED` | Current — TC-13.18d.2 (blocked path) |
| 11 | `BLOCKER_RESOLVED` | Current — TC-13.18d.3 (escalation resume → ready → single redispatch) |
| 12 | `BLOCKER_RESCOPED` | Current — TC-13.18d.5 (expert blocked → draft rescope) |
| 13 | `BLOCKER_CANCELLED` | Current — TC-13.18d.6 (expert blocked → cancelled) |
| 14 | `TASK_CANCELLED` (quiescent path) | Current — TC-13.18d.7 |
| 15 | `TASK_CANCELLED` (active dispatch path) | Contract Current — TC-13.18d.9a / Runtime Target — TC-13.18d.9b |
| 16 | `TASK_SUPERSEDED` (quiescent path) | Current — TC-13.18d.8 |
| 17 | TASK_SUPERSEDED (active dispatch path) | Target |

The orchestrator does **not** duplicate `_TRANSITION_SPECS`.  It constructs
typed `TransitionRequest` objects and passes them to `apply_transition()`.

---

## 10. Exception Hierarchy

### 10.1 Underlying Exceptions Pass Through Unchanged

The following exception classes propagate **unchanged** through the
WorkflowOrchestrator boundary:

| Exception root | Module |
|---------------|--------|
| `WorkerSlotLeaseError` (all subclasses) | `worker_slot_lease` |
| `DispatchGatewayError` (all subclasses) | `dispatcher_gateway` |
| `ApprovalError` (all subclasses) | `approval_gate` |
| `ControlPlaneTransitionError` (all subclasses) | `control_plane_transition` |
| `GatewayError` (all subclasses) | `mad_gateway` / `mad_audit_gateway` |
| `StateProviderError` (all subclasses) | `state_provider` |

No parallel wrapping exception (`WorkflowSlotError`, `WorkflowDispatchError`,
`WorkflowAuditError`, etc.) is created.

### 10.2 Orchestrator-Specific Exceptions — Exactly Three

```text
WorkflowOrchestratorError                  (Exception)
├── WorkflowInputError                     — invalid argument types/values
├── WorkflowHeartbeatError                 — heartbeat renewal lost
└── WorkflowInvariantError                 — internal precondition violated
```

**No other** exception types exist at the orchestrator level.

### 10.3 Error Message Safety

Exception messages must **never** contain:

- The task prompt
- Raw `stdout` or `stderr` bytes
- Workspace paths
- `holder_instance_id` / `holder_dispatch_id`
- `canonical_worktree`
- `dispatch_id` / `task_id`
- User-generated content (delivery rationale, criteria evidence, etc.)
- Secrets or API keys

Exception messages **may** contain: `type(x).__name__`, field names, and
the exception class name.

---

## 11. Public API Sketch (Current — TC-13.18c.1)

```python
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol

from dispatcher_gateway import AgentCliProvider, ModelSelectionSnapshot, DispatchRequest
from core_types import WorkerKind, TaskDifficulty
from worker_slot_lease import WorkerSlotLease
from worker_adapter import WorkerResult
from worker_output_decoder import WorkerOutput, DeliveryReceipt
from control_plane_transition import (
    TransitionRequest, TransitionResult, TransitionEventContext,
    TransitionCAS, DispatchCAS,
    DispatchPayload, AcknowledgePayload, DeliverySubmittedPayload,
)
from escalation_service import EscalationAction, EscalationDecision
from approval_gate import ApprovalScope, ApprovalSubject
from mad_gateway import MadGatewayConfig
from mad_audit_gateway import MadAuditGatewayResult
from state_provider import StateSnapshot


class WorkflowClock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class DispatchCycleRequest:
    """Immutable input for a single dispatch + delivery cycle — nine fields."""
    dispatch_request: DispatchRequest
    dispatch_transition_request: TransitionRequest
    acknowledge_transition_request: TransitionRequest
    delivery_event_id: str
    delivery_event_context: TransitionEventContext
    provider_cli_version: str
    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    holder_instance_id: str


@dataclass(frozen=True, slots=True)
class DispatchCycleResult:
    """Immutable result — nine fields."""
    worker_result: WorkerResult
    worker_output: WorkerOutput
    delivery_receipt: DeliveryReceipt
    dispatch_transition: TransitionResult
    acknowledge_transition: TransitionResult
    delivery_transition: TransitionResult
    slot_id: str
    lease_epoch: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class AcceptanceCycleRequest:
    """Immutable input for an independent acceptance cycle — exactly six fields."""
    dispatch_cycle_result: DispatchCycleResult
    audit_input: MadAuditGatewayInput
    acceptance_transition_request: TransitionRequest
    integration_transition_request: TransitionRequest | None
    worker_kind: WorkerKind
    holder_instance_id: str


@dataclass(frozen=True, slots=True)
class AcceptanceCycleResult:
    """Immutable result of an acceptance cycle — exactly four fields."""
    task_id: str
    audit_result: MadAuditGatewayResult
    accept_transition: TransitionResult | None
    integrate_transition: TransitionResult | None


class WorkflowOrchestratorError(Exception):
    """Base for all WorkflowOrchestrator errors."""


class WorkflowInputError(WorkflowOrchestratorError):
    """Invalid argument types/values."""


class WorkflowHeartbeatError(WorkflowOrchestratorError):
    """Heartbeat renewal lost — lease expired or fence rejected."""


class WorkflowInvariantError(WorkflowOrchestratorError):
    """Internal precondition violated."""
```

---

## 12. Task-Card Split

| Card | Description | Depends on | Scope |
|------|------------|-----------|-------|
| **TC-13.18a** | This frozen contract | This ADR | Contract only |
| **TC-13.9c** | Typed WorkerOutput / DeliveryReceipt decoding | TC-13.9b + reliable output-schema evidence | Output decoding |
| **TC-13.18b** | Lease + heartbeat + Worker execution + bounded cleanup | TC-13.18a, TC-13.10c, TC-13.11c, TC-13.12d, TC-13.13b, TC-13.17b | Production |
| **TC-13.18c** | DeliverySubmitted + MAD audit + Acceptance + Integration | TC-13.18b, TC-13.16b | Production |
| **TC-13.18c.1** | DELIVERY_SUBMITTED via decode_worker_result + require_delivery_receipt | TC-13.18b.2, TC-13.9c.1 | Production — Current |
| **TC-13.18c.2** | MAD audit + Acceptance + Integration | TC-13.18c.1 | Current |
| **TC-13.18d.1** | DELIVERY_RETURNED + TASK_REQUEUED (fail remediation) | TC-13.18c.2 | Current |
| **TC-13.18d.2** | Blocked audit escalation (TASK_BLOCKED + EscalationDecision) | TC-13.18d.1 | Current |
| **TC-13.18d.4** | INTEGRATION_FAILED recording (accepted → blocked) | TC-13.18d.3 | Current |
| **TC-13.18d.5** | BLOCKER_RESCOPED (expert blocked → draft rescope) | TC-13.18d.2 | Current |
| **TC-13.18d.6** | BLOCKER_CANCELLED (expert blocked → cancelled) | TC-13.18d.2 | Current |
| **TC-13.18d-ext** | Retry loop, escalation replay, cancellation, fault recovery | TC-13.18d.3 | Target |
| **TC-13.18d.9a** | Active-dispatch cancellation contract freeze | TC-13.18d.7 | Contract Current |
| **TC-13.18d.9b** | Active-dispatch cancellation production implementation | TC-13.18d.9a | Runtime Target |
| **TC-13.9c.2** | Codex decoder | TC-13.9c.1 | Target |
| **TC-13.19** | Real E2E closed-loop tests | TC-13.18d.3 | Current — TC-13.19j |
| **TC-13.20** | HTML Dashboard | TC-13.17, TC-13.19 | Read-only UI |

---

## 13. Explicit Non-Goals

TC-13.18d.5 does **not** implement:

- Automatic `run_dispatch_cycle()` on escalation → TC-13.18d.3
- Auto-retry → TC-13.18d.3
- Auto-unblock → TC-13.18d.3
- `BLOCKER_CANCELLED` → Current — TC-13.18d.6
- `TASK_CANCELLED` (quiescent path) → Current — TC-13.18d.7
- `TASK_CANCELLED` (active dispatch path) → Contract Current — TC-13.18d.9a / Runtime Target — TC-13.18d.9b
- `TASK_SUPERSEDED` (quiescent path) → Current — TC-13.18d.8
- TASK_SUPERSEDED (active dispatch path) → Target
- `INTEGRATION_FAILED` → TC-13.18d.4 (already implemented)
- User notification / UI → Target
- Expert user-decision external interaction (chat UI, approval UI) → Target
- Escalation decision consumption (auto re-dispatch) → TC-13.18d.3 (already implemented)
- Rescue execution boundary — rescope is a PM control-plane only action
- `BLOCKER_RESOLVED` → TC-13.18d.3 (already implemented)

TC-13.18d.9a does **not** implement:

- Active-dispatch supersession → Target
- RateLimit retry → Target
- Retry-loop fault recovery → Target
- Codex decoding → Target
- Active-dispatch cancellation production code → TC-13.18d.9b
- Modification of `dispatcher_gateway` termination implementation
- Modification of `WorkflowOrchestrator` existing frozen/slots three-field shape
- Real Worker or subprocess execution
- Model/API/network calls
- Any change to the quiescent `cancel_quiescent_task` API

---

## 14. Active-Dispatch Cancellation — Frozen Contract (Contract Current — TC-13.18d.9a)

TC-13.18d.9a freezes the contract for cancelling a task that has an active
in-flight dispatch.  This is the active-dispatch counterpart to the quiescent
`cancel_quiescent_task` (TC-13.18d.7).  Production implementation is
TC-13.18d.9b.

### 14.1 Ownership Model — Caller-Owned Typed Dispatch Handle

The active-dispatch cancellation protocol uses a **caller-owned typed dispatch
handle**.  The `WorkflowOrchestrator.run_dispatch_cycle` method returns an
`ActiveDispatchHandle` alongside the `DispatchCycleResult` when the caller
requests an active handle.  The caller holds this handle and passes it to
`cancel_active_dispatch` to request cancellation.

**Why this model was chosen:**

| Alternative | Rejected because |
|------------|-----------------|
| **Injection-style cancellation token** | Would require the orchestrator to mutate a shared token object, violating deep immutability. The token would need to carry a mutable `.cancel()` method, breaking the frozen/slots contract. |
| **Orchestrator-owned active task registry** | Would require a global mutable `dict[str, asyncio.Task]` inside the orchestrator, creating unverifiable task identity (two orchestrator instances cannot cancel each other's tasks), and leaking bare `asyncio.Task` references into orchestrator internals. |
| **Global mutable registry** | Explicitly forbidden by the task card. Creates cross-instance cancellation problems and makes deterministic testing impossible. |

**Why the chosen model does not:**

- **Leak bare `asyncio.Task` to public API**: `ActiveDispatchHandle` is a
  `frozen=True, slots=True` dataclass containing only typed, serialisable
  fields (six identity strings/ints and a `WorkerSlotLease`).  No `Task`,
  `Future`, `dict`, `Any`, or `object` field appears in the handle.

- **Introduce unverifiable task identity**: The handle carries six identity
  fields (`task_id`, `revision`, `attempt`, `dispatch_id`,
  `holder_instance_id`, `lease_epoch`) — all derived from existing
  `DispatchIdentity` and `WorkerSlotLease` fields.  Identity is verified
  structurally at cancellation time, not by object reference.

- **Break WorkflowOrchestrator's existing frozen/slots three-field shape**:
  `WorkflowOrchestrator` remains `project_root: Path`, `clock: WorkflowClock`,
  `heartbeat_interval_seconds: float`.  The active handle is a *return value*,
  not an instance attribute.  No new instance state is added.

- **Create cross-instance cancellation problems**: The handle is a value
  object.  Any `WorkflowOrchestrator` instance with the same `project_root`
  can process the handle.  The six-field identity plus the `WorkerSlotLease`
  ensure the cancellation targets the correct dispatch, regardless of which
  orchestrator instance receives the request.

### 14.2 Precise Identity — Six Fields

An active-dispatch cancellation must be bound to **exactly** these six fields:

| Field | Source | Purpose |
|-------|--------|---------|
| `task_id` | `DispatchIdentity.task_id` | Task identity |
| `revision` | `DispatchIdentity.revision` | Snapshot revision |
| `attempt` | `DispatchIdentity.attempt` | Dispatch attempt counter |
| `dispatch_id` | `DispatchIdentity.dispatch_id` | Unique dispatch identifier |
| `holder_instance_id` | `DispatchCycleRequest.holder_instance_id` | Instance that started the dispatch |
| `lease_epoch` | `WorkerSlotLease.lease_epoch` | Slot lease epoch at acquisition |

Cancellation based on `task_id` alone is **prohibited**.  The six-field
identity ensures that:

1. A stale handle from a previous attempt cannot cancel a new attempt.
2. A handle from one project cannot cancel a dispatch in another project.
3. A lease epoch mismatch catches expired/re-acquired slots.

### 14.3 Public Types

#### 14.3.1 `ActiveDispatchHandle` — Exactly 7 Fields

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchHandle:
    """Immutable, caller-owned handle for an active dispatch.

    Returned by run_dispatch_cycle when the caller requests an active
    handle.  Passed to cancel_active_dispatch to request cancellation.
    Contains no asyncio.Task, Future, dict, Any, or object.
    """

    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    holder_instance_id: str
    lease_epoch: int
    lease: WorkerSlotLease
```

**Frozen rules:**

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state
- All six identity fields are non-empty `str` or positive `int`
- `lease` must be a `WorkerSlotLease` instance (exact type check)
- No `asyncio.Task`, `Future`, `dict`, `Any`, or `object` field
- The handle is a **value object** — it carries no behaviour, no callbacks,
  and no mutable references

#### 14.3.2 `ActiveDispatchCancellationRequest` — Exactly 2 Fields

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchCancellationRequest:
    """Immutable input for active-dispatch cancellation — exactly two fields."""

    handle: ActiveDispatchHandle
    cancellation_transition_request: TransitionRequest
```

**Frozen rules:**

- `frozen=True`, `slots=True`
- `handle` must be an `ActiveDispatchHandle` instance (exact type check)
- `cancellation_transition_request` must carry:
  - `event_type="TASK_CANCELLED"`
  - `payload` of type `CancelledPayload`
  - `dispatch_cas` with `expected_dispatch_id` and `expected_attempt`
    matching the handle's `dispatch_id` and `attempt`
- `dispatch_cas` is **mandatory** for the active path (unlike the quiescent
  path where `dispatch_cas=None`)

#### 14.3.3 `ActiveDispatchCancellationResult` — Exactly 3 Fields

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchCancellationResult:
    """Immutable result of active-dispatch cancellation — exactly three fields."""

    task_id: str
    worker_result: WorkerResult | None
    cancellation_transition: TransitionResult
```

**Frozen rules:**

- `frozen=True`, `slots=True`
- `worker_result` is `None` when the worker was cancelled before producing
  a result; it is a `WorkerResult` when the worker completed despite the
  cancellation request (race: worker finished first)
- `cancellation_transition` is the result of the `TASK_CANCELLED` transition

#### 14.3.4 `DispatchCycleResult` Extension — Exactly 1 Additional Field

```python
@dataclass(frozen=True, slots=True)
class DispatchCycleResult:
    """Immutable result — ten fields (was nine)."""
    worker_result: WorkerResult
    worker_output: WorkerOutput
    delivery_receipt: DeliveryReceipt
    dispatch_transition: TransitionResult
    acknowledge_transition: TransitionResult
    delivery_transition: TransitionResult
    slot_id: str
    lease_epoch: int
    duration_seconds: float
    active_dispatch_handle: ActiveDispatchHandle | None  # new field
```

**Frozen rules:**

- `active_dispatch_handle` is `None` when the caller did not request an
  active handle (existing behaviour unchanged)
- `active_dispatch_handle` is a populated `ActiveDispatchHandle` when the
  caller requested an active handle via `DispatchCycleRequest`
- This field is **not** a breaking change: existing callers that do not
  access the new field are unaffected

#### 14.3.5 `DispatchCycleRequest` Extension — Exactly 1 Additional Field

```python
@dataclass(frozen=True, slots=True)
class DispatchCycleRequest:
    """Immutable input for a single dispatch + delivery cycle — ten fields (was nine)."""
    dispatch_request: DispatchRequest
    dispatch_transition_request: TransitionRequest
    acknowledge_transition_request: TransitionRequest
    delivery_event_id: str
    delivery_event_context: TransitionEventContext
    provider_cli_version: str
    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    holder_instance_id: str
    request_active_handle: bool  # new field — default False
```

**Frozen rules:**

- `request_active_handle: bool` — when `True`, the orchestrator populates
  `DispatchCycleResult.active_dispatch_handle`; when `False` (default),
  the handle is `None`
- Default is `False` — existing callers that do not pass this field get
  the existing behaviour

### 14.4 Execution Order — Active-Dispatch Cancellation

`cancel_active_dispatch` MUST execute in this exact order:

```text
1. Validate request (type, identity, dispatch_cas consistency)
2. Cancel worker dispatch task (asyncio.Task.cancel)
3. Await worker task completion (DispatchCancelledError expected)
4. Cancel and await heartbeat task
5. Release WorkerSlotLease
6. Apply TASK_CANCELLED transition with lease=None + DispatchCAS
7. Return ActiveDispatchCancellationResult
```

**Step 2** calls `worker_task.cancel()` which propagates to
`run_dispatch_observed` → `_terminate_process` → `DispatchCancelledError`.

**Step 3** awaits the worker task, which completes with
`DispatchCancelledError` (the normal cancellation path).

**Step 4** cancels and awaits the heartbeat task, which is still running
from the dispatch cycle.

**Step 5** calls `release_worker_slot` after both tasks are done.

**Step 6** applies the `TASK_CANCELLED` transition with `lease=None`
(because the lease was already released) and `DispatchCAS` populated
from the handle's `dispatch_id` and `attempt`.

**Invariant**: `TASK_CANCELLED` MUST NOT be applied while the worker
process or heartbeat is still active.  Steps 2–4 complete before step 6.

### 14.5 Race Conditions — Exhaustive Classification

| # | Race | Primary exception | `__cause__` rule |
|---|------|-------------------|-----------------|
| R1 | Worker completes before cancellation request | `DispatchCancelledError` is NOT raised; worker_result is populated in `ActiveDispatchCancellationResult`. Cancellation proceeds: heartbeat → release → `TASK_CANCELLED`. | No `__cause__`. |
| R2 | Worker and cancellation complete simultaneously | Same as R1: worker may or may not have produced a result. The `worker_result` field reflects the actual outcome. | No `__cause__`. |
| R3 | Heartbeat fails concurrently with cancellation | `WorkerSlotLeaseError` from heartbeat. Cancellation continues: worker cancel → await worker → release (may fail with `WorkerSlotFencingError`) → `TASK_CANCELLED`. | If both body and release fail: body exception is primary, release exception is `__cause__`. |
| R4 | Subprocess termination fails | `DispatchCancelledError` is still raised by `run_dispatch_observed` after `_terminate_process` completes (even if process was already dead). The gateway's existing `_terminate_process` handles `ProcessLookupError` silently. | No `__cause__`. |
| R5 | Lease release fails | `WorkerSlotLeaseError` (or subclass). If cancellation body succeeded: release failure propagates. If cancellation body also failed: body exception is primary, release exception is `__cause__`. | Same as `run_dispatch_cycle` finally-block pattern. |
| R6 | Termination succeeds but transition CAS conflict | `TransitionCASConflictError`. The worker was terminated and the lease was released, but the state transition could not be applied. This is a terminal inconsistency — the caller must resolve the CAS conflict. | No `__cause__`. |
| R7 | Outer coroutine receives CancelledError again | The `cancel_active_dispatch` method must be cancellation-safe: it catches `CancelledError` in its body, but if the outer coroutine is cancelled while the method is running, the `finally` block ensures: heartbeat cancelled, worker cancelled, both awaited, lease released. The outer `CancelledError` propagates after cleanup. | No `__cause__`. |
| R8 | Duplicate cancellation of the same dispatch | Second cancellation receives the same `ActiveDispatchHandle`. The worker task is already done (cancelled or completed). Steps 2–4 are no-ops (task already done). Step 5: lease already released → `WorkerSlotNotHeldError`. Step 6: transition CAS conflict (state already `cancelled`) → `TransitionCASConflictError`. | Primary: `TransitionCASConflictError`. `__cause__`: `WorkerSlotNotHeldError` if both fail. |
| R9 | Cancel old attempt, but new attempt already started | The handle carries `attempt` and `lease_epoch`. The new attempt has a different `dispatch_id`, `attempt`, and `lease_epoch`. The cancellation request's `dispatch_cas` does not match the current state. Result: `TransitionCASConflictError`. | No `__cause__`. |

### 14.6 Transition — Active vs Quiescent

| Path | `dispatch_cas` | `lease` | Rationale |
|------|---------------|---------|-----------|
| **Active** (TC-13.18d.9a) | **Required** — `expected_dispatch_id` and `expected_attempt` from handle | `None` (lease already released in step 5) | Active cancellation must bind to the exact dispatch identity to prevent stale/ambiguous cancellations. |
| **Quiescent** (TC-13.18d.7) | `None` | `None` | No active dispatch — no dispatch identity to bind. |

Both paths use `CancelledPayload` and `TASK_CANCELLED` event type.

### 14.7 Reuse — No Second Kill/Terminate Implementation

The active-dispatch cancellation protocol **reuses** existing capabilities:

| Capability | Reused from | How |
|-----------|-------------|-----|
| `asyncio.Task.cancel()` | Python stdlib | `worker_task.cancel()` and `hb_task.cancel()` |
| Subprocess termination | `dispatcher_gateway._terminate_process` | `DispatchCancelledError` raised by `run_dispatch_observed` |
| `DispatchCancelledError` | `dispatcher_gateway` | Propagated through `run_dispatch_observed` |
| `WorkerSlotLease` release | `worker_slot_lease.release_worker_slot` | Same release path as `run_dispatch_cycle` finally block |
| `CancelledPayload` | `control_plane_transition` | Same payload type as quiescent path |
| `TransitionRequest` | `control_plane_transition` | Same request type, with `dispatch_cas` populated |
| `DispatchCAS` | `control_plane_transition` | Same CAS type, with `expected_dispatch_id` and `expected_attempt` |
| `ControlPlaneTransitionService.apply_transition()` | `control_plane_transition` | Same apply method |

**No second kill/terminate implementation is created.**  The cancellation
protocol delegates to the existing `run_dispatch_observed` cancellation path,
which calls `_terminate_process` on the subprocess.  The orchestrator never
calls `_terminate_process` directly.

### 14.8 WorkflowOrchestrator Method Signatures

```python
class WorkflowOrchestrator:
    """Three-field shape unchanged."""

    project_root: Path
    clock: WorkflowClock
    heartbeat_interval_seconds: float

    async def run_dispatch_cycle(
        self,
        request: DispatchCycleRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> DispatchCycleResult:
        """... (existing signature unchanged; result may include active_handle)"""
        ...

    async def cancel_active_dispatch(
        self,
        request: ActiveDispatchCancellationRequest,
    ) -> ActiveDispatchCancellationResult:
        """Cancel an active dispatch identified by the handle.

        Execution order (§14.4):
        1. Validate request shape and identity
        2. Cancel worker dispatch task
        3. Await worker task completion
        4. Cancel and await heartbeat task
        5. Release WorkerSlotLease
        6. Apply TASK_CANCELLED with lease=None + DispatchCAS
        7. Return ActiveDispatchCancellationResult

        Raises:
            WorkflowInputError: Invalid request type or identity mismatch.
            DispatchCancelledError: Worker cancelled (expected; caught internally).
            WorkerSlotLeaseError: Lease release or renewal failure.
            TransitionCASConflictError: CAS conflict on TASK_CANCELLED.
            WorkflowInvariantError: Internal precondition violated.
        """
        ...

    async def cancel_quiescent_task(
        self,
        request: TaskCancellationRequest,
    ) -> TaskCancellationResult:
        """... (existing quiescent API unchanged)"""
        ...
```

### 14.9 Exception Hierarchy — No New Parallel Hierarchy

Active-dispatch cancellation **does not** introduce new exception types.
All exceptions are existing types from the underlying services:

| Exception | Source module | When |
|-----------|-------------|------|
| `DispatchCancelledError` | `dispatcher_gateway` | Worker subprocess cancelled (expected) |
| `WorkerSlotLeaseError` (all subclasses) | `worker_slot_lease` | Lease release or renewal failure |
| `TransitionCASConflictError` | `control_plane_transition` | CAS conflict on TASK_CANCELLED |
| `WorkflowInputError` | `workflow_orchestrator` | Invalid request type or identity mismatch |
| `WorkflowInvariantError` | `workflow_orchestrator` | Internal precondition violated |
| `WorkflowHeartbeatError` | `workflow_orchestrator` | Heartbeat failure during cancellation |

**No** `WorkflowCancellationError`, `ActiveDispatchError`, or other
parallel exception hierarchy is created.

### 14.10 Deep Immutability

All public data types in this section use `frozen=True, slots=True`:

- `ActiveDispatchHandle` — 7 fields, all typed, no `dict`/`Any`/`object`
- `ActiveDispatchCancellationRequest` — 2 fields, all typed
- `ActiveDispatchCancellationResult` — 3 fields, all typed
- `DispatchCycleResult` — 10 fields (was 9), `active_dispatch_handle` is
  `ActiveDispatchHandle | None`, not `dict`/`Any`/`object`
- `DispatchCycleRequest` — 10 fields (was 9), `request_active_handle` is
  `bool`, not `dict`/`Any`/`object`

No `asyncio.Task`, `Future`, `dict`, `Any`, `object`, or untyped payload
appears in any public type.

### 14.11 Relationship to Existing Paths

| Path | Status | Relationship |
|------|--------|-------------|
| `cancel_quiescent_task` | Current — TC-13.18d.7 | **Unchanged** — no active dispatch, no handle, no DispatchCAS |
| `cancel_active_dispatch` | Contract Current — TC-13.18d.9a | **New** — active dispatch, handle, DispatchCAS required |
| `run_dispatch_cycle` | Current — TC-13.18b | **Extended** — `request_active_handle` and `active_dispatch_handle` fields added; existing callers unaffected |
| `run_dispatch_observed` | Current — TC-13.18b | **Unchanged** — `DispatchCancelledError` and `_terminate_process` are reused as-is |
| `WorkerSlotLease` | Current — TC-13.10c | **Unchanged** — `release_worker_slot` is reused as-is |
| `ControlPlaneTransitionService` | Current — TC-13.11c | **Unchanged** — `apply_transition` with `CancelledPayload` and `DispatchCAS` is reused as-is |

### 14.12 Task-Card Split — Active Cancellation

| Card | Description | Status |
|------|-------------|--------|
| **TC-13.18d.9a** | Active-dispatch cancellation contract freeze | Contract Current |
| **TC-13.18d.9b** | Active-dispatch cancellation production implementation | Runtime Target |
