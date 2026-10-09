"""Test doubles for mpv and YouTube Music."""

from __future__ import annotations

from player.music import Track


class FakeBackend:
    """Records commands and lets a test emit the events mpv would send."""

    def __init__(self):
        self.on_event = None
        self.volume = 70
        self.calls: list[tuple] = []
        self.entry_id = 0

    async def play(self, url: str) -> None:
        self.calls.append(("play", url))
        self.entry_id += 1
        await self.on_event({"event": "start-file", "playlist_entry_id": self.entry_id})

    async def stop(self) -> None:
        self.calls.append(("stop",))

    async def set_pause(self, paused: bool) -> None:
        self.calls.append(("pause", paused))

    async def set_volume(self, volume: int) -> None:
        self.volume = volume

    async def position(self) -> float | None:
        return 12.0

    async def finish(self, reason: str = "eof", **extra) -> None:
        """The current file ends on its own."""
        await self.on_event(
            {"event": "end-file", "reason": reason, "playlist_entry_id": self.entry_id, **extra}
        )

    @property
    def played(self) -> list[str]:
        return [c[1].rsplit("=", 1)[1] for c in self.calls if c[0] == "play"]


def track(n: int | str, duration: int | None = 200, by: str = "ann") -> Track:
    vid = f"{n}".rjust(11, "x")
    return Track(vid, f"Song {n}", "Artist", duration, by)


class FakeSearch:
    def __init__(self, results: dict[str, list[Track]] | None = None, radio=None):
        self.results = results or {}
        self.radio_tracks = radio or []
        self.playlist_tracks: list[Track] = []
        self.matches: dict[str, list[Track]] = {}  # song names with several matches

    def resolve(self, text: str, limit: int = 100) -> list[Track]:
        return self.resolve_choices(text, limit, 1)[0]

    def resolve_choices(self, text: str, limit: int = 100, choices: int = 1):
        if text in self.matches:
            found = self.matches[text][:choices]
            return found, len(found) > 1
        return list(self.results.get(text, [])), False

    def more(self, text: str, exclude, count: int = 3) -> list[Track]:
        return [t for t in self.matches.get(text, []) if t.video_id not in exclude][:count]

    def radio(self, video_id: str, limit: int = 25) -> list[Track]:
        return list(self.radio_tracks)

    def playlist(self, playlist_id: str, limit: int = 100) -> list[Track]:
        return list(self.playlist_tracks)
