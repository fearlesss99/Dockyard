# AgentDesk HTML Dashboard — Frozen Contract (Target — TC-13.20)

Interface #24 frozen contract.  TC-13.20a freezes the API, data scope,
security rules, and digest algorithm.  TC-13.20b will deliver the
production `html_dashboard.py` module.

## Status

**Target** as of TC-13.20a.  This document is the authoritative frozen
specification for the Dashboard public API, data model, security
boundary, exception hierarchy, and snapshot digest algorithm.  No
production module is shipped under TC-13.20a.

The ADR contract is in `001-mad-agentdesk-integration.md` §2.21;
this document expands the per-interface details.

---

## 1. Purpose

`render_dashboard()` is a **pure synchronous function** that consumes a
pre-built `StateSnapshot` and produces a self-contained, offline-capable
HTML artifact.  It owns zero state, zero I/O, zero subprocess execution,
and zero network access.

---

## 2. Architecture

```text
StateProvider.snapshot()
        ↓
DashboardRenderRequest
        ↓
render_dashboard()
        ↓
DashboardArtifact(html bytes)
        ↓
caller displays or saves as they choose
```

### 2.1 Hard Boundaries

The Dashboard renderer must **never**:

| Forbidden action | Correct alternative |
|-----------------|-------------------|
| Start HTTP server / FastAPI / Flask / Streamlit | Caller owns serving |
| Run React, Vue, Node, npm, or any JS bundler | Pure Python string builder |
| Read `tasks.yaml`, events, outbox, acceptances directly | Data arrives via `StateSnapshot` |
| Read `mad-refs.yaml` directly | Data arrives via `StateSnapshot` |
| Read runtime lease/lock files | Not in scope |
| Execute `git` commands | Not in scope |
| Access MAD archive paths | Not in scope |
| Access API keys, env vars, or credentials | Not in scope |
| Write any file or create any directory | Caller owns persistence |
| Call `datetime.now()` or `time.time()` | `generated_at` is caller-supplied |
| Refresh `StateProvider` internally | Single snapshot per call |
| Provide approve/retry/cancel/requeue controls | Read-only by design |
| Start subprocess or call external binaries | Pure function |
| Make HTTP requests or open sockets | Offline-only |
| Use Python `hash()`, `repr()`, or `id()` for digests | Deterministic SHA-256 |

---

## 3. Public API — Exactly 7 Symbols

```python
__all__ = [
    "DashboardRenderRequest",
    "DashboardArtifact",
    "render_dashboard",
    "DashboardError",
    "DashboardInputError",
    "DashboardRenderError",
    "DashboardSecurityError",
]
```

No other public names.  No internal helpers are exposed.  No constants,
enums, or utility functions are part of the public API.

### 3.1 `DashboardRenderRequest` — Exactly 2 Fields

```python
from dataclasses import dataclass
from datetime import datetime
from state_provider import StateSnapshot


@dataclass(frozen=True, slots=True)
class DashboardRenderRequest:
    """Immutable input for a single dashboard render — exactly 2 fields."""
    snapshot: StateSnapshot
    generated_at: datetime
```

Requirements:

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state
- Exactly 2 fields: `snapshot` and `generated_at`
- `snapshot` must be a `StateSnapshot` instance — no other type accepted
- `generated_at` must be a timezone-aware UTC `datetime`
- No `project_root`, `output_path`, `refresh_interval`, or mutable mapping fields
- Must not internally call `datetime.now()`

### 3.2 `DashboardArtifact` — Exactly 4 Fields

```python
@dataclass(frozen=True, slots=True)
class DashboardArtifact:
    """Immutable render output — exactly 4 fields."""
    html: bytes
    snapshot_digest: str
    generated_at: str
    task_count: int
```

Requirements:

- `frozen=True`, `slots=True` — no `__dict__`, no mutable state
- Exactly 4 fields: `html`, `snapshot_digest`, `generated_at`, `task_count`
- `html` is `bytes` — UTF-8 encoded, self-contained HTML document
- `snapshot_digest` is `str` — format `sha256:` + 64 lowercase hex chars
- `generated_at` is `str` — RFC 3339 UTC (e.g. `2026-07-29T12:00:00Z`)
- `task_count` is `int` — non-negative, not `bool`

