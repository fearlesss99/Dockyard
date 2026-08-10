"""TC-13.18d.12c.3 real-process owner-loss automatic-retry E2E."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "agentdesk" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dispatch_supervisor_evidence as dse
from control_plane_transition import (
    ControlPlaneTransitionService,
    DispatchCAS,
    OwnerLossRetryReservationRequest,
    TransitionCAS,
    TransitionEventContext,
    execute_owner_loss_retry_reservation,
)
from dispatcher_gateway import AgentCliInvocation
from state_provider import StateProvider
from worker_slot_lease import acquire_worker_slot, read_worker_slot_leases
from workflow_orchestrator import (
    BoundedDispatchRetryRequest,
    OwnerLossRecoveryRequest,
    WorkflowInvariantError,
    WorkflowOrchestrator,
)

from tests.test_workflow_orchestrator import (
    FakeClock,
    TestBoundedDispatchRetry,
    _git_head,
    _make_dispatch_cycle_request,
    _setup_project,
)


class _LocalPythonProvider:
    """Provider adapter that launches only the local Python interpreter."""

    provider_id = "claude"

    def __init__(
        self,
        stdout: bytes,
        *,
        reservation_project: Path | None = None,
        reservation_dispatch_id: str | None = None,
    ) -> None:
        encoded = base64.b64encode(stdout).decode("ascii")
        self._code = (
            "import base64,sys;"
            f"sys.stdout.buffer.write(base64.b64decode({encoded!r}));"
            "sys.stdout.buffer.flush()"
        )
        self.build_count = 0
        self._reservation_project = reservation_project
        self._reservation_dispatch_id = reservation_dispatch_id
        self.phase_at_worker_build: str | None = None

    def build_invocation(self, request: Any) -> AgentCliInvocation:
        self.build_count += 1
        if self._reservation_project is not None:
            receipt = dse.read_retry_receipt(
                self._reservation_project,
                self._reservation_dispatch_id or "",
            )
            if receipt is None or receipt.phase != "RETRY_RESERVED":
                raise AssertionError(
                    "retry reservation must be durable before Worker invocation"
                )
            self.phase_at_worker_build = receipt.phase
        return AgentCliInvocation(
            executable=sys.executable,
            argv=("-c", self._code),
            stdin=None,
            env_overrides=(),
        )


def _claude_success(
    *,
    task_id: str,
    revision: int,
    attempt: int,
    dispatch_id: str,
    commit: str,
) -> bytes:
    envelope = {
        "schema_version": "agentdesk.worker-output/v1",
        "task_id": task_id,
        "revision": revision,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "status": "completed",
        "implementation_commit": commit,
        "report_commit": "f" * 40,
        "summary": "local deterministic helper completed",
        "warnings": [],
    }
    wrapper = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "ttft_ms": 1,
        "ttft_stream_ms": 1,
        "time_to_request_ms": 1,
        "num_turns": 1,
        "result": json.dumps(envelope, separators=(",", ":")),
        "stop_reason": "end_turn",
        "session_id": "local-e2e",
        "total_cost_usd": None,
        "usage": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 1,
            "server_tool_use": {
                "web_search_requests": 0,
                "web_fetch_requests": 0,
            },
            "service_tier": "standard",
            "cache_creation": {
                "ephemeral_1h_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
            },
        },
        "modelUsage": {
            "test-model": {"inputTokens": 1, "outputTokens": 1},
        },
        "permission_denials": [],
        "terminal_reason": "completed",
        "fast_mode_state": "off",
        "uuid": "owner-loss-e2e-result",
    }
    return json.dumps(wrapper, separators=(",", ":")).encode("utf-8")


def _retry_plan(project: Path) -> BoundedDispatchRetryRequest:
    attempt = TestBoundedDispatchRetry._attempt(2)
    cycle = attempt.dispatch_cycle_request
    head = _git_head(project)
    dispatch = dataclasses.replace(
        cycle.dispatch_request,
        workspace=project,
    )
    dispatch_transition = dataclasses.replace(
        cycle.dispatch_transition_request,
        cas=dataclasses.replace(
            cycle.dispatch_transition_request.cas,
            expected_snapshot_commit=head,
        ),
    )
    acknowledge_transition = dataclasses.replace(
        cycle.acknowledge_transition_request,
        cas=dataclasses.replace(
            cycle.acknowledge_transition_request.cas,
            expected_snapshot_commit=head,
        ),
    )
    cycle = dataclasses.replace(
        cycle,
        dispatch_request=dispatch,
        dispatch_transition_request=dispatch_transition,
        acknowledge_transition_request=acknowledge_transition,
    )
    failure = dataclasses.replace(
        attempt.failure_transition_request,
        cas=dataclasses.replace(
            attempt.failure_transition_request.cas,
            expected_snapshot_commit=head,
        ),
    )
    return BoundedDispatchRetryRequest(
        (
            dataclasses.replace(
                attempt,
                dispatch_cycle_request=cycle,
                failure_transition_request=failure,
            ),
        )
    )


def _cycle_for_attempt(
    project: Path,
    *,
    attempt: int,
    dispatch_id: str,
):
    base = _make_dispatch_cycle_request(
        tmp=project,
        dispatch_id=dispatch_id,
        holder_instance_id=f"owner-attempt-{attempt}",
        delivery_event_id=f"EVT-OWNER-DELIVERY-{attempt}",
    )
    identity = dataclasses.replace(
        base.dispatch_request.identity,
        attempt=attempt,
        dispatch_id=dispatch_id,
    )
    dispatch_request = dataclasses.replace(
        base.dispatch_request,
        identity=identity,
    )
    payload = dataclasses.replace(
        base.dispatch_transition_request.payload,
        dispatch_id=dispatch_id,
        new_attempt=attempt,
        outbox_message_id=f"MSG-OWNER-{attempt}",
    )
    dispatch_transition = dataclasses.replace(
        base.dispatch_transition_request,
        event_id=f"EVT-OWNER-DISPATCH-{attempt}",
        payload=payload,
    )
    acknowledge = dataclasses.replace(
        base.acknowledge_transition_request,
        dispatch_cas=DispatchCAS(
            expected_dispatch_id=dispatch_id,
            expected_attempt=attempt,
        ),
        event_id=f"EVT-OWNER-ACK-{attempt}",
    )
    return dataclasses.replace(
        base,
        dispatch_request=dispatch_request,
        dispatch_transition_request=dispatch_transition,
        acknowledge_transition_request=acknowledge,
    )


def _write_dispatch_grant(
    project: Path,
    *,
    event_id: str,
    approval_id: str,
    attempt: int,
    dispatch_id: str,
) -> None:
    grant = {
        "schema_version": "agentdesk.task-approval/v1",
        "record_type": "grant",
        "approval_id": approval_id,
        "event_id": event_id,
        "scope": "dispatch",
        "task_id": "TC-001",
        "revision": 1,
        "attempt": attempt,
        "dispatch_id": dispatch_id,
        "accepted_commit": None,
        "actor_role_id": "PM",
        "lease_epoch": 1,
        "granted_at": "2026-08-01T11:00:00Z",
        "expires_at": "2026-08-02T12:00:00Z",
        "reason": "owner-loss E2E dispatch approval",
        "snapshot_commit": _git_head(project),
    }
    path = project / "docs" / "pm" / "approvals" / f"{event_id}.yaml"
    path.write_text(
        json.dumps(grant, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class _SupervisorHarness:
    """Real supervisor/Worker pair with event-driven startup observation."""

    def __init__(
        self,
        project: Path,
        *,
        dispatch_id: str,
        generation_id: str,
        identity: dict[str, object],
    ) -> None:
        self._project = project
        self._dispatch_id = dispatch_id
        runner = _SCRIPTS / "dispatch_supervisor_runner.py"
        payload = {
            "project_root": str(project),
            "dispatch_id": dispatch_id,
            "generation_id": generation_id,
            "workspace": str(project),
            "timeout_seconds": 60,
            "provider": "claude",
            "model_id": "test-model",
            "executable": sys.executable,
            "argv": [
                "-c",
                "import threading; threading.Event().wait()",
            ],
            "stdin_b64": None,
            "env_overrides": [],
            "identity": identity,
        }
        self.process = subprocess.Popen(
            [sys.executable, str(runner)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload).encode("utf-8"))
        self.process.stdin.close()
        self.worker_started = threading.Event()
        self.frames: list[dict[str, object]] = []
        self._reader = threading.Thread(target=self._read_frames, daemon=True)
        self._reader.start()

    def _read_frames(self) -> None:
        assert self.process.stdout is not None
        for raw in iter(self.process.stdout.readline, b""):
            frame = json.loads(raw.decode("utf-8"))
            self.frames.append(frame)
            if frame.get("event") == "worker_started":
                self.worker_started.set()

    def wait_started(self) -> None:
        if not self.worker_started.wait(timeout=20):
            stderr = b""
            if self.process.stderr is not None:
                stderr = self.process.stderr.read()
            raise AssertionError(
                "supervisor did not start Worker: "
                + stderr.decode("utf-8", "replace")
            )

    def terminate(self) -> None:
        if os.name != "nt":
            receipt = dse.read_dispatch_receipt(
                self._project, self._dispatch_id
            )
            if receipt is not None:
                if receipt.worker_process_group is not None:
                    try:
                        os.killpg(
                            receipt.worker_process_group,
                            signal.SIGTERM,
                        )
                    except ProcessLookupError:
                        pass
                elif receipt.worker_pid is not None:
                    try:
                        os.kill(receipt.worker_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=20)
        else:
            self.process.terminate()
            self.process.wait(timeout=20)
        self._reader.join(timeout=20)
        if self._reader.is_alive():
            raise AssertionError("supervisor frame reader did not stop")
        for stream in (
            self.process.stdout,
            self.process.stderr,
            self.process.stdin,
        ):
            if stream is not None and not stream.closed:
                stream.close()


class OwnerLossAutomaticRetryE2E(unittest.TestCase):
    """Real-process happy path; no production entry or store is mocked."""

    def _assert_liveness_rejects_without_side_effects(
        self,
        *,
        boot_id: str,
        expected_message: str,
    ) -> None:
        project = _setup_project()
        try:
            now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
            dispatch_id = "DSP-OWNER-LIVENESS-1"
            _write_dispatch_grant(
                project,
                event_id="EVT-OWNER-LIVENESS-APPROVAL-1",
                approval_id="APR-OWNER-LIVENESS-1",
                attempt=1,
                dispatch_id=dispatch_id,
            )
            cycle = _make_dispatch_cycle_request(
                tmp=project,
                dispatch_id=dispatch_id,
                holder_instance_id="owner-liveness-1",
            )
            identity = cycle.dispatch_request.identity
            lease = acquire_worker_slot(
                project,
                cycle.worker_kind,
                dispatch_id,
                cycle.holder_instance_id,
                project,
                now,
            )
            service = ControlPlaneTransitionService(project)
            service.apply_transition(cycle.dispatch_transition_request, lease, now)
            service.apply_transition(cycle.acknowledge_transition_request, lease, now)
            pid = os.getpid()
            creation = dse.get_process_creation_time(pid)
            self.assertIsNotNone(creation)
            generation = "GEN-OWNER-LIVENESS-1"
            dse.reserve_receipt(
                project,
                task_id=identity.task_id,
                revision=identity.revision,
                attempt=identity.attempt,
                dispatch_id=dispatch_id,
                lease_epoch=lease.lease_epoch,
                holder_instance_id=cycle.holder_instance_id,
                generation_id=generation,
                creator_pid=pid,
                creator_creation_time=creation or "",
                boot_id=boot_id,
            )
            dse.advance_to_supervisor_ready(
                project,
                dispatch_id=dispatch_id,
                generation_id=generation,
                supervisor_pid=pid,
                supervisor_creation_time=creation or "",
            )
            dse.advance_to_worker_started(
                project,
                dispatch_id=dispatch_id,
                generation_id=generation,
                worker_pid=pid,
                worker_creation_time=creation or "",
                worker_process_group=None,
            )
            plan = _retry_plan(project)
            request = OwnerLossRecoveryRequest(
                task_id=identity.task_id,
                expected_revision=identity.revision,
                expected_attempt=identity.attempt,
                expected_dispatch_id=dispatch_id,
                expected_generation_id=generation,
                recovery_event_id="EVT-OWNER-LIVENESS-RECOVERY-1",
                recovery_event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=("docs/pm/evidence/liveness-e2e.yaml",),
                    guard_results=(),
                ),
                failure_kind="worker_failed",
                evidence_refs=("docs/pm/evidence/liveness-e2e.yaml",),
                retry_plan=plan,
            )
            before = StateProvider(project).snapshot()
            with self.assertRaisesRegex(WorkflowInvariantError, expected_message):
                asyncio.run(
                    WorkflowOrchestrator(
                        project_root=project,
                        clock=FakeClock(
                            start=datetime(
                                2026, 8, 1, 12, 1, tzinfo=timezone.utc
                            )
                        ),
                    ).recover_owner_lost_dispatch(
                        request,
                        {"claude": _LocalPythonProvider(b"")},
                    )
                )
            after = StateProvider(project).snapshot()
            self.assertEqual(before.events, after.events)
            self.assertIsNone(dse.read_retry_receipt(project, "DSP-RETRY-2"))
        finally:
            shutil.rmtree(project, ignore_errors=True)

    def _prepare_dead_active_dispatch(
        self,
        *,
        failed_attempt: int = 1,
    ) -> tuple[Path, OwnerLossRecoveryRequest]:
        project = _setup_project()
        dispatch_id = f"DSP-OWNER-DEAD-{failed_attempt}"
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        if failed_attempt > 1:
            tasks_path = project / "docs" / "pm" / "state" / "tasks.yaml"
            tasks_doc = json.loads(tasks_path.read_text(encoding="utf-8"))
            tasks_doc["tasks"][0]["attempt"] = failed_attempt - 1
            tasks_path.write_text(
                json.dumps(tasks_doc, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        _write_dispatch_grant(
            project,
            event_id=f"EVT-OWNER-DEAD-APPROVAL-{failed_attempt}",
            approval_id=f"APR-OWNER-DEAD-{failed_attempt}",
            attempt=failed_attempt,
            dispatch_id=dispatch_id,
        )
        cycle = _cycle_for_attempt(
            project,
            attempt=failed_attempt,
            dispatch_id=dispatch_id,
        )
        lease = acquire_worker_slot(
            project,
            cycle.worker_kind,
            dispatch_id,
            cycle.holder_instance_id,
            project,
            now,
        )
        service = ControlPlaneTransitionService(project)
        service.apply_transition(cycle.dispatch_transition_request, lease, now)
        service.apply_transition(cycle.acknowledge_transition_request, lease, now)

        dead = subprocess.Popen(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        creation = dse.get_process_creation_time(dead.pid)
        self.assertIsNotNone(creation)
        worker_process_group = (
            os.getpgid(dead.pid) if os.name != "nt" else None
        )
        dead.terminate()
        dead.wait(timeout=20)
        generation = f"GEN-OWNER-DEAD-{failed_attempt}"
        owner = dse._DispatchJobOwner(generation)
        owner.close()
        dse.reserve_receipt(
            project,
            task_id="TC-001",
            revision=1,
            attempt=failed_attempt,
            dispatch_id=dispatch_id,
            lease_epoch=lease.lease_epoch,
            holder_instance_id=cycle.holder_instance_id,
            generation_id=generation,
            creator_pid=dead.pid,
            creator_creation_time=creation or "",
            boot_id=dse.get_boot_id(),
        )
        dse.advance_to_supervisor_ready(
            project,
            dispatch_id=dispatch_id,
            generation_id=generation,
            supervisor_pid=dead.pid,
            supervisor_creation_time=creation or "",
        )
        dse.advance_to_worker_started(
            project,
            dispatch_id=dispatch_id,
            generation_id=generation,
            worker_pid=dead.pid,
            worker_creation_time=creation or "",
            worker_process_group=worker_process_group,
        )
        evidence = ("docs/pm/evidence/dead-owner-e2e.yaml",)
        request = OwnerLossRecoveryRequest(
            task_id="TC-001",
            expected_revision=1,
            expected_attempt=failed_attempt,
            expected_dispatch_id=dispatch_id,
            expected_generation_id=generation,
            recovery_event_id=f"EVT-OWNER-DEAD-RECOVERY-{failed_attempt}",
            recovery_event_context=TransitionEventContext(
                source_message_id=None,
                evidence_refs=evidence,
                guard_results=(),
            ),
            failure_kind="worker_failed",
            evidence_refs=evidence,
            retry_plan=None,
        )
        return project, request

    def test_alive_is_zero_transition_zero_retry(self) -> None:
        self._assert_liveness_rejects_without_side_effects(
            boot_id=dse.get_boot_id(),
            expected_message="still alive",
        )

    def test_boot_id_mismatch_is_unknown_and_fail_closed(self) -> None:
        self._assert_liveness_rejects_without_side_effects(
            boot_id="boot-id-mismatch-e2e",
            expected_message="liveness is unknown",
        )

    def test_attempt_three_recovers_without_attempt_four(self) -> None:
        project, request = self._prepare_dead_active_dispatch(failed_attempt=3)
        try:
            result = asyncio.run(
                WorkflowOrchestrator(
                    project_root=project,
                    clock=FakeClock(
                        start=datetime(2026, 8, 1, 12, 5, tzinfo=timezone.utc)
                    ),
                ).recover_owner_lost_dispatch(request)
            )
            self.assertIsNone(result.retry_result)
            snapshot = StateProvider(project).snapshot()
            task = next(t for t in snapshot.tasks if t.task_id == "TC-001")
            self.assertEqual(task.state, "ready")
            self.assertEqual(task.attempt, 3)
            self.assertIsNone(task.current_dispatch)
            self.assertFalse(
                any(event.attempt == 4 for event in snapshot.events)
            )
            runtime = project / ".agentdesk" / "runtime" / "dispatch-supervisor"
            self.assertEqual(list(runtime.glob("retry-*.json")), [])
        finally:
            shutil.rmtree(project, ignore_errors=True)

    def test_retry_reserved_crash_replay_reuses_identity(self) -> None:
        project, transition_only = self._prepare_dead_active_dispatch()
        try:
            clock = FakeClock(
                start=datetime(2026, 8, 1, 12, 5, tzinfo=timezone.utc)
            )
            orchestrator = WorkflowOrchestrator(
                project_root=project,
                clock=clock,
            )
            asyncio.run(
                orchestrator.recover_owner_lost_dispatch(transition_only)
            )
            plan = _retry_plan(project)
            identity2 = plan.attempts[0].dispatch_cycle_request.dispatch_request.identity
            _write_dispatch_grant(
                project,
                event_id="EVT-OWNER-REPLAY-APPROVAL-2",
                approval_id="APR-OWNER-REPLAY-2",
                attempt=2,
                dispatch_id=identity2.dispatch_id,
            )
            digest = dse.compute_retry_content_digest(
                task_id="TC-001",
                revision=1,
                failed_attempt=1,
                failed_dispatch_id=transition_only.expected_dispatch_id,
                recovery_event_id=transition_only.recovery_event_id,
                recovery_generation_id=transition_only.expected_generation_id,
                next_attempt=2,
                next_dispatch_id=identity2.dispatch_id,
                next_dispatch_event_id=(
                    plan.attempts[0]
                    .dispatch_cycle_request.dispatch_transition_request.event_id
                ),
            )
            pid, creation = dse.get_current_process_identity()
            reservation = OwnerLossRetryReservationRequest(
                task_id="TC-001",
                revision=1,
                failed_attempt=1,
                failed_dispatch_id=transition_only.expected_dispatch_id,
                recovery_event_id=transition_only.recovery_event_id,
                recovery_generation_id=transition_only.expected_generation_id,
                next_attempt=2,
                next_dispatch_id=identity2.dispatch_id,
                next_dispatch_event_id=(
                    plan.attempts[0]
                    .dispatch_cycle_request.dispatch_transition_request.event_id
                ),
                content_digest=digest,
                creator_pid=pid,
                creator_creation_time=creation,
                creator_boot_id=dse.get_boot_id(),
            )
            first = execute_owner_loss_retry_reservation(
                ControlPlaneTransitionService(project), reservation
            )
            replay_request = dataclasses.replace(
                transition_only,
                retry_plan=plan,
            )
            output = _claude_success(
                task_id="TC-001",
                revision=1,
                attempt=2,
                dispatch_id=identity2.dispatch_id,
                commit=_git_head(project),
            )
            result = asyncio.run(
                orchestrator.recover_owner_lost_dispatch(
                    replay_request,
                    {"claude": _LocalPythonProvider(output)},
                )
            )
            self.assertIsNotNone(result.retry_result)
            final = dse.read_retry_receipt(project, identity2.dispatch_id)
            self.assertIsNotNone(final)
            assert final is not None
            self.assertEqual(first.next_dispatch_id, final.next_dispatch_id)
            self.assertEqual(final.phase, "RETRY_FINALIZED")
            snapshot = StateProvider(project).snapshot()
            self.assertEqual(
                sum(e.event_type == "TASK_DISPATCHED" for e in snapshot.events),
                2,
            )
        finally:
            shutil.rmtree(project, ignore_errors=True)

    def test_concurrent_recoverers_reserve_and_start_only_once(self) -> None:
        project, transition_only = self._prepare_dead_active_dispatch()
        try:
            plan = _retry_plan(project)
            identity2 = plan.attempts[0].dispatch_cycle_request.dispatch_request.identity
            _write_dispatch_grant(
                project,
                event_id="EVT-OWNER-CONCURRENT-APPROVAL-2",
                approval_id="APR-OWNER-CONCURRENT-2",
                attempt=2,
                dispatch_id=identity2.dispatch_id,
            )
            request = dataclasses.replace(transition_only, retry_plan=plan)
            provider = _LocalPythonProvider(
                _claude_success(
                    task_id="TC-001",
                    revision=1,
                    attempt=2,
                    dispatch_id=identity2.dispatch_id,
                    commit=_git_head(project),
                )
            )

            async def race() -> tuple[object, object]:
                gate = asyncio.Event()

                async def contender() -> object:
                    await gate.wait()
                    orchestrator = WorkflowOrchestrator(
                        project_root=project,
                        clock=FakeClock(
                            start=datetime(
                                2026, 8, 1, 12, 5, tzinfo=timezone.utc
                            )
                        ),
                    )
                    return await orchestrator.recover_owner_lost_dispatch(
                        request,
                        {"claude": provider},
                    )

                first = asyncio.create_task(contender())
                second = asyncio.create_task(contender())
                gate.set()
                gathered = await asyncio.gather(
                    first, second, return_exceptions=True
                )
                return gathered[0], gathered[1]

            results = asyncio.run(race())
            successes = tuple(
                result for result in results if not isinstance(result, BaseException)
            )
            fenced = tuple(
                result for result in results if isinstance(result, BaseException)
            )
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(fenced), 1)
            self.assertEqual(provider.build_count, 1)

            retry_receipt = dse.read_retry_receipt(project, identity2.dispatch_id)
            self.assertIsNotNone(retry_receipt)
            assert retry_receipt is not None
            self.assertEqual(retry_receipt.next_dispatch_id, identity2.dispatch_id)
            self.assertEqual(retry_receipt.phase, "RETRY_FINALIZED")
            snapshot = StateProvider(project).snapshot()
            self.assertEqual(
                sum(
                    event.event_type == "TASK_DISPATCHED"
                    and event.attempt == 2
                    for event in snapshot.events
                ),
                1,
            )
        finally:
            shutil.rmtree(project, ignore_errors=True)

    def test_happy_path_real_process_to_finalized_retry(self) -> None:
        project = _setup_project()
        creator: subprocess.Popen[bytes] | None = None
        supervisor: _SupervisorHarness | None = None
        try:
            now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
            _write_dispatch_grant(
                project,
                event_id="EVT-OWNER-E2E-APPROVAL-1",
                approval_id="APR-OWNER-E2E-1",
                attempt=1,
                dispatch_id="DSP-OWNER-E2E-1",
            )
            _write_dispatch_grant(
                project,
                event_id="EVT-OWNER-E2E-APPROVAL-2",
                approval_id="APR-OWNER-E2E-2",
                attempt=2,
                dispatch_id="DSP-RETRY-2",
            )
            cycle1 = _make_dispatch_cycle_request(
                tmp=project,
                dispatch_id="DSP-OWNER-E2E-1",
                holder_instance_id="owner-e2e-1",
                delivery_event_id="EVT-OWNER-E2E-DELIVERY-1",
            )
            identity1 = cycle1.dispatch_request.identity
            lease1 = acquire_worker_slot(
                project,
                cycle1.worker_kind,
                identity1.dispatch_id,
                cycle1.holder_instance_id,
                project,
                now,
            )
            service = ControlPlaneTransitionService(project)
            service.apply_transition(
                cycle1.dispatch_transition_request,
                lease1,
                now,
            )

            creator = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import threading; threading.Event().wait()",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            creator_creation = dse.get_process_creation_time(creator.pid)
            self.assertIsNotNone(creator_creation)
            failed_generation = "GEN-OWNER-E2E-1"
            dse.reserve_receipt(
                project,
                task_id=identity1.task_id,
                revision=identity1.revision,
                attempt=identity1.attempt,
                dispatch_id=identity1.dispatch_id,
                lease_epoch=lease1.lease_epoch,
                holder_instance_id=cycle1.holder_instance_id,
                generation_id=failed_generation,
                creator_pid=creator.pid,
                creator_creation_time=creator_creation or "",
                boot_id=dse.get_boot_id(),
            )
            supervisor = _SupervisorHarness(
                project,
                dispatch_id=identity1.dispatch_id,
                generation_id=failed_generation,
                identity={
                    "task_id": identity1.task_id,
                    "revision": identity1.revision,
                    "attempt": identity1.attempt,
                    "dispatch_id": identity1.dispatch_id,
                },
            )
            supervisor.wait_started()
            service.apply_transition(
                cycle1.acknowledge_transition_request,
                lease1,
                now,
            )

            creator.terminate()
            creator.wait(timeout=20)
            supervisor.terminate()
            creator = None
            supervisor = None

            failed_receipt = dse.read_dispatch_receipt(
                project, identity1.dispatch_id
            )
            self.assertIsNotNone(failed_receipt)
            assert failed_receipt is not None
            self.assertIs(
                dse.probe_process(
                    failed_receipt.creator_pid,
                    failed_receipt.creator_creation_time,
                    failed_receipt.boot_id,
                ),
                dse.ProcessLiveness.DEAD,
            )
            worker_liveness = dse.probe_process(
                failed_receipt.worker_pid,
                failed_receipt.worker_creation_time,
                failed_receipt.boot_id,
            )
            self.assertIs(worker_liveness, dse.ProcessLiveness.DEAD)
            tree_liveness = dse.probe_dispatch_process_tree(failed_receipt)
            direct_tree_liveness = dse.probe_process(
                failed_receipt.worker_pid,
                failed_receipt.worker_creation_time,
                failed_receipt.boot_id,
                recorded_process_group=failed_receipt.worker_process_group,
                require_tree=True,
            )
            group_members: list[tuple[int, str, int]] = []
            if os.name != "nt" and failed_receipt.worker_process_group is not None:
                for proc_entry in Path("/proc").iterdir():
                    if not proc_entry.name.isdigit():
                        continue
                    try:
                        stat_text = (proc_entry / "stat").read_text(
                            encoding="utf-8"
                        )
                        stat_end = stat_text.rfind(")")
                        stat_fields = stat_text[stat_end + 2 :].split()
                        if int(stat_fields[2]) == failed_receipt.worker_process_group:
                            group_members.append(
                                (
                                    int(proc_entry.name),
                                    stat_fields[0],
                                    int(stat_fields[2]),
                                )
                            )
                    except (OSError, ValueError, IndexError):
                        continue
            self.assertIs(
                tree_liveness,
                dse.ProcessLiveness.DEAD,
                (
                    "durable supervisor and Worker tree must be DEAD; "
                    f"direct={direct_tree_liveness.value}; "
                    f"group={failed_receipt.worker_process_group!r}; "
                    f"members={group_members!r}"
                ),
            )

            plan = _retry_plan(project)
            identity2 = (
                plan.attempts[0]
                .dispatch_cycle_request.dispatch_request.identity
            )
            evidence = ("docs/pm/evidence/owner-loss-e2e.yaml",)
            request = OwnerLossRecoveryRequest(
                task_id=identity1.task_id,
                expected_revision=identity1.revision,
                expected_attempt=identity1.attempt,
                expected_dispatch_id=identity1.dispatch_id,
                expected_generation_id=failed_generation,
                recovery_event_id="EVT-OWNER-LOSS-E2E-RECOVERY-1",
                recovery_event_context=TransitionEventContext(
                    source_message_id=None,
                    evidence_refs=evidence,
                    guard_results=(),
                ),
                failure_kind="worker_failed",
                evidence_refs=evidence,
                retry_plan=plan,
            )
            output = _claude_success(
                task_id=identity2.task_id,
                revision=identity2.revision,
                attempt=identity2.attempt,
                dispatch_id=identity2.dispatch_id,
                commit=_git_head(project),
            )
            orchestrator = WorkflowOrchestrator(
                project_root=project,
                clock=FakeClock(
                    start=datetime(
                        2026, 8, 1, 12, 5, tzinfo=timezone.utc
                    )
                ),
                heartbeat_interval_seconds=10.0,
            )
            retry_provider = _LocalPythonProvider(
                output,
                reservation_project=project,
                reservation_dispatch_id=identity2.dispatch_id,
            )

            async def recover_and_check_tasks() -> object:
                recovered = await orchestrator.recover_owner_lost_dispatch(
                    request,
                    {"claude": retry_provider},
                )
                pending = tuple(
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task() and not task.done()
                )
                self.assertEqual(pending, ())
                return recovered

            result = asyncio.run(recover_and_check_tasks())

            self.assertIsNotNone(result.retry_result)
            retry_receipt = dse.read_retry_receipt(
                project, identity2.dispatch_id
            )
            self.assertIsNotNone(retry_receipt)
            assert retry_receipt is not None
            self.assertEqual(retry_receipt.phase, "RETRY_FINALIZED")
            self.assertEqual(retry_receipt.next_attempt, 2)
            self.assertEqual(
                retry_receipt.next_dispatch_id, identity2.dispatch_id
            )
            self.assertLessEqual(
                retry_receipt.reserved_at,
                retry_receipt.started_at or "",
            )
            self.assertEqual(
                retry_provider.phase_at_worker_build, "RETRY_RESERVED"
            )
            retry_tombstone = dse.read_dispatch_tombstone(
                project, identity2.dispatch_id
            )
            self.assertIsNotNone(retry_tombstone)
            retry_dispatch_receipt = dse.read_dispatch_receipt(
                project, identity2.dispatch_id
            )
            self.assertIsNotNone(retry_dispatch_receipt)
            assert retry_tombstone is not None
            assert retry_dispatch_receipt is not None
            self.assertEqual(retry_dispatch_receipt.phase, "FINALIZED")
            self.assertEqual(
                retry_tombstone.dispatch_id, retry_receipt.next_dispatch_id
            )
            self.assertEqual(retry_tombstone.winner, "completion")

            snapshot = StateProvider(project).snapshot()
            task = next(t for t in snapshot.tasks if t.task_id == "TC-001")
            self.assertEqual(task.state, "review_ready")
            self.assertEqual(task.attempt, 2)
            self.assertIsNotNone(task.current_dispatch)
            self.assertEqual(
                task.current_dispatch.dispatch_id,
                identity2.dispatch_id,
            )
            event_types = [event.event_type for event in snapshot.events]
            self.assertEqual(event_types.count("DISPATCH_FAILED"), 1)
            self.assertEqual(event_types.count("TASK_DISPATCHED"), 2)
            self.assertEqual(
                event_types.count("DISPATCH_ACKNOWLEDGED"), 2
            )
            self.assertEqual(event_types.count("DELIVERY_SUBMITTED"), 1)
            lifecycle_keys = (
                (1, "TASK_DISPATCHED"),
                (1, "DISPATCH_ACKNOWLEDGED"),
                (1, "DISPATCH_FAILED"),
                (2, "TASK_DISPATCHED"),
                (2, "DISPATCH_ACKNOWLEDGED"),
                (2, "DELIVERY_SUBMITTED"),
            )
            lifecycle_events = tuple(
                next(
                    event
                    for event in snapshot.events
                    if (event.attempt, event.event_type) == key
                )
                for key in lifecycle_keys
            )
            self.assertEqual(
                tuple(event.attempt for event in lifecycle_events),
                (1, 1, 1, 2, 2, 2),
            )
            self.assertEqual(
                tuple(sorted(event.occurred_at for event in lifecycle_events)),
                tuple(event.occurred_at for event in lifecycle_events),
            )
            self.assertFalse(any(event.attempt == 3 for event in snapshot.events))
            self.assertEqual(read_worker_slot_leases(project)["leases"], {})
            temp_residue = tuple(
                path
                for path in project.rglob("*")
                if path.is_file()
                and (path.suffix == ".tmp" or path.name.endswith(".tmp"))
            )
            self.assertEqual(temp_residue, ())
        finally:
            if supervisor is not None:
                supervisor.terminate()
            if creator is not None:
                creator.terminate()
                creator.wait(timeout=20)
            shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
