# WorkerOutput Decoder — Implementable Contract (Current — TC-13.9c.1)

Version-locked, fail-closed, pure-function decoder for Claude Code 2.1.214
Worker output. Interface #33 frozen contract.

## Status

**Current** as of TC-13.9c.1.  The Claude 2.1.214 decoder is implemented and
callable.  Codex decoding remains Target; the full TC-13.9c task card remains
Target until Codex is complete.

This document is the authoritative frozen specification for the
WorkerOutput decoder public API, data model, security boundaries, and
exception hierarchy.

---

## 1. Purpose

Convert opaque `WorkerResult.dispatch_result.stdout` bytes into typed,
trustable `WorkerOutput` and `DeliveryReceipt` data classes.  The decoder is
version-locked: it only supports `claude` / `claudecode` at CLI version
`2.1.214`.  Any other provider or version is fail-closed.

---

## 2. Public API

Exactly 12 symbols:

```python
__all__ = [
    "WorkerCompletionStatus",
    "WorkerOutput",
    "DeliveryReceipt",
    "decode_worker_result",
    "require_delivery_receipt",
    "WorkerOutputError",
    "WorkerOutputUnsupportedProviderError",
    "WorkerOutputUnsupportedVersionError",
    "WorkerOutputIntegrityError",
    "WorkerOutputDecodeError",
    "WorkerOutputSchemaError",
    "WorkerOutputIdentityError",
]
```

### 2.1 `WorkerCompletionStatus`

Strict three-value `str, Enum`:

```python
class WorkerCompletionStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
```

No case-folding, no aliases, no unknown fallback.

### 2.2 `WorkerOutput`

Frozen, slots, nine-field dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `identity` | `DispatchIdentity` | Audit identity from the dispatch |
| `provider` | `str` | Provider string (`claude` or `claudecode`) |
| `model_id` | `str` | Model identifier from the dispatch snapshot |
| `status` | `WorkerCompletionStatus` | Decoded completion status |
| `implementation_commit` | `str \| None` | 40-char lowercase hex or None |
| `report_commit` | `str` | 40-char lowercase hex (always present) |
| `summary` | `str` | Non-empty result text |
| `warnings` | `tuple[str, ...]` | Warning strings (may be empty) |
| `stdout_sha256` | `str` | SHA-256 hex digest of raw stdout |

### 2.3 `DeliveryReceipt`

Frozen, slots, six-field dataclass — only for `COMPLETED` status:

| Field | Type | Description |
|-------|------|-------------|
| `identity` | `DispatchIdentity` | Audit identity |
| `provider` | `str` | Provider string |
| `model_id` | `str` | Model identifier |
| `implementation_commit` | `str` | 40-char lowercase hex |
| `report_commit` | `str` | 40-char lowercase hex |
| `stdout_sha256` | `str` | SHA-256 hex digest |

---

## 3. Entry Points

### 3.1 `decode_worker_result`

```python
def decode_worker_result(
    result: WorkerResult,
    provider_cli_version: str,
) -> WorkerOutput:
    ...
```

- `result` must be exactly `WorkerResult`
- `provider_cli_version` must be a non-empty string without whitespace
- Extracts provider, model, identity, stdout, and SHA from `result.dispatch_result`
- Does NOT read from PATH, env vars, filesystem, or CLI
- Does NOT modify `WorkerResult` or `DispatchResult`

### 3.2 `require_delivery_receipt`

```python
def require_delivery_receipt(
    output: WorkerOutput,
) -> DeliveryReceipt:
    ...
```

- Only allows `WorkerCompletionStatus.COMPLETED`
- Both commits must be present and valid
- `PARTIAL` / `BLOCKED` raise `WorkerOutputSchemaError`

---

## 4. Supported Provider / Version Matrix

| provider | version | Status |
|----------|---------|--------|
| `claude` | `2.1.214` | Current |
| `claudecode` | `2.1.214` | Current |
| `codex` | any | Unsupported — raises `WorkerOutputUnsupportedProviderError` |
| `Claude` | `2.1.214` | Unsupported — fail-closed |
| `claude-code` | `2.1.214` | Unsupported — fail-closed |
| `claude` | `2.1.213` | Unsupported — raises `WorkerOutputUnsupportedVersionError` |
| `claude` | `>=2.1.214` | Unsupported — fail-closed |

---

## 5. Decode Pipeline

