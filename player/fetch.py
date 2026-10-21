"""Downloads a song's audio with yt-dlp before mpv plays it.

Letting mpv stream YouTube itself fails more and more with "HTTP error 403 Forbidden": YouTube
only serves the audio to requests that look like yt-dlp's own downloader (chunked, matching
headers). So yt-dlp downloads the file (a few MB) and mpv plays it from disk.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import yt_dlp

from .music import Track

log = logging.getLogger(__name__)

# "ERROR: [youtube] zr-QdCcG3w8: This video is not available" -> "This video is not available"
_PREFIX = re.compile(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]{11}:\s*)?")


class FetchError(Exception):
    """The song can't be played (blocked, removed, or the download failed)."""


class BotCheck(FetchError):
    """YouTube wants a signed-in person ("Sign in to confirm you're not a bot"). It blocks the
    Pi's internet address, not one song, so trying more songs only makes it worse."""


BOT_CHECK = ("not a bot", "sign in to confirm")
# After a bot check, YouTube isn't asked again for this long (asking keeps the address flagged).
YOUTUBE_PAUSE = 30 * 60
SOUNDCLOUD_PREFIX = "sc-"  # downloaded file names from SoundCloud start with this

# Words in YouTube titles that a SoundCloud upload of the same song won't have.
_NOISE = re.compile(
    r"\((?:official|lyrics?|audio|video|hd|hq|remaster|explicit|clip)[^)]*\)|"
    r"\[(?:official|lyrics?|audio|video|hd|hq|remaster|explicit|clip)[^\]]*\]",
    re.I,
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", _NOISE.sub(" ", text).lower()) if len(w) > 1}


def soundcloud_match(track: Track, entry: dict) -> bool:
    """Whether a SoundCloud search result is the same song: most title words match and the
    length is close (which also rules out 30-second previews)."""
    wanted = _words(track.title)
    found = _words(f"{entry.get('title') or ''} {entry.get('uploader') or ''}")
    if not wanted or len(wanted & found) < max(1, round(len(wanted) * 0.6)):
        return False
    duration = entry.get("duration") or 0
    if duration <= 35:
        return False
    if track.duration:
        return abs(duration - track.duration) <= max(15, track.duration * 0.12)
    return True


def is_soundcloud(path: str) -> bool:
    return Path(path).name.startswith(SOUNDCLOUD_PREFIX)


def clean_error(message: str) -> str:
    first = str(message).strip().splitlines()[0] if str(message).strip() else ""
    first = first.split(". Use --cookies")[0]  # yt-dlp's long advice after the reason
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
        cookies_file: Path | None = None,
        pot_provider_url: str = "",
        soundcloud: bool = True,
        ydl_factory: Callable[[dict], yt_dlp.YoutubeDL] = yt_dlp.YoutubeDL,
    ):
        self.cache_dir = Path(cache_dir)
        self.keep = keep  # the song playing now, the next one, and one spare
        self.pot_provider_url = pot_provider_url
        self.soundcloud = soundcloud
        self.ydl_factory = ydl_factory
        self.youtube_blocked_until = 0.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for old in self.cache_dir.iterdir():  # left over from before a restart
            old.unlink(missing_ok=True)
        # Optional cookies of a signed-in (spare) YouTube account, for when YouTube asks to
        # confirm we're not a bot. yt-dlp writes the cookies back, so it gets a copy: the
        # original is a read-only Docker secret.
        self.cookies: Path | None = None
        if cookies_file and Path(cookies_file).is_file() and Path(cookies_file).stat().st_size:
            self.cookies = self.cache_dir / ".cookies.txt"
            shutil.copyfile(cookies_file, self.cookies)
            log.info("Using YouTube cookies from %s", cookies_file)

    def options(self, prefix: str = "") -> dict:
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(self.cache_dir / f"{prefix}%(id)s.%(ext)s"),
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
        if self.cookies:
            options["cookiefile"] = str(self.cookies)
        if self.pot_provider_url:
            # The bgutil PO token server (a second container) makes requests look like they
            # come from a real browser, which YouTube's bot check trusts more.
            options["extractor_args"] = {
                "youtubepot-bgutilhttp": {"base_url": [self.pot_provider_url]}
            }
        return options

    @property
    def youtube_blocked(self) -> bool:
        return time.monotonic() < self.youtube_blocked_until

    def _extract(self, url: str, download: bool, prefix: str = "") -> dict:
        try:
            with self.ydl_factory(self.options(prefix)) as ydl:
                return ydl.extract_info(url, download=download) or {}
        except yt_dlp.utils.DownloadError as e:
            if any(words in str(e).lower() for words in BOT_CHECK):
                raise BotCheck("YouTube asks to confirm I'm not a bot") from None
            raise FetchError(clean_error(str(e))) from None

    @staticmethod
    def _path(info: dict) -> Path:
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads else None
        if path is None or not path.exists():
            raise FetchError("download failed")
        return path

    def _download(self, track: Track) -> Path:
        if not self.youtube_blocked:
            try:
                return self._path(self._extract(track.url, download=True))
            except BotCheck:
                self.youtube_blocked_until = time.monotonic() + YOUTUBE_PAUSE
                log.warning("YouTube bot check: not asking YouTube for %d min", YOUTUBE_PAUSE // 60)
                if not self.soundcloud:
                    raise
        elif not self.soundcloud:
            raise BotCheck("YouTube asks to confirm I'm not a bot")
        return self._from_soundcloud(track)

    def _from_soundcloud(self, track: Track) -> Path:
        """The same song from SoundCloud (no account needed), while YouTube blocks the Pi."""
        query = " ".join(_NOISE.sub(" ", f"{track.artist} {track.title}").split())
        try:
            found = self._extract(f"scsearch5:{query}", download=False)
        except FetchError as e:
            log.warning("SoundCloud search failed for %r: %s", query, e)
            found = {}
        for entry in found.get("entries") or []:
            if entry and soundcloud_match(track, entry):
                url = entry.get("webpage_url") or entry.get("url")
                log.info("Playing %s from SoundCloud: %s", track.video_id, url)
                return self._path(self._extract(url, download=True, prefix=SOUNDCLOUD_PREFIX))
        raise FetchError("YouTube is blocking the Pi and this song isn't on SoundCloud")

    async def __call__(self, track: Track) -> str:
        path = await asyncio.to_thread(self._download, track)
        self._prune(path)
        return str(path)

    def _prune(self, newest: Path) -> None:
        files = sorted(
            (
                p
                for p in self.cache_dir.iterdir()
                if p.is_file() and p != newest and not p.name.startswith(".")
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[self.keep - 1 :]:
            old.unlink(missing_ok=True)
