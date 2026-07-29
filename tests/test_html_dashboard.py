"""TC-13.20b — HTML Dashboard renderer targeted unit tests.

Covers the frozen contract (html-dashboard-contract.md) and the task
card TC-13.20b requirements: API surface, data-model shape, digest
determinism, HTML escaping, CSP, security invariants, zero I/O,
input immutability, encoding, responsive CSS, and status accessibility.

Tests exercise the real ``render_dashboard()`` output.  Source-string
assertions are used only where the task card explicitly requires a
boundary check (e.g. forbidden imports are not asserted here — that
is the smoke suite's job).
"""

from __future__ import annotations

import builtins
import inspect
import os
import sys
import unittest
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SKILL_SCRIPTS = _SKILL_ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))

from state_provider import (  # noqa: E402
    AcceptanceEntry, DispatchInfo, EventEntry, MadRefEntry,
    ModelSelectionSnapshot, OutboxEntry, OutboxPayload,
    StateSnapshot, TaskEntry, TaskTimestamps,
)
import html_dashboard as hd  # noqa: E402
from html_dashboard import (  # noqa: E402
    DashboardArtifact, DashboardInputError, DashboardRenderError,
    DashboardRenderRequest, DashboardSecurityError, render_dashboard,
)

_UTC = timezone.utc
_SHA40 = "0" * 40
_SHA256_HEX = "a" * 64

_FROZEN_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "script-src 'none'; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


# ── builders ───────────────────────────────────────────────────────────────


def _model_selection(**over):
    base = dict(
        required_model_tier="standard",
        required_model_capabilities=("code",),
        model_binding_id="bind-1",
        selected_model_provider="anthropic",
        selected_model_id="claude-sonnet-4-6",
        selected_model_tier="standard",
        selected_deliberation_tier="balanced",
        selected_context_window_tokens=200000,
        selected_model_capabilities=("code",),
        model_degradation_approval_id=None,
    )
    base.update(over)
    return ModelSelectionSnapshot(**base)


def _dispatch(**over):
    base = dict(
        dispatch_id="DSP-1",
        attempt_id="ATT-1",
        role_id="worker-1",
        base_commit=_SHA40,
        branch="feat-x",
        dispatched_at="2026-07-29T09:00:00Z",
        model_selection=_model_selection(),
    )
    base.update(over)
    return DispatchInfo(**base)


def _payload(**over):
    base = dict(
        task_path="docs/t.md",
        task_card_commit=_SHA40,
        base_commit=_SHA40,
        branch="feat-x",
        report_path="docs/r.md",
    )
    base.update(over)
    return OutboxPayload(**base)


def _timestamps(**over):
    base = dict(
        created_at="2026-07-29T08:00:00Z",
        ready_at="2026-07-29T08:10:00Z",
        dispatched_at="2026-07-29T09:00:00Z",
        started_at="2026-07-29T09:05:00Z",
        delivered_at=None,
        blocked_at=None,
        accepted_at=None,
        integrated_at=None,
        updated_at="2026-07-29T10:00:00Z",
    )
    base.update(over)
    return TaskTimestamps(**base)


def _task(task_id="TC-001", **over):
    base = dict(
        task_id=task_id,
        revision=1,
        task_card_path="docs/pm/cards/TC-001.md",
        task_card_commit=_SHA40,
        state="ready",
        attempt=None,
        current_dispatch=None,
        report_path=None,
        granted_approval_ids=None,
        delivery_state="none",
        integration_state="not_applicable",
        implementation_commit=None,
        report_commit=None,
        accepted_commit=None,
        acceptance_path=None,
        integrated_commit=None,
        blocked_reason=None,
        blocked_kind=None,
        blocked_owner=None,
        unblock_condition=None,
        review_after=None,
        blocked_attempt_valid=None,
        resume_state=None,
        timestamps=_timestamps(),
        superseded_by=None,
    )
    base.update(over)
    return TaskEntry(**base)


