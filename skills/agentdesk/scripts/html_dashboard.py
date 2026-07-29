"""AgentDesk HTML Dashboard — read-only renderer (TC-13.20b).

Interface #24 production module.  ``render_dashboard()`` is a pure
synchronous function that consumes a pre-built ``StateSnapshot`` and
produces a self-contained, offline HTML artifact.  It owns zero state,
zero I/O, zero subprocess execution, and zero network access.

Public API — exactly 7 symbols::

    DashboardRenderRequest, DashboardArtifact, render_dashboard,
    DashboardError, DashboardInputError, DashboardRenderError,
    DashboardSecurityError
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from state_provider import StateSnapshot

__all__ = [
    "DashboardRenderRequest",
    "DashboardArtifact",
    "render_dashboard",
    "DashboardError",
    "DashboardInputError",
    "DashboardRenderError",
    "DashboardSecurityError",
]


# ── exceptions ─────────────────────────────────────────────────────────────


class DashboardError(Exception):
    """Base for all Dashboard errors."""


class DashboardInputError(DashboardError):
    """Invalid input — wrong type, missing field, non-UTC datetime."""


class DashboardRenderError(DashboardError):
    """Render failure — internal precondition violated.

    Preserves the underlying exception as ``__cause__``.
    """


class DashboardSecurityError(DashboardError):
    """Security boundary violation — unsafe content rejected (fail-closed)."""


# ── frozen dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DashboardRenderRequest:
    """Immutable input for a single dashboard render — exactly 2 fields.

    Fields:
        snapshot: a ``StateSnapshot`` instance (exact type).
        generated_at: a timezone-aware UTC ``datetime``.
    """

    snapshot: StateSnapshot
    generated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, StateSnapshot):
            raise DashboardInputError("snapshot must be a StateSnapshot instance")
        if not isinstance(self.generated_at, datetime):
            raise DashboardInputError("generated_at must be a datetime instance")
        if self.generated_at.tzinfo is None:
            raise DashboardInputError("generated_at must be timezone-aware UTC")
        offset = self.generated_at.utcoffset()
        if offset is None or offset != timedelta(0):
            raise DashboardInputError("generated_at must have a zero UTC offset")


@dataclass(frozen=True, slots=True)
class DashboardArtifact:
    """Immutable render output — exactly 4 fields.

    Fields:
        html: UTF-8 bytes of a self-contained HTML document.
        snapshot_digest: ``sha256:`` + 64 lowercase hex chars.
        generated_at: RFC 3339 UTC string derived from the request.
        task_count: non-negative, non-bool integer.
    """

    html: bytes
    snapshot_digest: str
    generated_at: str
    task_count: int


# ── constants ──────────────────────────────────────────────────────────────

_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "script-src 'none'; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

# Deterministic task-state display order (contract §4.2).  The eight
# canonical board states come first; the remaining valid states fall
# after them in a stable alphabetical tail.
_STATE_ORDER: dict[str, int] = {
    "blocked": 0,
    "dispatched": 1,
    "in_progress": 2,
    "ready": 3,
    "draft": 4,
    "integrated": 5,
    "cancelled": 6,
    "superseded": 7,
    "accepted": 8,
    "returned": 9,
    "review_ready": 10,
}

_STATE_SYMBOL: dict[str, str] = {
    "blocked": "⦸",
    "dispatched": "▶",
    "in_progress": "●",
    "ready": "○",
    "draft": "✎",
    "integrated": "✓",
    "cancelled": "✕",
    "superseded": "↳",
    "accepted": "★",
    "returned": "↩",
    "review_ready": "◎",
}

_NONE_DISPLAY = "—"  # — em dash for absent optional values

# Frozen section navigation (contract §7 keyboard accessibility).  href
# fragments are module constants — never derived from snapshot or user
# input — so the only hrefs that can ever reach the output are the five
# allow-listed fragments below.
_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("Overview", "#overview"),
    ("Task Board", "#task-board"),
    ("Task Details", "#task-details"),
    ("System Health", "#system-health"),
)
_SKIP_LINK_HREF = "#main-content"

# Security-invariant scanners (contract §6).  Every pattern is anchored
# on a leading ``<`` so static CSS / CSP text never matches.
_SCRIPT_RE = re.compile(r"<script", re.IGNORECASE)
_FORM_RE = re.compile(r"<form", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_JS_URL_RE = re.compile(r"javascript:", re.IGNORECASE)
_HTTP_URL_RE = re.compile(r"https?://", re.IGNORECASE)


# ── digest (contract §9) ──────────────────────────────────────────────────


def _enc_val(value: Any) -> str:
    """Canonical text encoding of a single digest field value.

    ``None`` becomes the literal string ``None``; ints become their
    decimal string; strings pass through unchanged.  Booleans (not used
    in digest fields) map to ``True``/``False``.  No ``repr()`` or
    ``str()`` of untrusted objects is ever produced.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    return value  # already a str


