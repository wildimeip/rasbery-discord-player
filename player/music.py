"""Finding songs on YouTube Music (ytmusicapi). Blocking calls; run them in a thread."""

from __future__ import annotations

import logging
import re
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
        for kind in ("songs", "videos"):
            for item in self.client.search(query, filter=kind, limit=5):
                track = track_from_item(item)
                if track:
                    return track
        return None

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
        """A message -> tracks: a song link or id, a playlist link or id, or a song name."""
        text = clean_query(text)
        video_id, playlist_id = parse_link(text)
        if video_id:
            track = self.lookup(video_id)
            return [track] if track else []
        if playlist_id:
            return self.playlist(playlist_id, limit)
        if _VIDEO_ID.match(text):
            # A bare YouTube Music id (dQw4w9WgXcQ); an 11-letter song name falls through.
            track = self.lookup(text)
            if track:
                return [track]
        elif _PLAYLIST_ID.match(text):
            try:
                tracks = self.playlist(text, limit)
            except Exception:
                tracks = []
            if tracks:
                return tracks
        query = " ".join(_URL.sub(" ", text).split())
        if not query:
            return []
        track = self.search(query)
        return [track] if track else []
