# PM TaskDifficulty Assessment — Frozen Contract (Current — TC-13.22a)

This document freezes the PM-side assessment evidence used before dispatch.
It freezes public data shapes and validation boundaries only.  It does not
ship canonical evidence runtime or dispatch enforcement, modify
`core_types.py`, extend `ApprovalScope`, or change any runtime gate.

## 1. Scope and independent concepts

`TaskDifficulty` is a task-intrinsic assessment result.  It is independent of:

- `WorkerKind`, which is the logical Worker/slot kind used by dispatch;
- model tier, which describes execution-resource capability;
- model price or model capability;
- `risk`, which remains a task-card governance field;
- provider-specific rate-limit or escalation signals.

No consumer may derive `TaskDifficulty` from `WorkerKind`, model price,
model capability, or a risk floor.  A stronger or more expensive model never reduces task difficulty.
Risk may contribute to a dimension result through a frozen policy rationale,
but `risk` itself is not a difficulty value or a hard-floor shortcut.

The deterministic assessor is implemented as a pure production module by
TC-13.22b.1 and is **Current — TC-13.22b.1**.  Canonical evidence runtime
remains **Target — TC-13.22b.2**.

## 2. Public types

### 2.1 `AssessmentDimension`

`AssessmentDimension` is a string enum with exactly seven values, in this
serialization and `dimension_results` order:

1. `modification_scope`
2. `requirement_clarity`
3. `state_concurrency`
4. `public_contract`
5. `failure_impact`
6. `rollback_complexity`
7. `dependency_conflict`

Unknown values fail closed.  Reordering, aliasing, or adding a dimension is a
new contract revision.

### 2.2 `DimensionResult`

`DimensionResult` is a frozen, slots value with exactly four fields:

| # | Field | Type | Rule |
|---:|---|---|---|
| 1 | `dimension` | `AssessmentDimension` | One of the seven dimensions above |
| 2 | `difficulty` | `TaskDifficulty` | Existing four-value enum: `basic`, `standard`, `advanced`, `expert` |
| 3 | `rationale_key` | `str` | Must be in the frozen policy allowlist in §3 |
| 4 | `hard_floor` | `bool` | Must equal the allowlist value for `rationale_key` |

The value has no free-form rationale text.  It has no `Any`, `dict`,
`Mapping`, mutable `list`, provider, model, price, or risk-floor field.

### 2.3 `TaskDifficultyAssessment`

`TaskDifficultyAssessment` is a frozen, slots value with exactly twelve
public fields.  `dimension_results` is an immutable tuple, not a mutable list.

| # | Field | Type | Rule |
|---:|---|---|---|
| 1 | `schema_version` | `str` | Exactly `agentdesk.difficulty-assessment/v1` |
| 2 | `assessment_id` | `str` | Matches `ASM-[A-Za-z0-9][A-Za-z0-9._-]*` |
| 3 | `task_id` | `str` | Exact task-card identity |
| 4 | `revision` | `int` | Non-bool integer, at least 1 |
| 5 | `minimum_difficulty` | `TaskDifficulty` | Aggregated hard-floor minimum |
| 6 | `recommended_difficulty` | `TaskDifficulty` | Aggregated seven-dimension recommendation |
| 7 | `selected_difficulty` | `TaskDifficulty` | PM selection subject to §4 |
| 8 | `dimension_results` | `tuple[DimensionResult, ...]` | Exactly seven entries in enum order |
| 9 | `override_direction` | `str` | Exactly `none`, `up`, or `down` |
| 10 | `override_reason` | `str | None` | Structured allowlist value only when required |
| 11 | `approval_id` | `str | None` | `APR-*` only for an approved downward override |
| 12 | `policy_version` | `str` | Frozen policy identifier used for this assessment |

The public value has no `snapshot_commit` field.  `snapshot_commit` is an
evidence-envelope provenance key defined in §6 and must not be added to the public twelve-field value.

## 3. Frozen policy and rationale allowlist

Every `rationale_key` maps to one `TaskDifficulty` and one `hard_floor`
boolean.  The assessor or PM must not invent a key or free-form explanation.

