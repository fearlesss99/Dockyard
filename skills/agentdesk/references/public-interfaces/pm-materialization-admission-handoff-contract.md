# PM Materialization-to-Scheduler Admission Handoff Contract

Status: **Contract Current — TC-13.29l.5a.1**
Runtime: **Current — TC-13.29l.5d.4**
Schema: `agentdesk.pm-materialization-admission-handoff/v1`

This contract freezes the durable boundary between a PM plan that has reached
`MATERIALIZED` and PortfolioScheduler admission. It does not implement that
boundary, start a Worker, call a provider/model/API, or widen Dockyard,
WorkflowOrchestrator, or PortfolioScheduler ownership.

## 1. Ownership

Ownership is exact and non-overlapping:

| Evidence or operation | Sole owner |
|---|---|
| approved plan, materialized task-card bytes, and `MaterializedTaskEvidence` | PM plan runtime |
| Git/card evidence binding and handoff receipt | materialization-admission handoff owner |
| canonical task registration and its generation | canonical task-state service |
| `queue_id`, `enqueue_sequence`, QueueEntry, and queue replay | PortfolioScheduler store |
| selection and admission | PortfolioScheduler runtime |
| ACK, cancellation, delivery, acceptance, and integration | WorkflowOrchestrator |
| project registration, projection, and user commands | Dockyard |

PM does not enqueue. The handoff owner does not select, admit, acquire a lease,
or start a Worker. Dockyard coordinates the user-facing flow but never writes
Git evidence, canonical task state, QueueEntry, or admission evidence itself.

## 2. Frozen public types

Every public type is `frozen=True, slots=True`. Public annotations contain no
`Any`, `object`, `dict`, `Mapping`, `list`, `set`, callback, provider, model,
filesystem handle, or mutable collection.

### 2.0 `DispatchApprovalAuthorization` — exactly 8 fields

`schema_version`, `plan_id`, `plan_revision`, `plan_digest`,
`confirmation_id`, `approval_operation_id`, `approved_at`, and
`content_digest`, in that exact order. It is constructed only by the Dockyard
confirmed plan-approval command and is bound to the materialized plan before
handoff. After Git evidence is committed and before Scheduler submission, the
handoff owner writes or byte-exactly replays one existing
`ApprovalScope.DISPATCH` grant. The existing `ApprovalGate` remains the sole
transition authority and is never bypassed.

### 2.1 `MaterializedTaskEvidence` — exactly 13 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `project_id` | `str` |
| 3 | `plan_id` | `str` |
| 4 | `plan_revision` | `int` |
| 5 | `plan_content_digest` | `str` |
| 6 | `materialization_operation_id` | `str` |
| 7 | `task_id` | `str` |
| 8 | `revision` | `int` |
| 9 | `task_card_relative_path` | `str` |
| 10 | `task_card_content_digest` | `str` |
| 11 | `pm_owner_id` | `str` |
| 12 | `materialized_at` | `str` |
| 13 | `content_digest` | `str` |

The relative path is repository-relative, slash-normalized, and must remain
under `docs/pm/tasks/`. It is never an absolute path, symlink, reparse point,
or traversal. The task-card digest binds the exact canonical UTF-8 bytes.

### 2.2 `MaterializationAdmissionPlanTemplate` — exactly 20 fields

The template freezes every security-critical Scheduler plan input that exists
before Git evidence and QueueEntry allocation:

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `template_id` | `str` |
| 3 | `task_id` | `str` |
| 4 | `revision` | `int` |
| 5 | `worker_kind` | `WorkerKind` |
| 6 | `assessment_id` | `str` |
| 7 | `dispatch_id` | `str` |
| 8 | `event_id` | `str` |
| 9 | `outbox_message_id` | `str` |
| 10 | `role_id` | `str` |
| 11 | `report_path` | `str` |
| 12 | `model_selection` | `ModelSelectionSnapshot` |
| 13 | `expected_task_state` | `str` |
| 14 | `expected_task_attempt` | `int` |
| 15 | `new_attempt` | `int` |
| 16 | `policy_version` | `str` |
| 17 | `holder_instance_id` | `str` |
| 18 | `canonical_worktree_identity` | `str` |
| 19 | `created_at` | `str` |
| 20 | `content_digest` | `str` |

`model_selection` is the existing frozen/slotted typed value, not a mapping.
The template is durable before handoff reservation and never changes after a
queue identity is allocated.

