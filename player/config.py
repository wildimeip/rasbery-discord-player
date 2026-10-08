"""Settings from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _read_token(env: dict[str, str]) -> str:
    token = env.get("DISCORD_TOKEN", "").strip()
    if token:
        return token
    path = Path(env.get("DISCORD_TOKEN_FILE", "/run/secrets/discord_token"))
    if path.is_file():
        token = path.read_text().strip()
    if not token:
        raise ConfigError(f"No Discord bot token: put it in {path} (one line) or set DISCORD_TOKEN")
    return token


@dataclass(frozen=True)
class Settings:
    discord_token: str
    channel_ids: frozenset[int]
    channel_name: str
    command_prefix: str
    queue_plain_messages: bool
    announce: bool
    data_dir: Path
    audio_device: str
    volume: int
    max_queue: int
    max_song_minutes: int
    history_scan_limit: int
    random_playlist_id: str
    mpv_socket: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        ids = set()
        for part in env.get("DISCORD_CHANNEL_IDS", "").replace(" ", "").split(","):
            if not part:
                continue
            if not part.isdigit():
                raise ConfigError(f"DISCORD_CHANNEL_IDS: {part!r} is not a channel id")
            ids.add(int(part))
        return cls(
            discord_token=_read_token(env),
            channel_ids=frozenset(ids),
            channel_name=env.get("DISCORD_CHANNEL_NAME", "music").strip().lstrip("#") or "music",
            command_prefix=env.get("COMMAND_PREFIX", "!").strip() or "!",
            queue_plain_messages=_bool(env.get("QUEUE_PLAIN_MESSAGES", "true")),
            announce=_bool(env.get("ANNOUNCE_NOW_PLAYING", "true")),
            data_dir=Path(env.get("DATA_DIR", "/data")),
            audio_device=env.get("MPV_AUDIO_DEVICE", "").strip(),
            volume=_int(env, "VOLUME", 70, 0, 130),
            max_queue=_int(env, "MAX_QUEUE", 200, 1, 10000),
            max_song_minutes=_int(env, "MAX_SONG_MINUTES", 15, 0, 600),
            history_scan_limit=_int(env, "HISTORY_SCAN_LIMIT", 500, 0, 100000),
            random_playlist_id=env.get("RANDOM_PLAYLIST_ID", "").strip(),
            mpv_socket=Path(env.get("MPV_SOCKET", "/tmp/mpv.sock")),
        )
