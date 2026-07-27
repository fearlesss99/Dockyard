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
| 14 | AgentDesk WorkerAdapter — four-tier Worker execution orchestration | **Current** | TC-13.9b | Basic/Standard/Advanced/Expert; WorkerKind + TaskDifficulty as independent inputs; provider/model from bindings only; budget computed, not enforced; concurrency slots deferred to TC-13.10 |
| 15 | AgentDesk WorkerSlotLease | **Current** | TC-13.10c | `agentdesk.worker-slot-lease/v1`; frozen contract §2.5; implemented by TC-13.10a (frozen contract), TC-13.10b (data model, store, atomic I/O), TC-13.10c (acquire/release/renew/hold fence) |
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
| 29 | AgentDesk DispatcherAgentGateway | **Current** | TC-13.7 | Frozen contract (§2.10); execution-only single-shot agent CLI boundary; depends on TC-13.4, TC-13.6 |
| 30 | Claude Code CLI contract | **Current** | TC-13.8 | Public CLI interface contract for `claude` invocation; depends on TC-13.4 |
| 31 | AgentDesk Codex CLI Provider | **Current** | TC-13.8.4 | Frozen contract §2.12 established by TC-13.8.3; production module implemented by TC-13.8.4 |
| 32 | AgentDesk WorkerAdapter Core — Frozen Contract | **Current** | TC-13.9b | §2.13; run_worker(request, worker_kind, task_difficulty, providers) → WorkerResult; budget informational only; output remains opaque bytes; no retry/slot/lease/state writes |

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

### 2.4 Worker Tiers (Target — TC-13.9a/9b)

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
`ContextBudgetPolicy` (TC-13.5.1 — Current).  WorkerAdapter (TC-13.9a/9b)
consumes the policy's `BudgetResult`; this section describes the
*Worker-tier behaviours* that use that budget, not the arithmetic itself.

### 2.5 Worker Slot Lease (Current — TC-13.10c)

> **Frozen Contract — TC-13.10a.**  Subsections §2.5.1–§2.5.16 below are
> the frozen contract for ``agentdesk.worker-slot-lease/v1``.  The
> production implementation is now complete: TC-13.10b (data model,
> validation, runtime store, atomic I/O) and TC-13.10c (acquire / release
> / renew / hold fence, stale‑lease cleanup, capacity allocation,
> workspace normalisation, time monotonicity fencing).  TC-13.10a/b/c are
> all **Current** as of this commit.

Each Worker slot lease is independent of the PM lease.  A **provider
request permit** is independent of the Worker lifecycle slot: rate-limiting
a provider (429) does not release the Worker slot, and releasing a Worker
slot does not reset the provider rate-limit window.

---
#### 2.5.1 Stable Slot Identifiers

Eight stable slots are frozen — two per ``WorkerKind``:

```text
basic_agent-1     basic_agent-2
standard_agent-1  standard_agent-2
advanced_agent-1  advanced_agent-2
expert_agent-1    expert_agent-2
```

**Frozen rules:**

1. Slot IDs are permanent — they are never created or destroyed at runtime.
2. The same slot can be acquired, released, and re-acquired indefinitely.
3. On the first acquire of a given slot, ``lease_epoch`` is set to 1.
4. Each subsequent acquire of the same slot increments ``lease_epoch`` by 1.
5. ``renew`` does **not** increment the epoch.
6. ``release`` does **not** delete the epoch from ``slot_epochs`` — the
   current epoch value persists in the store so that the next acquire can
   pick the correct successor.
7. When selecting a free slot, the lowest-numbered available stable slot
   for the requested ``WorkerKind`` is chosen — this guarantees
   deterministic allocation.
8. Random / dynamic slot IDs are forbidden.
9. Parsing arbitrary free-form slot IDs is forbidden.
10. The capacity (2 per ``WorkerKind``) is hard-coded and cannot be
    overridden by configuration.

---

#### 2.5.2 Runtime Store

Path: ``.agentdesk/runtime/worker-slot-lease.yaml`` (gitignored, runtime-only).

JSON-compatible YAML.  Root object has exactly four keys:

```json
{
  "schema_version": "agentdesk.worker-slot-lease/v1",
  "updated_at": "2026-07-27T00:00:00Z",
  "slot_epochs": {
    "basic_agent-1": 0,
    "basic_agent-2": 0,
    "standard_agent-1": 0,
    "standard_agent-2": 0,
    "advanced_agent-1": 0,
    "advanced_agent-2": 0,
    "expert_agent-1": 0,
    "expert_agent-2": 0
  },
  "leases": {}
}
```

**Frozen rules:**

* ``slot_epochs`` contains exactly the eight stable slot IDs.
* Initial epoch for every slot is ``0``.
* Epoch is a non-bool integer ``>= 0``.
* ``release`` preserves the current epoch in ``slot_epochs``; the value
  is never reset to 0.
* ``leases`` contains only currently-held slots (empty when no Workers are
  active).
* Extra or missing root keys → fail-closed.
* Extra or missing slot epoch entries → fail-closed.
* When the file does not exist, read logic returns a canonical empty
  document; the file is created on first write.
* The canonical empty document uses a sentinel ``updated_at`` of
  ``1970-01-01T00:00:00Z`` — this value must **not** depend on the
  current system clock.  It is a deterministic marker meaning "no real
  write has occurred yet."

---

#### 2.5.3 Lease Fields — Exact Ten

``worker_kind`` is added to the ADR field set as a frozen field so that
capacity classification is self-contained within the lease store.

```python
@dataclass(frozen=True, slots=True)
class WorkerSlotLease:
    lease_id: str
    lease_epoch: int
    slot_id: str
    worker_kind: WorkerKind
    holder_dispatch_id: str
    holder_instance_id: str
    canonical_worktree: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str
```

**Exactly ten fields — no more, no less:**

| # | Field | Type | Rule |
|---|-------|------|------|
| 1 | ``lease_id`` | ``str`` | Unique per acquisition; non-empty |
| 2 | ``lease_epoch`` | ``int`` | Non-bool, ``>= 1``; incremented on each re-acquire of the same slot |
| 3 | ``slot_id`` | ``str`` | One of the eight stable slot IDs |
| 4 | ``worker_kind`` | ``WorkerKind`` | Must match the tier of ``slot_id`` |
| 5 | ``holder_dispatch_id`` | ``str`` | Which dispatch holds this slot; non-empty |
| 6 | ``holder_instance_id`` | ``str`` | Which runtime instance holds this slot; non-empty |
| 7 | ``canonical_worktree`` | ``str`` | Normalised real path (see §2.5.13); non-empty |
| 8 | ``acquired_at`` | ``str`` | RFC 3339 UTC |
| 9 | ``heartbeat_at`` | ``str`` | RFC 3339 UTC; updated on every renew |
| 10 | ``expires_at`` | ``str`` | RFC 3339 UTC; ``acquired_at + LEASE_TTL_SECONDS`` |

**Fields permanently forbidden from ``WorkerSlotLease``:**

```text
task_id        — derivable from holder_dispatch_id via outbox
revision       — derivable from holder_dispatch_id via outbox
attempt        — derivable from holder_dispatch_id via outbox
provider       — belongs to ModelSelectionSnapshot
model_id       — belongs to ModelSelectionSnapshot
task_difficulty — independent of slot allocation
prompt         — never stored in lease
PID            — runtime-only, not comparable across restarts
process handle — non-serialisable
retry count    — belongs to escalation layer
```

---

#### 2.5.4 Lease ID

Format:

```text
WSL-<32 lowercase hex>
```

**Frozen rules:**

* Generated fresh on every ``acquire``.
* Unique across the entire file.
* Not derived from ``dispatch_id``, ``task_id``, or any other business
  identifier.
* Must not embed paths, ``WorkerKind``, or holder information.
* 32 hex characters (128 bits) — not 8 or 16.

---

#### 2.5.5 TTL and Timestamps

Frozen constants:

```text
LEASE_TTL_SECONDS             = 60
MAX_HEARTBEAT_INTERVAL_SECONDS = 20
```

**Frozen rules:**

* All public API functions accept an explicit timezone-aware UTC
  ``datetime``.
* Naive ``datetime`` → rejected (``TypeError`` or ``ValueError``).
* Non-UTC offset → rejected.
* On acquire: ``acquired_at == heartbeat_at == now``; ``expires_at == now
  + LEASE_TTL_SECONDS``.
* On renew: only ``heartbeat_at`` and ``expires_at`` are updated;
  ``lease_id``, ``lease_epoch``, ``slot_id``, ``acquired_at``, and holder
  fields are unchanged.
* ``now >= expires_at`` → stale (expired).
* Monotonic time is **not** written to the file — the file always stores
  UTC wall-clock timestamps.
* Callers cannot override the TTL.
* Heartbeat scheduling (the 20 s cadence) belongs to TC-13.18
  (``WorkflowOrchestrator``), not to this module.

---

#### 2.5.6 Acquire Semantics

The entire operation executes inside one exclusive lock:

```text
lock
read existing store (or canonical empty document)
validate schema
remove expired active leases (all WorkerKinds)
select the lowest-numbered free stable slot for the requested WorkerKind
  → if none free: WorkerSlotCapacityError
increment slot_epochs[slot_id]
create lease entry with the new epoch
write store atomically
unlock
```

**Frozen rules:**

1. Stale-lease cleanup runs **before** capacity counting — an expired
   holder must not block a new acquire.
2. Only one active lease per ``(WorkerKind, canonical_worktree)`` pair.
3. Per-worktree limits are enforced independently for each ``WorkerKind``
   — a ``basic_agent`` lease on worktree A does not block an
   ``advanced_agent`` lease on the same worktree.
4. When no free slot exists for the requested ``WorkerKind`` →
   ``WorkerSlotCapacityError``.
5. Acquire must reject a duplicate active pair with ``WorkerSlotCapacityError``
   (per-worktree) — see the full duplicate rules below.
6. Two different canonical worktrees for the same ``WorkerKind`` may occupy
   both global slots for that tier.
7. A third distinct worktree for the same ``WorkerKind`` → global capacity
   reached → ``WorkerSlotCapacityError``.
8. Different ``WorkerKind`` leases on the same canonical worktree do **not**
   block each other; each operates under its own independent per-worktree
   limit.

**Duplicate active-pair rules (frozen):**

When ``acquire`` finds a matching ``(worker_kind, canonical_worktree)``
pair that is **not** expired after stale cleanup, it must:

* reject the acquire with ``WorkerSlotCapacityError``;
* **not** return the existing lease;
* **not** create a new lease;
* **not** occupy a second slot;
* **not** increment any epoch in ``slot_epochs``;
* **not** update ``updated_at``;
* **not** write the store file — the original file bytes are unchanged.

When the matching pair **was** present but expired before stale cleanup:

* the stale lease is removed during cleanup;
* the acquire proceeds normally (lowest-numbered free stable slot for
  the ``WorkerKind``);
* if the re-used slot is the same stable ``slot_id`` as the expired
  lease, its epoch is incremented from the previous epoch value
  (retained in ``slot_epochs``).

Acquire does **not** offer idempotent replay — a second active acquire
is a definite capacity error, never a silent success.

---

#### 2.5.7 Release Semantics

The entire operation executes inside one exclusive lock:

```text
lock
read store
validate:
  slot_id exists in leases
  lease_id matches
  lease_epoch matches
  worker_kind matches
  holder_dispatch_id matches
  holder_instance_id matches
delete leases[slot_id]
preserve slot_epochs[slot_id] (do NOT reset or delete)
update updated_at
write store atomically
unlock
```

**Frozen rules:**

* All six identity fields are validated before deletion.
* Only ``leases[slot_id]`` is removed — the corresponding
  ``slot_epochs[slot_id]`` is retained.
* Repeating the same release (slot already gone) → fail-closed
  (``WorkerSlotNotHeldError``).  The module does **not** silently treat
  a missing slot as success.
* An expired lease may still be released (expiry is a separate concern
  from intentional release).

---

#### 2.5.8 Renew Semantics

The entire operation executes inside one exclusive lock:

```text
lock
read store
find the slot
validate full lease identity (all six fields)
validate lease is not expired
  → if expired: WorkerSlotFencingError
update heartbeat_at = now
update expires_at = now + LEASE_TTL_SECONDS
preserve lease_id, lease_epoch, slot_id, worker_kind,
         holder_dispatch_id, holder_instance_id, acquired_at
write store atomically
unlock
```

**Frozen rules:**

* An expired lease **cannot** be resurrected via renew — the caller must
  ``release`` and ``acquire`` again, which yields a higher epoch.
* ``lease_epoch`` is **never** changed by renew.

---

#### 2.5.9 Fencing API — Lock-Held Context

A lock-free ``validate_lease()`` function **must not** be used to
authorise state commits.  The only fencing-gate API is a context manager
that holds the worker-slot lock across the entire protected operation:

```python
@contextmanager
def hold_worker_slot_fence(
    project_root: Path,
    lease: WorkerSlotLease,
    now: datetime,
) -> Iterator[None]:
    ...
```

**Semantics:**

1. Acquire the worker-slot exclusive lock.
2. Read and validate the store.
3. Verify the lease's full identity (all six fields match the current
   store).
4. Verify the lease is not expired (``now < expires_at``).
5. Yield — the caller performs its fenced state transition inside the
   ``with`` block while the lock is held.
6. In the ``finally`` block, release the lock.
7. This function does **not** modify the lease — it does not auto-renew,
   does not extend the expiry, and does not update ``heartbeat_at``.

**Global lock ordering** (frozen):

```text
1. acquire worker-slot lease lock    (.worker-slot-lease.lock)
2. acquire control-plane / state lock
3. atomic state / event write
4. release control-plane / state lock
5. release worker-slot lease lock
```

**No component may acquire these two locks in the reverse order.**
TC-13.11 (``ControlPlaneTransitionService``) must perform its
authoritative state writes inside ``hold_worker_slot_fence``.

Diagnostic / read-only functions may exist (e.g. ``read_worker_slot_leases``,
``find_lease_by_dispatch``) but they must **never** be used to gate state
writes.

---

#### 2.5.10 File Lock

Lock path: ``.agentdesk/runtime/.worker-slot-lease.lock``.

**Frozen rules:**

* ``os.open(path, O_CREAT | O_EXCL | O_WRONLY)`` — exclusive creation.
* Contention → ``WorkerSlotContentionError`` raised immediately.
* No waiting, no sleeping, no automatic retry.
* No automatic removal of a lock based on mtime — a lock file is never
  assumed to be stale by normal API functions.
* Crash-orphaned locks can only be removed by an explicit recovery task
  (out of scope for TC-13.10).
* No ``force_unlock`` function in the public API.
* Every successful lock acquisition must have a corresponding release
  in a ``finally`` block.
* A process must **never** delete a lock file it did not create.

---

#### 2.5.11 Atomic Write

Follows the established pattern from ``mad_refs.py`` and ``render_views.py``:

* Unique temporary file in the same directory (``tempfile.mkstemp``).
* UTF-8, ``ensure_ascii=False``.
* Stable key ordering / formatting.
* Trailing newline.
* ``os.fsync`` on the file descriptor before closing.
* ``os.replace`` to atomically swap the temp file into place.
* Best-effort ``os.fsync`` on the parent directory after replacement.
* On any exception before ``os.replace``, the temporary file is cleaned
  up in a ``finally`` block.
* The original file bytes are **never** modified by a failed write — only
  ``os.replace`` mutates the target path, and the temp file is discarded
  on failure.

On Windows: directory ``fsync`` behaves differently than on POSIX and may
raise ``OSError``.  The implementation must silently accept that specific
error on Windows rather than claiming directory fsync is universally
supported.  File-level ``fsync`` (step 5) is required on all platforms.

---

#### 2.5.12 Worktree Normalisation

The public ``acquire`` input is a ``Path`` — callers pass the workspace
path directly without pre-normalisation:

```python
def acquire_worker_slot(
    ...,
    workspace: Path,
    ...,
) -> WorkerSlotLease:
    ...
```

The module internally normalises:

1. Require ``workspace.is_absolute()`` — reject relative paths.
2. Require ``workspace.exists()`` and ``workspace.is_dir()``.
3. Reject a workspace that is itself a symlink or reparse point
   (``Path.is_symlink()`` on POSIX; on Windows, ``is_symlink()`` and
   junction / mount-point detection via ``os.path.realpath()``
   comparison).