### 2.3 `MaterializationAdmissionRequest` — exactly 26 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `handoff_id` | `str` |
| 3 | `project_id` | `str` |
| 4 | `plan_id` | `str` |
| 5 | `task_id` | `str` |
| 6 | `revision` | `int` |
| 7 | `materialized_task_content_digest` | `str` |
| 8 | `admission_plan_template_id` | `str` |
| 9 | `admission_plan_template_digest` | `str` |
| 10 | `expected_plan_revision` | `int` |
| 11 | `expected_plan_content_digest` | `str` |
| 12 | `expected_task_card_relative_path` | `str` |
| 13 | `expected_task_card_content_digest` | `str` |
| 14 | `expected_assessment_relative_path` | `str` |
| 15 | `expected_assessment_content_digest` | `str` |
| 16 | `repository_identity` | `str` |
| 17 | `expected_head_commit` | `str` |
| 18 | `expected_base_commit` | `str` |
| 19 | `expected_branch` | `str` |
| 20 | `expected_canonical_generation` | `int` |
| 21 | `business_priority` | `BusinessPriority` |
| 22 | `worker_kind_request` | `WorkerKind` |
| 23 | `assessment_id` | `str` |
| 24 | `policy_version` | `str` |
| 25 | `requested_at` | `str` |
| 26 | `content_digest` | `str` |

The request repeats security-critical identity deliberately. Each repeated
value must equal the materialized evidence and the fresh repository/canonical
snapshot before any write.

### 2.4 Enums

`MaterializationAdmissionPhase` has exactly these forward-only values:

```text
MATERIALIZED
GIT_EVIDENCE_COMMITTED
CANONICAL_TASK_REGISTERED
QUEUE_RESERVED
ADMISSION_READY
ADMISSION_SUBMITTED
FINALIZED
```

`MaterializationAdmissionOutcome` has exactly these values:

```text
RESERVED
REPLAYED
FINALIZED
RECOVERY_REQUIRED
REJECTED
```

### 2.5 `MaterializationAdmissionReceipt` — exactly 44 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `receipt_id` | `str` |
| 3 | `handoff_id` | `str` |
| 4 | `project_id` | `str` |
| 5 | `plan_id` | `str` |
| 6 | `plan_revision` | `int` |
| 7 | `plan_content_digest` | `str` |
| 8 | `materialization_operation_id` | `str` |
| 9 | `materialized_task_content_digest` | `str` |
| 10 | `admission_plan_template_id` | `str` |
| 11 | `admission_plan_template_digest` | `str` |
| 12 | `pm_owner_id` | `str` |
| 13 | `task_id` | `str` |
| 14 | `revision` | `int` |
| 15 | `task_card_relative_path` | `str` |
| 16 | `task_card_content_digest` | `str` |
| 17 | `assessment_relative_path` | `str` |
| 18 | `assessment_content_digest` | `str` |
| 19 | `repository_identity` | `str` |
| 20 | `base_commit` | `str` |
| 21 | `branch` | `str` |
| 22 | `task_card_git_commit` | `str | None` |
| 23 | `canonical_task_state` | `str | None` |
| 24 | `canonical_task_generation` | `int | None` |
| 25 | `queue_id` | `str | None` |
| 26 | `enqueue_sequence` | `int | None` |
| 27 | `policy_version` | `str` |
| 28 | `business_priority` | `BusinessPriority` |
| 29 | `worker_kind_request` | `WorkerKind` |
| 30 | `assessment_id` | `str` |
| 31 | `admission_plan_content_digest` | `str | None` |
| 32 | `schedule_receipt_id` | `str | None` |
| 33 | `admission_plan_receipt_id` | `str | None` |
| 34 | `dispatch_id` | `str | None` |
| 35 | `dispatch_event_id` | `str | None` |
| 36 | `phase` | `MaterializationAdmissionPhase` |
| 37 | `outcome` | `MaterializationAdmissionOutcome` |
| 38 | `reserved_at` | `str` |
| 39 | `git_evidence_committed_at` | `str | None` |
| 40 | `canonical_registered_at` | `str | None` |
| 41 | `queue_reserved_at` | `str | None` |
| 42 | `admission_submitted_at` | `str | None` |
| 43 | `finalized_at` | `str | None` |
| 44 | `content_digest` | `str` |

Optional fields are absent until their owning phase is reached and become
immutable afterward. The receipt may copy typed owner evidence but may not
manufacture it. `dispatch_event_id` is the canonical event ID, never a
`dispatch_id` in disguise.

## 3. Durable format, path, and digest

The handoff receipt path is exactly:

```text
.agentdesk/runtime/materialization-admission/receipts/<receipt_id>.yaml
```

The immutable admission template and constructed pre-admission plan paths are:

```text
.agentdesk/runtime/materialization-admission/templates/<template_id>.yaml
.agentdesk/runtime/materialization-admission/admission-plans/<handoff_id>.yaml
```

Evidence uses canonical UTF-8 YAML: no BOM, LF endings, one final LF, exact
field order, no duplicate keys, anchors, aliases, implicit timestamps, or
unknown fields. `content_digest` is `sha256:` plus 64 lowercase hexadecimal
characters over canonical bytes excluding only the digest field.

Writes use same-directory unique temporary files, file flush, atomic replace,
and directory flush. Same identity plus identical canonical bytes is
byte-exact replay. Same identity with different bytes is divergent replay and
is rejected without advancing a phase.

## 4. Identity and evidence binding

Before phase advancement, the owner re-reads and exactly binds:

1. `project_id`, repository identity, branch, base commit, and fresh HEAD;
2. plan ID/revision/digest and PM materialization operation;
3. task ID/revision, card and assessment relative paths, bytes, and SHA-256 digests;
4. Git ancestry proving `task_card_git_commit` contains both exact files;
5. canonical task ID/revision/state/generation;
6. QueueEntry task/revision/assessment/priority/WorkerKind/policy identity;
7. Scheduler receipt, admission plan, dispatch, and canonical event identity
   only after their owner has produced them.
8. confirmed plan authorization, generated dispatch subject, grant identity,
   grant snapshot, and approval operation before Scheduler submission.

Repository replacement, branch drift, stale HEAD/base, path substitution,
card tampering, symlink/reparse substitution, stale canonical generation,
duplicate task registration, divergent QueueEntry, or swapped dispatch/event
identity is a typed fail-closed conflict with zero downstream writes.

## 5. Forward-only operation and lock order

The only legal phase chain is:

```text
MATERIALIZED -> GIT_EVIDENCE_COMMITTED -> CANONICAL_TASK_REGISTERED
-> QUEUE_RESERVED -> ADMISSION_READY -> ADMISSION_SUBMITTED -> FINALIZED
```

The operation order is frozen:

1. validate request, materialized evidence, and exact admission template
   without writes;
2. under the handoff-receipt lock, persist/replay the template and
   `MATERIALIZED` receipt, then release;
3. outside state/store locks, commit or prove the exact task-card and typed
   assessment Git evidence;
4. under the receipt lock, advance to `GIT_EVIDENCE_COMMITTED`, then release;
   outside all owner locks, write or byte-exactly replay the exact confirmed
   dispatch grant before Scheduler submission;
5. outside all owner locks, derive and byte-exactly create/replay the sole
   managed worktree through WorktreeLifecycleManager; its path, branch, base,
   task/revision/attempt, and dispatch identity must match the frozen template;
6. under the canonical state-transition lock, register/replay the exact task,
   then release before advancing the receipt;
7. under the PortfolioScheduler sequence/queue-store lock, reserve/replay one
   `queue_id` and `enqueue_sequence`, then release before advancing the receipt;
8. re-read Git, canonical task, QueueEntry, managed-worktree record, template,
   and receipt; construct
   the exact `PortfolioAdmissionPlan`, persist it, bind its canonical digest,
   and only then advance to `ADMISSION_READY`;
9. outside the handoff, Git, worktree, canonical, and queue locks, invoke the typed
   PortfolioScheduler selection/admission boundary;
10. bind its exact typed evidence as `ADMISSION_SUBMITTED`, then `FINALIZED`.

Lock acquisition order is exactly: **handoff receipt lock -> canonical
state-transition lock -> PortfolioScheduler sequence/queue-store lock**.
The locks are not nested across owner calls: release the current owner lock
before entering the next owner. WorktreeLifecycleManager owns its independent
reservation/store/Git boundary and is called before canonical registration and
Scheduler lease acquisition. Git commands, Scheduler selection/admission,
Worker/lease/process startup, provider/model/API/network calls, and Dockyard
projection never run while any of these locks is held.

## 6. Crash and recovery matrix