def _event(event_id="EVT-1", **over):
    base = dict(
        schema_version="agentdesk.state-event/v2",
        event_id=event_id,
        event_type="TASK_SPECIFIED",
        task_id="TC-001",
        revision=1,
        attempt=None,
        dispatch_id=None,
        from_state="draft",
        to_state="ready",
        lease_epoch=1,
        actor_role_id="pm-1",
        occurred_at="2026-07-29T08:10:00Z",
        source_message_id=None,
        evidence_refs=(),
        guard_results=(),
        payload_digest=None,
        extra_fields=None,
    )
    base.update(over)
    return EventEntry(**base)


def _outbox(message_id="MSG-1", **over):
    base = dict(
        schema_version="agentdesk.outbox-message/v2",
        message_id=message_id,
        event_id="EVT-1",
        message_type="TASK_DISPATCHED",
        dedupe_key="dk-1",
        task_id="TC-001",
        revision=1,
        attempt=1,
        dispatch_id="DSP-1",
        destination_role_id="worker-1",
        created_at="2026-07-29T09:00:00Z",
        model_selection=_model_selection(),
        payload=_payload(),
    )
    base.update(over)
    return OutboxEntry(**base)


def _acceptance(task_id="TC-001", review_n=1, **over):
    base = dict(
        schema_version="agentdesk.acceptance/v2",
        task_id=task_id,
        revision=1,
        attempt=1,
        review_n=review_n,
        decision="accepted",
        implementation_commit=_SHA40,
        report_commit=_SHA40,
        base_commit=_SHA40,
        accepted_commit=_SHA40,
        reviewed_dispatch_id="DSP-1",
        type="implementation",
        owner_approval_gate="none",
        owner_approval_ids=(),
        raw_body="",
        filename="TC-001-r1-a1-review1.md",
    )
    base.update(over)
    return AcceptanceEntry(**base)


def _mad_ref(task_id="TC-001", **over):
    base = dict(
        task_id=task_id,
        dispatch_id="DSP-1",
        purpose="planning",
        deliberation_id="DEL-1",
        depth="fast",
        stdout_sha256="b" * 64,
        report_sha256="c" * 64,
        status="complete",
        archive_path="C:/abs/archive.zip",
        created_at="2026-07-29T09:30:00Z",
    )
    base.update(over)
    return MadRefEntry(**base)


def _snapshot(
    tasks=(),
    events=(),
    outbox=(),
    acceptances=(),
    mad_refs=None,
    project_id="demo",
    updated_at="2026-07-29T10:00:00Z",
    read_hexsha=_SHA256_HEX,
    **over,
):
    base = dict(
        project_root=Path("C:/proj"),
        schema_version="agentdesk.tasks/v2",
        project_id=project_id,
        adoption_level="standard",
        updated_at=updated_at,
        pm_holder_id="pm-1",
        pm_lease_epoch=1,
        pm_mode="manual",
        tasks=tuple(tasks),
        events=tuple(events),
        outbox=tuple(outbox),
        acceptances=tuple(acceptances),
        mad_refs=None if mad_refs is None else tuple(mad_refs),
        read_hexsha=read_hexsha,
    )
    base.update(over)
    return StateSnapshot(**base)


def _req(snap, gen=None):
    if gen is None:
        gen = datetime(2026, 7, 29, 12, 0, 0, tzinfo=_UTC)
    return DashboardRenderRequest(snapshot=snap, generated_at=gen)


def _rich_snapshot():
    t1 = _task(
        task_id="TC-001",
        state="dispatched",
        attempt=1,
        current_dispatch=_dispatch(),
        delivery_state="working",
        integration_state="pending",
    )
    t2 = _task(
        task_id="TC-002",
        state="blocked",
        blocked_kind="dependency",
        blocked_reason="waiting on upstream",
    )
    ev1 = _event(
        event_id="EVT-1",
        event_type="TASK_DISPATCHED",
        task_id="TC-001",
        from_state="ready",
        to_state="dispatched",
        dispatch_id="DSP-1",
        attempt=1,
        occurred_at="2026-07-29T09:00:00Z",
        evidence_refs=("EVT-1-ev0", "EVT-1-ev1"),
    )
    ev2 = _event(
        event_id="EVT-2",
        event_type="TASK_BLOCKED",
        task_id="TC-002",
        from_state="ready",
        to_state="blocked",
        occurred_at="2026-07-29T09:30:00Z",
        evidence_refs=(),
    )
    ob = _outbox()
    acc = _acceptance(task_id="TC-001", review_n=1)
    mr = _mad_ref(task_id="TC-001")
    return _snapshot(
        tasks=(t1, t2),
        events=(ev1, ev2),
        outbox=(ob,),
        acceptances=(acc,),
        mad_refs=(mr,),
    )


