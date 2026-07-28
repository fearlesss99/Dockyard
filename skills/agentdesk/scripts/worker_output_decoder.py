"""AgentDesk WorkerOutput Decoder — TC-13.9c.1.

Version-locked, fail-closed, pure-function decoder for Claude Code 2.1.214
Worker output.  Converts opaque ``WorkerResult`` bytes into typed,
trustable ``WorkerOutput`` and ``DeliveryReceipt``.

Supported:
  * provider: ``claude`` / ``claudecode``
  * CLI version: ``2.1.214``

Codex and any other provider / version are explicitly unsupported and
fail-closed with a typed exception.

Non-goals (explicitly excluded):
  * Real CLI, model, API, or network calls
  * Filesystem reads, PATH queries, environment variable checks
  * Git operations, commit ancestry validation
  * Retry, fallback decoding, auto-version detection
  * Modifying WorkerAdapter, WorkflowOrchestrator, or any provider module
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass

from dispatcher_gateway import DispatchIdentity, DispatchResult
from worker_adapter import WorkerResult

__all__ = [
    "WorkerCompletionStatus",
    "WorkerOutput",
    "DeliveryReceipt",
    "decode_worker_result",
    "require_delivery_receipt",
    "WorkerOutputError",
    "WorkerOutputUnsupportedProviderError",
    "WorkerOutputUnsupportedVersionError",
    "WorkerOutputIntegrityError",
    "WorkerOutputDecodeError",
    "WorkerOutputSchemaError",
    "WorkerOutputIdentityError",
]

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"claude", "claudecode"})
_SUPPORTED_VERSION: str = "2.1.214"

# Observed 20 top-level keys in Claude 2.1.214 JSON wrapper output.
_CLAUDE_WRAPPER_KEYS: frozenset[str] = frozenset({
    "type",
    "subtype",
    "is_error",
    "api_error_status",
    "duration_ms",
    "duration_api_ms",
    "ttft_ms",
    "ttft_stream_ms",
    "time_to_request_ms",
    "num_turns",
    "result",
    "stop_reason",
    "session_id",
    "total_cost_usd",
    "usage",
    "modelUsage",
    "permission_denials",
    "terminal_reason",
    "fast_mode_state",
    "uuid",
})

# AgentDesk Worker Completion Envelope exact 10 keys.
_ENVELOPE_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "status",
    "implementation_commit",
    "report_commit",
    "summary",
    "warnings",
})

_ENVELOPE_SCHEMA_VERSION: str = "agentdesk.worker-output/v1"

_COMMIT_HEX_RE: str = "^[0-9a-f]{40}$"

# ═══════════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════════


class WorkerCompletionStatus(str, enum.Enum):
    """Strict three-value Worker completion status.

    No case-folding, no aliases, no unknown fallback.
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"

    def __str__(self) -> str:
        return self.value


# ═══════════════════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class WorkerOutput:
    """Immutable nine-field decoded Worker output.

    Fields:
        identity: The ``DispatchIdentity`` from the dispatch.
        provider: Provider string (``claude`` or ``claudecode``).
        model_id: Model identifier from the dispatch snapshot.
        status: Decoded ``WorkerCompletionStatus``.
        implementation_commit: 40-char lowercase hex or None.
        report_commit: 40-char lowercase hex (always present).
        summary: Non-empty result summary text.
        warnings: Tuple of warning strings (may be empty).
        stdout_sha256: SHA-256 hex digest of the raw stdout bytes.
    """

    identity: DispatchIdentity
    provider: str
    model_id: str
    status: WorkerCompletionStatus
    implementation_commit: str | None
    report_commit: str
    summary: str
    warnings: tuple[str, ...]
    stdout_sha256: str


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Immutable six-field delivery receipt for completed work only.

    Fields:
        identity: The ``DispatchIdentity`` from the dispatch.
        provider: Provider string.
        model_id: Model identifier.
        implementation_commit: 40-char lowercase hex.
        report_commit: 40-char lowercase hex.
        stdout_sha256: SHA-256 hex digest.
    """

    identity: DispatchIdentity
    provider: str
    model_id: str
    implementation_commit: str
    report_commit: str
    stdout_sha256: str


# ═══════════════════════════════════════════════════════════════════════════════
# Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


class WorkerOutputError(Exception):
    """Base for all WorkerOutput decoder errors."""


class WorkerOutputUnsupportedProviderError(WorkerOutputError):
    """Provider is not supported by this decoder (e.g. ``codex``)."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"unsupported provider: {provider!r}; "
            f"expected one of {sorted(_SUPPORTED_PROVIDERS)}"
        )
        self.provider = provider


