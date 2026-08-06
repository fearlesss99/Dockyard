# Provider Doctor Contract — Public Interface Reference

**Status: Current — TC-13.21e.1**

This document freezes the contract of `skills/agentdesk/scripts/doctor.py`,
the read-only pre-start diagnostic for AgentDesk external provider and MAD
configuration. It lets an ordinary user confirm — without running a real
model, exposing an API key, or touching the network — whether a project
satisfies the startup conditions for provider execution and the MAD gateway.

Out of scope (unchanged by this contract):

- Real provider execution: **Target** (WorkflowOrchestrator Interface #22).
- Codex worker-output decoder: **Target/deferred — TC-13.9c.2**.
- Provider rate-limit detection: **Target — TC-13.14c**.
- Owner-loss contract and Interface #22: **unchanged**.
- Reasonix Basic Provider: **Contract Current — TC-13.28a.2 Phase A** (contract + adapter +
  decoder implemented; permission policy evidence-current via loopback stub
  (`acceptEdits` + `Bash,Read,Write,Edit`); API probe and end-to-end runtime
  remain Target — TC-13.28a.2 Phase B).
  D009 does not yet include `reasonix`; it will be extended when the adapter
  is production-ready.

---

## 1. Public API (exact)

```python
class DoctorCheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
```

Exactly three members.

```python
@dataclass(frozen=True, slots=True)
class DoctorCheck:
    check_id: str
    status: DoctorCheckStatus
    summary: str
    remediation: str | None
```

Exactly four fields.

```python
@dataclass(frozen=True, slots=True)
class DoctorRequest:
    project_root: Path
    mad_home: Path | None
```

Exactly two fields.

```python
@dataclass(frozen=True, slots=True)
class DoctorReport:
    ready: bool
    checks: tuple[DoctorCheck, ...]
```

Exactly two fields.

```python
def run_doctor(request: DoctorRequest) -> DoctorReport: ...
def main(argv: list[str] | None = None) -> int: ...
```

`__all__` is exactly:

```python
[
    "DoctorCheckStatus",
    "DoctorCheck",
    "DoctorRequest",
    "DoctorReport",
    "DoctorError",
    "DoctorInputError",
    "run_doctor",
    "main",
]
```

`DoctorError` is the base exception; `DoctorInputError(DoctorError)` is
raised for request shape/type violations. All exception messages are fixed
strings: they never echo user input, paths, config values, or secrets, and
user-controlled objects are never `repr()`-ed.

No public field is typed `dict` or `Any`. `DoctorReport.checks` is a tuple
of frozen dataclasses (deep immutability).

## 2. Checks D001–D012 (fixed order)

| ID | Name | PASS | WARN | FAIL |
|----|------|------|------|------|
| D001 | Project Root | absolute, exists, plain directory, contains `docs/pm` + `.agentdesk` | — | missing / file / link / no AgentDesk structure |
| D002 | Runtime Directory | `.agentdesk/runtime/` exists as plain directory | — | missing / link / reparse point |
| D003 | Model Bindings | `model-bindings.yaml` parses via canonical loader and passes `agentdesk.model-bindings/v2`; at least one enabled binding | empty bindings, or all disabled | missing / unreadable / schema-invalid |
| D004 | Role Policies | `docs/pm/ROLE-POLICIES.yaml` parses; every role valid via canonical policy requirements; every role has ≥1 eligible enabled binding (minimum tier, deliberation tier, capabilities at risk `L0`, `inherit`) | valid policy but no enabled bindings | missing / invalid policy / role with zero eligible bindings |
| D005 | Gateway Config | `gateway.yaml` parses via canonical loader and passes `validate_gateway_config()` (`agentdesk.gateway-config/v1`) | — | missing / unreadable / template placeholders / validation failure |
| D006 | MAD Executable | configured `mad_executable` resolves to a file or via local PATH (never executed) | — | not configured / placeholder / unresolvable |
| D007 | MAD Home | absolute plain directory outside the AgentDesk worktree; `config/agents.toml` regular non-link file; registry parses (TOML) with unique non-empty agent IDs and ≥1 agent | — | not configured / relative / missing / link / inside worktree / registry missing, malformed, duplicate, or empty |
| D008 | Audit Agents | `audit_agent_ids` non-empty unique strings; `audit_report_agent_id` ∈ `audit_agent_ids`; all IDs registered in the MAD registry | — | unconfigured / placeholder / duplicates / report agent outside set / unknown IDs / registry unavailable |
| D009 | Provider Executables | every enabled binding's provider ∈ {`claude`, `claudecode`, `codex`} and its canonical CLI (`claude`/`claude`/`codex`) resolves locally (never executed) | no enabled bindings | unsupported provider / unresolvable CLI / bindings unavailable |
| D010 | Decoder Eligibility | all enabled bindings use decoder-supported providers (`claude`, `claudecode`; version-locked Claude 2.1.214) | codex enabled but no role depends solely on codex; or no enabled bindings | codex is the sole eligible provider for ≥1 role (decoder deferred TC-13.9c.2); or a provider has no decoder support |
| D011 | Dashboard Availability | `html_dashboard` module exists next to `doctor.py` and imports cleanly; no HTML generated, no browser opened | — | module missing / import failure |
| D012 | Runtime Safety | exact entry `.agentdesk/runtime/` present in root `.gitignore` or `.git/info/exclude`; no credential-like key segments in gateway/model-bindings schemas; known lock/evidence runtime files are not links | — | ignore entry missing / credential-like field / linked evidence file |

Notes:

- D003/D004/D005 reuse the canonical parsers and validators
  (`select_model._load_worktree_json_object`, `select_model._validated_bindings`,
  `select_model._policy_requirements`, `select_model._rejection_reasons`,
  `mad_gateway.validate_gateway_config`). Doctor never ships a second
  gateway or model-binding parser.
- D006/D009 reuse `mad_gateway._resolve_executable` semantics (path
  candidate → regular executable file; bare name → local `PATH` lookup).
  Executables are only resolved, never run; `mad --version` is never called.
- D007 parses only non-secret registry identifiers (`id`). Doctor never
  reads credential fields; MAD keeps secrets inside each provider CLI.
- Worker provider executables are call-level configuration in AgentDesk;
  the canonical CLI name (D009) is the only locally resolvable artifact.
  Doctor must not claim full dispatch closure merely because a CLI exists.

## 3. `ready` computation

```text
any FAIL → ready = False
no FAIL  → ready = True   (WARN never blocks readiness)
```

Check order is fixed (D001…D012). Identical file state always yields
identical order, statuses, summaries, and remediations. No wall-clock time,
randomness, or UUIDs are used.

## 4. CLI

```text
python <skill>/scripts/doctor.py --project <absolute-path> [--mad-home <absolute-path>] [--format text|json]
```

- `--project` (required): absolute AgentDesk project root.
- `--mad-home` (optional): absolute MAD home; overrides `gateway.yaml`
  `mad_home` for D007.
- `--format`: `text` (default) or `json`.

Exit codes:

| Exit | Meaning |
|------|---------|
| `0` | `ready = true` |
| `1` | `ready = false` |
| `2` | argument or input type error (including non-absolute paths) |

No interactive prompts. Python ≥ 3.11 is required (TOML registry parsing).

## 5. Output schemas

Text format — one line per check, then a verdict line:

```text
PASS D001 project root is a valid AgentDesk project directory
WARN D003 no model bindings configured | remediation: add at least one enabled binding for the local providers
FAIL D005 gateway config is missing or unreadable | remediation: copy the template gateway.yaml into .agentdesk/runtime/ and configure every field
NOT READY
```

JSON format — root object with exactly three keys:

```json
{
  "schema_version": "agentdesk.doctor-report/v1",
  "ready": false,
  "checks": [
    {"check_id": "D001", "status": "pass", "summary": "...", "remediation": null}
  ]
}
```

Each check object has exactly four keys (`check_id`, `status`, `summary`,
`remediation`). Both formats must never contain:

- API keys, tokens, or environment variable values
- configuration file contents
- absolute workspace paths
- user task payloads
- captured stdout/stderr of any external process

## 6. Security boundaries

Doctor guarantees: zero file writes, zero network access, zero subprocess
(including Git), zero model/API calls, no API-key reads, no secret output,
no `repr()` of untrusted objects, fixed exception messages. The only
filesystem reads allowed are inside the explicitly passed project root and
MAD home directories.

## 7. Gateway template

`assets/project-template/.agentdesk/runtime/gateway.yaml` ships with
`init_project.py` and contains only the frozen
`agentdesk.gateway-config/v1` fields:

| Field | Template value |
|-------|----------------|
| `schema_version` | `agentdesk.gateway-config/v1` |
| `mad_executable` | `<configure-mad-executable>` |
| `mad_home` | `<configure-mad-home-absolute-path>` |
| `timeout_seconds` | `1800` |
| `planning_agent_ids` | `["<configure-planning-agent-id>"]` |
| `planning_report_agent_id` | `<configure-planning-report-agent-id>` |
| `audit_agent_ids` | `["<configure-audit-agent-id>"]` |
| `audit_report_agent_id` | `<configure-audit-report-agent-id>` |

Rules:

- Same schema as the production validator (exact eight keys, no more).
- No API key / token fields. No user-machine absolute paths.
- All placeholders carry the explicit `<configure-*>` marker; Doctor D005
  reports them as pending configuration until replaced.
- `init_project.py` never scans the user's system or writes real paths
  into this file.

## 8. Revision history

| Date | Revision | Changes |
|------|----------|---------|
| 2026-07-30 | 1 (Current) | TC-13.21e.1: initial freeze — D001–D012, CLI, JSON schema, gateway template. |
| 2026-08-02 | 2 (Current) | TC-13.28a.1: Reasonix Basic Provider contract freeze noted in out-of-scope; D009 extension deferred to adapter card. |
