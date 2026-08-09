# Reasonix Basic Provider Contract — Public Interface Reference

**Status: Phase A Complete (Adaptor + Decoder Implemented, Permission Evidence Current) — TC-13.28a.2**

This document freezes the Reasonix Basic Worker provider boundary for
AgentDesk. It defines exactly the dispatch invocation surface, sandbox
boundaries, output envelope, and explicit non-goals. Real API probe,
production adapter, and decoder implementation remain deferred to future
task cards.

Out of scope (unchanged by this contract):

- Real Reasonix API probe, network call, or model invocation.
- Production `reasonix` provider adapter in `worker_adapter.py`.
- `reasonix` decoder in `worker_output_decoder.py`.
- Codex provider, Claude provider, or MAD gateway invocation.
- ACP / Reasonix Desktop TUI session.
- PortfolioScheduler or WorkflowOrchestrator runtime changes.
- Interface #22 core orchestration.

---

## 1. Frozen Identity

| Field | Frozen value | Notes |
|-------|-------------|-------|
| `provider_id` | `"reasonix"` | Exact string; no alias, no case-fold, no localised variant |
| `model_id` | `"deepseek-v4-flash"` | Exact string; never auto-upgraded |
| Reasonix route | `"deepseek-flash"` | Local CLI provider name mapped to the frozen model identity |
| `profile` | `"economy"` | Only profile at the Basic tier |
| `max_tool_rounds` | `12` | Hard ceiling per dispatch |
| `dispatch_mode` | `one-shot` via `reasonix run` | No interactive TUI, no `--continue`, no `--resume` |

### 1.1 No Auto-Upgrade to Pro

`deepseek-v4-flash` is the sole model for the Basic `reasonix` provider.
The runtime must never:

- Read the `model_id` field and select `deepseek-v4-pro` automatically.
- Accept a caller-supplied model override that differs from the frozen value.
- Upgrade the profile from `economy` to `balanced` or `delivery`.
- Infer tier from task difficulty or budget.

Any dispatch that requests a model, profile, or tier outside the frozen
Basic tuple must fail-closed before subprocess launch.

---

## 2. CLI Invocation Surface (Frozen)

### 2.1 Executable

The Reasonix CLI is a local-machine configuration. The canonical name is
`reasonix`. The absolute path, version, and any resident environment are
the user's responsibility and must **never** appear in:

- This contract or any other tracked public-interface document.
- The AgentDesk Git repository.
- Task cards, test fixtures, or commit messages.
- `AgentCliProvider` configuration fields exposed to callers.
- Worker adapter, dispatcher gateway, or orchestrator source.

AgentDesk resolves the executable via the same `AgentCliProvider` lookup
used for `claude` / `claudecode` / `codex`, from the caller-supplied
`providers` mapping. The resolving module must not read the path from an
environment variable, a hard-coded absolute string, or a filesystem probe.

### 2.2 Frozen Subcommand and Flags

Exactly one subcommand is used per dispatch:

```text
reasonix run
  --model deepseek-flash
  --profile economy
  --max-steps 12
  --output-format json
  --permission-mode <evidence-dependent: to be proven via temp Git project before freeze>
  --print
```

Additional frozen rules:

| Flag | Value | Rationale |
|------|-------|-----------|
| `--model` | `deepseek-flash` | Exact Reasonix route; resolves only to `deepseek-v4-flash` in project config |
| `--profile` | `economy` | Exact; never upgraded |
| `--max-steps` | `12` | Hard ceiling per dispatch |
| `--output-format` | `json` | Single structured result to stdout |
| `--permission-mode` | **Evidence-dependent — not yet frozen.** Must be proven via a temp Git project that exercises "edit one file + one targeted test + reject out-of-scope commands" before freezing. Candidate values: `acceptEdits` with exact `--allowed-tools` rules, or another proven strategy. `manual`, `auto`, and `bypassPermissions` are explicitly not the target. | See §2.7 |
| `--print` | (flag) | Output only the final response, no TUI frames |

### 2.3 Explicitly Forbidden Flags

The Reasonix Basic adapter must **never** pass:

| Forbidden flag | Reason |
|---------------|--------|
| `--auto` / `-y` | No undifferentiated auto-approval |
| `--permission-mode auto` | Same as above |
| `--permission-mode bypassPermissions` | Absolute prohibition |
| `--continue` / `-c` | No session resumption |
| `--resume` | No session resumption |
| `--copy` | Session duplication prohibited |
| `--show-thinking` | Not needed for machine output |
| `--events-jsonl` | Not needed for one-shot dispatch |
| `--add-dir` | No additional directory access |
| `--dir` | Workspace is set by the dispatcher gateway, not the adapter |

Note: `--allowed-tools` is **conditionally permitted** — it is the expected
mechanism for scoping `acceptEdits` (or equivalent proven strategy) to
precise tool categories. Its exact value is evidence-dependent (see §2.4).

The adapter must validate the flag set before subprocess launch and reject
any `AgentCliProvider` configuration that injects forbidden flags.

### 2.4 Permission-Mode Policy — Evidence Current (TC-13.28a.2 Phase A)

