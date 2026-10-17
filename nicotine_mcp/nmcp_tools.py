"""MCP tools for searching and downloading music with Nicotine+.

Nicotine+ itself is reached through a bridge object (see NicotineBridge in
__init__.py) so the logic here can be tested without the application:

  bridge.status() -> dict
  bridge.start_search(query) -> token    (must also call store.create_search)
  bridge.enqueue_folder(username, folder_path)
  bridge.enqueue_files(username, folder_path, files)    files: [(virtual_path, size, attributes)]
  bridge.list_downloads() -> [dict]
  bridge.remove_searches(tokens)
"""

import threading
import time

from collections import Counter
from collections import OrderedDict

from nmcp_server import Tool
from nmcp_server import ToolError

AUDIO_EXTENSIONS = {
    "aac", "aif", "aiff", "alac", "ape", "dff", "dsf", "flac", "m4a", "mp3", "mpc", "ogg", "opus", "wav", "wma", "wv"
}
LOSSLESS_EXTENSIONS = {"aif", "aiff", "alac", "ape", "dff", "dsf", "flac", "wav", "wv"}
MAX_TRACKED_SEARCHES = 30
MIN_SEARCH_INTERVAL = 3.0

# File attribute codes used by the Soulseek protocol
ATTR_BITRATE = 0
ATTR_DURATION = 1
ATTR_SAMPLE_RATE = 4
ATTR_BIT_DEPTH = 5

STATUS_GROUPS = {
    "Transferring": "active",
    "Getting status": "active",
    "Queued": "queued",
    "Finished": "finished",
    "Paused": "paused",
    "Cancelled": "paused",
    "Filtered": "failed",
    "User logged off": "failed",
    "Connection closed": "failed",
    "Connection timeout": "failed",
    "Download folder error": "failed",
    "Local file error": "failed"
}


def _extension(path):
    basename = path.rpartition("\\")[2]
    return basename.rpartition(".")[2].lower() if "." in basename else ""


class FolderResult:
    __slots__ = ("id", "username", "folder_path", "files", "free_slot", "speed", "queue", "private")

    def __init__(self, result_id, username, folder_path, free_slot, speed, queue, private):
        self.id = result_id
        self.username = username
        self.folder_path = folder_path
        self.files = OrderedDict()  # virtual_path -> (size, attributes)
        self.free_slot = free_slot
        self.speed = speed
        self.queue = queue
        self.private = private

    def audio_files(self):
        return [(path, size, attrs) for path, (size, attrs) in self.files.items() if _extension(path) in AUDIO_EXTENSIONS]

    def summary(self):

        audio_files = self.audio_files()
        formats = Counter(_extension(path) for path, _size, _attrs in audio_files)
        bitrates = [attrs.get(ATTR_BITRATE) for _path, _size, attrs in audio_files if attrs.get(ATTR_BITRATE)]
        sample_rates = {attrs.get(ATTR_SAMPLE_RATE) for _path, _size, attrs in audio_files} - {None}
        bit_depths = {attrs.get(ATTR_BIT_DEPTH) for _path, _size, attrs in audio_files} - {None}
        duration = sum(attrs.get(ATTR_DURATION) or 0 for _path, _size, attrs in audio_files)
        size = sum(size for size, _attrs in self.files.values())

        summary = {
            "result_id": self.id,
            "user": self.username,
            "folder": self.folder_path,
            "audio_files": len(audio_files),
            "other_files": len(self.files) - len(audio_files),
            "formats": dict(formats.most_common()),
            "size_mb": round(size / 1048576, 1),
            "free_slot": self.free_slot,
            "speed_kbps": round(self.speed / 1024),
            "queue_length": self.queue
        }

        if bitrates:
            summary["avg_bitrate_kbps"] = round(sum(bitrates) / len(bitrates))

        if sample_rates:
            summary["sample_rate_hz"] = sorted(sample_rates)

        if bit_depths:
            summary["bit_depth"] = sorted(bit_depths)

        if duration:
            summary["duration_min"] = round(duration / 60, 1)

        if self.private:
            summary["private"] = True

        summary["tracks"] = [path.rpartition("\\")[2] for path, _size, _attrs in audio_files[:30]]
        return summary


class SearchSession:
    __slots__ = ("token", "query", "started", "folders", "users")

    def __init__(self, token, query, started):
        self.token = token
        self.query = query
        self.started = started
        self.folders = OrderedDict()  # (username, folder_path) -> FolderResult
        self.users = set()


