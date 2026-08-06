"""TC-13.29j PM requirement/plan runtime tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from core_types import TaskDifficulty  # noqa: E402
from dockyard_plan_store import (  # noqa: E402
    DockyardPlanConflictError,
    DockyardPlanInputError,
    DockyardPlanPhase,
    DockyardPlanStore,
    DockyardPlanTask,
)
from dockyard_pm_service import (  # noqa: E402
    DockyardPlanApprovalRequest,
    DockyardPlanDraftRequest,
    DockyardPlanEditRequest,
    DockyardPlanMaterializeRequest,
    DockyardPlanTransitionRequest,
    DockyardPmService,
    _task_digest,
)
from portfolio_scheduler_store import BusinessPriority  # noqa: E402
sys.path.pop(0)

_RATIONALE = (
    "scope.bounded", "clarity.explicit", "concurrency.single_writer",
    "contract.internal", "impact.informational", "rollback.simple",
    "dependency.none",
)


def _task(
    task_id: str,
    dependencies: tuple[str, ...] = (),
    *,
    provider: str = "codex",
) -> DockyardPlanTask:
    return DockyardPlanTask(
        task_id,
        f"任务 {task_id}",
        "执行本机、确定性的实现。",
        dependencies,
        "parallel" if not dependencies else "serial",
        "R1",
        "implementation",
        provider,
        "gpt-5.6-sol" if provider == "codex" else "deepseek-v4-flash",
        "standard" if provider == "codex" else "basic",
        24000,
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


class DockyardPmServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cards = root / "docs" / "pm" / "tasks"
        self.service = DockyardPmService(
            DockyardPlanStore(root / ".agentdesk" / "runtime" / "plans"),
            self.cards,
        )
        self.tasks = (
            _task("TC-001"),
            _task("TC-002", ("TC-001",), provider="reasonix"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def draft(self):
        return self.service.draft(DockyardPlanDraftRequest(
            "PLAN-1",
            "PRJ-1",
            "实现 Dockyard 本机闭环",
            self.tasks,
            "PM-1",
            "2026-08-02T00:00:00Z",
            "OP-DRAFT",
        ))

    def pending(self):
        draft = self.draft()
        return self.service.request_approval(DockyardPlanTransitionRequest(
            draft.plan_id,
            draft.revision,
            draft.content_digest,
            "2026-08-02T00:01:00Z",
            "OP-PENDING",
        ))

    def approved(self):
        pending = self.pending()
        return self.service.approve(DockyardPlanApprovalRequest(
            pending.plan_id,
            pending.revision,
            pending.content_digest,
            pending.plan_digest,
            len(pending.tasks),
            "2026-08-02T00:02:00Z",
            "CONF-1",
            "OP-APPROVE",
        ))

    def test_full_forward_lifecycle_and_materialization(self) -> None:
        draft = self.draft()
        self.assertEqual(draft.phase, DockyardPlanPhase.DRAFTED)
        self.assertEqual(tuple(self.cards.iterdir()), ())
        edited = self.service.edit(DockyardPlanEditRequest(
            draft.plan_id,
            draft.revision,
            draft.content_digest,
            "实现 Dockyard 本机可视化闭环",
            self.tasks,
            "2026-08-02T00:00:30Z",
            "OP-EDIT",
        ))
        pending = self.service.request_approval(DockyardPlanTransitionRequest(
            edited.plan_id,
            edited.revision,
            edited.content_digest,
            "2026-08-02T00:01:00Z",
            "OP-PENDING",
        ))
        approved = self.service.approve(DockyardPlanApprovalRequest(
            pending.plan_id,
            pending.revision,
            pending.content_digest,
            pending.plan_digest,
            2,
            "2026-08-02T00:02:00Z",
            "CONF-1",
            "OP-APPROVE",
        ))
        result = self.service.materialize(DockyardPlanMaterializeRequest(
            approved.plan_id,
            approved.revision,
            approved.content_digest,
            approved.plan_digest,
            "2026-08-02T00:03:00Z",
            "OP-MATERIALIZE",
        ))
        self.assertEqual(result.plan.phase, DockyardPlanPhase.MATERIALIZED)
        self.assertEqual(result.task_card_paths, (
            "TC-001-r1-dockyard.md",
            "TC-002-r1-dockyard.md",
        ))
        self.assertEqual(len(tuple(self.cards.iterdir())), 2)

    def test_materialize_byte_exact_replay(self) -> None:
        approved = self.approved()
        request = DockyardPlanMaterializeRequest(
            approved.plan_id,
            approved.revision,
            approved.content_digest,
            approved.plan_digest,
            "2026-08-02T00:03:00Z",
            "OP-MATERIALIZE",
        )
        first = self.service.materialize(request)
        second = self.service.materialize(request)
        self.assertTrue(second.replayed)
        self.assertEqual(first.plan, second.plan)

    def test_no_materialization_before_approval(self) -> None:
        draft = self.draft()
        with self.assertRaises(DockyardPlanConflictError):
            self.service.materialize(DockyardPlanMaterializeRequest(
                draft.plan_id,
                draft.revision,
                draft.content_digest,
                draft.plan_digest,
                "2026-08-02T00:03:00Z",
                "OP-EARLY",
            ))
        self.assertEqual(tuple(self.cards.iterdir()), ())

    def test_approval_confirmation_must_bind_digest_and_count(self) -> None:
        pending = self.pending()
        with self.assertRaises(DockyardPlanConflictError):
            self.service.approve(DockyardPlanApprovalRequest(
                pending.plan_id,
                pending.revision,
                pending.content_digest,
                "e" * 64,
                99,
                "2026-08-02T00:02:00Z",
                "CONF-1",
                "OP-APPROVE",
            ))

    def test_edit_after_approval_creates_new_revision(self) -> None:
        approved = self.approved()
        edited = self.service.edit(DockyardPlanEditRequest(
            approved.plan_id,
            approved.revision,
            approved.content_digest,
            "修改后的需求",
            self.tasks,
            "2026-08-02T00:04:00Z",
            "OP-REVISE",
        ))
        self.assertEqual(edited.revision, approved.revision + 1)
        self.assertEqual(edited.phase, DockyardPlanPhase.EDITED)
        self.assertIsNone(edited.approved_at)

    def test_edit_while_approval_pending_is_rejected(self) -> None:
        pending = self.pending()
        with self.assertRaises(DockyardPlanConflictError):
            self.service.edit(DockyardPlanEditRequest(
                pending.plan_id,
                pending.revision,
                pending.content_digest,
                pending.requirement,
                pending.tasks,
                "2026-08-02T00:04:00Z",
                "OP-EDIT-LATE",
            ))

    def test_dependency_cycle_is_rejected(self) -> None:
        tasks = (
            _task("TC-001", ("TC-002",)),
            _task("TC-002", ("TC-001",)),
        )
        with self.assertRaises(DockyardPlanInputError):
            self.service.draft(replace(
                DockyardPlanDraftRequest(
                    "PLAN-1", "PRJ-1", "循环计划", tasks, "PM-1",
                    "2026-08-02T00:00:00Z", "OP-DRAFT",
                ),
                tasks=tasks,
            ))

    def test_single_pm_owner_is_fenced(self) -> None:
        draft = self.draft()
        with self.assertRaises(DockyardPlanConflictError):
            self.service.draft(DockyardPlanDraftRequest(
                draft.plan_id,
                draft.project_id,
                draft.requirement,
                draft.tasks,
                "PM-2",
                "2026-08-02T00:00:00Z",
                "OP-OTHER",
            ))

    def test_partial_card_crash_replay_is_byte_exact(self) -> None:
        approved = self.approved()
        first_name = self.service._write_card(approved.tasks[0])
        first_bytes = (self.cards / first_name).read_bytes()
        result = self.service.materialize(DockyardPlanMaterializeRequest(
            approved.plan_id,
            approved.revision,
            approved.content_digest,
            approved.plan_digest,
            "2026-08-02T00:03:00Z",
            "OP-MATERIALIZE",
        ))
        self.assertEqual((self.cards / first_name).read_bytes(), first_bytes)
        self.assertEqual(result.plan.phase, DockyardPlanPhase.MATERIALIZED)

    def test_task_cards_are_versioned_and_do_not_dispatch(self) -> None:
        approved = self.approved()
        result = self.service.materialize(DockyardPlanMaterializeRequest(
            approved.plan_id,
            approved.revision,
            approved.content_digest,
            approved.plan_digest,
            "2026-08-02T00:03:00Z",
            "OP-MATERIALIZE",
        ))
        card = (self.cards / result.task_card_paths[0]).read_text(encoding="utf-8")
        self.assertIn("schema_version: agentdesk.task-card/v2", card)
        self.assertIn("revision: 1", card)
        self.assertIn("- business_priority: P1", card)
        self.assertIn(
            "- difficulty_rationale_keys: " + ", ".join(_RATIONALE), card
        )
        self.assertIn("- risk: L0", card)
        self.assertIn("- task_capabilities: coding", card)
        self.assertIn("- expected_task_attempt: 0", card)
        self.assertIn("- new_attempt: 1", card)
        self.assertNotIn("TASK_DISPATCHED", card)

    def test_each_admission_input_changes_plan_digest(self) -> None:
        baseline = self.draft().plan_digest
        downward = replace(
            self.tasks[0],
            difficulty_rationale_keys=("scope.single_module", *_RATIONALE[1:]),
            selected_difficulty=TaskDifficulty.BASIC,
            difficulty_override_reason="pm_known_pattern",
            difficulty_approval_id="APR-1",
        )
        variants = (
            replace(self.tasks[0], business_priority=BusinessPriority.P2),
            replace(self.tasks[0], difficulty_rationale_keys=(
                "scope.single_module", *_RATIONALE[1:]
            )),
            replace(self.tasks[0], selected_difficulty=TaskDifficulty.STANDARD),
            replace(self.tasks[0], risk="L1"),
            replace(self.tasks[0], task_capabilities=("coding", "testing")),
            replace(self.tasks[0], degradation_approval_id="APPROVAL-1"),
            replace(self.tasks[0], expected_task_attempt=1, new_attempt=2),
            downward,
            replace(downward, difficulty_override_reason="pm_scope_calibration"),
            replace(downward, difficulty_approval_id="APR-2"),
        )
        self.assertNotEqual(
            _task_digest(downward),
            _task_digest(replace(downward, difficulty_override_reason="pm_scope_calibration")),
        )
        self.assertNotEqual(
            _task_digest(downward),
            _task_digest(replace(downward, difficulty_approval_id="APR-2")),
        )
        for index, changed in enumerate(variants):
            with self.subTest(index=index):
                root = self.cards.parent.parent.parent / f"variant-{index}"
                service = DockyardPmService(
                    DockyardPlanStore(root / "plans"), root / "cards",
                )
                record = service.draft(DockyardPlanDraftRequest(
                    f"PLAN-V{index}", "PRJ-1", "实现 Dockyard 本机闭环",
                    (changed, self.tasks[1]), "PM-1",
                    "2026-08-02T00:00:00Z", f"OP-V{index}",
                ))
                self.assertNotEqual(record.plan_digest, baseline)

    def test_source_has_no_scheduler_worker_or_transition_calls(self) -> None:
        source = (_SCRIPTS / "dockyard_pm_service.py").read_text(encoding="utf-8")
        for forbidden in (
            "workflow_orchestrator",
            "portfolio_scheduler",
            "worker_adapter",
            "control_plane_transition",
            "task_dispatched",
            "subprocess",
        ):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
