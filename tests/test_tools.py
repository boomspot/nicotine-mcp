import io
import json
import unittest

from nmcp_musicbrainz import MusicBrainz
from nmcp_server import ToolError
from nmcp_tools import NicotineTools
from nmcp_tools import SearchStore

FLAC = {1: 240, 4: 44100, 5: 16}


def mp3(bitrate):
    return {0: bitrate, 1: 240, 2: 0}


def files(folder, extension, attributes, count=10, size=30000000):
    return [
        (1, f"{folder}\\{number:02d} - Track {number}.{extension}", size, None, attributes)
        for number in range(1, count + 1)
    ]


class FakeBridge:

    def __init__(self, store):
        self.store = store
        self.connected = True
        self.next_token = 100
        self.searches = []
        self.folders = []
        self.file_batches = []
        self.removed = []
        self.downloads = []

    def status(self):
        return {"connected": self.connected, "username": "me"}

    def start_search(self, query):
        self.next_token += 1
        self.searches.append(query)
        self.store.create_search(self.next_token, query)
        return self.next_token

    def enqueue_folder(self, username, folder_path):
        self.folders.append((username, folder_path))

    def enqueue_files(self, username, folder_path, file_list):
        self.file_batches.append((username, folder_path, file_list))

    def list_downloads(self):
        return self.downloads

    def remove_searches(self, tokens):
        self.removed.extend(tokens)


