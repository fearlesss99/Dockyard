# Dockyard Post-Delivery Review Evidence Contract

Status: **Contract Current — TC-13.29l.5f**
Store: **Current — TC-13.29l.5g.1**
Owner Interface #22 runtime: **Current — TC-13.29l.5g.2a**
Durable fail/blocked owner runtime: **Current — TC-13.29l.5g.2b**
Dockyard composition runtime: **Normal-path Current — TC-13.29l.5g.2c.2;
mid-flight crash resume Target**
Owner plan projection contract: **Contract Current — TC-13.29l.5f.3**
Schema: `dockyard.post-delivery-review/v1`

This contract freezes the crash-safe boundary between a finalized durable
Worker delivery and the existing MAD audit, acceptance/remediation, and
integration owners. It does not implement MAD, acceptance, return, block,
integration, Worker execution, or a new WorkflowOrchestrator lifecycle.

## 1. Ownership and invariants

- Canonical task/event/outbox evidence comes only from StateProvider and
  ControlPlaneTransitionService.
- Delivery identity comes only from the validated Worker `DeliveryReceipt`,
  immutable delivery event, and finalized external-Worker/handoff evidence.
- Managed-worktree identity and Git ancestry come only from
  WorktreeLifecycleStore plus exact Git verification.
- Interface #20 MadAuditGateway remains the sole audit execution owner.
- Interface #22 WorkflowOrchestrator remains the sole acceptance,
  remediation, block, and integration lifecycle owner.
- Dockyard owns only local review reservation, durable evidence binding, and
  owner composition. It never fabricates `DispatchCycleResult`, reruns a
  finalized Worker, or interprets exception text, stdout/stderr, prompts,
  provider/model names, PIDs, lease expiry, or UI state as review evidence.
- API keys, credentials, provider clients, process handles, prompts, report
  bodies, stdout/stderr, and absolute worktree paths are forbidden in every
  durable review document.

## 2. Frozen public types

All public types are `frozen=True, slots=True`. Public annotations contain no
`Any`, `object`, `dict`, `Mapping`, `list`, `set`, mutable collection,
callback/factory, provider/model client, process handle, or secret-bearing
object.

### 2.1 `DockyardPostDeliveryReviewRequest` — exactly 38 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `project_id` | `str` |
| 4 | `task_id` | `str` |
| 5 | `revision` | `int` |
| 6 | `attempt` | `int` |
| 7 | `dispatch_id` | `str` |
| 8 | `dispatch_generation_id` | `str` |
| 9 | `queue_id` | `str` |
| 10 | `schedule_receipt_id` | `str` |
| 11 | `admission_plan_digest` | `str` |
| 12 | `handoff_id` | `str` |
| 13 | `external_worker_receipt_digest` | `str` |
| 14 | `dispatch_event_id` | `str` |
| 15 | `dispatch_event_digest` | `str` |
| 16 | `acknowledge_event_id` | `str` |
| 17 | `acknowledge_event_digest` | `str` |
| 18 | `delivery_event_id` | `str` |
| 19 | `delivery_event_digest` | `str` |
| 20 | `delivery_receipt_digest` | `str` |
| 21 | `implementation_commit` | `str` |
| 22 | `report_commit` | `str` |
| 23 | `worktree_id` | `str` |
| 24 | `worktree_identity_digest` | `str` |
| 25 | `branch` | `str` |
| 26 | `base_commit` | `str` |
| 27 | `audit_input_digest` | `str` |
| 28 | `audit_config_id` | `str` |
| 29 | `acceptance_event_id` | `str` |
| 30 | `acceptance_request_digest` | `str` |
| 31 | `integration_event_id` | `str | None` |
| 32 | `integration_request_digest` | `str | None` |
| 33 | `return_event_id` | `str | None` |
| 34 | `requeue_event_id` | `str | None` |
| 35 | `block_event_id` | `str | None` |
| 36 | `routing_digest` | `str` |
| 37 | `reserved_at` | `str` |
| 38 | `content_digest` | `str` |

