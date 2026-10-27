"""Internet radio stations from radio-browser.info (free, no account): the last fallback when
neither YouTube nor SoundCloud has songs to play, and `!radio <genre>` on demand."""

from __future__ import annotations

import json
import logging
import random
import urllib.parse
import urllib.request
from collections.abc import Callable

from .music import Track

log = logging.getLogger(__name__)

# radio-browser.info runs several equal servers; the first one that answers is used.
SERVERS = (
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://all.api.radio-browser.info",
)
USER_AGENT = "rasbery-discord-player/1.0 (+https://github.com/wildimeip/rasbery-discord-player)"
RADIO_PREFIX = "radio-"


def is_radio(track: Track | None) -> bool:
    return bool(track) and track.video_id.startswith(RADIO_PREFIX)


def _get_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


class RadioSearch:
    def __init__(
        self, servers: tuple[str, ...] = SERVERS, get_json: Callable[[str], object] = _get_json
    ):
        self.servers = servers
        self.get_json = get_json

    def _ask(self, path: str, params: dict[str, str | int]) -> list:
        query = urllib.parse.urlencode(params)
        last: Exception | None = None
        for server in self.servers:
            try:
                answer = self.get_json(f"{server}{path}?{query}")
                return answer if isinstance(answer, list) else []
            except Exception as e:  # try the next server
                last = e
        raise RuntimeError(f"radio-browser.info is not answering ({last})")

    def search(self, text: str, limit: int = 10) -> list[Track]:
        """The most popular working stations for a genre ("rock", "jazz") or, failing that,
        with that name ("Radio Beat"). Raises RuntimeError when the service is down."""
        text = text.strip().lower()
        common = {"order": "clickcount", "reverse": "true", "hidebroken": "true", "limit": limit}
        stations = self._ask("/json/stations/search", {"tag": text, **common})
        if not stations:
            stations = self._ask("/json/stations/search", {"name": text, **common})
        tracks = []
        for s in stations:
            url = s.get("url_resolved") or s.get("url") or ""
            if not s.get("stationuuid") or not url.startswith(("http://", "https://")):
                continue
            name = " ".join(str(s.get("name") or "radio").split())[:80]
            tracks.append(Track(f"{RADIO_PREFIX}{s['stationuuid']}", name, "", None, "", url))
        return tracks

    def pick(self, genre: str, exclude: set[str] | frozenset[str] = frozenset()) -> Track | None:
        """A random station among the most popular of a genre, avoiding `exclude`."""
        stations = self.search(genre)
        fresh = [s for s in stations if s.video_id not in exclude] or stations
        return random.choice(fresh) if fresh else None
