# ADR 001 — MAD × AgentDesk Integration Contract

## Status

Accepted (Target).  This ADR records the integration contract between the
Multi-Agent Decision (`mad`) CLI and the AgentDesk Skill.  Interfaces
marked **Current** exist and are callable today; interfaces marked
**Target** are specified but not yet implemented.

## Interface Status

| # | Interface | Status | Implemented by | Notes |
|---|-----------|--------|----------------|-------|
| 1 | `mad agents` (TSV) | **Current** | N/A (MVP) | Tab-separated `id name adapter enabled` to stdout |
| 2 | `mad agents --format json` | **Target** | TC-13.2 | `mad.agents/v1` — root object with `schema_version` + `agents` array |
| 3 | `mad deliberate --format json` | **Current** | N/A (MVP) | `RunResult.to_dict()` to stdout; no public `schema_version` |
| 4 | `mad.run-result/v1` schema | **Target** | TC-13.2 | Add `schema_version` only; keep all existing field shapes |
| 5 | `mad audit` sub-command | **Target** | TC-13.15 | `mad.audit-result/v1`; structured audit of a delivery |
| 6 | `MAD_HOME` environment variable | **Current** | N/A (MVP) | Override data directory; read by `app_home()` |
| 7 | `MAD_PARTICIPANT` recursion guard | **Current** | N/A (MVP) | Set to `"1"` in subprocess env to prevent re-entry |
| 8 | Claude CliAdapter (read-only) | **Current** | N/A (MVP) | `--permission-mode plan`, tools limited to Read/Glob/Grep/WebSearch/WebFetch |
| 9 | AgentDesk `model-bindings/v2` | **Current** | TC-13.3 | Per-binding: `provider`, `model_id`, `tier`, `deliberation_tier`, `context_window_tokens`, `capabilities`, `enabled` |
| 10 | AgentDesk PM lease | **Current** | N/A (existing) | `agentdesk.pm-lease/v1` in `.agentdesk/runtime/` |
| 11 | AgentDesk event / outbox | **Current** | N/A (existing) | `agentdesk.state-event/v2`, `agentdesk.outbox-message/v2` |
| 12 | AgentDesk double-commit protocol | **Current** | N/A (existing) | `implementation_commit` → `report_commit` |
| 13 | AgentDesk MAD Decision Gateway | **Current** | TC-13.6 | Config-driven subprocess invocation of `mad` for planning/deliberation |
| 14 | AgentDesk WorkerAdapter + four-tier slots | **Target** | TC-13.9 | Basic/Standard/Advanced/Expert; provider/model from bindings only |
| 15 | AgentDesk WorkerSlotLease | **Target** | TC-13.10 | `agentdesk.worker-slot-lease/v1` |
| 16 | AgentDesk ControlPlaneTransitionService | **Target** | TC-13.11 | CAS-write tasks, immutable events, replayable outbox |
| 17 | AgentDesk ApprovalGate | **Target** | TC-13.12 | TASK_APPROVAL with structured scope (dispatch/accept/integrate) |
| 18 | AgentDesk EscalationService | **Target** | TC-13.13 | Difficulty escalation independent of rate-limit |
| 19 | AgentDesk RateLimit service | **Target** | TC-13.14 | Provider rate-limit handling independent of escalation |
| 20 | AgentDesk MadAuditGateway | **Target** | TC-13.16 | Subprocess invocation of `mad audit` with worktree validation |
| 21 | AgentDesk StateProvider (read-only) | **Target** | TC-13.17 | Read-only access to tasks, events, outbox, acceptances |
| 22 | AgentDesk WorkflowOrchestrator | **Target** | TC-13.18 | Central scheduler integrating all services |
| 23 | E2E / Recovery tests | **Target** | TC-13.19 | End-to-end validation and recovery scenarios |
| 24 | AgentDesk HTML Dashboard | **Target** | TC-13.20 | Read-only dashboard via StateProvider |
| 25 | ADR status update (Target → Current) | **Target** | TC-13.21 | Update this ADR after all implementations complete |
| 26 | `agentdesk.mad-refs/v1` runtime schema | **Current** | TC-13.6 | Gitignored runtime record of MAD invocations |
| 27 | AgentDesk shared core data types | **Current** | TC-13.4 | `TaskDifficulty`, `MadDeliberationDepth`, `WorkerKind` enums; no budget calculation or WorkerAdapter implementation |
| 28 | AgentDesk ContextBudgetPolicy | **Current** | TC-13.5.1 | Per-tier budget: 20% / 35% / 50% / 65% with 64k / 128k / 256k / 512k hard caps; min(floor %, cap); six-field BudgetResult; retains ≥35% reserved; depends on TC-13.4 |
| 29 | AgentDesk DispatcherAgentGateway | **Target** | TC-13.7 | Frozen contract (§2.10); execution-only single-shot agent CLI boundary; depends on TC-13.4, TC-13.6 |
| 30 | Claude Code CLI contract | **Target** | TC-13.8 | Public CLI interface contract for `claude` invocation; depends on TC-13.4 |

---

## 1. Current — What Exists Today

### 1.1 MAD Current Capabilities

- **`mad agents`** prints a TSV table to stdout: `id\tname\tadapter\tenabled`.
  There is no `--format json` flag, no structured output.
- **`mad deliberate --format json`** prints the result of `RunResult.to_dict()`
  to stdout.  The JSON object contains `deliberation_id`, `status`, `report`,
  `archive_path`, `warnings`, `participants`, `convergence`, and `plan`.
  There is **no `schema_version`** field.
  - `status` is a Chinese string: `"完成"` or `"带警告完成"`.
  - `participants` is a flat `list[str]` of agent IDs.
  - `convergence` and `plan` use their current shapes as produced by the engine.
- **`mad resume --format json`** prints the same shape as `deliberate`.
- **`mad audit` does not exist.**  There is no audit sub-command.
- **`MAD_HOME`** is read by `config.app_home()`; when set, it overrides the
  default platform data directory.  This is the mechanism for isolating MAD
  state per project or per Gateway instance.
- **`MAD_PARTICIPANT="1"`** is set in the subprocess environment by every
  `CliAdapter.invoke()` call to prevent accidental recursive MAD invocations.
- **Claude CliAdapter** (`adapter = "claude" | "claudecode"`) invokes the
  Claude CLI with `--permission-mode plan`, limiting tool access to
  `Read,Glob,Grep,WebSearch,WebFetch`.  It does not modify files.

**Current exit codes (`mad deliberate`)**:

| Exit | Meaning |
|------|---------|
| `0` | Deliberation completed (including `status: "带警告完成"`) |
| `1` | Workflow, recovery, or report failure |
| `2` | Parameter, plan, or configuration error |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

**Current exit codes (`mad resume`)**:

| Exit | Meaning |
|------|---------|
| `0` | Resume completed |
| `1` | Workflow or recovery failure |
| `130` | User cancellation or SIGINT |

`mad resume` does **not** have a dedicated exit code `3` for insufficient
participants — that is a `mad deliberate` distinction.  Argparse-level
parameter errors on `resume` are exit code `2` (Python `argparse` default).
Uncaught configuration exceptions must not be documented as stable public
exit semantics.

**Current exit codes (`mad agents`)**:

| Exit | Meaning |
|------|---------|
| `0` | Normal output |
| `2` | Argparse parameter error |

MAD does **not**:
- Create, remove, or prune Git worktrees.
- Write to any AgentDesk control-plane file.
- Parse AgentDesk `tasks.yaml`, events, or outbox.
- Understand AgentDesk task-card or delivery-report semantics.

### 1.2 AgentDesk Current Capabilities

- **`model-bindings/v2`** maps `binding_id` → `{provider, model_id, tier,
  deliberation_tier, context_window_tokens, capabilities, enabled}`.  It is gitignored runtime config.
- **PM lease** (`agentdesk.pm-lease/v1`) records `lease_id`, `holder_id`,
  `holder_instance_id`, `lease_epoch`, `last_snapshot_commit`, `acquired_at`,
  `heartbeat_at`, `expires_at` in `.agentdesk/runtime/`.
- **Event / outbox protocol** (`agentdesk.state-event/v2`,
  `agentdesk.outbox-message/v2`) provides immutable audit trail and replayable
  dispatch intent.
- **Double-commit delivery**: Worker produces `implementation_commit` first,
  then `report_commit` referencing it.  PM's acceptance freezes
  `accepted_commit`.  Integration records `integrated_commit` separately.
- **`select_model.py`** deterministically maps role policy + task requirements
  + risk floor to a ten-field `model_selection` snapshot.
- **`validate_project.py`** (strict) proves dispatch first-state commit
  atomicity, event/outbox digest parity, and ten-field model snapshot
  consistency.
- **`validate_runtime.py`** verifies real Codex tasks, exact titles, worktree
  paths, and transport receipts.

AgentDesk does **not** currently have:
- `WorkerSlotLease` — no per-slot concurrency fencing.
- `WorkflowOrchestrator` — dispatch and state transitions are manual PM steps.
- `MAD Gateway` — no automated MAD subprocess invocation.
- `ControlPlaneTransitionService` — write logic is distributed across runbook
  procedures.
- `EscalationService` / `RateLimit` as separate services.
- `TASK_APPROVAL` with structured scope (only `MODEL_DEGRADATION_APPROVED`
  exists for model-tier exceptions).
- `StateProvider` as an explicit read-only service boundary.

---

## 2. Target — Integration Design

### 2.1 MAD Target Interfaces

| Interface | Schema | Implemented by | Description |
|-----------|--------|----------------|-------------|
| `mad agents --format json` | `mad.agents/v1` | TC-13.2 | Root object with `schema_version` + `agents` array |
| `mad deliberate --format json` | `mad.run-result/v1` | TC-13.2 | Current output plus `schema_version` at top level; backward-compatible |
| `mad audit <question> --workspace …` | `mad.audit-result/v1` | TC-13.15 | Structured audit of a delivery workspace |

**`mad.agents/v1` (Target — TC-13.2)**:

Uses a root object, not a bare array:

```json
{
  "schema_version": "mad.agents/v1",
  "agents": [
    {
      "id": "pi-deepseek",
      "name": "Pi · DeepSeek V4 Pro",
      "adapter": "pi",
      "model": "deepseek/deepseek-v4-pro",
      "enabled": true,
      "default_report": false,
      "timeout_seconds": 300,
      "context_budget": 1000000
    }
  ]
}
```

Each agent object exposes only these fields:

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Stable unique agent ID |
| `name` | string | Display name |
| `adapter` | string | Adapter type (`claude`, `pi`, `codex`, …) |
| `model` | string\|null | Model identifier |
| `enabled` | boolean | Whether eligible for preflight |
| `default_report` | boolean | Default report agent (at most one) |
| `timeout_seconds` | integer | Single-invocation timeout |
| `context_budget` | integer | Declared context token budget |

The following AgentProfile fields are **forbidden** in the public output:
- `executable` — local filesystem path; security boundary.
- `extra_args` — may contain sensitive or local configuration.
- `role` — internal prompt modifier, not a public interface field.

The contract must never claim that `mad.agents/v1` outputs all `AgentProfile`
fields.

**`mad.run-result/v1` (Target — TC-13.2)**:

Backward-compatible: the only change from Current is the addition of
`schema_version` at the top level.  All other fields keep their Current
shapes:

```json
{
  "schema_version": "mad.run-result/v1",
  "deliberation_id": "<id>",
  "status": "完成 | 带警告完成",
  "report": "<full-markdown-report>",
  "archive_path": "<absolute-path>",
  "warnings": ["<warning>"],
  "participants": ["<agent-id>", "..."],
  "convergence": {"strategy": "auto", "triggered": false, "reason": "...", "marked_participants": 0, "disputes": [], "status": "未触发"},
  "plan": {"participants": [{"id": "...", "name": "...", "adapter": "...", "model": "...", "role": "..."}], "report_agent_id": "...", "organizer_agent_id": null, "source": "manual", "depth": "deep", "critic_agent_id": null}
}
```

V1 explicitly does **not**:
- Change `status` to English enums (`"completed"`, `"completed_with_warnings"`).
- Change `participants` from `list[str]` to an object array.
- Restructure `convergence` or `plan`.

If future versions need English status strings or object-typed participants,
they must use `mad.run-result/v2`.

**`mad.audit-result/v1` (Target — TC-13.15)**:

```bash
mad audit "<question>" \
  --workspace <path> \
  --task-card-commit <sha> \
  --task-card-path <relpath> \
  --delivery-report-path <relpath> \
  --report-commit <sha> \
  --base-commit <sha> \
  --implementation-commit <sha> \
  --agents <id1,id2,...> \
  --report-agent <id> \
  --depth deep \
  --convergence auto \
  --confirm-plan \
  --format json
```

`--agents` is a **single CSV parameter** (e.g. `--agents id1,id2,id3`).

Stdout (`mad.audit-result/v1`):