`attempt` is in `1..3`; attempt four is rejected before reservation, MAD,
lease, canonical transition, Worker, or Git write. `integration_event_id` and
`integration_request_digest` are both null or both present. Pass routing
requires all return/requeue/block IDs null. Fail routing requires return and
requeue IDs and a null block ID. Blocked routing requires only block ID.

The request binds canonical digests rather than mutable objects. It is not a
serialized `DispatchCycleResult` and contains no `WorkerResult`,
`WorkerOutput`, `TransitionResult`, live lease, or provider output.

### 2.2 `DockyardPostDeliveryReviewReceipt` — exactly 27 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `review_id` | `str` |
| 3 | `operation_id` | `str` |
| 4 | `request_content_digest` | `str` |
| 5 | `task_id` | `str` |
| 6 | `revision` | `int` |
| 7 | `attempt` | `int` |
| 8 | `dispatch_id` | `str` |
| 9 | `dispatch_generation_id` | `str` |
| 10 | `phase` | `DockyardPostDeliveryReviewPhase` |
| 11 | `outcome` | `DockyardPostDeliveryReviewOutcome` |
| 12 | `audit_result_id` | `str | None` |
| 13 | `audit_result_digest` | `str | None` |
| 14 | `audit_verdict` | `str | None` |
| 15 | `acceptance_event_id` | `str | None` |
| 16 | `integration_event_id` | `str | None` |
| 17 | `return_event_id` | `str | None` |
| 18 | `requeue_event_id` | `str | None` |
| 19 | `block_event_id` | `str | None` |
| 20 | `reserved_at` | `str` |
| 21 | `mad_started_at` | `str | None` |
| 22 | `mad_completed_at` | `str | None` |
| 23 | `review_applied_at` | `str | None` |
| 24 | `integration_applied_at` | `str | None` |
| 25 | `finalized_at` | `str | None` |
| 26 | `updated_at` | `str` |
| 27 | `content_digest` | `str` |

The receipt never stores an audit transcript, prompt, report body, stdout,
stderr, credential, provider configuration, or worktree path. `audit_verdict`
is exactly `pass`, `fail`, or `blocked` after `MAD_COMPLETED`; no message-text
classification is permitted.

### 2.3 Exact forward-only phase enum

```text
DELIVERY_BOUND
REVIEW_RESERVED
MAD_STARTED
MAD_COMPLETED
REVIEW_APPLIED
INTEGRATION_APPLIED
FINALIZED
```

Phases advance by at most one position. `INTEGRATION_APPLIED` is required only
for a pass request carrying integration identity; otherwise the legal path is
`REVIEW_APPLIED -> FINALIZED`. A phase never moves backward, skips required
evidence, changes generation, or reopens after finalization.

### 2.4 Exact outcome enum

```text
RESERVED
AUDIT_PASS
AUDIT_FAIL
AUDIT_BLOCKED
ACCEPTED
RETURNED
BLOCKED
INTEGRATED
RECOVERY_REQUIRED
REJECTED
FINALIZED
```

## 3. Durable path and canonical bytes

```text
.agentdesk/runtime/dockyard-review/requests/<review_id>.yaml
.agentdesk/runtime/dockyard-review/receipts/<review_id>.yaml
```

Files are local-runner-only and gitignored. Encoding is canonical UTF-8 YAML,
LF, exactly one final LF, exact field order, no aliases/tags/duplicate keys,
and `sha256:` over all fields except `content_digest`. Writes use atomic
same-directory replacement plus file and parent-directory durability.
Symlinks, junctions, reparse points, extra sibling files, non-regular files,
and path substitution fail closed.

Same `review_id` plus byte-identical request is byte-exact replay. Same identity
plus different bytes is a typed conflict. A finalized receipt is immutable.

## 4. Evidence validation order

Before reservation or MAD execution, the future runtime must:

1. validate exact request type, field order, digest, attempt, and routing;
2. read the canonical task and exact dispatch/ACK/delivery events;
3. read the exact admission plan, handoff, process tombstone, external-Worker
   receipt, and delivery receipt projection;