4. Resolve via ``os.path.realpath()``.
5. On Windows: apply ``os.path.normcase()`` to the resolved real path.
6. On POSIX: preserve case (``normcase`` is a no-op).
7. Store the resulting normalised absolute string as
   ``canonical_worktree``.

Calling ``str.lower()`` on a path without ``realpath`` resolution is
**forbidden** — it is not a substitute for proper normalisation and would
not resolve symlinks or reparse points.

Windows reparse-point detection must use standard-library facilities
only (``os.path``, ``pathlib``); third-party packages are prohibited.

---

#### 2.5.13 Exception Hierarchy

Frozen:

```text
WorkerSlotLeaseError
├── WorkerSlotValidationError   — schema / field violations
├── WorkerSlotCapacityError     — no free slot for the requested WorkerKind
├── WorkerSlotContentionError   — lock already held
├── WorkerSlotNotHeldError      — release / renew of unknown slot
└── WorkerSlotFencingError      — epoch mismatch / expired lease
```

No ``WorkerSlotConfigError`` — TTL and capacity are hard-coded, not
configured.

Exception messages must **not** contain:

* The task prompt
* stdout / stderr content
* Secrets
* The full process environment
* The full value of ``holder_instance_id``
* The full value of ``canonical_worktree``

Exception messages may contain safe identifiers: ``slot_id``,
``lease_id``, field names, and the exception class name.

---

#### 2.5.14 Module Boundaries

TC-13.10 must **not**:

* Call ``run_worker`` or import ``worker_adapter``.
* Call ``run_dispatch`` or import ``dispatcher_gateway``.
* Launch subprocesses.
* Write to ``tasks.yaml``, events, outbox, or delivery reports.
* Execute Git commands or create / remove worktrees.
* Implement retry, escalation, or rate-limit logic.
* Read or write the PM lease file.
* Cancel running processes whose lease has expired.

The ``WorkflowOrchestrator`` (TC-13.18) is responsible for the full
lifecycle:

```text
acquire → heartbeat / renew → run_worker → fenced state transition → release
```

---

#### 2.5.15 Task-Card Split

* **TC-13.10a** — this frozen contract section (§2.5).
* **TC-13.10b** — data model (``WorkerSlotLease`` dataclass),
  validation, runtime store read / write, atomic I/O, file lock.
  Depends on TC-13.10a.
* **TC-13.10c** — ``acquire_worker_slot``, ``release_worker_slot``,
  ``renew_worker_slot``, ``hold_worker_slot_fence``, stale cleanup,
  fencing validation (lock-held).  Depends on TC-13.10b.

Dependencies:

```text
TC-13.10b → TC-13.10a
TC-13.10c → TC-13.10b
TC-13.11  → TC-13.10c
TC-13.18  → TC-13.10c + TC-13.11 + …
```

TC-13.10a, TC-13.10b, and TC-13.10c are all **Current** as of this commit.
The overall WorkerSlotLease interface is fully implemented.

---

#### 2.5.16 Status

* ADR Interface Status row #15 is now **Current** — TC-13.10c.
* The Notes column references ``agentdesk.worker-slot-lease/v1``; frozen
  contract in §2.5 (TC-13.10a).
* **TC-13.10a** — frozen contract section (§2.5) — committed and stable.
* **TC-13.10b** — data model (``WorkerSlotLease``), validation, runtime
  store, read-only load, file lock, atomic write — committed
  (``worker_slot_lease.py`` + ``test_worker_slot_lease.py``).
* **TC-13.10c** — acquire / release / renew / hold fence, stale‑lease
  cleanup, capacity allocation, workspace normalisation, time monotonicity
  fencing — committed (same module + tests).
* TC-13.10a, TC-13.10b, and TC-13.10c are all **Current**.
* TC-13.9c remains **Target** — not blocked by this contract.
* TC-13.11 and all subsequent interfaces remain **Target**.

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
  provider to use, which model tier to bind, or which WorkerKind to
  allocate.  These remain the responsibility of the selector
  (`select_model.py`) and WorkerAdapter (TC-13.9a/9b).
- `TaskDifficulty` **is** the primary input to `ContextBudgetPolicy`
  (TC-13.5.1).  Each difficulty value selects a frozen percentage (20 /
  35 / 50 / 65) and hard cap (64 000 / 128 000 / 256 000 / 512 000).
  This relationship is an arithmetic contract of `compute_budget`, not a
  property of the enum itself.
- `TaskDifficulty` values must not be treated as model-tier labels even
  though the four strings coincide today — budget percentages are not
  model tiers.
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
  (TC-13.9a/9b) and WorkerSlotLease (TC-13.10).
- `WorkerKind` does **not** encode concurrency limits (2 global / 1 per
  worktree), budget percentages, or escalation behaviour — those
  belong to ContextBudgetPolicy (TC-13.5.1) and WorkerAdapter (TC-13.9a/9b).
- A `WorkerKind` value must not be used as a model tier, and a model
  tier must not be used as a `WorkerKind`.
- `WorkerKind` and `TaskDifficulty` are **independent** inputs.
  There is no fixed one-to-one mapping between them — an Advanced task
  may be executed by an Expert Worker after escalation, or a Basic task
  may be routed to a Standard Worker during capacity overflow.
  Consumers must not derive a `TaskDifficulty` from a `WorkerKind` via
  string manipulation, enum-value casting, or a mapping table.

---

#### 2.8.4 Non-Goals of TC-13.4

TC-13.4 explicitly does **not** include:

- Budget calculation, context-window arithmetic, or token budgeting
  (→ TC-13.5.1)
- Model selection, provider binding, or `select_model.py` logic
- Any subprocess invocation, CLI call, or filesystem write
- Worker scheduling, slot allocation, lease acquisition, or
  concurrency fencing (→ TC-13.9a/9b, TC-13.10)
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

**Consumption by WorkerAdapter (TC-13.9a/9b)**:  WorkerAdapter calls
`compute_budget()` as a pure function and uses `budget_tokens` to
constrain the Worker's effective context window.  WorkerAdapter is
responsible for sourcing `context_window_tokens` from the selected
model binding's `selected_context_window_tokens` field.  WorkerAdapter
is a Target (TC-13.9a/9b) — ContextBudgetPolicy is Current and available
today.

---

### 2.10 DispatcherAgentGateway — Frozen Contract (Current — TC-13.7)

TC-13.7 defines an **execution-only** boundary for a single agent CLI
subprocess dispatch.  This section preserves the Frozen Contract — the
frozen TC-13.7 public interfaces.  TC-13.7 is now **Current**: a
matching production module (`dispatcher_gateway.py`) and test suite
exist, and the DispatcherAgentGateway interface status is Current.

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
    to TC-13.8, TC-13.9a/9b, TC-13.10, TC-13.11, and TC-13.18.

---

#### 2.10.1 Exact Responsibility Boundary

TC-13.7 is a **single‑shot**, **config‑driven**, **provider‑agnostic**
agent CLI execution gateway.  Every invocation:

* strictly validates a frozen, immutable dispatch request and its
  ten‑field `ModelSelectionSnapshot`;
* resolves the provider adapter from the caller‑supplied
  ``providers`` mapping keyed by
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
| 2 | ``required_model_capabilities`` | ``tuple[str, ...]`` |
| 3 | ``model_binding_id`` | ``str`` |
| 4 | ``selected_model_provider`` | ``str`` |
| 5 | ``selected_model_id`` | ``str`` |
| 6 | ``selected_model_tier`` | ``str`` |
| 7 | ``selected_deliberation_tier`` | ``str`` |
| 8 | ``selected_context_window_tokens`` | ``int`` (non‑bool, ≥ 1) |
| 9 | ``selected_model_capabilities`` | ``tuple[str, ...]`` |
| 10 | ``model_degradation_approval_id`` | ``str`` or ``null`` |

Rules:

* ``provider``, ``model_id``, and ``deliberation_tier`` for the
  subprocess call are taken **only** from this snapshot — callers must
  not supply an independent override.
* ``model_binding_id`` is the **only** binding identifier.  There is
  no separate ``provider_config_id`` — the snapshot's
  ``selected_model_provider`` is the adapter lookup key.
* ``selected_model_tier`` is stored but not consumed by TC-13.7;
  it is a passthrough audit field.
* ``selected_context_window_tokens`` is stored but not consumed by
  TC-13.7; budget arithmetic belongs to ContextBudgetPolicy
  (TC-13.5.1) and WorkerAdapter (TC-13.9a/9b).
* ``required_model_capabilities`` and ``selected_model_capabilities``
  are **deeply immutable** ``tuple[str, ...]``:
  * The external selector JSON produces arrays; the snapshot
    constructor must copy each array into a tuple.
  * Every element must be a non‑empty string.
  * Original JSON array order is preserved.
  * Duplicate capabilities are rejected at construction time.
  * Post‑construction mutations of the source ``list`` must not
    affect the snapshot.
  * The snapshot must not contain any mutable ``list``, ``dict``,
    or ``set`` — every collection field is an immutable sequence
    or mapping.  (``required_model_capabilities`` and
    ``selected_model_capabilities`` are the only collection fields
    in the ten‑field set; both are ``tuple[str, ...]``.)

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
* ``workspace`` is the **authoritative subprocess working directory**.
  The Gateway must pass it directly as ``cwd`` to
  ``asyncio.create_subprocess_exec`` on every invocation — on both
  Windows and POSIX.  The adapter has no say in the working directory;
  ``AgentCliInvocation`` carries no ``cwd`` field.
* ``prompt`` is the **only** source of task content — the adapter
  derives ``stdin`` from it.
* ``timeout_seconds`` is a positive integer (non‑bool).
* The entire request is frozen before Gateway invocation; the Gateway
  does not enrich, derive, or persist any additional fields.

---

#### 2.10.5 Provider Adapter Protocol

TC-13.7 defines ``AgentCliProvider`` as a ``typing.Protocol``
(runtime‑checkable).  TC-13.7 itself only ships the Protocol;
it does **not** bundle any real provider implementation.

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

**Provider lookup — call‑level explicit mapping**:

TC-13.7 does **not** provide a module‑level mutable registry.  The
public entry point receives provider instances explicitly:

```python
async def run_dispatch(
    request: DispatchRequest,
    providers: Mapping[str, AgentCliProvider],
) -> DispatchResult:
    ...
```

Rules:

* ``providers`` is a **call‑level explicit dependency** — the Gateway
  stores no global state.
* The lookup key is exactly ``request.model_selection.selected_model_provider``.
* When the key is not present, ``ProviderNotSupportedError`` is raised
  **before** any subprocess is launched.
* Every adapter's ``provider_id`` must equal its key in the mapping.
* Both the provider key and ``provider_id`` must be non‑empty strings.
* ``providers`` must not be empty — at least one provider must be
  supplied.
* The Gateway does **not** mutate the mapping.
* There is **no** ``register_provider()``, ``unregister_provider()``,
  or clear‑registry function.
* TC-13.8 / TC-13.9a callers construct the mapping before invoking
  ``run_dispatch``.
* Tests construct a local ``{"fake": FakeAgentCliProvider()}``
  mapping per call — ``FakeAgentCliProvider`` remains only in the
  test directory.

---

#### 2.10.6 AgentCliInvocation — Frozen Invocation Value

Returned by ``AgentCliProvider.build_invocation()``.  Represents a
fully‑resolved, safe subprocess invocation.

| # | Field | Type | Rule |
|---|-------|------|------|
| 1 | ``executable`` | ``str`` | Absolute path or plain name (resolved via ``shutil.which``) |
| 2 | ``argv`` | ``tuple[str, ...]`` | Arguments only — does **not** include executable; each element is exactly one argument; empty tuple is legal |
| 3 | ``stdin`` | ``bytes`` or ``None`` | Derived from ``DispatchRequest.prompt``; ``None`` → ``DEVNULL`` |
| 4 | ``env_overrides`` | ``tuple[tuple[str, str], ...]`` | Pairs of ``(KEY, value)``; keys are unique |

The Gateway executes the invocation as:

```python
resolved_executable = resolve(invocation.executable)
await asyncio.create_subprocess_exec(
    resolved_executable,
    *invocation.argv,
    cwd=str(request.workspace),   # authoritative — never from the adapter
    ...
)
```

Rules:

* ``argv`` must **not** contain the executable — ``executable`` is a
  separate field and is passed as the first argument to
  ``create_subprocess_exec``.  The adapter must not duplicate it.
* Empty ``argv`` is legal (the subprocess receives zero arguments).
* ``executable`` is a **single** path or command name — it must not
  embed arguments, flags, or shell metacharacters.  Spaces in the
  path are permitted (the path is passed as a single ``exec*``
  argument).
* ``shell=True`` is **never** used — the Gateway exclusively uses
  ``asyncio.create_subprocess_exec``.
* ``create_subprocess_shell`` is **never** used.
* ``stdin`` is produced by the adapter from the sole ``prompt``.
  When ``None`` the Gateway passes ``DEVNULL``.
* ``env_overrides`` keys are unique.  The Gateway copies the full
  parent environment and then applies each ``(KEY, value)`` pair.
* The adapter **does not** receive the full parent environment —
  it only declares the overrides it needs.
* The Gateway **must not** log, persist, or return the content of
  environment variables in any result, exception, event, or report.
  API keys must never appear in Gateway output.
* **No ``cwd`` field.**  The working directory is the sole
  responsibility of the Gateway and is taken from
  ``DispatchRequest.workspace`` (§2.10.4).  ``AgentCliInvocation``
  carries exactly four fields — the adapter has no ability to
  influence, override, or suggest the subprocess working directory.

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
  responsibility (TC-13.9a/9b / TC-13.11).

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
Provider‑specific decoding belongs to TC-13.8 / TC-13.9c.

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
  (TC-13.9a/9b / TC-13.11).

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
| **TC-13.9a/9b** | Consumer — WorkerAdapter calls the Gateway, maps ``WorkerKind`` + ``TaskDifficulty`` as independent inputs, applies budget, and returns ``WorkerResult``; single-attempt, no retry/slot/lease |
| **TC-13.10** | Separate — slot lease and fencing are independent of a single subprocess execution |
| **TC-13.11** | Consumer — ControlPlaneTransitionService invokes the Gateway and writes canonical state / events / outbox from the result |
| **TC-13.18** | Consumer — WorkflowOrchestrator coordinates Gateway calls |

---

#### 2.10.13 Status

This section (§2.10) is now an implemented contract.

* ADR Interface Status row #29 “AgentDesk DispatcherAgentGateway”
  is **Current**.
* TC-13.8 and all subsequent Target interfaces remain **Target**.
* TC-13.7 is marked **Current** — ``dispatcher_gateway.py`` and matching
  tests are committed.

### 2.11 Claude Code CLI Provider — Frozen Contract (Current — TC-13.8)

TC-13.8 defines the **Claude Code CLI Provider** — a concrete
`AgentCliProvider` adapter for the `claude` CLI.  This section is the
Frozen Contract for TC-13.8 public interfaces.  TC-13.8 is **Current**:
the production provider module (`claude_code_provider.py`) and matching
test suite (`test_claude_code_provider.py`) exist and are committed.

---

#### 2.11.1 Relationship to TC-13.7

Claude Code Provider is a concrete implementation of the TC-13.7
`AgentCliProvider` Protocol.  It translates a `DispatchRequest` into
an `AgentCliInvocation`:

```
DispatchRequest
    ↓
AgentCliInvocation(
    executable,
    argv,
    stdin,
    env_overrides,
)
```

Claude Code Provider is responsible **only** for constructing this
invocation value.  It does **not**:

* launch subprocesses — TC-13.7 `DispatcherAgentGateway` owns this;
* set `cwd` — `request.workspace` is passed as `cwd=str(request.workspace)`
  by the Gateway, never by the provider;
* implement timeout or cancellation — Gateway responsibility;
* terminate process trees — Gateway responsibility;
* compute stdout/stderr SHA-256 — Gateway responsibility;
* parse `DispatchResult.stdout` — stdout is opaque bytes (TC-13.7);
* write files or persist state — Gateway is a pure execution boundary;
* implement retry, lease, slot, or escalation — deferred to TC-13.9a/9b / TC-13.10 / TC-13.11.

