"""Durable evidence that binds one returned-delivery redispatch completion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

SCHEMA_VERSION = "dockyard.redispatch-receipt/v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class DockyardRedispatchStoreError(ValueError):
    pass


class DockyardRedispatchConflictError(DockyardRedispatchStoreError):
    pass


class DockyardRedispatchNotFoundError(DockyardRedispatchStoreError):
    pass


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise DockyardRedispatchStoreError(f"redispatch:{field}")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise DockyardRedispatchStoreError(f"redispatch:{field}")
    return value


def _positive(value: object, field: str, maximum: int | None = None) -> int:
    if (
        type(value) is not int
        or value < 1
        or (maximum is not None and value > maximum)
    ):
        raise DockyardRedispatchStoreError(f"redispatch:{field}")
    return value


@dataclass(frozen=True, slots=True)
class DockyardRedispatchReceipt:
    schema_version: str
    command_id: str
    source_review_id: str
    source_review_receipt_digest: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    generation_id: str
    dispatch_event_id: str
    dispatch_event_digest: str
    acknowledge_event_id: str
    acknowledge_event_digest: str
    delivery_event_id: str
    delivery_event_digest: str
    delivery_receipt_digest: str
    prompt_digest: str
    finalized_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DockyardRedispatchStoreError("redispatch:schema_version")
        for field in (
            "command_id", "source_review_id", "task_id", "dispatch_id",
            "generation_id", "dispatch_event_id", "acknowledge_event_id",
            "delivery_event_id",
        ):
            _identifier(getattr(self, field), field)
        _positive(self.revision, "revision")
        _positive(self.attempt, "attempt", 3)
        for field in (
            "source_review_receipt_digest", "dispatch_event_digest",
            "acknowledge_event_digest", "delivery_event_digest",
            "delivery_receipt_digest", "prompt_digest", "content_digest",
        ):
            _digest(getattr(self, field), field)
        if (
            type(self.finalized_at) is not str
            or not self.finalized_at.endswith("Z")
            or "T" not in self.finalized_at
        ):
            raise DockyardRedispatchStoreError("redispatch:finalized_at")


def with_content_digest(
    receipt: DockyardRedispatchReceipt,
) -> DockyardRedispatchReceipt:
    if type(receipt) is not DockyardRedispatchReceipt:
        raise DockyardRedispatchStoreError("redispatch:receipt_type")
    values = tuple(
        str(getattr(receipt, field.name))
        for field in fields(receipt)
        if field.name != "content_digest"
    )
    encoded = "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")
    return replace(
        receipt,
        content_digest="sha256:" + hashlib.sha256(encoded).hexdigest(),
    )


def _encode(receipt: DockyardRedispatchReceipt) -> bytes:
    if with_content_digest(receipt) != receipt:
        raise DockyardRedispatchStoreError("redispatch:content_digest")
    return (
        json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _decode(raw: bytes) -> DockyardRedispatchReceipt:
    try:
        value = json.loads(raw.decode("utf-8"))
        if type(value) is not dict or tuple(value) != tuple(
            sorted(field.name for field in fields(DockyardRedispatchReceipt))
        ):
            raise ValueError
        receipt = DockyardRedispatchReceipt(**value)
        if with_content_digest(receipt) != receipt:
            raise ValueError
        return receipt
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise DockyardRedispatchStoreError("redispatch:parse") from exc


class DockyardRedispatchReceiptStore:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardRedispatchStoreError("redispatch:project_root")
        self.root = project_root

    def _path(self, dispatch_id: str, *, create: bool) -> Path:
        dispatch_id = _identifier(dispatch_id, "dispatch_id")
        directory = self.root / ".agentdesk/runtime/dockyard-redispatch"
        try:
            if create:
                directory.mkdir(parents=False, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise DockyardRedispatchStoreError("redispatch:directory")
            for item in directory.iterdir():
                if (
                    item.is_symlink()
                    or not item.is_file()
                    or not item.name.startswith("DSP-")
                    or item.suffix != ".json"
                ):
                    raise DockyardRedispatchStoreError("redispatch:extra_entry")
        except OSError as exc:
            raise DockyardRedispatchStoreError("redispatch:filesystem") from exc
        return directory / f"{dispatch_id}.json"

    def save(self, receipt: DockyardRedispatchReceipt) -> DockyardRedispatchReceipt:
        encoded = _encode(receipt)
        path = self._path(receipt.dispatch_id, create=True)
        temporary: Path | None = None
        try:
            if path.exists():
                current = path.read_bytes()
                if current != encoded:
                    raise DockyardRedispatchConflictError("redispatch:replay")
                return _decode(current)
            temporary = path.parent.parent / (
                ".dockyard-redispatch-"
                + path.name
                + f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
            )
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                current = path.read_bytes()
                if current != encoded:
                    raise DockyardRedispatchConflictError("redispatch:replay")
                return _decode(current)
        except DockyardRedispatchStoreError:
            raise
        except OSError as exc:
            raise DockyardRedispatchStoreError("redispatch:write") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return receipt

    def read(self, dispatch_id: str) -> DockyardRedispatchReceipt:
        try:
            return _decode(self._path(dispatch_id, create=False).read_bytes())
        except FileNotFoundError as exc:
            raise DockyardRedispatchNotFoundError("redispatch:not_found") from exc
        except OSError as exc:
            raise DockyardRedispatchStoreError("redispatch:read") from exc


__all__ = [
    "SCHEMA_VERSION",
    "DockyardRedispatchConflictError",
    "DockyardRedispatchNotFoundError",
    "DockyardRedispatchReceipt",
    "DockyardRedispatchReceiptStore",
    "DockyardRedispatchStoreError",
    "with_content_digest",
]
