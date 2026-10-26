import asyncio

import pytest

from player.radio import RadioSearch, is_radio
from tests.conftest import settle
from tests.fakes import track


def station(n, url=None):
    return {
        "stationuuid": f"u{n}",
        "name": f"  Station   {n} ",
        "url_resolved": url or f"http://s/{n}",
    }


def test_search_by_genre_then_by_name():
    asked = []

    def get_json(url):
        asked.append(url)
        if "tag=rock" in url:
            return [station(1), station(2, url="ftp://nope"), {"name": "no id"}]
        if "name=radio+beat" in url:
            return [station(3)]
        return []

    radio = RadioSearch(servers=("https://a",), get_json=get_json)
    rock = radio.search("Rock")
    assert [(t.video_id, t.title, t.url) for t in rock] == [("radio-u1", "Station 1", "http://s/1")]
    assert is_radio(rock[0]) and not is_radio(track(1)) and not is_radio(None)
    assert "order=clickcount" in asked[0] and "hidebroken=true" in asked[0]
    assert [t.title for t in radio.search("radio beat")] == ["Station 3"]


def test_next_server_when_one_is_down():
    def get_json(url):
        if url.startswith("https://down"):
            raise OSError("no route")
        return [station(1)]

    radio = RadioSearch(servers=("https://down", "https://up"), get_json=get_json)
    assert radio.pick("jazz").title == "Station 1"
    with pytest.raises(RuntimeError, match="not answering"):
        RadioSearch(servers=("https://down",), get_json=get_json).search("jazz")


class FakeRadio:
    def __init__(self):
        self.genres: list[str] = []
        self.n = 0

    def search(self, genre, limit=10):
        self.genres.append(genre)
        if genre == "nothing":
            return []
        self.n += 1
        return RadioSearch(get_json=lambda url: [station(self.n)], servers=("x",)).search(genre)

    def pick(self, genre, exclude=frozenset()):
        found = self.search(genre)
        return found[0] if found else None


@pytest.fixture
def radio_player(backend, search, store, said, monkeypatch):
    import player.player as pp
    from player.fetch import BotCheck
    from player.player import Player

    async def announce(text):
        said.append(text)

    state = {"blocked": True}

    async def fetch(t):
        if state["blocked"]:
            raise BotCheck("bot")
        return f"/tmp/{t.video_id}.webm"

    monkeypatch.setattr(pp, "BOT_CHECK_PAUSE", 0.05)
    player = Player(
        backend, search, store, announce=announce, fetch=fetch, radio=FakeRadio(), radio_genre="pop"
    )
    player.state = state
    return player


async def test_radio_plays_while_songs_cannot_then_songs_return(radio_player, backend, said):
    player = radio_player
    await player.add([track(1)])
    assert player.current.title == "Station 1" and backend.calls[-1] == ("play", "http://s/1")
    assert player.source() == "internet radio: Station 1 (pop)"
    assert said[0] == "Now playing internet radio (pop): **Station 1**"
    assert said[1].endswith("Internet radio plays meanwhile.")
    assert player.queue == [track(1)]  # the song waits for its turn

    player.state["blocked"] = False
    await asyncio.sleep(0.1)
    await settle()
    assert player.current == track(1)  # back to songs by itself
    assert backend.calls[-1] == ("play", f"/tmp/{track(1).video_id}.webm")


async def test_station_that_stops_is_replaced_while_waiting(radio_player, backend):
    player = radio_player
    player.random_mode = True
    await player.add([track(1)])
    await backend.finish("error", file_error="loading failed")
    for _ in range(200):  # the station is looked up in a thread
        await asyncio.sleep(0.005)
        if player.current.title != "Station 1":
            break
    assert player.current.title == "Station 2"
    assert await player.skip() == player.current and player.current.title == "Station 3"


async def test_radio_command_remembers_genre_and_off_returns_to_songs(
    radio_player, backend, store, search
):
    from player.commands import Commands

    player = radio_player
    player.state["blocked"] = False
    commands = Commands(player, search, "!", plain_messages=True)
    answer = await commands.handle("!radio Jazz", "ann")
    assert answer.startswith("Internet radio (jazz): **Station 2**")
    assert store.get_meta("radio_genre") == "jazz" and player.radio.genres[-1] == "jazz"
    assert (await commands.handle("!radio nothing", "ann")).startswith("No internet radio station")
    assert store.get_meta("radio_genre") == "jazz"
    await player.add([track(1)])  # a song request ends the radio
    assert player.current == track(1)
    await commands.handle("!radio", "ann")
    assert player.current.title.startswith("Station")
    player.queue.append(track(2))
    assert await commands.handle("!radio off", "ann") == "Back to songs: **Artist - Song 2 (3:20)**"


async def test_no_radio_without_radio_search(backend, search, store):
    from player.commands import Commands
    from player.player import Player

    player = Player(backend, search, store)
    commands = Commands(player, search, "!", plain_messages=True)
    assert await commands.handle("!radio", "ann") == "Internet radio is off."
