# Dockyard Local Control API Contract

Status: **Contract Current — TC-13.29b**
Runtime: **Current — TC-13.29f**
Retry owner composition: **Current — TC-13.29l.13b**
Retry closed-loop E2E: **Verified — TC-13.29l.13c**
Returned-delivery redispatch: **Runtime Current — TC-13.29l.14c; review handoff Current — TC-13.29l.14d**
Owner-loss automatic recovery: **Runtime + local E2E Current — TC-13.29l.15a**
Adversarial verification: **Verified — TC-13.29m / extended matrix TC-13.29m.1b**
Remote relay and cloud deployment: **Target — post-TC-13.29o**

## 1. Purpose and ownership

Dockyard is the Chinese, dark-mode visual control surface for AgentDesk. The
local Control API projects existing AgentDesk evidence and submits typed
commands to existing owners. It is not a second state machine, PM, Scheduler,
approval service, Git facade, shell, or provider launcher.

The ownership boundary is exact:

- `StateProvider` owns canonical read snapshots.
- the single logical PM owns requirement decomposition and task-card revisions.
- `PortfolioScheduler` owns selection and admission.
- `WorkflowOrchestrator` owns dispatch, ACK, delivery, acceptance, integration,
  cancellation, supersession, retry, remediation, and owner-loss recovery.
- `WorktreeLifecycleManager` owns managed worktree creation and release.
- MAD owns deliberation and audit results.
- provider adapters own their frozen CLI invocation boundaries.

The API must never write `docs/pm/state/tasks.yaml`, events, outbox, acceptance,
Scheduler evidence, worktree evidence, or runtime receipts directly. Every
mutation delegates to the typed production owner and returns its durable result.

## 2. Transport boundary

The namespace is exactly `/api/dockyard/v1`.

The local runtime binds only to `127.0.0.1` and `::1`. It must reject a
non-loopback bind request, non-loopback peer, unrecognized `Host`, and
unrecognized `Origin`. The first implementation uses HTTP plus Server-Sent
Events (SSE); WebSocket is not required.

All JSON is UTF-8, duplicate-key rejecting, finite-depth, and size bounded.
Command requests require `Content-Type: application/json`. State-changing
requests require a paired device token or the loopback bootstrap capability,
an exact Origin, a CSRF value, an idempotency key, a snapshot CAS, and the
endpoint-specific typed body. CORS wildcard, JSONP, form bodies, method
override headers, arbitrary redirects, and URL credentials are forbidden.

## 3. Public envelopes

Public values are frozen/slotted typed records. Public annotations must not use
`Any`, `object`, `dict`, `Mapping`, `list`, `set`, mutable collections,
callbacks, factories, exception objects, provider objects, process handles, or
filesystem handles.

### `DockyardCommandEnvelope`

Exactly nine fields in this order:

1. `schema_version`
2. `command_id`
3. `project_id`
4. `command_type`
5. `idempotency_key`
6. `expected_snapshot_commit`
7. `expected_revision`
8. `confirmation_id`
9. `payload_digest`

### `DockyardCommandReceipt`

Exactly ten fields in this order:

1. `schema_version`
2. `command_id`
3. `project_id`
4. `command_type`
5. `outcome`
6. `canonical_event_id`
7. `snapshot_commit`
8. `replayed`
9. `completed_at`
10. `content_digest`

`canonical_event_id` may be absent only when the delegated production owner
defines a typed zero-write result. A byte-exact replay returns the same receipt.
The same idempotency key with divergent content is a typed conflict.

### `DockyardErrorEnvelope`

Exactly seven fields in this order:

1. `schema_version`
2. `request_id`
3. `error_code`
4. `category`
5. `retryable`
6. `safe_message`
7. `correlation_digest`

Errors never contain an exception message, traceback, stdout, stderr, prompt,
API key, environment value, argv, absolute path, route/thread/session/host ID,
PID creation evidence, raw YAML, or provider response.

### `DockyardSseEvent`

Exactly eight fields in this order:

1. `schema_version`
2. `event_cursor`
3. `project_id`
4. `event_type`
5. `snapshot_commit`
6. `resource_id`
7. `occurred_at`
8. `content_digest`

The exact event types are `snapshot.changed`, `task.changed`, `run.changed`,
`approval.changed`, `provider.changed`, `project.changed`, and
`health.changed`. SSE publishes projections only; it is not canonical evidence.

## 4. Read endpoints

The frozen read endpoints are:

- `GET /api/dockyard/v1/health`
- `GET /api/dockyard/v1/projects`
- `GET /api/dockyard/v1/projects/{project_id}`
- `GET /api/dockyard/v1/projects/{project_id}/overview`
- `GET /api/dockyard/v1/projects/{project_id}/tasks`
- `GET /api/dockyard/v1/projects/{project_id}/tasks/{task_id}`
- `GET /api/dockyard/v1/projects/{project_id}/runs`
- `GET /api/dockyard/v1/projects/{project_id}/approvals`
- `GET /api/dockyard/v1/projects/{project_id}/providers`
- `GET /api/dockyard/v1/projects/{project_id}/events`