The provider must **not** have a `cwd` field, must not call `os.chdir()`,
and must not use `--add-dir` to simulate the primary workspace directory.

---

#### 2.11.2 Provider ID — Dual-Instance Design

A single provider implementation may be registered under two distinct
identifiers:

```python
ClaudeCodeProvider(provider_id="claude", ...)
ClaudeCodeProvider(provider_id="claudecode", ...)
```

**Frozen rules:**

1. `provider_id` must be exactly `"claude"` or `"claudecode"` — no
   other values are permitted.
2. Each instance's `provider_id` **must** equal its key in the providers
   `Mapping[str, AgentCliProvider]` passed to `run_dispatch`.
3. `"claudecode"` must **not** be alias-normalized to `"claude"`.
4. `ModelSelectionSnapshot.selected_model_provider` must **not** be
   modified by the provider.
5. There is **no** module-level mutable provider registry — no
   `register_provider()`, no `unregister_provider()`.

**Explicitly prohibited:**

```python
if selected_provider == "claudecode":
    selected_provider = "claude"
```

The TC-13.7 mapping-key/provider-id consistency rule is preserved
unchanged.

---

#### 2.11.3 Prompt Transmission — Stdin-Only

`DispatchRequest.prompt` is the sole source of task content.

The complete task prompt must be transmitted **exclusively via stdin**:

```python
stdin = request.prompt.encode("utf-8")
```

**Frozen rules:**

* UTF-8 strict encoding — no `errors="ignore"` or `errors="replace"`.
* No BOM (byte-order mark).
* The prompt must not be modified, trimmed, normalized, or concatenated
  with any other content.
* `request.prompt` must **not** appear in `argv`.
* The prompt must **not** be placed in any environment variable.
* The prompt must **not** be written to a temporary file.

**Control prompt:** `argv` may contain **one** fixed, non-sensitive
control string:

```text
Read the task instructions from stdin and execute them.
```

This string must be a compile-time constant in source code.  It must
**not** contain task content, paths, dispatch IDs, or any data from
`DispatchRequest`.

| Channel | Content |
|---------|---------|
| **stdin** | Complete task prompt |
| **argv control string** | Fixed instruction to read from stdin |
| **argv** | Must NOT contain task content |
| **environment** | Must NOT contain task content |
| **temporary files** | Must NOT be used for task content |

---

#### 2.11.4 CLI Invocation Pattern

Frozen as **non-interactive, single-shot** execution:

```text
claude -p <fixed-control-prompt>
```

Required flags:

| Flag | Value / Source |
|------|---------------|
| `-p` | Fixed control prompt (compile-time constant) |
| `--output-format` | `json` |
| `--model` | `request.model_selection.selected_model_id` |
| `--permission-mode` | Configured safe mode (see §2.11.7) |
| `--effort` | Mapped effort (see §2.11.6) |
| `--no-session-persistence` | (flag, no argument) |
| `--allowedTools` | Each allowed tool as a separate argv element (see below) |
| `--disallowedTools` | Each disallowed tool as a separate argv element (see below) |

`executable` and `argv` are strictly separated:

```python
def build_invocation(
    self,
    request: DispatchRequest,
) -> AgentCliInvocation:

    mapped_effort = _EFFORT_MAP[request.model_selection.selected_deliberation_tier]

    argv = [
        "-p",
        _CONTROL_PROMPT,
        "--output-format", "json",
        "--model", request.model_selection.selected_model_id,
        "--permission-mode", self.permission_mode,
        "--effort", mapped_effort,
        "--no-session-persistence",
    ]

    if self.allowed_tools:
        argv.extend(["--allowedTools", *self.allowed_tools])

    if self.disallowed_tools:
        argv.extend(["--disallowedTools", *self.disallowed_tools])

    return AgentCliInvocation(
        executable=self.executable,
        argv=tuple(argv),
        stdin=request.prompt.encode("utf-8"),
        env_overrides=(),
    )
```

**argv ordering rules (frozen):**

1. Base flags: `-p`, `CONTROL_PROMPT`, `--output-format json`,
   `--model ...`, `--permission-mode ...`, `--effort ...`,
   `--no-session-persistence`.
2. `--allowedTools` block (if non-empty): the flag followed by each
   tool expression as a separate argv element.
3. `--disallowedTools` block (if non-empty): the flag followed by each
   tool expression as a separate argv element.
4. Allowed block always precedes disallowed block.
5. When a tuple is empty, the corresponding flag and its arguments
   are omitted entirely.
6. The flag is emitted once — it is NOT repeated per tool.
7. `executable` does NOT appear in `argv`.
8. The result is always `tuple(argv)`.

**Tool flag casing is frozen exactly:**

```text
--allowedTools
--disallowedTools
```

**Precise example** (non-empty both):

```python
allowed_tools = ("Read", "Bash(git status:*)")
disallowed_tools = ("WebFetch",)

argv == (
    "-p",
    _CONTROL_PROMPT,
    "--output-format", "json",
    "--model", selected_model_id,
    "--permission-mode", permission_mode,
    "--effort", mapped_effort,
    "--no-session-persistence",
    "--allowedTools",
    "Read",
    "Bash(git status:*)",
    "--disallowedTools",
    "WebFetch",
)
```

**Precise example** (allowed only — disallowed omitted):

```python
allowed_tools = ("Bash(curl:*)",)
disallowed_tools = ()

argv == (
    "-p",
    _CONTROL_PROMPT,
    "--output-format", "json",
    "--model", selected_model_id,
    "--permission-mode", permission_mode,
    "--effort", mapped_effort,
    "--no-session-persistence",
    "--allowedTools",
    "Bash(curl:*)",
)
```

**Non-negotiable:**

* Each tool expression is a separate `argv` element.
* Multiple tools are NOT joined by commas, spaces, or shell
  concatenation.
* The task prompt must NOT appear in tool flags.
* No shell string — each element is a separate `argv` token.
* No shell wrapper (`cmd /c`, `powershell -Command`, `bash -c`).

---

#### 2.11.5 Model Mapping

The CLI model argument is taken **exactly** from the frozen snapshot:

```text
request.model_selection.selected_model_id
```

generates:

```text
--model <selected_model_id>
```

**Must not:**

* use `required_model_tier` in place of a model ID;
* override with a hard-coded default model;
* rewrite the model ID based on `provider_id` alias;
* read a different model from an environment variable;
* silently fall back to another model.

An empty string or otherwise invalid model ID must **fail closed**
(see §2.11.12).

---

#### 2.11.6 Deliberation Tier → Claude Effort Mapping

Frozen mapping:

| AgentDesk `deliberation_tier` | Claude `--effort` |
|-------------------------------|-------------------|
| `efficient` | `low` |
| `balanced` | `medium` |
| `deep` | `high` |

Input source: `request.model_selection.selected_deliberation_tier`.

**Important caveats (must be stated in the ADR):**

* This is a **provider-specific** mapping from AgentDesk terminology
  to Claude CLI terminology.
* It does **not** imply the two tier systems are semantically identical.
* An unknown deliberation tier must **fail closed** — no silent default
  to `medium`.
* `MadDeliberationDepth.fast` is a **MAD** concept distinct from
  AgentDesk `efficient`; they are not the same enum and must not be
  treated as interchangeable.

---

#### 2.11.7 Permission Mode — Safe Set

Allowed permission modes:

```text
default
plan
acceptEdits
dontAsk
```

Explicitly **forbidden**:

```text
bypassPermissions
delegate
```

Reasons:
* `bypassPermissions` bypasses the security boundary.
* `delegate` is not part of the single-worker CLI execution model
  frozen in this task.

An unknown permission mode must **fail closed** — no silent fallback
to `default`.

No equivalent dangerous skip-permissions argument may be enabled.

---

#### 2.11.8 Tool Allow / Deny Configuration

The provider configuration carries two **deeply immutable** tuples:

```text
allowed_tools: tuple[str, ...]
disallowed_tools: tuple[str, ...]
```

**Rules:**

* At construction time, external sequences are copied into `tuple`.
* Every entry must be a non-empty string.
* Leading and trailing whitespace on any entry is forbidden — reject
  at construction.
* Duplicate entries are forbidden — reject at construction.
* Original order is preserved.
* Mutating the source list after construction does **not** affect the
  provider's stored tuples.
* Both tuples may be empty.
* When empty, the corresponding `--allowedTools` / `--disallowedTools`
  block is omitted entirely from `argv` (see §2.11.4).

When non-empty, each entry is serialized as a separate `argv` element
following the flag.  No shell string concatenation is performed.

**Intersection must be fail-closed:**

```python
if not set(allowed_tools).isdisjoint(disallowed_tools):
    raise ValueError(
        "allowed_tools and disallowed_tools must be disjoint"
    )
```

The same exact tool string must **not** appear in both tuples.  This is
validated at construction time — the provider raises `ValueError`, not
a warning.

Must **not:**

* guess deny-priority or allow-priority;
* silently remove the entry from one side;
* case-fold before comparing;
* trim before comparing.

The intersection check uses exact string equality on the validated
entries (after individual-entry validation — non-empty, no whitespace,
etc.).

---

#### 2.11.9 ClaudeCodeProvider — Frozen Public API

`ClaudeCodeProvider` **is** the configuration.  There is no separate
`ClaudeCodeProviderConfig` class.

```python
from __future__ import annotations

from dataclasses import dataclass

from dispatcher_gateway import (
    AgentCliInvocation,
    AgentCliProvider,
    DispatchRequest,
)

_CONTROL_PROMPT = (
    "Read the task instructions from stdin and execute them."
)


@dataclass(frozen=True, slots=True)
class ClaudeCodeProvider:
    """Claude Code CLI adapter — TC-13.8 frozen contract."""

    provider_id: str
    executable: str
    permission_mode: str
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        # Reject at construction — ValueError on any illegal input.
        ...

    def build_invocation(
        self,
        request: DispatchRequest,
    ) -> AgentCliInvocation:
        ...


__all__ = ["ClaudeCodeProvider"]
```

**Exactly five fields — no more, no less:**

| # | Field | Type | Constraint |
|---|-------|------|------------|
| 1 | `provider_id` | `str` | `"claude"` or `"claudecode"` only; satisfies `AgentCliProvider.provider_id` |
| 2 | `executable` | `str` | See executable rules below |
| 3 | `permission_mode` | `str` | `"default"`, `"plan"`, `"acceptEdits"`, or `"dontAsk"` |
| 4 | `allowed_tools` | `tuple[str, ...]` | Deeply immutable; may be empty |
| 5 | `disallowed_tools` | `tuple[str, ...]` | Deeply immutable; may be empty |

`_CONTROL_PROMPT` is a **private** module-level constant (not a
dataclass field, not a `ClassVar`, not in `__all__`).  ``dataclasses
.fields(ClaudeCodeProvider)`` returns **exactly five** field objects.
It is a compile-time string literal — it never contains task content,
paths, dispatch IDs, or any request data.

**Forbidden sixth field** — these must **never** appear on
`ClaudeCodeProvider`:

```text
cwd
workspace
prompt
timeout
model
model_id
effort
env
env_overrides
api_key
token
session_id
resume_id
retry
slot
lease
persistence_path
```

`env_overrides=()` is a fixed value on the generated `AgentCliInvocation`,
not a sixth provider field.

**Executable rules:**

* Must be `str`, non-empty, not pure whitespace.
* Must not contain leading or trailing whitespace.
* Must not contain NUL (`\x00`), CR (`\r`), or LF (`\n`).
* Must not contain embedded arguments — no shell-command-as-string.
* A path containing ordinary spaces (e.g.
  `C:\Program Files\Claude\claude.exe`) **is** legal and must not be
  rejected as "embedded arguments".
* The provider does **not** call `shlex.split()`, `shutil.which()`,
  `os.fspath`, or any other path-resolution function — executable
  resolution remains the Gateway's responsibility (§2.10.9).

**Construction rejection — `ValueError`:** `ClaudeCodeProvider.__init__`
and `__post_init__` raise `ValueError` (not a custom exception class,
not a warning) for:

* `provider_id` not `"claude"` or `"claudecode"`;
* `executable` empty, pure whitespace, contains NUL/CR/LF, or contains
  embedded arguments;
* `permission_mode` not in the safe set (§2.11.7);
* any tool entry not a `str`, empty, or with leading/trailing whitespace;
* duplicate tool entry;
* non-empty intersection between `allowed_tools` and
  `disallowed_tools` (see §2.11.8).

**`build_invocation` rejection — `ValueError`:** raises `ValueError`
for:

* unknown `selected_deliberation_tier` (see §2.11.6);
* any other request field that cannot be mapped per this contract.

**Gateway wrapping:** `run_dispatch()` (TC-13.7) wraps provider
exceptions into `DispatchInvocationError`.  TC-13.8 does **not**
introduce a parallel public exception hierarchy.

**Module public surface** — the production module
`skills/agentdesk/scripts/claude_code_provider.py` will export
exactly one public symbol:

```python
__all__ = ["ClaudeCodeProvider"]
```

No module-level registry, no `register_provider()`, no
`unregister_provider()`.

---

#### 2.11.10 Environment Variable & Authentication Boundary

Frozen:

```python
env_overrides == ()
```

The Claude Code Provider:

* does **not** read, set, or forward any API key.
* does **not** copy or inspect the full parent process environment.
* does **not** set any authentication environment variable.
* does **not** set `MAD_HOME`.
* does **not** set `MAD_PARTICIPANT`.
* does **not** set `CLAUDECODE` or any recursive-session guard.
* does **not** log or return secrets in any form.

Authentication is the responsibility of the installation environment
and the Claude CLI itself.

The `--bare` flag is **not** included in this frozen contract — it
may alter authentication and configuration loading behavior and
requires separate evaluation.

---

#### 2.11.11 Explicitly Forbidden CLI Behavior

The following must **never** appear in `argv`:

| Forbidden Flag / Pattern | Reason |
|--------------------------|--------|
| `--continue` | Session resumption — single-shot only |
| `--resume` | Session resumption — single-shot only |
| `--session-id` | Session persistence — single-shot only |
| `--fork-session` | Multi-session — not in scope |
| `--remote` | Remote execution — not in scope |
| `--teleport` | Remote execution — not in scope |
| `--dangerously-skip-permissions` | Security bypass |
| `--permission-mode bypassPermissions` | Security bypass |
| `--add-dir` for primary workspace | cwd handled by Gateway |
| Interactive mode (no `-p`) | Single-shot only |
| Shell wrapper (`cmd /c`, `powershell -Command`, `bash -c`) | Process integrity |
| Task prompt in argv | Stdin-only contract |
| Secrets / API keys / tokens in argv | Security boundary |

Future `--add-dir` usage for auxiliary directory access is not
within TC-13.8 scope.

---

#### 2.11.12 Fail-Closed Rules

The provider must **reject** (fail closed, no silent recovery) for:

| Condition | Exception |
|-----------|-----------|
| `provider_id` not `"claude"` or `"claudecode"` | `ValueError` at construction |
| `executable` empty, pure whitespace, or `None` | `ValueError` at construction |
| `executable` contains NUL, CR, or LF | `ValueError` at construction |
| `executable` contains embedded arguments | `ValueError` at construction |
| `permission_mode` not in safe set | `ValueError` at construction |
| Tool entry not a `str` | `ValueError` at construction |
| Tool entry empty string | `ValueError` at construction |
| Tool entry has leading/trailing whitespace | `ValueError` at construction |
| Duplicate tool entry in same tuple | `ValueError` at construction |
| allowed ∩ disallowed not disjoint | `ValueError` at construction |
| Unknown `selected_deliberation_tier` | `ValueError` in `build_invocation` |
| `provider_id` ≠ mapping key | `DispatchInputError` by Gateway (TC-13.7) |
| Prompt cannot be transmitted per UTF-8 contract | `ValueError` — no replacement or trimming |

No silent correction, trimming, fallback, or alias normalization is
permitted.

---

#### 2.11.13 Output Boundary

The provider requests JSON output:

```text
--output-format json
```

but its responsibility ends at constructing the invocation.  It must
**not**:

* parse stdout JSON;
* convert stdout to text;
* extract report data from output;
* determine business success/failure;
* modify the Gateway's opaque-bytes contract.

