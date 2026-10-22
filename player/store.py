"""Every song ever requested, kept in SQLite: the pool random mode picks from."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .music import Track

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    duration INTEGER,
    requested_by TEXT NOT NULL DEFAULT '',
    request_count INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    first_requested REAL NOT NULL,
    last_played REAL
);
CREATE TABLE IF NOT EXISTS banned (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    banned_by TEXT NOT NULL DEFAULT '',
    banned_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Whether a song could be played from SoundCloud while YouTube blocked the Pi.
CREATE TABLE IF NOT EXISTS soundcloud (
    video_id TEXT PRIMARY KEY,
    found INTEGER NOT NULL,
    checked_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def record_request(self, track: Track) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO songs (video_id, title, artist, duration, requested_by,
                                      request_count, first_requested)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(video_id) DO UPDATE SET
                     title = excluded.title, artist = excluded.artist,
                     duration = COALESCE(excluded.duration, duration),
                     request_count = request_count + 1""",
                (
                    track.video_id,
                    track.title,
                    track.artist,
                    track.duration,
                    track.requested_by,
                    time.time(),
                ),
            )

    def record_play(self, video_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE songs SET play_count = play_count + 1, last_played = ? WHERE video_id = ?",
                (time.time(), video_id),
            )

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0]

    def random_tracks(
        self,
        n: int,
        exclude: set[str] | frozenset[str] = frozenset(),
        on_soundcloud: bool = False,
    ) -> list[Track]:
        """Up to n random songs, preferring ones not in `exclude` (recently played/queued).
        `on_soundcloud`: only songs that played from SoundCloud before."""
        only = (
            "AND video_id IN (SELECT video_id FROM soundcloud WHERE found = 1) "
            if on_soundcloud
            else ""
        )
        rows = self.db.execute(
            "SELECT * FROM songs WHERE video_id NOT IN (SELECT video_id FROM banned) "
            f"{only}ORDER BY RANDOM() LIMIT ?",
            (n + len(exclude),),
        ).fetchall()
        fresh = [r for r in rows if r["video_id"] not in exclude]
        stale = [r for r in rows if r["video_id"] in exclude]
        return [
            Track(r["video_id"], r["title"], r["artist"], r["duration"], "random")
            for r in (fresh + stale)[:n]
        ]

    def ban(self, track: Track, by: str) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO banned (video_id, title, artist, banned_by, banned_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (track.video_id, track.title, track.artist, by, time.time()),
            )

    def unban(self, video_id: str) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM banned WHERE video_id = ?", (video_id,))
        return cur.rowcount > 0

    def banned(self) -> list[Track]:
        """Banned songs, oldest ban first; requested_by is who banned it."""
        rows = self.db.execute("SELECT * FROM banned ORDER BY banned_at").fetchall()
        return [Track(r["video_id"], r["title"], r["artist"], None, r["banned_by"]) for r in rows]

    def banned_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT video_id FROM banned")}

    def set_on_soundcloud(self, video_id: str, found: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO soundcloud (video_id, found, checked_at) VALUES (?, ?, ?)",
                (video_id, int(found), time.time()),
            )

    def not_on_soundcloud(self, days: int = 30) -> set[str]:
        """Songs SoundCloud didn't have when last searched (in the last `days` days)."""
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT video_id FROM soundcloud WHERE found = 0 AND checked_at > ?", (since,)
        )
        return {r[0] for r in rows}

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
