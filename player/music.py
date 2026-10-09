"""Finding songs on YouTube Music (ytmusicapi). Blocking calls; run them in a thread."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID = re.compile(r"^(PL|OLAK5uy_|VL|RD)[A-Za-z0-9_-]{10,}$")
_URL = re.compile(r"https?://\S+")
_DISCORD_MARKUP = re.compile(r"<[@#][!&]?\d+>|<a?:\w+:\d+>")


@dataclass(frozen=True)
class Track:
    video_id: str
    title: str
    artist: str = ""
    duration: int | None = None  # seconds
    requested_by: str = ""

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"

    @property
    def label(self) -> str:
        name = f"{self.artist} - {self.title}" if self.artist else self.title
        return f"{name} ({format_duration(self.duration)})" if self.duration else name

    def by(self, requested_by: str) -> Track:
        return Track(self.video_id, self.title, self.artist, self.duration, requested_by)


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def parse_duration(text: str | None) -> int | None:
    """'3:45' or '1:02:03' -> seconds."""
    if not text:
        return None
    try:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    except ValueError:
        return None


def clean_query(text: str) -> str:
    """Strips Discord mentions/emoji markup and link brackets from a message."""
    text = _DISCORD_MARKUP.sub(" ", text)
    text = text.replace("<http", "http").replace("`", " ")
    text = re.sub(r"(https?://\S+)>", r"\1", text)
    return " ".join(text.split())


def parse_link(text: str) -> tuple[str | None, str | None]:
    """Returns (video_id, playlist_id) of the first YouTube / YouTube Music link in text."""
    match = _URL.search(text)
    if not match:
        return None, None
    url = urlparse(match.group(0))
    host = (url.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = parse_qs(url.query)
    playlist = (query.get("list") or [None])[0]
    video = None
    if host == "youtu.be":
        video = url.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "music.youtube.com"}:
        if url.path == "/watch":
            video = (query.get("v") or [None])[0]
        elif url.path.startswith(("/shorts/", "/live/", "/embed/")):
            video = url.path.split("/")[2] if len(url.path.split("/")) > 2 else None
    else:
        return None, None
    if video and not _VIDEO_ID.match(video):
        video = None
    return video, playlist


def normalize(text: str) -> str:
    """'Kabát - Pivrnec!' -> 'kabat pivrnec': lower case, no accents or punctuation."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def artist_named(track: Track, query: str) -> bool:
    """True when one of the track's artists (the band) is written in the query."""
    padded = f" {normalize(query)} "
    names = (normalize(n) for n in track.artist.split(", "))
    return any(n and f" {n} " in padded for n in names)


def word_score(track: Track, query: str) -> tuple[bool, int]:
    """(every query word is in the artist or title, how many of them are)."""
    words = normalize(query).split()
    haystack = f" {normalize(f'{track.artist} {track.title}')} "
    hits = sum(f" {w}" in haystack for w in words)
    return hits == len(words), hits


def rank(tracks: list[Track], query: str) -> list[Track]:
    """Songs by an artist named in the query first, then by matching words; ties keep
    YouTube Music's order."""

    def key(track: Track) -> tuple[bool, bool, int]:
        full, hits = word_score(track, query)
        return not artist_named(track, query), not full, -hits

    return sorted(tracks, key=key)


def _artists(item: dict) -> str:
    names = [a.get("name", "") for a in item.get("artists") or [] if a.get("name")]
    return ", ".join(names)


def track_from_item(item: dict) -> Track | None:
    """A Track from a ytmusicapi search/playlist/watch item, or None if not playable."""
    video_id = item.get("videoId")
    if not video_id or item.get("isAvailable") is False:
        return None
    duration = item.get("duration_seconds")
    if duration is None:
        duration = parse_duration(item.get("duration") or item.get("length"))
    return Track(video_id, item.get("title") or video_id, _artists(item), duration)


