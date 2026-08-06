"""TC-13.29f real-loopback Dockyard Control API tests."""

from __future__ import annotations

import hashlib
import http.client
import json
import sys
import unittest
from dataclasses import fields
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from dockyard_control_api import (  # noqa: E402
    DockyardApiConfig,
    DockyardCommandEnvelope,
    DockyardCommandReceipt,
    DockyardControlServer,
    DockyardErrorEnvelope,
    DockyardJsonResponse,
)
from dockyard_sse import DockyardSseHub  # noqa: E402
sys.path.pop(0)


class _ReadService:
    def __init__(self) -> None:
        self.requests = []
        self.failure = False

    def read(self, request):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError("SECRET-PATH-C:\\private")
        body = json.dumps(
            {"endpoint": request.endpoint, "project_id": request.project_id},
            separators=(",", ":"),
        ).encode("utf-8")
        return DockyardJsonResponse(200, body, '"snapshot-a"')


class _Gateway:
    def __init__(self) -> None:
        self.requests = []
        self.failure = False

    def execute(self, request):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError("SECRET-STDERR")
        envelope = request.envelope
        return DockyardCommandReceipt(
            "dockyard.command-receipt/v1",
            envelope.command_id,
            envelope.project_id,
            envelope.command_type,
            "committed",
            "EVT-1",
            "b" * 40,
            False,
            "2026-08-02T00:00:00Z",
            "c" * 64,
        )


