# AgentDesk RateLimit Service — Frozen Contract (Current — TC-13.14b)

Interface #19 frozen contract.  TC-13.14a freezes the Provider-neutral
RateLimit types, decision semantics, and exception hierarchy.  TC-13.14b
delivers the production `rate_limit.py` module.  Provider detection will
ship under TC-13.14c.

## Status

**Current** as of TC-13.14b.  The production `rate_limit.py` module now
implements this frozen contract.  Provider detection remains
evidence-dependent Target.

The ADR contract is in `001-mad-agentdesk-integration.md` §2.20;
this document expands the per-interface details.

---

## 1. Purpose

`RateLimitService` is a **deterministic, stateless, pure-function policy
evaluator** that consumes a set of typed rate-limit signals and produces a
typed decision: allow, wait, or fail-closed.  It owns zero state, zero
I/O, zero subprocess execution, and zero network access.

The RateLimit contract is split into two layers:

```text
Provider-specific detector (TC-13.14c — evidence-dependent Target)
        ↓ RateLimitSignal
Provider-neutral RateLimit policy (TC-13.14b — Runtime Target)
        ↓ RateLimitDecision
WorkflowOrchestrator (future wiring)
```

This contract freezes the **Provider-neutral** layer only.  Provider
detection is explicitly out of scope and must not be inferred.

---

## 2. Architecture

```text
RateLimitCheckRequest(provider, scope, now, units_requested, signals)
        ↓
RateLimitService.evaluate()
        ↓
RateLimitDecision(action, wait_seconds, reason)
```

### 2.1 Hard Boundaries

The RateLimit service must **never**:

| Forbidden action | Correct alternative |
|-----------------|-------------------|
| Read or parse stdout, stderr, or exit codes | Provider detector (TC-13.14c) produces typed signals |
| Read or parse HTTP headers or response bodies | Provider detector (TC-13.14c) produces typed signals |
| Call `datetime.now()`, `time.time()`, or `monotonic` | `now` is caller-supplied in `RateLimitCheckRequest` |
| Maintain internal token bucket, counter, or state | Pure function — all state arrives via `signals` |
| Store state to disk, database, or network | Stateless evaluation |
| Implement `record()` or `consume()` methods | Future TC-13.14b adds stateful policy |
| Start subprocess, thread, timer, or background task | Pure function |
| Access API keys, env vars, or credentials | Not in scope |
| Access filesystem, Git, or network | Not in scope |
| Retry, backoff, or loop | Orchestrator owns retry decisions |
| Guess or infer provider-specific 429 formats | Evidence-dependent TC-13.14c |

---

## 3. Public API — Exactly 12 Symbols

```python
__all__ = [
    "RateLimitSignal",
    "RateLimitScope",
    "RateLimitSignalSource",
    "RateLimitCheckRequest",
    "RateLimitDecision",
    "RateLimitAction",
    "RateLimitReason",
    "RateLimitService",
    "RateLimitError",
    "RateLimitInputError",
    "RateLimitStateError",
    "RateLimitSecurityError",
]
```

No other public names.  No internal helpers are exposed.  No constants,
mappings, or utility functions are part of the public API.

### 3.1 `RateLimitScope` — Strict Four-Value Enum

```python
class RateLimitScope(str, Enum):
    """What kind of limit the signal refers to."""
    REQUEST = "request"
    TOKEN = "token"
    CONCURRENCY = "concurrency"
    UNKNOWN = "unknown"
```

Requirements:

- `str, Enum` — no case-folding, no aliases, no unknown fallback
- Exactly 4 values: `REQUEST`, `TOKEN`, `CONCURRENCY`, `UNKNOWN`
- `UNKNOWN` must be used when the provider does not distinguish the limit
  type; it must not be used as a lazy default when the type is known

### 3.2 `RateLimitSignalSource` — Strict Three-Value Enum

```python
class RateLimitSignalSource(str, Enum):
    """Where the rate-limit signal originated."""
    PROVIDER_429 = "provider_429"
    BUDGET_THROTTLE = "budget_throttle"
    MANUAL = "manual"
```

Requirements:

- `str, Enum` — no case-folding, no aliases, no unknown fallback
- Exactly 3 values: `PROVIDER_429`, `BUDGET_THROTTLE`, `MANUAL`
- `PROVIDER_429` — signal from a provider HTTP 429 / rate-limit response
  (typed by TC-13.14c; the raw response is never stored)
- `BUDGET_THROTTLE` — signal from AgentDesk context budget policy
- `MANUAL` — signal injected by an operator or test

### 3.3 `RateLimitSignal` — Exactly 8 Fields