```json
{
  "schema_version": "mad.audit-result/v1",
  "deliberation_id": "<uuid-or-archive-id>",
  "status": "completed | failed | blocked",
  "verdict": "pass | fail | blocked",
  "issues": [
    {
      "id": "ISS-<unique>",
      "severity": "critical | high | medium | low | info",
      "category": "security | correctness | completeness | consistency | evidence | process",
      "title": "<one-line>",
      "description": "<detailed-finding>",
      "location": {
        "file": "<relative-path>",
        "line": "<optional>",
        "commit": "<sha>"
      },
      "recommendation": "<actionable-fix>"
    }
  ],
  "evidence": [
    {
      "ref": "<evidence-id>",
      "type": "git-ancestry | git-diff | file-content | commit-message | check-output | model-output",
      "source": "<path-or-sha>",
      "summary": "<one-line>",
      "verified": true
    }
  ],
  "warnings": ["<human-readable-warning>"],
  "report": "<full-audit-report-markdown>",
  "archive_path": "<absolute-path-to-archive>",
  "participants": ["<agent-id>", "..."],
  "plan": {
    "participants": ["..."],
    "report_agent_id": "<id>",
    "organizer_agent_id": "<id-or-null>",
    "source": "organizer | manual",
    "depth": "fast | balanced | deep",
    "critic_agent_id": "<id-or-null>",
    "audit_specific": {
      "task_card_commit": "<sha>",
      "task_card_path": "<relative-path>",
      "delivery_report_path": "<relative-path>",
      "base_commit": "<sha>",
      "implementation_commit": "<sha>",
      "report_commit": "<sha>",
      "workspace": "<absolute-path>"
    }
  }
}
```

Key semantics:

- `status` (string) describes whether the audit **process** completed.  When
  exit code is `0`, `status` is fixed to `"completed"` — the audit ran to
  its natural conclusion.  `"failed"` and `"blocked"` are only used when the
  process itself did not complete normally.
- `verdict` (enum `pass | fail | blocked`) describes the **business
  conclusion** of the audit.  `verdict: "blocked"` means the audit process
  completed (`status: "completed"`) but could not reach a pass/fail
  determination due to missing or unreachable evidence.  It is not an
  infrastructure failure.
- Infrastructure failures (model crash, parse error, evidence verification
  error) are **not** encoded as verdict values — they are reported via
  non-zero exit codes and `status`.
- `participants` is `list[str]` (agent IDs), matching the shape in
  `mad.run-result/v1`.  It does not embed stage contribution details in V1.
- `verdict` is a mutually-exclusive enum: `pass`, `fail`, or `blocked`.

**Exit codes for `mad audit`**:

| Exit | Condition |
|------|-----------|
| `0` | Audit process completed — `status: "completed"`; `verdict` may be `pass`, `fail`, or `blocked` |
| `1` | Parse failure, model invocation failure, or evidence verification failure |
| `2` | Parameter, configuration, or workspace validation failure (caller error) |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

**Critical rule**: When the audit process completes normally but the
deliberation finds issues (`verdict: fail`) or cannot reach a conclusion
(`verdict: blocked`), exit code is `0`.  Exit code `1` is ONLY for
infrastructure/model failures.

**Fail-closed rule**: Unknown, malformed, or structurally invalid MAD output
(JSON that does not parse, missing `schema_version`, or unknown `verdict`
value) is treated as an audit failure by the Gateway.

### 2.2 AgentDesk Target Architecture

**AgentDesk invokes MAD only through CLI/JSON subprocess boundaries.**
It must never:
- Import MAD Python modules.
- Read or write MAD internal `state.json`, `plan.json`, or `result.json` files.
- Assume MAD archive layout stability across versions.

**Worktree ownership**:

| Action | Owner |
|--------|-------|
| `git worktree add --detach <path> <report_commit>` | AgentDesk WorkflowOrchestrator |
| Validate workspace integrity (7 checks) | MAD `audit` (before any model call) |
| `git worktree remove <path>` | AgentDesk WorkflowOrchestrator (after audit) |
| `git worktree prune` | Never automatic — manual PM recovery only |

**Gateway configuration** (`.agentdesk/runtime/gateway.yaml`, Target —
TC-13.6):

```json
{
  "schema_version": "agentdesk.gateway-config/v1",
  "mad_executable": "mad",
  "mad_home": "<absolute-path>",
  "timeout_seconds": 1800,
  "planning_agent_ids": ["pi-deepseek", "pi-minimax"],
  "planning_report_agent_id": "pi-minimax",
  "audit_agent_ids": ["pi-deepseek", "pi-minimax", "codebuddy-reviewer"],
  "audit_report_agent_id": "pi-minimax"
}
```

The Gateway sets `MAD_HOME=<mad_home>` in the subprocess environment for all
call types (`agents`, `deliberate`, `audit`).

**Data the Gateway captures from MAD stdout**:

| Field | Source | Storage |
|-------|--------|---------|
| Raw stdout bytes | Subprocess capture | Not persisted (transient) |
| SHA-256 of stdout | Computed by Gateway | `agentdesk.mad-refs/v1` → `stdout_sha256` (runtime-only) |
| Parsed `report` field | Extracted from JSON | SHA-256 of report UTF-8 bytes → `agentdesk.mad-refs/v1` → `report_sha256` (runtime-only) |
| `deliberation_id` | Parsed from JSON | `agentdesk.mad-refs/v1` → `deliberation_id` |
| `archive_path` | Parsed from JSON | `agentdesk.mad-refs/v1` → `archive_path` (runtime-only, never in Git) |
| `verdict` / `issues` | Parsed from JSON | Audit event (Git-tracked) |

### 2.3 `agentdesk.mad-refs/v1` Runtime Schema (Current — TC-13.6)

Gitignored runtime file at `.agentdesk/runtime/mad-refs.yaml`.
Strict JSON root:

```json
{
  "schema_version": "agentdesk.mad-refs/v1",
  "updated_at": "<RFC3339 UTC>",
  "refs": [
    {
      "task_id": "TC-031",
      "dispatch_id": "DSP-TC031-R2-A1-7F3C",
      "purpose": "planning | audit",
      "deliberation_id": "<mad-archive-id>",
      "depth": "deep",
      "stdout_sha256": "<sha256-hex>",
      "report_sha256": "<sha256-hex>",
      "status": "<MAD status string>",
      "archive_path": "<absolute-path>",
      "created_at": "<RFC3339 UTC>"
    }
  ]
}
```

Rules:

- `stdout_sha256` is the SHA-256 of MAD's raw stdout bytes (before JSON parse).
- `report_sha256` is the SHA-256 of the parsed `report` field's UTF-8 bytes.
- `archive_path` is absolute and must only exist in gitignored runtime — never
  in tracked files.
- The entire `mad-refs` file is gitignored and must never be committed.
- Git-tracked events may record the SHA-256 digests as references, but must
  not include `archive_path`.

### 2.4 Worker Tiers (Target — TC-13.9)

| Tier | Context Budget (% of model window) | Max Active | Escalation Behaviour |
|------|-----------------------------------|------------|---------------------|
| Basic | 20% | 2 global / 1 per worktree | Retry once same-tier → escalate to Standard |
| Standard | 35% | 2 global / 1 per worktree | Retry once same-tier → escalate to Advanced |
| Advanced | 50% | 2 global / 1 per worktree | First failure → escalate to Expert |
| Expert | 65% | 2 global / 1 per worktree | Failure → request user decision |

