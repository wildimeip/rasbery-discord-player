import asyncio

from player.player import MAX_FAILURES_IN_A_ROW
from tests.conftest import settle
from tests.fakes import track


async def test_first_song_plays_now_rest_queue(player, backend):
    r1 = await player.add([track(1)])
    r2 = await player.add([track(2), track(3)])
    assert r1.position == 0 and r2.position == 1
    assert backend.played == [track(1).video_id]
    assert player.current == track(1)
    assert [t.title for t in player.queue] == ["Song 2", "Song 3"]


async def test_song_end_plays_next_then_goes_idle(player, backend, said):
    await player.add([track(1), track(2)])
    await backend.finish()
    await settle()
    assert player.current == track(2)
    await backend.finish()
    await settle()
    assert player.current is None
    assert player.last == track(2)
    assert backend.played == [track(1).video_id, track(2).video_id]
    assert said[0].startswith("Now playing: **Artist - Song 1")


async def test_stale_end_event_after_skip_is_ignored(player, backend):
    await player.add([track(1), track(2), track(3)])
    stale_id = backend.entry_id
    await player.skip()
    # Song 1's natural end arrives after the skip: must not skip song 2 too.
    await backend.on_event({"event": "end-file", "reason": "eof", "playlist_entry_id": stale_id})
    await backend.on_event({"event": "end-file", "reason": "stop", "playlist_entry_id": stale_id})
    await settle()
    assert player.current == track(2)


async def test_skip_last_song_stops(player, backend):
    await player.add([track(1)])
    assert await player.skip() is None
    assert ("stop",) in backend.calls
    assert player.current is None


async def test_limits(player):
    result = await player.add([track("long", duration=601)] + [track(i) for i in range(10)])
    assert [t.title for t in result.too_long] == ["Song long"]
    # 1 plays right away, 5 fit in the queue
    assert len(result.added) == 6
    assert result.queue_full == 4


async def test_error_skips_and_gives_up_after_repeated_failures(player, backend, said):
    await player.add([track(i) for i in range(1, 6)])
    player.random_mode = True
    await player.add([])
    for _ in range(MAX_FAILURES_IN_A_ROW):
        await backend.finish("error", file_error="loading failed")
        await settle()
    assert player.current is None
    assert not player.random_mode
    assert "failed in a row" in said[-1]


async def test_random_mode_uses_history_and_avoids_repeats(player, backend, store):
    for i in range(1, 4):
        store.record_request(track(i))
    started = await player.set_random(True)
    assert started is not None and started.requested_by == "random"
    seen = [player.current.video_id]
    for _ in range(2):
        await backend.finish()
        await settle()
        seen.append(player.current.video_id)
    assert len(set(seen)) == 3  # all three before any repeat


async def test_random_falls_back_to_radio(player, backend, search):
    search.radio_tracks = [track("r1"), track("r2")]
    await player.add([track(1)])
    store_ids = {t.video_id for t in player.store.random_tracks(10)}
    assert store_ids == {track(1).video_id}
    result = await player.add_random(2)
    assert [t.title for t in result.added] == ["Song r1", "Song r2"]
    assert all(t.requested_by == "random" for t in result.added)


async def test_random_without_any_songs(player):
    assert await player.set_random(True) is None
    assert not player.random_mode


async def test_random_playlist(backend, search, store):
    from player.player import Player

    search.playlist_tracks = [track("p1"), track("p2")]
    player = Player(backend, search, store, random_playlist_id="PL123")
    result = await player.add_random(2)
    assert {t.title for t in result.added} == {"Song p1", "Song p2"}


async def test_stop_pause_resume_shuffle_remove(player, backend):
    await player.add([track(i) for i in range(1, 5)])
    assert await player.pause() and player.paused
    assert await player.resume() and not player.paused
    assert player.shuffle() == 3
    assert player.remove(9) is None
    assert player.remove(1) is not None and len(player.queue) == 2
    await player.stop()
    assert player.current is None and player.queue == []
    assert not await player.pause()


async def test_backend_restart_moves_on(player, backend):
    await player.add([track(1), track(2)])
    await player.backend_restarted()
    assert player.current == track(2)


async def test_random_plays_songs_like_the_last_request(backend, search, store):
    from player.player import SIMILAR_RUN, Player

    for i in range(1, 4):
        store.record_request(track(i))
    asked: list[str] = []

    def radio(video_id, limit=25):
        asked.append(video_id)
        return [track(f"{video_id[-1]}r{n}") for n in range(10)]

    search.radio = radio
    player = Player(backend, search, store, similar_percent=100)
    await player.add([track(9)])
    result = await player.add_random(SIMILAR_RUN + 2)
    titles = [t.title for t in result.added]
    assert asked[0] == track(9).video_id  # the first run follows the song just asked for
    assert all(t.startswith("Song 9r") for t in titles[:SIMILAR_RUN])
    assert len(asked) == 2 and asked[1] != track(9).video_id  # then a seed from the history
    assert len(set(titles)) == len(titles)


