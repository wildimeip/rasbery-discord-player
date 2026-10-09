"""The queue and what plays next: requested songs first, then random songs in random mode."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .fetch import FetchError
from .music import MusicSearch, Track
from .store import Store

log = logging.getLogger(__name__)

Announce = Callable[[str], Awaitable[None]]
# Turns a track into what mpv plays (a downloaded file); raises FetchError when it can't.
Fetch = Callable[[Track], Awaitable[str]]

# After this many songs fail in a row (no network, YouTube changed), stop instead of spinning.
MAX_FAILURES_IN_A_ROW = 5
# Songs YouTube won't give to anyone here (blocked in this country, removed, private) say
# nothing about the network, so they don't count as failures; this caps the skipping.
MAX_SKIPS_PER_PICK = 10
UNAVAILABLE = ("not available", "unavailable", "private video", "removed", "blocked")
# Similar songs come in runs: this many from one seed song's radio, then a new seed.
SIMILAR_RUN = 5


@dataclass
class AddResult:
    added: list[Track] = field(default_factory=list)
    too_long: list[Track] = field(default_factory=list)
    banned: list[Track] = field(default_factory=list)
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
        random_mode: bool = False,
        similar_percent: int = 0,
        announce: Announce | None = None,
        fetch: Fetch | None = None,
    ):
        self.backend = backend
        self.search = search
        self.store = store
        self.max_queue = max_queue
        self.max_song_seconds = max_song_seconds
        self.random_playlist_id = random_playlist_id
        self.similar_percent = similar_percent
        self.announce = announce
        self.fetch = fetch
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.last: Track | None = None
        self.paused = False
        self.random_mode = random_mode
        self._entry_id: int | None = None  # mpv's id for the file now playing
        self._recent: deque[str] = deque(maxlen=50)
        self._failures = 0
        self._playlist: list[Track] | None = None
        self._similar: list[Track] = []  # the current run of songs like a seed song
        self._next_seed: Track | None = None  # the last song someone asked for
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        # Downloading the next song while the current one plays, so the next one starts at once.
        self._ready: dict[str, str] = {}  # video id -> downloaded file
        self._fetching: dict[str, asyncio.Task] = {}
        self._next_random: Track | None = None  # random song picked in advance
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
                    await self._give_up()
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
        """Starts the next song (queue, then random mode), skipping songs that can't be
        downloaded. Call with the lock held."""
        for _ in range(MAX_SKIPS_PER_PICK):
            track = await self._next_track()
            if track is None:
                if self.current:
                    self.last = self.current
                self.current = None
                self.paused = False
                return None
            try:
                source = await self._source(track)
            except FetchError as e:
                if not await self._fetch_failed(track, str(e)):
                    return None
                continue
            except Exception:
                log.exception("Could not download %s", track.video_id)
                if not await self._fetch_failed(track, "download failed"):
                    return None
                continue
            return await self._start(track, source)
        await self._give_up()
        return None

    async def _source(self, track: Track) -> str:
        """What mpv should play: the downloaded file (from the prefetch when there is one)."""
        if self.fetch is None:
            return track.url
        ready = self._ready.pop(track.video_id, None)
        if ready and Path(ready).exists():
            return ready
        if track.video_id in self._fetching:
            source = await self._fetching[track.video_id]
            self._ready.pop(track.video_id, None)
            return source
        return await self.fetch(track)

    def _prefetch(self) -> None:
        """Starts downloading the song that will play next, if it isn't already."""
        if self.fetch is None or self.current is None:
            return
        target = self.queue[0] if self.queue else None
        if target is None and self.random_mode:
            if self._next_random is None:
                task = asyncio.create_task(self._pick_next_random())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                return
            target = self._next_random
        if target is None or target.video_id in self._ready or target.video_id in self._fetching:
            return
        task = asyncio.create_task(self.fetch(target))
        self._fetching[target.video_id] = task
        task.add_done_callback(lambda t, vid=target.video_id: self._prefetched(vid, t))

    async def _pick_next_random(self) -> None:
        picks = await self._random_picks(1)
        if picks and self._next_random is None:
            self._next_random = picks[0]
            self._prefetch()

    def _prefetched(self, video_id: str, task: asyncio.Task) -> None:
        self._fetching.pop(video_id, None)
        if task.cancelled() or task.exception() is not None:
            return  # tried again (and reported) when the song's turn comes
        self._ready[video_id] = task.result()
        while len(self._ready) > 2:  # older downloads are deleted by the fetcher anyway
            self._ready.pop(next(iter(self._ready)))

    async def _fetch_failed(self, track: Track, reason: str) -> bool:
        """Notes a song that could not be downloaded; False when it is time to give up."""
        log.warning("Could not download %s: %s", track, reason)
        self._recent.append(track.video_id)  # random mode: don't pick it again soon
        if not any(word in reason.lower() for word in UNAVAILABLE):
            self._failures += 1
        if track.requested_by != "random":
            # Random songs that are blocked (often "not available" in this country) are
            # skipped quietly; a song someone asked for gets an answer.
            await self._say(f"Could not play **{track.label}** ({reason}), skipping.")
        if self._failures >= MAX_FAILURES_IN_A_ROW:
            await self._give_up()
            return False
        return True

    async def _give_up(self) -> None:
        if self.current:
            self.last = self.current
        self.current = None
        self.random_mode = False
        await self._say(
            "Too many songs failed in a row, so I stopped. "
            "Check the Pi's network, then play something again."
        )
        self._failures = 0

    async def _next_track(self) -> Track | None:
        track = self.queue.pop(0) if self.queue else None
        if track is None and self.random_mode:
            if self._next_random and self._next_random.video_id not in self.store.banned_ids():
                track, self._next_random = self._next_random, None
            else:
                self._next_random = None
                picks = await self._random_picks(1)
                track = picks[0] if picks else None
            if track is None:
                self.random_mode = False
                if self.current:  # a song just ended; otherwise the caller answers
                    await self._say("Random mode is off: there are no songs to pick from yet.")
        return track

    async def _start(self, track: Track, source: str) -> Track | None:
        if self.current:
            self.last = self.current
        self.current = track
        self.paused = False
        self._entry_id = None
        self._recent.append(track.video_id)
        self.store.record_play(track.video_id)
        log.info("Playing %s (%s)", track.label, track.video_id)
        try:
            await self.backend.play(source)
        except Exception:
            log.exception("mpv could not start %s", source)
            self.current = None
            await self._say("The audio player is not responding; it will restart shortly.")
            return None
        source = " (random)" if track.requested_by == "random" else ""
        await self._say(f"Now playing{source}: **{track.label}**")
        self._prefetch()
        return track

    async def add(self, tracks: list[Track]) -> AddResult:
        result = AddResult()
        async with self._lock:
            # The first song starts right away when nothing plays, so it does not take a slot.
            room = self.max_queue - len(self.queue) + (1 if self.current is None else 0)
            banned = self.store.banned_ids()
            for track in tracks:
                if track.video_id in banned:
                    result.banned.append(track)
                    continue
                if self.max_song_seconds and (track.duration or 0) > self.max_song_seconds:
                    result.too_long.append(track)
                    continue
                if len(result.added) >= room:
                    result.queue_full += 1
                    continue
                self.store.record_request(track)
                if track.requested_by != "random":
                    # Random songs that follow should sound like what was just asked for.
                    self._next_seed = track
                    self._similar.clear()
                self.queue.append(track)
                result.added.append(track)
            if not result.added:
                return result
            result.position = len(self.queue) - len(result.added) + 1
            if self.current is None:
                await self._play_next()
                result.position -= 1
            else:
                self._prefetch()
        return result

    async def ban(self, track: Track, by: str) -> bool:
        """Never plays this song again: drops it from the queue, and skips it if it is playing.
        Returns True when it was playing."""
        self.store.ban(track, by)
        async with self._lock:
            self.queue = [t for t in self.queue if t.video_id != track.video_id]
            self._similar = [t for t in self._similar if t.video_id != track.video_id]
            if self.current is None or self.current.video_id != track.video_id:
                return False
            self._entry_id = None
            if await self._play_next() is None:
                await self.backend.stop()
            return True

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
        """Stops playing and empties the queue (random mode stays as it is for !start)."""
        async with self._lock:
            self.queue.clear()
            self._next_random = None
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

    async def start(self) -> Track | None:
        """Plays from the queue, or random songs when it is empty (turns random mode on)."""
        async with self._lock:
            if self.current is not None:
                return None
            if not self.queue:
                self.random_mode = True
            return await self._play_next()

    async def set_random(self, on: bool) -> Track | None:
        """Turns random mode on/off; starts a random song right away if nothing plays."""
        async with self._lock:
            self.random_mode = on
            if not on:
                self._next_random = None
            if on and self.current is None:
                return await self._play_next()
            self._prefetch()
        return None

    async def add_random(self, count: int) -> AddResult:
        async with self._lock:
            picks = await self._random_picks(count)
        return await self.add(picks)

    async def _random_picks(self, count: int) -> list[Track]:
        banned = self.store.banned_ids()
        exclude = set(self._recent) | {t.video_id for t in self.queue} | banned
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
                if track.video_id in seen or track.video_id in banned:
                    continue
                if fresh_only and track.video_id in exclude:
                    continue
                seen.add(track.video_id)
                picks.append(track.by("random"))

        similar = sum(random.randrange(100) < self.similar_percent for _ in range(count))
        if similar:
            take(await self._similar_tracks(similar, exclude), fresh_only=True)
        take(candidates, fresh_only=True)
        if len(picks) < count and self.similar_percent:
            take(await self._similar_tracks(count - len(picks), exclude), fresh_only=True)
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

    async def _similar_tracks(self, count: int, exclude: set[str]) -> list[Track]:
        """Songs like the ones people asked for: YouTube Music's radio for a seed song, which is
        the last requested song, or else a random one from the history. Takes a few songs from
        each seed, so the music drifts slowly instead of jumping between genres."""
        out: list[Track] = []
        for _ in range(3):  # a seed may give nothing new; try a couple of others
            self._similar = [t for t in self._similar if t.video_id not in exclude]
            while self._similar and len(out) < count:
                out.append(self._similar.pop(0))
            if len(out) == count:
                break
            seed = self._next_seed
            self._next_seed = None
            if seed is None:
                history = self.store.random_tracks(1, exclude)  # not a song just played
                if not history:
                    break
                seed = history[0]
            try:
                radio = await asyncio.to_thread(self.search.radio, seed.video_id, 25)
            except Exception:
                log.exception("Radio lookup failed for %s", seed.video_id)
                continue
            taken = {t.video_id for t in out}
            fresh = [t for t in radio if t.video_id not in exclude and t.video_id not in taken]
            random.shuffle(fresh)
            self._similar = fresh[:SIMILAR_RUN]
            log.info("Random songs now like %s", seed.label)
        return out

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
