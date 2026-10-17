"""Minimal MCP server using only the Python standard library.

Implements the Streamable HTTP transport with plain JSON responses (no SSE
streams), which is all a tools-only server needs. The packaged Nicotine+ app
bundles a stdlib-only Python, so the official MCP SDK cannot be used.
"""

import inspect
import json
import threading
import traceback

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
MAX_BODY_SIZE = 1024 * 1024


class ToolError(Exception):
    """An error the model should see and can react to (returned as isError)."""


class Tool:
    __slots__ = ("name", "description", "input_schema", "handler")

    def __init__(self, name, description, input_schema, handler):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler

    def to_dict(self):
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


class McpApp:
    """Transport-independent JSON-RPC message handling."""

    def __init__(self, name, version, tools, instructions=None, log=None):
        self.name = name
        self.version = version
        self.tools = {tool.name: tool for tool in tools}
        self.instructions = instructions
        self.log = log or (lambda message: None)

    def handle_payload(self, payload):
        """Returns the JSON-serializable response, or None when nothing should be sent back."""

        if isinstance(payload, list):
            if not payload:
                return self._error(None, INVALID_REQUEST, "Empty batch")

            responses = [response for response in map(self.handle_message, payload) if response is not None]
            return responses or None

        return self.handle_message(payload)

    def handle_message(self, message):

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, INVALID_REQUEST, "Invalid JSON-RPC message")

        method = message.get("method")
        msg_id = message.get("id")

        if method is None:
            # Response to a server-initiated request; we never send any
            return None

        if "id" not in message:
            # Notification (e.g. notifications/initialized), no response
            return None

        params = message.get("params") or {}

        if not isinstance(params, dict):
            return self._error(msg_id, INVALID_PARAMS, "params must be an object")

        if method == "initialize":
            return self._result(msg_id, self._initialize(params))

        if method == "ping":
            return self._result(msg_id, {})

        if method == "tools/list":
            return self._result(msg_id, {"tools": [tool.to_dict() for tool in self.tools.values()]})

        if method == "tools/call":
            return self._call_tool(msg_id, params)

        return self._error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params):

        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version}
        }

        if self.instructions:
            result["instructions"] = self.instructions

        return result

    def _call_tool(self, msg_id, params):

        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = self.tools.get(name)

        if tool is None:
            return self._error(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")

        if not isinstance(arguments, dict):
            return self._error(msg_id, INVALID_PARAMS, "arguments must be an object")

        try:
            inspect.signature(tool.handler).bind(**arguments)

        except TypeError as error:
            # Unexpected or missing arguments
            return self._result(
                msg_id, {"content": [{"type": "text", "text": f"Invalid arguments for {name}: {error}"}],
                         "isError": True})

        try:
            output = tool.handler(**arguments)
            is_error = False

        except ToolError as error:
            output = str(error)
            is_error = True

        except Exception as error:
            self.log(f"Tool {name} failed:\n{traceback.format_exc()}")
            output = f"Internal error in {name}: {error}"
            is_error = True

        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, indent=1)
        result = {"content": [{"type": "text", "text": text}]}

        if is_error:
            result["isError"] = True

        return self._result(msg_id, result)

    @staticmethod
    def _result(msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class _RequestHandler(BaseHTTPRequestHandler):

    server_version = "NicotineMCP"
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # pylint: disable=invalid-name

        if not self._check_request():
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1

        if length < 0 or length > MAX_BODY_SIZE:
            self._send_json(413, McpApp._error(None, INVALID_REQUEST, "Invalid request size"))
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, McpApp._error(None, PARSE_ERROR, "Parse error"))
            return

        response = self.server.app.handle_payload(payload)

        if response is None:
            self._send_empty(202)
            return

        self._send_json(200, response)

    def do_GET(self):  # pylint: disable=invalid-name
        # No server-initiated SSE stream
        if self._check_request():
            self._send_empty(405, {"Allow": "POST"})

    def do_DELETE(self):  # pylint: disable=invalid-name
        if self._check_request():
            self._send_empty(405, {"Allow": "POST"})

    def _check_request(self):

        if urlsplit(self.path).path.rstrip("/") not in {"", "/mcp"}:
            self._send_empty(404)
            return False

        # Protect against DNS rebinding: only accept local Host and Origin values
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")

        if self._hostname(f"//{host}") not in LOCAL_HOSTNAMES:
            self._send_empty(403)
            return False

        if origin is not None and self._hostname(origin) not in LOCAL_HOSTNAMES:
            self._send_empty(403)
            return False

        return True

    @staticmethod
    def _hostname(url):
        try:
            return urlsplit(url).hostname
        except ValueError:
            return None

    def _send_json(self, status, data):

        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status, headers=None):

        self.send_response(status)

        for key, value in (headers or {}).items():
            self.send_header(key, value)

        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        self.server.log_function("HTTP " + (format % args))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class McpHttpServer:
    """Runs an McpApp on a local HTTP port in a background thread."""

    def __init__(self, app, host="127.0.0.1", port=8765, log=None):
        self.app = app
        self.host = host
        self.port = port
        self.log = log or (lambda message: None)
        self._httpd = None
        self._thread = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/mcp"

    def start(self):

        self._httpd = _Server((self.host, self.port), _RequestHandler)
        self._httpd.app = self.app
        self._httpd.log_function = self.log

        # Port 0 picks a free port (used by tests)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.5}, name="NicotineMCPServer", daemon=True
        )
        self._thread.start()

    def stop(self):

        if self._httpd is None:
            return

        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None