class FakeClock:

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ToolsTest(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.store = SearchStore(clock=self.clock)
        self.bridge = FakeBridge(self.store)
        self.tools = NicotineTools(self.bridge, self.store, musicbrainz=None, default_wait=5,
                                   sleep=self.clock.sleep, clock=self.clock)

    def add_results(self, token):

        # A slow user with FLAC, a fast user with MP3, a user with a free slot and FLAC, and non-audio noise
        self.store.add_response(token, "slowflac", files("Music\\Artist - Album [FLAC]", "flac", FLAC),
                                free_slot=False, speed=50000, queue=10)
        self.store.add_response(token, "fastmp3", files("mp3\\Artist\\Album (2001)", "mp3", mp3(320)),
                                free_slot=True, speed=900000, queue=0)
        self.store.add_response(token, "freeflac", files("share\\Artist\\Album", "flac", FLAC, count=12),
                                free_slot=True, speed=200000, queue=0)
        self.store.add_response(token, "lowmp3", files("x\\Artist - Album", "mp3", mp3(128)),
                                free_slot=True, speed=100000, queue=0)
        self.store.add_response(token, "docs", [(1, "docs\\Artist - Album.pdf", 1000, None, {})],
                                free_slot=True, speed=100000, queue=0)

    def test_search_waits_and_ranks(self):

        self.bridge.start_search = self._start_search_with_results
        result = self.tools.search_music("  artist   album ")

        self.assertEqual(self.bridge.searches, ["artist album"])
        self.assertEqual(self.clock.sleeps, [5])
        self.assertEqual(result["users_responded"], 5)
        users = [item["user"] for item in result["results"]]

        # Free slot first, then lossless; non-audio folders are skipped
        self.assertEqual(users, ["freeflac", "fastmp3", "lowmp3", "slowflac"])
        self.assertEqual(result["results"][0]["formats"], {"flac": 12})
        self.assertEqual(result["results"][0]["sample_rate_hz"], [44100])
        self.assertEqual(result["results"][1]["avg_bitrate_kbps"], 320)

    def _start_search_with_results(self, query):
        token = FakeBridge.start_search(self.bridge, query)
        self.add_results(token)
        return token

    def test_filters(self):

        token = self.bridge.start_search("artist album")
        self.add_results(token)

        def users(**kwargs):
            return [item["user"] for item in self.tools.get_search_results(token, **kwargs)["results"]]

        self.assertEqual(users(format="mp3", prefer="lossy"), ["fastmp3", "lowmp3"])
        self.assertEqual(users(format="lossless"), ["freeflac", "slowflac"])
        self.assertEqual(users(min_bitrate=256, prefer="lossy"), ["fastmp3", "freeflac", "slowflac"])
        self.assertEqual(users(free_slot_only=True, format="flac"), ["freeflac"])
        self.assertEqual(users(contains="2001"), ["fastmp3"])
        self.assertEqual(users(user="SLOWFLAC"), ["slowflac"])
        self.assertEqual(len(users(limit=2)), 2)
        self.assertEqual(users(offset=3), ["slowflac"])

    def test_search_spacing_and_validation(self):

        self.tools.search_music("first search", wait_seconds=2)
        self.tools.search_music("second search", wait_seconds=2)

        # Second search waits so searches are at least 3 seconds apart
        self.assertEqual(self.clock.sleeps, [2, 1.0, 2])

        with self.assertRaises(ToolError):
            self.tools.search_music("ab")

        self.bridge.connected = False

        with self.assertRaises(ToolError):
            self.tools.search_music("valid query")

    def test_download_modes(self):

        token = self.bridge.start_search("artist album")
        self.add_results(token)
        ids = {item["user"]: item["result_id"] for item in self.tools.get_search_results(token)["results"]}

        self.tools.download(token, [ids["freeflac"]])
        self.assertEqual(self.bridge.folders, [("freeflac", "share\\Artist\\Album")])

        self.tools.download(token, ids["fastmp3"], mode="matched_files")
        username, folder_path, file_list = self.bridge.file_batches[0]
        self.assertEqual((username, folder_path, len(file_list)), ("fastmp3", "mp3\\Artist\\Album (2001)", 10))
        self.assertEqual(file_list[0][0], "mp3\\Artist\\Album (2001)\\01 - Track 1.mp3")

        with self.assertRaises(ToolError):
            self.tools.download(token, [999])

        with self.assertRaises(ToolError):
            self.tools.download(12345, [1])

    def test_list_downloads_groups_by_folder(self):

        self.bridge.downloads = [
            {"user": "a", "path": "x\\Album\\01.flac", "size": 100, "status": "Finished", "offset": 100,
             "speed": 0, "queue_position": 0},
            {"user": "a", "path": "x\\Album\\02.flac", "size": 100, "status": "Transferring", "offset": 50,
             "speed": 2048, "queue_position": 0},
            {"user": "b", "path": "y\\Other\\01.mp3", "size": 100, "status": "Queued", "offset": 0,
             "speed": 0, "queue_position": 4}
        ]

        result = self.tools.list_downloads()
        self.assertEqual(result["totals"], {"finished": 1, "active": 1, "queued": 1})
        album = next(folder for folder in result["folders"] if folder["folder"] == "x\\Album")
        self.assertEqual(album["files"], 2)
        self.assertEqual(album["progress_percent"], 75.0)
        self.assertEqual(album["speed_kbps"], 2)

        self.assertEqual([folder["user"] for folder in self.tools.list_downloads(status="queued")["folders"]], ["b"])
        self.assertEqual(len(self.tools.list_downloads(contains="other")["folders"]), 1)

    def test_clear_searches(self):

        self.bridge.start_search("one two three")
        self.bridge.start_search("four five six")
        self.assertEqual(self.tools.clear_searches(), {"removed_searches": 2})
        self.assertEqual(self.bridge.removed, [101, 102])
        self.assertEqual(self.store.tokens(), [])


class FakeResponse(io.BytesIO):

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MusicBrainzTest(unittest.TestCase):

    def setUp(self):

        self.requests = []
        self.clock = FakeClock()
        self.responses = {
            "artist": {"artists": [
                {"id": "a1", "name": "Radiohead", "score": 100, "type": "Group", "country": "GB",
                 "life-span": {"begin": "1991", "ended": False}},
                {"id": "a2", "name": "Radiohead Tribute", "score": 85},
                {"id": "a3", "name": "Unrelated", "score": 40}
            ]},
            "release-group": {"release-group-count": 3, "release-groups": [
                {"id": "r2", "title": "OK Computer", "first-release-date": "1997-05-21",
                 "primary-type": "Album", "secondary-types": []},
                {"id": "r1", "title": "Pablo Honey", "first-release-date": "1993-02-22",
                 "primary-type": "Album", "secondary-types": []},
                {"id": "r3", "title": "I Might Be Wrong", "first-release-date": "2001-11-12",
                 "primary-type": "Album", "secondary-types": ["Live"]}
            ]},
            "release": {"releases": [
                {"id": "x2", "title": "OK Computer", "status": "Official", "date": "1997-06-16", "media": []},
                {"id": "x1", "title": "OK Computer", "status": "Official", "date": "1997-05-21", "media": [
                    {"position": 1, "tracks": [
                        {"number": "1", "title": "Airbag", "length": 284000},
                        {"number": "2", "title": "Paranoid Android", "length": 383000}
                    ]}
                ]}
            ]}
        }
        self.musicbrainz = MusicBrainz("test-agent", opener=self.open, sleep=self.clock.sleep, clock=self.clock)
        self.tools = NicotineTools(None, SearchStore(), self.musicbrainz)

    def open(self, request, timeout):

        self.requests.append(request)
        path = request.full_url.split("/ws/2/", 1)[1].split("?", 1)[0]
        return FakeResponse(json.dumps(self.responses[path]).encode())

    def test_discography(self):

        result = self.tools.artist_discography("Radiohead")

        self.assertEqual(result["artist"]["name"], "Radiohead")
        self.assertEqual(result["artist"]["active"], "1991-present")
        self.assertEqual([release["title"] for release in result["releases"]], ["Pablo Honey", "OK Computer"])
        self.assertEqual(result["releases"][0]["year"], 1993)
        self.assertEqual([artist["id"] for artist in result["other_matching_artists"]], ["a2"])

        url = self.requests[1].full_url
        self.assertIn("type=album%7Cep", url)
        self.assertEqual(self.requests[0].get_header("User-agent"), "test-agent")

        # Second request is rate limited
        self.assertEqual(len(self.clock.sleeps), 1)

        result = self.tools.artist_discography("Radiohead", include_other=True)
        self.assertEqual(len(result["releases"]), 3)

        # Cached requests don't hit the network again
        self.assertEqual(len(self.requests), 2)

    def test_tracklist_prefers_earliest_official_release(self):

        result = self.tools.album_tracklist("r2")
        self.assertEqual(result["release_id"], "x1")
        self.assertEqual(result["track_count"], 2)
        self.assertEqual(result["total_length"], "11:07")
        self.assertEqual(result["tracks"][1], {"disc": 1, "number": "2", "title": "Paranoid Android",
                                               "length": "6:23"})


if __name__ == "__main__":
    unittest.main()