def _sub_digest(lines: list[str]) -> str:
    """SHA-256 over newline-terminated canonical lines."""
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _tasks_sub_digest(tasks: tuple[Any, ...]) -> str:
    lines: list[str] = []
    for t in sorted(tasks, key=lambda x: x.task_id):
        lines.append("|".join((
            _enc_val(t.task_id),
            _enc_val(t.state),
            _enc_val(t.revision),
            _enc_val(t.attempt),
            _enc_val(t.delivery_state),
            _enc_val(t.integration_state),
            _enc_val(t.timestamps.updated_at),
        )))
    return _sub_digest(lines)


def _events_sub_digest(events: tuple[Any, ...]) -> str:
    lines: list[str] = []
    for e in sorted(events, key=lambda x: x.event_id):
        lines.append("|".join((
            _enc_val(e.event_id),
            _enc_val(e.event_type),
            _enc_val(e.task_id),
            _enc_val(e.occurred_at),
        )))
    return _sub_digest(lines)


def _outbox_sub_digest(outbox: tuple[Any, ...]) -> str:
    lines: list[str] = []
    for o in sorted(outbox, key=lambda x: x.message_id):
        lines.append("|".join((
            _enc_val(o.message_id),
            _enc_val(o.message_type),
            _enc_val(o.task_id),
            _enc_val(o.created_at),
        )))
    return _sub_digest(lines)


def _acceptances_sub_digest(acceptances: tuple[Any, ...]) -> str:
    lines: list[str] = []
    for a in sorted(acceptances, key=lambda x: (x.task_id, x.review_n)):
        lines.append("|".join((
            _enc_val(a.task_id),
            _enc_val(a.review_n),
            _enc_val(a.decision),
            _enc_val(a.accepted_commit),
        )))
    return _sub_digest(lines)


def _mad_refs_sub_digest(mad_refs: tuple[Any, ...] | None) -> str:
    if mad_refs is None:
        return _sub_digest([])  # empty collection -> sha256 of empty input
    lines: list[str] = []
    for m in sorted(mad_refs, key=lambda x: x.deliberation_id):
        lines.append("|".join((
            _enc_val(m.deliberation_id),
            _enc_val(m.task_id),
            _enc_val(m.purpose),
            _enc_val(m.status),
        )))
    return _sub_digest(lines)


def _compute_snapshot_digest(snapshot: StateSnapshot) -> str:
    """Deterministic SHA-256 over the snapshot (contract §9.1)."""
    parts = [
        snapshot.schema_version,
        snapshot.project_id,
        snapshot.updated_at,
        snapshot.read_hexsha,
        _tasks_sub_digest(snapshot.tasks),
        _events_sub_digest(snapshot.events),
        _outbox_sub_digest(snapshot.outbox),
        _acceptances_sub_digest(snapshot.acceptances),
        _mad_refs_sub_digest(snapshot.mad_refs),
    ]
    data = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ── formatting helpers ────────────────────────────────────────────────────


