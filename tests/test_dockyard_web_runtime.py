"""Directed TC-13.29l.6 loopback Web shell tests."""

from __future__ import annotations

import http.client
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_web_runtime import (
    DockyardBrowserBootstrap,
    DockyardWebRuntime,
    DockyardWebRuntimeConfig,
)


class DockyardWebRuntimeTests(unittest.TestCase):
    def test_production_index_bootstraps_before_head_closes(self) -> None:
        production_index = (
            Path(__file__).resolve().parents[1]
            / "apps"
            / "dockyard-web"
            / "index.html"
        ).read_text(encoding="utf-8")
        runtime_offset = production_index.index(
            '<script src="/dockyard-runtime.js"></script>'
        )
        self.assertLess(runtime_offset, production_index.index("</head>"))
        self.assertLess(runtime_offset, production_index.index("/src/main.tsx"))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "dist"
        self.root.mkdir()
        (self.root / "index.html").write_text(
            "<!doctype html><div id='root'></div>"
            "<script src='/dockyard-runtime.js'></script>"
            "<script src='/app.js'></script>\n",
            encoding="utf-8",
        )
        (self.root / "app.js").write_text(
            "document.getElementById('root').textContent="
            "window.__DOCKYARD_LOOPBACK__.projectId;\n",
            encoding="utf-8",
        )
        self.bootstrap = DockyardBrowserBootstrap(
            "http://127.0.0.1:43210",
            "PRJ-1",
            "DEV-1",
            "TOKEN-SECRET",
            "CSRF-SECRET",
        )
        self.runtime = DockyardWebRuntime(DockyardWebRuntimeConfig(
            self.root, self.bootstrap, "127.0.0.1", 0
        ))
        self.result = self.runtime.start()

    def tearDown(self) -> None:
        self.runtime.stop()
        self.temp.cleanup()

    def _get(self, path: str, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection(
            self.result.host, self.result.port, timeout=5
        )
        connection.request("GET", path, headers={} if headers is None else headers)
        response = connection.getresponse()
        body = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, body

    def test_index_orders_bootstrap_before_application(self) -> None:
        status, _, body = self._get("/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertLess(text.index("dockyard-runtime.js"), text.index("app.js"))
        route_status, _, route_body = self._get("/requirements")
        self.assertEqual(route_status, 200)
        self.assertEqual(route_body, body)

    def test_runtime_script_is_exact_in_memory_and_no_store(self) -> None:
        status, headers, body = self._get("/dockyard-runtime.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, self.bootstrap.script_bytes())
        self.assertIn(b"TOKEN-SECRET", body)
        self.assertNotIn(str(self.root).encode("utf-8"), body)
        self.assertFalse((self.root / "dockyard-runtime.js").exists())
        self.assertNotIn("TOKEN-SECRET", repr(self.runtime))

    def test_host_origin_traversal_and_methods_fail_closed(self) -> None:
        status, _, _ = self._get("/", {"Host": "example.invalid"})
        self.assertEqual(status, 421)
        status, _, _ = self._get(
            "/", {"Origin": "http://example.invalid"}
        )
        self.assertEqual(status, 403)
        status, _, _ = self._get("/%2e%2e/secret")
        self.assertEqual(status, 404)
        connection = http.client.HTTPConnection(
            self.result.host, self.result.port, timeout=5
        )
        connection.request("POST", "/")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 405)

    def test_symlink_asset_is_rejected_when_supported(self) -> None:
        outside = Path(self.temp.name) / "outside.js"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "linked.js"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("file symlink creation is unavailable")
        status, _, body = self._get("/linked.js")
        self.assertEqual(status, 404)
        self.assertNotIn(b"secret", body)

    def test_start_stop_are_idempotent_and_release_listener(self) -> None:
        self.assertEqual(self.runtime.start(), self.result)
        address = (self.result.host, self.result.port)
        self.runtime.stop()
        self.runtime.stop()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            self.assertNotEqual(probe.connect_ex(address), 0)
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
