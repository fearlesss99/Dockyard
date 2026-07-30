"""Durable Dispatch Supervisor Runner — TC-13.18d.12a-pre2.

Private subprocess entrypoint spawned by ``WorkflowOrchestrator`` for every
dispatch.  Owns the provider Worker subprocess so that the Worker is a child
of the supervisor (not the Orchestrator), letting the supervisor outlive a
creator crash.

Lifecycle (durable evidence under
``.agentdesk/runtime/dispatch-supervisor/<dispatch_id>.receipt.yaml``):

1. read sealed invocation payload from stdin (no prompt/argv/env on argv);
2. record own PID / creation time / boot id → write ``SUPERVISOR_READY``;
3. spawn the provider Worker via ``run_dispatch_from_invocation``;
4. in ``on_worker_started`` record worker PID / creation / process group and
   atomically write ``WORKER_STARTED`` *before* ``communicate()`` sends stdin;
5. emit a JSONL control line ``{"event":"worker_started",...}`` so the
   Orchestrator may apply ``DISPATCH_ACKNOWLEDGED``;
6. on Worker exit, write ``FINALIZING`` and emit a JSONL result frame.

Nothing the Worker produces (prompt, argv, env, stdout, stderr) is ever
persisted, logged, or placed in an exception message.  Only JSONL control
events are written to stdout; only generic ``error_kind`` strings to stderr.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# Sibling modules: running ``python <scripts>/dispatch_supervisor_runner.py``
# puts the scripts dir on sys.path[0].
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import dispatch_supervisor_evidence as dse  # noqa: E402
from dispatcher_gateway import (  # noqa: E402
    AgentCliInvocation,
    AgentCliProvider,
    DispatchCancelledError,
    DispatchIdentity,
    DispatchInvocationError,
    DispatchLaunchError,
    DispatchNonZeroExitError,
    DispatchRequest,
    DispatchResult,
    DispatchStarted,
    DispatchTimeoutError,
    ExecutableNotFoundError,
    resolve_invocation,
    run_dispatch_from_invocation,
)


def _emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_fatal(error_kind: str) -> None:
    _emit({"event": "fatal", "error_kind": error_kind})


def _b64(data: bytes | None) -> str | None:
    if data is None:
        return None
    return base64.b64encode(data).decode("ascii")


async def _read_payload() -> dict[str, object]:
    raw = await asyncio.get_event_loop().run_in_executor(
        None, sys.stdin.buffer.read
    )
    if not raw:
        raise ValueError("no payload on stdin")
    return json.loads(raw.decode("utf-8"))


def _build_invocation(payload: dict[str, object]) -> AgentCliInvocation:
    return AgentCliInvocation(
        executable=str(payload["executable"]),
        argv=tuple(str(a) for a in payload["argv"]),
        stdin=base64.b64decode(payload["stdin_b64"]) if payload.get("stdin_b64") else None,
        env_overrides=tuple(
            (str(k), str(v)) for k, v in payload["env_overrides"]
        ),
    )


def _build_identity(payload: dict[str, object]) -> DispatchIdentity:
    ident = payload["identity"]  # type: ignore[index]
    return DispatchIdentity(
        task_id=str(ident["task_id"]),
        revision=int(ident["revision"]),
        attempt=int(ident["attempt"]),
        dispatch_id=str(ident["dispatch_id"]),
    )


async def _on_worker_started(
    process: asyncio.subprocess.Process,
    started,  # noqa: ANN001 - DispatchStarted, unused here
    *,
    project_root: Path,
    dispatch_id: str,
    generation_id: str,
) -> None:
    worker_pid = process.pid
    worker_creation = dse.get_process_creation_time(worker_pid) or ""
    worker_pgid: int | None = None
    if os.name != "nt":
        try:
            worker_pgid = os.getpgid(worker_pid)
        except OSError:
            worker_pgid = worker_pid
    dse.advance_to_worker_started(
        project_root,
        dispatch_id=dispatch_id,
        generation_id=generation_id,
        worker_pid=worker_pid,  # type: ignore[arg-type]
        worker_creation_time=worker_creation,
        worker_process_group=worker_pgid,
    )
    _emit({"event": "worker_started", "worker_pid": worker_pid})


async def _amain() -> int:
    try:
        payload = await _read_payload()
    except Exception:
        _emit_fatal("bad_payload")
        return 2

    try:
        project_root = Path(str(payload["project_root"]))  # type: ignore[index]
        dispatch_id = str(payload["dispatch_id"])  # type: ignore[index]
        generation_id = str(payload["generation_id"])  # type: ignore[index]
        workspace = Path(str(payload["workspace"]))  # type: ignore[index]
        timeout_seconds = int(payload["timeout_seconds"])  # type: ignore[index]
        provider = str(payload["provider"])  # type: ignore[index]
        model_id = str(payload["model_id"])  # type: ignore[index]
        invocation = _build_invocation(payload)
        identity = _build_identity(payload)
    except Exception:
        _emit_fatal("bad_payload")
        return 2

    # ── SUPERVISOR_READY ───────────────────────────────────────────────
    try:
        sup_pid = os.getpid()
        sup_creation = dse.get_process_creation_time(sup_pid) or ""
        dse.advance_to_supervisor_ready(
            project_root,
            dispatch_id=dispatch_id,
            generation_id=generation_id,
            supervisor_pid=sup_pid,
            supervisor_creation_time=sup_creation,
        )
    except Exception:
        _emit_fatal("supervisor_ready_failed")
        return 2

    _emit({"event": "ready"})

    # ── spawn Worker under supervisor ─────────────────────────────────
    try:
        result = await run_dispatch_from_invocation(
            identity,
            workspace,
            timeout_seconds,
            invocation,
            provider,
            model_id,
            on_worker_started=lambda proc, started: _on_worker_started(
                proc,
                started,
                project_root=project_root,
                dispatch_id=dispatch_id,
                generation_id=generation_id,
            ),
        )
    except DispatchNonZeroExitError as exc:
        dse.advance_to_finalizing(
            project_root, dispatch_id=dispatch_id, generation_id=generation_id
        )
        _emit(
            {
                "event": "result",
                "status": "error",
                "error_kind": "nonzero",
                "exit_code": exc.exit_code,
                "stdout_b64": _b64(b""),  # never relay raw stdout
                "stderr_b64": _b64(b""),  # never relay raw stderr
                "stdout_sha256": exc.stdout_sha256,
                "stderr_sha256": exc.stderr_sha256,
                "duration_seconds": 0.0,
            }
        )
        return 1
    except DispatchTimeoutError:
        dse.advance_to_finalizing(
            project_root, dispatch_id=dispatch_id, generation_id=generation_id
        )
        _emit(
            {
                "event": "result",
                "status": "error",
                "error_kind": "timeout",
                "exit_code": -1,
                "stdout_b64": None,
                "stderr_b64": None,
                "stdout_sha256": None,
                "stderr_sha256": None,
                "duration_seconds": float(timeout_seconds),
            }
        )
        return 1
    except DispatchCancelledError:
        dse.advance_to_finalizing(
            project_root, dispatch_id=dispatch_id, generation_id=generation_id
        )
        _emit(
            {
                "event": "result",
                "status": "error",
                "error_kind": "cancelled",
                "exit_code": -1,
                "stdout_b64": None,
                "stderr_b64": None,
                "stdout_sha256": None,
                "stderr_sha256": None,
                "duration_seconds": 0.0,
            }
        )
        return 1
    except (DispatchLaunchError, ExecutableNotFoundError, DispatchInvocationError):
        _emit(
            {
                "event": "result",
                "status": "error",
                "error_kind": "launch",
                "exit_code": -1,
                "stdout_b64": None,
                "stderr_b64": None,
                "stdout_sha256": None,
                "stderr_sha256": None,
                "duration_seconds": 0.0,
            }
        )
        return 1

    # ── success ───────────────────────────────────────────────────────
    dse.advance_to_finalizing(
        project_root, dispatch_id=dispatch_id, generation_id=generation_id
    )
    _emit(
        {
            "event": "result",
            "status": "ok",
            "exit_code": 0,
            "stdout_b64": _b64(result.stdout),
            "stderr_b64": _b64(result.stderr),
            "stdout_sha256": result.stdout_sha256,
            "stderr_sha256": result.stderr_sha256,
            "duration_seconds": result.duration_seconds,
        }
    )
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())


# ── typed supervisor API (called by WorkerAdapter, not by the Orchestrator) ──
#
# All process launch, pipe communication, and output forwarding lives here.
# The Orchestrator never imports ``subprocess``/``asyncio.subprocess``; it
# reaches the supervisor only through ``WorkerAdapter.run_worker_observed``,
# which delegates here when the observer carries a ``generation_id``.

async def run_supervised_dispatch(
    request: DispatchRequest,
    providers: Mapping[str, AgentCliProvider],
    observer,  # noqa: ANN001 - Orchestrator _AckObserver (project_root/generation_id)
) -> DispatchResult:
    """Spawn the durable supervisor subprocess and relay its result.

    The supervisor owns the provider Worker.  ``on_dispatch_started`` is fired
    on *observer* only after the supervisor reports a durable ``WORKER_STARTED``
    receipt — never on the old in-process observer alone.  Prompt, argv, env,
    stdout, and stderr cross the process boundary only via the sealed stdin
    payload and the JSONL control pipe; none are persisted, logged, or placed
    in exception messages.
    """
    invocation, provider, model_id = resolve_invocation(request, providers)
    project_root = Path(str(observer.project_root))  # type: ignore[attr-defined]
    generation_id = str(observer.generation_id)  # type: ignore[attr-defined]
    payload = {
        "project_root": str(project_root),
        "dispatch_id": request.identity.dispatch_id,
        "generation_id": generation_id,
        "workspace": str(request.workspace),
        "timeout_seconds": request.timeout_seconds,
        "provider": provider,
        "model_id": model_id,
        "executable": invocation.executable,
        "argv": list(invocation.argv),
        "stdin_b64": (
            base64.b64encode(invocation.stdin).decode("ascii")
            if invocation.stdin is not None
            else None
        ),
        "env_overrides": [list(p) for p in invocation.env_overrides],
        "identity": {
            "task_id": request.identity.task_id,
            "revision": request.identity.revision,
            "attempt": request.identity.attempt,
            "dispatch_id": request.identity.dispatch_id,
        },
    }
    runner = Path(__file__).resolve()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(runner),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    result_event: dict[str, object] | None = None
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("supervisor ended without a result frame")
            event = json.loads(line.decode("utf-8").strip())
            kind = str(event.get("event"))
            if kind == "worker_started":
                started = DispatchStarted(
                    identity=request.identity,
                    provider=provider,
                    model_id=model_id,
                )
                await observer.on_dispatch_started(started)
            elif kind == "result":
                result_event = event
                break
        await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except BaseException:
            pass
        raise

    if result_event is None:
        raise RuntimeError("supervisor produced no result frame")
    status = str(result_event.get("status"))
    if status != "ok":
        error_kind = str(result_event.get("error_kind"))
        if error_kind == "nonzero":
            raise DispatchNonZeroExitError(
                exit_code=int(result_event.get("exit_code") or -1),
                stdout_sha256=str(result_event.get("stdout_sha256") or ""),
                stderr_sha256=str(result_event.get("stderr_sha256") or ""),
                stderr_bytes=b"",
            )
        if error_kind == "timeout":
            raise DispatchTimeoutError(request.timeout_seconds)
        if error_kind == "cancelled":
            raise DispatchCancelledError()
        raise DispatchLaunchError("supervisor reported launch failure")
    return DispatchResult(
        identity=request.identity,
        provider=provider,
        model_id=model_id,
        duration_seconds=float(result_event.get("duration_seconds") or 0.0),
        stdout=(
            base64.b64decode(result_event["stdout_b64"])  # type: ignore[arg-type]
            if result_event.get("stdout_b64")
            else b""
        ),
        stderr=(
            base64.b64decode(result_event["stderr_b64"])  # type: ignore[arg-type]
            if result_event.get("stderr_b64")
            else b""
        ),
        stdout_sha256=str(result_event.get("stdout_sha256") or ""),
        stderr_sha256=str(result_event.get("stderr_sha256") or ""),
    )