At least **35% of context window** is reserved for system prompt, tool
definitions, and overhead.  The per-worktree writer limit of 1 means no two
Workers may write to the same ordinary worktree concurrently.

The budget percentages and the ≥35 % reserved rule are computed by
`ContextBudgetPolicy` (TC-13.5.1 — Current).  WorkerAdapter (TC-13.9)
consumes the policy's `BudgetResult`; this section describes the
*Worker-tier behaviours* that use that budget, not the arithmetic itself.

### 2.5 Worker Slot Lease (Target — TC-13.10)

Each Worker slot lease is independent of the PM lease.  Fields:

- `lease_id` — unique per acquisition.
- `lease_epoch` — incremented on each re-acquisition; all state writes check it.
- `slot_id` — references the Worker slot.
- `holder_dispatch_id` — which dispatch holds this slot.
- `holder_instance_id` — which runtime instance holds this slot.
- `canonical_worktree` — normalised, case-insensitive real path; reparse points
  rejected.
- `acquired_at`, `heartbeat_at`, `expires_at` — lifecycle timestamps.

A **provider request permit** is independent of the Worker lifecycle slot:
rate-limiting a provider (429) does not release the Worker slot, and releasing
a Worker slot does not reset the provider rate-limit window.

### 2.6 Approval, Escalation, Event, and Outbox Separation

- **TASK_APPROVAL** (TC-13.12) uses structured scope (dispatch / accept /
  integrate), not free-text.  Each approval is for exactly one action and one
  delivery.  Revocation is an independent, immutable event.
- **Escalation** (TC-13.13 — difficulty tier change) is separate from
  **RateLimit** (TC-13.14 — provider 429 handling).  A rate-limit event must
  not change difficulty or generate `TASK_ESCALATED`.
- **Event** records what happened (audit trail).  **Outbox** records what
  should be sent (replayable intent).  They use separate ID namespaces and
  must not be conflated.

### 2.7 Skill and Dashboard Data Sharing

The Skill (PM/Worker runbooks) and the HTML Dashboard both consume data
through a **read-only StateProvider** (TC-13.17).  Neither writes to canonical
state directly — all writes go through `ControlPlaneTransitionService`
(TC-13.11).

### 2.8 Shared Core Data Types (Current — TC-13.4)

TC-13.4 freezes three shared enumerations used across the MAD–AgentDesk
integration.  These types carry **no** provider identity, model ID,
thread ID, worktree path, budget calculation, lease, slot, escalation,
retry logic, or subprocess mechanics.  They are pure semantic tags.

**All three enums use lowercase strings as their stable serialisation
values.  Unknown values MUST be treated as fail-closed — consumers must
not silently fall back to a default or guess a meaning.**

---

#### 2.8.1 `TaskDifficulty`

`TaskDifficulty` describes the **difficulty** of a task — its inherent
complexity, risk, and reasoning depth — independently of any model tier
or binding.  It is **not** a model tier, even when the string values
happen to coincide with AgentDesk model-tier labels.

| Value | Semantic |
|-------|----------|
| `basic` | Low-risk, well-bounded, mechanical or informational tasks with minimal context |
| `standard` | Routine single-module implementation, testing, documentation, known-pattern fixes |
| `advanced` | Cross-module implementation, complex debugging, ambiguous constraints, security/compat-sensitive work |
| `expert` | Architecture, critical migrations, high-risk review, complex decision support with costly failure |

Rules:

- `TaskDifficulty` strings are **not** interchangeable with
  `agentdesk.model-bindings/v2` tier labels (`basic` / `standard` /
  `advanced` / `expert`), even though the four string values are
  identical today.  Model tier describes execution-resource capability;
  difficulty describes task-intrinsic complexity.  A future
  specification may diverge the two sets; consumers must not couple
  them structurally.
- `TaskDifficulty` does **not** dictate which model to select, which
  percentage budget to apply, or which Worker slot to allocate.
- Adding a new difficulty value requires a new revision of this type
  contract.

---

#### 2.8.2 `MadDeliberationDepth`

`MadDeliberationDepth` is the **public deliberation depth** recognised
by MAD.  It is **not** an AgentDesk model deliberation tier
(`efficient` / `balanced` / `deep`), even though `balanced` and `deep`
appear in both sets.

| Value | Semantic |
|-------|----------|
| `fast` | Fast, cost-minimising deliberation for clearly-scoped questions |
| `balanced` | Standard engineering-tradeoff deliberation |
| `deep` | Multi-constraint, long-chain reasoning, or high-stakes deliberation |

Rules:

- `MadDeliberationDepth` values belong to the `mad.*` semantic space.
  The AgentDesk `deliberation_tier` enum (`efficient` / `balanced` /
  `deep`) maps to provider-specific reasoning controls and belongs to
  the `agentdesk.*` semantic space.  `fast` ≠ `efficient`; the two
  enums are separate by design.
- Gateway and WorkerAdapter code must map between the two enums
  explicitly rather than casting strings across namespaces.

---

#### 2.8.3 `WorkerKind`

`WorkerKind` describes the **logical Worker type** for dispatch.  It
carries no provider, model revision, thread ID, worktree path, or lease.

| Value | Semantic |
|-------|----------|
| `basic_agent` | Basic-tier Worker for low-complexity, mechanical tasks |
| `standard_agent` | Standard-tier Worker for routine engineering tasks |
| `advanced_agent` | Advanced-tier Worker for cross-module, constrained, or sensitive tasks |
| `expert_agent` | Expert-tier Worker for architecture, critical migrations, or high-risk decisions |

Rules:

- `WorkerKind` is a **logical slot label**, not a concrete runtime
  binding.  Mapping a `WorkerKind` to a specific provider, model
  revision, or worktree is the responsibility of the WorkerAdapter
  (TC-13.9) and WorkerSlotLease (TC-13.10).
- `WorkerKind` does **not** encode concurrency limits (2 global / 1 per
  worktree), budget percentages, or escalation behaviour — those
  belong to ContextBudgetPolicy (TC-13.5.1) and WorkerAdapter (TC-13.9).
- A `WorkerKind` value must not be used as a model tier, and a model
  tier must not be used as a `WorkerKind`.

---

#### 2.8.4 Non-Goals of TC-13.4

TC-13.4 explicitly does **not** include:

- Budget calculation, context-window arithmetic, or token budgeting
  (→ TC-13.5.1)
- Model selection, provider binding, or `select_model.py` logic
- Any subprocess invocation, CLI call, or filesystem write
- Worker scheduling, slot allocation, lease acquisition, or
  concurrency fencing (→ TC-13.9, TC-13.10)
- Retry, escalation, or rate-limit logic (→ TC-13.13, TC-13.14)
- Task-card, outbox, or event schema changes
- A runtime data file; these enums are compile-time / specification
  constants only

