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
        self.author = SimpleNamespace(bot=bot, display_name=name, name=name, mention="<@7>", id=7)
        self.replies = []

    async def reply(self, text, mention_author=False, view=None):
        self.replies.append(text)
        self.view = view
        return Sent()


class Sent:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None, view="unchanged"):
        self.edits.append((content, view))


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
    missing = FakeMessage("no such song", FakeChannel())
    await bot.on_message(missing)
    assert missing.replies == ["<@7> Song not found on YouTube Music: *no such song*"]

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


async def test_choice_buttons_and_typed_answer(bot, player, search):
    search.matches = {"hello": [track("a"), track("b")]}
    ask = FakeMessage("hello", FakeChannel())
    await bot.on_message(ask)
    assert ask.replies[0].startswith("<@7> I found more than one")
    assert [b.label for b in ask.view.children] == ["1", "2", "None of these"]
    choice = next(iter(bot.choice_messages))
    sent = bot.choice_messages[choice]
    answer = FakeMessage("2", FakeChannel())
    await bot.on_message(answer)
    assert answer.replies[0].startswith("Playing **Artist - Song b")
    assert sent.edits == [(None, None)]  # buttons removed
    assert bot.choice_messages == {}


async def test_choice_timeout_plays_first(bot, player, search):
    search.matches = {"hello": [track("a"), track("b")]}
    ask = FakeMessage("hello", FakeChannel())
    await bot.on_message(ask)
    sent = next(iter(bot.choice_messages.values()))
    await ask.view.on_timeout()
    assert player.current.title == "Song a"
    assert sent.edits[0][0].startswith("No answer, so: Playing **Artist - Song a")


class FakeInteraction:
    def __init__(self, user_id=7):
        self.user = SimpleNamespace(id=user_id)
        self.message = Sent()
        self.response = SimpleNamespace(
            defer=self._defer, send_message=self._send, edit_message=self.edit_original_response
        )
        self.edits = []
        self.sent = []

    async def _defer(self):
        pass

    async def _send(self, text, ephemeral=False):
        self.sent.append(text)

    async def edit_original_response(self, content=None, view=None):
        self.edits.append((content, view))


async def test_none_of_these_button_shows_more(bot, player, search):
    search.matches = {"hello": [track(c) for c in "abcdefg"]}
    bot.commands.choices = 2
    ask = FakeMessage("hello", FakeChannel())
    await bot.on_message(ask)
    none_button = ask.view.children[-1]
    stranger = FakeInteraction(user_id=8)
    await none_button.callback(stranger)
    assert stranger.sent and not stranger.edits
    click = FakeInteraction()
    await none_button.callback(click)
    content, view = click.edits[0]
    assert content.startswith("<@7> More matches.") and "Song e" in content
    assert [b.label for b in view.children] == ["1", "2", "3", "None of these"]
    assert list(bot.choice_messages.values()) == [click.message]
    await ask.view.on_timeout()  # the old list's timer does nothing any more
    assert player.current is None
    pick = FakeInteraction()
    await view.children[2].callback(pick)
    assert player.current.title == "Song e" and pick.edits[0][0].startswith("Playing")


async def test_typed_zero_shows_more(bot, player, search):
    search.matches = {"hello": [track(c) for c in "abc"]}
    bot.commands.choices = 2
    ask = FakeMessage("hello", FakeChannel())
    await bot.on_message(ask)
    sent = next(iter(bot.choice_messages.values()))
    zero = FakeMessage("0", FakeChannel())
    await bot.on_message(zero)
    assert sent.edits == [(None, None)]
    assert (
        zero.replies[0].startswith("<@7> More matches.")
        and "`1.` Artist - Song c" in zero.replies[0]
    )
    nothing = FakeMessage("0", FakeChannel())
    await bot.on_message(nothing)
    assert nothing.replies[0].startswith("<@7> No more matches for *hello*")
    assert bot.commands.pending == {} and bot.choice_messages == {}
