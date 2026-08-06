"""Serve the deterministic TC-13.29l browser fixture on loopback only."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_dockyard_local_e2e import DockyardLocalClosedLoopE2ETests
import workflow_orchestrator
from tests import test_workflow_orchestrator as workflow_fixtures


def _free_loopback_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--preload-review", action="store_true")
    args = parser.parse_args()
    case = DockyardLocalClosedLoopE2ETests(
        "test_requirement_to_explicit_acceptance_and_integration"
    )
    case.setUp()
    stopped = threading.Event()

    def stop(*_: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    assets = Path(__file__).resolve().parents[1] / "dist"
    if not (assets / "index.html").is_file():
        raise RuntimeError("build apps/dockyard-web before starting the E2E fixture")
    port = args.port or _free_loopback_port()
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    origin = f"http://127.0.0.1:{port}"
    case.config = replace(
        case.config,
        browser_origin=origin,
        web_assets_root=assets,
    )

    async def audit(*args: object, **kwargs: object):
        return workflow_fixtures._make_fake_audit_result("pass")

    try:
        with patch.object(workflow_orchestrator, "run_audit_gateway", audit):
            case._start(workers=True)
            assert case.result is not None
            if args.preload_review:
                pending = case._create_and_submit()
                status, result = case._post(
                    "/api/dockyard/v1/projects/PRJ-1/plans/PLAN-E2E/approve",
                    "plan.approve",
                    "CMD-BROWSER-PRELOAD",
                    str(pending["snapshot_commit"]),
                    int(pending["revision"]),
                    {
                        "plan_id": "PLAN-E2E",
                        "plan_digest": pending["plan_digest"],
                        "task_count": pending["task_count"],
                    },
                    confirmation_id="CONF-BROWSER-PRELOAD",
                )
                if status != 200 or result.get("outcome") != "materialized":
                    raise RuntimeError("browser review preload failed")
            print(json.dumps({
                "browser_url": case.result.browser_url,
                "project_id": "PRJ-1",
                "plan_id": "PLAN-E2E",
                "external_network": False,
                "preloaded_review": args.preload_review,
            }, separators=(",", ":")), flush=True)
            while not stopped.wait(30):
                pass
    finally:
        case.tearDown()


if __name__ == "__main__":
    main()
