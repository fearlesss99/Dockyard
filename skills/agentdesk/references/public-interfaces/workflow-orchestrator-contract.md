# WorkflowOrchestrator — Implementable Contract (Core Orchestration Current — TC-13.18d.13b; E2E Evidence — Current — TC-13.19j; Active Cancellation — Current — TC-13.18d.9b; Active Supersession — Current — TC-13.18d.10b; Dispatch Failure Recovery Contract — Current — TC-13.18d.11a; Canonical Transition — Current — TC-13.18d.11b; Creator-Alive Bounded Retry — Current — TC-13.18d.11c; Owner-Loss Recovery Contract — Current — TC-13.18d.12b; Owner-Loss Durable Retry — Current — TC-13.18d.12c.2; Codex — Target/deferred — TC-13.9c.2; Provider 429 — Evidence-dependent Target — TC-13.14c; Interface #24 Dashboard — Current — TC-13.20b)

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
TC-13.18d.9a.1 repairs the contract (execution-based model). Production implementation is TC-13.18d.9b.
TC-13.18d.10a freezes the active-dispatch supersession contract on the same
creator-owned execution model. TC-13.18d.10b provides the production
implementation.
TC-13.18d.11a freezes dispatch failure recovery and bounded retry.
TC-13.18d.11b provides the canonical `DISPATCH_FAILED` transition production
implementation; TC-13.18d.11c provides creator-alive bounded retry
orchestration.

## Status

