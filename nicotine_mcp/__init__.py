"""Nicotine+ MCP plugin: lets an AI assistant search Soulseek, look up artists
and queue downloads through a local MCP server.

Nicotine+ adds this folder to sys.path, so helper modules are imported as
top-level modules. They use an nmcp_ prefix to avoid name clashes.
"""

import threading

from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.pluginsystem import BasePlugin
from pynicotine.slskmessages import UserStatus

from nmcp_musicbrainz import MusicBrainz
from nmcp_server import McpApp
from nmcp_server import McpHttpServer
from nmcp_server import ToolError
from nmcp_tools import NicotineTools
from nmcp_tools import SearchStore

VERSION = "0.1.0"
PROJECT_URL = "https://github.com/boomspot/nicotine-mcp"
MAIN_THREAD_TIMEOUT = 15

INSTRUCTIONS = """Tools for the Nicotine+ Soulseek client running on this computer.
Typical flow: artist_discography to see an artist's albums, search_music with 'artist album' words
to find copies on Soulseek, compare results (prefer complete folders from users with a free slot),
then download with mode 'folder'. Check progress with list_downloads.
Only download what the user asked for, and confirm before queueing many albums at once."""


class NicotineBridge:
    """Runs operations on the Nicotine+ main thread, where all core state lives."""

    def __init__(self, core, store):
        self.core = core
        self.store = store

    @staticmethod
    def _run(function, *args):

        if threading.current_thread() is threading.main_thread():
            return function(*args)

        done = threading.Event()
        outcome = {}

        def wrapper():
            try:
                outcome["result"] = function(*args)
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        events.invoke_main_thread(wrapper)

        if not done.wait(MAIN_THREAD_TIMEOUT):
            raise ToolError("Nicotine+ did not respond in time")

        if "error" in outcome:
            raise outcome["error"]

        return outcome.get("result")

    def status(self):
        return self._run(self._status)

    def _status(self):

        users = self.core.users
        downloads = self.core.downloads

        return {
            "connected": users.login_status != UserStatus.OFFLINE,
            "username": users.login_username,
            "away": users.login_status == UserStatus.AWAY,
            "download_folder": downloads.get_default_download_folder(),
            "downloads_in_list": len(downloads.transfers)
        }

    def start_search(self, query):
        return self._run(self._start_search, query)

    def _start_search(self, query):

        search = self.core.search
        search.do_search(query, "global", switch_page=False)

        # do_search() doesn't return the token. The attribute is "token" in 3.3.x
        # and "_token" in later versions.
        token = search.token if hasattr(search, "token") else search._token  # pylint: disable=protected-access
        self.store.create_search(token, query)
        return token

    def enqueue_folder(self, username, folder_path):

        # Nicotine+ 3.4 replaced enqueue_folder() with a GUI-driven request_folder() flow
        if not hasattr(self.core.downloads, "enqueue_folder"):
            raise ToolError("Folder downloads aren't supported with this Nicotine+ version yet, "
                            "use mode 'matched_files'")

        self._run(self.core.downloads.enqueue_folder, username, folder_path)

    def enqueue_files(self, username, folder_path, files):
        self._run(self._enqueue_files, username, folder_path, files)

    def _enqueue_files(self, username, folder_path, files):

        downloads = self.core.downloads
        destination = downloads.get_folder_destination(username, folder_path)

        for virtual_path, size, attributes in files:
            downloads.enqueue_download(
                username, virtual_path, folder_path=destination, size=size, file_attributes=attributes
            )

    def list_downloads(self):
        return self._run(self._list_downloads)

    def _list_downloads(self):
        return [
            {
                "user": transfer.username,
                "path": transfer.virtual_path,
                "size": transfer.size,
                "status": transfer.status,
                "offset": transfer.current_byte_offset,
                "speed": transfer.speed,
                "queue_position": transfer.queue_position,
                "local_folder": transfer.folder_path
            }
            for transfer in self.core.downloads.transfers.values()
        ]

    def remove_searches(self, tokens):
        self._run(self._remove_searches, tokens)

    def _remove_searches(self, tokens):
        for token in tokens:
            if token in self.core.search.searches:
                self.core.search.remove_search(token)


class Plugin(BasePlugin):

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self.settings = {
            "port": 8765,
            "search_wait_seconds": 10,
            "musicbrainz_contact": ""
        }
        self.metasettings = {
            "port": {
                "description": "Local port for the MCP server (http://127.0.0.1:<port>/mcp):",
                "type": "int", "minimum": 1024, "maximum": 65535
            },
            "search_wait_seconds": {
                "description": "Default seconds to collect search results before answering:",
                "type": "int", "minimum": 2, "maximum": 30
            },
            "musicbrainz_contact": {
                "description": "Contact (email or URL) sent to MusicBrainz in the User-Agent, as they request:",
                "type": "string"
            }
        }

        self.store = SearchStore()
        self.tools = None
        self.server = None

    def init(self):

        contact = self.settings["musicbrainz_contact"].strip()
        user_agent = f"nicotine-mcp/{VERSION} ( {contact or PROJECT_URL} )"

        self.tools = NicotineTools(
            NicotineBridge(self.core, self.store), self.store, MusicBrainz(user_agent),
            default_wait=self.settings["search_wait_seconds"]
        )
        app = McpApp("nicotine-plus", VERSION, self.tools.definitions(), instructions=INSTRUCTIONS, log=self.log)
        self.server = McpHttpServer(app, port=self.settings["port"], log=self._log_debug)

        events.connect("file-search-response", self._file_search_response)

        try:
            self.server.start()

        except OSError as error:
            self.server = None
            self.log("Could not start MCP server on port %s: %s", (self.settings["port"], error))
            return

        self.log("MCP server listening on %s", (self.server.url,))

    def disable(self):
        events.disconnect("file-search-response", self._file_search_response)
        self._stop_server()

    def shutdown_notification(self):
        self._stop_server()

    def _stop_server(self):

        if self.server is not None:
            self.server.stop()
            self.server = None

    @staticmethod
    def _log_debug(message):
        # HTTP access logs are noisy, only show them in the debug log
        log.add_debug("Nicotine MCP: %s", (message,))

    def _file_search_response(self, msg):

        # Nicotine+'s own handler runs first and clears the token for rejected responses
        if msg.token is None or not self.store.is_tracked(msg.token):
            return

        self.store.add_response(
            msg.token, msg.username, msg.list or [], msg.freeulslots, msg.ulspeed, msg.inqueue
        )

        if msg.privatelist:
            self.store.add_response(
                msg.token, msg.username, msg.privatelist, msg.freeulslots, msg.ulspeed, msg.inqueue, private=True
            )