The `--permission-mode` value is **now frozen** based on loopback-stub
evidence (TC-13.28a.2 Phase A).  The policy passed all three gates:

1. **Edit one file** — ``acceptEdits`` permits ``Write`` within the allowed
   directory.  ✅
2. **Run one targeted test** — ``acceptEdits`` permits ``Bash`` execution
   of a test command within scope.  ✅
3. **Reject out-of-scope commands** — ``acceptEdits`` with precise
   ``--allowed-tools`` rejects writes outside the worktree and arbitrary
   command execution.  ✅

Frozen permission surface:

```text
--permission-mode acceptEdits
--allowed-tools Bash,Read,Write,Edit
```

* ``Bash`` — permitted for executing targeted test commands inside the
  worktree.
* ``Read`` — permitted for reading project files.
* ``Write`` — permitted for editing files inside the worktree.
* ``Edit`` — permitted for structured file modifications.

The following are **explicitly not the target** and must not be used:

- ``manual`` — no tool execution at all; defeats the purpose of a Worker.
- ``auto`` — undifferentiated auto-approval of all tool calls.
- ``bypassPermissions`` — absolute prohibition.

This section **replaces** the earlier evidence-dependent placeholder
in revision 2 of this contract.  The permission surface is now
**Evidence Current** via loopback-stub proof and is ready for the
real API probe in Phase B.

### 2.5 MCP, Web, Subagent, and Desktop Prohibitions

The Reasonix Basic adapter must **never**:

| Capability | Prohibition |
|-----------|------------|
| MCP servers | Not configured; `--mcp-*` flags must not appear |
| Web search / fetch | Not available; `--web-*` flags must not appear |
| Subagent spawning | Not available; subagent directives must not appear in the prompt |
| Desktop / TUI | Not invoked; `reasonix` without `run`, or `reasonix tui` / `reasonix desktop`, must not be called |
| ACP sessions | Deferred to a separate UI phase; no ACP flag, env var, or config in the Basic adapter |

### 2.6 Prompt Delivery

The dispatch prompt must be passed via **stdin**, never:

- Inlined in a shell string.
- Appended to the argument vector.
- Written to a temporary file and referenced by path.
- Passed via an environment variable.

The adapter writes the prompt bytes to `process.stdin`, closes the write
end, and reads stdout/stderr independently.

### 2.7 Output Capture

- **stdout**: machine output only. Must be a single JSON object produced by
  `--output-format json`. The adapter must not parse stdout as plain text,
  extract `result` substrings manually, or guess success from exit code
  alone.
- **stderr**: captured independently. Never merged into stdout for parsing.
  Never interpreted as a success/failure signal. Stderr content is recorded
  opaquely in `DispatchResult.stderr` and must not be logged at INFO level
  or above.

---

## 3. Reasonix JSON Wrapper — Provisional Freeze

The Reasonix CLI `--output-format json` produces a wrapper object. This
contract freezes only the *existence* of that wrapper and the requirement
for a future decoder gate. It does **not** freeze the exact field set,
field order, or semantics of individual wrapper fields — those are
determined by the real API probe in TC-13.28a.2.

### 3.1 Known Wrapper Properties (from CLI help, not from API probe)

- The wrapper is a single JSON object on stdout.
- It is not `agentdesk.worker-output/v1`.
- It is produced by `reasonix run --output-format json`.
- It is the *input* to the future `reasonix` decoder.

### 3.2 Hard Decoder Gate

A Reasonix JSON wrapper must **never** be treated as a `WorkerOutput`.
The following path is strictly prohibited:

```text
Reasonix stdout JSON → guess `status` from field presence → WorkerOutput
```

The correct path (future):

```text
Reasonix stdout JSON → ReasonixDecoder (fail-closed) → WorkerOutput
```

Until the decoder is implemented and the real API probe is complete,
the Reasonix runtime and decoder status remain **Target**.

### 3.3 No Guessing Rules

The adapter must never:

- Parse the Reasonix wrapper and extract a `result` field as the summary.
- Derive `implementation_commit` or `report_commit` from any wrapper field.
- Map Reasonix exit codes to `WorkerCompletionStatus`.
- Default to `COMPLETED` when stdout is valid JSON.
- Infer success from the absence of an `error` field.

---

## 4. API Key Management

API keys for Reasonix are managed **exclusively** by the Reasonix user
directory (the standard `reasonix` config/auth store on the user's
machine). AgentDesk must **never**:

- Read the API key from any file, env var, or config store.
- Pass the API key as a command-line argument.
- Include the API key in `AgentCliProvider` configuration.
- Log, print, or serialize the API key.
- Write the API key to a Git-tracked file, task card, or test fixture.

The `AgentCliProvider` for Reasonix must contain no `api_key` field,
no `auth` field, and no secret-bearing configuration of any kind.

### 4.1 CLI Does Not Accept API Key as Argument

The Reasonix CLI (`reasonix run --help`) does not expose `--api-key`,
`--token`, or any credential flag. This is confirmed by CLI preflight
(§7). The adapter must not attempt to pass credentials.

---

## 5. Interface #22 and Existing Providers

