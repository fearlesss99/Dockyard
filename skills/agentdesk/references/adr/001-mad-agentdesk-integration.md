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
| 9 | AgentDesk `model-bindings/v1` | **Current** | N/A (existing) | Per-binding: `provider`, `model_id`, `tier`, `deliberation_tier`, `capabilities`, `enabled` |
| 10 | AgentDesk PM lease | **Current** | N/A (existing) | `agentdesk.pm-lease/v1` in `.agentdesk/runtime/` |
| 11 | AgentDesk event / outbox | **Current** | N/A (existing) | `agentdesk.state-event/v2`, `agentdesk.outbox-message/v2` |
| 12 | AgentDesk double-commit protocol | **Current** | N/A (existing) | `implementation_commit` → `report_commit` |
| 13 | AgentDesk MAD Decision Gateway | **Target** | TC-13.6 | Config-driven subprocess invocation of `mad` for planning/deliberation |
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
| 26 | `agentdesk.mad-refs/v1` runtime schema | **Target** | TC-13.6 | Gitignored runtime record of MAD invocations |

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

- **`model-bindings/v1`** maps `binding_id` → `{provider, model_id, tier,
  deliberation_tier, capabilities, enabled}`.  It is gitignored runtime config.
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
  + risk floor to a nine-field `model_selection` snapshot.
- **`validate_project.py`** (strict) proves dispatch first-state commit
  atomicity, event/outbox digest parity, and nine-field model snapshot
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

### 2.3 `agentdesk.mad-refs/v1` Runtime Schema (Target — TC-13.6)

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
      "status": "completed | completed_with_warnings",
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
| Basic | 15% | 2 global / 1 per worktree | Retry once same-tier → escalate to Standard |
| Standard | 30% | 2 global / 1 per worktree | Retry once same-tier → escalate to Advanced |
| Advanced | 50% | 2 global / 1 per worktree | First failure → escalate to Expert |
| Expert | 65% | 2 global / 1 per worktree | Failure → request user decision |

At least **35% of context window** is reserved for system prompt, tool
definitions, and overhead.  The per-worktree writer limit of 1 means no two
Workers may write to the same ordinary worktree concurrently.

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
| TC-13.6 | AgentDesk MAD Decision Gateway + `agentdesk.mad-refs/v1` | This ADR |
| TC-13.9 | WorkerAdapter + four-tier Worker slots | TC-13.6 |
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
