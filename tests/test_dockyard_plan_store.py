"""TC-13.29j durable PM plan-store tests."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import fields, replace
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from dockyard_plan_store import (  # noqa: E402
    DockyardPlanConflictError,
    DockyardPlanFilesystemError,
    DockyardPlanInputError,
    DockyardPlanPhase,
    DockyardPlanRecord,
    DockyardPlanStore,
    DockyardPlanTask,
)
from portfolio_scheduler_store import BusinessPriority  # noqa: E402
sys.path.pop(0)

_RATIONALE = (
    "scope.bounded", "clarity.explicit", "concurrency.single_writer",
    "contract.internal", "impact.informational", "rollback.simple",
    "dependency.none",
)


def _task(task_id: str = "TC-001", dependencies: tuple[str, ...] = ()) -> DockyardPlanTask:
    return DockyardPlanTask(
        task_id,
        "实现任务",
        "完成指定的本机实现。",
        dependencies,
        "parallel",
        "R1",
        "implementation",
        "codex",
        "gpt-5.6-sol",
        "standard",
        32000,
        3,
        "a" * 40,
        BusinessPriority.P1,
        _RATIONALE,
        None,
        None,
        None,
        "L0",
        ("coding",),
        None,
        0,
        1,
    )


def _record(revision: int = 1, previous: str | None = None) -> DockyardPlanRecord:
    return DockyardPlanRecord(
        "dockyard.pm-plan/v1",
        "PLAN-1",
        "PRJ-1",
        revision,
        DockyardPlanPhase.DRAFTED if revision == 1 else DockyardPlanPhase.EDITED,
        "实现本机功能",
        "b" * 64,
        (_task(),),
        "c" * 64,
        previous,
        "PM-1",
        "2026-08-02T00:00:00Z",
        "2026-08-02T00:00:00Z",
        None,
        None,
        f"OP-{revision}",
        "d" * 64,
        "0" * 64,
    )


class DockyardPlanStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DockyardPlanStore(Path(self.temp.name) / "plans")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_record_has_exact_eighteen_fields(self) -> None:
        self.assertEqual(len(fields(DockyardPlanRecord)), 18)
        self.assertEqual(len(fields(DockyardPlanTask)), 23)

    def test_save_read_and_latest(self) -> None:
        saved = self.store.save(_record())
        self.assertNotEqual(saved.content_digest, "0" * 64)
        self.assertEqual(self.store.read("PLAN-1", 1), saved)
        self.assertEqual(self.store.latest("PLAN-1"), saved)

    def test_current_session_recovers_unique_successor_after_materialized_base(self) -> None:
        previous = None
        phases = (
            DockyardPlanPhase.DRAFTED,
            DockyardPlanPhase.EDITED,
            DockyardPlanPhase.APPROVAL_PENDING,
            DockyardPlanPhase.APPROVED,
            DockyardPlanPhase.MATERIALIZED,
        )
        for revision, phase in enumerate(phases, start=1):
            saved = self.store.save(replace(
                _record(revision, previous),
                phase=phase,
                approved_at="2026-08-02T00:00:00Z" if revision >= 4 else None,
                materialized_at="2026-08-02T00:00:00Z" if revision == 5 else None,
            ))
            previous = saved.content_digest
        successor = self.store.save(replace(
            _record(),
            plan_id="PLAN-1-r6",
            phase=DockyardPlanPhase.DRAFTED,
        ))
        self.assertEqual(self.store.current_session("PLAN-1"), successor)

    def test_current_session_rejects_ambiguous_active_successors(self) -> None:
        self.store.save(replace(_record(), plan_id="PLAN-1-r6"))
        self.store.save(replace(_record(), plan_id="PLAN-1-r7"))
        with self.assertRaises(DockyardPlanConflictError):
            self.store.current_session("PLAN-1")

    def test_current_session_recovers_deepest_materialized_successor(self) -> None:
        base = self.store.save(replace(
            _record(), phase=DockyardPlanPhase.MATERIALIZED,
            approved_at="2026-08-02T00:00:00Z", materialized_at="2026-08-02T00:00:00Z",
        ))
        successor = self.store.save(replace(
            _record(), plan_id="PLAN-1-r6", phase=DockyardPlanPhase.MATERIALIZED,
            approved_at="2026-08-02T00:00:00Z", materialized_at="2026-08-02T00:00:00Z",
        ))
        self.assertNotEqual(base.plan_id, successor.plan_id)
        self.assertEqual(self.store.current_session("PLAN-1"), successor)

    def test_current_session_rejects_ambiguous_terminal_successors(self) -> None:
        for plan_id in ("PLAN-1-r6", "PLAN-1-r7"):
            self.store.save(replace(
                _record(), plan_id=plan_id, phase=DockyardPlanPhase.MATERIALIZED,
                approved_at="2026-08-02T00:00:00Z", materialized_at="2026-08-02T00:00:00Z",
            ))
        with self.assertRaises(DockyardPlanConflictError):
            self.store.current_session("PLAN-1")

    def test_byte_exact_replay_returns_same_record(self) -> None:
        saved = self.store.save(_record())
        self.assertEqual(self.store.save(_record()), saved)

    def test_divergent_same_revision_is_rejected(self) -> None:
        self.store.save(_record())
        with self.assertRaises(DockyardPlanConflictError):
            self.store.save(replace(_record(), requirement="divergent"))

    def test_revision_must_be_contiguous_and_bound_to_digest(self) -> None:
        first = self.store.save(_record())
        with self.assertRaises(DockyardPlanConflictError):
            self.store.save(_record(3, first.content_digest))
        with self.assertRaises(DockyardPlanConflictError):
            self.store.save(_record(2, "e" * 64))
        second = self.store.save(_record(2, first.content_digest))
        self.assertEqual(second.revision, 2)

    def test_pm_owner_cannot_change(self) -> None:
        first = self.store.save(_record())
        with self.assertRaises(DockyardPlanConflictError):
            self.store.save(replace(
                _record(2, first.content_digest),
                pm_owner_id="PM-2",
            ))

    def test_phase_cannot_skip_forward(self) -> None:
        first = self.store.save(_record())
        with self.assertRaises(DockyardPlanConflictError):
            self.store.save(replace(
                _record(2, first.content_digest),
                phase=DockyardPlanPhase.MATERIALIZED,
            ))

    def test_extra_record_field_is_rejected(self) -> None:
        saved = self.store.save(_record())
        path = self.store.root / "PLAN-1" / "r1.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["secret"] = "forbidden"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(DockyardPlanFilesystemError):
            self.store.read("PLAN-1", saved.revision)

    def test_attempt_four_is_rejected(self) -> None:
        with self.assertRaises(DockyardPlanInputError):
            replace(_task(), max_attempts=4)

    def test_admission_attempt_four_and_bool_are_prewrite_rejected(self) -> None:
        for changes in (
            {"expected_task_attempt": 3, "new_attempt": 4},
            {"expected_task_attempt": False, "new_attempt": 1},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(DockyardPlanInputError):
                    replace(_task(), **changes)
                self.assertEqual(tuple(self.store.root.iterdir()), ())

    def test_rationale_shape_and_priority_are_typed(self) -> None:
        for changes in (
            {"difficulty_rationale_keys": _RATIONALE[:-1]},
            {"difficulty_rationale_keys": tuple(reversed(_RATIONALE))},
            {"business_priority": "P1"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(DockyardPlanInputError):
                    replace(_task(), **changes)

    def test_attempt_three_round_trips_exactly(self) -> None:
        record = replace(
            _record(),
            tasks=(replace(_task(), expected_task_attempt=2, new_attempt=3),),
        )
        saved = self.store.save(record)
        self.assertEqual(saved.tasks[0].expected_task_attempt, 2)
        self.assertEqual(saved.tasks[0].new_attempt, 3)

    def test_existing_store_lock_fails_closed(self) -> None:
        (self.store.root / ".plan-store.lock").write_text("foreign", encoding="utf-8")
        with self.assertRaises(DockyardPlanConflictError):
            self.store.save(_record())

    def test_concurrent_divergent_revision_has_one_winner(self) -> None:
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def run(record: DockyardPlanRecord) -> None:
            barrier.wait(timeout=2)
            try:
                self.store.save(record)
                outcome = "winner"
            except DockyardPlanConflictError:
                outcome = "conflict"
            with guard:
                outcomes.append(outcome)

        records = (_record(), replace(_record(), requirement="另一份计划"))
        threads = [threading.Thread(target=run, args=(record,)) for record in records]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["conflict", "winner"])


if __name__ == "__main__":
    unittest.main()