---

---

### 2.9 ContextBudgetPolicy (Current — TC-13.5.1)

`ContextBudgetPolicy` (TC-13.5.1) computes a per-task token budget from
`TaskDifficulty` and a model's `context_window_tokens`.  It is a **pure
arithmetic strategy** with no I/O, no provider knowledge, and no
WorkerAdapter mechanics.

The budget is the **smaller** of the percentage-floor result and a
per-difficulty hard cap — this prevents oversized budgets on very large
context windows while still reserving at least 35 % of the window.

**Public API** (`skills/agentdesk/scripts/context_budget.py`):

| Symbol | Kind | Description |
|--------|------|-------------|
| `compute_budget(context_window_tokens, difficulty)` | function | Returns a `BudgetResult` |
| `BudgetResult` | `NamedTuple` | Six-field immutable result |

**Inputs**:

| Parameter | Type | Constraint |
|-----------|------|------------|
| `context_window_tokens` | `int` | Positive (≥1), non-bool, from a validated `model-bindings/v2` binding |
| `difficulty` | `TaskDifficulty` | Must be a `TaskDifficulty` enum member; bare strings and other enum types are rejected |

**Frozen percentages and hard caps**:

| `TaskDifficulty` | Budget % | Hard cap (tokens) |
|------------------|----------|-------------------|
| `BASIC` | 20 | 64,000 |
| `STANDARD` | 35 | 128,000 |
| `ADVANCED` | 50 | 256,000 |
| `EXPERT` | 65 | 512,000 |

**Integer arithmetic** (no floating-point, no `Decimal`):

```text
percentage_budget  = context_window_tokens × budget_percent // 100   (floor)
budget_tokens      = min(percentage_budget, budget_cap_tokens)
reserved_tokens     = context_window_tokens - budget_tokens
```

Callers cannot override the percentages or the caps — both tables are
module-private and frozen.

**Invariants** (enforced at computation time):

```text
budget_tokens >= 1
budget_tokens <= percentage_budget
budget_tokens <= budget_cap_tokens
budget_tokens + reserved_tokens == context_window_tokens
reserved_tokens >= (context_window_tokens × 35 + 99) // 100   (ceil of 35 %)
```

If `budget_tokens` would round to 0 (window too small for the requested
difficulty), `compute_budget()` raises `ValueError`.

**Result fields** (`BudgetResult`):

| Field | Type | Description |
|-------|------|-------------|
| `context_window_tokens` | `int` | As supplied |
| `difficulty` | `TaskDifficulty` | As supplied |
| `budget_percent` | `int` | 20 / 35 / 50 / 65 |
| `budget_cap_tokens` | `int` | Frozen hard cap for this difficulty (64,000 / 128,000 / 256,000 / 512,000) |
| `budget_tokens` | `int` | `min(floor percentage, cap)` |
| `reserved_tokens` | `int` | `context_window_tokens - budget_tokens` |

**Explicit non-goals**:

- `BudgetResult` is **not** written to `model_selection`, dispatch,
  outbox, event, or delivery report evidence.
- The percentage table and cap table are module-private; callers
  cannot override either.
- `compute_budget()` does **not** perform any I/O, subprocess call,
  filesystem write, environment-variable read, or network access.
- The module does **not** implement WorkerAdapter, slot allocation,
  lease, scheduling, retry, or escalation.
- The module does **not** map `TaskDifficulty` to `WorkerKind`,
  `MadDeliberationDepth`, or any other enum.

**Consumption by WorkerAdapter (TC-13.9)**:  WorkerAdapter calls
`compute_budget()` as a pure function and uses `budget_tokens` to
constrain the Worker's effective context window.  WorkerAdapter is
responsible for sourcing `context_window_tokens` from the selected
model binding's `selected_context_window_tokens` field.  WorkerAdapter
is a Target (TC-13.9) — ContextBudgetPolicy is Current and available
today.

---

### 2.10 DispatcherAgentGateway — Frozen Contract (Target — TC-13.7)

TC-13.7 defines an **execution-only** boundary for a single agent CLI
subprocess dispatch.  This section is the Frozen Contract — the freeze
of all TC-13.7 public interfaces — and does **not**
mark TC-13.7 as Current.

TC-13.7 freezes:

1.  **responsibility boundary** — what the Gateway does and what it
    explicitly does not do;
2.  **immutable input types** — `DispatchIdentity`,
    `ModelSelectionSnapshot`, `DispatchRequest`;
3.  **provider adapter Protocol** — `AgentCliProvider` and the
    `AgentCliInvocation` value it produces;
4.  **immutable result type** — `DispatchResult` (exit‑0 success only);
5.  **failure semantics** — exception‑only paths for non‑zero exit,
    timeout, cancellation, and structural errors;
6.  **output semantics** — stdout/stderr as opaque bytes with SHA‑256
    hashing;
7.  **executable security** — absolute‑path / ``shutil.which()``
    resolution, ``shell=False`` locked;
8.  **state & persistence boundary** — zero file writes, zero
    canonical‑state, event, outbox, or report writes;
9.  **timeout & cancellation** — full process‑tree termination;
10. **dependency boundary** — what TC-13.7 consumes vs what is deferred
    to TC-13.8, TC-13.9, TC-13.10, TC-13.11, and TC-13.18.

---

#### 2.10.1 Exact Responsibility Boundary

TC-13.7 is a **single‑shot**, **config‑driven**, **provider‑agnostic**
agent CLI execution gateway.  Every invocation:

* strictly validates a frozen, immutable dispatch request and its
  ten‑field `ModelSelectionSnapshot`;
* resolves the provider adapter from a registry keyed by
  ``selected_model_provider``;
* constructs a safe subprocess invocation via the adapter;
* launches **exactly one** subprocess;
* captures raw stdout and stderr as `bytes`;
* handles normal exit (code 0), non‑zero exit, timeout, and caller
  cancellation;
* returns an immutable, classified ``DispatchResult`` (success) **or**
  raises a precise exception (every other outcome).

TC-13.7 **explicitly does NOT**:

* write canonical state, dispatch files, events, outbox messages,
  delivery reports, transport receipts, or any other Git‑tracked
  artifact;
* retry, escalate, approve, rate‑limit, or re‑queue;
* consume ``WorkerKind``, ``TaskDifficulty``,
  ``ContextBudgetPolicy``, or ``BudgetResult``;
* acquire, release, or validate slot leases or fencing tokens;
* know Claude CLI specifics (`--permission-mode`, tool allowlists,
  etc.);
* implement WorkerAdapter lifecycle or WorkflowOrchestrator
  coordination;
* invoke ``mad`` in any form — the MAD Gateway (TC-13.6) and ``mad``
  refs are separate domains.

---

#### 2.10.2 DispatchIdentity — Frozen Audit Identity

