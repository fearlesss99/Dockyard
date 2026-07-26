# Claude Code 2.1.214 Observed Output Shape

## Version Scope

```
observed_version == 2.1.214
```

This document reports only what was observed in three real CLI captures
of `@anthropic-ai/claude-code@2.1.214` using `--output-format json`.
It is **not** a frozen schema, public contract, upstream guarantee,
or compatibility statement for any other version.

It must **not** be described as:

- `agentdesk.claude-output/v1`
- frozen contract
- upstream guarantee
- compatible with `>=2.1.214`
- stable across Claude versions

## Evidence Source

| Fixture | Sample ID | raw stdout SHA-256 |
|---|---|---|
| `success-minimal.json` | success-minimal | `06ebbfe7656a202137316272e653352d9d09ee26067613842455737f8941a4de` |
| `success-unicode.json` | success-unicode | `d9ec017cd1834b02bdaf6a5913762db9b5a5955d09dad3423c8fe5a42356fb54` |
| `application-boundary.json` | application-boundary | `0422fdb0cf8b09f754d620bd74bbe1abb74fc7848bdc438ad08205e3a4007a80` |

All 3 captures: real CLI invocations via production DispatcherAgentGateway.
exit code 0, stderr empty, stdout valid UTF-8 single-object JSON.

## Top-Level Field Observation Matrix

**20 fields observed in 3/3 captures.  Key set identical across all 3.**

| # | Field | Observed | Type(s) | Same type | Null seen | Note |
|---|---|---|---|---|---|---|
| 1 | `type` | 3/3 | `string` | yes | no | `"result"` in all 3 |
| 2 | `subtype` | 3/3 | `string` | yes | no | `"success"` in all 3 |
| 3 | `is_error` | 3/3 | `boolean` | yes | no | `false` in all 3 |
| 4 | `api_error_status` | 3/3 | `null` | yes | yes | `null` in all 3 |
| 5 | `duration_ms` | 3/3 | `integer` | yes | no | runtime metadata |
| 6 | `duration_api_ms` | 3/3 | `integer` | yes | no | runtime metadata |
| 7 | `ttft_ms` | 3/3 | `integer` | yes | no | runtime metadata |
| 8 | `ttft_stream_ms` | 3/3 | `integer` | yes | no | runtime metadata |
| 9 | `time_to_request_ms` | 3/3 | `integer` | yes | no | runtime metadata |
| 10 | `num_turns` | 3/3 | `integer` | yes | no | `1` in all 3 |
| 11 | `result` | 3/3 | `string` | yes | no | **final-text candidate** |
| 12 | `stop_reason` | 3/3 | `string` | yes | no | `"end_turn"` in all 3 |
| 13 | `session_id` | 3/3 | `string` | yes | no | redacted |
| 14 | `total_cost_usd` | 3/3 | `null` | yes | yes | redacted (was `number`) |
| 15 | `usage` | 3/3 | `object` | yes | no | runtime metadata |
| 16 | `modelUsage` | 3/3 | `object` | yes | no | runtime metadata |
| 17 | `permission_denials` | 3/3 | `array` | yes | no | `[]` in all 3 |
| 18 | `terminal_reason` | 3/3 | `string` | yes | no | `"completed"` in all 3 |
| 19 | `fast_mode_state` | 3/3 | `string` | yes | no | `"off"` in all 3 |
| 20 | `uuid` | 3/3 | `string` | yes | no | redacted |

**Classification key:**

| Field | Category |
|---|---|
| `result` | final-text candidate |
| `type`, `subtype`, `is_error`, `api_error_status` | success/error discrimination |
| `duration_ms`, `duration_api_ms`, `ttft_ms`, `ttft_stream_ms`, `time_to_request_ms`, `num_turns`, `stop_reason`, `terminal_reason`, `fast_mode_state`, `usage`, `modelUsage` | runtime metadata |
| `permission_denials` | access control state |
| `session_id`, `uuid`, `total_cost_usd` | redacted |
| `modelUsage.*.costUSD` | redacted |

## Nested Structure Analysis

### `usage` (object, 10 sub-keys; intersection == union == 10)