Every project read starts from one validated snapshot. Pagination tokens bind
the project identity, snapshot commit, sort key, and filter digest. A stale or
divergent token is rejected; results from two snapshots are never combined.

## 5. Command endpoints

The frozen command endpoints are:

- `POST /api/dockyard/v1/projects/register`
- `POST /api/dockyard/v1/projects/{project_id}/plans`
- `PATCH /api/dockyard/v1/projects/{project_id}/plans/{plan_id}`
- `POST /api/dockyard/v1/projects/{project_id}/plans/{plan_id}/approve`
- `POST /api/dockyard/v1/projects/{project_id}/tasks/{task_id}/retry`
- `POST /api/dockyard/v1/projects/{project_id}/tasks/{task_id}/cancel`
- `POST /api/dockyard/v1/projects/{project_id}/dispatches/{dispatch_id}/terminate`
- `POST /api/dockyard/v1/projects/{project_id}/deliveries/{task_id}/accept`
- `POST /api/dockyard/v1/projects/{project_id}/deliveries/{task_id}/return`
- `PUT /api/dockyard/v1/projects/{project_id}/providers/{provider_id}/binding`
- `POST /api/dockyard/v1/projects/{project_id}/remove`

There is no generic transition, event, YAML, filesystem, Git, shell, argv,
provider, process, SQL, template, plugin, or Python execution endpoint.

## 6. Confirmation matrix

The exact mandatory confirmations are:

| Command | Confirmation evidence |
|---|---|
| approve plan | plan ID, revision, digest, task count |
| active manual retry | task, failed attempt, next attempt, provider/model |
| model or strategy escalation | old and new provider/model/tier |
| terminate dispatch | task, attempt, dispatch ID, known Worker identity |
| remove project | project identity and the phrase “不会删除 Git 仓库” |

Automatic bounded retry and owner-loss recovery continue under their existing
contracts without a new Dockyard confirmation. Attempt 4 is forbidden. Manual
retry, provider/model changes, and policy-limit overrides require confirmation.
Dockyard cannot weaken ALIVE/UNKNOWN/DEAD, approval, lease, CAS, attempt, or
generation rules.

## 7. Project removal

Project removal is a soft operation. It may disable/remove the Dockyard registry
entry and remove Dockyard-owned runtime cache only after exact identity checks.
It must not remove, recursively delete, clean, reset, prune, rewrite, or modify
the Git repository, worktrees, task cards, evidence, reports, branches, refs, or
commits. Reparse points, symlinks, identity divergence, dirty ownership, and
UNKNOWN filesystem state fail closed.

## 8. Provider status

Provider state is exactly `READY`, `DEGRADED`, `NOT_READY`, or `UNKNOWN`.

- `READY`: executable, pinned version, binding, decoder, required capability,
  and permission evidence are complete.
- `DEGRADED`: executable but below preferred policy and subject to the existing
  approval gate.
- `NOT_READY`: a typed, known missing prerequisite exists.
- `UNKNOWN`: evidence cannot be confirmed; dispatch is forbidden.

The API never infers provider state from an exception string, stdout/stderr,
exit code alone, provider marketing name, model name, PID, or lease expiry.

## 9. Device pairing

Device pairing is Contract Target until TC-13.29e. It uses a single-use,
short-lived high-entropy pairing code and stores only an irreversible token
verifier. Device scopes are `READ`, `APPROVE`, `RETRY`, and `TERMINATE`.
Tokens can be revoked individually or all at once. The first local runtime does
not claim that this constitutes a cloud relay or public-internet security.

## 10. Limits and timing

The runtime freezes explicit maximums for request bytes, JSON nesting, field
lengths, collection sizes, concurrent streams, stream backlog, command timeout,
and idle timeout before becoming Runtime Current. It uses monotonic time for
durations and UTC timestamps for durable records. Clock rollback and timeout
ambiguity fail closed.

## 11. Status boundary

- Dockyard product/control contract: **Contract Current — TC-13.29b**.
- Read projection: **Current — TC-13.29c**.
- Local registry: **Current — TC-13.29d**.
- Device pairing: **Current — TC-13.29e**.
- Loopback Control API and SSE: **Current — TC-13.29f**.
- PM plan runtime: **Current — TC-13.29l.7**.
- Dockyard loopback Web shell runtime: **Current — TC-13.29l.6**.
- Deterministic local requirement-to-plan composition: **Current — TC-13.29l.7**.
- PM difficulty/Agent route core: **Current — local unified integration**. A
  deterministic decomposer may split explicit numbered/bulleted requirements,
  inspect a read-only tracked-path inventory bound to the exact Git HEAD,
  assess the seven frozen difficulty dimensions, and route each task to an
  enabled model binding. No capable binding or stale repository evidence is a
  typed pre-dispatch rejection; this core never starts a Worker or bypasses
  the existing approval owner.
- Adaptive model-based PM decomposition: **Target**. Provider-specific model
  invocation, live capability discovery, and learned decomposition remain out
  of this core.
- Active terminate capability: **Verified — TC-13.29l.12e**. Exact live
  ownership evidence, confirmation-bound command, loopback gateway,
  cancellation receipt, and replay are covered.
