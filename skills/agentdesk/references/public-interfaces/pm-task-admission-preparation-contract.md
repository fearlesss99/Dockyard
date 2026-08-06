# PM Task Admission Preparation Contract

Status: **Contract Current — TC-13.29l.5c.1b**
Runtime: **Current — TC-13.29l.5c.2**
Dockyard composition: **Contract Current — TC-13.29l.5d.3; Runtime Current — TC-13.29l.5d.4**
Schema: `agentdesk.pm-task-admission-preparation/v1`

This contract freezes how one approved and materialized Dockyard PM task gains
the typed evidence required by the PM materialization-admission handoff. It
does not implement preparation, modify a PM plan, reserve a Scheduler queue,
acquire a lease, start a Worker, or call a model/provider/API/network service.

## 1. Independent inputs and owners

| Evidence | Sole source/owner |
|---|---|
| plan/task identity and task-card materialization | Dockyard PM plan runtime |
| `business_priority` | explicit PM decision |
| seven ordered difficulty rationale keys | explicit PM assessment input |
| difficulty result and `assessment_id` | `assess_task_difficulty()` plus Difficulty Assessment Store |
| role/risk/capability minimum | committed `agentdesk.role-policies/v1` |
| locally available model bindings | validated `agentdesk.model-bindings/v2` |
| exact model choice | `select_model.select_binding()` |
| handoff template/request construction | Admission Preparation runtime |
| Git/card/canonical/queue/admission execution | materialization-admission handoff runtime |

`BusinessPriority` is never derived from `TaskDifficulty`, WorkerKind, tier,
risk, provider, model, cost, deadline, or task text. Difficulty is never
derived from free text, provider/model names, exit code, stdout/stderr, or
BusinessPriority. Model selection never copies the provider/model/tier strings
from `DockyardPlanTask`; it consumes only committed role policy and validated
local bindings through `select_model.select_binding()`.

## 2. Frozen public types

All public values are `frozen=True, slots=True`. Public annotations contain no
`Any`, `object`, `dict`, `Mapping`, `list`, `set`, callback, provider object,
model client, subprocess handle, or mutable collection. Ordered collections
are tuples.

### 2.1 `PmTaskAdmissionProfile` — exactly 19 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `profile_id` | `str` |
| 3 | `project_id` | `str` |
| 4 | `plan_id` | `str` |
| 5 | `plan_revision` | `int` |
| 6 | `task_id` | `str` |
| 7 | `revision` | `int` |
| 8 | `business_priority` | `BusinessPriority` |
| 9 | `difficulty_rationale_keys` | `tuple[str, ...]` |
| 10 | `selected_difficulty` | `TaskDifficulty | None` |
| 11 | `difficulty_override_reason` | `str | None` |
| 12 | `difficulty_approval_id` | `str | None` |
| 13 | `risk` | `str` |
| 14 | `task_capabilities` | `tuple[str, ...]` |
| 15 | `degradation_approval_id` | `str | None` |
| 16 | `expected_task_attempt` | `int` |
| 17 | `new_attempt` | `int` |
| 18 | `prepared_at` | `str` |
| 19 | `content_digest` | `str` |

The rationale tuple has exactly seven entries in `AssessmentDimension` order.
The PM supplies rationale keys, not difficulty values for individual
dimensions. `selected_difficulty` may be absent to accept the deterministic
recommendation. A downward override remains governed by the existing typed
reason and approval rules.

`expected_task_attempt` and `new_attempt` are non-bool integers. The only legal
relationship is `new_attempt == expected_task_attempt + 1`, with
`0 <= expected_task_attempt <= 2` and `1 <= new_attempt <= 3`. Attempt four is
invalid before any profile, receipt, assessment, model-selection, or
handoff-input write.

### 2.2 `AdmissionPreparationRequest` — exactly 20 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `preparation_id` | `str` |
| 3 | `profile_id` | `str` |
| 4 | `profile_content_digest` | `str` |
| 5 | `materialized_task_content_digest` | `str` |
| 6 | `repository_identity` | `str` |
| 7 | `materialization_base_commit` | `str` |
| 8 | `role_policy_path` | `str` |
| 9 | `role_policy_commit` | `str` |
| 10 | `role_policy_content_digest` | `str` |
| 11 | `model_bindings_path` | `str` |
| 12 | `model_bindings_content_digest` | `str` |
| 13 | `expected_binding_schema` | `str` |
| 14 | `expected_handoff_generation` | `int` |
| 15 | `canonical_worktree_identity` | `str` |
| 16 | `holder_instance_id` | `str` |
| 17 | `report_path` | `str` |
| 18 | `expected_head_commit` | `str` |
| 19 | `expected_branch` | `str` |
| 20 | `content_digest` | `str` |

`materialization_base_commit` equals `expected_head_commit` and the current
Git HEAD. The materialized task card is read as exact uncommitted working-tree
evidence from `MaterializedTaskEvidence.task_card_relative_path`; preparation
does not stage or commit it. The downstream handoff remains the sole task-card
Git commit owner.

