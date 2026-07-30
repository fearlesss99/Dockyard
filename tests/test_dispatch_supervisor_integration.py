"""TC-13.18d.12a-pre2 — supervisor runner integration (real subprocess, no model).

Exercises the real supervisor subprocess with a ``sys.executable`` helper
Worker.  No model, API, or network.  Incremental targeted tests only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dispatch_supervisor_evidence as dse  # noqa: E402

_RUNNER = _SCRIPTS / "dispatch_supervisor_runner.py"


def _payload(project: Path, *, dispatch_id: str, generation_id: str,
             argv: tuple[str, ...], executable: str | None = None) -> dict[str, object]:
    return {
        "project_root": str(project),
        "dispatch_id": dispatch_id,
        "generation_id": generation_id,
        "workspace": str(project),
        "timeout_seconds": 30,
        "provider": "claudecode",
        "model_id": "test-model",
        "executable": executable or sys.executable,
        "argv": list(argv),
        "stdin_b64": None,
        "env_overrides": [],
        "identity": {
            "task_id": "TC-001", "revision": 1, "attempt": 1,
            "dispatch_id": dispatch_id,
        },
    }


class SupervisorRunnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dsr-int-")
        self.project = Path(self.tmp.name) / "project"
        (self.project / ".agentdesk" / "runtime").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, payload: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(_RUNNER)],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    def _events(self, proc: subprocess.CompletedProcess[bytes]) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
        return events

    def test_success_full_lifecycle(self) -> None:
        did = "DSP-int-0001"
        gen = "GEN-int-0001"
        dse.reserve_receipt(
            self.project, task_id="TC-001", revision=1, attempt=1,
            dispatch_id=did, lease_epoch=1, holder_instance_id="inst",
            generation_id=gen, creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            boot_id=dse.get_boot_id(),
        )
        proc = self._run(_payload(self.project, dispatch_id=did, generation_id=gen,
                                  argv=("-c", "pass")))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        events = self._events(proc)
        kinds = [str(e.get("event")) for e in events]
        self.assertIn("ready", kinds)
        self.assertIn("worker_started", kinds)
        self.assertTrue(any(k == "result" and e.get("status") == "ok"
                            for k, e in zip(kinds, events)))
        receipt = dse.read_dispatch_receipt(self.project, did)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.phase, "FINALIZING")  # type: ignore[union-attr]
        self.assertIsNotNone(receipt.supervisor_pid)  # type: ignore[union-attr]
        self.assertIsNotNone(receipt.worker_pid)  # type: ignore[union-attr]
        # The helper Worker has exited.  On POSIX the process-group tree can be
        # confirmed dead ⇒ DEAD; on Windows the tree is unconfirmable without a
        # Job Object ⇒ UNKNOWN (contract-safe; never ALIVE).
        worker_probe = dse.probe_process(
            receipt.worker_pid,  # type: ignore[arg-type]
            receipt.worker_creation_time,  # type: ignore[arg-type]
            receipt.boot_id,
            recorded_process_group=receipt.worker_process_group,
            require_tree=True,
        )
        self.assertNotEqual(worker_probe, dse.ProcessLiveness.ALIVE)
        if os.name != "nt":
            self.assertEqual(worker_probe, dse.ProcessLiveness.DEAD)

    def test_nonzero_exit(self) -> None:
        did = "DSP-int-0002"
        gen = "GEN-int-0002"
        dse.reserve_receipt(
            self.project, task_id="TC-001", revision=1, attempt=1,
            dispatch_id=did, lease_epoch=1, holder_instance_id="inst",
            generation_id=gen, creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            boot_id=dse.get_boot_id(),
        )
        proc = self._run(_payload(self.project, dispatch_id=did, generation_id=gen,
                                  argv=("-c", "import sys; sys.exit(7)")))
        self.assertEqual(proc.returncode, 1)
        events = self._events(proc)
        result = next(e for e in events if e.get("event") == "result")
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("error_kind"), "nonzero")
        self.assertEqual(result.get("exit_code"), 7)
        receipt = dse.read_dispatch_receipt(self.project, did)
        self.assertEqual(receipt.phase, "FINALIZING")  # type: ignore[union-attr]

    def test_launch_failure_stays_before_worker_started(self) -> None:
        did = "DSP-int-0003"
        gen = "GEN-int-0003"
        dse.reserve_receipt(
            self.project, task_id="TC-001", revision=1, attempt=1,
            dispatch_id=did, lease_epoch=1, holder_instance_id="inst",
            generation_id=gen, creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            boot_id=dse.get_boot_id(),
        )
        payload = _payload(self.project, dispatch_id=did, generation_id=gen,
                           argv=(), executable=str(self.project / "nope.exe"))
        proc = self._run(payload)
        self.assertEqual(proc.returncode, 1)
        events = self._events(proc)
        result = next(e for e in events if e.get("event") == "result")
        self.assertEqual(result.get("error_kind"), "launch")
        receipt = dse.read_dispatch_receipt(self.project, did)
        # Launch failed before WORKER_STARTED — phase stays SUPERVISOR_READY.
        self.assertEqual(receipt.phase, "SUPERVISOR_READY")  # type: ignore[union-attr]
        self.assertIsNone(receipt.worker_pid)  # type: ignore[union-attr]

    def test_supervisor_ready_before_worker_started(self) -> None:
        did = "DSP-int-0004"
        gen = "GEN-int-0004"
        dse.reserve_receipt(
            self.project, task_id="TC-001", revision=1, attempt=1,
            dispatch_id=did, lease_epoch=1, holder_instance_id="inst",
            generation_id=gen, creator_pid=os.getpid(),
            creator_creation_time=dse.get_process_creation_time(os.getpid()) or "",
            boot_id=dse.get_boot_id(),
        )
        proc = self._run(_payload(self.project, dispatch_id=did, generation_id=gen,
                                  argv=("-c", "pass")))
        events = self._events(proc)
        ready_idx = [i for i, e in enumerate(events) if e.get("event") == "ready"][0]
        ws_idx = [i for i, e in enumerate(events) if e.get("event") == "worker_started"][0]
        self.assertLess(ready_idx, ws_idx)
        # No worker_started event should precede the SUPERVISOR_READY receipt.
        receipt = dse.read_dispatch_receipt(self.project, did)
        self.assertEqual(receipt.phase, "FINALIZING")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
