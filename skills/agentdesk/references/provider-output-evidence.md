# Provider Output Evidence — TC-13.9c Investigation

## Package Versions

| Provider | NPM Package | Version | Executable SHA-256 |
|---|---|---|---|
| Claude Code | `@anthropic-ai/claude-code` | 2.1.214 | `da26afacbc58d6f569d0921ff6825772220c734327f5f976c0c73bd0b7dd3cf8` |

## Fixture Evidence Summary

| Sample | Exit | stdout bytes | raw stdout SHA-256 | sanitized stdout SHA-256 |
|---|---|---|---|---|
| success-minimal | 0 | 1549 | `06ebbfe7656a202137316272e653352d9d09ee26067613842455737f8941a4de` | `d447f50c976d3b58fb3b1b9c80e82b42ce089d4f19f4f2e7e69ff775ef281a85` |
| success-unicode | 0 | 1575 | `d9ec017cd1834b02bdaf6a5913762db9b5a5955d09dad3423c8fe5a42356fb54` | `d0d97454dcbe02d43fa25cc702fee1cd84bed85c65119e1d7762861b74a610bd` |
| application-boundary | 0 | 1679 | `0422fdb0cf8b09f754d620bd74bbe1abb74fc7848bdc438ad08205e3a4007a80` | `c29a846620b9b9b438bd235d8852fb043d3964afd82f49cc1077de7cfb561921` |

All 3 captures sourced from real Claude CLI invocations via the production
DispatcherAgentGateway.  All stderr empty.  All stdout valid UTF-8, single
JSON object, no NUL, no ANSI escapes.

## Codex CLI Capture Attempts (No Usable Output)

Codex was invoked 3 times through the production Gateway:

| Attempt | Exit | stdout bytes | raw stdout SHA-256 |
|---|---|---|---|
| Sample 1 (success-minimal equivalent) | 1 | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| Sample 2 (success-unicode equivalent) | 1 | 0 | (same empty) |
| Sample 3 (application-boundary equivalent) | 1 | 0 | (same empty) |

All 3 attempts returned exit code 1 through the production
DispatcherAgentGateway.  The `CodexCliProvider.build_invocation()` argv was
rejected by the codex CLI binary.  stdout bytes = 0 for all 3.  The Gateway
does not expose raw stderr on non-zero exit, so stderr was not preserved.

**These 3 attempts produced:**
- Zero output fixture files
- Zero JSONL evidence
- Zero sanitized stdout
- No output schema data

**Codex evidence grade: F** — No usable output.  Codex decoder remains
blocked.  No new Codex calls were made during the remediation pass.

These 3 attempts are recorded here as investigation notes, not as fixture
provenance.  They are not described as "success captures", "JSONL fixtures",
"sanitized stdout", or "output schema evidence".

## Claude Output Structure

### Root type

`object` (single JSON document per invocation)

### Top-level fields (20, consistent across all 3 samples)

