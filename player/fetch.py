"""Downloads a song's audio with yt-dlp before mpv plays it.

Letting mpv stream YouTube itself fails more and more with "HTTP error 403 Forbidden": YouTube
only serves the audio to requests that look like yt-dlp's own downloader (chunked, matching
headers). So yt-dlp downloads the file (a few MB) and mpv plays it from disk.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

import yt_dlp

from .music import Track

log = logging.getLogger(__name__)

# "ERROR: [youtube] zr-QdCcG3w8: This video is not available" -> "This video is not available"
_PREFIX = re.compile(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]{11}:\s*)?")


class FetchError(Exception):
    """The song can't be played (blocked, removed, or the download failed)."""


def clean_error(message: str) -> str:
    first = str(message).strip().splitlines()[0] if str(message).strip() else ""
    return _PREFIX.sub("", first).strip() or "download failed"


class _Log:
    """Sends yt-dlp's messages to our log instead of the terminal."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


class Fetcher:
    def __init__(
        self,
        cache_dir: Path,
        keep: int = 3,
        ydl_factory: Callable[[dict], yt_dlp.YoutubeDL] = yt_dlp.YoutubeDL,
    ):
        self.cache_dir = Path(cache_dir)
        self.keep = keep  # the song playing now, the next one, and one spare
        self.ydl_factory = ydl_factory
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for old in self.cache_dir.iterdir():  # left over from before a restart
            old.unlink(missing_ok=True)

    def options(self) -> dict:
        return {
            "format": "bestaudio/best",
            "outtmpl": str(self.cache_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "logger": _Log(),
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 20,
            "max_filesize": 100 * 1024 * 1024,
        }

    def _download(self, url: str) -> Path:
        try:
            with self.ydl_factory(self.options()) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            raise FetchError(clean_error(str(e))) from None
        downloads = (info or {}).get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads else None
        if path is None or not path.exists():
            raise FetchError("download failed")
        return path

    async def __call__(self, track: Track) -> str:
        path = await asyncio.to_thread(self._download, track.url)
        self._prune(path)
        return str(path)

    def _prune(self, newest: Path) -> None:
        files = sorted(
            (p for p in self.cache_dir.iterdir() if p.is_file() and p != newest),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[self.keep - 1 :]:
            old.unlink(missing_ok=True)
