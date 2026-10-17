import json
import unittest

from http.client import HTTPConnection

from nmcp_server import McpApp
from nmcp_server import McpHttpServer
from nmcp_server import Tool
from nmcp_server import ToolError


def echo(text, times=1):
    return {"echo": text * times}


def fail():
    raise ToolError("nope")


def crash():
    raise RuntimeError("boom")


TOOLS = [
    Tool("echo", "Echo text", {"type": "object", "properties": {"text": {"type": "string"}}}, echo),
    Tool("fail", "Fails", {"type": "object", "properties": {}}, fail),
    Tool("crash", "Crashes", {"type": "object", "properties": {}}, crash)
]


class McpAppTest(unittest.TestCase):

    def setUp(self):
        self.app = McpApp("test", "1.0", TOOLS, instructions="hi")

    def request(self, method, params=None, msg_id=1):
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method}

        if params is not None:
            message["params"] = params

        return self.app.handle_payload(message)

    def test_initialize_negotiates_version(self):

        result = self.request("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "test")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["instructions"], "hi")

        result = self.request("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")

    def test_notification_has_no_response(self):
        self.assertIsNone(self.app.handle_payload({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list(self):
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["echo", "fail", "crash"])
        self.assertIn("inputSchema", tools[0])

    def test_tools_call(self):

        result = self.request("tools/call", {"name": "echo", "arguments": {"text": "ab", "times": 2}})["result"]
        self.assertNotIn("isError", result)
        self.assertEqual(json.loads(result["content"][0]["text"]), {"echo": "abab"})

    def test_tool_errors(self):

        result = self.request("tools/call", {"name": "fail"})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "nope")

        result = self.request("tools/call", {"name": "crash"})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("boom", result["content"][0]["text"])

        result = self.request("tools/call", {"name": "echo", "arguments": {"bogus": 1}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Invalid arguments", result["content"][0]["text"])

        error = self.request("tools/call", {"name": "missing"})["error"]
        self.assertEqual(error["code"], -32602)

    def test_unknown_method_and_invalid_message(self):
        self.assertEqual(self.request("resources/list")["error"]["code"], -32601)
        self.assertEqual(self.app.handle_payload({"id": 1})["error"]["code"], -32600)

    def test_batch(self):

        responses = self.app.handle_payload([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        ])
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": 1, "result": {}}])


class McpHttpServerTest(unittest.TestCase):

    def setUp(self):
        self.server = McpHttpServer(McpApp("test", "1.0", TOOLS), port=0)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def post(self, body, headers=None):

        connection = HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        connection.request(
            "POST", "/mcp", body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                     **(headers or {})}
        )
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_round_trip(self):

        status, data = self.post(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_notification_returns_accepted(self):
        status, data = self.post(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual((status, data), (202, b""))

    def test_parse_error(self):
        status, data = self.post("{not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], -32700)

    def test_rejects_foreign_origin_and_host(self):

        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(self.post(body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post(body, {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.post(body, {"Origin": "http://localhost:3000"})[0], 200)

    def test_get_not_allowed(self):

        connection = HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        connection.request("GET", "/mcp")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 405)

    def test_restart_on_same_port(self):

        port = self.server.port
        self.server.stop()
        self.server = McpHttpServer(McpApp("test", "1.0", TOOLS), port=port)
        self.server.start()
        self.assertEqual(self.post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))[0], 200)


if __name__ == "__main__":
    unittest.main()
