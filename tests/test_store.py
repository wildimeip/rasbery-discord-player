from player.store import Store
from tests.fakes import track


def test_requests_and_plays(tmp_path):
    store = Store(tmp_path / "sub" / "player.db")
    store.record_request(track(1))
    store.record_request(track(1, duration=None))
    store.record_play(track(1).video_id)
    row = store.db.execute("SELECT * FROM songs").fetchone()
    assert (row["request_count"], row["play_count"], row["duration"]) == (2, 1, 200)
    assert store.count() == 1
    store.close()


def test_random_prefers_not_excluded(store):
    for i in range(5):
        store.record_request(track(i))
    exclude = {track(i).video_id for i in range(4)}
    for _ in range(10):
        assert store.random_tracks(1, exclude)[0] == track(4, by="random")
    assert len(store.random_tracks(10)) == 5


def test_meta(store):
    assert store.get_meta("a") is None
    store.set_meta("a", "1")
    store.set_meta("a", "2")
    assert store.get_meta("a") == "2"
