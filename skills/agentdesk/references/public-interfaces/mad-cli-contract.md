# MAD CLI Contract — Public Interface Reference

This document is the canonical public-interface contract between the
Multi-Agent Decision (`mad`) CLI and the AgentDesk Skill.  Every
interface is marked **Current** (callable today) or **Target**
(specified, not yet implemented).  AgentDesk code must never depend on
a Target interface as if it were Current.

---

## 1. Interface Status Summary

| Interface | Status | Implemented by | Schema |
|-----------|--------|----------------|--------|
| `mad agents` (TSV) | **Current** | N/A (MVP) | None (tab-separated) |
| `mad agents --format json` | **Current** — verified by TC-13.21f | TC-13.2 | `mad.agents/v1` |
| `mad deliberate --format json` | **Current** | N/A (MVP) | `mad.run-result/v1` (via `RunResult.to_dict()`) |
| `mad deliberate --format json` (formal) | **Current** — verified by TC-13.21f | TC-13.2 | `mad.run-result/v1` |
| `mad resume --format json` | **Current** — verified by TC-13.21f | TC-13.2 | `mad.run-result/v1` (same shape as deliberate) |
| `mad audit` | **Current** | TC-13.15 | `mad.audit-result/v1` |
| `agentdesk.mad-refs/v1` | **Current** | TC-13.6 | Runtime MAD invocation record |

---

## 2. Current Interfaces

### 2.1 `mad agents`

**Status: Current**

```bash
mad agents
```

Output (tab-separated to stdout):

```text
<id>\t<name>\t<adapter>\t<启用|禁用>
```

Example:

```text
pi-deepseek	Pi · DeepSeek V4 Pro	pi	启用
codebuddy-reviewer	CodeBuddy Reviewer	codebuddy	启用
```

Exit codes:

| Exit | Meaning |
|------|---------|
| `0` | Normal output |
| `2` | Argparse parameter error |

Uncaught configuration exceptions (e.g. corrupt `agents.toml`) may produce
non-zero exits; these are not stable public exit semantics.

Limitations (Current):
- TSV output shows enabled/disabled as a localised Chinese string
  (`启用`/`禁用`), not a machine-readable boolean.  For machine-readable
  output use `mad agents --format json` (see §3.1).

### 2.2 `mad deliberate --format json`

**Status: Current — verified by TC-13.21f**

```bash
mad deliberate "<question>" \
  --workspace <path> \
  --agents <id1,id2,...> \
  --report-agent <id> \
  --confirm-plan \
  --format json
```

Stdout is a single JSON object produced by `RunResult.to_dict()`:

```json
{
  "schema_version": "mad.run-result/v1",
  "deliberation_id": "<id>",
  "status": "完成 | 带警告完成",
  "report": "<full-markdown-report>",
  "archive_path": "<absolute-path>",
  "warnings": ["<warning>", "..."],
  "participants": ["<agent-id>", "..."],
  "convergence": {"strategy": "auto", "triggered": false, "reason": "...", "marked_participants": 0, "disputes": [], "status": "未触发"},
  "plan": {"participants": [{"id": "...", "name": "...", "adapter": "...", "model": "...", "role": "..."}], "report_agent_id": "...", "organizer_agent_id": null, "source": "manual", "depth": "deep", "critic_agent_id": null}
}
```

The root object contains exactly the `RunResult` fields plus `schema_version`
(`"mad.run-result/v1"`); all existing field shapes are unchanged.  Stdout
contains **only** this single JSON object — progress, preflight output, plan
preview, and warnings all go to **stderr**.  See §3.2 for the formal schema.

Notes:
- `status` is a Chinese string (`"完成"`, `"带警告完成"`), not a machine-readable enum.
- No `verdict` field (the concept does not exist in deliberation).
- No structured `issues` or `evidence` arrays.
- `participants` is a flat list of strings (agent IDs).

Exit codes for `mad deliberate`:

| Exit | Meaning |
|------|---------|
| `0` | Deliberation completed (including `status: "带警告完成"`) |
| `1` | Workflow, recovery, or report failure |
| `2` | Parameter, plan, or configuration error |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

### 2.3 `mad resume --format json`

