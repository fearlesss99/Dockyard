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
| `mad agents --format json` | **Target** | TC-13.1 | `mad.agents/v1` |
| `mad deliberate --format json` | **Current** | N/A (MVP) | Informal (`RunResult.to_dict()`) |
| `mad deliberate --format json` (formal) | **Target** | TC-13.1 | `mad.run-result/v1` |
| `mad resume --format json` | **Current** | N/A (MVP) | Informal (same shape as deliberate) |
| `mad audit` | **Target** | TC-13.1 | `mad.audit-result/v1` |

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
- `0` — always (unless argument parse error → `2`).

Limitations (Current):
- No `--format` flag.
- No JSON output.
- Enabled/disabled is a localised Chinese string, not a machine-readable boolean.

### 2.2 `mad deliberate --format json`

**Status: Current**

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

Limitations (Current):
- **No `schema_version`** field — consumers cannot version-detect the output.
- `status` is a Chinese string (`"完成"`, `"带警告完成"`), not a machine-readable enum.
- No `verdict` field (the concept does not exist in deliberation).
- No structured `issues` or `evidence` arrays.
- `participants` is a flat list of strings (agent IDs), not objects with
  stage contributions.

Exit codes:

| Exit | Meaning |
|------|---------|
| `0` | Deliberation completed (including `status: "带警告完成"`) |
| `1` | Workflow, recovery, or report failure |
| `2` | Parameter, plan, or configuration error |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

### 2.3 `mad resume --format json`

**Status: Current**

```bash
mad resume <deliberation_id> --format json
```

Output shape is identical to `mad deliberate --format json`.  Exit codes are
identical.  The same limitations apply (no `schema_version`, Chinese `status`
strings).

---

## 3. Target Interfaces

### 3.1 `mad agents --format json` → `mad.agents/v1`

**Status: Target — to be implemented by TC-13.1**

```bash
mad agents --format json
```

Stdout (JSON array):

```json
[
  {
    "id": "pi-deepseek",
    "name": "Pi · DeepSeek V4 Pro",
    "adapter": "pi",
    "model": "deepseek/deepseek-v4-pro",
    "role": "侧重严谨推理、识别假设和构造反例",
    "executable": null,
    "enabled": true,
    "default_report": false,
    "timeout_seconds": 300,
    "context_budget": 1000000
  }
]
```

Schema: `mad.agents/v1`.  Array elements contain all fields of `AgentProfile`.

Exit codes: `0` (success), `2` (argument error).

### 3.2 `mad deliberate --format json` → `mad.run-result/v1`

**Status: Target — to be implemented by TC-13.1**

Identical invocation to Current `mad deliberate --format json`, but the output
is versioned with `schema_version: "mad.run-result/v1"` and `status` is
a machine-readable enum (`"completed" | "completed_with_warnings"`).

```json
{
  "schema_version": "mad.run-result/v1",
  "deliberation_id": "<id>",
  "status": "completed | completed_with_warnings",
  "report": "<full-markdown-report>",
  "archive_path": "<absolute-path>",
  "warnings": ["<warning>"],
  "participants": [
    {
      "agent_id": "<id>",
      "agent_name": "<name>",
      "adapter": "<adapter>",
      "model": "<model-or-null>",
      "stage_contributions": ["OPENING", "CRITIQUE"]
    }
  ],
  "convergence": {
    "strategy": "auto",
    "triggered": false,
    "reason": "...",
    "marked_participants": 0,
    "disputes": [],
    "status": "not_triggered"
  },
  "plan": {
    "participants": [{"id": "...", "name": "...", "adapter": "...", "model": "...", "role": "..."}],
    "report_agent_id": "...",
    "organizer_agent_id": null,
    "source": "manual",
    "depth": "deep",
    "critic_agent_id": null
  }
}
```

### 3.3 `mad audit` → `mad.audit-result/v1`

**Status: Target — to be implemented by TC-13.1**

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

`--agents` is a **single CSV parameter** (e.g. `--agents id1,id2,id3`), not
a repeated flag.  This matches the existing `mad deliberate --agents` convention.

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
  "participants": [
    {
      "agent_id": "<id>",
      "agent_name": "<name>",
      "adapter": "<adapter>",
      "model": "<model-id>",
      "stage_contributions": ["OPENING", "CRITIQUE", "REVISION"]
    }
  ],
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

**`verdict` is a mutually-exclusive enum**: `pass`, `fail`, or `blocked`.

**Exit codes for `mad audit`**:

| Exit | Condition |
|------|-----------|
| `0` | Audit completed normally — `verdict` may be `pass`, `fail`, or `blocked` |
| `1` | Parse failure, model invocation failure, or evidence verification failure |
| `2` | Parameter, configuration, or workspace validation failure (caller error) |
| `3` | Insufficient available participants |
| `130` | User cancellation or SIGINT |

**Critical rule**: When the audit process runs to completion but the
deliberation finds issues (`verdict: fail`) or cannot reach a conclusion
(`verdict: blocked`), the exit code is still `0`.  Exit code `1` is ONLY
for infrastructure/model failures, not for negative business findings.

---

## 4. Gateway Invocation Contract

### 4.1 Subprocess Environment

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

### 4.2 Timeout

The Gateway applies `timeout_seconds` from its configuration at the subprocess
level.  If the subprocess does not exit within this window, the Gateway sends
`SIGTERM` (or `taskkill /T /F` on Windows), waits a grace period, then sends
`SIGKILL`.  This is a **GatewayTimeoutError** — distinct from any MAD exit code.

### 4.3 Output Capture

The Gateway:
1. Captures stdout and stderr separately.
2. Computes SHA-256 of the raw stdout bytes.
3. Parses stdout as JSON.
4. Validates `schema_version` matches the expected schema.
5. Stores the SHA-256 and `archive_path` in runtime receipts (never in Git).
6. Extracts `verdict` and `issues` for the audit event.

If stdout is not valid JSON, or `schema_version` is missing/unknown, the
Gateway treats this as **fail-closed**: it records an audit failure without
exposing raw output to downstream systems.

### 4.4 Gateway Configuration Schema

**Status: Target** — `agentdesk.gateway-config/v1`

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

## 5. Schema Namespaces

| Namespace prefix | Owner | Versioning |
|------------------|-------|------------|
| `mad.*` | MAD CLI | `/v<N>` suffix, monotonic integer |
| `agentdesk.*` | AgentDesk Skill | `/v<N>` suffix, monotonic integer |

Cross-references use opaque foreign keys only.  Example: an AgentDesk audit
event may record `deliberation_id` from MAD, but must not embed or interpret
MAD schema objects.

---

## 6. Fail-Closed Rules

1. Unknown `schema_version` → fail closed.  Do not attempt best-effort parsing.
2. Missing required fields → fail closed.
3. `verdict` value outside `["pass", "fail", "blocked"]` → fail closed.
4. Non-JSON stdout (parse error) → fail closed.
5. Subprocess timeout → GatewayTimeoutError (fail closed).
6. Subprocess exit code not in `{0, 1, 2, 3, 130}` → fail closed.

"Fail closed" means: record an audit failure event, do not pass partial or
unvalidated data to downstream systems, and alert the PM.

---

## 7. Revision History

| Date | Revision | Author | Changes |
|------|----------|--------|---------|
| 2026-07-26 | 1 (Target) | PM | Initial contract.  All Target interfaces pending TC-13.1. |
