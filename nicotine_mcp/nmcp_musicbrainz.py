"""Small MusicBrainz web service client (https://musicbrainz.org/doc/MusicBrainz_API).

MusicBrainz allows about one request per second per client and asks for a
descriptive User-Agent, both of which are enforced here.
"""

import json
import threading
import time

from collections import OrderedDict
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

from nmcp_server import ToolError

API_URL = "https://musicbrainz.org/ws/2/"
MIN_REQUEST_INTERVAL = 1.1
CACHE_SIZE = 256

# Secondary types that usually aren't part of an artist's core discography
EXCLUDED_SECONDARY_TYPES = {
    "Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo", "Interview",
    "Spokenword", "Audiobook", "Audio drama", "Soundtrack", "Field recording"
}


class MusicBrainz:

    def __init__(self, user_agent, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.user_agent = user_agent
        self._opener = opener or urlopen
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_request_time = 0.0
        self._cache = OrderedDict()

    def get(self, path, **params):

        params["fmt"] = "json"
        url = API_URL + path + "?" + urlencode(params)

        with self._lock:
            if url in self._cache:
                self._cache.move_to_end(url)
                return self._cache[url]

            wait = self._last_request_time + MIN_REQUEST_INTERVAL - self._clock()

            if wait > 0:
                self._sleep(wait)

            self._last_request_time = self._clock()

            try:
                request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})

                with self._opener(request, timeout=15) as response:
                    data = json.loads(response.read())

            except HTTPError as error:
                if error.code == 404:
                    raise ToolError("MusicBrainz: not found") from error

                if error.code == 503:
                    raise ToolError("MusicBrainz is rate limiting requests, try again in a few seconds") from error

                raise ToolError(f"MusicBrainz request failed: HTTP {error.code}") from error

            except (URLError, OSError, ValueError) as error:
                raise ToolError(f"MusicBrainz request failed: {error}") from error

            self._cache[url] = data

            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)

            return data

    def search_artists(self, name, limit=5):

        escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
        data = self.get("artist", query=f'artist:"{escaped_name}"', limit=limit)

        return [
            {
                "id": artist["id"],
                "name": artist.get("name"),
                "disambiguation": artist.get("disambiguation") or None,
                "type": artist.get("type"),
                "country": artist.get("country"),
                "active": _life_span(artist.get("life-span")),
                "score": artist.get("score")
            }
            for artist in data.get("artists", [])
        ]

    def get_artist(self, artist_id):

        artist = self.get(f"artist/{artist_id}")

        return {
            "id": artist["id"],
            "name": artist.get("name"),
            "disambiguation": artist.get("disambiguation") or None,
            "type": artist.get("type"),
            "country": artist.get("country"),
            "active": _life_span(artist.get("life-span"))
        }

    def get_release_groups(self, artist_id, primary_types=("album", "ep"), include_secondary=False):

        release_groups = []
        offset = 0

        while True:
            data = self.get(
                "release-group", artist=artist_id, type="|".join(primary_types), limit=100, offset=offset
            )
            items = data.get("release-groups", [])
            release_groups.extend(items)
            offset += len(items)

            if not items or offset >= data.get("release-group-count", 0) or offset >= 500:
                break

        results = []

        for group in release_groups:
            secondary_types = group.get("secondary-types") or []

            if not include_secondary and EXCLUDED_SECONDARY_TYPES.intersection(secondary_types):
                continue

            date = group.get("first-release-date") or ""
            results.append({
                "id": group["id"],
                "title": group.get("title"),
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "type": group.get("primary-type"),
                "secondary_types": secondary_types or None
            })

        results.sort(key=lambda item: (item["year"] or 9999, item["title"] or ""))
        return results

    def get_tracklist(self, release_group_id):

        data = self.get("release", **{"release-group": release_group_id, "inc": "recordings media", "limit": 25})
        releases = data.get("releases", [])

        if not releases:
            raise ToolError("No releases found for this release group")

        # Prefer the earliest official release, which is usually the original edition
        def sort_key(release):
            return (release.get("status") != "Official", release.get("date") or "9999")

        release = min(releases, key=sort_key)
        tracks = []
        total_ms = 0

        for medium in release.get("media", []):
            for track in medium.get("tracks", []):
                length = track.get("length") or (track.get("recording") or {}).get("length")
                total_ms += length or 0
                tracks.append({
                    "disc": medium.get("position"),
                    "number": track.get("number"),
                    "title": track.get("title"),
                    "length": _format_ms(length)
                })

        return {
            "release_id": release["id"],
            "title": release.get("title"),
            "date": release.get("date"),
            "country": release.get("country"),
            "editions_found": len(releases),
            "track_count": len(tracks),
            "total_length": _format_ms(total_ms),
            "tracks": tracks
        }


def _life_span(life_span):

    if not life_span:
        return None

    begin = (life_span.get("begin") or "")[:4]
    end = (life_span.get("end") or "")[:4]

    if not begin and not end:
        return None

    if life_span.get("ended"):
        return f"{begin or '?'}-{end or '?'}"

    return f"{begin}-present" if begin else None


def _format_ms(milliseconds):

    if not milliseconds:
        return None

    seconds = round(milliseconds / 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"
