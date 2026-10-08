import pytest

from player.commands import Commands, parse_command
from tests.fakes import track


@pytest.fixture
def commands(player, search):
    search.results = {
        "song one": [track(1)],
        "song two": [track(2)],
        "!weird": [track(3)],
    }
    return Commands(player, search, "!", plain_messages=True)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("!p  song name ", ("play", "song name")),
        ("!SKIP", ("skip", "")),
        ("!vol 50", ("volume", "50")),
        ("!random 5", ("random", "5")),
        ("!unknown", None),
        ("!", None),
        ("hello", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text, "!") == expected


async def test_plain_message_queues_song(commands, player):
    assert (await commands.handle("song one", "ann")).startswith("Playing **Artist - Song 1")
    assert "(#1)" in await commands.handle("song two", "bob")
    assert player.queue[0].requested_by == "bob"
    assert await commands.handle("nope", "bob", "<@42>") == (
        "<@42> Song not found on YouTube Music: *nope*"
    )
    assert await commands.handle("!p nope", "bob") == "Song not found on YouTube Music: *nope*"
    # Unknown commands are not treated as songs.
    assert await commands.handle("!weird", "bob") is None


async def test_plain_messages_off(player, search):
    commands = Commands(player, search, "!", plain_messages=False)
    assert await commands.handle("song one", "ann") is None


async def test_queue_np_skip(commands):
    assert await commands.handle("!np", "a") == "Nothing is playing."
    await commands.handle("!play song one", "ann")
    await commands.handle("!play song two", "ann")
    assert "[0:12/3:20]" in await commands.handle("!np", "a")
    queue = await commands.handle("!q", "a")
    assert "`1.` Artist - Song 2" in queue
    assert (await commands.handle("!skip", "a")).startswith("Skipped. Now: **Artist - Song 2")
    assert await commands.handle("!skip", "a") == "Skipped. The queue is empty."


async def test_random_commands(commands, player):
    assert "No songs" in await commands.handle("!random", "a")
    await commands.handle("song one", "a")
    await commands.handle("song two", "a")
    await commands.handle("!stop", "a")
    assert (await commands.handle("!random on", "a")).startswith("Random mode on. Playing")
    assert "Random mode is on" in await commands.handle("!queue", "a")
    assert await commands.handle("!random off", "a") == "Random mode off."
    reply = await commands.handle("!random 1", "a")
    assert reply.startswith("Queued **") or reply.startswith("Queued 1")
    assert "Use `!random`" in await commands.handle("!random maybe", "a")


async def test_volume(commands, player):
    assert await commands.handle("!volume", "a") == "Volume: 70"
    assert await commands.handle("!vol 200", "a") == "Volume: 130"
    assert await commands.handle("!vol -30", "a") == "Volume: 100"
    assert await commands.handle("!vol loud", "a") == "`!volume 0-130`"


async def test_help(commands):
    text = await commands.handle("!help", "a")
    assert "`!random`" in text and text.startswith("Write a song name")


async def test_start_and_stop(commands, player):
    assert "No songs to pick from yet" in await commands.handle("!start", "a")
    await commands.handle("song one", "a")
    assert await commands.handle("!start", "a") == "Already playing."
    await commands.handle("!pause", "a")
    assert await commands.handle("!start", "a") == "Resumed."
    assert await commands.handle("!stop", "a") == "Stopped. `!start` plays again."
    assert player.current is None
    reply = await commands.handle("!start", "a")
    assert reply.startswith("Started (random songs). Playing **Artist - Song 1")
    assert player.random_mode
    # !stop does not turn random mode off; !start brings it back.
    await commands.handle("!stop", "a")
    assert player.random_mode and player.current is None
    await commands.handle("!p song two", "a")
    await commands.handle("!stop", "a")
    await commands.handle("!p song two", "a")
    assert player.current.title == "Song 2"
