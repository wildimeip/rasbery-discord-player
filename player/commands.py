"""Chat commands: turns a message into a player action and a short answer."""

from __future__ import annotations

import asyncio
import logging

from .music import MusicSearch, format_duration
from .player import AddResult, Player

log = logging.getLogger(__name__)

ALIASES = {
    "p": "play",
    "s": "skip",
    "next": "skip",
    "n": "skip",
    "r": "resume",
    "unpause": "resume",
    "q": "queue",
    "list": "queue",
    "now": "np",
    "nowplaying": "np",
    "rnd": "random",
    "shuf": "shuffle",
    "vol": "volume",
    "v": "volume",
    "rm": "remove",
    "h": "help",
}

COMMANDS = {
    "play": "<song or link>  add a song (YouTube / YouTube Music link, playlist, or search)",
    "skip": "skip the current song",
    "pause": "pause",
    "resume": "continue playing",
    "stop": "stop, empty the queue and turn random mode off",
    "queue": "show what is playing and what comes next",
    "np": "show the current song",
    "random": "[on|off|<number>]  random mode, or add <number> random songs",
    "shuffle": "shuffle the queue",
    "clear": "empty the queue (the current song keeps playing)",
    "remove": "<number>  remove a song from the queue",
    "volume": "[0-130]  show or set the volume",
    "help": "this list",
}


def parse_command(text: str, prefix: str) -> tuple[str, str] | None:
    """'!p  song name' -> ('play', 'song name'); None when text is not a known command."""
    text = text.strip()
    if not text.startswith(prefix):
        return None
    body = text[len(prefix) :].strip()
    if not body:
        return None
    name, _, arg = body.partition(" ")
    name = name.lower()
    name = ALIASES.get(name, name)
    if name not in COMMANDS:
        return None
    return name, arg.strip()


def help_text(prefix: str, plain_messages: bool) -> str:
    lines = [f"`{prefix}{name}` {desc}" for name, desc in COMMANDS.items()]
    if plain_messages:
        lines.insert(0, "Write a song name or paste a link in this channel and I'll queue it.")
    return "\n".join(lines)


def describe_add(result: AddResult) -> str:
    parts = []
    if len(result.added) == 1:
        track = result.added[0]
        if result.position == 0:
            parts.append(f"Playing **{track.label}**")
        else:
            parts.append(f"Queued **{track.label}** (#{result.position})")
    elif result.added:
        start = "starting now" if result.position == 0 else f"from #{result.position}"
        parts.append(f"Queued {len(result.added)} songs, {start}")
    if result.too_long:
        names = ", ".join(t.title for t in result.too_long[:3])
        parts.append(f"skipped {len(result.too_long)} too long ({names})")
    if result.queue_full:
        parts.append(f"queue is full, {result.queue_full} not added")
    return "; ".join(parts) or "Nothing added."


