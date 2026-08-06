"""
Loopback OpenAPI-compatible stub for Reasonix permission-evidence testing.

This is NOT a real API server. It runs on 127.0.0.1 only, uses a
worthless test token, and deterministically returns tool-call sequences
that prove the three permission-evidence scenarios.

Phase A (zero network): this stub replaces the real Reasonix CLI
invocation at the subprocess level. The stub accepts the same argv
(--model, --profile, --max-steps, --output-format, --permission-mode,
etc.) and reads the prompt from stdin. Based on a deterministic prompt
keyword match, it produces one of three canned JSON wrapper outputs:

1. "PERM-EVIDENCE-EDIT"  -> success: edit a file in the worktree
2. "PERM-EVIDENCE-TEST"  -> success: run a targeted test
3. "PERM-EVIDENCE-REJECT" -> failure: attempt to write out-of-scope rejected

The stub writes the JSON wrapper to stdout and exits 0 (scenarios 1, 2)
or exits non-zero (scenario 3). Stderr receives progress text.

Usage (via reasonix_cli_provider):
    $env:REASONIX_TEST_EXECUTABLE = "python reasonix_loopback_stub.py"
    reasonix run --model deepseek-v4-flash --profile economy ...
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════════════════════
# Deterministic fixtures
# ═══════════════════════════════════════════════════════════════════════════════

_SUCCESS_WORKER_OUTPUT = {
    "schema_version": "agentdesk.worker-output/v1",
    "task_id": "TC-EVIDENCE-001",
    "revision": 1,
    "attempt": 1,
    "dispatch_id": "DSP-EVIDENCE-001",
    "status": "completed",
    "implementation_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "report_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "summary": (
        "Permission evidence: task completed successfully. "
        "The requested file edit and test execution were performed "
        "within the allowed worktree scope. "
    ),
    "warnings": [],
}

_REJECTED_WORKER_OUTPUT = {
    "schema_version": "agentdesk.worker-output/v1",
    "task_id": "TC-EVIDENCE-001",
    "revision": 1,
    "attempt": 1,
    "dispatch_id": "DSP-EVIDENCE-001",
    "status": "partial",
    "implementation_commit": None,
    "report_commit": "cccccccccccccccccccccccccccccccccccccccc",
    "summary": (
        "Permission evidence: out-of-scope operation was blocked. "
        "The requested write to a path outside the worktree was "
        "rejected by the permission policy."
    ),
    "warnings": [
        "permission denied: write outside allowed directory",
    ],
}

# Reasonix v1.19.1 JSON wrapper — success shape.
_SUCCESS_WRAPPER = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 150,
    "duration_api_ms": 120,
    "num_turns": 3,
    "result": json.dumps(_SUCCESS_WORKER_OUTPUT, ensure_ascii=False),
    "session_id": "evidence-session-001",
    "total_cost_usd": 0.0,
    "usage": {"input_tokens": 100, "output_tokens": 200},
    "modelUsage": {"deepseek-v4-flash": {"inputTokens": 100, "outputTokens": 200}},
    "permission_denials": [],
    "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
}

# Reasonix v1.19.1 JSON wrapper — failure shape (is_error=True, blocked).
_FAILURE_WRAPPER = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "duration_ms": 80,
    "duration_api_ms": 60,
    "num_turns": 2,
    "result": json.dumps(_REJECTED_WORKER_OUTPUT, ensure_ascii=False),
    "session_id": "evidence-session-002",
    "total_cost_usd": 0.0,
    "usage": {"input_tokens": 50, "output_tokens": 100},
    "modelUsage": {"deepseek-v4-flash": {"inputTokens": 50, "outputTokens": 100}},
    "permission_denials": [
        {"tool": "Write", "path": "../out_of_scope.txt"},
    ],
    "uuid": "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb",
}


def main() -> int:
    """Entry point — reads prompt from stdin, dispatches to scenario."""

    # Parse minimal argv to extract model and permission-mode for validation.
    argv = sys.argv[1:]
    model = None
    permission_mode = None
    output_format = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
            continue
        if arg == "--permission-mode" and i + 1 < len(argv):
            permission_mode = argv[i + 1]
            i += 2
            continue
        if arg == "--output-format" and i + 1 < len(argv):
            output_format = argv[i + 1]
            i += 2
            continue
        if arg == "--print":
            i += 1
            continue
        i += 1

    # Validate required flags
    if model != "deepseek-v4-flash":
        print(f"stub error: expected --model deepseek-v4-flash, got {model!r}",
              file=sys.stderr)
        return 2

    if output_format != "json":
        print(f"stub error: expected --output-format json, got {output_format!r}",
              file=sys.stderr)
        return 2

    # Read prompt from stdin
    try:
        prompt = sys.stdin.read()
    except Exception:
        prompt = ""

    # Progress to stderr
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] reasonix stub v1.19.1 — permission evidence mode",
          file=sys.stderr)
    print(f"[{timestamp}] model: {model}, permission_mode: {permission_mode}",
          file=sys.stderr)
    print(f"[{timestamp}] prompt length: {len(prompt)} chars", file=sys.stderr)

    # Dispatch to scenario based on prompt keyword
    prompt_lower = prompt.lower()

    if "perm-evidence-reject" in prompt_lower:
        return _emit_rejected()
    elif "perm-evidence-test" in prompt_lower:
        return _emit_success_test()
    elif "perm-evidence-edit" in prompt_lower:
        return _emit_success_edit()
    else:
        # Default: treat as a valid generic task and return success.
        return _emit_success_generic(prompt)


def _emit_success_edit() -> int:
    """Scenario 1: edit file in worktree — must succeed."""
    print("Progress: reading src/hello.py ... done.", file=sys.stderr)
    print("Progress: editing src/hello.py ... done.", file=sys.stderr)
    wrapper = dict(_SUCCESS_WRAPPER)
    envelope = dict(_SUCCESS_WORKER_OUTPUT)
    envelope["summary"] = (
        "Permission evidence (edit): modified src/hello.py in worktree. "
        "File edit succeeded within allowed directory scope."
    )
    wrapper["result"] = json.dumps(envelope, ensure_ascii=False)
    _emit_json(wrapper)
    return 0


def _emit_success_test() -> int:
    """Scenario 2: run targeted test — must succeed."""
    print("Progress: running python tests/test_hello.py ... done.", file=sys.stderr)
    print("Progress: test passed (1/1).", file=sys.stderr)
    wrapper = dict(_SUCCESS_WRAPPER)
    envelope = dict(_SUCCESS_WORKER_OUTPUT)
    envelope["summary"] = (
        "Permission evidence (test): executed python tests/test_hello.py "
        "successfully. Test command completed within allowed scope."
    )
    wrapper["result"] = json.dumps(envelope, ensure_ascii=False)
    _emit_json(wrapper)
    return 0


def _emit_rejected() -> int:
    """Scenario 3: write outside worktree — must be rejected."""
    print("Progress: attempting to write ../out_of_scope.txt ...", file=sys.stderr)
    print("Permission denied: write outside allowed directory.", file=sys.stderr)
    wrapper = dict(_FAILURE_WRAPPER)
    wrapper["result"] = json.dumps(_REJECTED_WORKER_OUTPUT, ensure_ascii=False)
    _emit_json(wrapper)
    # Exit non-zero to signal failure — the decoder must handle this.
    return 1


def _emit_success_generic(prompt: str) -> int:
    """Generic success for any other prompt during testing."""
    print("Progress: processing ... done.", file=sys.stderr)
    wrapper = dict(_SUCCESS_WRAPPER)
    envelope = dict(_SUCCESS_WORKER_OUTPUT)
    envelope["summary"] = (
        f"Generic task completed. Prompt preview: {prompt[:80]}..."
    )
    wrapper["result"] = json.dumps(envelope, ensure_ascii=False)
    _emit_json(wrapper)
    return 0


def _emit_json(obj: dict) -> None:
    """Write a single JSON object to stdout."""
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=None)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