| Crash/race window | Required disposition |
|---|---|
| before `MATERIALIZED` receipt | resume exact reservation |
| template exists, receipt missing | adopt only byte-exact template and request |
| receipt write interrupted/noncanonical | fail-closed |
| `MATERIALIZED`, before Git evidence | resume exact Git binding |
| Git commit exists, receipt still `MATERIALIZED` | adopt only exact card bytes and ancestry |
| Git evidence divergent or repository replaced | reject |
| Git evidence committed, before canonical registration | resume exact canonical registration |
| canonical task exists, receipt lags | adopt only byte-exact task/revision/generation |
| canonical task divergent or stale generation | reject |
| canonical registered, before queue reservation | resume exact queue reservation |
| QueueEntry exists, receipt lags | adopt only exact QueueEntry identity |
| sequence advanced but QueueEntry missing | fail-closed; never guess a sequence |
| QueueEntry divergent or duplicate task registration | reject |
| `QUEUE_RESERVED`, before `ADMISSION_READY` | resume validation, allocate nothing new |
| constructed admission plan write interrupted/divergent | fail-closed |
| `ADMISSION_READY`, before Scheduler call | resume the exact admission identity |
| Scheduler admission committed, receipt lags | adopt exact typed Scheduler evidence |
| admission evidence divergent | fail-closed |
| `ADMISSION_SUBMITTED`, before final receipt | resume finalization only |
| valid `FINALIZED` receipt | byte-exact replay/no-op |
| two owners race with identical bytes | one write, byte-exact replay |
| same handoff identity with different bytes/generation | reject |
| attempt 4 requested or inferred | reject before receipt, Git, canonical, queue, lease, event, or Worker |

Recovery never allocates a replacement identity merely because a downstream
phase is incomplete. An ambiguous or UNKNOWN condition returns
`RECOVERY_REQUIRED` or `REJECTED`; it is never guessed from exception text,
stdout/stderr, provider name, PID, or lease expiry.

## 7. Replay and concurrency

- One materialized task/revision has at most one winning `handoff_id`, receipt,
  canonical registration, `queue_id`, and enqueue sequence.
- `ADMISSION_READY` requires a durable exact `PortfolioAdmissionPlan` whose
  digest is bound by the receipt; no in-memory-only plan authorizes admission.
- The plan is constructed only from the immutable template plus exact Git,
  canonical task, QueueEntry ID/sequence, and receipt evidence.
- Identical concurrent requests return the same receipt and downstream IDs.
- Divergent plan/card/repository/canonical/queue/admission content is rejected.
- A finalized handoff cannot be reopened or overwritten by late PM, Git,
  canonical, Scheduler, ACK, delivery, or tombstone evidence.
- Attempt three may be admitted; attempt four is forbidden before any write.
- No phase may move backward, skip an owner boundary, or change generation.

## 8. Status and non-goals

| Capability | Status |
|---|---|
| PM materialization-to-Scheduler handoff contract | **Contract Current — TC-13.29l.5a.1** |
| PM materialization-to-Scheduler handoff runtime | **Current — TC-13.29l.5d.4** |
| Dockyard local closed-loop E2E | **Blocked — TC-13.29l** |
| Interface #22 WorkflowOrchestrator ownership | **Unchanged** |
| Interface #36 PortfolioScheduler ownership | **Unchanged** |

The contract-only TC-13.29l.5a.1 commit added no runtime. TC-13.29l.5d.4 now
implements the owner-separated Git, canonical registration, queue reservation,
dispatch-approval projection, and Scheduler admission path. It still performs
no Worker/process start or model/provider/API/network call.

## 9. Admission preparation dependency (TC-13.29l.5c.1)

The handoff runtime may consume only a finalized PM Task Admission Preparation
receipt from `pm-task-admission-preparation-contract.md`. It must not default
BusinessPriority, synthesize TaskDifficulty, copy Dockyard provider/model/tier
strings, or construct a `ModelSelectionSnapshot` without exact
`select_model.select_binding()` evidence.

Admission Preparation Contract is **Contract Current — TC-13.29l.5c.1b**;
runtime is **Current — TC-13.29l.5c.2** and Dockyard composition is
**Contract Current — TC-13.29l.5d.3; Runtime Current — TC-13.29l.5d.4**. The materialization-admission handoff core remains
implemented and composed through Scheduler admission; the full TC-13.29l
Worker/MAD/acceptance/integration E2E remains Target.

TC-13.29l.5e composes the exact finalized admission receipt into the existing
managed-worktree, Scheduler-to-Worker handoff, and PM external Worker owners.
It advances through ACK, delivery, finalizer, and byte-exact replay without
moving process or lifecycle ownership into this handoff module. MAD,
acceptance, remediation, and integration remain outside this segment.

The only legal preparation output is the durable
`AdmissionPreparationHandoffInputs` document at the frozen preparation-store
path. The handoff runtime must read that exact document and verify its receipt,
profile, materialized-task, nested template/request, and content digests before
creating any handoff receipt or downstream write.
The document's explicit attempt pair must equal the nested template attempt
pair; handoff generation is never accepted as attempt evidence.