### 3.3 `render_dashboard` — Synchronous Pure Function

```python
def render_dashboard(
    request: DashboardRenderRequest,
) -> DashboardArtifact:
    """Render a read-only HTML dashboard from a StateSnapshot."""
    ...
```

Requirements:

- **Synchronous** — must NOT be `async def`
- **Pure** — same `request` → byte-for-byte identical `html`
- Accepts exactly one argument of type `DashboardRenderRequest`
- Returns exactly `DashboardArtifact`
- No side effects: no file writes, no network, no subprocess, no clock calls

---

## 4. Data Presented

All data originates exclusively from `StateSnapshot` typed fields.
No raw file parsing, no direct YAML/JSON access, no regex over
untrusted strings.

### 4.1 Overview Section

| Field | Source |
|-------|--------|
| `project_id` | `StateSnapshot.project_id` |
| Snapshot `updated_at` | `StateSnapshot.updated_at` |
| `generated_at` | `DashboardRenderRequest.generated_at` (RFC 3339) |
| Total task count | `len(snapshot.tasks)` |
| Count per state | Group by `TaskEntry.state` |
| Active / blocked / terminal counts | Derived from state |
| Acceptance count | `len(snapshot.acceptances)` |
| MAD reference count | `len(snapshot.mad_refs)` if not `None`, else 0 |

### 4.2 Task Board

Each task row displays **only**:

| Column | Source |
|--------|--------|
| `task_id` | `TaskEntry.task_id` |
| `state` | `TaskEntry.state` |
| `revision` | `TaskEntry.revision` |
| `attempt` | `TaskEntry.attempt` |
| `delivery_state` | `TaskEntry.delivery_state` |
| `integration_state` | `TaskEntry.integration_state` |
| Current worker role | `TaskEntry.current_dispatch.role_id` when dispatch exists |
| `updated_at` | `TaskEntry.timestamps.updated_at` |
| `blocked_kind` | `TaskEntry.blocked_kind` when present |
| `superseded_by` | `TaskEntry.superseded_by` when present |

Sorting: deterministic by `(state, task_id)`.  States order: Blocked,
Dispatched, InProgress, Ready, Draft, Integrated, Cancelled, Superseded.

### 4.3 Task Detail

- Task base state (all `TaskEntry` non-sensitive fields)
- Dispatch summary (`dispatch_id`, `role_id`, `dispatched_at`, `model_id`
  — from `DispatchInfo` and `ModelSelectionSnapshot`)
- Event timeline sorted by `occurred_at` ascending, then `event_id`
  ascending (deterministic tie-break).  Each event shows: `event_type`,
  `from_state` → `to_state`, `occurred_at`, `event_id`.
- Acceptance decisions summary: `decision`, `accepted_commit`,
  `reviewed_dispatch_id`, `review_n`
- MAD reference summary: `purpose`, `deliberation_id`, `status`,
  `created_at`
- Evidence reference count: `len(event.evidence_refs)` per event

### 4.4 System Health

| Metric | Source |
|--------|--------|
| Snapshot digest | Computed per §10 |
| Canonical source counts | `tasks`, `events`, `outbox`, `acceptances`, `mad_refs` tuple lengths |
| Blocked tasks present | `any(t.blocked_kind is not None for t in snapshot.tasks)` |
| Pending outbox messages | `any(o for o in snapshot.outbox)` |
| Empty-state indicator | When `task_count == 0`, a clear empty-state message is shown |

---

## 5. Data Explicitly Excluded

The Dashboard must **never** render, embed, or derive:

| Forbidden data | Reason |
|---------------|--------|
| Prompt text | User content — security boundary |
| `stdout` / `stderr` bytes | Opaque Worker output |
| Raw model responses | Unvalidated external content |
| Delivery report body | Free-form text |
| Acceptance Markdown body | Free-form text |
| Issue description / recommendation text | Free-form text |
| MAD archive absolute paths | Filesystem exposure |
| `project_root` absolute path | Filesystem exposure |
| `holder_instance_id` | Lease ownership token |
| Lease ownership token | Security boundary |
| API keys, environment variables, credentials | Security boundary |
| Raw event/outbox/acceptance mapping dicts | Un-typed data |
| Untyped arbitrary payloads | Un-typed data |
| Python `repr()` output | Leaks internal representation |

