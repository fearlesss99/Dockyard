"""Durable exact DeliveryReceipt evidence used by Dockyard review resume."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from dispatcher_gateway import DispatchIdentity
from worker_output_decoder import DeliveryReceipt


class DockyardDeliveryReceiptStoreError(ValueError):
    pass


class DockyardDeliveryReceiptConflictError(DockyardDeliveryReceiptStoreError):
    pass


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


def _file(root: Path, dispatch_id: str, create: bool) -> Path:
    if type(dispatch_id) is not str or _ID.fullmatch(dispatch_id) is None:
        raise DockyardDeliveryReceiptStoreError("delivery_receipt:dispatch_id")
    directory = root / ".agentdesk" / "runtime" / "dockyard-delivery-receipts"
    try:
        if create:
            directory.mkdir(parents=False, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise DockyardDeliveryReceiptStoreError("delivery_receipt:directory")
        for item in directory.iterdir():
            if item.is_symlink() or not item.is_file() or re.fullmatch(r"DSP-[A-Za-z0-9._:-]+\.json", item.name) is None:
                raise DockyardDeliveryReceiptStoreError("delivery_receipt:extra_entry")
    except OSError as exc:
        raise DockyardDeliveryReceiptStoreError("delivery_receipt:filesystem") from exc
    return directory / f"{dispatch_id}.json"


def _encode(receipt: DeliveryReceipt) -> bytes:
    if type(receipt) is not DeliveryReceipt:
        raise DockyardDeliveryReceiptStoreError("delivery_receipt:type")
    body = {
        "task_id": receipt.identity.task_id,
        "revision": receipt.identity.revision,
        "attempt": receipt.identity.attempt,
        "dispatch_id": receipt.identity.dispatch_id,
        "provider": receipt.provider,
        "model_id": receipt.model_id,
        "implementation_commit": receipt.implementation_commit,
        "report_commit": receipt.report_commit,
        "stdout_sha256": receipt.stdout_sha256,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = {
        "schema_version": "dockyard.delivery-receipt/v1",
        "receipt": body,
        "content_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }
    return (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _decode(data: bytes) -> DeliveryReceipt:
    try:
        raw = json.loads(data.decode("utf-8"))
        if type(raw) is not dict or set(raw) != {"schema_version", "receipt", "content_digest"}:
            raise ValueError
        body = raw["receipt"]
        fields = {
            "task_id", "revision", "attempt", "dispatch_id", "provider",
            "model_id", "implementation_commit", "report_commit", "stdout_sha256",
        }
        if raw["schema_version"] != "dockyard.delivery-receipt/v1" or type(body) is not dict or set(body) != fields:
            raise ValueError
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if raw["content_digest"] != "sha256:" + hashlib.sha256(canonical).hexdigest():
            raise ValueError
        identity = DispatchIdentity(
            body["task_id"], body["revision"], body["attempt"], body["dispatch_id"]
        )
        return DeliveryReceipt(
            identity,
            body["provider"],
            body["model_id"],
            body["implementation_commit"],
            body["report_commit"],
            body["stdout_sha256"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise DockyardDeliveryReceiptStoreError("delivery_receipt:parse") from exc


class DockyardDeliveryReceiptStore:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardDeliveryReceiptStoreError("delivery_receipt:project_root")
        self.root = project_root

    def save(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        encoded = _encode(receipt)
        path = _file(self.root, receipt.identity.dispatch_id, True)
        try:
            if path.exists():
                current = path.read_bytes()
                if current != encoded:
                    raise DockyardDeliveryReceiptConflictError("delivery_receipt:replay")
                return _decode(current)
            temp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(8)}")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except DockyardDeliveryReceiptStoreError:
            raise
        except OSError as exc:
            raise DockyardDeliveryReceiptStoreError("delivery_receipt:write") from exc
        return receipt

    def read(self, dispatch_id: str) -> DeliveryReceipt:
        try:
            return _decode(_file(self.root, dispatch_id, False).read_bytes())
        except FileNotFoundError as exc:
            raise DockyardDeliveryReceiptStoreError("delivery_receipt:not_found") from exc
        except OSError as exc:
            raise DockyardDeliveryReceiptStoreError("delivery_receipt:read") from exc


__all__ = [
    "DockyardDeliveryReceiptStore",
    "DockyardDeliveryReceiptStoreError",
    "DockyardDeliveryReceiptConflictError",
]