**Current** as of TC-13.18d.8; E2E evidence program closed as of TC-13.19j.
Active-dispatch cancellation contract frozen as of TC-13.18d.9a; production
implementation is Current — TC-13.18d.9b.
Active-dispatch supersession is Contract Current — TC-13.18d.10a and
Current — TC-13.18d.10b.
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
validates all Current runtime paths. Active-dispatch supersession production
is Current — TC-13.18d.10b. Dispatch failure recovery is Contract Current —
TC-13.18d.11a, its canonical transition is Current — TC-13.18d.11b, and
bounded retry orchestration is Current — TC-13.18d.11c. Owner-loss recovery
is Contract Current — TC-13.18d.12b; transition-only production is
Current — TC-13.18d.12c. The durable automatic-retry contract is Contract
Current — TC-13.18d.12c.1; its runtime is Current — TC-13.18d.12c.2.
Owner-loss automatic retry is Current.
Codex decoding,
Codex rate-limit classification, and provider rate-limit wiring remain Target
(optional Provider extensions; do not block Interface #22 core orchestration).
Active-dispatch cancellation
contract is repaired and frozen as of TC-13.18d.9a.1 (execution-based
model); production implementation is Current — TC-13.18d.9b.  See
`reports/tc-13.19-final-delivery-report.md` and `test_release_smoke.py`
class `TC1319jE2EProgramClosureTests`.  TC-13.20 (HTML Dashboard Interface #24)
is Current — TC-13.20b; visual acceptance passed via TC-13.20c.2.

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

`ControlPlaneTransitionService` now defines all 16 canonical transition types
(§2.14.8 of the ADR). TC-13.18d.11a freezes `DISPATCH_FAILED` as the
sixteenth type and TC-13.18d.11b provides its production implementation:

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
| 15 | `TASK_CANCELLED` (active dispatch path) | Current — TC-13.18d.9b |
| 16 | `TASK_SUPERSEDED` (quiescent path) | Current — TC-13.18d.8 |
| 17 | `TASK_SUPERSEDED` (active dispatch path) | Contract Current — TC-13.18d.10a / Current — TC-13.18d.10b |
| 18 | `DISPATCH_FAILED` (`dispatched|in_progress -> ready`) | Contract Current — TC-13.18d.11a / Current — TC-13.18d.11b |

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
| **TC-13.18d-ext** | Legacy aggregate retry/fault-recovery placeholder | TC-13.18d.3 | Superseded by TC-13.18d.11a/11b/11c split |
| **TC-13.18d.9a** | Active-dispatch cancellation contract freeze (non-implementable) | TC-13.18d.7 | Superseded |
| **TC-13.18d.9a.1** | Active-dispatch cancellation contract repair (execution model) | TC-13.18d.9a | Contract Current |
| **TC-13.18d.9b** | Active-dispatch cancellation production implementation | TC-13.18d.9a.1 | Current |
| **TC-13.18d.10a** | Active-dispatch supersession contract freeze | TC-13.18d.9b | Contract Current |
| **TC-13.18d.10b** | Active-dispatch supersession production implementation | TC-13.18d.10a | Current |
| **TC-13.18d.11a** | Dispatch failure recovery + bounded retry contract | TC-13.18d.10b | Contract Current |
| **TC-13.18d.11b** | Canonical `DISPATCH_FAILED` transition production | TC-13.18d.11a | Current |
| **TC-13.18d.11c** | Creator-alive bounded retry orchestration production | TC-13.18d.11b | Current |
| **TC-13.18d.12a-pre1** | Durable dispatch supervisor evidence contract freeze | TC-13.18d.11c | Contract Current |
| **TC-13.18d.12a-pre2.1** | Real supervisor chain, durable receipt/tombstone store, three-state liveness probing | TC-13.18d.12a-pre1 | Current |
| **TC-13.18d.12a-pre2.2** | Windows Job Object process-tree containment | TC-13.18d.12a-pre2.1 | Current |
| **TC-13.18d.12a-pre2.2.1** | Public API boundary closure (raw handles removed) | TC-13.18d.12a-pre2.2 | Current |
| **TC-13.18d.12b** | Owner-loss dispatch recovery contract freeze | TC-13.18d.12a-pre2.2.1 | Contract Current |
| **TC-13.18d.12c** | Owner-loss transition-only recovery production implementation | TC-13.18d.12b | Current |
| **TC-13.18d.12c.1** | Owner-loss automatic retry durable contract freeze | TC-13.18d.12c | Contract Current |
| **TC-13.18d.12c.2** | Owner-loss automatic retry production implementation | TC-13.18d.12c.1 | Current |
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
- `TASK_CANCELLED` (active dispatch path) → Current — TC-13.18d.9b
- `TASK_SUPERSEDED` (quiescent path) → Current — TC-13.18d.8
- `TASK_SUPERSEDED` (active dispatch path) → Contract Current — TC-13.18d.10a / Current — TC-13.18d.10b
- `INTEGRATION_FAILED` → TC-13.18d.4 (already implemented)
- User notification / UI → Target
- Expert user-decision external interaction (chat UI, approval UI) → Target
- Escalation decision consumption (auto re-dispatch) → TC-13.18d.3 (already implemented)
- Rescue execution boundary — rescope is a PM control-plane only action
- `BLOCKER_RESOLVED` → TC-13.18d.3 (already implemented)

TC-13.18d.9b still does **not** implement:

- Active-dispatch supersession → Contract Current — TC-13.18d.10a / Current — TC-13.18d.10b
- RateLimit retry → Target
- Dispatch failure recovery → Contract Current — TC-13.18d.11a / canonical transition Current — TC-13.18d.11b
- Bounded retry orchestration → Current — TC-13.18d.11c
- Codex decoding → Target
- Modification of `dispatcher_gateway` termination implementation
- Modification of `WorkflowOrchestrator` existing frozen/slots three-field shape
- Persistence of `ActiveDispatchExecution` to YAML/events/outbox/runtime files
- Real Worker or subprocess execution
- Model/API/network calls
- Any change to the quiescent `cancel_quiescent_task` API
- Any change to `DispatchCycleRequest` (9 fields) or `DispatchCycleResult`
  (9 fields) field counts

---

## 14. Active-Dispatch Cancellation — Frozen Contract (Contract Repair — TC-13.18d.9a.1)

TC-13.18d.9a froze an active-dispatch cancellation contract that was **not
implementable**: it returned the cancellation handle inside the final
`DispatchCycleResult` — i.e. only after the Worker had already completed — so
the handle could never reach a still-running task.  TC-13.18d.9a.1 revises
the model so the running dispatch is **reachable for cancellation before the
Worker completes**, via an in-process runtime controller
(`ActiveDispatchExecution`) returned by `start_dispatch_cycle()`.

This is the active-dispatch counterpart to the quiescent
`cancel_quiescent_task` (TC-13.18d.7).  Production implementation is
Current — TC-13.18d.9b.

### 14.0 Revisions Relative to TC-13.18d.9a

The following TC-13.18d.9a provisions are **retracted**:

- `DispatchCycleRequest.request_active_handle` (field removed)
- `DispatchCycleResult.active_dispatch_handle` (field removed)
- "the active handle is returned alongside the `DispatchCycleResult`"
- "any `WorkflowOrchestrator` instance with the same `project_root` can
  process the handle" (cross-instance control is now **prohibited**)
- `ActiveDispatchCancellationResult.worker_result` as a required field
- any statement that a frozen value handle alone can call
  `asyncio.Task.cancel()`

Restored:

- `DispatchCycleRequest` — exactly **9 fields** (unchanged from TC-13.18b)
- `DispatchCycleResult` — exactly **9 fields** (unchanged from TC-13.18b)
- Existing `run_dispatch_cycle()` call-site compatibility — it becomes a
  thin compatibility wrapper over `start_dispatch_cycle()` + `wait()`

### 14.1 Ownership Model — Creator-Owned Runtime Execution

The active-dispatch cancellation protocol uses a **creator-owned runtime
controller**.  `WorkflowOrchestrator.start_dispatch_cycle()` returns an
`ActiveDispatchExecution` — an in-process object that **holds the real
running Worker task and heartbeat task** and is returned **before the Worker
completes**.  The caller holds this execution and passes it to
`cancel_active_dispatch()` to request cancellation.

The execution is **not** a persisted data model, **not** a frozen dataclass,
and **not** written to YAML, events, outbox, or runtime files.  It is an
in-process capability with a bounded lifetime that ends when the dispatch
terminates (completes or is cancelled).

#### 14.1.1 Why a Frozen Value Handle Alone Is Insufficient

A `frozen=True, slots=True` value object cannot, by construction, hold a
live `asyncio.Task` reference and invoke `asyncio.Task.cancel()` on it.
Any handle that is a pure value can only describe *identity*; it cannot
*act* on the running task.  TC-13.18d.9a tried to return such a handle from
the final result, which is doubly broken: (a) the handle arrives after the
Worker is done, so there is nothing to cancel; (b) even if it arrived
earlier, a value object has no path to the live task.

TC-13.18d.9a.1 separates the two concerns:

- **`ActiveDispatchHandle`** (frozen/slots value) — identity snapshot only,
  suitable for logging, transition `DispatchCAS` construction, and
  cross-boundary naming.  It does **not** claim cancellation capability.
- **`ActiveDispatchExecution`** (mutable runtime controller) — holds the
  live tasks, owns the cancellation path, and is the only object that may
  call `asyncio.Task.cancel()` on the Worker task it created.

#### 14.1.2 Why the Execution Is Creator-Owned

An execution may only be cancelled by the `WorkflowOrchestrator` instance
that created it.  This is enforced structurally:

- The execution carries a private owner reference to its creating
  orchestrator instance.
- `cancel_active_dispatch` performs an exact-instance check
  (`execution._owner is self`); a mismatch raises `WorkflowInputError`.
- No global registry, no `dict[str, ...]` lookup by `task_id` or
  `dispatch_id`, no cross-instance dispatch.

| Alternative | Rejected because |
|------------|-----------------|
| Frozen value handle returned from final result | Arrives after Worker completes; cannot reach the running task (TC-13.18d.9a failure mode). |
| Injection-style cancellation token | Requires a mutable `.cancel()` method on a shared token that the orchestrator mutates — but the token still needs the live task reference, which the caller never had. |
| Orchestrator-owned global task registry | `dict[str, asyncio.Task]` inside the orchestrator; creates unverifiable identity, cross-instance leakage of bare `asyncio.Task`, and non-deterministic testing. |
| Cross-instance cancellation by handle | Explicitly prohibited — a value handle has no owner binding and cannot be made to reach the correct live task without a registry. |

#### 14.1.3 Why the Chosen Model Does Not Violate the Frozen Rules

- **Does not leak bare `asyncio.Task` to public API**: `ActiveDispatchExecution`
  exposes `wait()` and a `handle` property only.  No `Task`/`Future`/`dict`/
  `Any`/`object` appears in any *public* type.  The live tasks live behind
  private `__slots__` attributes the caller never touches.
- **Does not introduce unverifiable task identity**: the six-field identity
  snapshot (`ActiveDispatchHandle`) is derived from existing
  `DispatchIdentity` and `WorkerSlotLease`; the owner binding is structural
  (`execution._owner is self`), not a free-form string.
- **Does not break `WorkflowOrchestrator`'s three-field shape**: the
  orchestrator remains `project_root`, `clock`, `heartbeat_interval_seconds`.
  The execution is a *return value*, not an instance attribute.
- **Does not create cross-instance cancellation**: creator-ownership + exact
  instance check make cross-instance cancellation raise `WorkflowInputError`
  by construction.
- **Does not persist mutable state**: the execution is never serialised to
  YAML/events/outbox/runtime files; its lifetime is bounded by the
  in-process dispatch.

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

#### 14.3.1 `ActiveDispatchHandle` — Exactly 7 Fields (Identity Snapshot)

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchHandle:
    """Immutable identity snapshot for an active dispatch.

    A value object only — it carries NO behaviour, NO callbacks, and NO
    reference to the live asyncio.Task.  It is used for transition
    DispatchCAS construction and cross-boundary naming.  It does NOT claim
    cancellation capability; only ActiveDispatchExecution can cancel.
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
- The handle is a **value object** — it cannot cancel anything by itself

#### 14.3.2 `ActiveDispatchExecution` — Runtime Controller (NOT frozen)

```python
class ActiveDispatchExecution:
    """In-process runtime controller for a live dispatch.

    Returned by start_dispatch_cycle BEFORE the Worker completes.
    Holds the real running Worker task and heartbeat task behind private
    __slots__ the caller never touches.  Only the creating
    WorkflowOrchestrator instance may cancel it.

    NOT a persisted data model.  NOT a frozen dataclass.  NOT written to
    YAML, events, outbox, or runtime files.  Lifetime is bounded by the
    in-process dispatch (completes or is cancelled).
    """

    __slots__ = (
        "_owner",            # the creating WorkflowOrchestrator instance
        "_handle",           # ActiveDispatchHandle (frozen snapshot)
        "_worker_task",      # asyncio.Task[WorkerResult] (private)
        "_heartbeat_task",   # asyncio.Task[None] (private)
        "_lease",            # WorkerSlotLease (acquired)
        "_state_lock",       # asyncio.Lock (private winner decision)
        "_ready",            # bool: start_dispatch_cycle fully set up
        "_completed",        # bool: dispatch reached terminal state
        "_result",           # DispatchCycleResult | None (set by wait)
    )

    @property
    def handle(self) -> ActiveDispatchHandle:
        """Return the frozen identity snapshot.  Does not expose the live task."""
        ...

    async def wait(self) -> DispatchCycleResult:
        """Await normal completion.  Returns the DispatchCycleResult.

        Must NOT expose the bare asyncio.Task.  Idempotent: a second call
        returns the same result (or raises the same stored exception).
        """
        ...
```

**Frozen rules:**

- **Not** a `@dataclass(frozen=True)` — it is a runtime controller with
  private mutable state behind `__slots__`.
- Exposed in `__all__` as a runtime capability, never as a persisted type;
  it is returned by `start_dispatch_cycle` and consumed by
  `cancel_active_dispatch` / `wait()`.
- No public attribute exposes `asyncio.Task`, `Future`, `dict`, `Any`, or
  `object`.  The live tasks live in private `_`-prefixed `__slots__`.
- The `handle` property returns the frozen `ActiveDispatchHandle` snapshot
  only — never the live task.
- `wait()` is the only public path to the final `DispatchCycleResult`.

#### 14.3.3 `ActiveDispatchCancellationRequest` — Exactly 2 Fields

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
- Because the frozen lower-level `TransitionRequest` constructor still
  accepts `TASK_CANCELLED` only without `DispatchCAS`, constructing
  `ActiveDispatchCancellationRequest` binds an otherwise-valid
  no-`DispatchCAS` transition to an immutable copy carrying the handle's
  exact `dispatch_id` and `attempt`; the caller's transition is not mutated.
- The request does **not** carry the execution — the execution is passed
  separately to `cancel_active_dispatch` so creator-ownership can be
  checked structurally

#### 14.3.4 `ActiveDispatchCancellationResult` — Exactly 3 Fields

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchCancellationResult:
    """Immutable result of active-dispatch cancellation — exactly three fields.

    Does NOT require a WorkerResult.  When cancellation wins, the Worker
    task was cancelled before producing a usable result, and the result is
    defined solely by the TASK_CANCELLED transition.
    """

    task_id: str
    dispatch_id: str
    cancellation_transition: TransitionResult
```

**Frozen rules:**

- `frozen=True`, `slots=True`
- **No `worker_result` field** — the cancellation path does not promise a
  `WorkerResult` (the Worker was cancelled).  This is the key correction
  to TC-13.18d.9a, which made `worker_result` a required field.
- `cancellation_transition` is the result of the `TASK_CANCELLED` transition

#### 14.3.5 `DispatchCycleRequest` — Exactly 9 Fields (Unchanged)

```python
@dataclass(frozen=True, slots=True)
class DispatchCycleRequest:
    """Immutable input for a single dispatch + delivery cycle — nine fields.

    Unchanged from TC-13.18b.  The request_active_handle field proposed by
    TC-13.18d.9a is RETRACTED.
    """
    dispatch_request: DispatchRequest
    dispatch_transition_request: TransitionRequest
    acknowledge_transition_request: TransitionRequest
    delivery_event_id: str
    delivery_event_context: TransitionEventContext
    provider_cli_version: str
    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    holder_instance_id: str
```

#### 14.3.6 `DispatchCycleResult` — Exactly 9 Fields (Unchanged)

```python
@dataclass(frozen=True, slots=True)
class DispatchCycleResult:
    """Immutable result — nine fields.

    Unchanged from TC-13.18b.  The active_dispatch_handle field proposed by
    TC-13.18d.9a is RETRACTED — the handle/execution is returned by
    start_dispatch_cycle, never embedded in the final result.
    """
    worker_result: WorkerResult
    worker_output: WorkerOutput
    delivery_receipt: DeliveryReceipt
    dispatch_transition: TransitionResult
    acknowledge_transition: TransitionResult
    delivery_transition: TransitionResult
    slot_id: str
    lease_epoch: int
    duration_seconds: float
```

### 14.4 Start Order — `start_dispatch_cycle`

`start_dispatch_cycle` MUST NOT return the `ActiveDispatchExecution` until
all six preconditions hold:

```text
1. WorkerSlotLease acquired
2. TASK_DISPATCHED transition applied successfully
3. Worker task created (asyncio.ensure_future(run_worker_observed(...)))
4. DISPATCH_ACKNOWLEDGED transition applied successfully
5. heartbeat task started and handshake confirmed
6. execution bound to the real cycle/worker task (owner, handle, tasks,
   state_lock, _ready=True)
```

Only after step 6 is the execution **reachable for cancellation**.  The
caller may then either `await execution.wait()` for normal completion or
`await orchestrator.cancel_active_dispatch(execution, request)` to cancel.

The existing `run_dispatch_cycle()` becomes a thin compatibility wrapper:

```python
async def run_dispatch_cycle(self, request, providers) -> DispatchCycleResult:
    execution = await self.start_dispatch_cycle(request, providers)
    return await execution.wait()
```

### 14.5 Cancellation Order — `cancel_active_dispatch`

`cancel_active_dispatch` MUST execute in this exact order:

```text
1. Validate execution owner (exact-instance check) and handle identity
   (six-field match between request.handle and execution.handle)
2. Acquire execution private state lock; decide the completion/cancellation
   winner atomically:
     - if worker_task.done(): completion wins → raise WorkflowInputError,
       do NOT write TASK_CANCELLED
     - else: cancellation wins → record cancellation-won flag
3. Cancel the real cycle/worker task (asyncio.Task.cancel on the live task)
4. Await worker task completion (DispatchCancelledError expected and caught)
5. Cancel and await heartbeat task
6. Release WorkerSlotLease — exactly once
7. Apply TASK_CANCELLED transition with lease=None + precise DispatchCAS
8. Return ActiveDispatchCancellationResult
```

**Step 2** is the winner decision: under the execution's private
`_state_lock`, the method checks `worker_task.done()`.  If the Worker
already completed, **completion wins** and the cancellation request is
rejected with `WorkflowInputError` (no `TASK_CANCELLED` is written).
Otherwise cancellation wins and is recorded so a concurrent `wait()` cannot
overwrite the cancellation.

**Step 3** cancels the real `asyncio.Task` that `start_dispatch_cycle`
created and that lives inside the execution.  This is the only code path
that calls `worker_task.cancel()`.

**Step 4** awaits the worker task, which completes with
`DispatchCancelledError` (the normal cancellation path inside
`run_dispatch_observed`).

**Step 6** calls `release_worker_slot` exactly once, after both tasks are
done.  A duplicate cancellation must NOT release again (guarded by the
`_completed` / cancellation-won flag).

**Step 7** applies `TASK_CANCELLED` with `lease=None` (lease already
released) and `DispatchCAS` populated from the handle's `dispatch_id` and
`attempt`.

**Invariant**: `TASK_CANCELLED` MUST NOT be applied while the Worker
process or heartbeat is still active.  Steps 3–5 complete before step 7.

### 14.6 Race Conditions — Explicit Winner Rules

Every race has exactly one winner, decided atomically under the execution's
private `_state_lock`.  The rules:

| # | Race | Winner | Result / Primary exception | `__cause__` |
|---|------|--------|-----------------------------|-------------|
| R1 | Worker completes before cancellation request | **Completion** | `wait()` returns the `DispatchCycleResult` normally.  `cancel_active_dispatch` raises `WorkflowInputError` ("dispatch already completed"); `TASK_CANCELLED` is NOT written. | No `__cause__`. |
| R2 | Worker and cancellation request arrive simultaneously | Decided atomically under `_state_lock`: check `worker_task.done()` before recording cancellation-won.  Exactly one wins. | Whoever wins; the loser is rejected with `WorkflowInputError` (cancellation) or proceeds (completion). | No `__cause__`. |
| R3 | Heartbeat fails concurrently with cancellation | **Heartbeat failure** takes priority. | `WorkerSlotLeaseError` propagates; `TASK_CANCELLED` is NOT written.  Cleanup still cancels the worker and releases the lease. | If body and release both fail: body is primary, release is `__cause__`. |
| R4 | Subprocess termination fails | Cancellation still proceeds. | `DispatchCancelledError` is still raised by `run_dispatch_observed` after `_terminate_process` (the gateway swallows `ProcessLookupError` silently). | No `__cause__`. |
| R5 | Lease release fails | Release failure propagates. | `WorkerSlotLeaseError` (or subclass); `TASK_CANCELLED` is NOT written (transition is skipped — see §14.5 step 7 precondition). | If body and release both fail: body is primary, release is `__cause__`. |
| R6 | Termination succeeds but transition CAS conflict | Process cleanup is already done; CAS conflict is terminal. | `TransitionCASConflictError` propagates; the already-completed process/lease cleanup is NOT rolled back. | No `__cause__`. |
| R7 | Duplicate cancellation of the same dispatch | Second call fail-closed. | `_completed` / cancellation-won flag is already set; second call raises `WorkflowInputError` ("dispatch already terminal"); does NOT release again or write `TASK_CANCELLED` again. | No `__cause__`. |
| R8 | Cancel old attempt, but new attempt already started | CAS conflict — cannot cancel new attempt. | The handle's `attempt` / `lease_epoch` / `dispatch_id` do not match the current state; `TransitionCASConflictError` (or `WorkflowInputError` if owner/identity mismatch). | No `__cause__`. |
| R9 | Outer coroutine receives `CancelledError` while `cancel_active_dispatch` runs | Cleanup completes first, then outer `CancelledError` propagates. | The `finally` block completes worker cancel + heartbeat cancel + lease release; then the outer `CancelledError` re-raises. | No `__cause__`. |

**Key rules:**

- A single WAIT candidate must not be overridden by completion: once
  cancellation wins under `_state_lock`, `wait()` must NOT return the
  Worker's result.
- **Completion winning** (R1, R2) means `TASK_CANCELLED` is NOT written —
  the task reaches its normal terminal transition via the dispatch cycle.
- **Heartbeat failure** (R3) always takes priority over cancellation —
  `TASK_CANCELLED` is NOT written.
- **Lease release failure** (R5) skips the transition — the
  `TASK_CANCELLED` precondition (lease released) is violated, so the
  transition must NOT be attempted.
- No `CONFLICT` reason or exception — races are resolved by the
  deterministic winner rules above.

### 14.7 Transition — Active vs Quiescent

| Path | `dispatch_cas` | `lease` | Rationale |
|------|---------------|---------|-----------|
| **Active** (TC-13.18d.9a.1) | **Required** — `expected_dispatch_id` and `expected_attempt` from the execution's handle | `None` (lease released in step 6 before the transition) | Active cancellation must bind to the exact dispatch identity to prevent stale/ambiguous cancellations. |
| **Quiescent** (TC-13.18d.7) | `None` | `None` | No active dispatch — no dispatch identity to bind. |

Both paths use `CancelledPayload` and `TASK_CANCELLED` event type.

### 14.8 Reuse — No Second Kill/Terminate Implementation

The active-dispatch cancellation protocol **reuses** existing capabilities:

| Capability | Reused from | How |
|-----------|-------------|-----|
| `asyncio.Task.cancel()` | Python stdlib | the execution calls `worker_task.cancel()` and `hb_task.cancel()` on the tasks it created |
| Subprocess termination | `dispatcher_gateway._terminate_process` | `DispatchCancelledError` raised by `run_dispatch_observed` when its `communicate()` is cancelled |
| `DispatchCancelledError` | `dispatcher_gateway` | Propagated through `run_dispatch_observed`, caught in step 4 |
| `WorkerSlotLease` release | `worker_slot_lease.release_worker_slot` | Same release path as `run_dispatch_cycle` finally block — exactly once |
| `CancelledPayload` | `control_plane_transition` | Same payload type as quiescent path |
| `TransitionRequest` | `control_plane_transition` | Same request type, with `dispatch_cas` populated |
| `DispatchCAS` | `control_plane_transition` | Same CAS type, with `expected_dispatch_id` and `expected_attempt` |
| `ControlPlaneTransitionService.apply_transition()` | `control_plane_transition` | Same apply method |

**No second kill/terminate implementation is created.**  The orchestrator
never calls `_terminate_process` directly; the Worker task's
`asyncio.Task.cancel()` propagates into `run_dispatch_observed`, which
calls `_terminate_process` on the subprocess exactly as it already does.
The execution merely owns the live `asyncio.Task` reference so cancellation
can reach it before completion.

### 14.9 WorkflowOrchestrator Method Signatures

```python
class WorkflowOrchestrator:
    """Three-field shape unchanged."""

    project_root: Path
    clock: WorkflowClock
    heartbeat_interval_seconds: float

    async def start_dispatch_cycle(
        self,
        request: DispatchCycleRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> ActiveDispatchExecution:
        """Start a dispatch and return the live execution BEFORE the Worker
        completes (TC-13.18d.9a.1).

        Preconditions that must hold before returning (§14.4):
        1. WorkerSlotLease acquired
        2. TASK_DISPATCHED applied
        3. Worker task created
        4. DISPATCH_ACKNOWLEDGED applied
        5. heartbeat started and handshake confirmed
        6. execution bound to the real cycle/worker task

        The returned execution is reachable for cancellation.  The caller
        either awaits execution.wait() for normal completion or calls
        cancel_active_dispatch(execution, request) to cancel.
        """
        ...

    async def run_dispatch_cycle(
        self,
        request: DispatchCycleRequest,
        providers: Mapping[str, AgentCliProvider],
    ) -> DispatchCycleResult:
        """Compatibility wrapper (unchanged signature from TC-13.18b).

        Returns DispatchCycleResult — exactly 9 fields, no embedded handle.
        """
        execution = await self.start_dispatch_cycle(request, providers)
        return await execution.wait()

    async def cancel_active_dispatch(
        self,
        execution: ActiveDispatchExecution,
        request: ActiveDispatchCancellationRequest,
    ) -> ActiveDispatchCancellationResult:
        """Cancel a live dispatch owned by this orchestrator (§14.5).

        Execution order:
        1. Validate execution owner (exact-instance) + handle identity
        2. Under execution _state_lock decide winner
        3. If cancellation wins: cancel real cycle/worker task
        4. Await worker task completion (DispatchCancelledError caught)
        5. Cancel and await heartbeat task
        6. Release WorkerSlotLease — exactly once
        7. Apply TASK_CANCELLED with lease=None + precise DispatchCAS
        8. Return ActiveDispatchCancellationResult

        Raises:
            WorkflowInputError: execution not owned by self; identity
                mismatch; dispatch already completed (completion won).
            WorkerSlotLeaseError: heartbeat or release failure.
            TransitionCASConflictError: CAS conflict on TASK_CANCELLED.
            WorkflowInvariantError: internal precondition violated.
        """
        ...

    async def cancel_quiescent_task(
        self,
        request: TaskCancellationRequest,
    ) -> TaskCancellationResult:
        """... (existing quiescent API unchanged)"""
        ...
```

### 14.10 Exception Hierarchy — No New Parallel Hierarchy

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

### 14.11 Deep Immutability

The **value** types in this section use `frozen=True, slots=True`:

- `ActiveDispatchHandle` — 7 fields, all typed, no `dict`/`Any`/`object`
- `ActiveDispatchCancellationRequest` — 2 fields, all typed
- `ActiveDispatchCancellationResult` — 3 fields, all typed; **no
  `worker_result` field**
- `DispatchCycleResult` — exactly **9 fields** (unchanged from TC-13.18b)
- `DispatchCycleRequest` — exactly **9 fields** (unchanged from TC-13.18b)

The **runtime** type `ActiveDispatchExecution` is **not** frozen (it holds
live `asyncio.Task` references), but exposes **no public attribute** of
type `Task`, `Future`, `dict`, `Any`, or `object` — the live tasks live
behind private `_`-prefixed `__slots__`, and the only public surface is
`handle` (property returning the frozen snapshot) and `wait()` (coroutine
returning `DispatchCycleResult`).

No `asyncio.Task`, `Future`, `dict`, `Any`, `object`, or untyped payload
appears in any **public** type or in the execution's public surface.

### 14.12 Relationship to Existing Paths

| Path | Status | Relationship |
|------|--------|-------------|
| `cancel_quiescent_task` | Current — TC-13.18d.7 | **Unchanged** — no active dispatch, no execution, no DispatchCAS |
| `cancel_active_dispatch` | Current — TC-13.18d.9b | **Revised** — takes execution + request; creator-owned; no `worker_result` in result |
| `start_dispatch_cycle` | Current — TC-13.18d.9b | **New** — returns live execution before Worker completes |
| `run_dispatch_cycle` | Current — TC-13.18b | **Unchanged signature** — compatibility wrapper over start+wait; 9-field result |
| `run_dispatch_observed` | Current — TC-13.18b | **Unchanged** — `DispatchCancelledError` and `_terminate_process` are reused as-is |
| `WorkerSlotLease` | Current — TC-13.10c | **Unchanged** — `release_worker_slot` is reused as-is |
| `ControlPlaneTransitionService` | Current — TC-13.11c | **Unchanged** — `apply_transition` with `CancelledPayload` and `DispatchCAS` is reused as-is |

### 14.13 Task-Card Split — Active Cancellation

| Card | Description | Status |
|------|-------------|--------|
| **TC-13.18d.9a** | Active-dispatch cancellation contract freeze (non-implementable) | Superseded by TC-13.18d.9a.1 |
| **TC-13.18d.9a.1** | Active-dispatch cancellation contract repair (execution model) | Contract Current |
| **TC-13.18d.9b** | Active-dispatch cancellation production implementation | Current |

---

## 15. Active-Dispatch Supersession — Frozen Contract (Contract Current — TC-13.18d.10a)

Active-dispatch supersession is the `TASK_SUPERSEDED` counterpart to
TC-13.18d.9b cancellation. It acts on the same creator-owned
`ActiveDispatchExecution`, terminates the old dispatch, and records the
replacement identity only after process, heartbeat, and lease cleanup.
Production implementation is Current — TC-13.18d.10b.

This path does **not** dispatch the replacement task. It performs one
terminal transition for the source task and returns.

### 15.1 Public Types

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchSupersessionRequest:
    """Immutable request for active-dispatch supersession — two fields."""

    handle: ActiveDispatchHandle
    supersession_transition_request: TransitionRequest
```

```python
@dataclass(frozen=True, slots=True)
class ActiveDispatchSupersessionResult:
    """Immutable active-dispatch supersession result — four fields."""

    task_id: str
    dispatch_id: str
    superseded_by: str
    supersession_transition: TransitionResult
```

Both are public value types with `frozen=True, slots=True`. The result has
no `worker_result`, task, future, callback, registry key, or persisted
runtime capability.

`ActiveDispatchSupersessionRequest` requires:

- `handle` exact type `ActiveDispatchHandle`;
- `supersession_transition_request` exact type `TransitionRequest`;
- `event_type="TASK_SUPERSEDED"`;
- payload exact type `SupersededPayload`;
- transition `cas.task_id == handle.task_id`;
- replacement `SupersededPayload.superseded_by != handle.task_id`;
- exact active-path `DispatchCAS` matching `handle.dispatch_id` and
  `handle.attempt`.

The frozen lower-level `TransitionRequest` constructor still accepts
`TASK_SUPERSEDED` only without `DispatchCAS`. Therefore the outer active
request uses the same immutable binding rule as
`ActiveDispatchCancellationRequest`: an otherwise-valid no-`DispatchCAS`
transition is copied, the handle-derived exact `DispatchCAS` is attached to
the copy, and the caller's transition is not mutated. No lower-level module
change is part of this contract.

### 15.2 Public Method

```python
async def supersede_active_dispatch(
    self,
    execution: ActiveDispatchExecution,
    request: ActiveDispatchSupersessionRequest,
) -> ActiveDispatchSupersessionResult:
    """Supersede a live dispatch owned by this exact orchestrator."""
    ...
```

The method performs exact-type input validation, requires
`execution._owner is self`, and requires `request.handle ==
execution.handle`. Cross-instance control, task-id-only lookup, and a global
execution registry remain prohibited.

### 15.3 One Winner Across Completion, Cancellation, and Supersession

Completion, cancellation, supersession, heartbeat failure, and the
compatibility wrapper's outer cancellation share the same execution
`_state_lock`, `_winner`, completion future, runner, and shared finalizer.

The production implementation must extend the TC-13.18d.9b finalizer. It
must not add a second state machine, a supersession-owned lease release, or
a second process-termination path.

Exactly one of these outcomes may win:

- `completion`;
- `cancellation`;
- `supersession`;
- `heartbeat`;
- `outer_cancellation`.

Cancellation and supersession can never both publish terminal transitions
for the same execution.

### 15.4 Fixed Supersession Order

```text
1. Validate execution exact type, exact owner, handle, and transition
2. Under execution._state_lock select the single winner
3. If supersession wins, cancel the real Worker task
4. Await DispatcherGateway-confirmed process-tree cleanup
5. Stop and await heartbeat
6. Release WorkerSlotLease exactly once
7. Apply TASK_SUPERSEDED with lease=None and exact DispatchCAS
8. Return ActiveDispatchSupersessionResult
```

The orchestrator never calls `_terminate_process()` directly. Cancelling
the real Worker task reaches `run_dispatch_observed`, which owns subprocess
tree termination and reports `DispatchCancelledError`.

`TASK_SUPERSEDED` must not be applied while the Worker process or heartbeat
is active, before lease release succeeds, or when Worker cleanup is not
confirmed.

### 15.5 Fail-Closed Race Rules

| Race or failure | Winner / result | Forbidden side effects |
|---|---|---|
| Worker completed before supersession | Completion; supersession raises `WorkflowInputError` | No `TASK_SUPERSEDED` |
| Worker and supersession simultaneous | Lock checks Worker completion before choosing supersession | Never two winners |
| Cancellation already won | Cancellation; supersession raises `WorkflowInputError` | No second release or transition |
| Supersession already won | Supersession; cancellation raises `WorkflowInputError` | No second release or transition |
| Heartbeat already failed | `WorkerSlotLeaseError` has priority | No `TASK_SUPERSEDED` |
| Worker cleanup unconfirmed | Cleanup exception propagates | No release, transition, or success result |
| Lease release failed | `WorkerSlotLeaseError` propagates | Transition skipped |
| Transition CAS conflict | `TransitionCASConflictError` propagates after cleanup | Cleanup is not rolled back |
| Stale attempt or dispatch | CAS conflict | New attempt and replacement remain untouched |
| Duplicate supersession | `WorkflowInputError` | Zero duplicate release/transition |
| Outer `CancelledError` after cleanup starts | Shield cleanup to completion, then re-raise original cancellation | No partial cleanup |
| Concurrent `execution.wait()` | Observes the same winner/final outcome | No second finalizer |

### 15.6 Active vs Quiescent Supersession

| Path | Execution | `dispatch_cas` | Lease at transition | Replacement dispatch |
|---|---|---|---|---|
| Quiescent — TC-13.18d.8 | None | None | None | Never automatic |
| Active — TC-13.18d.10b | Creator-owned `ActiveDispatchExecution` | Required and exact | None, after release | Never automatic |

`supersede_quiescent_task()` and its existing
`TaskSupersessionRequest`/`TaskSupersessionResult` remain unchanged.
`DispatchCycleRequest` and `DispatchCycleResult` remain exactly nine fields,
and `run_dispatch_cycle()` retains its signature and compatibility behavior.

### 15.7 Status and Task Split

- `TASK_SUPERSEDED` active-dispatch contract:
  **Contract Current — TC-13.18d.10a**.
- Production implementation: **Current — TC-13.18d.10b**.
- `TASK_CANCELLED` active-dispatch path remains
  **Current — TC-13.18d.9b**.
- WorkflowOrchestrator Interface #22 core orchestration is
  **Current — TC-13.18d.13b**. Codex runtime/decoder remains
  **Target/deferred — TC-13.9c.2**. Provider 429 detection remains
  **Evidence-dependent Target — TC-13.14c**. RateLimit → Orchestrator
  wiring remains **Target**, depending on real detection evidence.
  Interface #24 (HTML Dashboard) is **Current — TC-13.20b**.
- TC-13.19 and TC-13.20 statuses are unchanged.
- TaskDifficulty dispatch runtime wiring is **Current — TC-13.22b.3a**.

PortfolioScheduler admission is a separate contract introduced by
TC-13.24a.  Its future boundary is limited to `queued → selected →
TASK_DISPATCHED` admission; it does not change `DispatchRequest`, `DispatchCAS`,
`WorkerSlotLease`, `run_dispatch_cycle()`, or the WorkflowOrchestrator
delivery/acceptance/integration state machine.  After `TASK_DISPATCHED`, the
Orchestrator and ControlPlaneTransitionService remain the sole owners of ACK,
Worker execution, delivery, acceptance, integration, cancellation,
supersession, retry, and owner-loss semantics.  The PortfolioScheduler
selection/admission runtime remains Target until a later task card; the
durable evidence store is **Current — TC-13.24b.1**.

---

## 16. Dispatch Failure Recovery and Bounded Retry — Frozen Contract (Contract Current — TC-13.18d.11a)

TC-13.18d.11a freezes one recovery event and one finite, creator-alive retry
surface. The canonical transition is Current — TC-13.18d.11b; the
creator-alive retry orchestrator is Current — TC-13.18d.11c.

This contract does not broaden `TASK_REQUEUED`. That event remains exactly
`returned -> ready` for delivery remediation. Dispatch failure recovery uses
the new and unambiguous `DISPATCH_FAILED` event.

### 16.1 Failure Classification

| Failure | Canonical residual state | Required evidence | TC-13.18d.11c eligibility |
|---|---|---|---|
| Worker fails after ACK | `in_progress` | winner is `completion`; Worker and heartbeat done; release completed | Recoverable in the same creator |
| Output decode fails | `in_progress` | same cleanup proof; no delivery transition committed | Recoverable in the same creator |
| `DELIVERY_SUBMITTED` fails before commit | `in_progress` | cleanup complete; fresh snapshot proves exact dispatch remains `in_progress`; no matching delivery event | Recoverable in the same creator |
| Worker/start fails before `start_dispatch_cycle()` returns an execution | `dispatched` | no public execution/finalizer metadata exists | Ineligible; propagate unchanged with zero recovery and zero next attempt |
| Heartbeat fencing / ownership loss | `dispatched` or `in_progress` | ownership is uncertain | Fail-closed; no automatic recovery |
| Lease release fails | `dispatched` or `in_progress` | release is not confirmed | Fail-closed; no recovery transition |
| Recovery or terminal CAS conflict | Canonical state wins | conflict itself proves the plan is stale | Fail-closed; no retry |
| Outer caller cancellation | Existing active cancellation/outer-cancellation semantics | shared finalizer result | Never converted into retry |
| Active cancellation or supersession wins | `cancelled` or `superseded` | shared winner and terminal transition | Terminal; never retry |
| Creator process is lost | Unknown live-process state | no current cleanup authority exists | Owner-loss recovery remains Target |
| Late old-attempt completion/heartbeat/transition | Any newer state/attempt | exact `DispatchCAS` mismatch | Reject as stale |
| `review_ready`, `accepted`, `integrated`, `cancelled`, or `superseded` | State shown | canonical snapshot | Ineligible |

“Lease expired” is not process-tree cleanup evidence. A failure is eligible
only when the same creator can prove that its real Worker and heartbeat tasks
are done and its lease release completed.

### 16.2 Canonical Recovery Event — Unique Decision

The sole dispatch-failure recovery event is:

```python
@dataclass(frozen=True, slots=True)
class DispatchFailedPayload:
    """One-field payload for dispatched|in_progress -> ready."""

    failure_kind: str
```

`failure_kind` is exactly one of:

- `"dispatch_start_failed"`;
- `"worker_failed"`;
- `"worker_output_failed"`;
- `"delivery_transition_failed"`.

Provider names, exit output, exception strings, prompts, workspace paths, and
secrets are forbidden. At least one safe evidence reference is required in
`TransitionEventContext.evidence_refs`.

The `DISPATCH_FAILED` transition contract is:

| Property | Frozen rule |
|---|---|
| from state | exactly `dispatched` or `in_progress` |
| to state | exactly `ready` |
| `TransitionCAS` | exact task, revision, from state, and snapshot commit |
| `DispatchCAS` | required; exact failed `dispatch_id` and attempt |
| lease argument | exactly `None`, only after confirmed cleanup and release |
| task attempt | preserve the failed attempt number |
| `current_dispatch` | clear to `None` |
| delivery fields | clear `delivery_state`, `implementation_commit`, and `report_commit` |
| report path | clear `report_path` |
| timestamps | preserve lifecycle timestamps; common `updated_at` and immutable event `occurred_at` record recovery |
| state event | serialize exact `failure_kind`; preserve non-empty safe `evidence_refs` |
| outbox / acceptance | produce neither |
| automatic dispatch | forbidden |

`DISPATCH_FAILED` becomes the sixteenth canonical event type. It does not
modify the frozen state set. The next `TASK_DISPATCHED` is a separate,
approval-gated transition that supplies a distinct dispatch id and
`new_attempt == failed_attempt + 1`.

Strict byte-exact idempotency is inherited from
`ControlPlaneTransitionService`: replay of the same event/request at the
already-reached target returns the existing result with zero writes.
Reusing an event id with different bytes, using a stale dispatch/attempt, or
targeting a task changed by cancellation/supersession fails closed.

### 16.3 Public Bounded-Retry Types

```python
@dataclass(frozen=True, slots=True)
class DispatchRetryAttempt:
    """Two-field caller-supplied attempt and recovery pair."""

    dispatch_cycle_request: DispatchCycleRequest
    failure_transition_request: TransitionRequest
```

```python
@dataclass(frozen=True, slots=True)
class BoundedDispatchRetryRequest:
    """One-field finite plan; tuple length is the requested attempt budget."""

    attempts: tuple[DispatchRetryAttempt, ...]
```

```python
@dataclass(frozen=True, slots=True)
class BoundedDispatchRetryResult:
    """Four-field success result."""

    task_id: str
    attempts_started: int
    recovery_transitions: tuple[TransitionResult, ...]
    dispatch_cycle_result: DispatchCycleResult
```

All three types are exact, frozen, and slotted. The plan contains between one
and three attempts inclusive. Its tuple length is both the explicit caller
budget and the hard upper bound; there is no hidden default or unbounded
iterator.

The whole plan is validated before the first side effect:

- every attempt targets the same task and revision;
- attempt numbers are strictly consecutive;
- every dispatch id, dispatch event id, ACK event id, delivery event id, and
  outbox message id is distinct across the plan;
- every failure request is exact type `TransitionRequest`, event type
  `DISPATCH_FAILED`, payload exact type `DispatchFailedPayload`;
- every failure request has the exact task, exact expected state
  `"in_progress"` (never `"dispatched"`), dispatch id, and attempt for its
  paired dispatch;
- after each recovery, the next `TASK_DISPATCHED` CAS expects `ready`, retains
  the previous ledger attempt, and its payload increments it by exactly one.

The caller supplies every identifier. The orchestrator generates no ids and
does not clone an old `DispatchCycleRequest`.

### 16.4 Public Method

```python
async def run_bounded_dispatch_retry(
    self,
    request: BoundedDispatchRetryRequest,
    providers: Mapping[str, AgentCliProvider],
) -> BoundedDispatchRetryResult:
    ...
```

The method is a finite wrapper over the existing creator-owned execution. It
does not add a second Worker task, runner, heartbeat, lease release, process
termination operation, completion future, or winner.

### 16.5 Fixed Order

```text
1. Validate the complete one-to-three-attempt plan before side effects
2. start_dispatch_cycle for the current caller-supplied attempt
3. await the same ActiveDispatchExecution.wait()
4. on success, return BoundedDispatchRetryResult
5. on failure, preserve the original exception
6. require completion winner + Worker done + heartbeat done + release completed
7. require finalizer completion publication, then read its frozen private typed classification
8. read a fresh canonical snapshot and verify exact `in_progress` state/DispatchCAS
9. apply paired DISPATCH_FAILED with lease=None
10. if exhausted, verify a fresh exact `ready` snapshot and re-raise the original final exception
11. otherwise start the next distinct, consecutive attempt
```

There is no sleep, polling, exponential backoff, jitter, rate-limit policy, or
implicit retry. A failure recovery transition never starts a Worker.

### 16.6 Eligibility and Exception Priority

Only a `completion` winner with confirmed cleanup may enter recovery. Worker
execution failure, output decode failure, and a provably uncommitted delivery
transition failure are eligible. These outcomes are not eligible:

- `asyncio.CancelledError`;
- active cancellation or supersession;
- heartbeat/fencing failure;
- cleanup or release failure;
- any CAS conflict;
- invalid plan/input;
- an already-terminal or advanced canonical state.

The classification is a private typed enum stored in frozen finalizer
metadata before the shared completion future is published. The wrapper reads
it only after `wait()` proves completion publication and all Worker,
heartbeat, and release evidence is complete. It never inspects exception
messages, `str()`/`repr()`, stdout/stderr, provider names, or exit codes.

Any exception raised before `start_dispatch_cycle()` returns
`ActiveDispatchExecution` propagates as the same exception object. It causes
zero `DISPATCH_FAILED` calls and zero subsequent attempts; pre-ACK
`dispatched` recovery remains a future owner-loss/pre-ACK boundary.

If cleanup/release fails, that existing failure propagates and no
`DISPATCH_FAILED` is attempted. If recovery transition fails, its existing
transition exception is primary and the original dispatch failure is its
`__cause__`. If recovery succeeds and another planned attempt exists, the
original failure is superseded by the explicit retry. If the final attempt
fails, recovery is still committed exactly once and the original final
failure is re-raised unchanged to signal budget exhaustion.

### 16.7 Race and Exactly-Once Matrix

| Condition | Recovery writes | Next dispatches | Result |
|---|---:|---:|---|
| First attempt succeeds | 0 | 0 | success, `attempts_started=1` |
| Failure, recovery, next attempt succeeds | 1 | 1 | success with one recovery result |
| All three attempts fail | 3 | 2 | final failure re-raised after third recovery |
| Duplicate recovery request | 0 new writes | 0 | byte-exact replay or fail-closed mismatch |
| Cancellation/supersession wins | 0 | 0 | existing winner result/error |
| Heartbeat or release fails | 0 | 0 | existing lease error |
| Recovery CAS conflict | 0 successful recoveries | 0 | conflict propagates |
| Old attempt reports late | 0 | 0 | stale DispatchCAS rejected |
| Concurrent `execution.wait()` | one shared finalizer | at most one | same completion outcome |
| Outer caller cancellation | 0 | 0 | cleanup completes, cancellation propagates |

Each failed eligible attempt has at most one successful `DISPATCH_FAILED`,
one lease release owned by its existing finalizer, and at most one following
`TASK_DISPATCHED`. A retry wrapper never repeats finalization.

After the third recovery, a fresh canonical snapshot must be exactly
`state == "ready"`, retain the third failed attempt, and have
`current_dispatch is None`. No fourth dispatch is started before the same
third exception object is re-raised.

### 16.8 Owner-Loss Decision

Owner-loss/orphan recovery remains **Target**. The current repository has no
persisted `ActiveDispatchExecution`, PID/process-tree receipt, remote cleanup
authority, or verified callback that can prove an orphan Worker is dead.
Neither a missing in-memory owner nor lease expiry is sufficient evidence.

TC-13.18d.11c is therefore restricted to creator-alive failures after a live
execution was returned and cleanup was confirmed. Pre-ACK `dispatched`
recovery and restart/orphan recovery require a later contract backed by a
real cleanup authority.

### 16.9 Rate-Limit and Codex Boundary

Ordinary dispatch failure recovery is separate from provider rate limiting.
This contract adds no matching for `429`, quota, throttle, or backoff text; no
provider-specific classification; no wait/jitter; and no call to the
provider-neutral RateLimit service. Provider detection remains Target.

Codex runtime, Codex decoding, and Codex rate-limit classification remain
deferred pending observed evidence. Tests use mocks/fakes only.

### 16.10 Compatibility and Non-Goals

The following remain unchanged:

- `DispatchCycleRequest` and `DispatchCycleResult`: exactly nine fields each;
- `ActiveDispatchHandle`: exactly seven fields;
- `ActiveDispatchExecution`: public surface only `handle` and `wait()`;
- active cancellation: Current — TC-13.18d.9b;
- active supersession: Current — TC-13.18d.10b;
- quiescent cancellation/supersession and single escalated redispatch;
- Interface #22 core orchestration status: Current — TC-13.18d.13b;
  Codex / Provider 429 deferred (see §15.7);
- TC-13.19 status unchanged; TC-13.20 (Interface #24) is Current — TC-13.20b.

Non-goals are owner-loss cleanup, retry after fencing/release uncertainty,
unbounded retry, automatic replacement-task dispatch, provider rate-limit
wiring, Codex enablement, and changes to DispatcherGateway termination.

### 16.11 Production Split and Status

| Card | Scope | Status |
|---|---|---|
| **TC-13.18d.11a** | This failure recovery / bounded retry contract | **Contract Current** |
| **TC-13.18d.11b** | `DispatchFailedPayload` + `DISPATCH_FAILED` transition production | **Current** |
| **TC-13.18d.11c** | Creator-alive `run_bounded_dispatch_retry` production | **Current** |

Provider rate-limit wiring remains Target. Interface
#22 remains Target until the remaining runtime boundaries are implemented.

---

## 17. Owner-Loss Dispatch Recovery — Frozen Contract (Contract Current — TC-13.18d.12b)

TC-13.18d.12b freezes the contract for owner-loss dispatch recovery: a new
Orchestrator instance recovering a legacy active dispatch after the original
creator process has been lost. The production implementation is deferred to
TC-13.18d.12c.

This contract does **not** implement production recovery logic. It freezes
the types, safety criteria, recovery order, idempotency semantics, and
tombstone-racing rules. All criteria are derived from durable evidence
provided by the TC-13.18d.12a-pre2.x chain.

### 17.1 Purpose and Hard Gate

Owner-loss recovery is the path where the in-memory creator that held the
`ActiveDispatchExecution` is gone — crashed, killed, or restarted — and a
**different** Orchestrator instance must decide whether the legacy dispatch
can be safely declared failed and retried, or must be left alone.

The single hard gate is:

> Recovery may only proceed when durable evidence **independently proves**
> the old dispatch process tree is dead.

Lease expiry, heartbeat interruption, creator PID disappearance, supervisor
parent PID disappearance, Worker single-PID disappearance, receipt file age,
timeout, exception text, stdout/stderr, and provider exit code are **not**
sufficient — individually or in combination — to prove death.

### 17.2 Precise Public API — Frozen Types

#### 17.2.1 `OwnerLossRecoveryRequest` — Exactly 10 Fields

```python
@dataclass(frozen=True, slots=True)
class OwnerLossRecoveryRequest:
    task_id: str
    expected_revision: int
    expected_attempt: int
    expected_dispatch_id: str
    expected_generation_id: str
    recovery_event_id: str
    recovery_event_context: EventContext
    failure_kind: str
    evidence_refs: tuple[str, ...]
    retry_plan: BoundedDispatchRetryRequest | None
```

Frozen rules:

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state.
- All ten fields are typed; no `Any`, `dict`, `Mapping`, `Optional`, or
  string type escapes.
- `retry_plan` is either `None` or an exact `BoundedDispatchRetryRequest`
  (the existing frozen type from §16.3).
- `failure_kind` must be a member of the `DISPATCH_FAILED` frozen allowlist.
- `evidence_refs` must be non-empty, safe, and immutable.

#### 17.2.2 `OwnerLossRecoveryResult` — Exactly 6 Fields

```python
@dataclass(frozen=True, slots=True)
class OwnerLossRecoveryResult:
    task_id: str
    recovered_attempt: int
    recovered_dispatch_id: str
    process_liveness: ProcessLiveness
    recovery_transition: TransitionResult
    retry_result: BoundedDispatchRetryResult | None
```

Frozen rules:

- `frozen=True`, `slots=True`.
- All six fields are typed; no `Any`, `dict`, `Mapping`, or string escapes.
- `process_liveness` is the frozen `ProcessLiveness` enum from
  `dispatch_supervisor_evidence`.
- `retry_result` is `None` when `retry_plan is None`; it is populated only
  when `DISPATCH_FAILED` succeeded and `retry_plan` was supplied.

#### 17.2.3 Public Method

```python
async def recover_owner_lost_dispatch(
    self,
    request: OwnerLossRecoveryRequest,
) -> OwnerLossRecoveryResult:
    ...
```

No parallel exception hierarchy is created. All exceptions are existing
types from `WorkflowOrchestrator` (§10.2) and the underlying services
(`WorkerSlotLeaseError`, `TransitionCASConflictError`,
`DispatchSupervisorEvidenceError`, etc.).

If existing type dependencies would cause a circular import, the contract
requires the production implementation to resolve module ownership
explicitly. Using `Any`, `dict`, or string types to evade the dependency
graph is forbidden.

### 17.3 Recovery Preconditions — All 20 Must Hold

All conditions must be simultaneously true. If any single condition fails,
the path must fail-closed with zero writes, zero transitions, zero retries,
and zero new Workers:

1. `StateProvider.snapshot()` succeeds and returns a consistent snapshot.
2. Task exists exactly in the snapshot.
3. Task revision equals `expected_revision` exactly.
4. Task attempt equals `expected_attempt` exactly.
5. Task state is exactly `dispatched` or `in_progress`.
6. Task `current_dispatch` exists.
7. `dispatch_id` equals `expected_dispatch_id` exactly.
8. Durable receipt exists (via `read_dispatch_receipt`).
9. Receipt `task_id`/`revision`/`attempt`/`dispatch_id`/`generation_id`
   matches the request and the ledger snapshot exactly.
10. Receipt phase is an active phase that permits recovery
    (`SUPERVISOR_READY`, `WORKER_STARTED`, or `FINALIZING`).
11. Tombstone is absent, or if present must reach a frozen consensus with
    the receipt and ledger (see §17.8).
12. Creator exact identity probes `DEAD`.
13. `probe_dispatch_process_tree(receipt)` returns exactly `DEAD`.
14. Supervisor cannot be `ALIVE`.
15. Named Job Object active process count is not greater than zero
    (Windows; on POSIX, the process-group tree must be proven empty).
16. No Windows/API/permission evidence returns `UNKNOWN`.
17. `recovery_event_id` is globally unique.
18. `failure_kind` is a member of the `DISPATCH_FAILED` frozen allowlist.
19. `evidence_refs` is non-empty, safe, and immutable.
20. No other successful recovery event exists for this same dispatch.

### 17.4 Three-State Handling

#### `ALIVE`

- Do **not** write `DISPATCH_FAILED`.
- Do **not** clear `current_dispatch`.
- Do **not** start retry.
- Return or raise a clear "still running" result.
- Do **not** kill the old supervisor or Worker.

#### `UNKNOWN`

- Must fail-closed.
- Zero transitions.
- Zero retries.
- Zero Workers.
- `UNKNOWN` is never downgraded to `DEAD`.
- No timeout-based re-guessing is permitted.

#### `DEAD`

Only when **all** 20 preconditions in §17.3 hold may the recovery
transition proceed.

### 17.5 Exact Recovery Order — Frozen

```text
1.  Validate request (types, field presence, allowlists)
2.  StateProvider.snapshot() — obtain consistent read
3.  Bind task + current_dispatch from snapshot
4.  Read durable receipt + tombstone
5.  Validate exact identities (receipt vs request vs ledger)
6.  Probe creator exact identity (PID + creation time + boot_id)
7.  probe_dispatch_process_tree(receipt)
8.  Acquire single recovery authority / fence
9.  Repeat StateProvider.snapshot() + evidence checks (TOCTOU close)
10. Apply DISPATCH_FAILED with lease=None
11. Optionally invoke existing bounded retry
12. Return OwnerLossRecoveryResult
```

Step 9 (second check) closes the TOCTOU window between first probe and the
state write. Recovery must **not** write state after the first probe
without re-validating snapshot, task identity, receipt/tombstone state, and
process tree liveness.

### 17.6 Recovery Authority

The frozen authority rules are:

- The PM/Orchestrator holds the existing state-transition lock.
- The old `WorkerSlotLease` is **not** reused.
- `DISPATCH_FAILED` is applied with `lease=None`.
- The authority depends on `ControlPlaneTransition`'s CAS and dispatch
  identity fencing.
- No second global lock is created.
- Deleting the old lease file to claim ownership is forbidden.

**Blocking item for TC-13.18d.12c**: If the existing lock does not provide
an atomic boundary across "second check → transition", the production
implementation must raise this as a blocking issue. The contract must not
paper over the gap.

### 17.7 Pre-ACK and Post-ACK Paths

#### Pre-ACK

Task state is typically `dispatched`. Before allowing recovery, the
implementation must prove:

- `TASK_DISPATCHED` event exists in the ledger.
- ACK event does **not** exist.
- Receipt has reached a phase that permits recovery
  (`SUPERVISOR_READY` or later).
- Process tree is `DEAD`.
- No late-arriving Worker start or ACK that has not yet been persisted
  could create an identity collision.

#### Post-ACK

Task state is typically `in_progress`. Before allowing recovery, the
implementation must prove:

- ACK event exists.
- Receipt, ledger, and ACK dispatch identity are consistent.
- Process tree is `DEAD`.
- No successful delivery or finalizer tombstone conflicts with recovery.

Both paths use the same canonical `DISPATCH_FAILED` transition. No new
recovery event type is created for pre-ACK vs post-ACK.

### 17.8 Tombstone and Completion Races — Frozen Matrix

| Durable state | Conclusion |
|---|---|
| Receipt active, tombstone absent, tree `ALIVE` | Do not recover |
| Receipt active, tombstone absent, tree `UNKNOWN` | Fail-closed |
| Receipt active, tombstone absent, tree `DEAD` | Continue remaining checks |
| Receipt `FINALIZING`, tombstone absent | Fail-closed; resolve finalization first |
| Receipt `FINALIZED`, legal tombstone present | Do not recover |
| Receipt `FINALIZED`, tombstone missing | Evidence corruption; fail-closed |
| Tombstone success but ledger not yet updated | Do not treat as owner-loss retry |
| Delivery submitted or accepted/integrated | Do not recover dispatch |
| Recovery event already exists and byte-exact | Idempotent replay |
| Recovery event content diverges | Duplicate/fencing error |

### 17.9 Retry Boundary

Owner-loss recovery does **not** re-implement retry.

When `retry_plan is None`:

- Only `DISPATCH_FAILED` is applied.
- Task returns to `ready`.
- No Worker is started.

When `retry_plan` is present:

- `DISPATCH_FAILED` must succeed first.
- Then the existing `run_bounded_dispatch_retry()` is called.
- Fresh attempt = old attempt + 1.
- Fresh `dispatch_id` and `generation_id` must differ from the old ones.
- At most 1..3 attempts (the existing bounded retry ceiling).
- No hidden retry is added.
- No exception-text classification is performed.
- Recovery replay does not start retry again.

**Frozen result semantics when transition succeeds but retry fails:**
The `DISPATCH_FAILED` transition is **not** rolled back. The task is left
in `ready` state. The retry failure propagates to the caller. Replay
recognises the successful transition and does not retry again.

### 17.10 Idempotency and Concurrency — Frozen Rules

1. Replaying the same owner-loss request must not generate a second
   recovery event.
2. The same dead dispatch can have at most one recovery winner.
3. When two recoverers race, the loser must be rejected by lock/CAS/fencing.
4. A late ACK, delivery, or heartbeat from the old Worker cannot alter the
   new attempt.
5. After the new attempt starts, the old generation's receipt/evidence
   cannot bind to the new dispatch.
6. When recovery transition succeeded but the caller crashed, a subsequent
   call must recognise the existing success and must not repeat recovery.
7. When retry has already started, replay must not start a second identical
   attempt.

### 17.11 Safety — Error Messages

Exception messages must not contain:

- Task payload.
- `dispatch_id`.
- `generation_id`.
- PID.
- Job name.
- Workspace path.
- Receipt or tombstone content.
- stdout/stderr.
- API key or token.
- User-generated text.
- `repr()` of a malicious object.

Only fixed field names and fixed error categories are permitted.

### 17.12 Production Split and Status

| Card | Scope | Status |
|---|---|---|
| **TC-13.18d.12b** | This owner-loss recovery contract freeze | **Contract Current** |
| **TC-13.18d.12c** | Owner-loss transition-only recovery production implementation | **Current** |
| **TC-13.18d.12c.1** | Owner-loss automatic retry durable contract freeze | **Contract Current** |
| **TC-13.18d.12c.2** | Owner-loss automatic retry production implementation | **Current** |

Durable supervisor evidence remains **Current** —
TC-13.18d.12a-pre2.1 / pre2.2 / pre2.2.1.
Windows Job Object containment remains **Current** —
TC-13.18d.12a-pre2.2.
Interface #22 core orchestration is **Current — TC-13.18d.13b**; Codex / Provider 429 deferred.
Owner-loss transition-only recovery runtime is **Current** — TC-13.18d.12c.
The automatic-retry durable contract is **Contract Current** —
TC-13.18d.12c.1. Automatic-retry runtime is **Current** —
TC-13.18d.12c.2.
Owner-loss automatic retry is **Current**.

Historical TC-13.18d.12b freeze statement (superseded by the production
status above): Owner-loss recovery runtime remains **Target** —
TC-13.18d.12c.

---

## 18. Owner-Loss Automatic Retry Durable State Machine — Frozen Contract (Contract Current — TC-13.18d.12c.1)

TC-13.18d.12c.1 freezes the durable protocol required before a recovered
dispatch may automatically start its next attempt. It changes no production
module and starts no Worker. Runtime implementation is deferred to
TC-13.18d.12c.2.

### 18.1 Hard Safety Boundary

- A canonical task being `ready` is never sufficient authority to retry.
- An in-memory boolean, exception text, stdout/stderr, provider name, exit
  code, lease expiry, or a single PID observation is never retry evidence.
- The next attempt and dispatch identities must be durably reserved before
  any supervisor or Worker is started.
- Reservation is bound to the canonical `DISPATCH_FAILED` event, failed
  attempt, failed dispatch, recovery generation, task revision, and exact
  retry plan content digest.
- Retry execution and `run_dispatch_cycle()` occur outside the state lock.
- `ALIVE` still means no recovery write or retry. `UNKNOWN` remains
  fail-closed. Only exact `DEAD` plus every TC-13.18d.12c predicate permits
  the recovery transition and later reservation.

### 18.2 Exact Forward-Only Phases

```python
class OwnerLossRetryPhase(str, Enum):
    RECOVERY_TRANSITION_PENDING = "RECOVERY_TRANSITION_PENDING"
    RECOVERY_TRANSITION_COMMITTED = "RECOVERY_TRANSITION_COMMITTED"
    RETRY_RESERVED = "RETRY_RESERVED"
    RETRY_STARTED = "RETRY_STARTED"
    RETRY_FINALIZING = "RETRY_FINALIZING"
    RETRY_FINALIZED = "RETRY_FINALIZED"
```

The only legal phase edges are:

```text
RECOVERY_TRANSITION_PENDING -> RECOVERY_TRANSITION_COMMITTED
RECOVERY_TRANSITION_COMMITTED -> RETRY_RESERVED
RETRY_RESERVED -> RETRY_STARTED
RETRY_STARTED -> RETRY_FINALIZING
RETRY_FINALIZING -> RETRY_FINALIZED
```

Phases never move backward, skip an edge, or change generation. A write with
the same generation and phase is legal only as byte-exact replay. A phase
or generation overwrite is rejected.

### 18.3 `OwnerLossRetryReceipt` — Exactly 24 Fields

```python
@dataclass(frozen=True, slots=True)
class OwnerLossRetryReceipt:
    schema_version: str
    task_id: str
    revision: int
    failed_attempt: int
    failed_dispatch_id: str
    recovery_event_id: str
    recovery_generation_id: str
    next_attempt: int
    next_dispatch_id: str
    next_dispatch_event_id: str
    phase: OwnerLossRetryPhase
    creator_pid: int
    creator_creation_time: str
    creator_boot_id: str
    supervisor_pid: int | None
    supervisor_creation_time: str | None
    supervisor_boot_id: str | None
    worker_pid: int | None
    worker_creation_time: str | None
    worker_boot_id: str | None
    reserved_at: str
    started_at: str | None
    finalized_at: str | None
    content_digest: str
```

The type is exactly frozen and slotted. Public fields contain no `Any`,
`dict`, `Mapping`, mutable collection, or untyped metadata. The three
creator fields are required at reservation. Each optional supervisor or
Worker identity is all-present or all-absent. `next_attempt` is exactly
`failed_attempt + 1`; it must be in the existing range 1..3, so a failed
third attempt can never reserve or start attempt four.

`content_digest` is lowercase SHA-256 over canonical UTF-8 bytes of every
field except `content_digest`. Canonical serialization has frozen field
order, enum values as strings, no insignificant whitespace, and no
platform-dependent formatting. It binds the exact retry plan and identity;
it is not derived from exception text or process output.

### 18.4 Canonical Identity and Atomic Reservation

The following ordered boundary is frozen:

1. Validate the exact request and proposed receipt without writes.
2. Acquire the existing state lock.
3. Under that lock, re-read the canonical task, recovery event, failed
   dispatch receipt/tombstone, and any owner-loss retry receipt.
4. Require the byte-exact `DISPATCH_FAILED` event to be committed, task
   state exactly `ready`, `current_dispatch is None`, task revision exact,
   and task attempt still equal to `failed_attempt`.
5. Bind `task_id`, revision, failed attempt/dispatch, recovery event,
   recovery generation, next attempt/dispatch/event, and content digest.
6. Atomically persist the single `RETRY_RESERVED` receipt, or return the
   same bytes for an exact replay. Divergence rejects without a write.
7. Release the state lock.
8. Only a valid durable `RETRY_RESERVED` receipt authorizes starting its
   exact next dispatch identity.
9. Start the supervisor/Worker outside the state lock, then advance through
   `RETRY_STARTED`, `RETRY_FINALIZING`, and `RETRY_FINALIZED` using
   forward-only atomic evidence writes.

No code holding the state lock may call `run_dispatch_cycle()`,
`run_bounded_dispatch_retry()`, a provider, a supervisor, or a Worker.
Within the lock, the path performs only validation, exact replay, and the
atomic reservation write.

### 18.5 Crash-Window Decision Matrix

Each window has exactly one required disposition:

| Crash or race window | Required disposition |
|---|---|
| Before `DISPATCH_FAILED` commits | `resume` transition-only recovery; never reserve retry early |
| After `DISPATCH_FAILED`, before reservation | `resume` at locked reservation with the same recovery generation |
| Reservation atomic write is interrupted or malformed | `fail-closed` |
| Valid `RETRY_RESERVED`, before supervisor/Worker start | `resume` the exact reserved identity; never allocate another ID |
| Supervisor starts before durable receipt advances | `fail-closed` |
| Worker starts before `RETRY_STARTED` is durable | `fail-closed` |
| Retry is ACKed, then owner crashes | `resume` only the same `RETRY_STARTED` execution/finalization; never redispatch |
| Crash before retry finalizer publishes | `fail-closed` unless exact existing execution evidence can resume finalization |
| Crash after `RETRY_FINALIZED` is durable | `byte-exact replay` |
| Tombstone write and phase advance are separated by a crash | `fail-closed` until both durable records agree |
| PID reuse, boot-id change, or permission failure | `fail-closed` (`UNKNOWN` is never `DEAD`) |
| Two recoverers propose the same generation and bytes | `byte-exact replay`; one reservation write |
| Two generations contend for one failed dispatch | `reject`; one generation wins |
| Stale recovery generation replays | `reject` |

`resume` never means allocate a new identity. It means continue the exact
durably reserved identity at the permitted phase. A fail-closed disposition
performs no retry start and no canonical state mutation.

### 18.6 Idempotency, Concurrency, and Replay

- Same recovery generation plus identical canonical bytes returns the same
  `OwnerLossRetryReceipt` bytes and never creates another dispatch ID.
- Same generation with different phase, identity, plan, or digest rejects.
- Different generations competing for one failed dispatch have exactly one
  reservation winner; the loser rejects.
- Stale canonical CAS or stale recovery generation performs zero writes and
  starts zero Workers.
- A late success/tombstone/ACK from the failed attempt cannot reopen it or
  bind to the reserved retry.
- A retry may start only from exact durable `RETRY_RESERVED`; later phases
  never start another Worker.
- Attempt three exhaustion is a no-op for retry creation: no reservation,
  no fourth dispatch, no Worker.

### 18.7 Status

| Capability | Status |
|---|---|
| TC-13.18d.12c transition-only owner-loss recovery | **Current** |
| Owner-loss automatic retry durable contract | **Contract Current — TC-13.18d.12c.1** |
| Owner-loss automatic retry runtime | **Current — TC-13.18d.12c.2** |
| Interface #22 | **Current — TC-13.18d.13b** (core orchestration); Codex / Provider 429 deferred |

This contract's retry runtime is Current — TC-13.18d.12c.2.

## 19. WorktreeLifecycleManager Delegation Boundary (TC-13.25a)

WorktreeLifecycleManager Contract is **Contract Current — TC-13.25a**;
Store/reconciliation is **Current — TC-13.25b**; create/release runtime is
**Current — TC-13.25c**; adversarial verification is **Verified — TC-13.25d**.

WorkflowOrchestrator does not create, switch, remove, prune, scan, or reconcile
Git worktrees.  It never invokes `git worktree add`, `git worktree remove`,
`git worktree list`, `git checkout`, `git reset`, or `git clean`.  A future
integration may consume only a frozen validated `WorktreeLifecycleResult`
from the independent Manager.

The Manager's durable reservation must precede any Git worktree side effect.
Its Store lock is released before Git, Worker, heartbeat, provider, model,
API, or network calls.  `git worktree list --porcelain -z` is the sole Git
worktree inventory source.  Dirty, divergent, reparse/symlink, permission
UNKNOWN, live process/lease, or identity-mismatched worktrees are never
released.

Interface #22 remains **Current — TC-13.18d.13b** with unchanged ownership;
TC-13.25 adds no WorkflowOrchestrator production method or state field.

## 20. Admitted Scheduler Dispatch Adoption Boundary (TC-13.26a)

The Scheduler-to-Worker Handoff Runtime Contract is **Contract Current —
TC-13.26a**; runtime is **Target — TC-13.26b** and E2E verification is
**Target — TC-13.26c**. A future explicit
`start_admitted_dispatch_execution` entry may adopt an existing canonical
dispatch, lease, and handoff identity. It must not acquire a second lease,
apply `TASK_DISPATCHED`, change attempt, or call the private dispatch cycle.
Interface #22 ownership of ACK, cancellation, supersession, delivery, and
finalization remains unchanged.