**Status: Current — verified by TC-13.21f**

```bash
mad resume <deliberation_id> --format json
```

Output shape is identical to `mad deliberate --format json` — a single JSON
object with `"schema_version": "mad.run-result/v1"` to stdout; progress and
warnings go to stderr.

Exit codes for `mad resume`:

| Exit | Meaning |
|------|---------|
| `0` | Resume completed |
| `1` | Workflow or recovery failure |
| `130` | User cancellation or SIGINT |

`mad resume` does **not** have a dedicated exit code `3` for insufficient
participants — that distinction is specific to `mad deliberate`.
Argparse-level parameter errors (e.g. missing `deliberation_id`) produce exit
code `2` (Python `argparse` default).  Uncaught configuration exceptions must
not be documented as stable public exit semantics for `resume`.

### 2.4 `mad audit` → `mad.audit-result/v1`

**Status: Current — implemented by TC-13.15**

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
  --depth <fast|balanced|deep> \
  --convergence auto \
  --confirm-plan \
  --format json
```

`--agents` is a **single CSV parameter** (e.g. `--agents id1,id2,id3`), not
a repeated flag.  This matches the existing `mad deliberate --agents` convention.

Stdout (`mad.audit-result/v1`):

```json
{
  "schema_version": "mad.audit-result/v1",
  "deliberation_id": "<uuid-or-archive-id>",
  "status": "completed",
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
        "line": "<optional-line-number>",
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
  "plan": {"depth": "fast|balanced|deep"}
}
```

Key semantics:

- `status` describes the audit **process** outcome.  When exit code is `0`,
  `status` is **fixed** to the exact string `"completed"`.
- `verdict` is a mutually-exclusive enum (`pass`, `fail`, or `blocked`)
  describing the **business finding**.  `verdict` may be `pass`, `fail`, or
  `blocked` even when exit is `0`.
- `verdict: "blocked"` is not an infrastructure failure — the process completed
  but could not reach a pass/fail conclusion due to missing evidence.
- Infrastructure failures are not encoded as `verdict` values.
- When `verdict` is `fail` or `blocked` but the process completed normally,
  exit code is still `0`.
- `participants` is `list[str]` (agent ID strings), matching
  `mad.run-result/v1` shape.
- The root object has exactly **11** keys: `schema_version`, `deliberation_id`,
  `status`, `verdict`, `issues`, `evidence`, `warnings`, `report`,
  `archive_path`, `participants`, `plan`.

**Exit codes for `mad audit`**:

| Exit | Condition |
|------|-----------|
| `0` | Audit process completed — `status: "completed"`; `verdict` may be `pass`, `fail`, or `blocked` |
| `1` | Parse failure, model invocation failure, or evidence verification failure |
| `2` | Parameter, configuration, or workspace validation failure (caller error) |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

---

## 3. Target Interfaces

All interfaces previously listed here are now **Current — verified by TC-13.21f**
against the MAD production serialisation code.  The section heading is retained
for backward reference; status markers are updated below.

### 3.1 `mad agents --format json` → `mad.agents/v1`

**Status: Current — verified by TC-13.21f**

```bash
mad agents --format json
```

Stdout is a root JSON object, not a bare array:

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

Each agent object exposes only these public fields:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | yes | Stable unique agent ID |
| `name` | string | yes | Display name |
| `adapter` | string | yes | `claude`, `pi`, `codex`, `grok`, `codebuddy`, `agy`, `cursor`, `reasonix` |
| `model` | string\|null | yes | Model identifier; null if not configured |
| `enabled` | boolean | yes | Whether eligible for preflight |
| `default_report` | boolean | yes | At most one agent is `true` |
| `timeout_seconds` | integer | yes | Single-invocation timeout |
| `context_budget` | integer | yes | Declared context token budget |

The following AgentProfile fields are **forbidden** in public output:

| Field | Reason |
|-------|--------|
| `executable` | Local filesystem path; security boundary |
| `extra_args` | May contain sensitive or local-only configuration |
| `role` | Internal prompt modifier; not a public interface field |

The contract must never claim that `mad.agents/v1` outputs the full
`AgentProfile`.

Exit codes: `0` (success), `2` (argument error).

**stdout/stderr boundary**: with `--format json`, stdout contains **only** the
single JSON object above — no Markdown, no logging, no surrounding text.
`mad agents` calls `initialize()` and `load_agents()` before printing, neither
of which writes to stdout.

### 3.2 `mad deliberate --format json` → `mad.run-result/v1`

**Status: Current — verified by TC-13.21f**

Backward-compatible: the only change from Current is the addition of
`schema_version` at the top level.  All other fields keep their Current
shapes exactly:

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
- Change `participants` from `list[str]` to an object array with stage
  contributions.
- Restructure `convergence` or `plan` objects.

If future versions need English status strings, object-typed participants,
or restructured convergence/plan, they must use `mad.run-result/v2`.

Exit codes are the same as Current `mad deliberate`.

**stdout/stderr boundary**: for both `mad deliberate --format json` and
`mad resume --format json`, stdout contains **only** the single JSON object.
Progress messages, preflight output, the plan preview, warnings, and the
archive path line all go to **stderr** (via the `progress=` callback and
`print(..., file=sys.stderr)`).

**Verification source**: TC-13.21f verified these contracts against MAD
production serialisation code (`src/mad/models.py`, `src/mad/cli.py`) and the
corresponding CLI/audit tests at MAD commit
`9b402874c9fbd3fd28fb402dc28ce3de6b21a568`.

---

## 4. Runtime Schema: `agentdesk.mad-refs/v1`

**Status: Current — implemented by TC-13.6**

Gitignored runtime file at `.agentdesk/runtime/mad-refs.yaml`.
Uses a strict JSON root object:

```json
{
  "schema_version": "agentdesk.mad-refs/v1",
  "updated_at": "2026-07-26T12:00:00Z",
  "refs": [
    {
      "task_id": "TC-031",
      "dispatch_id": "DSP-TC031-R2-A1-7F3C",
      "purpose": "planning",
      "deliberation_id": "20260726T120000Z-a1b2c3d4",
      "depth": "deep",
      "stdout_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "report_sha256": "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a",
      "status": "<MAD status string>",
      "archive_path": "/absolute/path/to/MAD_HOME/deliberations/20260726T120000Z-a1b2c3d4",
      "created_at": "2026-07-26T12:05:00Z"
    }
  ]
}
```

Field rules:

| Field | Type | Rule |
|-------|------|------|
| `task_id` | string | AgentDesk task ID that triggered this MAD invocation |
| `dispatch_id` | string | AgentDesk dispatch ID |
| `purpose` | string | `"planning"` or `"audit"` |
| `deliberation_id` | string | MAD archive ID from stdout JSON |
| `depth` | string | `"fast"`, `"balanced"`, or `"deep"` |
| `stdout_sha256` | string | SHA-256 of raw stdout bytes (before JSON parse) |
| `report_sha256` | string | SHA-256 of the parsed `report` field's UTF-8 bytes |
| `status` | string | From MAD stdout's `status` field — Chinese string semantics (e.g. `"完成"`, `"带警告完成"`) |
| `archive_path` | string | Absolute path from MAD stdout; runtime-only, never in Git |
| `created_at` | string | RFC 3339 UTC timestamp |

Critical rules:

- `stdout_sha256` is computed on the raw subprocess stdout bytes
- `report_sha256` is computed on the parsed report field's UTF-8 bytes
- `archive_path` is **runtime-only** — the entire `mad-refs` file is
  gitignored and must never be committed
- Git-tracked events may reference `stdout_sha256` and `report_sha256` but
  must **never** include `archive_path`

---

## 5. Gateway Invocation Contract

### 5.1 Subprocess Environment

AgentDesk Gateway must set the following environment variables when invoking
`mad` as a subprocess:

```python
env = {
    **os.environ,
    "MAD_HOME": gateway_config["mad_home"],  # Absolute path, same for all calls
    # MAD_PARTICIPANT is set internally by MAD's CliAdapter; do not set here
}
```

`MAD_HOME` ensures the subprocess uses the correct agent registry and data
directory, isolating it from the user's global MAD installation.

### 5.2 Timeout

The Gateway applies `timeout_seconds` from its configuration at the subprocess
level.  If the subprocess does not exit within this window, the Gateway sends
`SIGTERM` (or `taskkill /T /F` on Windows), waits a grace period, then sends
`SIGKILL`.  This is a **GatewayTimeoutError** — distinct from any MAD exit code.

### 5.3 Output Capture

The Gateway:
1. Captures stdout and stderr separately.
2. Computes SHA-256 of the raw stdout bytes → `stdout_sha256`.
3. Parses stdout as JSON.
4. Validates `schema_version` matches the expected schema.
5. Extracts `report` field, computes SHA-256 of its UTF-8 bytes → `report_sha256`.
6. Records the invocation in `agentdesk.mad-refs/v1` (runtime-only).
7. Extracts `verdict` and `issues` for the audit event (Git-tracked, without
   `archive_path`).

If stdout is not valid JSON, or `schema_version` is missing/unknown, the
Gateway treats this as **fail-closed**: it records an audit failure without
exposing raw output to downstream systems.

### 5.4 Gateway Configuration Schema

**Status: Target** — `agentdesk.gateway-config/v1` (TC-13.6)

```json
{
  "schema_version": "agentdesk.gateway-config/v1",
  "mad_executable": "mad",
  "mad_home": "/absolute/path/to/MAD_HOME",
  "timeout_seconds": 1800,
  "planning_agent_ids": ["pi-deepseek", "pi-minimax"],
  "planning_report_agent_id": "pi-minimax",
  "audit_agent_ids": ["pi-deepseek", "pi-minimax", "codebuddy-reviewer"],
  "audit_report_agent_id": "pi-minimax"
}
```

All fields are required.  `mad_home` must be an absolute path to an
initialised MAD data directory (containing `config/agents.toml`).

---

## 6. Schema Namespaces

| Namespace prefix | Owner | Versioning |
|------------------|-------|------------|
| `mad.*` | MAD CLI | `/v<N>` suffix, monotonic integer |
| `agentdesk.*` | AgentDesk Skill | `/v<N>` suffix, monotonic integer |

Cross-references use opaque foreign keys only.  Example: an AgentDesk audit
event may record `deliberation_id` from MAD, but must not embed or interpret
MAD schema objects.

---

## 7. Fail-Closed Rules

1. Unknown `schema_version` → fail closed.  Do not attempt best-effort parsing.
2. Missing required fields → fail closed.
3. `verdict` value outside `["pass", "fail", "blocked"]` → fail closed.
4. Non-JSON stdout (parse error) → fail closed.
5. Subprocess timeout → GatewayTimeoutError (fail closed).
6. Subprocess exit code not in `{0, 1, 2, 3, 130}` → fail closed.

"Fail closed" means: record an audit failure event, do not pass partial or
unvalidated data to downstream systems, and alert the PM.

---

## 8. Revision History

| Date | Revision | Author | Changes |
|------|----------|--------|---------|
| 2026-07-26 | 1 (Target) | PM | Initial contract.  All Target interfaces pending future TC numbers. |
| 2026-07-26 | 2 (Target) | PM | Corrected Implemented-by references (→TC-13.2, TC-13.15, etc.). Added `mad.agents/v1` root object. Added `mad.run-result/v1` backward-compat rules. Split exit codes by sub-command. Added `agentdesk.mad-refs/v1` runtime schema. Clarified `verdict: blocked` vs infrastructure failure. |
| 2026-07-28 | 3 (Current) | PM | TC-13.16a: `mad audit` → Current (TC-13.15); moved to §2.4. Frozen MadAuditGateway contract (§2.17 in ADR). Fixed `--depth <fast|balanced|deep>`, `status: "completed"` on exit 0, `plan` exact shape as `{"depth": "fast|balanced|deep"}`, 11-key root object. |
| 2026-08-01 | 4 (Current) | TC-13.21f | `mad agents --format json` → `mad.agents/v1` and `mad deliberate/resume --format json` → `mad.run-result/v1` verified against MAD production code; statuses promoted to Current. Documented stdout/stderr boundary and MAD verification commit `9b402874c9fbd3fd28fb402dc28ce3de6b21a568`. |