Immutable, four‑field identity carried by every request and result.
All values originate from an already‑frozen dispatch / outbox committed
before the Gateway is invoked.

| Field | Type | Rule |
|-------|------|------|
| ``task_id`` | ``str`` | Non‑empty |
| ``revision`` | ``int`` | Non‑bool, ≥ 1 |
| ``attempt`` | ``int`` | Non‑bool, ≥ 1 |
| ``dispatch_id`` | ``str`` | Non‑empty |

The Gateway **must not** generate, modify, or derive any of these
values.  They are opaque audit markers from the caller's perspective.

---

#### 2.10.3 ModelSelectionSnapshot — Frozen Ten‑Field Snapshot

An immutable snapshot whose key set must **exactly** match the
deterministic selector output (TC-13.3 / TC-13.4).  Extra keys and
missing keys are both rejected.

| # | Field | Type |
|---|-------|------|
| 1 | ``required_model_tier`` | ``str`` |
| 2 | ``required_model_capabilities`` | ``list[str]`` |
| 3 | ``model_binding_id`` | ``str`` |
| 4 | ``selected_model_provider`` | ``str`` |
| 5 | ``selected_model_id`` | ``str`` |
| 6 | ``selected_model_tier`` | ``str`` |
| 7 | ``selected_deliberation_tier`` | ``str`` |
| 8 | ``selected_context_window_tokens`` | ``int`` (non‑bool, ≥ 1) |
| 9 | ``selected_model_capabilities`` | ``list[str]`` |
| 10 | ``model_degradation_approval_id`` | ``str`` or ``null`` |

Rules:

* ``provider``, ``model_id``, and ``deliberation_tier`` for the
  subprocess call are taken **only** from this snapshot — callers must
  not supply an independent override.
* ``model_binding_id`` is the **only** binding identifier.  There is
  no separate ``provider_config_id`` — the snapshot's
  ``selected_model_provider`` is the adapter registry key.
* ``selected_model_tier`` is stored but not consumed by TC-13.7;
  it is a passthrough audit field.
* ``selected_context_window_tokens`` is stored but not consumed by
  TC-13.7; budget arithmetic belongs to ContextBudgetPolicy
  (TC-13.5.1) and WorkerAdapter (TC-13.9).

---

#### 2.10.4 DispatchRequest — Frozen Input

Exactly five fields — every field has a verifiable origin in an
already‑frozen dispatch/outbox or runtime configuration.

| # | Field | Type | Rule |
|---|-------|------|------|
| 1 | ``identity`` | ``DispatchIdentity`` | Frozen audit identity |
| 2 | ``workspace`` | ``Path`` | Absolute, existing directory |
| 3 | ``prompt`` | ``str`` | Non‑empty; sole source of task content |
| 4 | ``model_selection`` | ``ModelSelectionSnapshot`` | Exactly ten fields, validated |
| 5 | ``timeout_seconds`` | ``int`` | Non‑bool, ≥ 1 |

Fields **explicitly excluded** from TC-13.7 (deferred to later TCs):

* ``project_root`` — Gateway resolves paths from ``workspace``.
* ``provider_config_id`` — ``selected_model_provider`` from the
  snapshot is the adapter registry key.
* ``environment_allowlist`` — environment filtering is deferred to
  TC-13.8 per‑provider contracts; TC-13.7 copies the full parent
  environment and applies adapter‑declared overrides.
* ``stdin_bytes`` — ``prompt`` is the **sole** task content source;
  the adapter produces ``stdin`` bytes from it inside
  ``build_invocation``.  No separate caller‑supplied stdin channel
  exists.
* ``task_difficulty``, ``worker_kind``, ``budget_tokens``,
  ``lease_id``, ``slot_id``, ``retry_count``,
  ``escalation_level``, ``approval_id``, ``rate_limit_token``.

Rules:

* ``workspace`` must exist and be an absolute directory.
* ``prompt`` is the **only** source of task content — the adapter
  derives ``stdin`` from it.
* ``timeout_seconds`` is a positive integer (non‑bool).
* The entire request is frozen before Gateway invocation; the Gateway
  does not enrich, derive, or persist any additional fields.

---

#### 2.10.5 Provider Adapter Protocol

TC-13.7 defines ``AgentCliProvider`` as a ``typing.Protocol``
(runtime‑checkable).  TC-13.7 itself only ships the Protocol and a
registry; it does **not** bundle any real provider implementation.

**Required attribute:**

| Name | Type | Rule |
|------|------|------|
| ``provider_id`` | ``str`` | Must equal ``selected_model_provider`` from the snapshot |

**Required method:**

| Method | Returns | Description |
|--------|---------|-------------|
| ``build_invocation(request)`` | ``AgentCliInvocation`` | Produce a fully‑resolved subprocess invocation from a validated ``DispatchRequest`` |

``build_invocation`` receives the **entire** frozen ``DispatchRequest``
(identity + workspace + prompt + snapshot + timeout) and returns a
self‑contained ``AgentCliInvocation``.

``parse_result()`` is **not** part of the TC-13.7 Protocol.  Provider‑
specific stdout parsing is deferred to TC-13.8 / TC-13.9.

A ``FakeAgentCliProvider`` may exist in ``tests/`` only — it must not
appear in any production module.

**Registry lookup**:

The Gateway resolves the adapter via a module‑level registry keyed by
``provider_id``.  Lookup for an unknown ``provider_id`` is fail‑closed:
it raises ``ProviderNotSupportedError`` before any subprocess is
launched.

---

#### 2.10.6 AgentCliInvocation — Frozen Invocation Value

Returned by ``AgentCliProvider.build_invocation()``.  Represents a
fully‑resolved, safe subprocess invocation.

| # | Field | Type | Rule |
|---|-------|------|------|
| 1 | ``executable`` | ``str`` | Absolute path or plain name (resolved via ``shutil.which``) |
| 2 | ``argv`` | ``tuple[str, ...]`` | Each element is exactly one argument; no shell concatenation |
| 3 | ``stdin`` | ``bytes`` or ``None`` | Derived from ``DispatchRequest.prompt``; ``None`` → ``DEVNULL`` |
| 4 | ``env_overrides`` | ``tuple[tuple[str, str], ...]`` | Pairs of ``(KEY, value)``; keys are unique |

Rules:

* ``executable`` is a **single** path or command name — it must not
  embed arguments, flags, or shell metacharacters.  Spaces in the
  path are permitted (the path is passed as a single ``exec*``
  argument).
* ``argv`` is an immutable tuple; each element is one argument.
  ``shell=True`` is **never** used — the Gateway exclusively uses
  ``asyncio.create_subprocess_exec``.
* ``stdin`` is produced by the adapter from the sole ``prompt``.
  When ``None`` the Gateway passes ``DEVNULL``.
* ``env_overrides`` keys are unique.  The Gateway copies the full
  parent environment and then applies each ``(KEY, value)`` pair.