class _Authorizer:
    def __init__(self) -> None:
        self.calls = []
        self.failure = False

    def authorize(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise RuntimeError("SECRET-TOKEN")


class DockyardControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.read_service = _ReadService()
        self.gateway = _Gateway()
        self.authorizer = _Authorizer()
        self.hub = DockyardSseHub(max_backlog=8, max_streams=2)
        self.server = DockyardControlServer(
            DockyardApiConfig(
                bind_host="127.0.0.1",
                port=0,
                allowed_hosts=("127.0.0.1", "localhost"),
                allowed_origins=("http://dockyard.local",),
                max_body_bytes=4096,
                sse_idle_seconds=0.05,
            ),
            self.read_service,
            self.gateway,
            self.authorizer,
            self.hub,
        )
        self.address = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.address.host, self.address.port, timeout=2)

    def command_body(
        self,
        *,
        command_type: str = "task.retry",
        project_id: str = "PRJ-1",
        payload: dict[str, object] | None = None,
    ) -> bytes:
        payload = {"attempt": 2} if payload is None else payload
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "schema_version": "dockyard.command/v1",
            "command_id": "CMD-1",
            "project_id": project_id,
            "command_type": command_type,
            "idempotency_key": "IDEMP-1",
            "expected_snapshot_commit": "a" * 40,
            "expected_revision": 1,
            "confirmation_id": "CONF-1",
            "payload_digest": hashlib.sha256(payload_json).hexdigest(),
        }
        return json.dumps({"envelope": envelope, "payload": payload}).encode("utf-8")

    def command_headers(self, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": "http://dockyard.local",
            "Authorization": "Bearer local-token",
            "X-Dockyard-Device-Id": "DEV-1",
            "X-Dockyard-CSRF": "CSRF-1",
            "X-Dockyard-Idempotency-Key": "IDEMP-1",
        }

    def request(self, method: str, path: str, body: bytes | None = None, headers=None):
        connection = self.connection()
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheaders(), data

    def test_health_read_uses_read_service(self) -> None:
        status, headers, body = self.request("GET", "/api/dockyard/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["endpoint"], "health")
        self.assertEqual(self.read_service.requests[0].endpoint, "health")
        self.assertIn(("ETag", '"snapshot-a"'), headers)

    def test_project_task_read_is_typed(self) -> None:
        status, _, body = self.request(
            "GET", "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project_id"], "PRJ-1")
        request = self.read_service.requests[0]
        self.assertEqual((request.endpoint, request.resource_id), ("task", "TC-1"))

    def test_unrecognized_host_is_rejected_before_read(self) -> None:
        status, _, _ = self.request(
            "GET",
            "/api/dockyard/v1/health",
            headers={"Host": "evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.read_service.requests, [])

    def test_path_traversal_has_no_file_surface(self) -> None:
        status, _, _ = self.request(
            "GET", "/api/dockyard/v1/projects/PRJ-1/%2e%2e/secrets"
        )
        self.assertEqual(status, 404)
        self.assertEqual(self.read_service.requests, [])

    def test_retry_command_delegates_once(self) -> None:
        body = self.command_body()
        status, _, response = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            self.command_headers(body),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["outcome"], "committed")
        self.assertEqual(len(self.authorizer.calls), 1)
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertEqual(self.gateway.requests[0].resource_id, "TC-1")

    def test_missing_origin_is_zero_gateway(self) -> None:
        body = self.command_body()
        headers = self.command_headers(body)
        del headers["Origin"]
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.gateway.requests, [])

    def test_wrong_content_type_is_zero_gateway(self) -> None:
        body = self.command_body()
        headers = self.command_headers(body)
        headers["Content-Type"] = "text/plain"
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            headers,
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.gateway.requests, [])

    def test_oversized_body_is_rejected_before_read(self) -> None:
        body = b"x" * 4097
        headers = self.command_headers(body)
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            headers,
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.gateway.requests, [])

    def test_cross_origin_is_rejected(self) -> None:
        body = self.command_body()
        headers = self.command_headers(body)
        headers["Origin"] = "http://evil.example"
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.gateway.requests, [])

    def test_method_override_is_rejected(self) -> None:
        body = self.command_body()
        headers = self.command_headers(body)
        headers["X-HTTP-Method-Override"] = "DELETE"
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.gateway.requests, [])

    def test_duplicate_json_key_is_rejected(self) -> None:
        body = self.command_body()
        text = body.decode("utf-8").replace(
            '"attempt": 2', '"attempt": 2, "attempt": 3'
        )
        duplicate = text.encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            duplicate,
            self.command_headers(duplicate),
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.gateway.requests, [])

    def test_payload_digest_substitution_is_rejected(self) -> None:
        document = json.loads(self.command_body())
        document["payload"]["attempt"] = 3
        body = json.dumps(document).encode("utf-8")
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            self.command_headers(body),
        )
        self.assertEqual(status, 409)
        self.assertEqual(self.gateway.requests, [])

    def test_command_route_substitution_is_rejected(self) -> None:
        body = self.command_body(command_type="task.cancel")
        status, _, _ = self.request(
            "POST",
            "/api/dockyard/v1/projects/PRJ-1/tasks/TC-1/retry",
            body,
            self.command_headers(body),
        )
        self.assertEqual(status, 409)

    def test_delete_method_is_not_a_generic_surface(self) -> None:
        status, _, _ = self.request("DELETE", "/api/dockyard/v1/projects/PRJ-1")
        self.assertEqual(status, 405)

    def test_internal_error_never_leaks_exception(self) -> None:
        self.read_service.failure = True
        status, _, body = self.request("GET", "/api/dockyard/v1/health")
        self.assertEqual(status, 500)
        rendered = body.decode("utf-8")
        self.assertNotIn("SECRET-PATH", rendered)
        self.assertNotIn("private", rendered)

    def test_sse_replays_typed_event(self) -> None:
        self.hub.publish(
            project_id="PRJ-1",
            event_type="task.changed",
            snapshot_commit="a" * 40,
            resource_id="TC-1",
            occurred_at="2026-08-02T00:00:00Z",
        )
        status, headers, body = self.request(
            "GET",
            "/api/dockyard/v1/projects/PRJ-1/events?after=0",
            headers={"Accept": "text/event-stream"},
        )
        self.assertEqual(status, 200)
        self.assertIn(("Content-Type", "text/event-stream; charset=utf-8"), headers)
        self.assertIn(b"event: task.changed", body)

    def test_sse_divergent_cursor_is_rejected(self) -> None:
        status, _, _ = self.request(
            "GET",
            "/api/dockyard/v1/projects/PRJ-1/events?after=1",
            headers={"Accept": "text/event-stream", "Last-Event-ID": "2"},
        )
        self.assertEqual(status, 409)

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DockyardApiConfig(
                bind_host="0.0.0.0",
                port=0,
                allowed_hosts=("localhost",),
                allowed_origins=("http://dockyard.local",),
            )


class DockyardPublicTypeTests(unittest.TestCase):
    def test_command_envelope_exact_nine_fields(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(DockyardCommandEnvelope)),
            (
                "schema_version",
                "command_id",
                "project_id",
                "command_type",
                "idempotency_key",
                "expected_snapshot_commit",
                "expected_revision",
                "confirmation_id",
                "payload_digest",
            ),
        )

    def test_command_receipt_exact_ten_fields(self) -> None:
        self.assertEqual(len(fields(DockyardCommandReceipt)), 10)

    def test_error_envelope_exact_seven_fields(self) -> None:
        self.assertEqual(len(fields(DockyardErrorEnvelope)), 7)


if __name__ == "__main__":
    unittest.main()