| Sub-field | Type | Null in any | Notes |
|---|---|---|---|
| `input_tokens` | `integer` | no | |
| `cache_creation_input_tokens` | `integer` | no | |
| `cache_read_input_tokens` | `integer` | no | |
| `output_tokens` | `integer` | no | |
| `server_tool_use` | `object` | no | see below |
| `service_tier` | `string` | no | `"standard"` in all 3 |
| `cache_creation` | `object` | no | see below |
| `inference_geo` | `string` | no | `""` in all 3 |
| `iterations` | `array` | no | `[]` (empty) in all 3 |
| `speed` | `string` | no | `"standard"` in all 3 |

### `usage.server_tool_use` (object, 2 sub-keys; intersection == union)

| Sub-field | Type |
|---|---|
| `web_search_requests` | `integer` (`0` in all 3) |
| `web_fetch_requests` | `integer` (`0` in all 3) |

### `usage.cache_creation` (object, 2 sub-keys; intersection == union)

| Sub-field | Type |
|---|---|
| `ephemeral_1h_input_tokens` | `integer` (`0` in all 3) |
| `ephemeral_5m_input_tokens` | `integer` (`0` in all 3) |

### `usage.iterations` (array)

`[]` (empty) in all 3 captures.  No iteration entries observed.

### `modelUsage` (object, model keys are dynamic)

**Model keys observed across 3 captures:**
`claude-haiku-4-5`, `claude-sonnet-4-5-20250914`

Model names are **dynamic keys** — must not be frozen in any contract.
The set of models may differ per invocation (routing, fallback, model selection).

Each model entry has **8 sub-keys** (intersection == union == 8):

| Sub-field | Type | Null in any |
|---|---|---|
| `inputTokens` | `integer` | no |
| `outputTokens` | `integer` | no |
| `cacheReadInputTokens` | `integer` | no |
| `cacheCreationInputTokens` | `integer` | no |
| `webSearchRequests` | `integer` | no |
| `costUSD` | `null` | yes (redacted) |
| `contextWindow` | `integer` | no |
| `maxOutputTokens` | `integer` | no |

### `permission_denials` (array)

`[]` (empty) in all 3 captures.  Element type: unknown outside these captures.

## `result` Field: Final-Text Candidate Analysis

| Fixture | `result` content | Bytes | Has `\n` | Contains expected marker |
|---|---|---|---|---|
| success-minimal | `EVIDENCE_OK` | 11 | no | `EVIDENCE_OK` |
| success-unicode | `第一行：证据\nSecond line: Ω-42` | 24 | yes | CJK characters, `Ω` (U+03A9) |
| application-boundary | `I cannot return an empty final answer…\n\nEVIDENCE_END` | 140 | yes | `EVIDENCE_END` |

Observations:

- `result` is present in 3/3 captures
- Type is `string` in 3/3 captures
- Non-empty in 3/3 captures
- Chinese characters preserved: `第一行：证据`
- Greek character preserved: `Ω`
- Newlines preserved within the string
- Unicode intact (UTF-8 valid, no replacement characters)
- Not redacted
- Does not contain session ID, UUID, or cost data

**Conclusion:**

```text
$.result is the final-text candidate in all three observed successful
Claude Code 2.1.214 captures.
```

This is an **observation**, not a cross-version guarantee.

## Success Discrimination Observations

All 3 captures share:

```text
type == "result"
subtype == "success"
is_error == false
api_error_status == null
```

Key caveats:

| What was observed | What was NOT observed |
|---|---|
| `subtype == "success"` | Any other `subtype` value |
| `is_error == false` | `is_error == true` |
| `api_error_status == null` | `api_error_status` as an object |
| `exit_code == 0` | Non-zero exit with `is_error` |
| `type == "result"` | Any other `type` value |

Only the **success path** was captured.  No conclusions can be drawn about:
- Error structure
- Permission denial structure
- Tool-use structure
- Exit 0 with `is_error == true`
- Exit non-zero with JSON output

## Non-Coverage Matrix

Every item below is **not observed — no conclusion**.

