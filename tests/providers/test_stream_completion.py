"""Exercise the real HTTP reader against a server that keeps a finished SSE stream open."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from boardmodeler.providers.http_inference import HttpRequest, urllib_transport


def test_completed_stream_returns_without_waiting_for_connection_close():
    release = threading.Event()
    body = (
        b'data: {"choices":[{"index":0,"delta":{"content":"answer"},"finish_reason":"stop"}]}\r\n\r\n'
        b"data: [DONE]\r\n\r\n"
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            release.wait(5)
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    progress = []
    try:
        start = time.monotonic()
        response = urllib_transport(
            HttpRequest(
                "POST",
                f"http://127.0.0.1:{server.server_port}/",
                {},
                json.dumps({"stream": True}).encode(),
                2,
                progress.append,
            )
        )
        assert time.monotonic() - start < 1.5
        assert response.body == body
        assert any("receiving stream" in message for message in progress)
        assert progress[-1] == "API stream complete; decoding response"
        assert not any("answer" in message for message in progress)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_split_marker_is_detected_and_json_response_is_not_cut_off():
    from boardmodeler.providers.http_inference import _read_response_body

    class Stream:
        headers = {"Content-Type": "text/event-stream"}
        chunks = iter([b'data: {"choices":[]}\n\n', b"data: [DO", b"NE]\r", b"\n\r\n"])

        def read1(self, size):
            return next(self.chunks)

    assert _read_response_body(Stream(), time.monotonic() + 2).endswith(b"[DONE]\r\n\r\n")

    class Json:
        headers = {"Content-Type": "application/json"}
        chunks = iter([b'{"content":"data: [DONE]"}', b""])

        def read1(self, size):
            return next(self.chunks)

    assert json.loads(_read_response_body(Json(), time.monotonic() + 2)) == {
        "content": "data: [DONE]"
    }