4. prove task/revision/attempt/dispatch/generation/event identities agree;
5. read the READY managed-worktree record and verify branch/base/path digest;
6. verify implementation and report commits exist in that worktree, are
   distinct, and have the required ancestry;
7. validate audit, acceptance, remediation, block, and optional integration
   request digests without executing them;
8. persist/replay request and `REVIEW_RESERVED` receipt under only the local
   review-store lock, then release it;
9. call MAD and Interface #22 outside every store/state/Git/lease lock.

Canonical event state progression is exact:

```text
TASK_DISPATCHED: ready -> dispatched
DISPATCH_ACKNOWLEDGED: dispatched -> in_progress
DELIVERY_SUBMITTED: in_progress -> review_ready
DELIVERY_ACCEPTED: review_ready -> accepted
CHANGE_INTEGRATED: accepted -> integrated
```

Fail uses `DELIVERY_RETURNED` followed by `TASK_REQUEUED`; blocked uses the
existing `TASK_BLOCKED` owner. No transition identity is generated after MAD.

## 5. Lock and execution boundary

The review-store lock protects only request/receipt validation and one
forward phase write. It is never nested with Git, canonical state,
PortfolioScheduler, WorkerSlotLease, WorktreeLifecycle, dispatch-supervisor,
external-Worker, MAD, or WorkflowOrchestrator locks.

MAD, review lease acquisition, acceptance/remediation/block/integration
transitions, Worker/process operations, Git commands, provider/model/API calls,
and network calls are forbidden while the review-store lock is held. The local
runtime may invoke existing owners only after releasing it.

## 6. Crash and replay matrix

| Window | Required disposition |
|---|---|
| delivery event committed, external-Worker receipt absent | `RECOVERY_REQUIRED`; never rerun Worker implicitly |
| external-Worker receipt committed, review request absent | resume exact validation and reservation |
| request write interrupted or noncanonical | fail-closed |
| request committed, receipt absent | byte-exactly create `DELIVERY_BOUND` |
| `DELIVERY_BOUND`, before reservation | resume exact `REVIEW_RESERVED` |
| reservation committed, MAD not started | resume MAD once |
| MAD process ALIVE | no-op / `RECOVERY_REQUIRED`; no second MAD process |
| MAD process UNKNOWN, PID reuse, boot change, or permission denial | fail-closed |
| MAD process DEAD without durable result | `RECOVERY_REQUIRED`; recovery owner decides restart |
| `MAD_STARTED`, result file absent | decide only from typed liveness evidence |
| MAD result durable, receipt lags | adopt only exact result ID/digest/verdict |
| MAD result identity or digest divergent | reject |
| pass verdict, acceptance absent | resume exact `DELIVERY_ACCEPTED` |
| fail verdict, return/requeue absent | resume exact return then requeue owner |
| blocked verdict, block event absent | resume exact blocked-audit owner |
| canonical review transition committed, receipt lags | adopt exact event only |
| acceptance committed, integration requested but absent | resume exact `CHANGE_INTEGRATED` |
| integration committed, receipt lags | adopt exact integration event |
| integration not requested after accepted | finalize accepted result |
| valid `FINALIZED` receipt | byte-exact replay/no-op |
| same generation and identical bytes race | one winner, identical replay |
| same identity with divergent bytes | typed reject before MAD/canonical write |
| stale task/revision/attempt/dispatch/generation | fail-closed |
| attempt 4 | reject before every write and owner call |

Each window has exactly one disposition. UNKNOWN is never treated as DEAD;
lease expiry alone is never liveness or review authorization.

## 7. Verdict routing

| Verdict | Required owner action |
|---|---|
| `pass`, no integration identity | exact `DELIVERY_ACCEPTED`, then finalize |
| `pass`, integration identity present | exact `DELIVERY_ACCEPTED`, then exact `CHANGE_INTEGRATED`, then finalize |
| `fail` | exact `DELIVERY_RETURNED`, then `TASK_REQUEUED`; no acceptance/integration |
| `blocked` | exact existing blocked-audit transition; no acceptance/integration |
| missing/unknown/divergent | fail-closed; zero canonical write |