async def test_similar_off_uses_history(backend, search, store):
    from player.player import Player

    store.record_request(track(1))
    search.radio_tracks = [track("r1")]
    player = Player(backend, search, store, similar_percent=0)
    result = await player.add_random(1)
    assert [t.title for t in result.added] == ["Song 1"]


async def test_ban_skips_and_never_plays_again(player, backend, search, store):
    await player.add([track(1), track(2), track(1)])
    assert await player.ban(track(1), "ann")
    assert player.current.video_id == track(2).video_id and player.queue == []
    assert not await player.ban(track(5), "ann")  # not playing: just remembered
    result = await player.add([track(1)])
    assert result.added == [] and result.banned == [track(1)]
    search.radio_tracks = [track(1), track(5), track(6)]
    picks = await player.add_random(3)
    titles = [t.title for t in picks.added]
    assert "Song 6" in titles and "Song 1" not in titles and "Song 5" not in titles


def fetcher(failing: dict[str, str]):
    """A fetch that 'downloads' to /tmp/<id>.webm, or fails with the given reason."""
    from player.fetch import FetchError

    async def fetch(t):
        if t.video_id in failing:
            raise FetchError(failing[t.video_id])
        return f"/tmp/audio/{t.video_id}.webm"

    return fetch


async def test_plays_downloaded_file_and_skips_failed_downloads(backend, search, store, said):
    from player.player import Player

    async def announce(text):
        said.append(text)

    blocked = {track(2).video_id: "This video is not available"}
    player = Player(backend, search, store, announce=announce, fetch=fetcher(blocked))
    await player.add([track(1), track(2), track(3)])
    assert backend.calls[0] == ("play", f"/tmp/audio/{track(1).video_id}.webm")
    await backend.finish()
    await settle()
    assert player.current.video_id == track(3).video_id  # 2 was skipped
    assert any("Could not play **Artist - Song 2" in s for s in said)


async def test_blocked_random_songs_are_skipped_quietly(backend, search, store, said):
    from player.player import MAX_SKIPS_PER_PICK, Player

    async def announce(text):
        said.append(text)

    for i in range(1, 4):
        store.record_request(track(i))
    unavailable = {track(i).video_id: "This video is not available" for i in (1, 2)}
    player = Player(backend, search, store, announce=announce, fetch=fetcher(unavailable))
    started = await player.set_random(True)
    assert started.video_id == track(3).video_id
    assert not any("Could not play" in s for s in said)
    # Nothing playable at all: it stops after a bounded number of tries instead of looping.
    everything = {track(i).video_id: "This video is not available" for i in range(1, 4)}
    player = Player(backend, search, store, announce=announce, fetch=fetcher(everything))
    assert await player.set_random(True) is None and not player.random_mode
    assert MAX_SKIPS_PER_PICK >= 3


async def test_repeated_download_errors_give_up(backend, search, store, said):
    from player.player import MAX_FAILURES_IN_A_ROW, Player

    async def announce(text):
        said.append(text)

    tracks = [track(i) for i in range(1, MAX_FAILURES_IN_A_ROW + 2)]
    forbidden = {t.video_id: "HTTP Error 403: Forbidden" for t in tracks}
    player = Player(backend, search, store, announce=announce, fetch=fetcher(forbidden))
    await player.add(tracks)
    assert player.current is None and backend.calls == []
    assert sum("Could not play" in s for s in said) == MAX_FAILURES_IN_A_ROW
    assert "failed in a row" in said[-1]


def recording_fetcher(tmp_path, calls: list[str]):
    """A fetch that writes a real file and records which songs it downloaded."""

    async def fetch(t):
        calls.append(t.video_id)
        await asyncio.sleep(0)
        path = tmp_path / f"{t.video_id}.webm"
        path.write_bytes(b"audio")
        return str(path)

    return fetch


async def test_next_queued_song_is_downloaded_while_one_plays(backend, search, store, tmp_path):
    from player.player import Player

    calls: list[str] = []
    player = Player(backend, search, store, fetch=recording_fetcher(tmp_path, calls))
    await player.add([track(1)])
    await player.add([track(2)])
    await settle()
    assert calls == [track(1).video_id, track(2).video_id]  # 2 fetched during song 1
    await backend.finish()
    await settle()
    assert backend.calls[-1] == ("play", str(tmp_path / f"{track(2).video_id}.webm"))
    assert calls.count(track(2).video_id) == 1  # not downloaded twice


async def test_next_random_song_is_picked_and_downloaded_in_advance(
    backend, search, store, tmp_path
):
    from player.player import Player

    for i in range(1, 4):
        store.record_request(track(i))
    calls: list[str] = []
    player = Player(backend, search, store, fetch=recording_fetcher(tmp_path, calls))
    first = await player.set_random(True)
    await settle()
    upcoming = player._next_random
    assert upcoming is not None and upcoming.video_id != first.video_id
    assert calls == [first.video_id, upcoming.video_id]
    await backend.finish()
    await settle()
    assert player.current.video_id == upcoming.video_id
    assert calls.count(upcoming.video_id) == 1