```python
@dataclass(frozen=True, slots=True)
class RateLimitSignal:
    """A single, immutable, Provider-neutral rate-limit observation."""
    provider: str
    scope: RateLimitScope
    observed_at: datetime
    retry_after_seconds: int | None
    reset_at: datetime | None
    limit: int | None
    remaining: int | None
    source: RateLimitSignalSource
```

Requirements:

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state
- `provider` — non-empty `str`, no leading/trailing whitespace, no NUL/CR/LF
- `scope` — must be `RateLimitScope` instance
- `observed_at` — must be timezone-aware UTC `datetime`
- `retry_after_seconds` — non-negative `int` or `None`; `0` means "no
  delay" (not the same as `None` which means "unknown")
- `reset_at` — timezone-aware UTC `datetime` or `None`; when the limit
  window resets
- `limit` — non-negative `int` or `None`; the maximum allowed in the
  current window
- `remaining` — non-negative `int` or `None`; the remaining allowance
- `source` — must be `RateLimitSignalSource` instance
- If both `limit` and `remaining` are present, `remaining` must be `<= limit`
- If `reset_at` is present, it must be `>= observed_at`
- **Must not** store raw error text, response body, header mapping,
  stack trace, or any untyped data

### 3.4 `RateLimitCheckRequest` — Exactly 5 Fields

```python
@dataclass(frozen=True, slots=True)
class RateLimitCheckRequest:
    """Immutable input for a single rate-limit evaluation."""
    provider: str
    scope: RateLimitScope
    now: datetime
    units_requested: int
    signals: tuple[RateLimitSignal, ...]
```

Requirements:

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state
- `provider` — non-empty `str`, no leading/trailing whitespace, no NUL/CR/LF
- `scope` — must be `RateLimitScope` instance; controls which signals
  are considered (see §3.9 Step 2)
- `now` — must be timezone-aware UTC `datetime`; the service must
  **never** call `datetime.now()` or `time.time()` internally
- `units_requested` — positive `int` (not `bool`), `>= 1`; the number of
  units the caller intends to consume
- `signals` — `tuple[RateLimitSignal, ...]`; may be empty; must be
  `tuple`, not `list`, `dict`, `set`, or `Any`

### 3.5 `RateLimitAction` — Strict Three-Value Enum

```python
class RateLimitAction(str, Enum):
    """What the caller should do."""
    ALLOW = "allow"
    WAIT = "wait"
    FAIL_CLOSED = "fail_closed"
```

Requirements:

- `str, Enum` — no case-folding, no aliases, no unknown fallback
- Exactly 3 values: `ALLOW`, `WAIT`, `FAIL_CLOSED`
- `ALLOW` — proceed with the operation
- `WAIT` — delay the operation; `wait_seconds` indicates how long
- `FAIL_CLOSED` — do not proceed; no retry is possible in the current
  window

### 3.6 `RateLimitReason` — Strict Five-Value Enum

```python
class RateLimitReason(str, Enum):
    """Typed reason for the decision — no arbitrary user strings."""
    NO_SIGNALS = "no_signals"
    WITHIN_LIMIT = "within_limit"
    RETRY_AFTER = "retry_after"
    INSUFFICIENT = "insufficient"
    EXHAUSTED = "exhausted"
```

Requirements:

- `str, Enum` — no case-folding, no aliases, no unknown fallback
- Exactly 5 values
- `NO_SIGNALS` — no matching signals after filtering; default to `ALLOW`
- `WITHIN_LIMIT` — matching signals present, remaining >= units; `ALLOW`
- `RETRY_AFTER` — at least one effective retry-after exists; `WAIT`
- `INSUFFICIENT` — no effective retry-after, but effective reset-at
  exists; `WAIT`
- `EXHAUSTED` — remaining == 0, or insufficient without wait source,
  or all informational fields absent; `FAIL_CLOSED`

### 3.7 `RateLimitDecision` — Exactly 3 Fields

```python
@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Immutable output of a single rate-limit evaluation."""
    action: RateLimitAction
    wait_seconds: int
    reason: RateLimitReason
```

Requirements:

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state
- `action` — must be `RateLimitAction` instance
- `wait_seconds` — non-negative `int` (not `bool`); `0` when `ALLOW`
  or `FAIL_CLOSED`; positive when `WAIT`
- `reason` — must be `RateLimitReason` instance; no arbitrary `str`
- `action` and `reason` must be consistent:
  - `ALLOW` → `NO_SIGNALS` or `WITHIN_LIMIT`
  - `WAIT` → `RETRY_AFTER` or `INSUFFICIENT`
  - `FAIL_CLOSED` → `EXHAUSTED`