**Allowed** stable business identifiers: `task_id`, `event_id`,
`message_id`, `dispatch_id`, `deliberation_id`.  Exception messages
must never contain any of these values.

---

## 6. HTML Security Contract

The generated artifact must satisfy every rule:

### 6.1 Self-Containment

- Single `.html` file, zero external dependencies
- No CDN, font, image, script, or stylesheet references
- No `<link rel="stylesheet">` to external URLs
- No `<script src="...">` to external URLs
- No `@import` in `<style>` blocks
- Opens correctly offline in any modern browser

### 6.2 No Interactive Writes

- No `<form>` elements
- No `<button>` elements that trigger state changes
- No `<a>` links to internal or external endpoints
- No JavaScript that modifies the DOM based on user input
- No `XMLHttpRequest`, `fetch()`, `WebSocket`, or `EventSource`

### 6.3 Content Security Policy

Delivered with a strict `<meta>` CSP:

```html
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none';
               img-src data:;
               style-src 'unsafe-inline';
               script-src 'none';
               connect-src 'none';
               form-action 'none';
               base-uri 'none';
               frame-ancestors 'none'">
```

If future production requires JavaScript, `script-src` may be relaxed
to `'unsafe-inline'` in TC-13.20b, but TC-13.20a mandates `'none'`.

### 6.4 Output Safety

- All dynamic text (task_id, state, timestamps, etc.) undergoes HTML
  entity escaping: `<` → `&lt;`, `>` → `&gt;`, `&` → `&amp;`,
  `"` → `&quot;`, `'` → `&#x27;`
- No dynamic data concatenated into `innerHTML` (if JS is later added)
- No `eval()`, `Function()`, or remote script execution
- No data URIs except for inline CSS icons (if any)

### 6.5 Encoding

- UTF-8 encoding, no BOM
- LF line endings (`\n`)
- Exactly one trailing newline
- `Content-Type` meta: `<meta charset="UTF-8">`

### 6.6 Determinism

Identical `DashboardRenderRequest` inputs must produce byte-for-byte
identical `DashboardArtifact.html`.  No timestamps, random values,
UUIDs, or non-deterministic ordering are introduced during render.

---

## 7. Usability Requirements

The contract mandates:

| Requirement | Detail |
|------------|--------|
| Responsive layout | Desktop and narrow viewport (≥320 px) |
| Semantic HTML | `<header>`, `<nav>`, `<main>`, `<table>`, `<section>`, `<h1>`–`<h3>` |
| Keyboard accessible | All content reachable via Tab; no keyboard traps |
| Status not color-alone | Blocked/failure states use icon + text, not color only |
| Contrast | Light/dark mode aware; WCAG AA minimum contrast |
| Blocked/failure visible | Distinct visual treatment without leaking payload |
| Empty project | Clear "No tasks" message, no error |
| No silent truncation | Large snapshots scroll; no hidden overflow |
| Sorting documented | All tables/sections declare sort order in §4 |

---

## 8. Exception Hierarchy — Exactly 4 Types

```python
class DashboardError(Exception):
    """Base for all Dashboard errors."""


class DashboardInputError(DashboardError):
    """Invalid input — wrong type, missing field, non-UTC datetime."""


class DashboardRenderError(DashboardError):
    """Render failure — internal precondition violated.

    Must preserve the underlying exception as __cause__.
    """


class DashboardSecurityError(DashboardError):
    """Security boundary violation — unsafe content rejected."""
```

### 8.1 Exception Semantics

| Exception | Trigger |
|-----------|---------|
| `DashboardInputError` | Request type invalid; `snapshot` is not `StateSnapshot`; `generated_at` is not timezone-aware UTC datetime; any data type or required input violates the contract |
| `DashboardRenderError` | Valid input encounters an internal rendering failure during deterministic HTML construction; must preserve the underlying exception as `__cause__` |
| `DashboardSecurityError` | Rendered output violates the frozen security boundary (e.g. forbidden tag detected, forbidden resource reference, CSP invariant not met) |

`DashboardRenderError` and `DashboardSecurityError` must not return HTML,
paths, or user-generated content in the exception message.