* The adapter **does not** receive the full parent environment —
  it only declares the overrides it needs.
* The Gateway **must not** log, persist, or return the content of
  environment variables in any result, exception, event, or report.
  API keys must never appear in Gateway output.

---

#### 2.10.7 DispatchResult — Frozen Success Result (Exit 0 Only)

``DispatchResult`` is returned **only** when the subprocess exits
cleanly with code 0.  Every other outcome uses exceptions (§2.10.8).

| # | Field | Type | Rule |
|---|-------|------|------|
| 1 | ``identity`` | ``DispatchIdentity`` | Echoed from request |
| 2 | ``provider`` | ``str`` | Echoed from ``selected_model_provider`` |
| 3 | ``model_id`` | ``str`` | Echoed from ``selected_model_id`` |
| 4 | ``duration_seconds`` | ``float`` | Wall‑clock duration |
| 5 | ``stdout`` | ``bytes`` | Raw subprocess stdout |
| 6 | ``stderr`` | ``bytes`` | Raw subprocess stderr |
| 7 | ``stdout_sha256`` | ``str`` | 64 lowercase hex of raw ``stdout`` bytes |
| 8 | ``stderr_sha256`` | ``str`` | 64 lowercase hex of raw ``stderr`` bytes |

Fields **explicitly excluded**:

* ``timed_out`` — timeout uses exceptions, not a flag.
* ``cancelled`` — cancellation uses exceptions, not a flag.
* ``process_id`` — runtime‑only identifier; not exposed.
* ``command_receipt`` — adapter‑internal detail; not exposed.
* ``archive_path``, ``report_sha256``, ``deliberation_id``, ``status``
  — these are MAD Gateway (TC-13.6) concepts, not DispatcherAgentGateway
  concepts.
* ``executor_model`` extension fields — the Gateway does not decide
  what enters the terminal executor_model; that is the caller's
  responsibility (TC-13.9 / TC-13.11).

Rules:

* ``provider`` and ``model_id`` are **echoed** from the frozen
  snapshot — the Gateway cannot substitute or resolve them.
* ``stdout`` and ``stderr`` are raw ``bytes`` — the Gateway does not
  decode, truncate, or interpret them.
* SHA‑256 digests are computed on the **original bytes** before any
  processing.
* The raw bytes are an in‑memory return value only; the Gateway does
  **not** persist them and does **not** decide whether they enter a
  Git‑tracked artifact.

---

#### 2.10.8 Failure Semantics — Exception‑Only

Every non‑success path raises a specific exception.  ``DispatchResult``
is **never** returned for a non‑zero exit, timeout, or cancellation.

| Condition | Exception | Carries |
|-----------|-----------|---------|
| Structural input error | ``DispatchInputError`` | field name, value |
| Snapshot validation failure | ``DispatchSnapshotError`` | missing/extra key details |
| Provider not registered | ``ProviderNotSupportedError`` | ``provider_id`` |
| Executable not found / not executable | ``ExecutableNotFoundError`` | ``executable`` |
| Invocation structure error | ``DispatchInvocationError`` | adapter‑side validation failure |
| Subprocess launch failure (OSError) | ``DispatchLaunchError`` | original exception |
| Subprocess exceeds ``timeout_seconds`` | ``DispatchTimeoutError`` | ``timeout_seconds`` |
| Caller cancellation | ``DispatchCancelledError`` | — |
| Non‑zero exit code | ``DispatchNonZeroExitError`` | ``exit_code``, ``stdout_sha256``, ``stderr_sha256``, length‑capped stderr preview |

``DispatchNonZeroExitError`` may carry:

* ``exit_code`` — the raw integer exit code;
* ``stdout_sha256`` / ``stderr_sha256`` — SHA‑256 of raw bytes;
* ``stderr_preview`` — a **length‑capped** (max 500 chars) preview,
  decoded with ``errors="replace"`` to guard against non‑UTF‑8.

``DispatchNonZeroExitError`` must **never** carry:

* the full environment;
* the full argv;
* the raw ``stdout`` or ``stderr`` bytes;
* any un‑redacted secret material.

There is no ``DispatchOutputDecodeError`` — the Gateway treats
stdout/stderr as opaque bytes and does not attempt UTF‑8 decoding.
Provider‑specific decoding belongs to TC-13.8 / TC-13.9.

Output size limits are **not** frozen as a TC-13.7 public
configuration knob.  Naïve post‑``communicate()`` length checks do not
prevent memory pressure; a proper streaming or capped‑reader design is
deferred as a future security hardening item.

---

#### 2.10.9 Executable Security

Frozen rules:

1. If ``executable`` is an absolute path, it must point to a regular
   file with the execute bit set (``os.access(path, os.X_OK)``).
2. If ``executable`` is a plain name (no directory separator), it is
   resolved via ``shutil.which()`` on the parent ``PATH``.
3. Spaces in the executable path are permitted — the path is passed
   as a single argument to ``exec*``.
4. ``executable`` must not embed command arguments — it is a pure
   path.
5. Each element of ``argv`` is one argument; ``argv`` is never a
   shell string.
6. The Gateway uses ``asyncio.create_subprocess_exec`` exclusively.
7. ``shell=True`` and ``create_subprocess_shell`` are **never** used.
   Tests must prove this by verifying the subprocess‑creation mock
   receives no ``shell`` keyword argument.

---

#### 2.10.10 State & Persistence Boundary

TC-13.7 is a **pure execution boundary** with **zero file writes**:

* It does **not** read canonical dispatch files, ``tasks.yaml``,
  events, or outbox.
* It does **not** write any file — not to ``.agentdesk/runtime/``,
  not to ``docs/pm/``, not to any Git‑tracked path.
* It does **not** write transport receipts, dispatch receipts, or
  callback receipts.
* It does **not** validate the terminal ``executor_model`` against
  the snapshot.
* It does **not** decide which result fields are copied into a
  delivery report or event — that is the caller's responsibility
  (TC-13.9 / TC-13.11).

The caller receives the ``DispatchResult`` in memory and may use its
fields according to later TC contracts.  This ADR does **not** pre‑
authorise writing ``duration_seconds``, ``exit_code``, or SHA
digests into ``executor_model`` — those decisions belong to the TC
that defines ``executor_model`` semantics.

---

#### 2.10.11 Timeout & Cancellation

Both timeout and caller cancellation must terminate the **entire**
process tree:

* **Timeout**: the Gateway uses ``asyncio.wait_for`` with the
  request's ``timeout_seconds``.  When the deadline is exceeded, the
  Gateway terminates the process tree and raises
  ``DispatchTimeoutError``.
* **Cancellation**: when the calling ``asyncio.Task`` is cancelled,
  the Gateway terminates the process tree and raises
  ``DispatchCancelledError``.

