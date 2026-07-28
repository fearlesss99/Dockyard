# WorkflowOrchestrator — Implementable Contract (Current — TC-13.18d.3)

Interface #22 frozen contract.  TC-13.18b implements the production
WorkflowOrchestrator module.  TC-13.18c.1 extends it with
DELIVERY_SUBMITTED.  TC-13.18c.2 extends it with MAD audit,
DELIVERY_ACCEPTED, and optional CHANGE_INTEGRATED.
TC-13.18d.1 extends it with DELIVERY_RETURNED and TASK_REQUEUED.
TC-13.18d.2 extends it with TASK_BLOCKED (audit blocked → escalation).
TC-13.18d.3 extends it with BLOCKER_RESOLVED (escalation resume → single redispatch).

## Status

**Current** as of TC-13.18d.3.  The dispatch cycle (snapshot → acquire →
TASK_DISPATCHED → heartbeat + run_worker →
DISPATCH_ACKNOWLEDGED → decode_worker_result →
require_delivery_receipt → DELIVERY_SUBMITTED → stop heartbeat → release
→ result), delivery remediation (audit fail → acquire remediation lease →
DELIVERY_RETURNED → release lease → TASK_REQUEUED (lease=None) →
DeliveryRemediationResult), blocked audit escalation (audit blocked →
evaluate_escalation → TASK_BLOCKED (lease=None) → BlockedAuditResult),
and escalated single redispatch (BLOCKER_RESOLVED (lease=None) → single
run_dispatch_cycle with next_worker_kind → EscalatedRedispatchResult)
are implemented and callable.

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
| 9 | `INTEGRATION_FAILED` | Target — TC-13.18d.3 (blocked path) |
| 10 | `TASK_BLOCKED` | Current — TC-13.18d.2 (blocked path) |
| 11 | `BLOCKER_RESOLVED` | Current — TC-13.18d.3 (escalation resume → ready → single redispatch) |
| 12 | `BLOCKER_RESCOPED` | TC-13.18d.2 (rescope path) |
| 13 | `BLOCKER_CANCELLED` | TC-13.18d.2 (cancel path) |
| 14 | `TASK_CANCELLED` | TC-13.18d.2 (cancel path) |
| 15 | `TASK_SUPERSEDED` | TC-13.18d.2 (supersede path) |

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
| **TC-13.18d.3** | Escalation resume + single redispatch (BLOCKER_RESOLVED + run_escalated_redispatch) | TC-13.18d.2 | Current |
| **TC-13.18d-ext** | Retry loop, escalation replay, cancellation, fault recovery | TC-13.18d.3 | Target |
| **TC-13.9c.2** | Codex decoder | TC-13.9c.1 | Target |
| **TC-13.19** | Real E2E closed-loop tests | TC-13.18d.3 | Target |
| **TC-13.20** | HTML Dashboard | TC-13.17, TC-13.19 | Read-only UI |

---

## 13. Explicit Non-Goals

TC-13.18d.2 does **not** implement:

- Automatic `run_dispatch_cycle()` on escalation → TC-13.18d.3
- Auto-retry → TC-13.18d.3
- Auto-unblock → TC-13.18d.3
- `BLOCKER_RESOLVED` / `BLOCKER_RESCOPED` / `BLOCKER_CANCELLED` → TC-13.18d.3
- `TASK_CANCELLED` / `TASK_SUPERSEDED` → TC-13.18d.3
- `INTEGRATION_FAILED` → TC-13.18d.3
- User notification / UI → TC-13.18d.3
- Expert user-decision external interaction → TC-13.18d.3
- Escalation decision consumption (auto re-dispatch) → TC-13.18d.3
