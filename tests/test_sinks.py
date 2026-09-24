import http.server
import json
import os
import socket
import socketserver
import threading
import time
import unittest

from .base import PluginTestCase
from .helpers import submodule

sinks = submodule("sinks")
version = submodule("version")


def _record(event="write_done", **extra):
    rec = {
        "ts": "2026-09-09T19:35:50Z",
        "schema": 1,
        "plugin": "gitlab",
        "event": event,
        "gitlab_url": "https://gitlab.example.com",
        "action": "mr.merge",
        "project": "platform/api",
        "target": {
            "kind": "merge_request",
            "iid": 10,
            "web_url": "https://gitlab.example.com/platform/api/-/merge_requests/10",
        },
        "staged_id": "glw-20260909T193512Z-4f1a",
        "actor": {
            "kind": "model",
            "via": "tool",
            "platform": "signal",
            "user_name": "Andrew",
            "chat_type": "dm",
            "chat_name": "Andrew",
            "request": "Merge !10 | now",
        },
        "host": "hermes-01",
        "pid": 4242,
    }
    rec.update(extra)
    return rec


class Severity(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(sinks.severity_of(_record("write_refused")), sinks.SEV_WARNING)
        self.assertEqual(sinks.severity_of(_record("write_failed")), sinks.SEV_WARNING)
        self.assertEqual(sinks.severity_of(_record("write_done")), sinks.SEV_NOTICE)
        self.assertEqual(sinks.severity_of(_record("write_staged")), sinks.SEV_NOTICE)
        self.assertEqual(sinks.severity_of(_record("staged_run", outcome="done")), sinks.SEV_NOTICE)
        self.assertEqual(sinks.severity_of(_record("staged_run", outcome="failed")), sinks.SEV_WARNING)
        self.assertEqual(sinks.severity_of(_record("read_done")), sinks.SEV_INFO)

    def test_cef(self):
        line = sinks.to_cef(_record("write_refused", reason="allow_merge is off", http_status=None))
        self.assertTrue(
            line.startswith(
                f"CEF:0|andrewvieyra|hermes-plugin-gitlab|{version.__version__}|write_refused|write refused|7|"
            )
        )
        self.assertIn("act=mr.merge", line)
        self.assertIn("cs6=platform/api", line)
        self.assertIn("externalId=10", line)
        self.assertIn("suser=Andrew", line)
        self.assertIn("msg=allow_merge is off", line)
        self.assertIn("cs8=Merge !10 | now", line)  # pipes are only escaped in the header
        from datetime import datetime, timezone

        expected_rt = int(datetime(2026, 9, 9, 19, 35, 50, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertIn(f"rt={expected_rt}", line)
        self.assertIn("cs7=https://gitlab.example.com/platform/api/-/merge_requests/10", line)
        self.assertNotIn("|1|", sinks.to_cef(_record("write_done", http_status=200)).split("|", 7)[7])

    def test_expand_env(self):
        os.environ["HERMES_TEST_TOKEN"] = "abc"
        try:
            self.assertEqual(sinks.expand_env("Splunk ${HERMES_TEST_TOKEN}"), "Splunk abc")
            self.assertEqual(sinks.expand_env("${HERMES_MISSING_XYZ}"), "")
        finally:
            del os.environ["HERMES_TEST_TOKEN"]

    def test_build_sink_validation(self):
        self.assertIsNone(sinks.build_sink({"type": "syslog"}))
        self.assertIsNone(sinks.build_sink({"type": "http", "url": "ftp://x"}))
        self.assertIsNone(sinks.build_sink({"type": "carrier-pigeon"}))
        self.assertIsNone(sinks.build_sink("junk"))
        self.assertIsNone(sinks.build_sink({"type": "syslog", "host": "x", "protocol": "smoke"}))
        self.assertIsNone(sinks.build_sink({"type": "syslog", "host": "x", "format": "xml"}))
        good = sinks.build_sink(
            {"type": "syslog", "host": "h", "format": "cef", "protocol": "tcp", "facility": "local3"}
        )
        self.assertIsInstance(good, sinks.SyslogSink)
        self.assertEqual((good.protocol, good.fmt, good.facility, good.framing), ("tcp", "cef", 19, "newline"))
        self.assertEqual(sinks.build_sink({"type": "syslog", "host": "h", "protocol": "tls"}).framing, "octet-counting")
        self.assertIsInstance(
            sinks.build_sink({"type": "http", "url": "https://x/collector", "format": "hec"}), sinks.HttpSink
        )
        worker = sinks.configure([{"type": "syslog", "host": "10.0.0.5"}, {"type": "bogus"}])
        self.assertEqual(len(worker.sinks), 1)
        worker.close(0.5)

    def test_rfc5424_message_shape(self):
        sink = sinks.SyslogSink("127.0.0.1", 514, facility="local0", app_name="hermes-gitlab")
        msg = sink.message(_record("read_done"))
        self.assertTrue(msg.startswith("<134>1 2026-09-09T19:35:50Z "), msg)  # local0 (16) * 8 + info (6)
        head, body = msg.split(" - ", 1)
        self.assertEqual(head.split(" ")[3:6], ["hermes-gitlab", "4242", "read_done"])
        self.assertEqual(json.loads(body)["event"], "read_done")
        self.assertTrue(sink.message(_record("write_refused")).startswith("<132>1 "))

    def test_cef_escaping(self):
        self.assertIn("|x\\|y|", sinks.to_cef(_record("x|y")))  # header pipes are escaped
        self.assertEqual(sinks._cef_ext("a=b\\c\nd"), "a\\=b\\\\c\\nd")  # extension: = \ and newlines
        self.assertIn("cn1Label=http_status cn1=403", sinks.to_cef(_record("write_failed", http_status=403)))

    def test_no_sinks_means_no_thread(self):
        worker = sinks.SinkWorker([])
        self.assertFalse(worker.enqueue(_record()))
        self.assertFalse(worker._thread.is_alive())


def _serve(factory):
    """Bind a local test server, or skip: some sandboxes forbid bind() entirely."""
    try:
        return factory()
    except PermissionError as exc:  # pragma: no cover - environment dependent
        raise unittest.SkipTest(f"cannot bind local sockets here: {exc}") from exc


def _free_port():
    """A port nothing listens on, so a sink pointed at it fails fast with a connection refusal."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(predicate, seconds=2.5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)


class _UDP(socketserver.UDPServer):
    allow_reuse_address = True


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.received.append(self.request[0])


class _HTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.received.append((dict(self.headers), self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        return None


class Delivery(PluginTestCase):
    def test_udp_syslog_json(self):
        server = _serve(lambda: _UDP(("127.0.0.1", 0), _UDPHandler))
        server.received = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sink = sinks.SyslogSink(
                "127.0.0.1", server.server_address[1], protocol="udp", fmt="json", facility="local3"
            )
            worker = sinks.SinkWorker([sink])
            worker.enqueue(_record("write_done"))
            self.assertTrue(worker.flush(5))
            worker.close()
            deadline = 50
            while not server.received and deadline:
                deadline -= 1
                threading.Event().wait(0.05)
            message = server.received[0].decode()
            # PRI = facility * 8 + severity: local3 (19) * 8 + notice (5)
            self.assertTrue(message.startswith(f"<{19 * 8 + sinks.SEV_NOTICE}>1 2026-09-09T19:35:50Z "), message)
            self.assertIn(" hermes-gitlab 4242 write_done - {", message)
            self.assertEqual(json.loads(message.split(" - ", 1)[1])["event"], "write_done")
        finally:
            server.shutdown()
            server.server_close()

    def test_http_hec(self):
        server = _serve(lambda: http.server.HTTPServer(("127.0.0.1", 0), _HTTPHandler))
        server.received = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["HERMES_TEST_HEC"] = "tok"
        try:
            sink = sinks.HttpSink(
                f"http://127.0.0.1:{server.server_address[1]}/collector",
                headers={"Authorization": "Splunk ${HERMES_TEST_HEC}"},
                fmt="hec",
            )
            worker = sinks.SinkWorker([sink])
            worker.enqueue(_record("write_refused"))
            self.assertTrue(worker.flush(5))
            worker.close()
            headers, body = server.received[0]
            self.assertEqual(headers["Authorization"], "Splunk tok")
            payload = json.loads(body)
            self.assertEqual(payload["sourcetype"], "hermes:gitlab")
            self.assertEqual(payload["event"]["event"], "write_refused")
            self.assertEqual(payload["host"], "hermes-01")
        finally:
            del os.environ["HERMES_TEST_HEC"]
            server.shutdown()
            server.server_close()

    def test_failures_and_full_queue_never_raise(self):
        class Broken(sinks.Sink):
            name = "broken"

            def send(self, record):
                raise OSError("down")

        worker = sinks.SinkWorker([Broken()], queue_size=2, retries=2, backoff=0.0)
        for _ in range(5):
            worker.enqueue(_record())
        self.assertGreaterEqual(worker.stats["dropped"], 1)
        worker.flush(5)
        worker.close()
        self.assertGreaterEqual(worker.stats["failed"], 1)

    def test_audit_dispatches_to_configured_sinks(self):
        server = _serve(lambda: _UDP(("127.0.0.1", 0), _UDPHandler))
        server.received = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.configure(audit_sinks=[{"type": "syslog", "host": "127.0.0.1", "port": server.server_address[1]}])
            self.sinks.set_worker(None)
            self.call("gitlab_issue_write", project=1, action="comment", iid=1, body="hi")
            self.call("gitlab_mr_write", project=1, action="merge", iid=10, sha="0" * 40)  # refused: allow_merge off
            self.assertTrue(self.sinks.get_worker().flush(5))
            file_events = [e["event"] for e in self.events()]
            _wait(lambda: len(server.received) >= len(file_events))
            wire_events = [json.loads(d.decode().split(" - ", 1)[1])["event"] for d in server.received]
            # every record in audit.jsonl reaches the wire, in order
            self.assertEqual(wire_events, file_events, self.sinks.get_worker().stats)
            self.assertIn("write_done", wire_events)
            self.assertIn("write_refused", wire_events)
        finally:
            server.shutdown()
            server.server_close()

    def test_worker_built_from_settings(self):
        self.configure(audit_sinks=[{"type": "syslog", "host": "127.0.0.1", "port": 1}])
        self.sinks.set_worker(None)
        worker = self.sinks.get_worker()
        self.assertEqual(len(worker.sinks), 1)
        self.assertEqual(worker.sinks[0].name, "syslog udp://127.0.0.1:1")

    def test_unreachable_sink_is_retried_then_dropped_quickly(self):
        port = _serve(_free_port)
        dead = sinks.HttpSink(f"http://127.0.0.1:{port}/collector", timeout=0.5)
        worker = sinks.SinkWorker([dead], retries=2, backoff=0.01)
        started = time.monotonic()
        worker.enqueue(_record())
        self.assertTrue(worker.flush(10))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual((worker.stats["sent"], worker.stats["failed"]), (0, 1))
        worker.close(1)

    def test_tcp_syslog_framing(self):
        server = _serve(lambda: socketserver.TCPServer(("127.0.0.1", 0), socketserver.StreamRequestHandler))
        server.allow_reuse_address = True
        received = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                received.append(self.rfile.readline())

        server.RequestHandlerClass = Handler
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sink = sinks.SyslogSink("127.0.0.1", server.server_address[1], protocol="tcp", fmt="cef")
            sink.send(_record())
            sink.close()
            deadline = 50
            while not received and deadline:
                deadline -= 1
                threading.Event().wait(0.05)
            self.assertTrue(received[0].endswith(b"\n"))
            self.assertIn(b"CEF:0|andrewvieyra|hermes-plugin-gitlab|", received[0])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
