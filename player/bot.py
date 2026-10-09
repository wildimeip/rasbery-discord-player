"""The Discord side: reads messages in the music channel(s) and answers them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

import discord

from .commands import Choice, Commands, parse_command
from .config import Settings
from .fetch import Fetcher
from .mpv import Mpv
from .music import MusicSearch
from .player import Player
from .store import Store

log = logging.getLogger(__name__)

HEARTBEAT_FILE = Path("/tmp/heartbeat")


class MusicBot(discord.Client):
    def __init__(self, settings: Settings, mpv: Mpv, player: Player, search: MusicSearch):
        intents = discord.Intents.default()
        intents.message_content = True  # must also be switched on in the Developer Portal
        super().__init__(intents=intents)
        self.settings = settings
        self.mpv = mpv
        self.player = player
        self.search = search
        self.store = player.store
        self.commands = Commands(
            player,
            search,
            settings.command_prefix,
            settings.queue_plain_messages,
            settings.choices,
        )
        self.choice_messages: dict[Choice, discord.Message] = {}
        self.announce_channel: discord.abc.Messageable | None = None
        self._background: set[asyncio.Task] = set()
        if settings.announce:
            player.announce = self.announce

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def setup_hook(self) -> None:
        await self.mpv.start()
        self._spawn(self._watch_mpv())
        self._spawn(self._heartbeat())

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await self.mpv.close()
        await super().close()

    def is_music_channel(self, channel) -> bool:
        if self.settings.channel_ids:
            return channel.id in self.settings.channel_ids
        return getattr(channel, "name", None) == self.settings.channel_name

    def music_channels(self) -> list[discord.TextChannel]:
        return [
            ch for guild in self.guilds for ch in guild.text_channels if self.is_music_channel(ch)
        ]

    async def on_ready(self) -> None:
        channels = self.music_channels()
        log.info("Logged in as %s; listening in %s", self.user, [f"#{c.name}" for c in channels])
        if not channels:
            where = (
                f"channel ids {sorted(self.settings.channel_ids)}"
                if self.settings.channel_ids
                else f"a channel named #{self.settings.channel_name}"
            )
            log.warning("No music channel found: invite the bot to a server with %s", where)
        if self.announce_channel is None and channels:
            self.announce_channel = channels[0]
        if self.settings.history_scan_limit:
            self._spawn(self._learn_history(channels))

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self.is_music_channel(message.channel):
            return
        text = message.content
        if not text.strip():
            return
        self.announce_channel = message.channel
        author = getattr(message.author, "display_name", None) or message.author.name
        mention = getattr(message.author, "mention", "")
        user = str(message.author.id)
        open_choice = self.commands.pending.get(user)
        try:
            async with message.channel.typing():
                answer = await self.commands.handle(text, author, mention, user)
        except Exception:
            log.exception("Command failed: %r", text)
            answer = "Something went wrong, see the logs on the Pi."
        if open_choice and self.commands.pending.get(user) is not open_choice:
            # Answered by typing a number, or replaced by a new request: remove its buttons.
            await self._close_choice(open_choice)
        if isinstance(answer, Choice):
            view = ChoiceView(self, answer, self.settings.choice_timeout)
            prompt = answer.prompt(self.settings.choice_timeout)
            self.choice_messages[answer] = await message.reply(prompt, view=view)
        elif answer:
            await message.reply(answer[:2000], mention_author=False)

    async def _close_choice(self, choice: Choice, text: str | None = None) -> None:
        sent = self.choice_messages.pop(choice, None)
        if sent is not None:
            with contextlib.suppress(discord.HTTPException):
                if text:
                    await sent.edit(content=text, view=None)
                else:
                    await sent.edit(view=None)

    async def announce(self, text: str) -> None:
        if self.announce_channel is not None:
            await self.announce_channel.send(text[:2000])

    async def _watch_mpv(self) -> None:
        """Starts mpv again whenever it exits."""
        while True:
            code = await self.mpv.wait()
            log.error("mpv exited with code %s; restarting", code)
            await self.mpv.close()
            await asyncio.sleep(2)
            try:
                await self.mpv.start()
            except Exception:
                log.exception("Could not restart mpv; trying again in 30 s")
                await asyncio.sleep(30)
                continue
            await self.player.backend_restarted()

    async def _heartbeat(self) -> None:
        """Touches a file the Docker health check looks at while everything is connected."""
        while True:
            if self.is_ready() and not self.is_closed() and self.mpv.running:
                with contextlib.suppress(OSError):
                    HEARTBEAT_FILE.write_text(str(int(time.time())))
            await asyncio.sleep(30)

    async def _learn_history(self, channels: list[discord.TextChannel]) -> None:
        """Once per channel: adds songs requested before the bot existed to the random pool."""
        prefix = self.settings.command_prefix
        for channel in channels:
            key = f"history_scanned:{channel.id}"
            if self.store.get_meta(key):
                continue
            queries: list[str] = []
            try:
                async for message in channel.history(limit=self.settings.history_scan_limit):
                    if message.author.bot or not message.content.strip():
                        continue
                    parsed = parse_command(message.content, prefix)
                    if parsed and parsed[0] == "play" and parsed[1]:
                        queries.append(parsed[1])
                    elif parsed is None and self.settings.queue_plain_messages:
                        if not message.content.strip().startswith(prefix):
                            queries.append(message.content)
            except discord.Forbidden:
                log.warning("No 'Read Message History' permission in #%s", channel.name)
                continue
            queries = list(dict.fromkeys(q.strip() for q in queries))
            log.info("Learning %d past requests from #%s", len(queries), channel.name)
            learned = 0
            for query in queries:
                try:
                    tracks = await asyncio.to_thread(self.search.resolve, query, 50)
                except Exception as e:
                    log.debug("History lookup failed for %r: %s", query, e)
                    tracks = []
                for track in tracks:
                    self.store.record_request(track.by("history"))
                    learned += 1
                await asyncio.sleep(1)  # be gentle with YouTube Music
            self.store.set_meta(key, str(int(time.time())))
            log.info("Learned %d songs from #%s history", learned, channel.name)


class ChoiceView(discord.ui.View):
    """Number buttons under a "which one?" message; only the person who asked can pick."""

    def __init__(self, bot: MusicBot, choice: Choice, timeout: int):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.choice = choice
        for i in range(len(choice.tracks)):
            button = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)
            button.callback = self._picker(i)
            self.add_item(button)

    def _picker(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if str(interaction.user.id) != self.choice.user:
                await interaction.response.send_message(
                    "Only the person who asked can pick. Send your own song name.",
                    ephemeral=True,
                )
                return
            answer = await self.bot.commands.pick(self.choice, index)
            self.bot.choice_messages.pop(self.choice, None)
            self.stop()
            await interaction.response.edit_message(content=answer, view=None)

        return callback

    async def on_timeout(self) -> None:
        if self.bot.commands.pending.get(self.choice.user) is self.choice:
            answer = await self.bot.commands.pick(self.choice, 0)
            await self.bot._close_choice(self.choice, f"No answer, so: {answer}")
        else:
            await self.bot._close_choice(self.choice)


async def run(settings: Settings) -> None:
    store = Store(settings.data_dir / "player.db")
    search = MusicSearch()
    mpv = Mpv(settings.mpv_socket, settings.audio_device, settings.volume)
    player = Player(
        mpv,
        search,
        store,
        max_queue=settings.max_queue,
        max_song_seconds=settings.max_song_minutes * 60,
        random_playlist_id=settings.random_playlist_id,
        random_mode=settings.random_default,
        similar_percent=settings.random_similar,
        fetch=Fetcher(settings.audio_cache_dir),
    )
    bot = MusicBot(settings, mpv, player, search)
    try:
        async with bot:
            await bot.start(settings.discord_token)
    finally:
        store.close()