The **exact** termination sequence (SIGTERM → grace → SIGKILL on
POSIX; ``taskkill /T /F`` on Windows) follows the pattern validated
in TC-13.6 but must be implemented independently — TC-13.7 must not
import TC-13.6's ``mad_gateway`` module (which carries MAD‑specific
command, environment, and parser logic).

The Gateway does **not** return partial stdout after timeout or
cancellation.  The Gateway does **not** retry.

---

#### 2.10.12 Dependency Boundary

| TC | Relationship to TC-13.7 |
|----|--------------------------|
| **TC-13.4** | Consumed — ``core_types`` enums are the only allowed import from the shared type layer |
| **TC-13.6** | Depends on — the Gateway pattern (subprocess lifecycle, executable resolution, process‑tree termination) is validated by TC-13.6; TC-13.7 must implement its own without importing ``mad_gateway`` |
| **TC-13.8** | Separate — defines the Claude‑specific ``AgentCliProvider`` implementation and CLI contract; TC-13.7 must not reference Claude |
| **TC-13.9** | Consumer — WorkerAdapter calls the Gateway, maps ``WorkerKind``, applies budget, and manages delivery lifecycle |
| **TC-13.10** | Separate — slot lease and fencing are independent of a single subprocess execution |
| **TC-13.11** | Consumer — ControlPlaneTransitionService invokes the Gateway and writes canonical state / events / outbox from the result |
| **TC-13.18** | Consumer — WorkflowOrchestrator coordinates Gateway calls |

---

#### 2.10.13 Status

This section (§2.10) is a **contract freeze**, not an implementation
completion marker.

* ADR Interface Status row #29 “AgentDesk DispatcherAgentGateway”
  remains **Target**.
* TC-13.8 and all subsequent Target interfaces remain **Target**.
* TC-13.7 will be marked **Current** only when
  ``dispatcher_gateway.py`` and matching tests are committed.

---

## 3. Ownership Boundaries

| Domain | Owned by | Description |
|--------|----------|-------------|
| Deliberation execution | **MAD** | Agent preflight, stage progression, transcript, report generation |
| Audit verdict & issues | **MAD** | Structured findings, evidence assessment, `mad.audit-result/v1` |
| MAD archives | **MAD** | `MAD_HOME/deliberations/<id>/` — state, transcript, diagnostics, result |
| Task state machine | **AgentDesk** | `tasks.yaml` — Draft → Ready → … → Integrated |
| Dispatch & attempt lifecycle | **AgentDesk** | `dispatch_id`, `attempt`, `current_dispatch`, `model_selection` |
| Worker slot & lease | **AgentDesk** | `WorkerSlotLease`, slot allocation, concurrency fencing |
| Worktree lifecycle | **AgentDesk** | Create at `report_commit`, remove after audit |
| Approval & revocation | **AgentDesk** | TASK_APPROVAL events, MODEL_DEGRADATION_APPROVED/REVOKED |
| Rate limiting | **AgentDesk** | Provider 429 handling, backoff, notification |
| Escalation | **AgentDesk** | Difficulty tier progression |
| Double-commit delivery | **AgentDesk** | `implementation_commit` → `report_commit` → acceptance → integration |
| HTML Dashboard | **AgentDesk** | Read-only views via StateProvider |

**Hard boundary rule**: Neither side directly reads or writes the other's
internal state files.  AgentDesk never opens `MAD_HOME/deliberations/*/state.json`
or `result.json`.  MAD never opens `docs/pm/state/tasks.yaml` or
`.agentdesk/runtime/*.yaml`.

---

## 4. Schema Namespaces

| Prefix | Owner | Examples |
|--------|-------|----------|
| `mad.*` | MAD | `mad.agents/v1`, `mad.run-result/v1`, `mad.audit-result/v1` |
| `agentdesk.*` | AgentDesk | `agentdesk.tasks/v2`, `agentdesk.state-event/v2`, `agentdesk.worker-slot-lease/v1`, `agentdesk.mad-refs/v1` |

No schema version from one namespace may be re-declared in the other.
Cross-references (e.g. an AgentDesk event referencing a `deliberation_id`)
use opaque foreign keys, not embedded schema objects.

---

## 5. Future Task Cards

| Task Card | Description | Depends on |
|-----------|-------------|------------|
| TC-13.2 | MAD `agents --format json` + `mad.run-result/v1` schema | This ADR |
| TC-13.4 | AgentDesk shared core data types (`TaskDifficulty`, `MadDeliberationDepth`, `WorkerKind`) | TC-13.3 |
| TC-13.5.1 | AgentDesk ContextBudgetPolicy (per-tier percentages: 20% / 35% / 50% / 65% with 64k / 128k / 256k / 512k hard caps; ≥35% reserved) | TC-13.4 |
| TC-13.6 | AgentDesk MAD Decision Gateway + `agentdesk.mad-refs/v1` | TC-13.2, TC-13.4 |
| TC-13.7 | AgentDesk DispatcherAgentGateway (frozen contract §2.10; execution-only single-shot agent CLI boundary) | TC-13.4, TC-13.6 |
| TC-13.8 | Claude Code CLI contract (public CLI interface for `claude` invocation) | TC-13.4 |
| TC-13.9 | WorkerAdapter + four-tier Worker slots | TC-13.5.1, TC-13.7, TC-13.8 |
| TC-13.10 | WorkerSlotLease implementation | TC-13.9 |
| TC-13.11 | ControlPlaneTransitionService | TC-13.2 |
| TC-13.12 | ApprovalGate (TASK_APPROVAL structured scope) | TC-13.11 |
| TC-13.13 | EscalationService | TC-13.11 |
| TC-13.14 | RateLimit service | TC-13.11 |
| TC-13.15 | MAD `audit` sub-command (`mad.audit-result/v1`) | TC-13.2 |
| TC-13.16 | AgentDesk MadAuditGateway | TC-13.15 |
| TC-13.17 | StateProvider (read-only) | TC-13.11 |
| TC-13.18 | WorkflowOrchestrator (full integration) | TC-13.10, 13.11, 13.12, 13.13, 13.14, 13.16, 13.17 |
| TC-13.19 | E2E / Recovery tests | TC-13.18 |
| TC-13.20 | HTML Dashboard | TC-13.17, TC-13.19 |
| TC-13.21 | ADR status update (Target → Current) | TC-13.19 |

---

## 6. Non-Goals (explicit exclusions)

- MAD will **not** be vendored, forked, or embedded inside AgentDesk.
- AgentDesk will **not** implement its own multi-agent deliberation engine.
- The Gateway will **not** parse MAD internal state files (`state.json`,
  `plan.json`); it only consumes stdout JSON.
- This ADR does **not** change the MAD TypeScript migration roadmap
  (ADR-0012, ADR-0013, ADR-0014).
- The HTML Dashboard is read-only; it will **not** drive state transitions.