The verdict comes only from a validated durable `MadAuditGatewayResult` digest
published before the receipt advances to `MAD_COMPLETED`.

## 8. Status

| Capability | Status |
|---|---|
| Dockyard post-delivery review evidence contract | **Contract Current — TC-13.29l.5f** |
| Dockyard post-delivery review Store | **Current — TC-13.29l.5g.1** |
| Durable Interface #22 acceptance owner runtime | **Current — TC-13.29l.5g.2a** |
| Durable remediation/blocked owner input contract | **Contract Current — TC-13.29l.5f.2** |
| Durable remediation/blocked owner runtime | **Current — TC-13.29l.5g.2b** |
| Dockyard owner plan projection | **Contract Current — TC-13.29l.5f.3; Runtime Target — TC-13.29l.5g.2c.1** |
| Dockyard Store-to-owner composition | **Normal-path Current — TC-13.29l.5g.2c.2; mid-flight crash resume Target** |
| Dockyard local closed-loop E2E | **Blocked — TC-13.29l** |
| Interface #20 MadAuditGateway ownership | **Unchanged** |
| Interface #22 WorkflowOrchestrator ownership | **Unchanged** |

This contract does not make the complete Scheduler runtime, Dockyard runtime,
or TC-13.29l Current.

## 9. Owner-neutral Interface #22 projection repair (TC-13.29l.5f.1)

The caller may translate a fully validated durable request into the frozen
owner-neutral `DurableDeliveryReviewEvidence` and
`DurableAcceptanceCycleRequest` types from
`workflow-orchestrator-contract.md`. The translation occurs in memory after
the review-store lock is released. WorkflowOrchestrator never imports this
Dockyard contract, reads Dockyard review files, or accepts a Dockyard-specific
type.

The projection carries the typed `DeliveryReceipt`, validated runtime
workspace `Path`, dispatch/ACK/delivery event IDs and digest, generation,
external-Worker receipt digest, worktree ID, and exact double commits. It does
carry the exact validated `WorkerKind` from admission/handoff/Worker evidence. It does
not carry or fabricate `DispatchCycleResult`, `WorkerResult`, `WorkerOutput`,
or `TransitionResult`.

Contract repair status: **Current — TC-13.29l.5f.1**. The owner-neutral types
and acceptance-owner method are **Current — TC-13.29l.5g.2a**. Translation
from the Dockyard Store plus fail/blocked routing remains
**Target — TC-13.29l.5g.2c.2**.

## 10. Durable fail/blocked owner projection (TC-13.29l.5f.2)

Audit `fail` projects the validated receipt and transition identities into
`DurableDeliveryRemediationRequest`; audit `blocked` projects them into
`DurableBlockedAuditRequest`. Both owner-neutral types are frozen by Interface
#22 and replace only the unavailable in-memory `DispatchCycleResult`. Dockyard
does not apply `DELIVERY_RETURNED`, `TASK_REQUEUED`, or `TASK_BLOCKED` itself.

The contract is **Current — TC-13.29l.5f.2**. Owner runtime is **Current —
TC-13.29l.5g.2b** and Dockyard Store-to-owner normal-path composition is
**Current — TC-13.29l.5g.2c.2**; mid-flight crash resume remains Target.

## 11. Deterministic owner plan projection (TC-13.29l.5f.3)

`DockyardPostDeliveryOwnerPlan` is an in-memory `frozen=True, slots=True`
type with exactly twelve fields:

| # | Field | Type |
|---:|---|---|
| 1 | `review_id` | `str` |
| 2 | `delivery_evidence` | `DurableDeliveryReviewEvidence` |
| 3 | `audit_input` | `MadAuditGatewayInput` |
| 4 | `acceptance_transition_request` | `TransitionRequest` |
| 5 | `git_integration_intent` | `DockyardGitIntegrationIntent | None` |
| 6 | `return_transition_request` | `TransitionRequest | None` |
| 7 | `requeue_transition_request` | `TransitionRequest | None` |
| 8 | `block_transition_request` | `TransitionRequest | None` |
| 9 | `worker_kind` | `WorkerKind` |
| 10 | `holder_instance_id` | `str` |
| 11 | `audit_config_id` | `str` |
| 12 | `content_digest` | `str` |