class SearchStore:
    """Collects search results for searches started through MCP. Thread-safe."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = OrderedDict()

    def create_search(self, token, query):

        with self._lock:
            self._sessions[token] = SearchSession(token, query, self._clock())

            while len(self._sessions) > MAX_TRACKED_SEARCHES:
                self._sessions.popitem(last=False)

    def is_tracked(self, token):
        return token in self._sessions

    def tokens(self):
        with self._lock:
            return list(self._sessions)

    def clear(self):
        with self._lock:
            tokens = list(self._sessions)
            self._sessions.clear()
            return tokens

    def add_response(self, token, username, files, free_slot, speed, queue, private=False):

        with self._lock:
            session = self._sessions.get(token)

            if session is None:
                return

            session.users.add(username)

            for _code, virtual_path, size, _ext, attributes, *_unused in files:
                folder_path = virtual_path.rpartition("\\")[0]
                key = (username, folder_path)
                folder = session.folders.get(key)

                if folder is None:
                    folder = session.folders[key] = FolderResult(
                        len(session.folders) + 1, username, folder_path, bool(free_slot), speed or 0, queue or 0,
                        private
                    )

                folder.files[virtual_path] = (size, attributes or {})

    def get(self, token):

        with self._lock:
            session = self._sessions.get(token)

            if session is None:
                raise ToolError(f"Unknown search_id {token}. Start a new search with search_music.")

            # Snapshot so callers can work without holding the lock
            return session.query, self._clock() - session.started, len(session.users), list(session.folders.values())

    def find_folder(self, token, result_id):

        with self._lock:
            session = self._sessions.get(token)

            if session is None:
                raise ToolError(f"Unknown search_id {token}")

            folders = list(session.folders.values())

            if not 1 <= result_id <= len(folders):
                raise ToolError(f"Unknown result_id {result_id} for search {token}")

            folder = folders[result_id - 1]
            return folder.username, folder.folder_path, folder.audio_files() or [
                (path, size, attrs) for path, (size, attrs) in folder.files.items()
            ]


def _format_matches(summary, folder, wanted_format):

    if not wanted_format:
        return True

    formats = set(summary["formats"])

    if wanted_format == "lossless":
        return bool(formats & LOSSLESS_EXTENSIONS)

    if wanted_format == "lossy":
        return bool(formats - LOSSLESS_EXTENSIONS)

    return wanted_format in formats or any(_extension(path) == wanted_format for path in folder.files)


def _rank_key(summary, prefer):

    formats = set(summary["formats"])

    if prefer == "lossless":
        format_score = 1 if formats & LOSSLESS_EXTENSIONS else 0
    elif prefer == "lossy":
        format_score = summary.get("avg_bitrate_kbps", 0) if formats - LOSSLESS_EXTENSIONS else 0
    else:
        format_score = 0

    return (
        summary["free_slot"],
        format_score,
        min(summary["audio_files"], 40),
        summary["speed_kbps"],
        -summary["queue_length"]
    )


class NicotineTools:

    def __init__(self, bridge, store, musicbrainz, default_wait=10, sleep=time.sleep, clock=time.monotonic):
        self.bridge = bridge
        self.store = store
        self.musicbrainz = musicbrainz
        self.default_wait = default_wait
        self._sleep = sleep
        self._clock = clock
        self._search_lock = threading.Lock()
        self._last_search_time = None

    # Nicotine+ #

    def nicotine_status(self):
        return self.bridge.status()

    def search_music(self, query, wait_seconds=None, format=None, prefer="lossless", min_bitrate=None,
                     free_slot_only=False, limit=10):  # pylint: disable=redefined-builtin

        query = " ".join(str(query).split())

        if len(query) < 3:
            raise ToolError("Search query is too short")

        status = self.bridge.status()

        if not status.get("connected"):
            raise ToolError("Nicotine+ is not connected to the Soulseek server")

        wait_seconds = self.default_wait if wait_seconds is None else wait_seconds
        wait_seconds = max(2, min(int(wait_seconds), 30))

        # Space out searches, the Soulseek server dislikes search floods
        with self._search_lock:
            now = self._clock()

            if self._last_search_time is not None:
                delay = self._last_search_time + MIN_SEARCH_INTERVAL - now

                if delay > 0:
                    self._sleep(delay)

            token = self.bridge.start_search(query)
            self._last_search_time = self._clock()

        self._sleep(wait_seconds)

        result = self.get_search_results(
            token, format=format, prefer=prefer, min_bitrate=min_bitrate, free_slot_only=free_slot_only, limit=limit
        )
        result["note"] = ("Results keep arriving in the background. Call get_search_results with this search_id "
                          "to see more, filter, or page through them.")
        return result

    def get_search_results(self, search_id, format=None, prefer="lossless", min_bitrate=None,
                           free_slot_only=False, contains=None, user=None, limit=10,
                           offset=0):  # pylint: disable=redefined-builtin

        query, elapsed, num_users, folders = self.store.get(int(search_id))
        wanted_format = (format or "").lower().lstrip(".") or None
        words = (contains or "").lower().split()
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        matches = []

        for folder in folders:
            summary = folder.summary()

            if not summary["audio_files"]:
                continue

            if free_slot_only and not folder.free_slot:
                continue

            if user and folder.username.lower() != user.lower():
                continue

            if not _format_matches(summary, folder, wanted_format):
                continue

            if min_bitrate and summary.get("avg_bitrate_kbps", 0) < int(min_bitrate) and not (
                    set(summary["formats"]) & LOSSLESS_EXTENSIONS):
                continue

            if words:
                haystack = (folder.folder_path + " " + " ".join(summary["tracks"])).lower()

                if not all(word in haystack for word in words):
                    continue

            matches.append(summary)

        matches.sort(key=lambda summary: _rank_key(summary, prefer), reverse=True)

        return {
            "search_id": int(search_id),
            "query": query,
            "seconds_since_search": round(elapsed),
            "users_responded": num_users,
            "matching_folders": len(matches),
            "offset": offset,
            "results": matches[offset:offset + limit]
        }

    def download(self, search_id, result_ids, mode="folder"):

        if isinstance(result_ids, int):
            result_ids = [result_ids]

        if not result_ids:
            raise ToolError("No result_ids given")

        if mode not in {"folder", "matched_files"}:
            raise ToolError("mode must be 'folder' or 'matched_files'")

        queued = []

        for result_id in result_ids:
            username, folder_path, files = self.store.find_folder(int(search_id), int(result_id))

            if mode == "folder":
                self.bridge.enqueue_folder(username, folder_path)
                queued.append({"result_id": result_id, "user": username, "folder": folder_path,
                               "queued": "entire folder (file list is requested from the user first)"})
            else:
                self.bridge.enqueue_files(username, folder_path, files)
                queued.append({"result_id": result_id, "user": username, "folder": folder_path,
                               "queued": f"{len(files)} files"})

        return {"queued": queued, "note": "Use list_downloads to follow progress."}

    def list_downloads(self, status="all", contains=None, limit=25):

        if status not in {"all", "active", "queued", "finished", "failed", "paused", "unfinished"}:
            raise ToolError("Unknown status filter")

        words = (contains or "").lower().split()
        limit = max(1, min(int(limit), 100))
        groups = OrderedDict()
        totals = Counter()

        for transfer in self.bridge.list_downloads():
            group = STATUS_GROUPS.get(transfer["status"], "queued")
            totals[group] += 1

            folder_path = transfer["path"].rpartition("\\")[0]
            key = (transfer["user"], folder_path)
            entry = groups.get(key)

            if entry is None:
                entry = groups[key] = {
                    "user": transfer["user"],
                    "folder": folder_path,
                    "files": 0,
                    "status": Counter(),
                    "size_bytes": 0,
                    "done_bytes": 0,
                    "speed_kbps": 0,
                    "queue_position": None,
                    "local_folder": transfer.get("local_folder")
                }

            entry["files"] += 1
            entry["status"][group] += 1
            entry["size_bytes"] += transfer["size"] or 0
            entry["done_bytes"] += (transfer["size"] or 0) if group == "finished" else (transfer["offset"] or 0)
            entry["speed_kbps"] += round((transfer["speed"] or 0) / 1024)

            if transfer.get("queue_position"):
                entry["queue_position"] = transfer["queue_position"]

        folders = []

        for entry in reversed(groups.values()):
            folder_status = entry["status"]

            if status == "unfinished" and set(folder_status) == {"finished"}:
                continue

            if status not in {"all", "unfinished"} and not folder_status.get(status):
                continue

            if words and not all(word in entry["folder"].lower() for word in words):
                continue

            size = entry.pop("size_bytes")
            done = entry.pop("done_bytes")
            entry["status"] = dict(folder_status)
            entry["progress_percent"] = round(100 * done / size, 1) if size else None
            entry["size_mb"] = round(size / 1048576, 1)
            folders.append(entry)

        return {"totals": dict(totals), "folders_shown": min(len(folders), limit), "folders": folders[:limit]}

    def clear_searches(self):
        tokens = self.store.clear()
        self.bridge.remove_searches(tokens)
        return {"removed_searches": len(tokens)}

    # MusicBrainz #

    def artist_discography(self, artist, artist_id=None, include_eps=True, include_singles=False,
                           include_other=False):

        primary_types = ["album"]

        if include_eps:
            primary_types.append("ep")

        if include_singles:
            primary_types.append("single")

        if artist_id:
            selected = self.musicbrainz.get_artist(artist_id)
            alternatives = []
        else:
            candidates = self.musicbrainz.search_artists(artist)

            if not candidates:
                raise ToolError(f"No artist named '{artist}' found on MusicBrainz")

            selected, *others = candidates
            alternatives = [candidate for candidate in others if (candidate.get("score") or 0) >= 80]

        releases = self.musicbrainz.get_release_groups(
            selected["id"], primary_types=primary_types, include_secondary=include_other
        )
        result = {"artist": selected, "release_count": len(releases), "releases": releases}

        if alternatives:
            result["other_matching_artists"] = alternatives
            result["note"] = "If this is the wrong artist, call again with artist_id of the right one."

        return result

    def album_tracklist(self, release_group_id):
        return self.musicbrainz.get_tracklist(release_group_id)

    # Definitions #

    def definitions(self):

        filter_properties = {
            "format": {"type": "string",
                       "description": "Only folders with this file format, e.g. 'flac', 'mp3', or 'lossless'/'lossy'"},
            "prefer": {"type": "string", "enum": ["lossless", "lossy", "any"], "default": "lossless",
                       "description": "Ranking preference. 'lossy' favors higher bitrate lossy files"},
            "min_bitrate": {"type": "integer", "description": "Minimum average bitrate in kbps for lossy files"},
            "free_slot_only": {"type": "boolean", "default": False,
                               "description": "Only users with a free upload slot (downloads start immediately)"},
            "limit": {"type": "integer", "default": 10, "description": "Max folders to return (1-50)"}
        }

        return [
            Tool(
                "nicotine_status",
                "Show whether Nicotine+ is connected to Soulseek, the logged-in username and download folder.",
                {"type": "object", "properties": {}},
                self.nicotine_status
            ),
            Tool(
                "search_music",
                "Search the Soulseek network for music and return the best matching folders (usually albums), "
                "ranked by free upload slot, format preference, completeness and speed. Soulseek matches every "
                "word against file paths, so use a few plain words without punctuation, e.g. "
                "'radiohead ok computer' or 'boards of canada geogaddi flac'. Prefix a word with - to exclude "
                "it. Searches take several seconds; avoid firing many in quick succession.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search words"},
                        "wait_seconds": {"type": "integer",
                                         "description": "Seconds to collect results before returning (2-30)"},
                        **filter_properties
                    },
                    "required": ["query"]
                },
                self.search_music
            ),
            Tool(
                "get_search_results",
                "Get (more) results for an earlier search_music call, with filtering and paging. Results keep "
                "arriving for a while after the search starts.",
                {
                    "type": "object",
                    "properties": {
                        "search_id": {"type": "integer"},
                        "contains": {"type": "string",
                                     "description": "Only folders whose path or track names contain all these words"},
                        "user": {"type": "string", "description": "Only results from this user"},
                        "offset": {"type": "integer", "default": 0},
                        **filter_properties
                    },
                    "required": ["search_id"]
                },
                self.get_search_results
            ),
            Tool(
                "download",
                "Queue downloads for search results. mode 'folder' downloads the entire remote folder (best for "
                "albums, includes cover art), 'matched_files' only the files that appeared in the search results.",
                {
                    "type": "object",
                    "properties": {
                        "search_id": {"type": "integer"},
                        "result_ids": {"type": "array", "items": {"type": "integer"},
                                       "description": "result_id values from the search results"},
                        "mode": {"type": "string", "enum": ["folder", "matched_files"], "default": "folder"}
                    },
                    "required": ["search_id", "result_ids"]
                },
                self.download
            ),
            Tool(
                "list_downloads",
                "List downloads in Nicotine+, grouped by remote folder, with status counts and progress.",
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "default": "all",
                                   "enum": ["all", "unfinished", "active", "queued", "finished", "failed", "paused"]},
                        "contains": {"type": "string", "description": "Only folders whose path contains these words"},
                        "limit": {"type": "integer", "default": 25}
                    }
                },
                self.list_downloads
            ),
            Tool(
                "clear_searches",
                "Close all search tabs that were opened through this MCP server.",
                {"type": "object", "properties": {}},
                self.clear_searches
            ),
            Tool(
                "artist_discography",
                "Look up an artist on MusicBrainz and list their studio albums (and EPs), oldest first. "
                "Compilations, live albums, remixes and soundtracks are excluded unless include_other is true.",
                {
                    "type": "object",
                    "properties": {
                        "artist": {"type": "string", "description": "Artist name"},
                        "artist_id": {"type": "string",
                                      "description": "MusicBrainz artist ID, to pick a specific artist"},
                        "include_eps": {"type": "boolean", "default": True},
                        "include_singles": {"type": "boolean", "default": False},
                        "include_other": {"type": "boolean", "default": False,
                                          "description": "Include compilations, live albums, remixes etc."}
                    },
                    "required": ["artist"]
                },
                self.artist_discography
            ),
            Tool(
                "album_tracklist",
                "Get the track list of an album from MusicBrainz (use the release id from artist_discography). "
                "Useful to check whether a search result folder is complete.",
                {
                    "type": "object",
                    "properties": {"release_group_id": {"type": "string"}},
                    "required": ["release_group_id"]
                },
                self.album_tracklist
            )
        ]