### 3.8 `RateLimitService` — Stateless Policy Evaluator

```python
class RateLimitService:
    """Deterministic, stateless, pure-function rate-limit policy evaluator."""

    def evaluate(
        self,
        request: RateLimitCheckRequest,
    ) -> RateLimitDecision:
        ...
```

Requirements:

- **Deterministic** — same `request` → identical `RateLimitDecision`
- **Stateless** — no internal state, no `record()`, no `consume()`,
  no token bucket, no file storage, no background timer
- **Pure** — no side effects: no file writes, no network, no subprocess,
  no clock calls
- Accepts exactly one argument of type `RateLimitCheckRequest`
- Returns exactly `RateLimitDecision`
- Must not call `datetime.now()`, `time.time()`, or `monotonic`
- Must not read or parse provider stdout, stderr, exit codes, HTTP
  headers, or response bodies

### 3.9 Evaluation Semantics

Given `RateLimitCheckRequest(provider, scope, now, units_requested, signals)`:

#### Step 1 — Input validation

- `units_requested` must be a positive `int` `>= 1` (not `bool`, not `0`);
  otherwise `RateLimitInputError`
- `scope` must be a `RateLimitScope` instance; otherwise `RateLimitInputError`

#### Step 2 — Provider and scope filter

- Retain only signals whose `provider` matches `request.provider` exactly.
- Then apply scope filtering:
  - If `request.scope != UNKNOWN`: accept signals where
    `signal.scope == request.scope` **or** `signal.scope == UNKNOWN`.
    Reject signals with a different specific scope.
  - If `request.scope == UNKNOWN`: accept all scopes for the matching
    provider.
- If no matching signals remain → `ALLOW(wait_seconds=0, reason=NO_SIGNALS)`

#### Step 3 — Future observation rejection

- If any matching signal has `observed_at > request.now` →
  `RateLimitStateError` (future observation)

#### Step 4 — Per-signal normalization

For each matching signal, compute:

**retry_wait** (if `retry_after_seconds` is not `None`):

```python
retry_wait = max(
    0,
    ceil(
        retry_after_seconds
        - (request.now - observed_at).total_seconds()
    ),
)
```

**reset_wait** (if `reset_at` is not `None`):

```python
reset_wait = max(
    0,
    ceil((reset_at - request.now).total_seconds()),
)
```

**effective_wait**:

```python
effective_wait = max(retry_wait, reset_wait)
```

`ceil` uses `math.ceil`.  `effective_wait > 0` means the signal
produces a **WAIT candidate**.

**Signal expiry rule**: If a signal has explicit time information
(`retry_after_seconds` is not `None` or `reset_at` is not `None`) and
**all** of its time information has expired (`effective_wait == 0`),
the signal is **expired**.  Expired signals are excluded from further
remaining/exhausted judgments.

**Per-signal classification** (for non-expired signals):

| Candidate | Condition |
|-----------|-----------|
| `WAIT` | `effective_wait > 0` |
| `FAIL` | `remaining == 0`, or `remaining is None`, or `0 < remaining < units_requested` |
| `ALLOW` | `remaining >= units_requested` |

Note: `remaining is None` produces a `FAIL` candidate (no basis for an
optimistic decision).  `0 < remaining < units_requested` produces a
`FAIL` candidate (insufficient without a separate time source — the
effective_wait is already accounted for in the WAIT/FAIL classification).

#### Step 5 — Aggregate candidates

The aggregation follows a deterministic priority:

1. **Any WAIT candidate exists** → `WAIT`
   - `wait_seconds` = max `effective_wait` across **all** candidates
   - If any candidate has an effective retry-after (`retry_wait > 0`):
     `reason = RETRY_AFTER`
   - Otherwise (only reset-at provides the wait): `reason = INSUFFICIENT`

2. **No WAIT, but any FAIL candidate exists** → `FAIL_CLOSED`
   - `wait_seconds = 0`, `reason = EXHAUSTED`

3. **No WAIT/FAIL, only ALLOW candidates** → `ALLOW`
   - `wait_seconds = 0`, `reason = WITHIN_LIMIT`

4. **All matching signals expired** → `ALLOW`
   - `wait_seconds = 0`, `reason = NO_SIGNALS`

**Key rules:**

- A single WAIT candidate must not be overridden by ALLOW candidates.
- `FAIL` includes `remaining == 0`, `remaining is None`, and
  `0 < remaining < units_requested` — all insufficient states.
- `INSUFFICIENT` reason is used only when the WAIT is driven by reset-at
  alone (no effective retry-after).
- No `CONFLICT` reason — conflicts are resolved by the deterministic
  WAIT > FAIL > ALLOW > expired priority.