Public annotations contain no `Any`, `object`, `dict`, `Mapping`, mutable
collection, callback, provider/model client, process handle, stdout/stderr,
prompt body, report body, credential, or API key.

The projection runtime accepts the exact typed evidence reconstructed from the
admission plan, Scheduler and
handoff receipts, external-Worker receipt, canonical dispatch/ACK/delivery
events, READY managed-worktree record, and double commits. It reconstructs the
typed `DeliveryReceipt`, `MadAuditGatewayInput`, and every verdict-route
`TransitionRequest`, then recomputes every digest frozen in the 38-field review
request. No digest is trusted as a substitute for its source bytes.

`audit_config_id` resolves only through a typed local-runner configuration
registry. The review Store and browser never receive the config body or a
credential. Projection performs no MAD, owner call, lease, transition, Worker,
process, model, API, or network action and holds no Store/state/Git/lease lock
while entering another owner.

Identical validated evidence produces the same plan digest. Missing,
noncanonical, stale, or divergent evidence fails closed before MAD or a
canonical write. Contract status is repaired and **Current —
TC-13.29l.5g.2c.1**; projection runtime is **Current —
TC-13.29l.5g.2c.1**, Store-to-owner
normal-path composition is **Current — TC-13.29l.5g.2c.2**, mid-flight crash
resume remains **Target**, and full TC-13.29l remains
Blocked.

WorkerKind evidence binding is **Current — TC-13.29l.5f.2a**. The durable
projection and every owner request bind the exact validated WorkerKind before
MAD, lease, escalation, or canonical transition.

The repaired plan never predicts a `CHANGE_INTEGRATED` payload before Git
runs. The frozen two-field `DockyardGitIntegrationIntent` binds the future
`event_id` plus the exact canonical `GitIntegrationRequest`;
`integration_request_digest` binds that request and acceptance always receives
`integration_transition_request=None`. Only the finalized Git receipt path in
TC-13.29l.5h.3 may construct the later integration transition.

## 12. Post-Git durable integration completion (TC-13.29l.5h.3)

The pass route no longer needs to predict an integration commit before Git
runs. After the dedicated Git owner finalizes its typed receipt, Dockyard may
project only the exact receipt identity into `DurableGitIntegrationEvidence`
and construct `DurableIntegrationCompletionRequest`. Interface #22 validates
the accepted result, durable delivery identity, finalized Git identity,
integrated commit, event uniqueness, receipt digest and accepted-state CAS,
then publishes one lease-free `CHANGE_INTEGRATED`.

This path does not rerun MAD, `DELIVERY_ACCEPTED`, a Worker, or Git. Durable
integration completion owner status is **Current — TC-13.29l.5h.3**.
Owner-plan projection runtime is **Current — TC-13.29l.5g.2c.1**;
Normal-path Store-to-owner composition is **Current — TC-13.29l.5g.2c.2**;
mid-flight crash resume remains **Target**. The full
TC-13.29l E2E remains
**Blocked**.

## 13. Store-to-owner normal-path composition (TC-13.29l.5g.2c.2)

`run_dockyard_post_delivery_composition()` projects the canonical review
request, advances the review receipt outside owner calls, invokes the durable
acceptance owner with a null integration transition, routes `fail` and
`blocked` to their existing durable owners, and routes `pass` through the real
Git owner followed by the TC-13.29l.5h.3 integration-completion owner. It never
implements MAD, lifecycle transitions, Git operations, Worker execution or
provider behavior itself.

The initial user-visible normal path is **Current — TC-13.29l.5g.2c.2**.
Because the current receipt stores only an audit-result digest after
`MAD_COMPLETED`, re-entry from an already advanced non-final receipt returns a
typed `RECOVERY_REQUIRED` result with zero blind owner replay. Persisting the
complete typed audit result for automatic mid-flight resume remains a bounded
post-MVP target. Finalized receipt replay is a no-op. Full TC-13.29l E2E remains
**Blocked** until the real browser/local-runner scenario passes.