class WorkerOutputUnsupportedVersionError(WorkerOutputError):
    """CLI version is not supported by this decoder."""

    def __init__(self, version: object) -> None:
        super().__init__(
            f"unsupported version: {version!r}; "
            f"expected {_SUPPORTED_VERSION!r}"
        )
        self.version = version


class WorkerOutputIntegrityError(WorkerOutputError):
    """SHA-256 mismatch — raw stdout bytes do not match the expected digest."""

    def __init__(self) -> None:
        super().__init__("stdout SHA-256 mismatch — integrity check failed")


class WorkerOutputDecodeError(WorkerOutputError):
    """Failed to parse stdout as valid JSON or valid UTF-8."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"failed to decode stdout: {reason}")
        self.reason = reason


class WorkerOutputSchemaError(WorkerOutputError):
    """Structural schema violation in the wrapper or envelope JSON."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"schema violation: {detail}")
        self.detail = detail


class WorkerOutputIdentityError(WorkerOutputError):
    """Identity mismatch — envelope fields do not match DispatchIdentity."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"identity mismatch: {detail}")
        self.detail = detail


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _constant_time_sha256_eq(a: str, b: str) -> bool:
    """Constant-time comparison of two SHA-256 hex digest strings."""
    if len(a) != 64 or len(b) != 64:
        return False
    try:
        digest_a = bytes.fromhex(a)
        digest_b = bytes.fromhex(b)
    except (ValueError, TypeError):
        return False
    # Constant-time comparison via XOR
    if len(digest_a) != len(digest_b):
        return False
    result = 0
    for x, y in zip(digest_a, digest_b):
        result |= x ^ y
    return result == 0


def _validate_40_hex(value: str) -> None:
    """Validate that *value* is exactly 40 lowercase hex characters."""
    if not isinstance(value, str):
        raise WorkerOutputSchemaError(
            f"commit must be str, got {type(value).__name__}"
        )
    if len(value) != 40:
        raise WorkerOutputSchemaError(
            f"commit must be 40 hex chars, got length {len(value)}"
        )
    if not all(c in "0123456789abcdef" for c in value):
        raise WorkerOutputSchemaError(
            "commit must be lowercase hex"
        )


def _validate_commit_field(
    value: object,
    field_name: str,
) -> str | None:
    """Validate a commit field (null or 40-char lowercase hex)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkerOutputSchemaError(
            f"{field_name} must be null or str, got {type(value).__name__}"
        )
    _validate_40_hex(value)
    return value


def _validate_nonempty_string(value: object, field_name: str) -> str:
    """Validate *value* is a non-empty string without NUL."""
    if not isinstance(value, str):
        raise WorkerOutputSchemaError(
            f"{field_name} must be str, got {type(value).__name__}"
        )
    if not value:
        raise WorkerOutputSchemaError(
            f"{field_name} must not be empty"
        )
    if "\x00" in value:
        raise WorkerOutputSchemaError(
            f"{field_name} must not contain NUL"
        )
    return value


def _validate_warnings(value: object) -> tuple[str, ...]:
    """Validate the warnings field and return an immutable tuple copy."""
    if not isinstance(value, list):
        raise WorkerOutputSchemaError(
            f"warnings must be list, got {type(value).__name__}"
        )
    result: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise WorkerOutputSchemaError(
                f"warnings[{i}] must be str, got {type(item).__name__}"
            )
        if not item:
            raise WorkerOutputSchemaError(
                f"warnings[{i}] must not be empty"
            )
        stripped = item.strip()
        if stripped != item:
            raise WorkerOutputSchemaError(
                f"warnings[{i}] must not have leading/trailing whitespace"
            )
        if "\x00" in item:
            raise WorkerOutputSchemaError(
                f"warnings[{i}] must not contain NUL"
            )
        if "\r" in item or "\n" in item:
            raise WorkerOutputSchemaError(
                f"warnings[{i}] must not contain CR/LF"
            )
        if item in seen:
            raise WorkerOutputSchemaError(
                f"warnings[{i}] must not be a duplicate"
            )
        seen.add(item)
        result.append(item)
    return tuple(result)