def _format_rfc3339_utc(dt: datetime) -> str:
    """Deterministic RFC 3339 UTC string, second precision, ``Z`` suffix."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _esc(value: Any) -> str:
    """HTML-escape a dynamic value for safe text insertion (contract §6.4)."""
    if value is None:
        return _NONE_DISPLAY
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return html.escape(str(value), quote=True)
    return html.escape(value, quote=True)


def _state_rank(state: str) -> int:
    return _STATE_ORDER.get(state, 1000)


def _state_badge(state: str) -> str:
    symbol = _STATE_SYMBOL.get(state, "●")
    cls = "state state-" + _esc(state)
    return (
        f'<span class="{cls}">'
        f'{symbol} {_esc(state)}</span>'
    )


# ── HTML construction ──────────────────────────────────────────────────────

_CSS = """\
:root{--bg:#f4f5f7;--fg:#16181d;--muted:#5b6270;--line:#d3d7de;--card:#ffffff;
--blocked:#b4230a;--ok:#157f3b;--warn:#946a00;--info:#1d4ed8}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,"Segoe UI",Roboto,Arial,sans-serif;
background:var(--bg);color:var(--fg);line-height:1.45}
.skip-link{position:absolute;left:-9999px;top:auto;width:1px;height:1px;
overflow:hidden;white-space:nowrap}
.skip-link:focus,.skip-link:focus-visible{position:fixed;top:.5rem;left:.5rem;
width:auto;height:auto;padding:.75rem 1rem;margin:0;overflow:visible;
background:var(--info);color:#fff;z-index:1000;border-radius:4px;
outline:2px solid #fff;outline-offset:1px}
header{background:#111827;color:#ffffff;padding:1rem 1.25rem}
header h1{margin:0;font-size:1.25rem}
header .meta{margin:.3rem 0 0;color:#cbd5e1;font-size:.85rem}
nav{background:#1f2937;color:#e5e7eb;padding:.5rem 1.25rem}
nav ul{margin:0;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:.85rem;font-size:.85rem}
nav li{font-weight:600}
nav a{color:#e5e7eb;text-decoration:none;display:inline-block;
padding:.15rem .25rem;border-radius:2px}
nav a:focus-visible{outline:2px solid var(--info);outline-offset:2px;
color:#fff;background:rgba(255,255,255,.12)}
main{padding:1.25rem 1.25rem 2rem;max-width:1440px;margin:0 auto}
section{margin-bottom:1.75rem}
h2{border-bottom:2px solid var(--line);padding-bottom:.3rem;margin:0 0 .85rem;font-size:1.1rem}
h3{margin:.4rem 0 .5rem;font-size:.95rem}
table{border-collapse:collapse;width:100%;font-size:.85rem;background:var(--card)}
caption{caption-side:top;text-align:left;font-weight:600;padding:.35rem 0;color:var(--muted)}
th,td{border:1px solid var(--line);padding:.4rem .55rem;text-align:left;vertical-align:top}
thead th{background:#eef0f4}
tbody tr:nth-child(even){background:#fafafb}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:.5rem}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.6rem}
.metric{background:var(--card);border:1px solid var(--line);padding:.6rem .75rem;border-radius:4px}
.metric .k{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.03em}
.metric .v{font-size:1rem;font-weight:600;word-break:break-all}
h3,p,dt,dd,.task-block h3,.task-block dl dd,header .meta,.metric .v{overflow-wrap:anywhere;word-break:break-word;min-width:0}
.state{font-weight:600;white-space:nowrap}
.empty-state{padding:.75rem;background:var(--card);border:1px dashed var(--line);color:var(--muted)}
article.task{background:var(--card);border:1px solid var(--line);padding:.75rem;margin-bottom:.75rem;border-radius:4px}
.task-block{margin-top:.6rem}
.task-block h3{margin:.1rem 0 .4rem}
dl.row{display:grid;grid-template-columns:max-content 1fr;gap:.15rem .6rem;margin:0}
dl.row dt{color:var(--muted);font-weight:600}
dl.row dd{margin:0}
:focus-visible{outline:2px solid var(--info);outline-offset:1px}
@media (prefers-reduced-motion: reduce){*{animation-duration:0s!important;transition:none!important}}
@media (max-width:320px){
body{font-size:.85rem}
main{padding:.5rem}
nav{padding:.4rem .5rem}
nav ul{gap:.5rem}
nav a:focus-visible{outline-offset:1px}
.metrics{grid-template-columns:1fr}
table{font-size:.74rem}
th,td{padding:.28rem .32rem}
}
"""


def _overview_section(snapshot: StateSnapshot, gen_str: str) -> list[str]:
    state_counts: dict[str, int] = {}
    for t in snapshot.tasks:
        state_counts[t.state] = state_counts.get(t.state, 0) + 1
    blocked = sum(1 for t in snapshot.tasks if t.blocked_kind is not None)
    terminal = sum(
        1 for t in snapshot.tasks
        if t.state in ("integrated", "cancelled", "superseded")
    )
    active = sum(
        1 for t in snapshot.tasks
        if t.state in ("dispatched", "in_progress", "review_ready", "ready")
    )
    mad_count = 0 if snapshot.mad_refs is None else len(snapshot.mad_refs)
    out: list[str] = []
    out.append('<section id="overview">')
    out.append("<h2>Project Overview</h2>")
    out.append('<div class="metrics">')
    for key, val in (
        ("Project ID", snapshot.project_id),
        ("Snapshot Updated At", snapshot.updated_at),
        ("Generated At", gen_str),
        ("Total Tasks", str(len(snapshot.tasks))),
        ("Active Tasks", str(active)),
        ("Blocked Tasks", str(blocked)),
        ("Terminal Tasks", str(terminal)),
        ("Acceptances", str(len(snapshot.acceptances))),
        ("MAD References", str(mad_count)),
    ):
        out.append('<div class="metric">')
        out.append(f'<div class="k">{_esc(key)}</div>')
        out.append(f'<div class="v">{_esc(val)}</div>')
        out.append("</div>")
    out.append("</div>")
    out.append('<div class="table-scroll">')
    out.append("<table>")
    out.append("<caption>Tasks by State</caption>")
    out.append("<thead><tr><th>State</th><th>Count</th></tr></thead>")
    out.append("<tbody>")
    for state in sorted(state_counts, key=lambda s: (_state_rank(s), s)):
        out.append(
            "<tr><td>" + _state_badge(state)
            + f"</td><td>{_esc(state_counts[state])}</td></tr>"
        )
    out.append("</tbody></table></div>")
    out.append("</section>")
    return out


def _task_board_section(snapshot: StateSnapshot) -> list[str]:
    out: list[str] = []
    out.append('<section id="task-board">')
    out.append("<h2>Task Board</h2>")
    tasks = sorted(snapshot.tasks, key=lambda t: (_state_rank(t.state), t.task_id))
    if not tasks:
        out.append('<p class="empty-state">No tasks</p>')
        out.append("</section>")
        return out
    out.append('<div class="table-scroll">')
    out.append("<table>")
    out.append("<caption>Tasks (sorted by state, then task id)</caption>")
    out.append(
        "<thead><tr>"
        "<th>Task ID</th><th>State</th><th>Revision</th><th>Attempt</th>"
        "<th>Delivery</th><th>Integration</th><th>Worker Role</th>"
        "<th>Updated At</th><th>Blocked Kind</th><th>Superseded By</th>"
        "</tr></thead>"
    )
    out.append("<tbody>")
    for t in tasks:
        role = ""
        if t.current_dispatch is not None:
            role = t.current_dispatch.role_id
        out.append("<tr>")
        out.append(f"<td>{_esc(t.task_id)}</td>")
        out.append("<td>" + _state_badge(t.state) + "</td>")
        out.append(f"<td>{_esc(t.revision)}</td>")
        out.append(f"<td>{_esc(t.attempt)}</td>")
        out.append(f"<td>{_esc(t.delivery_state)}</td>")
        out.append(f"<td>{_esc(t.integration_state)}</td>")
        out.append(f"<td>{_esc(role)}</td>")
        out.append(f"<td>{_esc(t.timestamps.updated_at)}</td>")
        out.append(f"<td>{_esc(t.blocked_kind)}</td>")
        out.append(f"<td>{_esc(t.superseded_by)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    out.append("</section>")
    return out


def _task_details_section(snapshot: StateSnapshot) -> list[str]:
    events_by_task: dict[str, list[Any]] = {}
    for e in snapshot.events:
        events_by_task.setdefault(e.task_id, []).append(e)
    acc_by_task: dict[str, list[Any]] = {}
    for a in snapshot.acceptances:
        acc_by_task.setdefault(a.task_id, []).append(a)
    mad_by_task: dict[str, list[Any]] = {}
    if snapshot.mad_refs is not None:
        for m in snapshot.mad_refs:
            mad_by_task.setdefault(m.task_id, []).append(m)

    out: list[str] = []
    out.append('<section id="task-details">')
    out.append("<h2>Task Details</h2>")
    tasks = sorted(snapshot.tasks, key=lambda t: t.task_id)
    if not tasks:
        out.append('<p class="empty-state">No tasks</p>')
        out.append("</section>")
        return out
    for t in tasks:
        out.append('<article class="task">')
        out.append(f"<h3>{_esc(t.task_id)}</h3>")
        # base state
        out.append('<div class="task-block">')
        out.append("<h3>Current State</h3>")
        out.append('<dl class="row">')
        for label, val in (
            ("Task ID", t.task_id),
            ("State", t.state),
            ("Revision", t.revision),
            ("Attempt", t.attempt),
            ("Delivery State", t.delivery_state),
            ("Integration State", t.integration_state),
            ("Updated At", t.timestamps.updated_at),
            ("Blocked Kind", t.blocked_kind),
            ("Superseded By", t.superseded_by),
        ):
            out.append(f"<dt>{_esc(label)}</dt>")
            if label == "State":
                out.append("<dd>" + _state_badge(t.state) + "</dd>")
            else:
                out.append(f"<dd>{_esc(val)}</dd>")
        out.append("</dl></div>")
        # dispatch summary
        if t.current_dispatch is not None:
            d = t.current_dispatch
            out.append('<div class="task-block">')
            out.append("<h3>Current Dispatch</h3>")
            out.append('<dl class="row">')
            for label, val in (
                ("Dispatch ID", d.dispatch_id),
                ("Role ID", d.role_id),
                ("Dispatched At", d.dispatched_at),
                ("Model ID", d.model_selection.selected_model_id),
            ):
                out.append(f"<dt>{_esc(label)}</dt>")
                out.append(f"<dd>{_esc(val)}</dd>")
            out.append("</dl></div>")
        # event timeline
        evs = sorted(
            events_by_task.get(t.task_id, []),
            key=lambda e: (e.occurred_at, e.event_id),
        )
        out.append('<div class="task-block">')
        out.append("<h3>Event Timeline</h3>")
        if not evs:
            out.append('<p class="empty-state">No events</p>')
        else:
            out.append('<div class="table-scroll"><table>')
            out.append("<caption>Events (sorted by occurred at, then event id)</caption>")
            out.append(
                "<thead><tr><th>Type</th><th>Transition</th>"
                "<th>Occurred At</th><th>Event ID</th>"
                "<th>Evidence</th></tr></thead>"
            )
            out.append("<tbody>")
            for e in evs:
                transition = f"{e.from_state} → {e.to_state}"
                out.append("<tr>")
                out.append(f"<td>{_esc(e.event_type)}</td>")
                out.append(f"<td>{_esc(transition)}</td>")
                out.append(f"<td>{_esc(e.occurred_at)}</td>")
                out.append(f"<td>{_esc(e.event_id)}</td>")
                out.append(f"<td>{_esc(len(e.evidence_refs))}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
        out.append("</div>")
        # acceptance decisions
        accs = sorted(acc_by_task.get(t.task_id, []), key=lambda a: a.review_n)
        out.append('<div class="task-block">')
        out.append("<h3>Acceptance Decisions</h3>")
        if not accs:
            out.append('<p class="empty-state">No acceptances</p>')
        else:
            out.append('<div class="table-scroll"><table>')
            out.append("<caption>Acceptances (sorted by review number)</caption>")
            out.append(
                "<thead><tr><th>Decision</th><th>Accepted Commit</th>"
                "<th>Reviewed Dispatch ID</th><th>Review</th></tr></thead>"
            )
            out.append("<tbody>")
            for a in accs:
                out.append("<tr>")
                out.append(f"<td>{_esc(a.decision)}</td>")
                out.append(f"<td>{_esc(a.accepted_commit)}</td>")
                out.append(f"<td>{_esc(a.reviewed_dispatch_id)}</td>")
                out.append(f"<td>{_esc(a.review_n)}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
        out.append("</div>")
        # mad refs
        mrefs = sorted(
            mad_by_task.get(t.task_id, []),
            key=lambda m: m.deliberation_id,
        )
        out.append('<div class="task-block">')
        out.append("<h3>MAD References</h3>")
        if not mrefs:
            out.append('<p class="empty-state">No MAD references</p>')
        else:
            out.append('<div class="table-scroll"><table>')
            out.append("<caption>MAD references (sorted by deliberation id)</caption>")
            out.append(
                "<thead><tr><th>Purpose</th><th>Deliberation ID</th>"
                "<th>Status</th><th>Created At</th></tr></thead>"
            )
            out.append("<tbody>")
            for m in mrefs:
                out.append("<tr>")
                out.append(f"<td>{_esc(m.purpose)}</td>")
                out.append(f"<td>{_esc(m.deliberation_id)}</td>")
                out.append(f"<td>{_esc(m.status)}</td>")
                out.append(f"<td>{_esc(m.created_at)}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
        out.append("</div>")
        out.append("</article>")
    out.append("</section>")
    return out


def _system_health_section(
    snapshot: StateSnapshot, snapshot_digest: str, task_count: int
) -> list[str]:
    blocked_present = any(t.blocked_kind is not None for t in snapshot.tasks)
    pending_outbox = bool(snapshot.outbox)
    mad_count = 0 if snapshot.mad_refs is None else len(snapshot.mad_refs)
    out: list[str] = []
    out.append('<section id="system-health">')
    out.append("<h2>System Health</h2>")
    if task_count == 0:
        out.append('<p class="empty-state">No tasks</p>')
    out.append('<div class="table-scroll"><table>')
    out.append("<caption>System Health</caption>")
    out.append("<thead><tr><th>Metric</th><th>Value</th></tr></thead>")
    out.append("<tbody>")
    rows = (
        ("Snapshot Digest", snapshot_digest),
        ("Tasks Count", str(len(snapshot.tasks))),
        ("Events Count", str(len(snapshot.events))),
        ("Outbox Count", str(len(snapshot.outbox))),
        ("Acceptances Count", str(len(snapshot.acceptances))),
        ("MAD References Count", str(mad_count)),
        ("Blocked Tasks Present", "yes" if blocked_present else "no"),
        ("Pending Outbox Messages", "yes" if pending_outbox else "no"),
        ("Empty Project", "yes" if task_count == 0 else "no"),
        ("Snapshot Consistency", "validated by StateProvider"),
    )
    for label, val in rows:
        out.append(f"<tr><td>{_esc(label)}</td><td>{_esc(val)}</td></tr>")
    out.append("</tbody></table></div>")
    out.append("</section>")
    return out


def _build_html(
    snapshot: StateSnapshot,
    gen_str: str,
    snapshot_digest: str,
    task_count: int,
) -> bytes:
    lines: list[str] = []
    lines.append("<!doctype html>")
    lines.append('<html lang="en">')
    lines.append("<head>")
    lines.append('<meta charset="UTF-8">')
    lines.append(
        '<meta http-equiv="Content-Security-Policy" content="' + _CSP + '">'
    )
    lines.append(
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
    )
    lines.append(
        "<title>AgentDesk Dashboard — " + _esc(snapshot.project_id) + "</title>"
    )
    lines.append("<style>")
    lines.append(_CSS)
    lines.append("</style>")
    lines.append("</head>")
    lines.append("<body>")
    lines.append(
        '<a class="skip-link" href="' + _SKIP_LINK_HREF
        + '">Skip to main content</a>'
    )
    lines.append("<header>")
    lines.append("<h1>AgentDesk Operations Console</h1>")
    lines.append(
        '<p class="meta">Project ' + _esc(snapshot.project_id)
        + " | Snapshot updated " + _esc(snapshot.updated_at)
        + " | Generated " + _esc(gen_str) + "</p>"
    )
    lines.append("</header>")
    lines.append('<nav aria-label="Dashboard sections">')
    lines.append("<ul>")
    for label, href in _NAV_ITEMS:
        lines.append(
            '<li><a href="' + href + '">' + _esc(label) + "</a></li>"
        )
    lines.append("</ul>")
    lines.append("</nav>")
    lines.append('<main id="main-content" tabindex="-1">')
    lines.extend(_overview_section(snapshot, gen_str))
    lines.extend(_task_board_section(snapshot))
    lines.extend(_task_details_section(snapshot))
    lines.extend(_system_health_section(snapshot, snapshot_digest, task_count))
    lines.append("</main>")
    lines.append("</body>")
    lines.append("</html>")
    text = "\n".join(lines) + "\n"
    return text.encode("utf-8")


# ── security invariants (contract §6) ─────────────────────────────────────


def _enforce_security_invariants(html_bytes: bytes) -> None:
    text = html_bytes.decode("utf-8")
    if _CSP not in text:
        raise DashboardSecurityError("CSP invariant not met")
    if _SCRIPT_RE.search(text):
        raise DashboardSecurityError("forbidden script tag detected")
    if _FORM_RE.search(text):
        raise DashboardSecurityError("forbidden form tag detected")
    if _EVENT_ATTR_RE.search(text):
        raise DashboardSecurityError("forbidden event handler attribute detected")
    if _JS_URL_RE.search(text):
        raise DashboardSecurityError("javascript URL detected")
    if _HTTP_URL_RE.search(text):
        raise DashboardSecurityError("external URL detected")


# ── public render entrypoint ───────────────────────────────────────────────


def render_dashboard(request: DashboardRenderRequest) -> DashboardArtifact:
    """Render a read-only HTML dashboard from a StateSnapshot.

    Pure synchronous: identical requests produce byte-for-byte identical
    output.  Zero file, network, subprocess, clock, or git access.
    """
    if not isinstance(request, DashboardRenderRequest):
        raise DashboardInputError("request must be a DashboardRenderRequest instance")
    snapshot = request.snapshot
    generated_at = request.generated_at

    try:
        digest_hex = _compute_snapshot_digest(snapshot)
        snapshot_digest = "sha256:" + digest_hex
        gen_str = _format_rfc3339_utc(generated_at)
        task_count = len(snapshot.tasks)
        if isinstance(task_count, bool) or task_count < 0:
            raise DashboardRenderError("invalid task count")
        html_bytes = _build_html(snapshot, gen_str, snapshot_digest, task_count)
    except DashboardError:
        raise
    except Exception as exc:  # noqa: BLE001 — preserve as __cause__
        raise DashboardRenderError("internal render failure") from exc

    _enforce_security_invariants(html_bytes)

    return DashboardArtifact(
        html=html_bytes,
        snapshot_digest=snapshot_digest,
        generated_at=gen_str,
        task_count=task_count,
    )
