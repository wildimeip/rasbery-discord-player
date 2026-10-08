from types import SimpleNamespace

import pytest

from player.bot import MusicBot
from player.config import Settings
from tests.fakes import track


class FakeChannel:
    def __init__(self, cid=1, name="music", history=()):
        self.id = cid
        self.name = name
        self.sent = []
        self._history = list(history)

    def typing(self):
        return _Typing()

    async def send(self, text):
        self.sent.append(text)

    async def history(self, limit):
        for m in self._history[:limit]:
            yield m


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeMessage:
    def __init__(self, content, channel, bot=False, name="ann"):
        self.content = content
        self.channel = channel
        self.author = SimpleNamespace(bot=bot, display_name=name, name=name)
        self.replies = []

    async def reply(self, text, mention_author=False):
        self.replies.append(text)


def make_bot(player, search, **env):
    settings = Settings.from_env({"DISCORD_TOKEN": "t", **env})
    return MusicBot(settings, SimpleNamespace(), player, search)


@pytest.fixture
def bot(player, search):
    search.results = {"song one": [track(1)], "old song": [track(9)]}
    return make_bot(player, search)


async def test_answers_in_music_channel_only(bot, player):
    msg = FakeMessage("song one", FakeChannel())
    await bot.on_message(msg)
    assert msg.replies[0].startswith("Playing **Artist - Song 1")
    assert player.queue == [] and player.current.requested_by == "ann"

    other = FakeMessage("song one", FakeChannel(2, "general"))
    await bot.on_message(other)
    assert other.replies == []
    from_bot = FakeMessage("song one", FakeChannel(), bot=True)
    await bot.on_message(from_bot)
    assert from_bot.replies == []


async def test_channel_ids_win_over_name(player, search):
    bot = make_bot(player, search, DISCORD_CHANNEL_IDS="5")
    assert bot.is_music_channel(FakeChannel(5, "anything"))
    assert not bot.is_music_channel(FakeChannel(1, "music"))


async def test_announces_in_last_used_channel(bot):
    channel = FakeChannel()
    bot.announce_channel = channel
    await bot.announce("hello")
    assert channel.sent == ["hello"]


async def test_learns_history_once(bot, player, monkeypatch):
    import player.bot as bot_module

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bot_module.asyncio, "sleep", no_sleep)
    channel = FakeChannel(
        history=[
            FakeMessage("!p old song", None),
            FakeMessage("old song", None),
            FakeMessage("!skip", None),
            FakeMessage("Now playing", None, bot=True),
            FakeMessage("nothing matches", None),
        ]
    )
    await bot._learn_history([channel])
    assert player.store.count() == 1
    assert player.store.get_meta("history_scanned:1")
    channel._history.append(FakeMessage("song one", None))
    await bot._learn_history([channel])  # already scanned: no new songs
    assert player.store.count() == 1