| Dimension | `rationale_key` | Difficulty | Hard floor |
|---|---|---|---:|
| `modification_scope` | `scope.bounded` | `basic` | false |
| `modification_scope` | `scope.single_module` | `standard` | false |
| `modification_scope` | `scope.cross_module` | `advanced` | false |
| `modification_scope` | `scope.architecture` | `expert` | false |
| `requirement_clarity` | `clarity.explicit` | `basic` | false |
| `requirement_clarity` | `clarity.known_pattern` | `standard` | false |
| `requirement_clarity` | `clarity.ambiguous_constraints` | `advanced` | false |
| `requirement_clarity` | `clarity.open_decision` | `expert` | false |
| `state_concurrency` | `concurrency.single_writer` | `basic` | false |
| `state_concurrency` | `concurrency.shared_state` | `standard` | false |
| `state_concurrency` | `concurrency.lock_cas` | `advanced` | true |
| `state_concurrency` | `concurrency.cross_process_recovery` | `expert` | true |
| `public_contract` | `contract.internal` | `basic` | false |
| `public_contract` | `contract.additive` | `standard` | false |
| `public_contract` | `contract.compatibility_sensitive` | `advanced` | false |
| `public_contract` | `contract.breaking_migration` | `expert` | true |
| `failure_impact` | `impact.informational` | `basic` | false |
| `failure_impact` | `impact.local_failure` | `standard` | false |
| `failure_impact` | `impact.service_degradation` | `advanced` | false |
| `failure_impact` | `impact.security_boundary` | `expert` | true |
| `failure_impact` | `impact.data_corruption` | `expert` | true |
| `rollback_complexity` | `rollback.simple` | `basic` | false |
| `rollback_complexity` | `rollback.tested` | `standard` | false |
| `rollback_complexity` | `rollback.multi_step` | `advanced` | false |
| `rollback_complexity` | `rollback.irreversible` | `expert` | true |
| `dependency_conflict` | `dependency.none` | `basic` | false |
| `dependency_conflict` | `dependency.known` | `standard` | false |
| `dependency_conflict` | `dependency.cross_repository` | `advanced` | false |
| `dependency_conflict` | `dependency.unresolved` | `expert` | false |

The following are hard-floor conditions when represented by their frozen
policy rationale: lock/CAS or cross-process recovery; a security boundary;
possible data corruption; an irreversible operation; and a breaking public
contract or cross-version migration.  `impact.security_boundary` is a valid
v1 rationale key and is an expert hard floor.

Cross-repository scope, file count, and ordinary dependency count are not by themselves an expert hard floor.
Code-line count is never a difficulty rule.
The policy never uses model price, model capability, WorkerKind, or a risk
floor to infer a dimension result.

## 4. Aggregation and override semantics

For the seven ordered `DimensionResult` values:

```text
minimum_difficulty = max(difficulty for hard_floor == true)
                      or BASIC when no hard floor exists
recommended_difficulty = max(difficulty for all seven results)
```

The order is the existing `TaskDifficulty` order
`BASIC < STANDARD < ADVANCED < EXPERT`.  `selected_difficulty` is valid only
under these rules:

1. `selected_difficulty < minimum_difficulty` always fails closed, even with approval.
2. `selected_difficulty == recommended_difficulty` sets `override_direction=none` and needs no override.
3. `selected_difficulty > recommended_difficulty` sets `override_direction=up` and needs no approval.
4. `minimum_difficulty <= selected_difficulty < recommended_difficulty` sets `override_direction=down` and requires both a structured `override_reason` and a valid `approval_id`.
5. A downward override without exact subject-bound approval is invalid.

The v1 structured `override_reason` allowlist is exactly:

- `pm_scope_calibration`
- `pm_known_pattern`
- `pm_bounded_failure_impact`
- `pm_reviewed_rollback`

`override_reason` and `approval_id` are null for `none` and `up`.  They are
both non-null for `down`.  LLM output may be an untrusted suggestion only;
it is never authoritative evidence and cannot replace a rationale key,
policy result, or approval.

## 5. Task Card v2 compatibility

The task-card schema remains `agentdesk.task-card/v2`.  This card adds only
these two optional fields:

```yaml
task_difficulty: standard
difficulty_assessment_id: ASM-TC-123-r1-abc123
```

The complete seven `dimension_results` are never embedded in a task card.

