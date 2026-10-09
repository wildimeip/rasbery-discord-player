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