TC-13.7 continues to treat `DispatchResult.stdout` and
`DispatchResult.stderr` as opaque `bytes`.  Output interpretation and
Worker delivery transformation belong to **TC-13.9c**.

---

#### 2.11.14 Explicitly Out of Scope

TC-13.8 does **not** implement:

* `WorkerAdapter` (TC-13.9a/9b)
* `WorkerKind` → provider selection (TC-13.9a/9b)
* `ContextBudgetPolicy` invocation (TC-13.5.1 / TC-13.9a/9b)
* Context budget → CLI argument translation (TC-13.9a/9b)
* Dispatch scheduling (TC-13.18)
* Worker slot allocation (TC-13.10)
* Lease management (TC-13.10)
* Retry logic (TC-13.9a/9b)
* Escalation (TC-13.13)
* Rate limiting (TC-13.14)
* State / event / outbox / report writes (TC-13.11)
* Stdout business parsing (TC-13.9c)
* Claude session resumption
* Remote Claude sessions
* Real Claude CLI invocation

All of the above remain **Target** for their respective task cards.

---

#### 2.11.15 Status

* ADR Interface Status row #30 "Claude Code CLI contract"
  is **Current**.
* This section (§2.11) is the Frozen Contract for TC-13.8 — it governs
  future implementation and test work.
* TC-13.7 and all prior Current interfaces remain **Current**.
* TC-13.8 is **Current** — the production provider module
  (`claude_code_provider.py`) and complete test suite
  (`test_claude_code_provider.py`) are committed.

### 2.12 Codex CLI Provider — Frozen Contract (Current — TC-13.8.4)

TC-13.8.3 froze the **Codex CLI Provider** contract — the frozen public
interface recorded in this section.  TC-13.8.4 implemented the production
provider module (`codex_cli_provider.py`) and the complete test suite
(`test_codex_cli_provider.py`).  Both are now committed and this
interface is **Current**.

---

#### 2.12.1 Relationship to TC-13.7

Codex CLI Provider is a concrete implementation of the TC-13.7
`AgentCliProvider` Protocol.  It translates a `DispatchRequest` into
an `AgentCliInvocation`:

```
DispatchRequest
    ↓
AgentCliInvocation(
    executable,
    argv,
    stdin,
    env_overrides,
)
```

Codex CLI Provider is responsible **only** for constructing this
invocation value.  It does **not**:

* launch subprocesses — TC-13.7 `DispatcherAgentGateway` owns this;
* set `cwd` — `request.workspace` is passed as `cwd=str(request.workspace)`
  by the Gateway, never by the provider;
* implement timeout or cancellation — Gateway responsibility;
* terminate process trees — Gateway responsibility;
* compute stdout/stderr SHA-256 — Gateway responsibility;
* parse `DispatchResult.stdout` — stdout is opaque bytes (TC-13.7);
* write files or persist state — Gateway is a pure execution boundary;
* implement retry, lease, slot, or escalation — deferred to TC-13.9a/9b / TC-13.10 / TC-13.11;
* manage authentication or secrets.

The provider must **not** have a `cwd` field, must not call `os.chdir()`,
and must not use `-C`/`--cd` or `--add-dir`.

---

#### 2.12.2 Provider ID — Single Identifier

Only one provider identifier is permitted:

```text
codex
```

**Frozen rules:**

1. `provider_id` must be exactly `"codex"` — no other values are permitted.
2. The instance's `provider_id` **must** equal its key in the providers
   `Mapping[str, AgentCliProvider]` passed to `run_dispatch`.
3. `ModelSelectionSnapshot.selected_model_provider` must **not** be
   modified by the provider.
4. There is **no** module-level mutable provider registry — no
   `register_provider()`, no `unregister_provider()`.

**Explicitly prohibited provider_id values:**

```text
openai        — may represent OpenAI API provider, not Codex CLI
openai-codex  — not a CLI provider identifier
codexcli      — not a CLI provider identifier
Codex         — case variant
CODEX         — case variant
claude        — Claude Code domain
""            — empty
None          — non-str
True / 1      — non-str
```

No alias normalization is permitted.  `"openai"` is semantically
ambiguous (it could mean the OpenAI API rather than the Codex CLI) and
must **not** be accepted as a provider_id for the Codex CLI.

---

#### 2.12.3 Prompt Transmission — Stdin-Only

`DispatchRequest.prompt` is the sole source of task content.

The complete task prompt must be transmitted **exclusively via stdin**:

```python
stdin = request.prompt.encode("utf-8")
```

**Frozen rules:**

* UTF-8 strict encoding — no `errors="ignore"` or `errors="replace"`.
* No BOM (byte-order mark).
* The prompt must not be modified, trimmed, normalized, or concatenated
  with any other content.
* `request.prompt` must **not** appear in `argv`.
* The prompt must **not** be placed in any environment variable.
* The prompt must **not** be written to a temporary file.

**No fixed control prompt:** Unlike the Claude Code Provider, Codex reads
instructions directly from stdin via the `-` positional marker.  The
`codex exec --help` output confirms:

> If not provided as an argument (or if `-` is used), instructions are
> read from stdin.

No compile-time control string is required or permitted in `argv`.

| Channel | Content |
|---------|---------|
| **stdin** | Complete task prompt |
| **argv `-` marker** | Signals stdin mode |
| **argv** | Must NOT contain task content |
| **environment** | Must NOT contain task content |
| **temporary files** | Must NOT be used for task content |

---

#### 2.12.4 CLI Invocation Pattern

Frozen as **non-interactive, single-shot** execution:

```text
codex --ask-for-approval never exec --ephemeral --json --color never
      --model <model_id> --sandbox <sandbox_mode>
      -c model_reasoning_effort="<effort>" -
```

Required flags:

| Flag | Value / Source |
|------|---------------|
| `--ask-for-approval` | `never` (hard-coded, must precede `exec`) |
| `exec` | Non-interactive sub-command |
| `--ephemeral` | (flag, no argument) |
| `--json` | (flag, no argument) |
| `--color` | `never` |
| `--model` | `request.model_selection.selected_model_id` |
| `--sandbox` | Configured safe mode (see §2.12.7) |
| `-c` | `model_reasoning_effort="<mapped_effort>"` (see §2.12.6) |
| `-` | Stdin positional marker (must be last) |

`executable` and `argv` are strictly separated:

```python
def build_invocation(
    self,
    request: DispatchRequest,
) -> AgentCliInvocation:

    mapped_effort = _EFFORT_MAP[request.model_selection.selected_deliberation_tier]
    model_id = request.model_selection.selected_model_id

    # Validate model_id against strict character allowlist
    _validate_model_id(model_id)

    argv = (
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--model",
        model_id,
        "--sandbox",
        self.sandbox_mode,
        "-c",
        f'model_reasoning_effort="{mapped_effort}"',
        "-",
    )

    return AgentCliInvocation(
        executable=self.executable,
        argv=argv,
        stdin=request.prompt.encode("utf-8"),
        env_overrides=(),
    )
```

**argv ordering rules (frozen):**

1. `--ask-for-approval never` must appear **before** `exec`.
   Placing it after `exec` causes a parameter error.
2. `exec` sub-command.
3. `exec` options: `--ephemeral`, `--json`, `--color never`.
4. `--model <model_id>`, `--sandbox <sandbox_mode>`.
5. `-c model_reasoning_effort="<effort>"` — the entire key=value is one
   argv element.
6. `-` — stdin marker, must be last.
7. `executable` does NOT appear in `argv`.
8. The result is always `tuple(argv)`.

**Model ID character allowlist:** When the resolved executable is a
`.cmd`/`.bat` shim on Windows, dynamic argv values must use a strict
character allowlist to prevent command-processor character injection.
The model_id value must consist only of:

```text
ASCII letters (A-Z, a-z)
digits (0-9)
- _ . : /
```

Forbidden characters in model_id: spaces, `&`, `|`, `;`, `<`, `>`,
`` ` ``, `$`, `"`, `'`, `(`, `)`, CR, LF, NUL.

This is a fail-closed defense: any forbidden character causes
`build_invocation` to raise `ValueError`.

**Windows `.cmd` shim caveat:** The npm-installed `codex.cmd` is a
Windows batch-file shim, not a PE binary.  While this has been verified
to work with `asyncio.create_subprocess_exec` on the author's machine,
the contract does **not** claim that all Windows environments exhibit
identical shim behaviour, nor that the command processor is fully
bypassed.  The strict model_id allowlist provides an additional layer
of defence against command-processor character interpretation.

---

#### 2.12.5 Model Mapping

The CLI model argument is taken **exactly** from the frozen snapshot:

```text
request.model_selection.selected_model_id
```

generates:

```text
--model <selected_model_id>
```

**Must not:**

* use `required_model_tier` in place of a model ID;
* override with a hard-coded default model;
* rewrite the model ID based on `provider_id` alias;
* read a different model from an environment variable;
* silently fall back to another model;
* add `--oss` or `--local-provider`.

An empty string or otherwise invalid model ID must **fail closed**
(see §2.12.12).

---

#### 2.12.6 Deliberation Tier → Codex Reasoning Effort Mapping

Codex CLI has no standalone `--effort` flag.  Reasoning effort is
configured via the `model_reasoning_effort` config key passed through
`-c`:

```text
-c model_reasoning_effort="<value>"
```

The entire `key=value` string is a single argv element.

Frozen mapping:

| AgentDesk `deliberation_tier` | Codex `model_reasoning_effort` |
|-------------------------------|-------------------------------|
| `efficient` | `low` |
| `balanced` | `medium` |
| `deep` | `high` |

Input source: `request.model_selection.selected_deliberation_tier`.

**Important caveats:**

* This is a **provider-specific** mapping from AgentDesk terminology
  to Codex CLI configuration.
* It does **not** imply the two tier systems are semantically identical.
* An unknown deliberation tier must **fail closed** — no silent default
  to `medium`.
* `MadDeliberationDepth.fast` is a **MAD** concept distinct from
  AgentDesk `efficient`; they are not the same enum and must not be
  treated as interchangeable.
* `minimal` is **not** mapped — AgentDesk has no corresponding tier.
* `xhigh` is **not** mapped — AgentDesk has no corresponding tier.
* Whether a specific model supports a given reasoning effort value is
  outside the provider's scope; if the CLI or model rejects the value,
  the Gateway's non-zero-exit semantics handle it.
* No other `-c` override is permitted — the caller cannot supply
  arbitrary config values.

---

#### 2.12.7 Sandbox Mode — Safe Set

Allowed sandbox modes:

```text
read-only
workspace-write
```

Explicitly **forbidden**:

```text
danger-full-access
```

These values are the published sandbox modes from `codex --help`:

> `-s, --sandbox <SANDBOX_MODE>` — `[possible values: read-only,
> workspace-write, danger-full-access]`

Reasons:
* `danger-full-access` grants full filesystem access — violates the
  least-privilege boundary for a single Worker invocation.

An unknown sandbox mode must **fail closed** — no silent fallback
to `workspace-write`.

The provider does **not** make additional promises about OS-level
sandbox implementation details beyond what the CLI help describes.
Gateway and deployment environment may still form an outer security
boundary.

---

#### 2.12.8 Approval Policy — Fixed `never`

Approval policy is hard-coded as `never` — it is **not** a provider
field:

```text
--ask-for-approval never
```

Frozen rules:

* The value is always `"never"` — callers cannot override it.
* The flag must appear **before** `exec`.
* `"never"` means: do not prompt for interactive approval; execution
  failures are immediately returned to the model.
* `"never"` does **not** bypass the sandbox — `--sandbox` remains in
  effect.
* `"untrusted"` is **forbidden** — it may still request interactive
  approval for non-trusted commands.
* `"on-request"` is **forbidden** — the model may request interactive
  approval, which is unavailable in a non-interactive subprocess.

AgentDesk's own `ApprovalGate` (TC-13.12) remains the control-plane
component for external approval.  The Codex subprocess must never wait
for unavailable interactive approval.

---

#### 2.12.9 Session & Persistence

The `--ephemeral` flag is mandatory:

```text
--ephemeral
```

The contract promises:

> Codex session files are not persisted to disk.

It does **not** promise zero file I/O — the Codex CLI may still access
authentication storage, configuration, caches, or platform runtime data.

Explicitly **forbidden**:

```text
exec resume       — session resumption
resume            — top-level session resumption
fork              — session forking
session ID flags  — session identity
```

---

#### 2.12.10 User Config & Rules — Not Overridden

The provider does **not** supply:

```text
--ignore-user-config
--ignore-rules
--profile
--enable
--disable
--strict-config
```

and does **not** accept arbitrary `-c` overrides from callers.

Rationale:

* `--ignore-user-config` — the effect on managed/enterprise policy is
  not yet verified; turning it on could drop enterprise security
  controls.
* `--ignore-rules` — disables execpolicy `.rules` files; clear security
  risk.
* `--profile`, `--enable`, `--disable` — introduce non-deterministic
  configuration.
* Arbitrary `-c` — could override sandbox, approval, or model settings.
* `--strict-config` — could cause unrelated config version mismatches
  to block Worker dispatch.

The **only** `-c` override the provider generates is the fixed
reasoning-effort key (§2.12.6).  Future changes to this policy require
a separate contract revision.

---

#### 2.12.11 CodexCliProvider — Frozen Public API

`CodexCliProvider` **is** the configuration.  There is no separate
`CodexCliProviderConfig` class.

```python
from __future__ import annotations

from dataclasses import dataclass

from dispatcher_gateway import (
    AgentCliInvocation,
    AgentCliProvider,
    DispatchRequest,
)


@dataclass(frozen=True, slots=True)
class CodexCliProvider:
    """Codex CLI adapter — TC-13.8.3 frozen contract."""

    provider_id: str
    executable: str
    sandbox_mode: str

    def __post_init__(self) -> None:
        # Reject at construction — ValueError on any illegal input.
        ...

    def build_invocation(
        self,
        request: DispatchRequest,
    ) -> AgentCliInvocation:
        ...


__all__ = ["CodexCliProvider"]
```

**Exactly three fields — no more, no less:**

| # | Field | Type | Constraint |
|---|-------|------|------------|
| 1 | `provider_id` | `str` | `"codex"` only; satisfies `AgentCliProvider.provider_id` |
| 2 | `executable` | `str` | See executable rules below |
| 3 | `sandbox_mode` | `str` | `"read-only"` or `"workspace-write"` |

`approval_policy` is **not** a field — it is hard-coded as `"never"`
(§2.12.8).

**Forbidden fields** — these must **never** appear on
`CodexCliProvider`:

```text
cwd
workspace
prompt
timeout
model
model_id
reasoning_effort
approval_policy
env
env_overrides
api_key
token
profile
config_overrides
session_id
retry
slot
lease
```

`env_overrides=()` is a fixed value on the generated `AgentCliInvocation`,
not a provider field.

**Executable rules:**

* Must be `str`, non-empty, not pure whitespace.
* Must not contain leading or trailing whitespace.
* Must not contain NUL (`\x00`), CR (`\r`), LF (`\n`), TAB (`\t`),
  VT (`\v`), or FF (`\f`).