def _validate_non_bool_int(value: object, field_name: str, min_value: int = 0) -> int:
    """Validate *value* is a non-bool int >= *min_value*."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerOutputSchemaError(
            f"{field_name} must be a non-bool int, got {type(value).__name__}"
        )
    if value < min_value:
        raise WorkerOutputSchemaError(
            f"{field_name} must be >= {min_value}, got {value}"
        )
    return value


# ═══════════════════════════════════════════════════════════════════════════════
# Claude 2.1.214 Wrapper Validation
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_json_strict(raw: bytes) -> dict[str, object]:
    """Strict JSON parse of *raw* bytes.

    Enforces:
    * Valid UTF-8, no BOM
    * Single JSON object root
    * No NaN, Infinity
    * No trailing text or code fences
    * No duplicate keys (via Python json.JSONDecoder with object_pairs_hook)

    Returns the parsed dict.
    """
    # 1. UTF-8 decode — no BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        raise WorkerOutputDecodeError("stdout contains UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerOutputDecodeError(
            f"stdout is not valid UTF-8: {exc.reason}"
        ) from None

    # 2. Strip surrounding whitespace for code-fence / prefix detection
    stripped = text.strip()
    if not stripped:
        raise WorkerOutputDecodeError("stdout is empty")

    # 3. Reject code fences
    if stripped.startswith("```"):
        raise WorkerOutputDecodeError("stdout contains markdown code fence")

    # 4. Must start with '{' (after strip)
    if not stripped.startswith("{"):
        raise WorkerOutputDecodeError(
            f"stdout root is not a JSON object (starts with {stripped[:20]!r})"
        )

    # 5. Parse with duplicate-key detection
    def _detect_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise WorkerOutputDecodeError(
                    f"duplicate JSON key: {key!r}"  # pragma: no cover
                )
            result[key] = value
        return result

    try:
        parsed = json.loads(
            stripped,
            parse_constant=_reject_special_floats,
            object_pairs_hook=_detect_duplicates,
        )
    except WorkerOutputDecodeError:
        raise
    except json.JSONDecodeError as exc:
        raise WorkerOutputDecodeError(
            f"invalid JSON: {exc.msg}"
        ) from None
    except ValueError as exc:
        raise WorkerOutputDecodeError(
            f"JSON parse error: {exc}"
        ) from None

    if not isinstance(parsed, dict):
        raise WorkerOutputDecodeError(
            f"JSON root must be object, got {type(parsed).__name__}"
        )

    # 6. Guard against trailing text by re-serializing and comparing
    #    (json.loads accepts trailing text; we must NOT)
    #    Re-serialize the parsed dict and compare lengths.
    re_encoded = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    # The re-serialized form may differ from the original in whitespace,
    # but the length should be <= original stripped length.
    # A more robust approach: after stripping, any trailing chars beyond
    # the JSON object are invalid.
    # We already checked starts with '{'.  We need to check that after
    # the parse position there's nothing but whitespace.
    idx = json.JSONDecoder().raw_decode(stripped)
    # raw_decode returns (obj, end_index)
    obj_hook, end_idx = idx[0], idx[1]
    trailing = stripped[end_idx:].strip()
    if trailing:
        raise WorkerOutputDecodeError(
            f"trailing text after JSON root"
        )

    return obj_hook  # type: ignore[return-value]


def _reject_special_floats(value: str) -> object:
    """Reject NaN and Infinity during JSON parsing."""
    raise WorkerOutputDecodeError(
        f"JSON contains forbidden constant: {value}"
    )


def _validate_claude_wrapper(obj: dict[str, object]) -> str:
    """Validate the Claude 2.1.214 wrapper JSON object.

    Returns the ``result`` string field value on success.
    """
    actual_keys = set(obj.keys())

    # Exact 20 keys
    if actual_keys != _CLAUDE_WRAPPER_KEYS:
        missing = _CLAUDE_WRAPPER_KEYS - actual_keys
        extra = actual_keys - _CLAUDE_WRAPPER_KEYS
        parts: list[str] = []
        if missing:
            parts.append(f"missing keys: {sorted(missing)}")
        if extra:
            parts.append(f"extra keys: {sorted(extra)}")
        raise WorkerOutputSchemaError("; ".join(parts))

    # type == "result"
    _type = obj["type"]
    if _type != "result":
        raise WorkerOutputSchemaError(
            f"type must be 'result', got {_type!r}"
        )

    # subtype == "success"
    _subtype = obj["subtype"]
    if _subtype != "success":
        raise WorkerOutputSchemaError(
            f"subtype must be 'success', got {_subtype!r}"
        )

    # is_error is False
    _is_error = obj["is_error"]
    if _is_error is not False:
        raise WorkerOutputSchemaError(
            f"is_error must be False, got {_is_error!r}"
        )

    # api_error_status is None
    _api_error_status = obj["api_error_status"]
    if _api_error_status is not None:
        raise WorkerOutputSchemaError(
            f"api_error_status must be None, got {_api_error_status!r}"
        )

    # result is non-empty str
    _result = obj["result"]
    if not isinstance(_result, str) or not _result:
        raise WorkerOutputSchemaError(
            f"result must be a non-empty str, got {_result!r}"
        )

    # duration/turn/time fields are non-bool int and non-negative
    _time_fields = [
        "duration_ms",
        "duration_api_ms",
        "ttft_ms",
        "ttft_stream_ms",
        "time_to_request_ms",
        "num_turns",
    ]
    for fname in _time_fields:
        val = obj[fname]
        if isinstance(val, bool) or not isinstance(val, int):
            raise WorkerOutputSchemaError(
                f"{fname} must be a non-bool int, got {type(val).__name__}"
            )
        if val < 0:
            raise WorkerOutputSchemaError(
                f"{fname} must be >= 0, got {val}"
            )

    # usage is object
    _usage = obj["usage"]
    if not isinstance(_usage, dict):
        raise WorkerOutputSchemaError(
            f"usage must be object, got {type(_usage).__name__}"
        )

    # modelUsage is object (dynamic keys allowed)
    _model_usage = obj["modelUsage"]
    if not isinstance(_model_usage, dict):
        raise WorkerOutputSchemaError(
            f"modelUsage must be object, got {type(_model_usage).__name__}"
        )

    # permission_denials is list
    _perm_denials = obj["permission_denials"]
    if not isinstance(_perm_denials, list):
        raise WorkerOutputSchemaError(
            f"permission_denials must be list, got {type(_perm_denials).__name__}"
        )

    # total_cost_usd is non-bool number or null
    _cost = obj["total_cost_usd"]
    if _cost is not None:
        if isinstance(_cost, bool) or not isinstance(_cost, (int, float)):
            raise WorkerOutputSchemaError(
                f"total_cost_usd must be number or null, "
                f"got {type(_cost).__name__}"
            )

    return _result


# ═══════════════════════════════════════════════════════════════════════════════
# AgentDesk Worker Completion Envelope Validation
# ═══════════════════════════════════════════════════════════════════════════════


def _validate_envelope(
    raw_result: str,
    identity: DispatchIdentity,
) -> dict[str, object]:
    """Parse and validate the AgentDesk Worker Completion Envelope.

    The *raw_result* is the Claude ``result`` field value — a JSON string
    that must itself be a single JSON object.

    Returns the parsed envelope dict.
    """
    # Parse as strict JSON
    result_bytes = raw_result.encode("utf-8")
    envelope = _parse_json_strict(result_bytes)

    actual_keys = set(envelope.keys())

    # Exact 10 keys
    if actual_keys != _ENVELOPE_KEYS:
        missing = _ENVELOPE_KEYS - actual_keys
        extra = actual_keys - _ENVELOPE_KEYS
        parts: list[str] = []
        if missing:
            parts.append(f"missing keys: {sorted(missing)}")
        if extra:
            parts.append(f"extra keys: {sorted(extra)}")
        raise WorkerOutputSchemaError("; ".join(parts))

    # schema_version exact match
    sv = envelope["schema_version"]
    if sv != _ENVELOPE_SCHEMA_VERSION:
        raise WorkerOutputSchemaError(
            f"schema_version must be {_ENVELOPE_SCHEMA_VERSION!r}, got {sv!r}"
        )

    # Identity match: task_id, revision, attempt, dispatch_id
    _check_identity_match(envelope, identity)

    # status is valid enum value
    _status_raw = envelope["status"]
    try:
        status = WorkerCompletionStatus(_status_raw)
    except ValueError:
        raise WorkerOutputSchemaError(
            f"status must be one of completed/partial/blocked, "
            f"got {_status_raw!r}"
        )

    # implementation_commit: null or 40-char lowercase hex
    impl_commit = _validate_commit_field(
        envelope["implementation_commit"], "implementation_commit"
    )

    # report_commit: always 40-char lowercase hex
    report_commit_raw = envelope["report_commit"]
    if not isinstance(report_commit_raw, str):
        raise WorkerOutputSchemaError(
            f"report_commit must be str, got {type(report_commit_raw).__name__}"
        )
    _validate_40_hex(report_commit_raw)
    report_commit: str = report_commit_raw

    # completed must have implementation_commit
    if status is WorkerCompletionStatus.COMPLETED:
        if impl_commit is None:
            raise WorkerOutputSchemaError(
                "completed status requires implementation_commit"
            )
        # Two commits must not be identical
        if impl_commit == report_commit:
            raise WorkerOutputSchemaError(
                "implementation_commit and report_commit must differ"
            )

    # summary: non-empty str, no NUL
    summary = _validate_nonempty_string(envelope["summary"], "summary")

    # warnings: list[str] — strict validation
    warnings = _validate_warnings(envelope["warnings"])

    return {
        "status": status,
        "implementation_commit": impl_commit,
        "report_commit": report_commit,
        "summary": summary,
        "warnings": warnings,
    }


def _check_identity_match(
    envelope: dict[str, object],
    identity: DispatchIdentity,
) -> None:
    """Verify envelope identity fields match DispatchIdentity exactly."""
    # task_id
    env_task_id = envelope["task_id"]
    if env_task_id != identity.task_id:
        raise WorkerOutputIdentityError(
            "task_id does not match DispatchIdentity"
        )

    # revision — non-bool int >= 1
    env_revision = envelope["revision"]
    _validate_non_bool_int(env_revision, "revision", min_value=1)
    if env_revision != identity.revision:
        raise WorkerOutputIdentityError(
            "revision does not match DispatchIdentity"
        )

    # attempt — non-bool int >= 1
    env_attempt = envelope["attempt"]
    _validate_non_bool_int(env_attempt, "attempt", min_value=1)
    if env_attempt != identity.attempt:
        raise WorkerOutputIdentityError(
            "attempt does not match DispatchIdentity"
        )

    # dispatch_id
    env_dispatch_id = envelope["dispatch_id"]
    if env_dispatch_id != identity.dispatch_id:
        raise WorkerOutputIdentityError(
            "dispatch_id does not match DispatchIdentity"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def decode_worker_result(
    result: WorkerResult,
    provider_cli_version: str,
) -> WorkerOutput:
    """Decode a ``WorkerResult`` into a typed ``WorkerOutput``.

    Args:
        result: Must be exactly a ``WorkerResult``.
        provider_cli_version: Non-empty string without whitespace envelope
            (e.g. ``"2.1.214"``).

    Returns:
        ``WorkerOutput`` on successful decode.

    Raises:
        TypeError: If *result* is not a ``WorkerResult``.
        ValueError: If *provider_cli_version* is empty or contains whitespace.
        WorkerOutputUnsupportedProviderError: Provider is not supported.
        WorkerOutputUnsupportedVersionError: Version is not supported.
        WorkerOutputIntegrityError: SHA-256 mismatch.
        WorkerOutputDecodeError: UTF-8 / JSON parse failure.
        WorkerOutputSchemaError: Wrapper or envelope schema violation.
        WorkerOutputIdentityError: Identity fields do not match.
    """
    # ── 1. Validate result type ──────────────────────────────────────────
    if not isinstance(result, WorkerResult):
        raise TypeError(
            f"result must be WorkerResult, got {type(result).__name__}"
        )

    # ── 2. Validate provider_cli_version ──────────────────────────────────
    if not isinstance(provider_cli_version, str):
        raise TypeError(
            "provider_cli_version must be str, "
            f"got {type(provider_cli_version).__name__}"
        )
    if not provider_cli_version:
        raise ValueError("provider_cli_version must not be empty")
    if provider_cli_version != provider_cli_version.strip() or any(
        c.isspace() for c in provider_cli_version
    ):
        raise ValueError(
            "provider_cli_version must not contain whitespace"
        )

    dispatch_result: DispatchResult = result.dispatch_result

    # ── 3. Provider boundary ─────────────────────────────────────────────
    provider = dispatch_result.provider

    if provider not in _SUPPORTED_PROVIDERS:
        if provider == "codex":
            raise WorkerOutputUnsupportedProviderError(provider)
        raise WorkerOutputUnsupportedProviderError(provider)

    if provider_cli_version != _SUPPORTED_VERSION:
        raise WorkerOutputUnsupportedVersionError(provider_cli_version)

    # ── 4. SHA-256 integrity check ───────────────────────────────────────
    actual_sha256 = hashlib.sha256(dispatch_result.stdout).hexdigest()
    expected_sha256 = dispatch_result.stdout_sha256

    if not _constant_time_sha256_eq(actual_sha256, expected_sha256):
        raise WorkerOutputIntegrityError()

    # ── 5. Parse Claude wrapper JSON ─────────────────────────────────────
    wrapper = _parse_json_strict(dispatch_result.stdout)
    inner_result = _validate_claude_wrapper(wrapper)

    # ── 6. Parse and validate completion envelope ────────────────────────
    envelope_data = _validate_envelope(inner_result, dispatch_result.identity)

    # ── 7. Construct WorkerOutput ────────────────────────────────────────
    return WorkerOutput(
        identity=dispatch_result.identity,
        provider=provider,
        model_id=dispatch_result.model_id,
        status=envelope_data["status"],  # type: ignore[arg-type]
        implementation_commit=envelope_data["implementation_commit"],  # type: ignore[arg-type]
        report_commit=envelope_data["report_commit"],  # type: ignore[arg-type]
        summary=envelope_data["summary"],  # type: ignore[arg-type]
        warnings=envelope_data["warnings"],  # type: ignore[arg-type]
        stdout_sha256=expected_sha256,
    )


def require_delivery_receipt(
    output: WorkerOutput,
) -> DeliveryReceipt:
    """Extract a ``DeliveryReceipt`` from a completed ``WorkerOutput``.

    Only ``COMPLETED`` status is allowed — ``PARTIAL`` and ``BLOCKED``
    raise ``WorkerOutputSchemaError``.

    Args:
        output: A validated ``WorkerOutput``.

    Returns:
        ``DeliveryReceipt`` with both commits and the SHA.

    Raises:
        TypeError: If *output* is not a ``WorkerOutput``.
        WorkerOutputSchemaError: If status is not ``COMPLETED`` or
            commits are missing/invalid.
    """
    if not isinstance(output, WorkerOutput):
        raise TypeError(
            f"output must be WorkerOutput, got {type(output).__name__}"
        )

    if output.status is not WorkerCompletionStatus.COMPLETED:
        raise WorkerOutputSchemaError(
            f"Cannot produce DeliveryReceipt: "
            f"status is {output.status.value!r}, "
            f"not {WorkerCompletionStatus.COMPLETED.value!r}"
        )

    # Both commits must be present for completed
    if output.implementation_commit is None:
        raise WorkerOutputSchemaError(
            "Cannot produce DeliveryReceipt: implementation_commit is None"
        )

    return DeliveryReceipt(
        identity=output.identity,
        provider=output.provider,
        model_id=output.model_id,
        implementation_commit=output.implementation_commit,
        report_commit=output.report_commit,
        stdout_sha256=output.stdout_sha256,
    )