class MusicSearch:
    def __init__(self, client=None):
        if client is None:
            from ytmusicapi import YTMusic

            client = YTMusic()
        self.client = client

    def search(self, query: str) -> Track | None:
        """Best song match; falls back to videos (covers, live versions, uploads)."""
        found = self.search_many(query, 1)
        return found[0] if found else None

    def search_many(self, query: str, count: int, exclude: Collection[str] = ()) -> list[Track]:
        """Up to `count` matches, best first, leaving out the video ids in `exclude`.

        Songs come first; videos (covers, live versions, uploads) are added when the songs run
        short or none of them has every word of the query. Matches by an artist named in the
        query go first, then the ones with more of the query's words (see rank())."""
        page = max(20, len(exclude) + count + 5)
        found: list[Track] = []
        seen = set(exclude)
        for kind in ("songs", "videos"):
            if (
                kind == "videos"
                and len(found) >= count
                and any(word_score(t, query)[0] for t in found)
            ):
                break
            for item in self.client.search(query, filter=kind, limit=page):
                track = track_from_item(item)
                if track and track.video_id not in seen:
                    seen.add(track.video_id)
                    found.append(track)
        return rank(found, query)[:count]

    def more(self, text: str, exclude: Collection[str], count: int = 3) -> list[Track]:
        """The next `count` matches for a song name, after the ones in `exclude`."""
        query = " ".join(_URL.sub(" ", clean_query(text)).split())
        return self.search_many(query, count, exclude) if query else []

    def lookup(self, video_id: str) -> Track | None:
        """The song with this id, or None when YouTube does not know it."""
        song = self.client.get_song(video_id) or {}
        details = song.get("videoDetails") or {}
        status = (song.get("playabilityStatus") or {}).get("status", "OK")
        if status != "OK" or not (details.get("videoId") or details.get("title")):
            return None
        length = details.get("lengthSeconds")
        return Track(
            video_id,
            details.get("title") or video_id,
            details.get("author", ""),
            int(length) if str(length or "").isdigit() else None,
        )

    def playlist(self, playlist_id: str, limit: int = 100) -> list[Track]:
        if playlist_id.startswith("VL"):  # browse id form of a playlist id
            playlist_id = playlist_id[2:]
        data = self.client.get_playlist(playlist_id, limit=limit)
        return [t for t in map(track_from_item, data.get("tracks") or []) if t][:limit]

    def radio(self, video_id: str, limit: int = 25) -> list[Track]:
        """Songs YouTube Music would play after this one."""
        data = self.client.get_watch_playlist(videoId=video_id, radio=True, limit=limit)
        tracks = [t for t in map(track_from_item, data.get("tracks") or []) if t]
        return [t for t in tracks if t.video_id != video_id]

    def resolve(self, text: str, limit: int = 100) -> list[Track]:
        """A message -> tracks to play: a song link or id, a playlist, or the best name match."""
        return self.resolve_choices(text, limit, 1)[0]

    def resolve_choices(
        self, text: str, limit: int = 100, choices: int = 1
    ) -> tuple[list[Track], bool]:
        """Like resolve(), but a song name gives up to `choices` matches to pick from.

        Returns (tracks, alternatives): alternatives is True when the tracks are different
        matches for a name (pick one), False when they are all to be played."""
        text = clean_query(text)
        video_id, playlist_id = parse_link(text)
        if video_id:
            track = self.lookup(video_id)
            return ([track] if track else []), False
        if playlist_id:
            return self.playlist(playlist_id, limit), False
        if _VIDEO_ID.match(text):
            # A bare YouTube Music id (dQw4w9WgXcQ); an 11-letter song name falls through.
            track = self.lookup(text)
            if track:
                return [track], False
        elif _PLAYLIST_ID.match(text):
            try:
                tracks = self.playlist(text, limit)
            except Exception:
                tracks = []
            if tracks:
                return tracks, False
        query = " ".join(_URL.sub(" ", text).split())
        if not query:
            return [], False
        found = self.search_many(query, max(1, choices))
        return found, len(found) > 1