`canonical_worktree_identity` is the exact future managed-worktree identity,
not the canonical repository root. It is deterministically derived from the
Git common directory, repository root, exact dispatch branch, base commit,
task/revision/attempt, and dispatch ID before preparation writes. Preparation
recomputes that identity and rejects substitution; it does not create the
worktree. WorktreeLifecycleManager remains the sole creation owner.

The role policy path is exactly `docs/pm/ROLE-POLICIES.yaml`; its blob is read
from `role_policy_commit`. The local binding path is exactly
`.agentdesk/runtime/model-bindings.yaml`. The binding file is never committed
and contains no API key.

### 2.3 Enums

`AdmissionPreparationPhase` has exactly these forward-only values:

```text
PROFILE_BOUND
ASSESSMENT_COMMITTED
MODEL_SELECTION_COMMITTED
HANDOFF_INPUTS_BOUND
FINALIZED
```

`AdmissionPreparationOutcome` has exactly these values:

```text
PREPARED
REPLAYED
FINALIZED
RECOVERY_REQUIRED
REJECTED
```

### 2.4 `AdmissionPreparationReceipt` — exactly 28 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `receipt_id` | `str` |
| 3 | `preparation_id` | `str` |
| 4 | `profile_id` | `str` |
| 5 | `profile_content_digest` | `str` |
| 6 | `materialized_task_content_digest` | `str` |
| 7 | `expected_task_attempt` | `int` |
| 8 | `new_attempt` | `int` |
| 9 | `assessment_id` | `str | None` |
| 10 | `assessment_content_digest` | `str | None` |
| 11 | `selected_difficulty` | `TaskDifficulty | None` |
| 12 | `worker_kind` | `WorkerKind | None` |
| 13 | `business_priority` | `BusinessPriority` |
| 14 | `role_policy_commit` | `str` |
| 15 | `role_policy_content_digest` | `str` |
| 16 | `model_bindings_content_digest` | `str` |
| 17 | `model_binding_id` | `str | None` |
| 18 | `model_selection` | `ModelSelectionSnapshot | None` |
| 19 | `admission_plan_template_id` | `str | None` |
| 20 | `admission_plan_template_digest` | `str | None` |
| 21 | `handoff_request_content_digest` | `str | None` |
| 22 | `phase` | `AdmissionPreparationPhase` |
| 23 | `outcome` | `AdmissionPreparationOutcome` |
| 24 | `created_at` | `str` |
| 25 | `assessed_at` | `str | None` |
| 26 | `model_selected_at` | `str | None` |
| 27 | `finalized_at` | `str | None` |
| 28 | `content_digest` | `str` |

Optional evidence is absent before its owning phase and immutable afterward.
The receipt copies only exact typed evidence; it never rejudges assessment or
model selection.

### 2.5 `AdmissionPreparationHandoffInputs` — exactly 11 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `preparation_id` | `str` |
| 3 | `receipt_id` | `str` |
| 4 | `profile_content_digest` | `str` |
| 5 | `materialized_task_content_digest` | `str` |
| 6 | `expected_task_attempt` | `int` |
| 7 | `new_attempt` | `int` |
| 8 | `admission_plan_template` | `MaterializationAdmissionPlanTemplate` |
| 9 | `materialization_admission_request` | `MaterializationAdmissionRequest` |
| 10 | `created_at` | `str` |
| 11 | `content_digest` | `str` |

This is the sole durable output consumed by the downstream handoff runtime.
Both nested values remain their exact frozen/slotted public types; they are not
converted to mappings or reconstructed from receipt digests.
The two explicit attempt fields must equal the nested template fields exactly.

## 3. Durable evidence

Paths are exactly:

```text
.agentdesk/runtime/admission-preparation/profiles/<profile_id>.yaml
.agentdesk/runtime/admission-preparation/receipts/<receipt_id>.yaml
.agentdesk/runtime/admission-preparation/model-selections/<preparation_id>.yaml
.agentdesk/runtime/admission-preparation/handoff-inputs/<preparation_id>.yaml
```

The existing Difficulty Assessment Store remains the sole assessment store.
Preparation must not create a second assessment format.

All new evidence uses canonical UTF-8 YAML with exact field order, LF endings,
one final LF, no BOM, duplicate keys, anchors, aliases, unknown fields, or
implicit timestamps. `content_digest` is `sha256:` plus 64 lowercase hex over
canonical bytes excluding only that digest field. Writes are atomic and
same-directory. Identical bytes replay; same identity with different bytes is
a typed conflict.

## 4. Validation and operation order

1. validate profile/request/materialized-task identity and all digests with
   zero writes;
2. re-read exact Git repository, branch, base HEAD, materialized working-tree
   task-card bytes, role-policy blob, and local model-bindings bytes;