- Dockyard user retry command contract: **Contract Current — TC-13.29l.13a**;
  runtime/owner composition is **Current — TC-13.29l.13b**; closed-loop E2E is
  **Verified — TC-13.29l.13c** and adversarial verification is
  **Verified — TC-13.29m**.
- Returned-delivery redispatch is **Runtime Current — TC-13.29l.14c** and its
  completed retry is handed back to the durable review owner by
  **TC-13.29l.14d**. The immutable task card and frozen MAD fail evidence are
  retained through the new attempt.
- Owner-loss automatic recovery is **Runtime + local E2E Current —
  TC-13.29l.15a**. ALIVE is zero-write, UNKNOWN is fail-closed, and only DEAD
  enters the existing Interface #22 recovery and bounded retry owner.
- Local core closed-loop E2E: **Verified — TC-13.29l.10**. This exact path is
  requirement input, deterministic PM plan, explicit approval, Scheduler and
  isolated worktree admission, deterministic Worker delivery, MAD pass,
  explicit acceptance, and Git integration.
- Supported Dockyard local closed loop: **E2E Verified — TC-13.29n**. The
  supported matrix includes pass/accept/integrate, active terminate, finalized
  failure retry, returned-delivery redispatch, and owner-loss recovery.
- Core and extended adversarial boundaries: **Verified — TC-13.29m / Extended
  TC-13.29m.1b**, including pairing, filesystem/repository identity, SSE,
  two-browser races, hostile provider output, and attempt-four fencing.
- Optional future matrices remain **Partial/Target**: adaptive model-based PM,
  Automated execution, and a versioned task-spec revision editor.
- Remote relay/cloud deployment: **Target — not in TC-13.29 local runtime**.

Interface #22 remains the canonical lifecycle owner. Interface #36 retains its
recorded Scheduler boundary. Interface #24 remains the existing read-only HTML
Dashboard and is not Dockyard.

## 12. Returned-delivery redispatch extension

The existing `task.retry` command has two mutually exclusive durable evidence
routes. The original route remains a finalized `DISPATCH_FAILED`. The second
route, frozen by **TC-13.29l.14b**, is an explicitly returned delivery. It is
legal only when all of the following evidence is exact before any new write:

1. the canonical task is `ready`, has no `current_dispatch`, and retains the
   prior attempt;
2. one `DELIVERY_RETURNED` and one later `TASK_REQUEUED` bind the same task,
   revision, prior attempt, and prior dispatch;
3. one finalized post-delivery review receipt binds a `fail` MAD result, the
   prior delivery receipt, and those exact return/requeue event identities;
4. the finalized dispatch receipt/tombstone, handoff, admission plan, outbox,
   managed worktree, provider, and model identities all agree;
5. the prior attempt is `1` or `2`, the next attempt is exactly prior plus one,
   and the confirmation displays task, prior/next attempt, provider, and model.

When the specification has not changed, the task revision remains unchanged
and only the attempt advances. This is the protocol's normal returned-delivery
rule. A change to goals, acceptance criteria, dependencies, allowed paths, base
or safety constraints is not a redispatch: it requires a separately versioned
task specification and is rejected by this route.

The next Worker prompt is the immutable task-card text followed by the durable
MAD fail report and typed issue summaries. The task card itself is never
overwritten. Exception text, stdout/stderr, provider/model names, exit codes,
PID state, or a bare `ready` task cannot substitute for the evidence above.

All new dispatch/event/outbox identities are fixed before Worker launch. The
runtime must call the existing bounded-dispatch owner outside the state lock,
must not reuse the prior dispatch ID, and must reject stale/divergent evidence
and attempt four with zero approval, lease, transition, outbox, or Worker side
effects. Interface #22 remains the sole dispatch lifecycle owner.

Returned-delivery redispatch is **Contract Current — TC-13.29l.14b**,
**Runtime Current — TC-13.29l.14c**, and **review handoff Current —
TC-13.29l.14d**. The supported unchanged-specification return/redispatch path
is closed. A versioned task-spec revision editor remains **Target** and is not
implied by this runtime.

## 13. Owner-loss automatic recovery

TC-13.29l.15a composes startup recovery without moving lifecycle ownership
into Dockyard. The background local runtime considers only exact canonical
dispatched/in-progress tasks with a durable dispatch receipt and no finalizer
tombstone. It validates task-card, approval, Scheduler plan, event/outbox,
handoff, provider/model, worktree, and process evidence before requesting the
existing WorkflowOrchestrator recovery entry.

ALIVE causes no write or retry. UNKNOWN is fail-closed. DEAD may proceed only
after Dockyard's two read-only probes and the WorkflowOrchestrator's second
liveness check inside the state-lock critical section. Retry executes outside
that lock, reuses the durable next identity, and cannot create attempt four.
Dockyard never classifies recovery from exception text, stdout/stderr, provider
name, exit code, PID alone, or lease expiry.

Owner-loss automatic recovery is **Runtime + local E2E Current —
TC-13.29l.15a**. Reconnect presentation is **Current — TC-13.29l.15b** and
retains the last validated projection while SSE reconnects.