### 8.2 Exception Message Safety — All Types

Exception messages must **never** contain:

- `project_root` or any absolute filesystem path
- `task_id`, `dispatch_id`, `event_id`, `message_id`, `deliberation_id` (actual input values)
- Prompt text, `stdout`, `stderr`
- MAD report body, issue text, acceptance Markdown body
- Raw HTML fragments
- Secrets, tokens, API keys
- `repr()` or `str()` of any untrusted object

Exception messages **may** contain: `type(x).__name__`, field names, and
the exception class name.

Rules:

- No parallel `StateProviderError` wrapping
- Non-`StateSnapshot` input → `DashboardInputError`
- Non-UTC `generated_at` → `DashboardInputError`
- Illegal dynamic content or unsafe encoding → `DashboardSecurityError` (fail-closed)

---

## 9. Snapshot Digest Algorithm

The digest identifies the exact snapshot used for rendering.  It is
deterministic, collision-resistant, and immune to Python hash
randomization.

### 9.1 Digest Input

```text
sha256(
    schema_version + "\n" +
    project_id + "\n" +
    updated_at + "\n" +
    read_hexsha + "\n" +
    tasks_digest + "\n" +
    events_digest + "\n" +
    outbox_digest + "\n" +
    acceptances_digest + "\n" +
    mad_refs_digest
)
```

### 9.2 Sub-Digests

Each collection digest is `sha256(...)` of its sorted, canonical
encoding:

| Collection | Sort key | Encoded fields per entry |
|-----------|---------|------------------------|
| `tasks_digest` | `task_id` | `task_id \| state \| revision \| attempt \| delivery_state \| integration_state \| updated_at` |
| `events_digest` | `event_id` | `event_id \| event_type \| task_id \| occurred_at` |
| `outbox_digest` | `message_id` | `message_id \| message_type \| task_id \| created_at` |
| `acceptances_digest` | `task_id` + `review_n` | `task_id \| review_n \| decision \| accepted_commit` |
| `mad_refs_digest` | `deliberation_id` | `deliberation_id \| task_id \| purpose \| status` |

Fields are joined by `|` (pipe).  `None` values are encoded as the
literal string `None`.  Each sub-digest line is terminated with `\n`.

### 9.3 Forbidden Hash Inputs

- Python `hash()` — not stable across processes
- `repr()` / `str()` of objects — not canonical
- `id()` / memory address — not deterministic
- `datetime.now()` / `time.time()` — not reproducible
- `dict.keys()` / `set` iteration — not ordered

---

## 10. Current / Target Boundary

| Scope | Status |
|-------|--------|
| Interface #24 — HTML Dashboard contract | **Target** — this document |
| `html_dashboard.py` production module | Target — TC-13.20b |
| `render_dashboard()` implementation | Target — TC-13.20b |
| HTML/CSS/JS page templates | Target — TC-13.20b |
| Dashboard unit tests | Target — TC-13.20b |
| Interface #24 status promotion to Current | Target — TC-13.20b |

No production code, HTML page, CSS, JS, or dashboard directory is
created under TC-13.20a.  TC-13.20b will implement the full production
module against this frozen contract.

---

## 11. Explicit Non-Goals

TC-13.20a does **not**:

- Create `skills/agentdesk/scripts/html_dashboard.py`
- Create any HTML, CSS, or JavaScript files
- Create a dashboard assets or build directory
- Install any npm/pip dependencies
- Start a browser, HTTP server, or WebSocket
- Modify `StateProvider`, `WorkflowOrchestrator`, or any production module
- Change Interface #24 from Target to Current
- Add write controls (approve, retry, cancel, requeue)
- Add real-time refresh, polling, or Server-Sent Events
- Add authentication, authorization, or user sessions
- Render prompt text, stdout/stderr, or raw model output
- Access MAD archive files or absolute paths

---

## 12. Future Production Card

**TC-13.20b** will implement `html_dashboard.py` against this contract:

- Pure synchronous `render_dashboard()` function
- All security rules enforced at the Python level (escaping, CSP, no I/O)
- No HTTP server — callers embed or serve the artifact
- Full unit test coverage against frozen contract assertions
- Interface #24 promoted to Current upon delivery