3. persist/replay `PROFILE_BOUND` under only the preparation-store lock;
4. release that lock; call `assess_task_difficulty()` with exactly seven
   rationale keys, persist through the existing Difficulty Assessment Store,
   then bind `ASSESSMENT_COMMITTED`;
5. release all stores; call `select_model.select_binding()` using the committed
   role policy, validated local bindings, role, risk, selected difficulty tier,
   task capabilities, and typed degradation approval;
6. persist exact typed `ModelSelectionSnapshot`, then bind
   `MODEL_SELECTION_COMMITTED`;
7. construct the exact `MaterializationAdmissionPlanTemplate` and
   `MaterializationAdmissionRequest`; atomically persist one
   `AdmissionPreparationHandoffInputs` containing both exact values, re-read
   it, then bind its nested digests as `HANDOFF_INPUTS_BOUND`;
8. re-read all evidence, finalize, and only afterward allow the separate
   materialization-admission handoff runtime to run.

No preparation, assessment, or binding-store lock may be nested. Git reads,
assessor calls, model selection, handoff, Scheduler, lease, Worker, provider,
model/API/network calls, and Dockyard projection happen outside every store
lock.

## 5. Exact mapping rules

- `assessment_id` is deterministic for `(task_id, revision, profile_digest)`.
- `selected_difficulty` maps to the same-named `WorkerKind` only through the
  existing explicit four-tier mapping; it never changes BusinessPriority.
- `task_min_tier` passed to `select_binding()` is the selected difficulty tier.
- `role_id`, risk, capabilities, and degradation approval are copied from
  typed profile/materialized evidence, not inferred from prose.
- The `select_binding()` result is converted field-for-field into
  `ModelSelectionSnapshot`; no output field is filled from Dockyard provider,
  model, tier, stdout/stderr, or exception text.
- Template dispatch/event/outbox IDs are deterministic from preparation
  identity and generation, allocated once before handoff, and byte-exact on
  replay.
- Profile attempt identities are copied unchanged into the receipt, durable
  handoff-input document, and `MaterializationAdmissionPlanTemplate`.
- Attempt three is allowed; attempt four is rejected before profile or receipt
  persistence. Attempt is never inferred from handoff generation, queue state,
  exception text, receipt count, or task-card prose.

## 6. Crash/replay matrix

| Window | Required disposition |
|---|---|
| before profile persistence | resume exact validation |
| profile write interrupted/noncanonical | fail-closed |
| profile exists, receipt missing | adopt byte-exact profile only |
| `PROFILE_BOUND`, before assessment | resume exact assessor input |
| assessment exists, receipt lags | adopt exact store evidence |
| assessment divergent/stale | reject |
| `ASSESSMENT_COMMITTED`, before model selection | resume exact policy/binding input |
| role-policy commit/path/digest changed | reject |
| local binding bytes/schema changed | reject; require a new preparation identity |
| model-selection write interrupted | fail-closed |
| model selection exists, receipt lags | adopt exact typed snapshot |
| `MODEL_SELECTION_COMMITTED`, before handoff inputs | resume exact construction |
| handoff-input document write interrupted/noncanonical | fail-closed |
| handoff-input document exists, receipt lags | adopt byte-exact document only |
| nested template/request or document digest divergent | reject before handoff |
| `HANDOFF_INPUTS_BOUND`, before finalization | resume finalization only |
| valid `FINALIZED` | byte-exact replay/no-op |
| `FINALIZED`, before downstream handoff consumes output | replay exact durable handoff-input document |
| two identical preparers race | one write; identical replay |
| divergent profile/preparation generation races | one winner; loser rejects |
| same preparation identity with changed attempt values | reject as divergent replay |
| attempt 4 | reject before any write or downstream call |

UNKNOWN, unreadable, permission-denied, repository replacement, symlink or
reparse evidence always fails closed. No decision parses exception messages,
stdout/stderr, provider names, or exit codes.

## 7. Status

| Capability | Status |
|---|---|
| PM Task Admission Preparation contract | **Contract Current — TC-13.29l.5c.1b** |
| PM Task Admission Preparation runtime | **Current — TC-13.29l.5c.2** |
| Dockyard preparation-to-handoff composition | **Contract Current — TC-13.29l.5d.3; Runtime Current — TC-13.29l.5d.4** |
| Dockyard local closed-loop E2E | **Blocked — TC-13.29l** |

Interfaces #22, #36, #40, and #41 retain their existing owners and status.

TC-13.29l.5c.1a repairs only durable output ownership. The downstream handoff
runtime may consume only the exact `AdmissionPreparationHandoffInputs` bytes;
caller memory, receipt-digest reconstruction, or regenerated identities never
authorize handoff.

TC-13.29l.5c.1b repairs attempt identity binding. Preparation runtime must copy
the explicit profile attempt pair unchanged through receipt, handoff-input
document, and nested template; no other field is an attempt surrogate.
