import asyncio

import pytest

from player.player import Player
from player.store import Store
from tests.fakes import FakeBackend, FakeSearch


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def search():
    return FakeSearch()


@pytest.fixture
def said():
    return []


@pytest.fixture
def player(backend, search, store, said):
    async def announce(text):
        said.append(text)

    return Player(backend, search, store, max_queue=5, max_song_seconds=600, announce=announce)


async def settle():
    """Lets tasks started by mpv events run."""
    for _ in range(5):
        await asyncio.sleep(0)