| Field | Type | Notes |
|---|---|---|
| `type` | `string` | `"result"` |
| `subtype` | `string` | `"success"` |
| `is_error` | `boolean` | `false` |
| `api_error_status` | `null` or `object` | `null` in all 3 samples |
| `duration_ms` | `number` | |
| `duration_api_ms` | `number` | |
| `ttft_ms` | `number` | Time-to-first-token |
| `ttft_stream_ms` | `number` | |
| `time_to_request_ms` | `number` | |
| `num_turns` | `number` | 1 in all 3 samples |
| `result` | `string` | **Final assistant text** — the primary candidate |
| `stop_reason` | `string` | `"end_turn"` |
| `session_id` | `string` | Redacted in fixtures |
| `total_cost_usd` | `number` or `null` | Redacted in fixtures |
| `usage` | `object` | Nested: `input_tokens`, `cache_*`, `output_tokens`, `server_tool_use`, `service_tier`, `cache_creation`, `inference_geo`, `iterations`, `speed` |
| `modelUsage` | `object` | Per-model: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`, `contextWindow`, `maxOutputTokens` |
| `permission_denials` | `array` | Empty `[]` in all 3 samples |
| `terminal_reason` | `string` | `"completed"` |
| `fast_mode_state` | `string` | `"off"` |
| `uuid` | `string` | Redacted in fixtures |

### Final text candidate

**`result`** — the string field at the root. Contains the assistant's final message text.
Present in all 3 samples; non-empty in all 3 samples.

### Consistency across samples

All 20 top-level keys are identical across all 3 Claude samples.
Field types are consistent.
`type` = `"result"`, `subtype` = `"success"`, `is_error` = `false` across all 3.

### Error/status fields

- `is_error`: `false` in all samples (success path only)
- `api_error_status`: `null` in all samples
- `subtype`: `"success"` — other observed values in `--output-format json` are not represented

### Extra fields beyond the 20

None observed. The 20-key set is stable across 3 samples but this is single-version evidence.

### Empty output behavior

Not observed — all 3 samples produced non-empty `result`.

### Non-JSON surrounding text

None observed. stdout is exactly one JSON document with no preceding/trailing text.

## Mock vs Real Output Differences

### Mock (test FakeAgentCliProvider)
- Fully configurable: any field, any value
- No real JSON structure
- Used for Gateway integration testing only

### Real (Claude CLI 2.1.214)
- Fixed 20-key JSON object
- `result` field carries final text
- Usage and modelUsage are deeply nested objects
- Session/request IDs and costs are present and must be redacted

## Evidence Grade Assessment

| Criterion | Claude | Codex |
|---|---|---|
| Real CLI calls | ✅ 3 calls, exit 0 | ❌ 3 calls, exit 1 |
| Valid fixture output | ✅ 3 JSON fixtures | ❌ No fixture output |
| Two distinct versions | ❌ Single version (2.1.214) | ❌ No evidence |
| Official serialization source | ❌ Not included | ❌ Not included |
| **Grade** | **B+** — single-version, 3-sample, real output, full provenance | **F** — no usable output |

**Overall: B+** for Claude alone.  Not sufficient to freeze a long-term
public contract (§13 freeze eligibility rules).  Codex evidence is absent,
preventing cross-provider schema comparison.

## Redaction Rules Applied

| # | Path | Kind | Reason |
|---|---|---|---|
| 1 | `$.session_id` | session_id | Session/conversation identifier |
| 2 | `$.uuid` | request_id | Request/dispatch UUID |
| 3 | `$.total_cost_usd` | cost | Account billing information |
| 4 | `$.modelUsage.*.costUSD` | cost | Per-model billing information |

Redactions preserve:
- JSON root type (object)
- All field names
- All field types (costUSD → null preserves numeric-or-null union)
- Array ordering
- Error/success structures

## Fixture Files (After Remediation)

```text
tests/fixtures/provider-output/
├── provenance.json
└── claude/
    └── 2.1.214/
        ├── success-minimal.json
        ├── success-unicode.json
        └── application-boundary.json
```

No codex/ directory exists.  No placeholder `.jsonl` files exist.
No `_tc139c_note` artifacts exist anywhere under fixtures/.

## Related Documents

- [Claude Code 2.1.214 Observed Output Shape](claude-output-observations-2.1.214.md) — detailed field-by-field analysis
- [ADR 001 — MAD × AgentDesk Integration](adr/001-mad-agentdesk-integration.md) — TC statuses

## Status

| Item | Status |
|---|---|
| Claude evidence collected | ✅ 3/3 real CLI captures |
| Claude output analysis | ✅ See [claude-output-observations-2.1.214.md](claude-output-observations-2.1.214.md) |
| Codex evidence collected | ❌ 3/3 Gateway failures (documented above, no fixture) |
| Codex fixture files | ❌ None — deleted during remediation |
| Decoder implemented | ❌ Not implemented (task card §13) |
| TC-13.9c status | **Target** (unchanged) |
| TC-13.10 status | **Target** (unchanged) |
| Production code modified | ❌ No changes |
| ADR status table modified | ❌ No changes |
