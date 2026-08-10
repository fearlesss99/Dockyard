from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from control_plane_transition import ControlPlaneTransitionService  # noqa: E402
from portfolio_scheduler_admission import PortfolioSchedulerAdmission  # noqa: E402
import portfolio_scheduler_runtime as pr  # noqa: E402
import portfolio_scheduler_store as ps  # noqa: E402
from tests import test_portfolio_scheduler_runtime as runtime_fixtures  # noqa: E402


@dataclasses.dataclass(frozen=True, slots=True)
class _LockObservingTransitions(ControlPlaneTransitionService):
    observed_lock_states: ClassVar[list[bool]] = []

    def apply_transition(self, request, lease, now):
        self.observed_lock_states.append(
            (self.project_root / ps.STORE_LOCK_FILENAME).exists()
        )
        return ControlPlaneTransitionService.apply_transition(
            self, request, lease, now
        )


class PortfolioSchedulerPlanBindingVerificationTests(unittest.TestCase):
    def _new_queued(self):
        root, _ = runtime_fixtures._setup_project()
        store, entry = runtime_fixtures._setup_queued(root)
        request = runtime_fixtures._make_tick_request(root, entry)
        return root, store, entry, request

    def _seed_selected(self, root, store, entry, request):
        runtime = pr.PortfolioSchedulerRuntime(root, store=store)
        receipt = runtime_fixtures._make_receipt(entry, request.plan.receipt_id)
        store.reserve_admission_selection(
            runtime._durable_plan(request.plan), receipt
        )
        return receipt

    def _assert_no_dispatch_side_effects(self, root, store, entry) -> None:
        self.assertEqual(runtime_fixtures._lease_count(root), 0)
        self.assertEqual(runtime_fixtures._event_ids(root), [])
        self.assertEqual(runtime_fixtures._outbox_ids(root), [])
        self.assertEqual(
            store.read_queue_entry(entry.queue_id).state,
            ps.QueuePhase.SELECTED,
        )

    def _assert_typed_identity_rejection(self, root, store, entry, request) -> None:
        runtime = pr.PortfolioSchedulerRuntime(root, store=store)
        with self.assertRaises(pr.PortfolioSchedulerRuntimeConflictError) as caught:
            runtime.tick(request)
        self.assertNotIsInstance(caught.exception, ps.PortfolioSchedulerNotFoundError)
        self._assert_no_dispatch_side_effects(root, store, entry)

    def test_dispatch_id_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            changed = dataclasses.replace(request.plan, dispatch_id="DSP-CHANGED")
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_event_id_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            changed = dataclasses.replace(request.plan, event_id="EVT-CHANGED")
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_outbox_message_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            changed = dataclasses.replace(
                request.plan, outbox_message_id="MSG-CHANGED"
            )
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_model_selection_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            model = dataclasses.replace(
                request.plan.model_selection,
                selected_model_id="different-model",
            )
            changed = dataclasses.replace(request.plan, model_selection=model)
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_expected_git_head_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            changed = dataclasses.replace(
                request.plan, expected_snapshot_commit="0" * 40
            )
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_holder_instance_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            changed = dataclasses.replace(
                request.plan, holder_instance_id="holder-changed"
            )
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_canonical_worktree_replacement_is_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            changed = dataclasses.replace(
                request.plan, canonical_worktree=str(root.parent)
            )
            self._assert_typed_identity_rejection(
                root, store, entry, dataclasses.replace(request, plan=changed)
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_task_card_base_branch_and_report_replacements_are_rejected(self) -> None:
        substitutions = (
            ("task_card_path", "tasks/other/task.md"),
            ("task_card_commit", "d" * 40),
            ("base_commit", "e" * 40),
            ("branch", "feat/other"),
            ("report_path", "reports/other.md"),
        )
        for field_name, value in substitutions:
            with self.subTest(field_name=field_name):
                root, store, entry, request = self._new_queued()
                try:
                    self._seed_selected(root, store, entry, request)
                    changed = dataclasses.replace(request.plan, **{field_name: value})
                    self._assert_typed_identity_rejection(
                        root, store, entry, dataclasses.replace(request, plan=changed)
                    )
                finally:
                    runtime_fixtures._remove_tree(root)

    def test_attempt_and_generation_replacements_are_rejected_before_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            self._seed_selected(root, store, entry, request)
            attempt_plan = dataclasses.replace(
                request.plan, expected_task_attempt=1, new_attempt=2
            )
            self._assert_typed_identity_rejection(
                root, store, entry,
                dataclasses.replace(request, plan=attempt_plan),
            )
            generation_plan = dataclasses.replace(
                request.plan, selection_generation=2
            )
            self._assert_typed_identity_rejection(
                root, store, entry,
                dataclasses.replace(request, plan=generation_plan),
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_new_operation_time_reuses_original_reserved_at(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            runtime = pr.PortfolioSchedulerRuntime(root, store=store)
            original_plan = runtime._durable_plan(request.plan)
            receipt = runtime_fixtures._make_receipt(entry, request.plan.receipt_id)
            store.reserve_admission_selection(original_plan, receipt)
            later = dataclasses.replace(
                request.plan, now=request.plan.now + timedelta(seconds=1)
            )
            result = runtime.tick(dataclasses.replace(request, plan=later))
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            durable = store.read_admission_plan(entry.queue_id, 1)
            self.assertEqual(durable.reserved_at, original_plan.reserved_at)
            self.assertEqual(runtime_fixtures._event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(runtime_fixtures._outbox_ids(root), ["MSG-ADMIT-001"])
            self.assertEqual(runtime_fixtures._lease_count(root), 1)
        finally:
            runtime_fixtures._remove_tree(root)

    def test_plan_only_crash_replay_completes_selection(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            runtime = pr.PortfolioSchedulerRuntime(root, store=store)
            durable = runtime._durable_plan(request.plan)
            store.reserve_admission_plan(durable)
            self.assertEqual(
                store.read_queue_entry(entry.queue_id).state,
                ps.QueuePhase.QUEUED,
            )
            result = runtime.tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(store.read_admission_plan(entry.queue_id, 1), durable)
            self.assertEqual(runtime_fixtures._event_ids(root), ["EVT-ADMIT-001"])
        finally:
            runtime_fixtures._remove_tree(root)

    def test_receipt_exists_queue_queued_replay_completes_plan(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            policy_receipt = runtime_fixtures._make_receipt(
                entry, request.plan.receipt_id
            )
            store_root = ps._prepare_store(root, True)
            encoded = ps.encode_schedule_receipt(policy_receipt)
            ps._atomic_store_bytes(
                ps._receipt_path(store_root, policy_receipt.receipt_id), encoded
            )
            ps._atomic_store_bytes(
                ps._reservation_path(
                    store_root,
                    policy_receipt.queue_id,
                    policy_receipt.selection_generation,
                ),
                encoded,
            )
            self.assertEqual(
                store.read_queue_entry(entry.queue_id).state,
                ps.QueuePhase.QUEUED,
            )
            result = pr.PortfolioSchedulerRuntime(root, store=store).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertIsNotNone(store.read_admission_plan(entry.queue_id, 1))
            self.assertEqual(runtime_fixtures._event_ids(root), ["EVT-ADMIT-001"])
        finally:
            runtime_fixtures._remove_tree(root)

    def test_selected_without_plan_returns_recovery_required(self) -> None:
        root, _ = runtime_fixtures._setup_project()
        try:
            store, entry, receipt = runtime_fixtures._setup_selected(root)
            request = runtime_fixtures._make_tick_request(root, entry, receipt)
            result = pr.PortfolioSchedulerRuntime(root, store=store).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.RECOVERY_REQUIRED,
            )
            self.assertEqual(runtime_fixtures._lease_count(root), 0)
            self.assertEqual(runtime_fixtures._event_ids(root), [])
            self.assertEqual(runtime_fixtures._outbox_ids(root), [])
        finally:
            runtime_fixtures._remove_tree(root)

    def test_two_schedulers_same_plan_have_one_durable_identity(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            first = pr.PortfolioSchedulerRuntime(root, store=store)
            second = pr.PortfolioSchedulerRuntime(root, store=store)

            async def run():
                return await asyncio.gather(
                    asyncio.to_thread(first.tick, request),
                    asyncio.to_thread(second.tick, request),
                    return_exceptions=True,
                )

            results = asyncio.run(run())
            successes = [
                item
                for item in results
                if isinstance(item, pr.PortfolioSchedulerTickResult)
            ]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(store.enumerate_validated_admission_plans()), 1)
            self.assertEqual(runtime_fixtures._event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(runtime_fixtures._lease_count(root), 1)
        finally:
            runtime_fixtures._remove_tree(root)

    def test_two_schedulers_divergent_plan_have_one_winner_and_loser_no_lease(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            divergent_plan = dataclasses.replace(
                request.plan, report_path="reports/divergent-report.md"
            )
            divergent_request = dataclasses.replace(request, plan=divergent_plan)
            first = pr.PortfolioSchedulerRuntime(root, store=store)
            second = pr.PortfolioSchedulerRuntime(root, store=store)

            async def run():
                return await asyncio.gather(
                    asyncio.to_thread(first.tick, request),
                    asyncio.to_thread(second.tick, divergent_request),
                    return_exceptions=True,
                )

            results = asyncio.run(run())
            successes = [
                item
                for item in results
                if isinstance(item, pr.PortfolioSchedulerTickResult)
            ]
            failures = [
                item for item in results if isinstance(item, BaseException)
            ]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(
                failures[0],
                (pr.PortfolioSchedulerRuntimeConflictError, ps.PortfolioSchedulerError),
            )
            self.assertEqual(runtime_fixtures._lease_count(root), 1)
            self.assertEqual(runtime_fixtures._event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(len(store.enumerate_validated_admission_plans()), 1)
        finally:
            runtime_fixtures._remove_tree(root)

    def test_attempt_four_is_rejected_before_plan_reservation(self) -> None:
        root, store, entry, _ = self._new_queued()
        try:
            with self.assertRaises(pr.PortfolioSchedulerRuntimeInputError):
                runtime_fixtures._make_tick_request(
                    root,
                    entry,
                    expected_task_attempt=3,
                    new_attempt=4,
                )
            self.assertEqual(runtime_fixtures._lease_count(root), 0)
            self.assertEqual(runtime_fixtures._event_ids(root), [])
            self.assertEqual(runtime_fixtures._outbox_ids(root), [])
            self.assertEqual(store.enumerate_validated_admission_plans(), ())
            self.assertEqual(
                store.read_queue_entry(entry.queue_id).state,
                ps.QueuePhase.QUEUED,
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_store_lock_is_released_before_real_admission(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            _LockObservingTransitions.observed_lock_states.clear()
            transitions = _LockObservingTransitions(root)
            admission = PortfolioSchedulerAdmission(
                root,
                store=store,
                transitions=transitions,
            )
            result = pr.PortfolioSchedulerRuntime(
                root,
                store=store,
                admission=admission,
            ).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(
                _LockObservingTransitions.observed_lock_states,
                [False],
            )
        finally:
            runtime_fixtures._remove_tree(root)

    def test_happy_path_emits_one_task_dispatched(self) -> None:
        root, store, entry, request = self._new_queued()
        try:
            result = pr.PortfolioSchedulerRuntime(root, store=store).tick(request)
            self.assertEqual(
                result.outcome.kind,
                pr.PortfolioSchedulerTickOutcomeKind.ADMITTED,
            )
            self.assertEqual(runtime_fixtures._lease_count(root), 1)
            self.assertEqual(runtime_fixtures._event_ids(root), ["EVT-ADMIT-001"])
            self.assertEqual(runtime_fixtures._outbox_ids(root), ["MSG-ADMIT-001"])
            self.assertEqual(
                store.read_queue_entry(entry.queue_id).state,
                ps.QueuePhase.DISPATCHED,
            )
            self.assertEqual(
                store.read_schedule_receipt(request.plan.receipt_id).phase,
                ps.ReceiptPhase.DISPATCHED,
            )
        finally:
            runtime_fixtures._remove_tree(root)


if __name__ == "__main__":
    unittest.main()
