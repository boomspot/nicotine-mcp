"""Loads the plugin through a real Nicotine+ core (no GUI, no network).

Set NICOTINE_SOURCE to a folder containing the pynicotine package of the
Nicotine+ version you run, e.g. a `git archive 3.3.10 pynicotine` extract.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from http.client import HTTPConnection

NICOTINE_SOURCE = os.environ.get("NICOTINE_SOURCE")
PLUGIN_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nicotine_mcp")


@unittest.skipUnless(NICOTINE_SOURCE, "NICOTINE_SOURCE not set")
class NicotineCompatTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):

        sys.path.insert(0, NICOTINE_SOURCE)

        # pylint: disable=import-outside-toplevel
        from pynicotine import slskmessages
        from pynicotine.config import config
        from pynicotine.core import core
        from pynicotine.events import events

        cls.slskmessages = slskmessages
        cls.config = config
        cls.core = core
        cls.events = events

        cls.data_folder = tempfile.mkdtemp(prefix="nicotine-mcp-test-")
        os.makedirs(os.path.join(cls.data_folder, "plugins"))
        os.symlink(PLUGIN_PATH, os.path.join(cls.data_folder, "plugins", "nicotine_mcp"))

        config.set_data_folder(cls.data_folder)
        config.set_config_file(os.path.join(cls.data_folder, "config"))

        core.init_components(enabled_components={
            "pluginhandler", "search", "downloads", "users", "shares", "network_filter", "statistics"
        })
        core.start()

    @classmethod
    def tearDownClass(cls):
        cls.core.quit()
        shutil.rmtree(cls.data_folder)

    def setUp(self):

        # Port 0 lets the OS pick a free port
        self.config.sections["plugins"]["nicotine_mcp"] = {"port": 0, "search_wait_seconds": 2,
                                                           "musicbrainz_contact": ""}
        self.core.pluginhandler.enable_plugin("nicotine_mcp")
        self.assertIn("nicotine_mcp", self.loaded_plugins(), "Plugin failed to load, see log output")
        self.plugin = self.loaded_plugins()["nicotine_mcp"]

    def tearDown(self):
        self.core.pluginhandler.disable_plugin("nicotine_mcp")

    def loaded_plugins(self):
        # Named enabled_plugins in Nicotine+ 3.3.x, loaded_plugins in later versions
        handler = self.core.pluginhandler
        return getattr(handler, "loaded_plugins", None) or handler.enabled_plugins

    def call_tool(self, name, arguments=None):
        """Calls a tool over HTTP while pumping Nicotine+ main thread events."""

        outcome = {}

        def request():
            connection = HTTPConnection("127.0.0.1", self.plugin.server.port, timeout=20)
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}}}
            connection.request("POST", "/mcp", body=json.dumps(body), headers={"Content-Type": "application/json"})
            outcome["response"] = json.loads(connection.getresponse().read())
            connection.close()

        thread = threading.Thread(target=request)
        thread.start()

        while thread.is_alive():
            self.events.process_thread_events()
            thread.join(0.05)

        result = outcome["response"]["result"]
        text = result["content"][0]["text"]
        return result.get("isError", False), (text if result.get("isError") else json.loads(text))

    def test_status_over_http(self):

        is_error, status = self.call_tool("nicotine_status")
        self.assertFalse(is_error)
        self.assertFalse(status["connected"])
        self.assertTrue(status["download_folder"])

        is_error, message = self.call_tool("search_music", {"query": "some artist"})
        self.assertTrue(is_error)
        self.assertIn("not connected", message)

    def test_search_results_and_downloads(self):

        bridge = self.plugin.tools.bridge
        token = bridge.start_search("artist album flac")
        self.assertIn(token, self.core.search.searches)

        msg = self.slskmessages.FileSearchResponse(
            search_username="peer", token=token, freeulslots=True, ulspeed=500000, inqueue=0,
            shares=[(1, "music\\Artist - Album\\01 - Intro.flac", 20000000, None, {1: 200, 4: 44100, 5: 16}),
                    (1, "music\\Artist - Album\\02 - Song.flac", 30000000, None, {1: 250, 4: 44100, 5: 16})]
        )
        msg.username = "peer"
        msg.addr = ("10.0.0.1", 2234)
        self.events.emit("file-search-response", msg)

        results = self.plugin.tools.get_search_results(token)
        self.assertEqual(results["users_responded"], 1)
        folder = results["results"][0]
        self.assertEqual(folder["folder"], "music\\Artist - Album")
        self.assertEqual(folder["formats"], {"flac": 2})

        is_error, queued = self.call_tool(
            "download", {"search_id": token, "result_ids": [folder["result_id"]], "mode": "matched_files"})
        self.assertFalse(is_error, queued)
        self.assertIn("peer" + "music\\Artist - Album\\01 - Intro.flac", self.core.downloads.transfers)

        is_error, downloads = self.call_tool("list_downloads")
        self.assertFalse(is_error, downloads)
        self.assertEqual(downloads["folders"][0]["files"], 2)

        is_error, queued = self.call_tool("download", {"search_id": token, "result_ids": [folder["result_id"]]})

        if hasattr(self.core.downloads, "enqueue_folder"):
            self.assertFalse(is_error, queued)
        else:
            self.assertIn("matched_files", queued)

        is_error, cleared = self.call_tool("clear_searches")
        self.assertFalse(is_error, cleared)
        self.assertNotIn(token, self.core.search.searches)

    def test_reload_frees_port_and_callbacks(self):

        port = self.plugin.server.port
        self.core.pluginhandler.disable_plugin("nicotine_mcp")

        callbacks = self.events._callbacks["file-search-response"]  # pylint: disable=protected-access
        self.assertFalse([callback for callback in callbacks if "nicotine_mcp" in str(callback.__module__)])

        self.config.sections["plugins"]["nicotine_mcp"]["port"] = port
        self.core.pluginhandler.enable_plugin("nicotine_mcp")
        self.assertIn("nicotine_mcp", self.loaded_plugins(), "Plugin failed to load, see log output")
        self.plugin = self.loaded_plugins()["nicotine_mcp"]
        self.assertEqual(self.plugin.server.port, port)


if __name__ == "__main__":
    unittest.main()