1. **Type check** `result` as `WorkerResult`
2. **Validate** `provider_cli_version` (non-empty str, no whitespace)
3. **Provider gate** — only `claude` / `claudecode`; `codex` returns typed error
4. **Version gate** — only `2.1.214`
5. **SHA-256 integrity** — constant-time comparison of computed vs expected hash
6. **JSON parse** — strict UTF-8, no BOM, single object root, no NaN/Infinity, no trailing text, no duplicate keys
7. **Claude wrapper validation** — exact 20 keys; `type=="result"`, `subtype=="success"`, `is_error is False`, `api_error_status is None`, `result` is non-empty str; time fields are non-bool int >= 0; `usage` and `modelUsage` are objects; `permission_denials` is list
8. **Envelope parse** — Claude `result` string parsed as strict JSON
9. **Envelope validation** — exact 10 keys; `schema_version` matches `agentdesk.worker-output/v1`; identity match on all 4 fields; status is valid enum; commits are 40-char lowercase hex; `completed` requires `implementation_commit`; commits must differ; summary non-empty, no NUL; warnings strict validation
10. **Construct** `WorkerOutput`

---

## 6. Claude Wrapper (20 Keys)

Observed from three real CLI 2.1.214 captures:

```text
type, subtype, is_error, api_error_status, duration_ms, duration_api_ms,
ttft_ms, ttft_stream_ms, time_to_request_ms, num_turns, result, stop_reason,
session_id, total_cost_usd, usage, modelUsage, permission_denials,
terminal_reason, fast_mode_state, uuid
```

Critical success conditions:
- `type == "result"`
- `subtype == "success"`
- `is_error is False`
- `api_error_status is None`
- `result` is non-empty str

Dynamic `modelUsage` keys are allowed (not frozen to specific model IDs).

---

## 7. AgentDesk Worker Completion Envelope (10 Keys)

```text
schema_version, task_id, revision, attempt, dispatch_id, status,
implementation_commit, report_commit, summary, warnings
```

- `schema_version` must be `"agentdesk.worker-output/v1"`
- All four identity fields must match `DispatchIdentity` exactly
- `revision` / `attempt` are non-bool int >= 1
- `status` is one of: `completed`, `partial`, `blocked`
- `report_commit` is always 40-char lowercase hex
- `implementation_commit` is null or 40-char lowercase hex
- `completed` requires `implementation_commit`; commits must differ
- `summary` is non-empty str, no NUL
- `warnings` is `list[str]`; each item non-empty, no whitespace edges, no NUL/CR/LF, no duplicates

---

## 8. Exception Hierarchy

```text
WorkerOutputError (Exception)
├── WorkerOutputUnsupportedProviderError
├── WorkerOutputUnsupportedVersionError
├── WorkerOutputIntegrityError
├── WorkerOutputDecodeError
├── WorkerOutputSchemaError
└── WorkerOutputIdentityError
```

### 8.1 Error Message Safety

Exception messages must NOT contain:
- stdout / stderr bytes
- Claude result or summary content
- warning content
- task_id / dispatch_id
- commit SHAs
- session_id / uuid / cost
- workspace / paths
- prompt
- secrets

Messages MAY contain: field names, expected types, `type(x).__name__`.

---

## 9. Security Boundaries

The production module must NOT:
- Call subprocess, asyncio, open(), pathlib read, os.environ
- Access Git, network, Claude/Codex CLI
- Use retry, fallback decoder, auto-version detection
- Direct API calls

Module import must have zero stdout, zero stderr, zero side effects.

---

## 10. Relationship to Upstream / Downstream

| Interface | Relationship |
|-----------|-------------|
| `WorkerAdapter` (TC-13.9b) | Supplies `WorkerResult` as input — not modified |
| `DispatcherAgentGateway` (TC-13.7) | Supplies `DispatchIdentity` and `DispatchResult` — not modified |
| `WorkflowOrchestrator` (TC-13.18b) | Consumer — receives typed `WorkerOutput` / `DeliveryReceipt` |
| TC-13.18c | Consumer — uses `implementation_commit` and `report_commit` for `DELIVERY_SUBMITTED` |

---

## 11. Task-Card Split

| Card | Description | Status |
|------|-------------|--------|
| **TC-13.9c.1** | Claude 2.1.214 WorkerOutput decoder | Current |
| **TC-13.9c.2** | Codex WorkerOutput decoder | Target |
| **TC-13.9c** | Full provider output decoding (both Claude + Codex) | Target |

---

## 12. Explicit Non-Goals

- Codex / non-Claude decoding → TC-13.9c.2
- Version auto-detection, fallback decoding
- Git commit ancestry validation
- Direct model, API, CLl, or network calls
- Real CLI execution or fixture capture
- Cross-version Claude compatibility claims
