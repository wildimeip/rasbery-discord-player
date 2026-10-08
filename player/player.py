"""The queue and what plays next: requested songs first, then random songs in random mode."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .music import MusicSearch, Track
from .store import Store

log = logging.getLogger(__name__)

Announce = Callable[[str], Awaitable[None]]

# After this many songs fail in a row (no network, YouTube changed), stop instead of spinning.
MAX_FAILURES_IN_A_ROW = 5


@dataclass
class AddResult:
    added: list[Track] = field(default_factory=list)
    too_long: list[Track] = field(default_factory=list)
    queue_full: int = 0
    position: int = 0  # queue position of the first added song; 0 = playing now


class Player:
    def __init__(
        self,
        backend,
        search: MusicSearch,
        store: Store,
        *,
        max_queue: int = 200,
        max_song_seconds: int = 900,
        random_playlist_id: str = "",
        announce: Announce | None = None,
    ):
        self.backend = backend
        self.search = search
        self.store = store
        self.max_queue = max_queue
        self.max_song_seconds = max_song_seconds
        self.random_playlist_id = random_playlist_id
        self.announce = announce
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.last: Track | None = None
        self.paused = False
        self.random_mode = False
        self._entry_id: int | None = None  # mpv's id for the file now playing
        self._recent: deque[str] = deque(maxlen=50)
        self._failures = 0
        self._playlist: list[Track] | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        backend.on_event = self.handle_event

    # --- mpv events -------------------------------------------------------------------------

    async def handle_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "start-file":
            self._entry_id = msg.get("playlist_entry_id")
        elif event == "end-file" and msg.get("reason") in ("eof", "error"):
            # Ignore the end of a file we already moved past (skip racing with a song's end).
            if self._entry_id is None or msg.get("playlist_entry_id") != self._entry_id:
                return
            self._entry_id = None
            # Runs as a task: this handler runs inside mpv's reader, which play() waits on.
            task = asyncio.create_task(self._song_ended(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _song_ended(self, msg: dict) -> None:
        async with self._lock:
            ended = self.current
            if msg.get("reason") == "error":
                self._failures += 1
                reason = msg.get("file_error") or "unknown error"
                log.warning("Could not play %s: %s", ended, reason)
                if ended:
                    await self._say(f"Could not play **{ended.label}** ({reason}), skipping.")
                if self._failures >= MAX_FAILURES_IN_A_ROW:
                    self.current = None
                    self.random_mode = False
                    await self._say(
                        f"{self._failures} songs failed in a row, so I stopped. "
                        "Check the Pi's network, then play something again."
                    )
                    self._failures = 0
                    return
            else:
                self._failures = 0
            await self._play_next()

    async def backend_restarted(self) -> None:
        """mpv crashed and was started again: carry on with the next song."""
        async with self._lock:
            self._entry_id = None
            if self.current:
                await self._play_next()

    # --- playing ------------------------------------------------------------------------------

    async def _say(self, text: str) -> None:
        if self.announce:
            try:
                await self.announce(text)
            except Exception:
                log.exception("Could not send announcement")

    async def _play_next(self) -> Track | None:
        """Starts the next song (queue, then random mode). Call with the lock held."""
        track = self.queue.pop(0) if self.queue else None
        if track is None and self.random_mode:
            picks = await self._random_picks(1)
            track = picks[0] if picks else None
            if track is None:
                self.random_mode = False
                if self.current:  # a song just ended; otherwise the caller answers
                    await self._say("Random mode is off: there are no songs to pick from yet.")
        if track is None:
            if self.current:
                self.last = self.current
            self.current = None
            self.paused = False
            return None
        if self.current:
            self.last = self.current
        self.current = track
        self.paused = False
        self._entry_id = None
        self._recent.append(track.video_id)
        self.store.record_play(track.video_id)
        log.info("Playing %s (%s)", track.label, track.video_id)
        try:
            await self.backend.play(track.url)
        except Exception:
            log.exception("mpv could not start %s", track.url)
            self.current = None
            await self._say("The audio player is not responding; it will restart shortly.")
            return None
        source = " (random)" if track.requested_by == "random" else ""
        await self._say(f"Now playing{source}: **{track.label}**")
        return track

    async def add(self, tracks: list[Track]) -> AddResult:
        result = AddResult()
        async with self._lock:
            # The first song starts right away when nothing plays, so it does not take a slot.
            room = self.max_queue - len(self.queue) + (1 if self.current is None else 0)
            for track in tracks:
                if self.max_song_seconds and (track.duration or 0) > self.max_song_seconds:
                    result.too_long.append(track)
                    continue
                if len(result.added) >= room:
                    result.queue_full += 1
                    continue
                self.store.record_request(track)
                self.queue.append(track)
                result.added.append(track)
            if not result.added:
                return result
            result.position = len(self.queue) - len(result.added) + 1
            if self.current is None:
                await self._play_next()
                result.position -= 1
        return result

    async def skip(self) -> Track | None:
        """Moves on to the next song; returns it (None when nothing is left)."""
        async with self._lock:
            if self.current is None:
                return None
            self._entry_id = None
            track = await self._play_next()
            if track is None:
                await self.backend.stop()
            return track

    async def stop(self) -> None:
        """Stops playing, empties the queue and turns random mode off."""
        async with self._lock:
            self.queue.clear()
            self.random_mode = False
            self._entry_id = None
            if self.current:
                self.last = self.current
            self.current = None
            self.paused = False
            await self.backend.stop()

    async def pause(self) -> bool:
        if self.current is None:
            return False
        await self.backend.set_pause(True)
        self.paused = True
        return True

    async def resume(self) -> bool:
        if self.current is None:
            return False
        await self.backend.set_pause(False)
        self.paused = False
        return True

    async def set_volume(self, volume: int) -> None:
        await self.backend.set_volume(volume)

    @property
    def volume(self) -> int:
        return self.backend.volume

    async def position(self) -> float | None:
        return await self.backend.position() if self.current else None

    def shuffle(self) -> int:
        random.shuffle(self.queue)
        return len(self.queue)

    def clear(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    def remove(self, position: int) -> Track | None:
        if 1 <= position <= len(self.queue):
            return self.queue.pop(position - 1)
        return None

    # --- random -------------------------------------------------------------------------------

    async def set_random(self, on: bool) -> Track | None:
        """Turns random mode on/off; starts a random song right away if nothing plays."""
        async with self._lock:
            self.random_mode = on
            if on and self.current is None:
                return await self._play_next()
        return None

    async def add_random(self, count: int) -> AddResult:
        async with self._lock:
            picks = await self._random_picks(count)
        return await self.add(picks)

    async def _random_picks(self, count: int) -> list[Track]:
        exclude = set(self._recent) | {t.video_id for t in self.queue}
        if self.current:
            exclude.add(self.current.video_id)
        candidates = self.store.random_tracks(count * 2, exclude)
        if self.random_playlist_id:
            candidates += random.sample(
                await self._playlist_tracks(), min(count * 2, len(self._playlist or []))
            )
        random.shuffle(candidates)
        picks: list[Track] = []
        seen: set[str] = set()

        def take(tracks: list[Track], fresh_only: bool) -> None:
            for track in tracks:
                if len(picks) == count:
                    return
                if track.video_id in seen or (fresh_only and track.video_id in exclude):
                    continue
                seen.add(track.video_id)
                picks.append(track.by("random"))

        take(candidates, fresh_only=True)
        seed = self.current or self.last
        if len(picks) < count and seed:
            # Not enough new songs in the history: ask YouTube Music for songs like the last one.
            try:
                radio = await asyncio.to_thread(self.search.radio, seed.video_id, 25)
            except Exception:
                log.exception("Radio lookup failed")
                radio = []
            take(radio, fresh_only=True)
        take(candidates, fresh_only=False)  # last resort: repeat recent songs
        return picks

    async def _playlist_tracks(self) -> list[Track]:
        if self._playlist is None:
            try:
                self._playlist = await asyncio.to_thread(
                    self.search.playlist, self.random_playlist_id, 500
                )
                log.info("Loaded %d songs from RANDOM_PLAYLIST_ID", len(self._playlist))
            except Exception:
                log.exception("Could not load RANDOM_PLAYLIST_ID %s", self.random_playlist_id)
                return []  # try again next time
        return self._playlist