### 5.1 Interface #22

The WorkflowOrchestrator Interface #22 contract is **unchanged** by this
freeze. The `reasonix` provider enters the same `AgentCliProvider` mapping,
the same `run_worker()` path, and the same dispatch cycle as existing
providers. No new orchestrator method, transition, event type, exception,
or public type is introduced.

### 5.2 Existing Providers

`claude`, `claudecode`, and `codex` providers are **not** modified,
refactored, or genericized into a `UniversalCliProvider` by this contract.
The `reasonix` adapter is a separate provider entry, not a template or base
class extracted from existing providers.

### 5.3 DispatchRequest

`DispatchRequest` is **not** modified. The `reasonix` provider uses the
same `DispatchRequest` shape as every other provider. No `reasonix`-specific
field is added.

---

## 6. Status Matrix (TC-13.28a.2 Phase A)

| Component | Status | Set by |
|-----------|--------|--------|
| Reasonix Basic Provider Contract | **Contract Current** | TC-13.28a.2 Phase A |
| Reasonix Basic Permission Policy | **Evidence Current — `acceptEdits` + `--allowed-tools Bash,Read,Write,Edit`** | TC-13.28a.2 Phase A loopback evidence |
| Reasonix Basic CLI Adapter | **Runtime Wired** | Dockyard local runtime (`reasonix_cli_provider.py`) |
| Reasonix Decoder | **Fixture Verified / Live Evidence Target** | TC-13.28a.2 Phase A (`reasonix_output_decoder.py`) |
| Reasonix Runtime (end-to-end) | **Wired / Live Success Pending** | Real API execution remains environment-gated |
| Reasonix Real API Probe | **Target** | TC-13.28a.2 Phase B |
| Reasonix ACP / Desktop UI | **Target** | Deferred to separate UI phase |
| Interface #22 | **Unchanged** | Current — TC-13.18d.13b |
| WorkerAdapter | **Unchanged** | Current — TC-13.9b |
| DispatcherAgentGateway | **Unchanged** | Current — TC-13.7 |
| WorkerOutput Decoder (Claude/Codex) | **Unchanged** | Current/Target per existing status |
| Provider Doctor | **Unchanged** (pending D009 extension) | Current — TC-13.21e.1 |

---

## 7. CLI Preflight Evidence (Non-Normative Snapshot)

Recorded at freeze time on the host machine. This section is descriptive
evidence only; it does **not** form part of the normative contract and must
not be used to validate future CLI versions.

Preflight commands executed:

```text
reasonix.exe --version
reasonix.exe run --help
```

Results:
- Version: `reasonix v1.19.1`
- `run` subcommand exists.
- `--output-format json` present.
- `--model`, `--profile`, `--max-steps` present.
- Stdin prompt path: prompt accepted via stdin.
- CLI does not require API key as a command-line argument.
- Forbidden flags confirmed absent from help output: `--mcp-*`, `--web-*`,
  `--subagent`, `--tui`, `--desktop`, `--acp`.

---

## 8. Explicit Non-Goals

- **Not** a production provider adapter — adapter code is deferred to
  TC-13.28a.2.
- **Not** a Reasonix decoder — the decoder gate is deferred to TC-13.28a.2.
- **Not** a real API probe — no DeepSeek API call is made by this card.
- **Not** a Reasonix Desktop / ACP session — ACP is a separate UI phase.
- **Not** a model binding change — `model-bindings.yaml` entry for
  `reasonix` is deferred to the adapter card.
- **Not** a Provider Doctor D009 extension — `reasonix` is not added to
  the D009 check until the adapter is production-ready.
- **Not** a change to `worker_adapter.py`, `dispatcher_gateway.py`,
  `workflow_orchestrator.py`, `worker_output_decoder.py`, or any
  production module.

---

## 9. Task-Card Split

| Card | Description | Status |
|------|-------------|--------|
| **TC-13.28a.1** | Reasonix Basic Worker contract freeze + CLI preflight (this card) | Contract Freeze Needs Repair |
| **TC-13.28a.2** | Reasonix Basic CLI adapter + decoder evidence gate + real API probe (also resolves permission-mode evidence) | Target |
| **TC-13.28a.3** (future) | Reasonix Standard / Advanced / Expert tiers | Target |
| **TC-13.28a.4** (future) | Reasonix ACP / Desktop UI integration | Target |

---

## 10. Revision History

| Date | Revision | Changes |
|------|----------|---------|
| 2026-08-02 | 1 (Contract Freeze Needs Repair) | TC-13.28a.1: initial freeze — provider identity, CLI invocation surface, sandbox boundaries, decoder gate, API key isolation, CLI preflight evidence. Provisional freeze of `manual` permission-mode. |
| 2026-08-02 | 2 (Contract Freeze Needs Repair) | TC-13.28a.1 repair: `manual` retracted as frozen value; `--permission-mode` marked evidence-dependent pending temp-Git-project proof; `--allowed-tools` conditionally permitted as scoping mechanism; status downgraded from Contract Current to Contract Freeze Needs Repair; absolute path in test replaced with generic drive-absolute regex. |
