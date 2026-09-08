from __future__ import annotations

import errno
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from dashboard_system.server import DashboardServer


class ReadPort:
    def overview(self) -> dict:
        return {"project": "Test", "run": {"status": "running"}}

    def main_memory(self, **kwargs) -> dict:
        return kwargs

    def main_record(self, record_id: str) -> dict | None:
        return {"id": record_id} if record_id == "FACT-1" else None

    def memory_graph(self) -> dict:
        return {"nodes": [{"id": "FACT-1", "type": "fact"}], "edges": [], "revision": 1}

    def explorer_memory(self, **kwargs) -> dict:
        return kwargs

    def monitor_snapshot(self) -> dict:
        return {"source_ids": []}


class Commands:
    def __init__(self) -> None:
        self.guidance = []
        self.feedback = []

    def submit_guidance(self, text: str) -> dict:
        self.guidance.append(text)
        return {"guidance_id": "HG-1", "status": "pending"}

    def submit_advisor_feedback(self, request_id: str, response: dict) -> dict:
        self.feedback.append((request_id, response))
        return {"command_id": "CMD-1", "status": "queued"}


class Model:
    def summarize(self, snapshot: dict) -> dict:
        raise AssertionError("Empty test memory must not launch the model")


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.read = ReadPort()
        self.commands = Commands()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        self.server = DashboardServer(self.read, self.commands, Model(), self.root / "cache", port=port)
        self.addCleanup(self.server.shutdown)
        static = self.root / "static"
        (static / "vendor" / "fonts").mkdir(parents=True)
        (static / "index.html").write_text("<!doctype html><title>Dashboard</title>")
        (static / "app.js").write_text("window.dashboard = true;")
        (static / "style.css").write_text("body { color: black; }")
        (static / "vendor" / "fonts" / "test.woff2").write_bytes(b"font")
        self.server._static = static.resolve()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, _, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.token = json.loads(body)["csrf_token"]

    def request(self, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.address, timeout=3)
        self.addCleanup(connection.close)
        supplied = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
            supplied.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def post(self, path: str, body, **headers):
        return self.request("POST", path, body, {"X-Dashboard-Token": self.token, **headers})

    def test_three_page_routes_share_static_shell(self) -> None:
        for path in ("/", "/main-memory", "/explorer-memory"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn(b"Dashboard", body)

    def test_assets_support_static_prefix_and_nested_vendor_fonts(self) -> None:
        for path, content_type in (("/static/app.js", "javascript"), ("/style.css", "text/css"),
                                   ("/static/vendor/fonts/test.woff2", "font/woff2")):
            status, headers, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(content_type, headers["Content-Type"])

    def test_overview_and_query_parameters_are_forwarded(self) -> None:
        self.assertEqual(json.loads(self.request("GET", "/api/overview")[2]), self.read.overview())
        self.assertEqual(json.loads(self.request("GET", "/api/memory-graph")[2]), self.read.memory_graph())
        response = json.loads(self.request("GET", "/api/main-memory?kind=fact&q=boundary&offset=20&limit=10")[2])
        self.assertEqual(response, {"kind": "fact", "query": "boundary", "offset": 20, "limit": 10})
        response = json.loads(self.request("GET", "/api/explorer-memory?type=summary&q=new&offset=2&limit=5")[2])
        self.assertEqual(response, {"record_type": "summary", "query": "new", "offset": 2, "limit": 5})

    def test_health_reports_the_host_assigned_instance_id(self) -> None:
        self.server.instance_id = "INSTANCE-TEST"
        self.assertEqual(json.loads(self.request("GET", "/api/health")[2]), {"instance_id": "INSTANCE-TEST"})

    def test_record_detail_and_missing_record(self) -> None:
        self.assertEqual(json.loads(self.request("GET", "/api/main-memory/FACT-1")[2]), {"id": "FACT-1"})
        self.assertEqual(self.request("GET", "/api/main-memory/MISSING")[0], 404)

    def test_invalid_pagination_and_explorer_type(self) -> None:
        for query in ("offset=-1", "limit=0", "limit=201", "offset=no", "limit=5&limit=6"):
            self.assertEqual(self.request("GET", "/api/main-memory?" + query)[0], 400)
        self.assertEqual(self.request("GET", "/api/explorer-memory?type=fact")[0], 400)

    def test_guidance_preserves_exact_human_text(self) -> None:
        text = "  # 建议\n\n研究 $X \\to S$。\n"
        status, _, body = self.post("/api/guidance", {"text": text}, Origin=self.server.url)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["guidance_id"], "HG-1")
        self.assertEqual(self.commands.guidance, [text])

    def test_advisor_feedback_is_forwarded_to_explicit_command_port(self) -> None:
        response = {"choices": [{"candidate_id": "C-1"}], "instructions": "Follow this choice."}
        self.assertEqual(self.post("/api/advisor-feedback", {"request_id": "REQ-1", "response": response})[0], 202)
        self.assertEqual(self.commands.feedback, [("REQ-1", response)])

    def test_missing_wrong_token_and_cross_origin_commands_are_rejected(self) -> None:
        self.assertEqual(self.request("POST", "/api/guidance", {"text": "no token"})[0], 403)
        self.assertEqual(self.request("POST", "/api/guidance", {"text": "bad token"}, {"X-Dashboard-Token": "bad"})[0], 403)
        self.assertEqual(self.request("POST", "/api/guidance", {"text": "bad token"}, {"X-Dashboard-Token": "é"})[0], 403)
        for origin in ("https://evil.example", "null", "http://127.0.0.1:1"):
            self.assertEqual(self.post("/api/guidance", {"text": "cross origin"}, Origin=origin)[0], 403)
        self.assertEqual(self.post("/api/guidance", {"text": "cross site"}, **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.commands.guidance, [])

    def test_invalid_host_is_rejected_even_for_session_token(self) -> None:
        self.assertEqual(self.request("GET", "/api/session", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/session", headers={"Host": "127.0.0.1:1"})[0], 403)

    def test_path_traversal_hidden_files_and_outside_symlinks_are_not_served(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("secret")
        (self.server._static / "vendor" / "escape.txt").symlink_to(secret)
        for path in ("/../secret.txt", "/static/vendor/%2e%2e/%2e%2e/secret.txt",
                     "/static/.secret", "/static/vendor/escape.txt", "/static/vendor/%5csecret",
                     "/static/vendor/%00bad", "/api/not-real"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_json_validation_size_limit_and_explicit_command_allowlist(self) -> None:
        self.assertEqual(self.post("/api/guidance", {"text": " "})[0], 400)
        self.assertEqual(self.post("/api/guidance", {"text": "a", "action": "delete"})[0], 400)
        self.assertEqual(self.post("/api/advisor-feedback", {"request_id": "R", "response": []})[0], 400)
        self.assertEqual(self.post("/api/delete", {})[0], 404)
        self.assertEqual(self.post("/api/monitor/refresh", {"force": True})[0], 400)
        self.assertEqual(self.request("POST", "/api/guidance", "{}", {"X-Dashboard-Token": self.token})[0], 415)
        self.assertEqual(self.request("POST", "/api/guidance", "{", {"X-Dashboard-Token": self.token, "Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/guidance", "{}", {"X-Dashboard-Token": self.token, "Content-Type": "application/json", "Content-Length": str(1024 * 1024 + 1)})[0], 413)
        self.assertEqual(self.commands.guidance, [])

    def test_cached_monitor_endpoint_and_manual_refresh(self) -> None:
        self.assertIn(json.loads(self.request("GET", "/api/monitor")[2])["status"], {"idle", "running"})
        self.assertEqual(self.post("/api/monitor/refresh", {})[0], 202)

    def test_headers_do_not_allow_cross_origin_reads_or_caching(self) -> None:
        _, headers, _ = self.request("GET", "/api/session")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_read_and_command_errors_are_contained(self) -> None:
        self.read.overview = lambda: (_ for _ in ()).throw(OSError("Broken read"))
        self.assertEqual(self.request("GET", "/api/overview")[0], 500)
        self.commands.submit_guidance = lambda text: (_ for _ in ()).throw(RuntimeError("Project is unavailable"))
        status, _, body = self.post("/api/guidance", {"text": "suggestion"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "Project is unavailable")
        self.assertEqual(self.request("GET", "/api/session")[0], 200)

    def test_occupied_port_increments_and_binds_loopback(self) -> None:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            if port == 65535:
                self.skipTest("Ephemeral port has no successor")
            server = DashboardServer(self.read, self.commands, Model(), self.root / "other", port=port, auto_start=False)
            self.addCleanup(server.shutdown)
            self.assertEqual(server.address[0], "127.0.0.1")
            self.assertGreater(server.address[1], port)
            self.assertEqual(server.url, f"http://127.0.0.1:{server.address[1]}")

    def test_non_address_in_use_error_is_not_retried(self) -> None:
        with mock.patch("dashboard_system.server._HTTPServer", side_effect=OSError(errno.EACCES, "denied")) as httpd:
            with self.assertRaises(OSError):
                DashboardServer(self.read, self.commands, Model(), self.root / "other", port=1113)
            self.assertEqual(httpd.call_count, 1)

    def test_loopback_server_startup_does_not_depend_on_reverse_dns(self) -> None:
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        with mock.patch("socket.getfqdn", side_effect=AssertionError("reverse DNS must not run")):
            server = DashboardServer(
                self.read, self.commands, Model(), self.root / "no-dns", port=port, auto_start=False,
            )
        self.addCleanup(server.shutdown)
        self.assertEqual(server._httpd.server_name, "127.0.0.1")
        self.assertEqual(server._httpd.server_port, server.address[1])

    def test_shutdown_is_idempotent_and_server_thread_exits(self) -> None:
        self.server.shutdown()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