# ── tests ──────────────────────────────────────────────────────────────────


class _StructureParser(HTMLParser):
    """Parse real rendered HTML to collect anchors, nav/main attrs,
    section ids, and tag order — used instead of source-string matching."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict] = []
        self.nav_attrs: dict | None = None
        self.main_attrs: dict | None = None
        self.section_ids: list[str] = []
        self.tag_order: list[tuple[str, dict]] = []
        self.all_attr_names: set[str] = set()
        self._nav_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        d = dict(attrs)
        self.tag_order.append((tag, d))
        for name in d:
            self.all_attr_names.add(name)
        if tag == "nav":
            self._nav_depth += 1
            if self.nav_attrs is None:
                self.nav_attrs = d
        elif tag == "main":
            if self.main_attrs is None:
                self.main_attrs = d
        elif tag == "section":
            sid = d.get("id")
            if sid:
                self.section_ids.append(sid)
        elif tag == "a":
            self.anchors.append({
                "href": d.get("href"),
                "classes": (d.get("class") or "").split(),
                "in_nav": self._nav_depth > 0,
            })

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self._nav_depth -= 1


class TC1320b1HtmlDashboardNavigationTests(unittest.TestCase):
    """TC-13.20b.1 — keyboard navigation fix for the HTML Dashboard.

    Every assertion inspects the real ``render_dashboard()`` output via
    ``html.parser`` rather than source-string matching, except where a
    content/CSS ban can only be expressed as a text check.
    """

    _ALLOWED_HREFS = {
        "#main-content", "#overview", "#task-board",
        "#task-details", "#system-health",
    }
    _SECTION_IDS = ("overview", "task-board", "task-details", "system-health")

    def setUp(self) -> None:
        self._art = render_dashboard(_req(_rich_snapshot()))
        self._text = self._art.html.decode("utf-8")
        self._p = _StructureParser()
        self._p.feed(self._text)

    # 1. exactly one skip link
    def test_exactly_one_skip_link(self) -> None:
        skips = [a for a in self._p.anchors if "skip-link" in a["classes"]]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["href"], "#main-content")

    # 2. nav contains exactly 4 anchors
    def test_nav_exactly_four_anchors(self) -> None:
        nav_anchors = [a for a in self._p.anchors if a["in_nav"]]
        self.assertEqual(len(nav_anchors), 4)

    # 3. all hrefs match the allow-list exactly
    def test_href_set_matches_allowlist(self) -> None:
        hrefs = {a["href"] for a in self._p.anchors}
        self.assertEqual(hrefs, self._ALLOWED_HREFS)
        for href in hrefs:
            self.assertTrue(href.startswith("#"))

    # 4. nav has aria-label="Dashboard sections"
    def test_nav_aria_label(self) -> None:
        self.assertIsNotNone(self._p.nav_attrs)
        self.assertEqual(self._p.nav_attrs.get("aria-label"), "Dashboard sections")

    # 5. main has id="main-content"
    def test_main_has_main_content_id(self) -> None:
        self.assertIsNotNone(self._p.main_attrs)
        self.assertEqual(self._p.main_attrs.get("id"), "main-content")

    # 6. main has tabindex="-1"
    def test_main_has_tabindex_minus_one(self) -> None:
        self.assertIsNotNone(self._p.main_attrs)
        self.assertEqual(self._p.main_attrs.get("tabindex"), "-1")

    # 7. four section ids all present
    def test_four_section_ids_present(self) -> None:
        self.assertEqual(set(self._p.section_ids), set(self._SECTION_IDS))

    # 8. skip link precedes header, nav, and main
    def test_skip_link_precedes_header_nav_main(self) -> None:
        order = self._p.tag_order
        skip_idx = next(i for i, (t, d) in enumerate(order)
                        if t == "a" and "skip-link" in (d.get("class") or "").split())
        header_idx = next(i for i, (t, _) in enumerate(order) if t == "header")
        nav_idx = next(i for i, (t, _) in enumerate(order) if t == "nav")
        main_idx = next(i for i, (t, _) in enumerate(order) if t == "main")
        self.assertLess(skip_idx, header_idx)
        self.assertLess(header_idx, nav_idx)
        self.assertLess(nav_idx, main_idx)

    # 9. skip-link focus CSS exists
    def test_skip_link_focus_css_exists(self) -> None:
        self.assertIn(".skip-link:focus", self._text)

    # 10. :focus-visible outline is not none/0
    def test_focus_visible_outline_not_none(self) -> None:
        self.assertIn(":focus-visible", self._text)
        self.assertIn("outline:2px solid", self._text)
        self.assertNotIn("outline:none", self._text)
        self.assertNotIn("outline:0", self._text)

    # 11. no external href
    def test_no_external_href(self) -> None:
        for a in self._p.anchors:
            href = a["href"] or ""
            self.assertTrue(href.startswith("#"), f"non-fragment href: {href}")
            for bad in ("http://", "https://", "//", "data:", "ftp:", "javascript:"):
                self.assertNotIn(bad, href, f"banned scheme in href: {href}")

    # 12. no javascript: URL
    def test_no_javascript_url(self) -> None:
        self.assertNotIn("javascript:", self._text.lower())

    # 13. no event-handler attributes, no target="_blank"
    def test_no_event_handler_attributes(self) -> None:
        for name in self._p.all_attr_names:
            self.assertFalse(
                name.startswith("on"),
                f"forbidden event-handler attribute: {name}",
            )
        self.assertNotIn("target", self._p.all_attr_names)

    # 14. malicious snapshot fields cannot inject extra anchors
    def test_malicious_snapshot_cannot_inject_anchors(self) -> None:
        evil = '<a href="#evil">inject</a>'
        snap = _snapshot(
            tasks=(_task(task_id=evil, state="ready"),),
            project_id=evil,
        )
        art = render_dashboard(_req(snap))
        p = _StructureParser()
        p.feed(art.html.decode("utf-8"))
        # still exactly 1 skip link + 4 nav anchors = 5 total
        self.assertEqual(len(p.anchors), 5)
        hrefs = {a["href"] for a in p.anchors}
        self.assertEqual(hrefs, self._ALLOWED_HREFS)
        self.assertNotIn("#evil", hrefs)
        # the raw malicious markup is escaped, never present as a live tag
        self.assertIn("&lt;a href=&quot;#evil&quot;&gt;", art.html.decode("utf-8"))

    # 15. same request still produces byte-identical HTML
    def test_byte_identical_for_same_request(self) -> None:
        req = _req(_rich_snapshot())
        self.assertEqual(render_dashboard(req).html, render_dashboard(req).html)

    # 16. snapshot digest is independent of navigation static HTML
    def test_snapshot_digest_independent_of_nav_html(self) -> None:
        snap = _rich_snapshot()
        art = render_dashboard(_req(snap))
        self.assertEqual(
            art.snapshot_digest,
            "sha256:" + hd._compute_snapshot_digest(snap),
        )

    # 17. __all__ still exactly 7 symbols
    def test_all_still_exactly_seven(self) -> None:
        expected = {
            "DashboardRenderRequest", "DashboardArtifact", "render_dashboard",
            "DashboardError", "DashboardInputError",
            "DashboardRenderError", "DashboardSecurityError",
        }
        self.assertEqual(set(hd.__all__), expected)
        self.assertEqual(len(hd.__all__), 7)

    # 18. CSP exactly unchanged
    def test_csp_exactly_unchanged(self) -> None:
        self.assertIn(_FROZEN_CSP, self._text)
        self.assertIn('http-equiv="Content-Security-Policy"', self._text)


class TC1320bHtmlDashboardUnitTests(unittest.TestCase):

    # 1. __all__ exactly 7 symbols
    def test_all_exactly_seven_symbols(self):
        expected = {
            "DashboardRenderRequest",
            "DashboardArtifact",
            "render_dashboard",
            "DashboardError",
            "DashboardInputError",
            "DashboardRenderError",
            "DashboardSecurityError",
        }
        self.assertEqual(set(hd.__all__), expected)
        self.assertEqual(len(hd.__all__), 7)

    # 2. Request exactly 2 fields
    def test_request_exactly_2_fields(self):
        names = {f.name for f in fields(DashboardRenderRequest)}
        self.assertEqual(names, {"snapshot", "generated_at"})

    # 3. Artifact exactly 4 fields
    def test_artifact_exactly_4_fields(self):
        names = {f.name for f in fields(DashboardArtifact)}
        self.assertEqual(names, {"html", "snapshot_digest", "generated_at", "task_count"})

    # 4. Both dataclasses are frozen + slots
    def test_dataclasses_frozen_and_slots(self):
        for cls in (DashboardRenderRequest, DashboardArtifact):
            self.assertTrue(is_dataclass(cls))
            self.assertTrue(cls.__dataclass_params__.frozen, f"{cls} must be frozen")
            self.assertTrue(hasattr(cls, "__slots__"), f"{cls} must have __slots__")
        self.assertEqual(
            set(DashboardRenderRequest.__slots__), {"snapshot", "generated_at"}
        )
        self.assertEqual(
            set(DashboardArtifact.__slots__),
            {"html", "snapshot_digest", "generated_at", "task_count"},
        )

    # 5. Artifact.generated_at is a string
    def test_artifact_generated_at_is_str(self):
        art = render_dashboard(_req(_rich_snapshot()))
        self.assertIsInstance(art.generated_at, str)
        self.assertEqual(art.generated_at, "2026-07-29T12:00:00Z")

    # 6. render_dashboard is synchronous
    def test_render_dashboard_is_sync(self):
        self.assertFalse(inspect.iscoroutinefunction(render_dashboard))
        sig = inspect.signature(render_dashboard)
        self.assertEqual(list(sig.parameters), ["request"])

    # 7. Valid snapshot returns bytes
    def test_render_returns_bytes(self):
        art = render_dashboard(_req(_rich_snapshot()))
        self.assertIsInstance(art, DashboardArtifact)
        self.assertIsInstance(art.html, bytes)
        self.assertGreater(len(art.html), 0)

    # 8. Same request -> byte-for-byte identical HTML
    def test_deterministic_html_bytes(self):
        req = _req(_rich_snapshot())
        a1 = render_dashboard(req)
        a2 = render_dashboard(req)
        self.assertEqual(a1.html, a2.html)

    # 9. Same snapshot -> same digest
    def test_digest_consistent_for_same_snapshot(self):
        snap = _rich_snapshot()
        d1 = render_dashboard(_req(snap)).snapshot_digest
        d2 = render_dashboard(_req(snap)).snapshot_digest
        self.assertEqual(d1, d2)
        self.assertTrue(d1.startswith("sha256:"))
        self.assertEqual(len(d1), len("sha256:") + 64)
        self.assertTrue(d1[len("sha256:"):].islower())

    # 10. Different snapshot content -> different digest
    def test_different_content_different_digest(self):
        d1 = render_dashboard(_req(_rich_snapshot())).snapshot_digest
        snap2 = _snapshot(tasks=(_task(task_id="TC-001", state="blocked"),))
        d2 = render_dashboard(_req(snap2)).snapshot_digest
        self.assertNotEqual(d1, d2)

    # 11. Empty task board shows "No tasks"
    def test_empty_tasks_shows_no_tasks(self):
        art = render_dashboard(_req(_snapshot(tasks=())))
        self.assertIn(b"No tasks", art.html)

    # 12. Required page sections present
    def test_required_sections_present(self):
        art = render_dashboard(_req(_rich_snapshot()))
        for sid in (b'id="overview"', b'id="task-board"',
                    b'id="task-details"', b'id="system-health"'):
            self.assertIn(sid, art.html, f"missing section {sid}")
        for tag in (b"<header", b"<nav", b"<main", b"<table",
                    b"<caption", b"<thead", b"<tbody", b"<h1", b"<h2", b"<h3"):
            self.assertIn(tag, art.html, f"missing semantic tag {tag}")

    # 13. Task Board required fields present
    def test_task_board_required_fields_present(self):
        art = render_dashboard(_req(_rich_snapshot()))
        for header in (b"Task ID", b"State", b"Revision", b"Attempt",
                       b"Delivery", b"Integration", b"Worker Role",
                       b"Updated At", b"Blocked Kind", b"Superseded By"):
            self.assertIn(header, art.html, f"task board missing column {header}")
        self.assertIn(b"TC-001", art.html)
        self.assertIn(b"TC-002", art.html)
        self.assertIn(b"dependency", art.html)

    # 14. HTML escaping covers & < > " '
    def test_html_escaping_covers_special_chars(self):
        bomb = 'a&b<c>d"e\'f'
        snap = _snapshot(
            tasks=(_task(task_id="TC-001", state="ready"),),
            project_id=bomb,
        )
        art = render_dashboard(_req(snap))
        self.assertIn(b"a&amp;b&lt;c&gt;d&quot;e&#x27;f", art.html)
        # raw unescaped concatenation must not appear
        self.assertNotIn(bomb.encode("utf-8"), art.html)

    # 15. Injected <script> never enters raw HTML
    def test_script_injection_neutralized(self):
        evil = "<script>alert(1)</script>"
        snap = _snapshot(
            tasks=(_task(task_id=evil, state="ready"),),
            project_id="p",
        )
        art = render_dashboard(_req(snap))
        self.assertNotIn(b"<script", art.html)
        self.assertIn(b"&lt;script&gt;", art.html)

    # 16. CSP exactly present
    def test_csp_exactly_present(self):
        art = render_dashboard(_req(_snapshot(tasks=())))
        self.assertIn(_FROZEN_CSP.encode("utf-8"), art.html)
        self.assertIn(b'http-equiv="Content-Security-Policy"', art.html)

    # 17. No script / form / fetch / WebSocket / event attrs
    def test_no_forbidden_active_elements(self):
        art = render_dashboard(_req(_rich_snapshot()))
        lower = art.html.lower()
        self.assertNotIn(b"<script", art.html)
        self.assertNotIn(b"<form", art.html)
        self.assertNotIn(b"fetch(", art.html)
        self.assertNotIn(b"websocket", lower)
        self.assertNotIn(b"onclick=", lower)
        self.assertNotIn(b"onload=", lower)
        self.assertNotIn(b"javascript:", lower)

    # 18. Non-StateSnapshot rejected
    def test_non_state_snapshot_rejected(self):
        with self.assertRaises(DashboardInputError):
            DashboardRenderRequest(
                snapshot=object(), generated_at=datetime(2026, 7, 29, tzinfo=_UTC)
            )
        with self.assertRaises(DashboardInputError):
            render_dashboard({"snapshot": "not a request"})  # type: ignore

    # 19. Naive datetime rejected
    def test_naive_datetime_rejected(self):
        with self.assertRaises(DashboardInputError):
            DashboardRenderRequest(
                snapshot=_snapshot(),
                generated_at=datetime(2026, 7, 29, 12, 0, 0),
            )

    # 20. Non-UTC offset rejected
    def test_non_utc_offset_rejected(self):
        cst = timezone(timedelta(hours=8))
        with self.assertRaises(DashboardInputError):
            DashboardRenderRequest(
                snapshot=_snapshot(),
                generated_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=cst),
            )

    # 21. task_count is int, not bool
    def test_task_count_not_bool(self):
        art = render_dashboard(_req(_rich_snapshot()))
        self.assertIsInstance(art.task_count, int)
        self.assertNotIsInstance(art.task_count, bool)
        self.assertGreater(art.task_count, 0)

    # 22. Exception messages do not leak malicious input
    def test_exception_messages_do_not_leak(self):
        class _Leak:
            def __repr__(self):
                return "LEAKED-SECRET-XYZ"

            def __str__(self):
                return "LEAKED-SECRET-XYZ"

        with self.assertRaises(DashboardInputError) as cm:
            DashboardRenderRequest(
                snapshot=_Leak(),
                generated_at=datetime(2026, 7, 29, tzinfo=_UTC),
            )
        self.assertNotIn("LEAKED-SECRET-XYZ", str(cm.exception))
        self.assertNotIn("LEAKED-SECRET-XYZ", repr(cm.exception))

    # 23. Malicious repr is never called during render
    def test_malicious_repr_not_called(self):
        class _ReprBomb(str):
            def __repr__(self):
                raise AssertionError("repr invoked on tainted field")

        tainted = _ReprBomb("TC-001")
        snap = _snapshot(
            tasks=(_task(task_id=tainted, state="ready"),),
            project_id=_ReprBomb("demo"),
        )
        # Must not raise — render must not call repr() on input fields.
        art = render_dashboard(_req(snap))
        self.assertIsInstance(art.html, bytes)

    # 24. Render performs no file I/O
    def test_render_no_file_io(self):
        real_open = builtins.open
        real_os_open = os.open

        def _guard_open(*args, **kwargs):
            raise AssertionError("builtins.open called during render")

        def _guard_os_open(*args, **kwargs):
            raise AssertionError("os.open called during render")

        builtins.open = _guard_open  # type: ignore
        os.open = _guard_os_open  # type: ignore
        try:
            art = render_dashboard(_req(_rich_snapshot()))
        finally:
            builtins.open = real_open  # type: ignore
            os.open = real_os_open  # type: ignore
        self.assertIsInstance(art.html, bytes)

    # 25. Input objects are not mutated
    def test_input_not_mutated(self):
        snap = _rich_snapshot()
        req = _req(snap)
        tasks_before = snap.tasks
        events_before = snap.events
        gen_before = req.generated_at
        render_dashboard(req)
        self.assertIs(req.snapshot, snap)
        self.assertIs(snap.tasks, tasks_before)
        self.assertIs(snap.events, events_before)
        self.assertEqual(snap.tasks, tasks_before)
        self.assertEqual(req.generated_at, gen_before)

    # 26. Output is UTF-8, LF-only, exactly one trailing newline
    def test_output_encoding(self):
        art = render_dashboard(_req(_rich_snapshot()))
        art.html.decode("utf-8")  # raises if not valid UTF-8
        self.assertNotIn(b"\r", art.html)
        self.assertTrue(art.html.endswith(b"\n"))
        self.assertFalse(art.html.endswith(b"\n\n"))
        self.assertTrue(art.html.startswith(b"<!doctype html>"))

    # 27. Responsive 320px CSS rule present
    def test_responsive_css_present(self):
        art = render_dashboard(_req(_rich_snapshot()))
        self.assertIn(b"@media (max-width:320px)", art.html)
        self.assertIn(b"prefers-reduced-motion", art.html)
        self.assertIn(b":focus-visible", art.html)

    # 28. Status display includes text and symbol
    def test_status_shows_text_and_symbol(self):
        snap = _snapshot(tasks=(
            _task(task_id="TC-001", state="blocked", blocked_kind="dependency"),
            _task(task_id="TC-002", state="dispatched", attempt=1,
                  current_dispatch=_dispatch()),
        ))
        art = render_dashboard(_req(snap))
        # each badge renders the state word as text content (before </span>)
        self.assertIn(b"blocked</span>", art.html)
        self.assertIn(b"dispatched</span>", art.html)
        # a non-empty symbol precedes the text inside the badge
        self.assertIn(b'state state-blocked">', art.html)
        self.assertIn(b'state state-dispatched">', art.html)
        text = art.html.decode("utf-8")
        # badge content includes a non-ASCII symbol char
        badge_start = text.find('state state-blocked">')
        snippet = text[badge_start:badge_start + 40]
        self.assertTrue(any(ord(ch) > 127 for ch in snippet))


if __name__ == "__main__":
    unittest.main()