| # | Gap | Status |
|---|---|---|
| 1 | Error path (`is_error == true`) | not observed |
| 2 | Non-success `subtype` | not observed |
| 3 | `api_error_status` as non-null structure | not observed |
| 4 | `type` other than `"result"` | not observed |
| 5 | Permission denial path | not observed |
| 6 | Tool-use path (`server_tool_use.* > 0`, `iterations` non-empty) | not observed |
| 7 | Multiple turns (`num_turns > 1`) | not observed |
| 8 | Empty `result` string | not observed |
| 9 | Missing `result` key | not observed |
| 10 | `result` as non-string type | not observed |
| 11 | Malformed JSON output | not observed |
| 12 | Non-JSON surrounding text in stdout | not observed |
| 13 | Additional top-level fields beyond 20 | not observed |
| 14 | Second Claude version | not observed |
| 15 | Version downgrade behavior | not observed |
| 16 | Version upgrade behavior | not observed |
| 17 | Oversized output (>10MB) | not observed |
| 18 | Cancellation or timeout path | not observed |
| 19 | Upstream schema/version identifier field | not observed |
| 20 | Non-standard service tier | not observed |
| 21 | Fast mode enabled | not observed |
| 22 | Streamed (non-JSON) output | not observed |
| 23 | Shell-injected output mixed with JSON | not observed |
| 24 | modelUsage with model routing changes | not observed |

## Evidence Grade Assessment

| Criterion | Status |
|---|---|
| Real CLI captures | ✅ 3/3 exit 0 |
| Same-version structure consistency | ✅ full key intersection |
| Final-text field reliability | ✅ `$.result` holds text in all 3 |
| Unicode + newline integrity | ✅ preserved |
| Error-path coverage | ❌ none |
| Two distinct versions | ❌ single version |
| Official serialization documentation | ❌ not included |
| **Grade** | **B+** |

## Decoder Feasibility Assessment

### Option A: Freeze long-term public decoder now

**Verdict: NOT ALLOWED.**

Only single-version success-path evidence exists.  No error, tool-use,
permission-denial, multi-turn, or cross-version data.  Freezing would
create an unvalidated compatibility obligation.

### Option B: Implement a `2.1.214` experimental success-result parser

**Assessment:**

| Factor | Finding |
|---|---|
| No runtime CLI version field in output | The output JSON carries no `version` key — detection would be out-of-band |
| Provider API carries no version | `ClaudeCodeProvider` and `AgentCliProvider` Protocol expose no `version` field |
| No error-path fixtures | A parser that only handles `subtype: "success"` would be incomplete |
| Risk of unvalidated compatibility promise | Any importable decoder module may be read as a schema commitment |
| Fail-closed possible | A private, non-default, `_experimental` module guarded by explicit opt-in is feasible |

**Feasibility:** An experimental, private, fail-closed parser restricted to
`observed_version == 2.1.214` and only the success path is technically
possible with current evidence.  However, the absence of error-path
coverage and runtime version signaling means it cannot be a default code
path or public API.

**This task does not implement such a parser.**

### Option C: Wait for evidence strengthening

Minimum strengthening conditions:

1. Success captures from a **second distinct Claude version** with matching structure
2. At least one **real error-path capture** (`is_error == true` or non-zero exit with JSON)
3. At least one **permission-denial or tool-use capture**
4. Full provenance (executable SHA, package version) for both versions
5. Ideally: official serialization source or formal documentation

## Codex Status

Codex CLI evidence: **grade F** — no usable output from 3 Gateway attempts.
Not in scope for this analysis.  See `provider-output-evidence.md`
Section "Codex CLI Capture Attempts (No Usable Output)".

## Related Documents

- [Provider Output Evidence](provider-output-evidence.md) — full provenance and capture metadata
- [ADR 001 — MAD × AgentDesk Integration](../adr/001-mad-agentdesk-integration.md) — TC statuses
- [tests/test_provider_output_evidence.py](../../../tests/test_provider_output_evidence.py) — fixture integrity
- [tests/test_claude_output_observations.py](../../../tests/test_claude_output_observations.py) — this document's test assertions
