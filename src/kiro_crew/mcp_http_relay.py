"""stdio -> HTTP MCP relay for HTTP-only ACP harnesses.

Measured need (muse-code-acp 0.11.0): the adapter's ``session/new``
``mcpServers`` element schema accepts ``{name, type: "http", url, headers}``
and rejects stdio-shaped elements with ``-32602``. Crew delivers its tools
as stdio array elements, so without this relay a backend behind this
adapter runs with none of Crew's own tools.

Stdlib only, loopback only. One relay per stdio server, owned by the
session that mounted it: :func:`start` returns a handle, :meth:`Relay.stop`
reaps the child. The relay speaks MCP Streamable-HTTP minimally --
single POST /mcp per JSON-RPC message, notifications get an empty 202 --
which is what the measured adapter accepts.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class _RelayServer(HTTPServer):
    """An HTTP server bound to the stdio child it relays for."""

    child: subprocess.Popen[str] | None = None


class _Handler(BaseHTTPRequestHandler):
    server: _RelayServer

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(size) or b"null")
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        child = self.server.child
        child_in = child.stdin if child is not None else None
        child_out = child.stdout if child is not None else None
        if msg is None or "id" not in msg:
            try:
                if child_in is None:
                    raise BrokenPipeError
                child_in.write(json.dumps(msg) + "\n")
                child_in.flush()
            except BrokenPipeError:
                pass
            self.send_response(202)
            self.end_headers()
            return
        try:
            if child_in is None or child_out is None:
                raise BrokenPipeError
            child_in.write(json.dumps(msg) + "\n")
            child_in.flush()
            line = child_out.readline()
            if line:
                body = line.encode()
            else:
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32603, "message": "relay: child closed"},
                    }
                ).encode()
        except BrokenPipeError:
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32603, "message": "relay: broken pipe"},
                }
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Relay:
    """One relayed stdio MCP server."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        import os

        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        self.child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=child_env,
        )
        server = _RelayServer(("127.0.0.1", 0), _Handler)
        server.child = self.child
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def element(self, name: str) -> dict[str, Any]:
        """The ACP ``mcpServers`` element for this relayed server."""
        return {"name": name, "type": "http", "url": self.url, "headers": []}

    def stop(self) -> None:
        try:
            self._server.shutdown()
        finally:
            try:
                self.child.terminate()
            except Exception:
                pass


def start(command: list[str], env: dict[str, str] | None = None) -> Relay:
    """Spawn a relay for one stdio MCP server command."""
    return Relay(command, env)
