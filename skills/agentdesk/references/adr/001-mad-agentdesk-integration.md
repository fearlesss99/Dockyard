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
| 2 | `mad agents --format json` | **Target** | TC-13.1 | `mad.agents/v1` JSON array to stdout |
| 3 | `mad deliberate --format json` | **Current** | N/A (MVP) | `RunResult.to_dict()` to stdout; no public `schema_version` |
| 4 | `mad.run-result/v1` schema | **Target** | TC-13.1 | Formalise the existing `RunResult` output with `schema_version` |
| 5 | `mad audit` sub-command | **Target** | TC-13.1 | `mad.audit-result/v1`; structured audit of a delivery |
| 6 | `MAD_HOME` environment variable | **Current** | N/A (MVP) | Override data directory; read by `app_home()` |
| 7 | `MAD_PARTICIPANT` recursion guard | **Current** | N/A (MVP) | Set to `"1"` in subprocess env to prevent re-entry |
| 8 | Claude CliAdapter (read-only) | **Current** | N/A (MVP) | `--permission-mode plan`, tools limited to Read/Glob/Grep/WebSearch/WebFetch |
| 9 | AgentDesk `model-bindings/v1` | **Current** | N/A (existing) | Per-binding: `provider`, `model_id`, `tier`, `deliberation_tier`, `capabilities`, `enabled` |
| 10 | AgentDesk PM lease | **Current** | N/A (existing) | `agentdesk.pm-lease/v1` in `.agentdesk/runtime/` |
| 11 | AgentDesk event / outbox | **Current** | N/A (existing) | `agentdesk.state-event/v2`, `agentdesk.outbox-message/v2` |
| 12 | AgentDesk double-commit protocol | **Current** | N/A (existing) | `implementation_commit` → `report_commit` |
| 13 | AgentDesk WorkerSlotLease | **Target** | TC-13.7 | `agentdesk.worker-slot-lease/v1` |
| 14 | AgentDesk WorkflowOrchestrator | **Target** | TC-13.9 | Central scheduler integrating all services |
| 15 | AgentDesk MAD Gateway | **Target** | TC-13.1 | Config-driven subprocess invocation of `mad` |
| 16 | AgentDesk ControlPlaneTransitionService | **Target** | TC-13.2 | CAS-write tasks, immutable events, replayable outbox |
| 17 | AgentDesk EscalationService | **Target** | TC-13.3 | Difficulty escalation independent of rate-limit |
| 18 | AgentDesk RateLimit service | **Target** | TC-13.4 | Provider rate-limit handling independent of escalation |
| 19 | AgentDesk TASK_APPROVAL structured scope | **Target** | TC-13.8 | Scoped approvals for dispatch / accept / integrate |
| 20 | AgentDesk HTML Dashboard | **Target** | TC-13.11 | Read-only dashboard via StateProvider |
| 21 | AgentDesk four-tier Worker (Basic/Standard/Advanced/Expert) | **Target** | TC-13.6 | Independent configurable Worker slots |

---

## 1. Current — What Exists Today

### 1.1 MAD Current Capabilities

- **`mad agents`** prints a TSV table to stdout: `id\tname\tadapter\tenabled`.
  There is no `--format json` flag, no structured output.
- **`mad deliberate --format json`** prints the result of `RunResult.to_dict()`
  to stdout.  The JSON object contains `deliberation_id`, `status`, `report`,
  `archive_path`, `warnings`, `participants`, `convergence`, and `plan`.
  There is **no `schema_version`** field.
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

---

## 2. Target — Integration Design

### 2.1 MAD Target Interfaces

| Interface | Schema | Description |
|-----------|--------|-------------|
| `mad agents --format json` | `mad.agents/v1` | JSON array of agent profiles to stdout |
| `mad deliberate --format json` | `mad.run-result/v1` | Formalised `RunResult` with `schema_version` |
| `mad audit <question> --workspace …` | `mad.audit-result/v1` | Structured audit of a delivery workspace |

**Audit CLI (Target)**:

```text
mad audit <question> \
  --workspace <path>              # AgentDesk-created worktree at report_commit
  --task-card-commit <sha>        # Frozen task card SHA
  --task-card-path <relpath>      # Relative path to task card in worktree
  --delivery-report-path <relpath># Relative path to delivery report in worktree
  --report-commit <sha>           # Worker's report_commit
  --base-commit <sha>             # Task card base_commit
  --implementation-commit <sha>   # Worker's implementation_commit
  --agents <id1,id2,...>          # Comma-separated participant IDs (single CSV)
  --report-agent <id>             # Report-writing agent ID
  --depth fast|balanced|deep      # Deliberation depth (default: deep)
  --convergence auto|always|never # Dispute convergence strategy (default: auto)
  --confirm-plan                  # Skip plan confirmation (required for auto)
  --format json|markdown          # Output format (default: json for Gateway)
```

**Exit codes (Target)**:

| Exit | Meaning |
|------|---------|
| `0` | Audit completed normally (`verdict` may be `pass`, `fail`, or `blocked`) |
| `1` | Parse failure, model invocation failure, or evidence verification failure |
| `2` | Parameter, configuration, or workspace validation failure (caller error) |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

A **GatewayTimeoutError** (AgentDesk-side) is distinct from MAD exit codes:
it means the subprocess did not produce a result within `timeout_seconds`.

**Audit verdict** is a mutually-exclusive enum: `pass | fail | blocked`.
- `pass` — no issues found, or only informational issues.
- `fail` — one or more issues of severity `critical`, `high`, or `medium` found.
- `blocked` — audit could not reach a conclusion due to missing evidence.

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

**Gateway configuration** (`.agentdesk/runtime/gateway.yaml`, Target):

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
three call types (`agents`, `deliberate`, `audit`).

**Data the Gateway captures from MAD stdout**:

| Field | Source | Storage |
|-------|--------|---------|
| Raw stdout | Subprocess capture | Not persisted (transient) |
| SHA-256 of stdout | Computed by Gateway | `dispatch_receipt` or audit event |
| `deliberation_id` | Parsed from JSON | Audit event `evidence_refs` |
| `archive_path` | Parsed from JSON | `.agentdesk/runtime/` (runtime-only, never in Git) |
| `verdict` / `issues` / `evidence` | Parsed from JSON | Audit event |

### 2.3 Worker Tiers (Target)

| Tier | Context Budget (% of model window) | Max Active | Escalation Behaviour |
|------|-----------------------------------|------------|---------------------|
| Basic | 15% | 2 global / 1 per worktree | Retry once same-tier → escalate to Standard |
| Standard | 30% | 2 global / 1 per worktree | Retry once same-tier → escalate to Advanced |
| Advanced | 50% | 2 global / 1 per worktree | First failure → escalate to Expert |
| Expert | 65% | 2 global / 1 per worktree | Failure → request user decision |

At least **35% of context window** is reserved for system prompt, tool
definitions, and overhead.  The per-worktree writer limit of 1 means no two
Workers may write to the same ordinary worktree concurrently.

### 2.4 Worker Slot Lease (Target)

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

### 2.5 Approval, Escalation, Event, and Outbox Separation

- **TASK_APPROVAL** uses structured scope (dispatch / accept / integrate), not
  free-text.  Each approval is for exactly one action and one delivery.
  Revocation is an independent, immutable event.
- **Escalation** (difficulty tier change) is separate from **RateLimit**
  (provider 429 handling).  A rate-limit event must not change difficulty or
  generate `TASK_ESCALATED`.
- **Event** records what happened (audit trail).  **Outbox** records what
  should be sent (replayable intent).  They use separate ID namespaces and
  must not be conflated.

### 2.6 Skill and Dashboard Data Sharing

The Skill (PM/Worker runbooks) and the HTML Dashboard both consume data
through a **read-only StateProvider**.  Neither writes to canonical state
directly — all writes go through `ControlPlaneTransitionService`.

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
| `agentdesk.*` | AgentDesk | `agentdesk.tasks/v2`, `agentdesk.state-event/v2`, `agentdesk.worker-slot-lease/v1` |

No schema version from one namespace may be re-declared in the other.
Cross-references (e.g. an AgentDesk event referencing a `deliberation_id`)
use opaque foreign keys, not embedded schema objects.

---

## 5. Future Task Cards

| Task Card | Description | Depends on |
|-----------|-------------|------------|
| TC-13.1 | MAD `audit` sub-command + `agents --format json` | This ADR |
| TC-13.2 | ControlPlaneTransitionService | TC-13.1 |
| TC-13.3 | EscalationService | TC-13.2 |
| TC-13.4 | RateLimit service | TC-13.2 |
| TC-13.5 | StateProvider (read-only) | TC-13.2 |
| TC-13.6 | WorkerAdapter + four-tier Worker slots | TC-12.3.1 |
| TC-13.7 | WorkerSlotLease implementation | TC-13.6 |
| TC-13.8 | TASK_APPROVAL structured scope | TC-13.2 |
| TC-13.9 | WorkflowOrchestrator (full integration) | TC-13.3, 13.4, 13.5, 13.6, 13.7, 13.8 |
| TC-13.10 | E2E / Recovery tests | TC-13.9 |
| TC-13.11 | HTML Dashboard | TC-13.5, TC-13.10 |
| TC-13.12 | ADR status update (Target → Current) | TC-13.10 |

---

## 6. Non-Goals (explicit exclusions)

- MAD will **not** be vendored, forked, or embedded inside AgentDesk.
- AgentDesk will **not** implement its own multi-agent deliberation engine.
- The Gateway will **not** parse MAD internal state files (`state.json`,
  `plan.json`); it only consumes stdout JSON.
- This ADR does **not** change the MAD TypeScript migration roadmap
  (ADR-0012, ADR-0013, ADR-0014).
- The HTML Dashboard is read-only; it will **not** drive state transitions.