---

## 4. Data Explicitly Excluded

The RateLimit service must **never** store, embed, or derive:

| Forbidden data | Reason |
|---------------|--------|
| Raw provider stdout/stderr bytes | Opaque Worker output — security boundary |
| Raw HTTP response body or headers | Unvalidated external content — security boundary |
| Raw exit codes | Provider-specific — detection is TC-13.14c |
| API keys, environment variables, credentials | Security boundary |
| `project_root` absolute path | Filesystem exposure |
| Prompt text, model responses | User content — security boundary |
| Python `repr()` / `str()` of untrusted objects | Leaks internal representation |
| `dict`, `Any`, `object` in public API | Untyped data — all fields must be typed |

**Allowed** stable business identifiers: `provider` string (e.g.
`"anthropic"`, `"openai"`, `"codex"`).

---

## 5. Exception Hierarchy — Exactly 4 Types

```python
class RateLimitError(Exception):
    """Base for all RateLimit errors."""


class RateLimitInputError(RateLimitError):
    """Invalid input — wrong type, missing field, non-UTC datetime."""


class RateLimitStateError(RateLimitError):
    """Inconsistent state — conflicting signals, invalid combination."""


class RateLimitSecurityError(RateLimitError):
    """Security boundary violation — unsafe content rejected."""
```

### 5.1 Exception Message Safety — All Types

Exception messages must **never** contain:

- Raw provider output (stdout, stderr, HTTP body, headers)
- API keys, tokens, or credentials
- Workspace or filesystem absolute paths
- `provider` actual input values (only `type(x).__name__` and field names)
- Prompt text, model responses
- `repr()` or `str()` of any untrusted object
- Stack traces from provider calls

Exception messages **may** contain: `type(x).__name__`, field names, and
the exception class name.

### 5.2 Exception Semantics

| Exception | Trigger |
|-----------|---------|
| `RateLimitInputError` | Request type invalid; `provider` not a non-empty str; `scope` not a `RateLimitScope` instance; `now` is not timezone-aware UTC datetime; `units_requested` is not a positive int >= 1 (including `0` or `bool`); `signals` is not a tuple of `RateLimitSignal`; signal field type violations; `remaining > limit` when both present; `reset_at < observed_at` |
| `RateLimitStateError` | Signal has `observed_at > request.now` (future observation); `action`/`reason` consistency violation in decision construction |
| `RateLimitSecurityError` | Signal contains raw provider output in forbidden fields; untyped data detected in public API boundary |

---

## 6. Determinism

Identical `RateLimitCheckRequest` inputs must produce identical
`RateLimitDecision` outputs.  No timestamps, random values, UUIDs, or
non-deterministic ordering are introduced during evaluation.

---

## 7. Relationship to Upstream / Downstream

| Interface | Relationship |
|-----------|-------------|
| `WorkerAdapter` (TC-13.9b) | Provider detector (TC-13.14c) will observe WorkerAdapter errors — not modified |
| `EscalationService` (TC-13.13b) | Independent — escalation is tier-based, not rate-based.  A rate-limit event must **never** call `evaluate_escalation()`, change `WorkerKind`, or generate an `EscalationDecision` (preserves §2.16.10) |
| `WorkflowOrchestrator` (TC-13.18b) | Future consumer — will call `evaluate()` before dispatch |
| TC-13.14b | Runtime implementation of this contract |
| TC-13.14c | Provider-specific detection — produces `RateLimitSignal` from raw provider output |

---

## 8. Task-Card Split

| Card | Description | Status |
|------|-------------|--------|
| **TC-13.14a** | Provider-neutral RateLimit contract freeze | Contract Current |
| **TC-13.14a.1** | RateLimit evaluation semantics closure | Contract Current |
| **TC-13.14a.2** | RateLimit multi-scope and combined signal closure | Contract Current |
| **TC-13.14b** | RateLimitService production module | Current |
| **TC-13.14c** | Provider-specific 429 detection (evidence-dependent) | Evidence-dependent Target |

---

## 9. Explicit Non-Goals

- Provider 429 detection, stdout/stderr parsing, exit-code classification → TC-13.14c
- Token bucket, sliding window, or fixed window implementation → TC-13.14b
- `record()` or `consume()` methods → TC-13.14b
- File storage, database, or network persistence → TC-13.14b
- Retry loop, backoff strategy, or jitter → WorkflowOrchestrator
- Guessing or inferring any provider's 429 text format → TC-13.14c
- Real CLI execution, API calls, or network access → TC-13.14c
- Integration with WorkflowOrchestrator dispatch cycle → future card