- An old v2 card without either field remains readable.
- A frozen old card is never modified in place.
- A new card and every new revision must write both fields.
- If either field is present, the enum and `ASM-*` format are strict.
- Before dispatch, the independent assessment evidence must exist and match
  the task identity and revision; task-card fields alone are insufficient.
- This contract does not upgrade v2 to v3 and does not modify the existing
  task-card template in this card.

## 6. Independent assessment evidence

The canonical evidence path is:

```text
docs/pm/assessments/<task_id>/r<revision>/<assessment_id>.yaml
```

`<task_id>` and `<revision>` in the path must exactly match the document
fields.  The evidence YAML root has exactly these thirteen keys in this
order:

```text
schema_version
assessment_id
task_id
revision
minimum_difficulty
recommended_difficulty
selected_difficulty
dimension_results
override_direction
override_reason
approval_id
policy_version
snapshot_commit
```

The first twelve keys are the public `TaskDifficultyAssessment` value.
`snapshot_commit` is the single evidence-only provenance key and is not a
public dataclass field.  The canonical representation is UTF-8 without BOM,
LF line endings, one final LF, no tabs, no YAML anchors or aliases, no
duplicate keys, and deterministic key/sequence order.  The serialized
`dimension_results` sequence has exactly seven entries in
`AssessmentDimension` order.

`snapshot_commit` is a full 40-hex Git commit.  Validation must prove it is
an ancestor of the task-card snapshot/commit used to bind this assessment;
a matching string without Git ancestry evidence is invalid.  The
`assessment_id` is globally unique across the assessment evidence namespace.
Same identity and byte-exact content is replay; the evidence is byte-exact replayable.
Divergent content for the same identity is rejected.  The evidence file and
every parent directory must reject symlink/reparse paths before reading or writing.

Validation errors may expose only safe type names, field names, and stable
failure codes.  They must not expose prompts, model output, raw provider
output, secrets, workspace paths, Git paths, or arbitrary object repr/str.

## 7. Difficulty override authorization boundary

The independent authorization domain is:

```text
scope = difficulty_override
```

Its exact subject is the four-tuple:

```text
task_id, revision, attempt, dispatch_id
```

The authorization evidence must record exactly these decision fields:

```text
recommended_difficulty
minimum_difficulty
selected_difficulty
override_reason
approval_id
```

The authorization cannot be reused across a revision, attempt, or dispatch.
It is separate from dispatch approval, acceptance approval, integration
approval, and model degradation approval.  An existing approval ID from one
of those domains cannot authorize `difficulty_override`.

TC-13.22a reuses existing ApprovalGate evidence storage, identity binding,
replay, and fail-closed validation primitives only.  This contract does not modify the existing `ApprovalScope` enum.
It does not modify `approval_gate.py` or the production gate.
No new production approval scope is enabled by this contract-only card.

## 8. Lifecycle and control-plane boundaries

- Initial dispatch requires a valid, independently verified assessment.
- Retry preserves `TaskDifficulty` and `assessment_id` exactly.
- Escalation may change `WorkerKind` only; it never changes `TaskDifficulty`.
- Rescope or a revision bump creates a new `assessment_id` and re-assesses.
- A task card and its assessment are immutable within one revision.
- A `policy_version` change does not automatically invalidate an in-flight attempt.
- A new revision must use the current policy version.

Validation has two layers:

1. `validate_project` / PM pre-commit provides early diagnostics.
2. The `TASK_DISPATCHED` control-plane boundary is the final hard
   fail-closed validation.

`WorkflowOrchestrator` consumes an already verified `TaskDifficulty`; it does not assess difficulty.
Difficulty is not added to `TransitionCAS` fields.
Assessment identity, task revision, attempt, dispatch identity, and the
verified call chain provide the consistency binding.

## 9. Status and deferred work

- TaskDifficulty Assessment Contract: **Current — TC-13.22a**.
- Runtime / deterministic assessor: **Current — TC-13.22b.1**.
- Canonical evidence runtime: **Target — TC-13.22b.2**.
- Dispatch/approval/lifecycle wiring: **Target — TC-13.22b.3**.
- PortfolioScheduler: **Target**.
- WorktreeLifecycleManager: **Target**.
- Interface #22 status is unchanged.

The deterministic assessor is a pure policy module only; canonical evidence
runtime and dispatch/approval/lifecycle wiring remain deferred.