class Commands:
    def __init__(self, player: Player, search: MusicSearch, prefix: str, plain_messages: bool):
        self.player = player
        self.search = search
        self.prefix = prefix
        self.plain_messages = plain_messages

    async def handle(self, text: str, author: str) -> str | None:
        """Answer for a chat message, or None when the message is not for the player."""
        parsed = parse_command(text, self.prefix)
        if parsed is None:
            if text.strip().startswith(self.prefix) or not self.plain_messages:
                return None
            parsed = ("play", text)
        name, arg = parsed
        return await getattr(self, f"cmd_{name}")(arg, author)

    async def cmd_play(self, arg: str, author: str) -> str:
        if not arg:
            if self.player.paused:
                await self.player.resume()
                return "Resumed."
            return f"What should I play? `{self.prefix}play <song or link>`"
        try:
            tracks = await asyncio.to_thread(self.search.resolve, arg, self.player.max_queue)
        except Exception:
            log.exception("Search failed for %r", arg)
            return "YouTube Music search failed, try again in a moment."
        if not tracks:
            return f"Found nothing for *{arg[:100]}*."
        return describe_add(await self.player.add([t.by(author) for t in tracks]))

    async def cmd_skip(self, arg: str, author: str) -> str:
        if self.player.current is None:
            return "Nothing is playing."
        track = await self.player.skip()
        return f"Skipped. Now: **{track.label}**" if track else "Skipped. The queue is empty."

    async def cmd_pause(self, arg: str, author: str) -> str:
        return "Paused." if await self.player.pause() else "Nothing is playing."

    async def cmd_resume(self, arg: str, author: str) -> str:
        return "Resumed." if await self.player.resume() else "Nothing is playing."

    async def cmd_stop(self, arg: str, author: str) -> str:
        await self.player.stop()
        return "Stopped and cleared the queue."

    async def cmd_np(self, arg: str, author: str) -> str:
        track = self.player.current
        if track is None:
            return "Nothing is playing."
        pos = await self.player.position()
        progress = (
            f" [{format_duration(int(pos))}/{format_duration(track.duration)}]" if pos else ""
        )
        state = " (paused)" if self.player.paused else ""
        who = f", requested by {track.requested_by}" if track.requested_by else ""
        return f"Now playing: **{track.label}**{progress}{state}{who}"

    async def cmd_queue(self, arg: str, author: str) -> str:
        lines = [await self.cmd_np("", author)]
        queue = self.player.queue
        for i, track in enumerate(queue[:15], 1):
            lines.append(f"`{i}.` {track.label}")
        if len(queue) > 15:
            lines.append(f"... and {len(queue) - 15} more")
        if not queue:
            lines.append("The queue is empty.")
        if self.player.random_mode:
            lines.append("Random mode is on: random songs play when the queue runs out.")
        return "\n".join(lines)

    async def cmd_random(self, arg: str, author: str) -> str:
        arg = arg.lower()
        if arg.isdigit():
            count = max(1, min(int(arg), 50))
            result = await self.player.add_random(count)
            if not result.added:
                return "No songs to pick from yet: play a few songs first."
            return describe_add(result)
        if arg in ("", "toggle"):
            on = not self.player.random_mode
        elif arg in ("on", "yes", "1", "start"):
            on = True
        elif arg in ("off", "no", "0", "stop"):
            on = False
        else:
            p = self.prefix
            return f"Use `{p}random`, `{p}random on|off` or `{p}random 10`."
        started = await self.player.set_random(on)
        if not on:
            return "Random mode off."
        if self.player.current is None:
            return "No songs to pick from yet: play a few songs first."
        if started:
            return f"Random mode on. Playing **{started.label}**"
        return "Random mode on: random songs play when the queue runs out."

    async def cmd_shuffle(self, arg: str, author: str) -> str:
        count = self.player.shuffle()
        return f"Shuffled {count} songs." if count else "The queue is empty."

    async def cmd_clear(self, arg: str, author: str) -> str:
        return f"Removed {self.player.clear()} songs from the queue."

    async def cmd_remove(self, arg: str, author: str) -> str:
        if not arg.isdigit():
            return f"Which one? `{self.prefix}remove 2` (see `{self.prefix}queue`)"
        track = self.player.remove(int(arg))
        return f"Removed **{track.label}**." if track else "There is no such song in the queue."

    async def cmd_volume(self, arg: str, author: str) -> str:
        if not arg:
            return f"Volume: {self.player.volume}"
        arg = arg.rstrip("%")
        if arg.startswith(("+", "-")) and arg[1:].isdigit():
            volume = self.player.volume + int(arg)
        elif arg.isdigit():
            volume = int(arg)
        else:
            return f"`{self.prefix}volume 0-130`"
        volume = max(0, min(volume, 130))
        await self.player.set_volume(volume)
        return f"Volume: {volume}"

    async def cmd_help(self, arg: str, author: str) -> str:
        return help_text(self.prefix, self.plain_messages)