* Must not contain embedded arguments — no shell-command-as-string.
* Must not contain shell metacharacters (`&`, `|`, `;`, `` ` ``,
  `$`, `<`, `>`, `(`, `)`, `"`, `'`).
* A path containing ordinary spaces (e.g.
  `C:\Program Files\OpenAI\codex.exe`) **is** legal and must not be
  rejected as "embedded arguments".
* The provider does **not** call `shlex.split()`, `shutil.which()`,
  `os.fspath`, or any other path-resolution function — executable
  resolution remains the Gateway's responsibility (§2.10.9).

**Allowed executable values:**

```text
codex
codex.exe
codex.cmd
<absolute native codex.exe path>
```

**Construction rejection — `ValueError`:** `CodexCliProvider.__init__`
and `__post_init__` raise `ValueError` for:

* `provider_id` not `"codex"`;
* `executable` empty, pure whitespace, contains NUL/CR/LF/TAB/VT/FF,
  or contains embedded arguments or shell metacharacters;
* `sandbox_mode` not in the safe set (§2.12.7).

**`build_invocation` rejection — `ValueError`:** raises `ValueError`
for:

* unknown `selected_deliberation_tier` (see §2.12.6);
* `selected_model_id` empty, with leading/trailing whitespace, or
  containing characters outside the strict allowlist (§2.12.4);
* any other request field that cannot be mapped per this contract.

**Gateway wrapping:** `run_dispatch()` (TC-13.7) wraps provider
exceptions into `DispatchInvocationError`.  TC-13.8.3 does **not**
introduce a parallel public exception hierarchy.

**Module public surface** — the future production module
`skills/agentdesk/scripts/codex_cli_provider.py` will export
exactly one public symbol:

```python
__all__ = ["CodexCliProvider"]
```

No module-level registry, no `register_provider()`, no
`unregister_provider()`.

---

#### 2.12.12 Environment Variable & Authentication Boundary

Frozen:

```python
env_overrides == ()
```

The Codex CLI Provider:

* does **not** read, set, or forward any API key.
* does **not** copy or inspect the full parent process environment.
* does **not** set any authentication environment variable.
* does **not** set `CODEX_HOME`.
* does **not** set `OPENAI_API_KEY`.
* does **not** set `MAD_HOME` or `MAD_PARTICIPANT`.
* does **not** log or return secrets in any form.
* does **not** run `codex login` or `codex logout`.

Authentication is the responsibility of the installation environment
and the Codex CLI itself.  The provider trusts that the local Codex
CLI environment is already authenticated before `run_dispatch` is
called.

---

#### 2.12.13 Output Boundary

The provider requests JSONL output:

```text
--json
--color never
```

but its responsibility ends at constructing the invocation.  It must
**not**:

* parse stdout JSONL;
* convert stdout to text;
* extract report data from output;
* determine business success/failure;
* modify the Gateway's opaque-bytes contract;
* freeze third-party-inferred event type names;
* invent a `codex.*` schema version.

TC-13.7 continues to treat `DispatchResult.stdout` and
`DispatchResult.stderr` as opaque `bytes`.  Output interpretation and
Worker delivery transformation belong to **TC-13.9c**.

The `--color never` flag ensures stdout is free of ANSI escape
sequences, keeping the opaque bytes contract clean.

---

#### 2.12.14 File Output Parameters — Permanently Forbidden

The following must **never** appear in `argv`:

```text
--output-schema <FILE>
--output-last-message <FILE>
-o <FILE>
```

Reasons:

* Both require file paths — introducing file lifecycle management
  beyond the pure `AgentCliInvocation` construction boundary.
* `--output-schema` requires a JSON Schema file to be created and
  managed externally.
* `--output-last-message` writes to the filesystem, violating the
  provider's zero-file-write contract.

The initial version consumes only stdout JSONL bytes.

---

#### 2.12.15 Explicitly Forbidden CLI Behavior

The following must **never** appear in `argv`:

| Forbidden Flag / Pattern | Reason |
|--------------------------|--------|
| `--dangerously-bypass-approvals-and-sandbox` | Security bypass |
| `--dangerously-bypass-hook-trust` | Hook trust bypass |
| `--sandbox danger-full-access` | Full filesystem access |
| `--search` | Live web search — non-deterministic |
| `--oss` | Provider switch — bypasses model binding |
| `--local-provider` | Provider switch — bypasses model binding |
| `--remote` | Remote execution — not in scope |
| `--remote-auth-token-env` | Remote auth — not in scope |
| `--enable` / `--disable` | Feature flag — non-deterministic |
| `--add-dir` | Additional writable directories |
| `--skip-git-repo-check` | Bypasses Git requirement |
| `-C` / `--cd` | Working directory override — cwd is Gateway's |
| `-p` / `--profile` | Config profile — non-deterministic |
| `-i` / `--image` | Image attachment — not in dispatch scope |
| `--ignore-rules` | Disables execpolicy |
| `--ignore-user-config` | Unverified enterprise policy impact |
| `--strict-config` | Unrelated config version mismatch risk |
| `--output-schema` | Requires file management |
| `--output-last-message` / `-o` | Requires file writes |
| `exec resume` | Session resumption |
| `exec review` | Code review — not dispatch |
| Shell wrapper (`cmd /c`, `powershell -Command`, `bash -c`) | Process integrity |
| Task prompt in argv | Stdin-only contract |
| Secrets / API keys / tokens in argv | Security boundary |

Top-level commands also **never** invoked:

```text
resume
fork
cloud
remote-control
app-server
exec-server
```

---

#### 2.12.16 Fail-Closed Rules

The provider must **reject** (fail closed, no silent recovery) for:

| Condition | Exception |
|-----------|-----------|
| `provider_id` not `"codex"` | `ValueError` at construction |
| `executable` empty, pure whitespace, or `None` | `ValueError` at construction |
| `executable` contains NUL, CR, LF, TAB, VT, or FF | `ValueError` at construction |
| `executable` contains embedded arguments or shell metacharacters | `ValueError` at construction |
| `sandbox_mode` not in `{"read-only", "workspace-write"}` | `ValueError` at construction |
| Unknown `selected_deliberation_tier` | `ValueError` in `build_invocation` |
| `selected_model_id` empty, whitespace, or with forbidden characters | `ValueError` in `build_invocation` |
| `provider_id` ≠ mapping key | `DispatchInputError` by Gateway (TC-13.7) |
| Prompt cannot be transmitted per UTF-8 contract | `ValueError` — no replacement or trimming |

No silent correction, trimming, fallback, or alias normalization is
permitted.

---

#### 2.12.17 Explicitly Out of Scope

TC-13.8.3 does **not** implement:

* Codex CLI Provider production module (`codex_cli_provider.py` — TC-13.8.4)
* `WorkerAdapter` (TC-13.9a/9b)
* `WorkerKind` → provider selection (TC-13.9a/9b)
* `ContextBudgetPolicy` invocation (TC-13.5.1 / TC-13.9a/9b)
* Context budget → CLI argument translation (TC-13.9a/9b)
* Dispatch scheduling (TC-13.18)
* Worker slot allocation (TC-13.10)
* Lease management (TC-13.10)
* Retry logic (TC-13.9a/9b)
* Escalation (TC-13.13)
* Rate limiting (TC-13.14)
* State / event / outbox / report writes (TC-13.11)
* Stdout JSONL parsing (TC-13.9c)
* Output schema extraction
* Additional directories or MCP configuration
* Network search
* Codex session resumption
* Remote Codex sessions
* Real Codex CLI invocation
* Model bindings modification
* `CODEX_HOME` management

All of the above remain **Target** for their respective task cards.

---

#### 2.12.18 Status

* ADR Interface Status row #31 "AgentDesk Codex CLI Provider"
  is **Current** (TC-13.8.4).
* TC-13.8.3 froze the contract; TC-13.8.4 implemented the production
  module and test suite.
* This section (§2.12) remains the Frozen Contract — it governs the
  implemented `CodexCliProvider`.
* The production provider module (`codex_cli_provider.py`) and complete
  test suite (`test_codex_cli_provider.py`) are committed.
* §2.10 (DispatcherAgentGateway), §2.11 (Claude Code CLI Provider),
  and all prior Current interfaces remain **Current**.
* TC-13.9a (WorkerAdapter Core contract) is **Target** — this section
  (§2.13) is the Frozen Contract.  TC-13.9b (production implementation)
  and TC-13.9c (output decoding) remain Target.

---

### 2.13 WorkerAdapter Core — Frozen Contract (Current — TC-13.9b)

TC-13.9a freezes the **core execution orchestration** contract for
`worker_adapter.py`.  It covers WorkerKind / TaskDifficulty separation,
budget computation, Gateway dispatch, and the `WorkerResult` return
type.  It explicitly does **not** cover provider output decoding,
concurrency slots, retry, escalation, or state persistence.

---

#### 2.13.1 WorkerKind and TaskDifficulty — Independent Inputs

`WorkerKind` and `TaskDifficulty` are **independent** inputs to
`run_worker`.  Neither is derived from the other.

**Frozen rules:**

1. `WorkerKind` describes the logical Worker tier for this execution
   (`basic_agent` / `standard_agent` / `advanced_agent` / `expert_agent`).
2. `TaskDifficulty` describes the task's intrinsic complexity
   (`basic` / `standard` / `advanced` / `expert`).
3. The two values may differ — an Advanced task may be executed by an
   Expert Worker after escalation, or a Basic task may be routed to a
   Standard Worker during capacity overflow.
4. There is **no** one-to-one mapping between `WorkerKind` and
   `TaskDifficulty`.  No `_WORKER_KIND_TO_DIFFICULTY` dictionary, no
   string manipulation (`_agent` stripping), no enum-value casting, and
   no reverse mapping is permitted in production code.
5. Both inputs must be enum members — bare strings, cross-enum values,
   and non-enum types are rejected (fail-closed).
6. Neither input has a default value in the `run_worker` signature.

Rationale: `TaskDifficulty` selects budget percentages (TC-13.5.1);
`WorkerKind` is a dispatch label that may diverge after escalation
(TC-13.13) or capacity routing.  Conflating them would make budget
semantics unstable across retries.

---

#### 2.13.2 Public Entry Point

```python
async def run_worker(
    request: DispatchRequest,
    worker_kind: WorkerKind,
    task_difficulty: TaskDifficulty,
    providers: Mapping[str, AgentCliProvider],
) -> WorkerResult:
    ...
```

**Frozen parameter order (exact, positional):**

| # | Parameter | Type | Rule |
|---|-----------|------|------|
| 1 | `request` | `DispatchRequest` | Validated frozen dispatch input |
| 2 | `worker_kind` | `WorkerKind` | Enum member, no default |
| 3 | `task_difficulty` | `TaskDifficulty` | Enum member, no default |
| 4 | `providers` | `Mapping[str, AgentCliProvider]` | Call-level explicit dependency; must not be empty |

**Frozen rules:**

* `providers` is a call-level explicit dependency — no module-level
  registry, no `register_provider()`, no `unregister_provider()`.
* Provider lookup follows the TC-13.7 pattern: the Gateway resolves the
  adapter from `request.model_selection.selected_model_provider`.
* WorkerAdapter does **not** create Provider instances, does **not**
  modify the mapping, and does **not** require all three Provider IDs
  to be present — only the one referenced by the snapshot is needed.
* WorkerAdapter does **not** call `select_model.py`, does **not**
  re-select the model, and does **not** apply fallback provider/model.
* `request.model_selection` is consumed as-is — its ten-field snapshot
  is the single source of truth for provider and model routing.

---

#### 2.13.3 WorkerResult — Exact Four Fields

```python
@dataclass(frozen=True, slots=True)
class WorkerResult:
    worker_kind: WorkerKind
    task_difficulty: TaskDifficulty
    budget: BudgetResult
    dispatch_result: DispatchResult
```

**Exactly four fields — no more, no less:**

| # | Field | Type | Rule |
|---|-------|------|------|
| 1 | `worker_kind` | `WorkerKind` | Echoed from input |
| 2 | `task_difficulty` | `TaskDifficulty` | Echoed from input |
| 3 | `budget` | `BudgetResult` | From `compute_budget()` |
| 4 | `dispatch_result` | `DispatchResult` | Raw Gateway result (exit 0 only) |

Fields **explicitly excluded**:

```text
identity        — present in dispatch_result.identity
request         — caller retains the original DispatchRequest
snapshot        — present in request.model_selection
provider        — present in dispatch_result.provider
model_id        — present in dispatch_result.model_id
final_text      — output decoding deferred to TC-13.9c
output          — output decoding deferred to TC-13.9c
events          — output decoding deferred to TC-13.9c
executor_model  — ten-field copy belongs to report layer (TC-13.11)
retry           — single-attempt only
slot            — belongs to TC-13.10
lease           — belongs to TC-13.10
report          — file writes belong to TC-13.11
```

`__all__` exports exactly two symbols:

```python
__all__ = ["WorkerResult", "run_worker"]
```

No internal helper functions, mapping tables, or decoder protocols are
exposed.

---

#### 2.13.4 Execution Order — Single Frozen Sequence

```text
1. Validate request / worker_kind / task_difficulty
2. budget = compute_budget(
       request.model_selection.selected_context_window_tokens,
       task_difficulty,
   )
3. dispatch_result = await run_dispatch(request, providers)
4. return WorkerResult(
       worker_kind=worker_kind,
       task_difficulty=task_difficulty,
       budget=budget,
       dispatch_result=dispatch_result,
   )
```

**Frozen rules:**

1. Budget **must** be computed before Gateway dispatch.  Budget failure
   (TypeError / ValueError from `compute_budget`) must propagate
   immediately — the Gateway must not be called.
2. `run_dispatch` is called exactly once per `run_worker` invocation.
3. `run_worker` does **not** call any `AgentCliProvider` method directly —
   all subprocess execution goes through `run_dispatch`.
4. `DispatchRequest`, `ModelSelectionSnapshot`, and `providers` are not
   modified.
5. `WorkerResult` is only returned when both steps succeed.  Every
   failure path uses exceptions — no error-result flag.

---

#### 2.13.5 Budget Semantics — Informational Only

WorkerAdapter computes the budget and returns it as an informational
result.  It does **not** enforce a token limit.

**Frozen rules:**

* `context_window_tokens` is sourced exclusively from
  `request.model_selection.selected_context_window_tokens`.
* The `task_difficulty` parameter drives percentage and cap selection
  inside `compute_budget()`.
* The full six-field `BudgetResult` is included in `WorkerResult.budget`.
* WorkerAdapter does **not** modify, truncate, or compress the prompt (no prompt modification).
* WorkerAdapter does **not** estimate token counts — character-count
  approximations must not be used as token-count substitutes.
* WorkerAdapter does **not** inject budget instructions into the prompt.
* WorkerAdapter does **not** add any CLI budget flag — neither the
  Claude Code Provider nor the Codex CLI Provider accepts one.
* WorkerAdapter does **not** write `BudgetResult` or any of its fields
  to a file, the existing ten-field snapshot, `executor_model`, an event,
  an outbox entry, or a delivery report.

> **TC-13.9a only computes and returns the budget.  It does not enforce
> the token limit.**  Actual token enforcement requires a future task
> card with a reliable tokenizer or native Provider support.

---

#### 2.13.6 Output Boundary — Opaque Bytes

WorkerAdapter core does **not** parse provider output.

`WorkerResult.dispatch_result.stdout` and
`WorkerResult.dispatch_result.stderr` remain opaque `bytes` — the exact
same contract as `DispatchResult` (TC-13.7 §2.10.7).

**Frozen rules:**

* No Claude JSON parsing in the core module.
* No Codex JSONL parsing in the core module.
* No final-assistant-message extraction.
* No third-party event-type recognition or freezing.
* No `WorkerOutput` type.
* No decoder Protocol.
* No `final_text` field on `WorkerResult`.

Provider output decoding is deferred to **TC-13.9c**, which may only
proceed after reliable Claude and Codex output-schema evidence is
available.

---

#### 2.13.7 Exception Semantics

WorkerAdapter core does **not** introduce a parallel exception hierarchy.

**Frozen rules:**

* Wrong `request` type → `TypeError`.
* Wrong `worker_kind` type → `TypeError`.
* Wrong `task_difficulty` type → `TypeError`.
* `compute_budget()` `TypeError` / `ValueError` → propagated as-is.
* All `DispatchGatewayError` subclasses → propagated as-is.
* `asyncio.CancelledError` / `DispatchCancelledError` → not wrapped.
* `WorkerResult` is only returned on complete success — no error-result
  variant.
* Exception messages must not contain the task prompt, full stdout, or
  full stderr bytes.
* No `WorkerBudgetError`, no `WorkerOutputDecodeError` in the core —
  these are deferred to future decoder work.

---

#### 2.13.8 Single-Attempt, No Retry

`run_worker` executes exactly one attempt.

**Frozen exclusions:**

* No retry loop.
* No attempt counter increment.
* No new `dispatch_id` generation.
* No fallback model selection.
* No difficulty escalation.
* No WorkerKind switching.
* No rate-limit handling.
* No re-queue.

Retry and orchestration → TC-13.18.  Escalation → TC-13.13.
Rate limiting → TC-13.14.

---

#### 2.13.9 Slot / Lease / Concurrency Boundary

WorkerAdapter core has **zero** slot or lease behaviour.

* No concurrency-slot data type.
* No slot allocation or release.
* No lease acquisition or release.
* No lease-expiry check.
* No global per-tier or per-worktree concurrency limit enforcement.
* No fencing.

All slot/lease/concurrency semantics belong to **TC-13.10**
(`WorkerSlotLease`).

---

#### 2.13.10 State & Persistence Boundary

WorkerAdapter core is a **pure execution orchestration** boundary with
**zero file writes**.

It does **not**:

* Write `tasks.yaml`.
* Write events (`docs/pm/events/`).
* Write outbox entries (`docs/pm/outbox/`).
* Write delivery reports (`docs/pm/reports/`).
* Write acceptance records (`docs/pm/acceptances/`).
* Write runtime files (`.agentdesk/runtime/`).
* Execute Git commands.
* Create or remove worktrees.
* Commit.
* Modify canonical state.
* Persist budget results or provider output.

> **WorkerAdapter is an execution-orchestration boundary, not a
> state-transition boundary.**  All state transitions belong to
> TC-13.11 (`ControlPlaneTransitionService`).

---

#### 2.13.11 executor_model Boundary

WorkerAdapter core does **not** construct or write `executor_model`
(the full ten-field selector snapshot).  Callers may copy it from
`request.model_selection` later (TC-13.11), but that is not a
WorkerAdapter core behaviour.

The following fields are **permanently forbidden** from the ten-field
`executor_model` / cross-field snapshot:

```text
worker_kind
task_difficulty
budget_percent
budget_cap_tokens
budget_tokens
reserved_tokens
stdout
stderr
provider_output
```

---

#### 2.13.12 Deep Immutability

* `WorkerResult` is a frozen/slots dataclass — fields cannot be
  reassigned after construction.
* `WorkerKind` and `TaskDifficulty` are enum members — hashable,
  identity-stable.
* `BudgetResult` is a `NamedTuple` — already immutable.
* `DispatchResult` is a frozen/slots dataclass — already immutable.
* `Mapping[str, AgentCliProvider]` is read-only — WorkerAdapter does
  not convert it into a mutable registry.
* No `list`, `dict`, or `set` is introduced in `WorkerResult`.
* No input object is mutated.

---

#### 2.13.13 Task-Card Split

The main Future Task Cards table (§5) records the three-card split
(TC-13.9a / TC-13.9b / TC-13.9c) and the updated TC-13.10 dependency.
This section defines the per-card scope:

* **TC-13.9a** — WorkerAdapter core contract freeze (this section).
  Depends on TC-13.5.1, TC-13.7, TC-13.8, TC-13.8.4.
* **TC-13.9b** — WorkerAdapter core production implementation
  (`worker_adapter.py`).  Depends on TC-13.9a.
* **TC-13.9c** — Provider output decoding investigation and contract
  (Claude JSON + Codex JSONL).  Depends on TC-13.9b + reliable
  Claude/Codex output-schema evidence.

TC-13.10a (`WorkerSlotLease` frozen contract — §2.5) depends on this ADR.
TC-13.10b (data model, store, atomic I/O) depends on TC-13.10a.
TC-13.10c (acquire / release / renew / hold fence) depends on TC-13.10b.
Concurrency fencing must not be blocked by output-decoding work.

---

#### 2.13.14 Status

* ADR Interface Status row #32 "AgentDesk WorkerAdapter Core — Frozen
  Contract" is **Current** (TC-13.9b).
* This section (§2.13) is the Frozen Contract for TC-13.9a — it governs
  the implemented production module (`worker_adapter.py`) and test suite.
* The production module `worker_adapter.py` and matching test suite
  `test_worker_adapter.py` exist and are committed.
* TC-13.9c (output decoding) remains **Target**.
* TC-13.10 is **Current** (fully implemented by TC-13.10a/b/c).
* TC-13.11, TC-13.13, TC-13.14, and TC-13.18 remain **Target**.
* All Current interfaces remain **Current**.


---

### 2.14 ControlPlaneTransitionService — Frozen Contract (Target — TC-13.11a)

TC-13.11a freezes the **authoritative state-transition contract** for
``ControlPlaneTransitionService``.  It defines the CAS preconditions,
lock ordering, event/outbox immutability rules, public API, exception
hierarchy, and explicit non-goals.  No production code is shipped under
TC-13.11a — the contract itself is the deliverable and must be
implemented by TC-13.11b/c.

---
#### 2.14.1 Authoritative Write Scope

The service owns all writes to the following canonical control-plane
files:

| File | Schema | Rule |
|------|--------|------|
| ``docs/pm/state/tasks.yaml`` | ``agentdesk.tasks/v2`` | Updated atomically on every transition |
| ``docs/pm/events/EVT-YYYYMMDD-NNNN.yaml`` | ``agentdesk.state-event/v2`` | Append-only; one new file per transition |
| ``docs/pm/outbox/MSG-YYYYMMDD-NNNN.yaml`` | ``agentdesk.outbox-message/v2`` | Immutable; created once during dispatch |
| ``docs/pm/acceptances/TC-*-rN-aN-reviewN.md`` | ``agentdesk.acceptance/v2`` | Immutable; created during acceptance |
| ``docs/pm/BOARD.md`` | *(derived)* | Regenerated from ``tasks.yaml`` |
| ``docs/pm/STATUS.md`` | *(derived)* | Regenerated from ``tasks.yaml`` |

**Canonical vs derived**:

* ``tasks.yaml``, ``events/*.yaml``, ``outbox/*.yaml``, and
  ``acceptances/*.yaml`` are canonical authority.
* ``BOARD.md`` and ``STATUS.md`` are derived views.  They are
  regenerated synchronously on every transition from the post-transition
  canonical state.  Derived views must **never** serve as CAS input.
* Event and outbox files are immutable after creation — they are never
  modified or overwritten.  Transport state (sent / acknowledged) is
  recorded in gitignored ``transport-receipts.yaml`` only, never in the
  immutable outbox file.

**Derived view failure semantics**:

* Canonical files are written first.  Derived views are rendered after
  all canonical ``os.replace`` operations have completed.
* A derived-view render failure after canonical writes complete leaves
  the repository in a **canonical-consistent / view-stale** state:
  ``tasks.yaml``, events, and outbox are correct; ``BOARD.md`` and/or
  ``STATUS.md`` are out of date.
* ``render_views.py --check`` detects the drift.
* The caller must re-render the views to restore consistency.
* The service does **not** attempt to roll back already-replaced
  canonical files — cross-file rollback is not possible at the
  filesystem level and the service makes no claim to provide it.

---
#### 2.14.2 CAS Preconditions

Every transition must satisfy a compare-and-swap guard before any file
is written.  The CAS input is a frozen ``TransitionCAS``:

```python
@dataclass(frozen=True, slots=True)
class TransitionCAS:
    task_id: str                            # non-empty
    expected_revision: int                  # non-bool, >= 1
    expected_state: str                     # from frozen STATES tuple
    expected_snapshot_commit: str           # 40-char hex SHA
```

**Field semantics**:

| Field | Source | Compared against |
|-------|--------|-----------------|
| ``task_id`` | Caller | Looked up in ``tasks.yaml`` |
| ``expected_revision`` | Caller | ``tasks.yaml`` task ``revision`` field |
| ``expected_state`` | Caller | ``tasks.yaml`` task ``state`` field |
| ``expected_snapshot_commit`` | Caller (observed ``git rev-parse HEAD`` before constructing the request) | ``git rev-parse HEAD`` at service entry |

``expected_snapshot_commit`` is an **opaque observation** supplied by
the caller — the service does not derive or guess it.  Before any file
write, the service reads the current repository HEAD via ``git
rev-parse HEAD`` and compares it to ``expected_snapshot_commit``.
When the values differ, a concurrent writer has modified the repository
since the caller's observation, and the transition must be rejected
with ``TransitionCASConflictError``.  **Zero canonical files are
written on CAS failure.**

**Git HEAD and orphan evidence**.  Uncommitted working-tree files (such
as event/outbox files left by a prior crash before ``tasks.yaml`` was
written) do **not** change ``git rev-parse HEAD``.  A subsequent call
with the same ``expected_snapshot_commit`` may therefore pass the Git
HEAD CAS check even though orphan evidence exists on disk.  The service
must **detect** orphan/partial evidence before writing (§2.14.6) and
must not silently replay over it.  The revision/state CAS still rejects
a transition if ``tasks.yaml`` already reflects the target state.

---
#### 2.14.3 Lease Epoch — Single Authority

Each transition category has exactly **one** authoritative lease-epoch
source:

| Transition category | Lease source | Epoch field |
|---------------------|-------------|-------------|
| Worker-lifecycle transitions (dispatched → in_progress → review_ready → accepted / returned) | ``WorkerSlotLease`` object supplied by caller | ``lease.lease_epoch`` |
| PM-only transitions (draft → ready, accepted → integrated, → blocked / cancelled / superseded) | ``pm_control.lease_epoch`` read from ``tasks.yaml`` at service entry | current ``pm_control.lease_epoch`` |

The service never accepts a bare epoch integer from the caller alongside
a ``WorkerSlotLease`` — the epoch is read exclusively from the supplied
lease object.  ``WorkerSlotLeaseError`` subclasses (including
``WorkerSlotFencingError`` and ``WorkerSlotNotHeldError``) are
propagated unchanged to the caller; the service does not introduce
semantically-duplicate fencing exception types.

For PM-only transitions, ``pm_control`` is validated as a side-car CAS
check: the store's ``holder_id`` and ``lease_epoch`` must match the
current ``tasks.yaml`` before the transition proceeds.

---
#### 2.14.4 Lock Ordering — Frozen

**Worker-lifecycle transitions** must execute inside
``hold_worker_slot_fence()`` (TC-13.10c §2.5.9) and additionally
acquire a project-level control-plane state lock.  The frozen lock
order is:

```text
1. acquire  worker-slot lease lock       (.agentdesk/runtime/.worker-slot-lease.lock)
2. acquire  control-plane state lock     (.agentdesk/runtime/.state-transition.lock)
3. validate CAS, lease epoch, task identity
4. prepare all serialised bytes for canonical files
5. write authoritative files (event, outbox if dispatch,
   updated tasks.yaml, acceptance if applicable)
6. write derived views (BOARD.md, STATUS.md)
7. release  control-plane state lock
8. release  worker-slot lease lock
```

**PM-only transitions** acquire only the control-plane state lock
(steps 2–7).  No component may acquire the control-plane state lock
before the worker-slot lock when a Worker lease is held.  A call that
enters the state lock while already holding the worker-slot lock is
valid; the reverse order is an immediate ``TransitionLockOrderError``
(fail-closed, zero writes).

The control-plane state lock follows the same ``os.open(O_CREAT |
O_EXCL | O_WRONLY)`` ownership-token pattern defined for the
worker-slot lease lock (ADR §2.5.10).  Contention raises
``TransitionLockContentionError`` immediately — no sleeping, waiting,
or retry.

**Granularity**: the control-plane state lock is a single project-level
lock.  Concurrent transitions for different tasks are serialised at
the lock boundary.  Per-task locking is not yet supported; there is
insufficient evidence in the existing protocol or validator to safely
define a per-task key space.

---
#### 2.14.5 ID Generation Responsibility

Every identity used in canonical files has exactly one responsible
party:

| Identity | Generated by | Rationale |
|----------|-------------|-----------|
| ``event_id`` | Caller | Must be globally unique before the transition begins; service validates uniqueness |
| ``message_id`` (outbox) | Caller | Must be globally unique; service validates uniqueness |
| ``dispatch_id`` | Caller | Generated pre-dispatch; service writes it into ``current_dispatch`` and outbox |
| ``dedupe_key`` | **Service** | Deterministically derived: ``{task_id}/r{revision}/a{attempt}/{dispatch_id}/{message_type}`` |

The service must **never** generate any of the first three IDs.
The ``dedupe_key`` is derived by the service from the outbox fields;
the caller cannot supply an alternative.

---
#### 2.14.6 Idempotency, Orphan Detection, and Execution Order

Before any file is written, the service must execute these checks in
fixed order:

```text
1. Acquire locks
2. Read existing canonical state (tasks.yaml, events/, outbox/,
   acceptances/)
3a. Full idempotent-replay check — if ALL of the following hold:
      a. event file with event_id already exists AND its serialised
         bytes match what would be written now;
      b. if an outbox is expected (dispatch transitions): outbox file
         with message_id already exists, dedupe_key matches, and
         serialised bytes match;
      c. tasks.yaml already reflects {task_id: {state: to_state}}
         with matching revision, attempt, and identity fields;
      d. if acceptance is expected: acceptance record exists with
         matching content;
      e. no orphan/partial evidence exists (see step 3b);
   → return idempotent success (TransitionResult, zero file writes).
3b. Orphan/partial evidence check — if some but not all of the
    expected files exist (e.g. event exists but tasks.yaml has
    not transitioned; event+outbox exist but tasks.yaml has not
    transitioned):
   → raise TransitionDuplicateEvidenceError with a description of
     which files are present and which are missing.  Zero writes.
     The caller must recover the partial transition before retrying.
3c. If neither 3a nor 3b applies → continue.
4. Validate CAS (revision, state, snapshot_commit, dispatch identity
   if applicable, lease epoch).
5. Serialise all file contents to bytes.
6. Write authoritative files in fixed order (§2.14.7).
7. Render and write derived views.
8. Release locks.
```

**Fail-closed duplicate detection**.  The service must **never**
overwrite an existing event, outbox, or acceptance record.

| Scenario | Detection step | Behaviour |
|----------|---------------|----------|
| Same ``event_id``, identical content, ``tasks.yaml`` already at target state, all companion files present, no orphan evidence | 3a | **Idempotent success** — return existing result, zero writes |
| Same ``event_id``, different content | 3b | ``TransitionDuplicateEvidenceError`` — zero writes |
| Same ``message_id``, different content | 3b | ``TransitionDuplicateEvidenceError`` — zero writes |
| Same ``dedupe_key``, different ``message_id`` | 3b | ``TransitionDuplicateEvidenceError`` — zero writes |
| Event exists but ``tasks.yaml`` not at target state (partial transition) | 3b | ``TransitionDuplicateEvidenceError`` — zero writes |
| Event+outbox exist but ``tasks.yaml`` not at target state (partial transition) | 3b | ``TransitionDuplicateEvidenceError`` — zero writes |

Equality for byte comparison is determined by exact byte equality of
the fully-serialised YAML content (same keys, same values, same
ordering, same trailing newline).

---
#### 2.14.7 Multi-File Writes and Crash Recovery

The service writes canonical files using the single-file atomic pattern
(``tempfile.mkstemp`` → write/fsync → ``os.replace`` → directory
fsync) already established by ``worker_slot_lease.py`` and
``render_views.py``.  **This guarantees per-file atomic replacement,
not cross-file atomicity.**

The service writes files in a fixed order:

```text
1. event file        (os.replace)
2. outbox file       (os.replace — dispatch transitions only)
3. acceptance record (os.replace — acceptance transitions only)
4. tasks.yaml        (os.replace — LAST authoritative write)
5. BOARD.md          (os.replace — derived)
6. STATUS.md         (os.replace — derived)
```

``tasks.yaml`` is written **last** among the canonical files.  The
service does **not** create a Git commit.  The caller must commit
canonical files as a single Git commit after the service returns
successfully.

**Exception categories and write guarantees**:

* **Pre-write failures** (input validation, schema read errors, CAS
  conflict, duplicate/integrity conflict, lock contention, lock-order
  violation, WorkerSlot fencing failure, serialisation failure): **all
  managed canonical files remain byte-for-byte unchanged.**  No
  temporary files are left on disk.

* **In-write failures** (``TransitionWriteError`` — an ``os.replace``
  or ``os.fsync`` failure partway through the write sequence): files
  that were already ``os.replace``-d **may** have been changed; files
  not yet replaced are **unchanged**; the temporary file for the
  failing write is cleaned up on a best-effort basis.  The error
  message includes a safe stage identifier (e.g. ``"event"``,
  ``"tasks.yaml"``) — never a full path or payload.

  The service does **not** provide cross-file automatic rollback.
  ``TransitionWriteError`` after one or more successful ``os.replace``
  operations may leave partial authoritative state on disk.  The caller
  must run the validator to detect and recover from partial transitions.

**Crash scenarios**:

| Crash point | Outcome | Recovery |
|------------|---------|----------|
| Before any ``os.replace`` | No files written | Retry with same request (idempotent) |
| After event, before outbox | Event orphaned; ``tasks.yaml`` unchanged | Idempotency check (§2.14.6 step 3b) detects orphan → ``TransitionDuplicateEvidenceError``; caller completes remaining writes or removes orphan |
| After event+outbox, before ``tasks.yaml`` | Event+outbox exist; ``tasks.yaml`` unchanged | Step 3b detects partial evidence → ``TransitionDuplicateEvidenceError``; caller completes or cleans up |
| After ``tasks.yaml``, before views | Canonical state consistent; views stale | ``render_views.py --check`` detects drift; caller re-renders |

---
#### 2.14.8 Transition Types and Required Payloads

All state names are taken from the frozen ``STATES`` tuple in
``validate_project.py``.  Event-type names match the protocol
(§2.6 of the core protocol reference).

| # | Transition | ``from_state`` | ``to_state`` | Worker lease required | Produces outbox | Produces acceptance |
|---|-----------|---------------|-------------|----------------------|-----------------|---------------------|
| 1 | ``draft → ready`` | ``draft`` | ``ready`` | No | No | No |
| 2 | ``ready → dispatched`` | ``ready`` | ``dispatched`` | **Yes** | **Yes** (``task.dispatch``) | No |
| 3 | ``dispatched → in_progress`` | ``dispatched`` | ``in_progress`` | **Yes** | No | No |
| 4 | ``in_progress → review_ready`` | ``in_progress`` | ``review_ready`` | **Yes** | No | No |
| 5 | ``review_ready → accepted`` | ``review_ready`` | ``accepted`` | **Yes** | No | **Yes** |
| 6 | ``review_ready → returned`` | ``review_ready`` | ``returned`` | **Yes** | No | No |
| 7 | ``returned → ready`` | ``returned`` | ``ready`` | No | No | No |
| 8 | ``accepted → integrated`` | ``accepted`` | ``integrated`` | No | No | No |
| 9 | ``accepted → blocked`` (INTEGRATION_FAILED) | ``accepted`` | ``blocked`` | No | No | No |
| 10 | ``* → blocked`` (TASK_BLOCKED) | any non-terminal | ``blocked`` | No | No | No |
| 11 | ``blocked → (resume)`` (BLOCKER_RESOLVED) | ``blocked`` | caller-specified | No | No | No |
| 12 | ``blocked → draft`` (BLOCKER_RESCOPED) | ``blocked`` | ``draft`` | No | No | No |
| 13 | ``blocked → cancelled`` (BLOCKER_CANCELLED) | ``blocked`` | ``cancelled`` | No | No | No |
| 14 | ``* → cancelled`` (TASK_CANCELLED) | any non-terminal | ``cancelled`` | Depends on active dispatch | No | No |
| 15 | ``* → superseded`` (TASK_SUPERSEDED) | any non-terminal | ``superseded`` | Depends on active dispatch | No | No |

**Field modifications per transition** (canonical fields in
``tasks.yaml``):

| Transition | Fields set / modified | Fields cleared |
|-----------|----------------------|---------------|
| draft → ready | ``revision`` frozen; ``ready_at`` | — |
| ready → dispatched | ``attempt``; ``current_dispatch`` (all sub-fields); ``dispatched_at``; ``model_selection``; ``report_path`` | — |
| dispatched → in_progress | ``started_at`` | — |
| in_progress → review_ready | ``implementation_commit``; ``report_commit``; ``delivered_at``; ``delivery_state: submitted`` | — |
| review_ready → accepted | ``accepted_commit``; ``acceptance_path``; ``accepted_at``; ``delivery_state: accepted`` | ``current_dispatch`` |
| review_ready → returned | ``delivery_state: rejected`` | ``current_dispatch`` |
| returned → ready | attempt preserved | ``current_dispatch`` |
| accepted → integrated | ``integrated_commit``; ``integrated_at`` | — |
| → blocked | blocked envelope (8 fields) | — (``current_dispatch`` retained only if ``blocked_attempt_valid: true``) |
| → cancelled | — | ``current_dispatch`` |
| → superseded | — | ``current_dispatch`` |

**Revision increment**: ``revision`` is bumped by the caller — the
service never increments it.  The service validates that the
caller-supplied ``expected_revision`` matches the current value.

**Attempt increment**: ``attempt`` is set by the caller for each new
dispatch — the service never increments it.  The service validates that
the dispatch CAS ``expected_attempt`` matches.

---
#### 2.14.9 Event File Rules — Frozen

Every transition produces exactly one ``agentdesk.state-event/v2`` file
in ``docs/pm/events/``.  Frozen rules:

1. Filename: ``EVT-YYYYMMDD-NNNN.yaml`` — derived from ``event_id``.
2. ``event_id`` is a globally unique ``EVT-*`` string supplied by the
   caller.  The service rejects duplicate ``event_id`` values.
3. **Common required keys** (present on every state event regardless of
   ``event_type``):
   ``schema_version`` (``"agentdesk.state-event/v2"``),
   ``event_id``,
   ``event_type``,
   ``task_id``,
   ``revision``,
   ``attempt``,
   ``dispatch_id`` (nullable — ``null`` when no active dispatch),
   ``from_state``,
   ``to_state``,
   ``lease_epoch``,
   ``actor_role_id`` (always ``"PM"`` for control-plane events),
   ``occurred_at``.
4. **Always-present nullable fields** (must exist as keys; value may be
   ``null``):
   ``source_message_id`` — the ``callback_id`` when the transition was
   triggered by a Worker callback; ``null`` otherwise.
5. **Always-present container fields** (must exist as keys; value may
   be empty list):
   ``evidence_refs`` — list of strings referencing evidence documents
   (e.g. ``["docs/pm/tasks/TC-031-r2-runtime-api.md"]``);
   ``guard_results`` — list of guard-check objects, each with
   ``guard``, ``inputs``, ``result``, ``checked_at``, and
   ``evidence_ref`` fields.
6. **Event-type‑specific fields**:
   * ``TASK_DISPATCHED``: ``payload_digest`` (required) — ``"sha256:"``
     + 64 lowercase hex characters.  Computed by the service as SHA-256
     of the paired outbox file's raw UTF-8 LF bytes.  The outbox file
     must be written and its bytes finalised before the event
     ``payload_digest`` is computed.
   * ``CHANGE_INTEGRATED`` when ``accepted_commit`` is not an ancestor
     of ``integrated_commit``: ``accepted_commit``,
     ``integrated_commit``, ``equivalence_method``, ``equivalence_result``,
     ``equivalence_evidence_ref`` (all required).
7. ``occurred_at`` is generated by the service from the caller-supplied
   ``now: datetime`` parameter.
8. Events are append-only — never modified after creation.
9. Unknown extra keys at the event root are rejected fail-closed
   (``TransitionSchemaError``).  Missing keys from sets 3–5 are
   rejected fail-closed.  Missing event-type‑specific keys (set 6) are
   rejected fail-closed for the relevant ``event_type``.
10. ``event_id`` is validated against the ``^EVT-.+`` pattern.
    ``message_id`` (outbox) is validated against the ``^MSG-.+`` pattern.

---
#### 2.14.10 Outbox File Rules — Frozen

Dispatch transitions produce exactly one ``agentdesk.outbox-message/v2``
file in ``docs/pm/outbox/``.  Frozen rules:

1. Filename: ``MSG-YYYYMMDD-NNNN.yaml`` — derived from ``message_id``.
2. ``message_id`` is a globally unique ``MSG-*`` string supplied by the
   caller.  The service rejects duplicate ``message_id`` values.
3. Required root keys: ``schema_version``, ``message_id``, ``event_id``
   (→ TASK_DISPATCHED event), ``message_type`` (``task.dispatch``),
   ``dedupe_key``, ``task_id``, ``revision``, ``attempt``,
   ``dispatch_id``, ``destination_role_id``, ``created_at``,
   ``model_selection``, ``payload``.
4. ``dedupe_key`` is derived by the service:
   ``{task_id}/r{revision}/a{attempt}/{dispatch_id}/task.dispatch``.
5. ``model_selection`` must be an exact ten-field object whose keys are
   the frozen ``MODEL_SELECTION_FIELDS`` tuple from
   ``validate_project.py``.  Extra keys, missing keys, or value
   mismatches against the ``current_dispatch.model_selection`` are
   rejected fail-closed (``TransitionSchemaError``).
6. ``payload`` must contain ``task_path``, ``task_card_commit``,
   ``base_commit``, ``branch``, and ``report_path`` — all matching the
   caller-supplied dispatch fields.
7. Outbox files are immutable — never modified after creation.
8. Transport status (sent/acknowledged) is recorded in gitignored
   ``transport-receipts.yaml`` only, never written into the outbox file.
   The outbox records intent, not delivery confirmation.

---
#### 2.14.11 Public API — Frozen Signatures

All public types are frozen/slots dataclasses.  Every type the caller
needs to construct or catch is exported via ``__all__``:

```python
__all__ = [
    "ControlPlaneTransitionService",
    "TransitionCAS",
    "DispatchCAS",
    "TransitionRequest",
    "TransitionPayload",
    "TransitionResult",
    "TransitionEventContext",
    "GuardResult",
    "GuardInput",
    "SpecifyPayload",
    "DispatchPayload",
    "AcknowledgePayload",
    "DeliverySubmittedPayload",
    "DeliveryAcceptedPayload",
    "DeliveryReturnedPayload",
    "RequeuePayload",
    "IntegrationPayload",
    "BlockedPayload",
    "BlockerResolvedPayload",
    "BlockerRescopedPayload",
    "BlockerCancelledPayload",
    "CancelledPayload",
    "SupersededPayload",
    "ControlPlaneTransitionError",
    "TransitionValidationError",
    "TransitionCASConflictError",
    "TransitionLockContentionError",
    "TransitionLockOrderError",
    "TransitionDuplicateEvidenceError",
    "TransitionSchemaError",
    "TransitionWriteError",
]
```

```python
@dataclass(frozen=True, slots=True)
class TransitionCAS:
    """Immutable CAS preconditions for a state transition."""
    task_id: str
    expected_revision: int
    expected_state: str
    expected_snapshot_commit: str


@dataclass(frozen=True, slots=True)
class DispatchCAS:
    """Immutable CAS extension for dispatch-lifecycle transitions."""
    expected_dispatch_id: str
    expected_attempt: int


@dataclass(frozen=True, slots=True)
class SpecifyPayload:
    """Payload for draft → ready (TASK_SPECIFIED)."""
    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class DispatchPayload:
    """Payload for ready → dispatched (TASK_DISPATCHED)."""
    dispatch_id: str
    role_id: str
    model_selection: ModelSelectionSnapshot
    task_card_path: str
    task_card_commit: str
    base_commit: str
    branch: str
    report_path: str
    outbox_message_id: str               # MSG-*


@dataclass(frozen=True, slots=True)
class AcknowledgePayload:
    """Payload for dispatched → in_progress (DISPATCH_ACKNOWLEDGED)."""
    # Requires DispatchCAS; no extra fields beyond common context.
    pass


@dataclass(frozen=True, slots=True)
class DeliverySubmittedPayload:
    """Payload for in_progress → review_ready (DELIVERY_SUBMITTED)."""
    implementation_commit: str           # 40-char SHA
    report_commit: str                   # 40-char SHA


@dataclass(frozen=True, slots=True)
class DeliveryAcceptedPayload:
    """Payload for review_ready → accepted (DELIVERY_ACCEPTED)."""
    accepted_commit: str                 # 40-char SHA
    acceptance_path: str                 # project-relative


@dataclass(frozen=True, slots=True)
class DeliveryReturnedPayload:
    """Payload for review_ready → returned (DELIVERY_RETURNED)."""
    # Requires DispatchCAS; no extra fields beyond common context.
    pass


@dataclass(frozen=True, slots=True)
class RequeuePayload:
    """Payload for returned → ready (TASK_REQUEUED)."""
    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class IntegrationPayload:
    """Payload for accepted → integrated (CHANGE_INTEGRATED)."""
    integrated_commit: str               # 40-char SHA
    equivalence_method: str | None       # patch_id | tree | approved_mapping
    equivalence_evidence_ref: str | None


@dataclass(frozen=True, slots=True)
class BlockedPayload:
    """Payload for * → blocked (TASK_BLOCKED, INTEGRATION_FAILED)."""
    blocked_reason: str
    blocked_kind: str
    blocked_owner: str
    unblock_condition: str
    resume_state: str                    # from frozen STATES tuple
    blocked_attempt_valid: bool | None


@dataclass(frozen=True, slots=True)
class BlockerResolvedPayload:
    """Payload for blocked → resume_state (BLOCKER_RESOLVED)."""
    resume_to_state: str                 # caller-specified target state


@dataclass(frozen=True, slots=True)
class BlockerRescopedPayload:
    """Payload for blocked → draft (BLOCKER_RESCOPED)."""
    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class BlockerCancelledPayload:
    """Payload for blocked → cancelled (BLOCKER_CANCELLED)."""
    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class CancelledPayload:
    """Payload for * → cancelled (TASK_CANCELLED)."""
    # No extra fields beyond common event context.
    pass


@dataclass(frozen=True, slots=True)
class SupersededPayload:
    """Payload for * → superseded (TASK_SUPERSEDED)."""
    superseded_by: str                   # valid task_id of the replacement


"""TransitionPayload is a closed union; one variant per event_type."""

TransitionPayload = Union[
    SpecifyPayload,
    DispatchPayload,
    AcknowledgePayload,
    DeliverySubmittedPayload,
    DeliveryAcceptedPayload,
    DeliveryReturnedPayload,
    RequeuePayload,
    IntegrationPayload,
    BlockedPayload,
    BlockerResolvedPayload,
    BlockerRescopedPayload,
    BlockerCancelledPayload,
    CancelledPayload,
    SupersededPayload,
]


@dataclass(frozen=True, slots=True)
class GuardInput:
    """Immutable single key-value pair for a guard input set.

    ``GuardResult.inputs`` is ``tuple[GuardInput, ...]`` — key order
    is caller-preserved.  Duplicate keys are rejected at construction.
    """
    key: str                             # non-empty, no leading/trailing whitespace
    value: str | int | bool | None       # legal YAML scalar only

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise TypeError(
                f"key must be a non-empty str, got {type(self.key).__name__}"
            )


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Immutable guard-check result for a state‑event ``guard_results`` list.

    Constructed from validated fields — fail-closed on illegal input.
    """
    guard: str                           # non-empty guard identifier
    inputs: tuple[GuardInput, ...]       # caller-preserved order; no duplicate keys
    result: str                          # passed | failed | skipped | not_applicable
    checked_at: str                      # RFC 3339 UTC
    evidence_ref: str                    # non-empty evidence reference

    def __post_init__(self) -> None:
        if not isinstance(self.guard, str) or not self.guard:
            raise TypeError(
                f"guard must be a non-empty str, got {type(self.guard).__name__}"
            )
        if not isinstance(self.inputs, tuple):
            raise TypeError(
                f"inputs must be a tuple, got {type(self.inputs).__name__}"
            )
        for i, item in enumerate(self.inputs):
            if not isinstance(item, GuardInput):
                raise TypeError(
                    f"inputs[{i}] must be GuardInput, got {type(item).__name__}"
                )
        keys: list[str] = []
        for inp in self.inputs:
            keys.append(inp.key)
        if len(keys) != len(set(keys)):
            raise ValueError("guard inputs must not contain duplicate keys")
        if self.result not in {"passed", "failed", "skipped", "not_applicable"}:
            raise ValueError(
                f"result must be passed|failed|skipped|not_applicable, "
                f"got {type(self.result).__name__}"
            )
        if not isinstance(self.checked_at, str) or not self.checked_at:
            raise TypeError(
                f"checked_at must be non-empty RFC 3339 UTC str"
            )
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref:
            raise TypeError(
                f"evidence_ref must be a non-empty str, "
                f"got {type(self.evidence_ref).__name__}"
            )


@dataclass(frozen=True, slots=True)
class TransitionEventContext:
    """Immutable event-context data supplied by the caller.

    Provides ``source_message_id``, ``evidence_refs``, and
    ``guard_results`` — the three fields that every
    ``agentdesk.state-event/v2`` must carry but that are independent
    of the transition payload.
    """
    source_message_id: str | None        # callback_id for callback-triggered transitions; None otherwise
    evidence_refs: tuple[str, ...]       # caller-preserved order; no duplicates; each non-empty
    guard_results: tuple[GuardResult, ...]  # caller-preserved order; may be empty

    def __post_init__(self) -> None:
        # source_message_id
        if self.source_message_id is not None:
            if not isinstance(self.source_message_id, str) or not self.source_message_id:
                raise TypeError(
                    "source_message_id must be a non-empty str or None, "
                    f"got {type(self.source_message_id).__name__}"
                )
            if "\0" in self.source_message_id or "\r" in self.source_message_id or "\n" in self.source_message_id:
                raise ValueError(
                    "source_message_id must not contain NUL, CR, or LF"
                )
        # evidence_refs
        if not isinstance(self.evidence_refs, tuple):
            raise TypeError(
                f"evidence_refs must be a tuple, got {type(self.evidence_refs).__name__}"
            )
        seen_refs: set[str] = set()
        for i, ref in enumerate(self.evidence_refs):
            if not isinstance(ref, str) or not ref:
                raise TypeError(
                    f"evidence_refs[{i}] must be a non-empty str"
                )
            if ref != ref.strip():
                raise ValueError(
                    f"evidence_refs[{i}] must not have leading or trailing whitespace"
                )
            if ref in seen_refs:
                raise ValueError(
                    f"evidence_refs must not contain duplicates"
                )
            seen_refs.add(ref)
        # guard_results
        if not isinstance(self.guard_results, tuple):
            raise TypeError(
                f"guard_results must be a tuple, got {type(self.guard_results).__name__}"
            )
        for i, item in enumerate(self.guard_results):
            if not isinstance(item, GuardResult):
                raise TypeError(
                    f"guard_results[{i}] must be GuardResult, got {type(item).__name__}"
                )


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    """Immutable input for a single state transition.

    All fields have verifiable origins in the current control-plane
    state.  The service validates CAS preconditions, writes canonical
    files, regenerates derived views, and returns a
    ``TransitionResult``.
    """
    cas: TransitionCAS
    dispatch_cas: DispatchCAS | None     # required for dispatch-lifecycle transitions
    event_id: str                        # EVT-*; caller-generated, globally unique
    event_type: str                      # from frozen event-type names
    payload: TransitionPayload
    event_context: TransitionEventContext


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Immutable result of a successful state transition."""
    task_id: str
    event_id: str
    from_state: str
    to_state: str
    occurred_at: str                     # RFC 3339 UTC
    outbox_message_id: str | None        # set when outbox was written


@dataclass(frozen=True, slots=True)
class ControlPlaneTransitionService:
    """CAS-write service for control-plane state transitions.

    Takes a ``project_root`` and does NOT store mutable state.
    Every ``apply_transition`` call is self-contained.
    """
    project_root: Path

    def apply_transition(
        self,
        request: TransitionRequest,
        lease: WorkerSlotLease | None,
        now: datetime,
    ) -> TransitionResult:
        """Validate CAS, write canonical files, render views, return result.

        *lease* is required for worker-lifecycle transitions (transition
        types 2–6 in §2.14.8) and optional for PM-only transitions.

        *now* must be a timezone-aware UTC ``datetime``.  Naive or
        non-UTC values are rejected (``TypeError``).
        """
        ...
```

**Frozen API rules**:

1. All public types use ``frozen=True, slots=True`` dataclasses.
   No ``object``, no bare ``dict``, no ``Any`` appear as payload types.
   ``TransitionPayload`` is a closed union of concrete payload
   dataclasses — the ``event_type`` deterministically selects which
   variant is valid.
2. ``project_root`` is an absolute ``Path`` supplied at service
   construction.
3. ``now`` is an explicit ``datetime`` parameter — the service never
   calls ``datetime.now()`` internally.
4. ``lease`` is an explicit ``WorkerSlotLease | None`` — ``None``
   signals a PM-only transition.  For worker-lifecycle transitions,
   ``lease`` must be non-``None``.
5. Error messages must **never** call ``repr()``, ``str()``, or ``{!r}``
   on untrusted input values.  Only ``type(value).__name__`` is safe for
   error context.  ``task_id``, ``event_id`` and ``dispatch_id`` are
   considered safe identifiers and may appear in error messages.
6. Error messages must **never** contain: the task prompt, any payload
   field values (except safe identifiers from rule 5), stdout/stderr
   content, secrets, full ``holder_instance_id``, full
   ``canonical_worktree``, or environment variable values.
7. An ``event_type`` / payload variant mismatch is a
   ``TransitionValidationError`` (fail-closed, zero writes).
8. ``TransitionEventContext`` is required in every ``TransitionRequest``.
   The service does not supply defaults for ``source_message_id``,
   ``evidence_refs``, or ``guard_results`` — the caller must explicitly
   provide them.
9. ``GuardInput`` values are restricted to YAML-friendly scalar types
   (``str | int | bool | None``).  Nested structures, ``float``, and
   arbitrary Python objects are rejected at construction.
10. ``GuardResult`` is deeply immutable — ``inputs``,
    ``evidence_refs`` (on ``TransitionEventContext``), and
    ``guard_results`` are all ``tuple``, never ``list``/``dict``/``set``.
    Source mutations after construction have no effect.

---
#### 2.14.12 Event Serialisation Source Table

Every field in a generated ``agentdesk.state-event/v2`` file has a
single, documented origin:

| Event field | Source | Notes |
|-------------|--------|-------|
| ``schema_version`` | Service constant | ``"agentdesk.state-event/v2"`` |
| ``event_id`` | ``TransitionRequest.event_id`` | Validated (``EVT-*``, unique) |
| ``event_type`` | ``TransitionRequest.event_type`` | Validated against frozen names |
| ``task_id`` | ``TransitionCAS.task_id`` | Validated against ``tasks.yaml`` |
| ``revision`` | ``TransitionCAS.expected_revision`` (CAS-verified) | Current value from ``tasks.yaml`` after CAS |
| ``attempt`` | ``DispatchCAS.expected_attempt`` (CAS-verified) | Current value from ``tasks.yaml``; ``null`` when no active dispatch |
| ``dispatch_id`` | ``DispatchCAS.expected_dispatch_id`` (CAS-verified) | Current value; ``null`` when no active dispatch |
| ``from_state`` | ``TransitionCAS.expected_state`` (CAS-verified) | Current value from ``tasks.yaml`` after CAS |
| ``to_state`` | ``TransitionRequest.payload`` → derived by transition type | From §2.14.8 transition table |
| ``source_message_id`` | ``TransitionEventContext.source_message_id`` | ``callback_id`` or ``null`` |
| ``evidence_refs`` | ``TransitionEventContext.evidence_refs`` | Serialised as YAML list |
| ``guard_results`` | ``TransitionEventContext.guard_results`` | Serialised as YAML list of mappings |
| ``occurred_at`` | ``apply_transition(... now=...)`` | Service-formatted RFC 3339 UTC |
| ``lease_epoch`` | ``WorkerSlotLease.lease_epoch`` (worker-lifecycle) or ``pm_control.lease_epoch`` (PM-only) | Current value after CAS |
| ``actor_role_id`` | Service constant | ``"PM"`` for all control-plane events |
| ``payload_digest`` (TASK_DISPATCHED only) | Service-computed | ``sha256:`` + SHA-256 of the final outbox file bytes |
| ``accepted_commit`` (CHANGE_INTEGRATED, non-ancestor) | ``IntegrationPayload.integrated_commit`` (caller) | Required when ``accepted_commit`` is not ancestor of ``integrated_commit`` |
| ``integrated_commit`` (CHANGE_INTEGRATED, non-ancestor) | ``IntegrationPayload.integrated_commit`` (caller) | Same as above |
| ``equivalence_method`` (CHANGE_INTEGRATED, non-ancestor) | ``IntegrationPayload.equivalence_method`` (caller) | ``patch_id`` / ``tree`` / ``approved_mapping`` |
| ``equivalence_result`` (CHANGE_INTEGRATED, non-ancestor) | Service constant | ``"passed"`` |
| ``equivalence_evidence_ref`` (CHANGE_INTEGRATED, non-ancestor) | ``IntegrationPayload.equivalence_evidence_ref`` (caller) | Immutable check output ref |

No event field is generated without a documented source.  No field
appears in the output without an input channel to supply it.

---
#### 2.14.14 Exception Hierarchy — Frozen

```text
ControlPlaneTransitionError                  (Exception)
├── TransitionValidationError                — input type/value violation, event_type/payload mismatch
├── TransitionCASConflictError               — CAS precondition failed
├── TransitionLockContentionError            — control-plane lock held
├── TransitionLockOrderError                 — reverse lock acquisition
├── TransitionDuplicateEvidenceError         — duplicate/conflicting event_id / message_id / dedupe_key, or orphan evidence
├── TransitionSchemaError                    — corrupt or invalid canonical file on read, extra/missing keys
└── TransitionWriteError                     — os.replace / os.fsync failure during the write sequence
```

**Pre-write failure guarantees (all exception types except
``TransitionWriteError``)**: zero authoritative file writes — every
managed canonical file remains byte-for-byte unchanged from its
pre-transaction state.  No temporary files are left on disk.

**``TransitionWriteError`` guarantee**: at least one ``os.replace``
succeeded before the failure.  The error's message includes a safe
stage identifier.  Cross-file automatic rollback is **not** provided.
The caller must run the validator and recovery procedures.

**Propagation rules**:

* ``WorkerSlotLeaseError`` subclasses are propagated **unchanged** to
  the caller.  The service never introduces a semantically-equivalent
  "TransitionFencingError".
* ``TypeError`` is raised for type violations (wrong input types,
  naive datetime, etc.) — matching existing module conventions.
* ``TransitionDuplicateEvidenceError`` covers all duplicate-ID
  scenarios including same-ID-different-content, same-dedupe-key-
  different-message-id, and orphan/partial evidence detection.
* ``TransitionCASConflictError`` covers stale ``expected_revision``,
  stale ``expected_state``, stale ``expected_dispatch_id``, stale
  ``expected_attempt``, and stale ``expected_snapshot_commit``.
* ``ControlPlaneTransitionService`` does **not** catch
  ``WorkerSlotLeaseError``.  Those exceptions propagate through the
  service boundary untouched.

---
#### 2.14.15 Acceptance Boundary

The service may write acceptance records and update
``accepted_commit`` / ``acceptance_path`` / ``delivery_state`` when
the caller supplies a valid ``TransitionRequest`` for the
``review_ready → accepted`` transition.  The service does **not**
authorise the acceptance — the caller must already have determined that
the acceptance is authorised (via ``ApprovalGate``, TC-13.12).

The service does **not**:

* Grant, revoke, or infer approval.
* Manage ``granted_approval_ids`` — that is the caller's responsibility.
* Generate ``MODEL_DEGRADATION_APPROVED`` or
  ``MODEL_DEGRADATION_REVOKED`` events — those belong to TC-13.12.
* Write ``owner_approval`` fields in the acceptance record beyond what
  the caller supplies.

---
#### 2.14.14 Explicit Non-Goals

TC-13.11 must **not** implement, freeze, or assume responsibility for:

* **ApprovalGate** (TC-13.12) — TASK_APPROVAL structured scope,
  MODEL_DEGRADATION_APPROVED/REVOKED events, ``granted_approval_ids``
  management.
* **EscalationService** (TC-13.13) — difficulty tier progression.
* **RateLimit service** (TC-13.14) — provider 429 handling.
* **StateProvider** (TC-13.17) — read-only access boundary.
* **WorkflowOrchestrator** (TC-13.18) — full lifecycle coordination
  (acquire → heartbeat → run_worker → fenced transition → release).
* **Provider output decoder** (TC-13.9c) — stdout parsing.
* **Retry / backoff** — single-attempt only.
* **Subprocess invocation** — no CLI, model, or network calls.
* **Git worktree creation or deletion** — the service assumes the
  worktree already exists.
* **Secret / auth management** — credentials are never read, written,
  or logged.
* **Dashboard** (TC-13.20) — read-only HTML views.
* **MAD audit** (TC-13.15 / TC-13.16) — audit subprocess invocation.
* **Git commit** — the service writes files but does not commit.
  The caller must commit all canonical files as a single Git commit.
* **Transport receipt management** — transport state lives in
  gitignored runtime files, never in canonical event/outbox files.
* **pm-lease acquisition / renewal** — the service reads
  ``pm_control.lease_epoch`` for PM-only transitions but does not
  manage the PM lease lifecycle.

---
#### 2.14.15 Task-Card Split

```text
TC-13.11a — this frozen contract (§2.14)
TC-13.11b — typed models, validation, state lock, serialisation helpers
TC-13.11c — CAS transition execution, event/outbox writes, view
            regeneration, crash-recovery tests
```

| Card | Depends on | Scope | Interface #16 status after completion |
|------|-----------|-------|--------------------------------------|
| TC-13.11a | TC-13.10c, TC-13.2 | This contract only | **Target** |
| TC-13.11b | TC-13.11a | Data model, store validation, state lock, serialisation | **Target** |
| TC-13.11c | TC-13.11b | Full ``apply_transition``, all 15 transition types, complete test matrix | **Target** → **Current** |

Interface #16 status must remain **Target** until TC-13.11c is complete
and the production module and full test suite are committed.  No
intermediate "Current (contract frozen)" sub-status is permitted.

---
#### 2.14.16 Status

* ADR Interface Status row #16 "AgentDesk ControlPlaneTransitionService"
  is **Target**.
* This section (§2.14) is the Frozen Contract for TC-13.11a.
* TC-13.11b (typed models, validation, state lock, serialisation helpers,
  single-file atomic write infrastructure) is **committed** — the
  production module (``control_plane_transition.py``) and matching test
  suite (``test_control_plane_transition.py``) exist with 31 frozen
  public symbols.  ``apply_transition()`` raises ``NotImplementedError``
  referencing TC-13.11c.
* TC-13.11c remains **Target** — ``apply_transition()`` is not yet
  executable.
* Interface #16 remains **Target** until TC-13.11c is complete.
* §2.5 (WorkerSlotLease) is **Current**.
* TC-13.10a/b/c are all **Current**.
* TC-13.12, TC-13.13, TC-13.14, TC-13.17, TC-13.18, and all subsequent
  Target interfaces remain **Target**.
* TC-13.11c remains **Target** — the production entry point is not yet
  callable.

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
| TC-13.8.3 | Codex CLI Provider frozen contract | This ADR |
| TC-13.8.4 | Codex CLI Provider implementation | TC-13.8.3 |
| TC-13.9a | WorkerAdapter core contract freeze (§2.13) | TC-13.5.1, TC-13.7, TC-13.8, TC-13.8.4 |
| TC-13.9b | WorkerAdapter core production implementation (`worker_adapter.py`) | TC-13.9a |
| TC-13.9c | Provider output decoding investigation and contract (Claude JSON + Codex JSONL) | TC-13.9b + reliable Claude/Codex output-schema evidence |
| TC-13.10a | WorkerSlotLease frozen contract (§2.5) | This ADR |
| TC-13.10b | WorkerSlotLease data model, validation, runtime store, atomic I/O, file lock | TC-13.10a |
| TC-13.10c | WorkerSlotLease acquire / release / renew / hold fence | TC-13.10b |
| TC-13.11 | ControlPlaneTransitionService | TC-13.10c, TC-13.2 |
| TC-13.12 | ApprovalGate (TASK_APPROVAL structured scope) | TC-13.11 |
| TC-13.13 | EscalationService | TC-13.11 |
| TC-13.14 | RateLimit service | TC-13.11 |
| TC-13.15 | MAD `audit` sub-command (`mad.audit-result/v1`) | TC-13.2 |
| TC-13.16 | AgentDesk MadAuditGateway | TC-13.15 |
| TC-13.17 | StateProvider (read-only) | TC-13.11 |
| TC-13.18 | WorkflowOrchestrator (full integration) | TC-13.10c, 13.11, 13.12, 13.13, 13.14, 13.16, 13.17 |
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
