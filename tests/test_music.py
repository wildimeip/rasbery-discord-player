import pytest

from player.music import (
    MusicSearch,
    Track,
    clean_query,
    format_duration,
    parse_duration,
    parse_link,
)

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"https://www.youtube.com/watch?v={VID}", (VID, None)),
        (
            f"look https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID} nice",
            (VID, f"RDAMVM{VID}"),
        ),
        (f"https://youtu.be/{VID}?si=abc", (VID, None)),
        (f"https://m.youtube.com/shorts/{VID}", (VID, None)),
        ("https://music.youtube.com/playlist?list=PL123", (None, "PL123")),
        ("https://open.spotify.com/track/abc", (None, None)),
        ("just a song name", (None, None)),
        ("https://youtube.com/watch?v=short", (None, None)),
    ],
)
def test_parse_link(text, expected):
    assert parse_link(text) == expected


def test_clean_query():
    assert clean_query("<@123> play <https://youtu.be/x>  `now` <:smile:99>") == (
        "play https://youtu.be/x now"
    )


def test_durations():
    assert parse_duration("3:45") == 225
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("x") is None
    assert format_duration(225) == "3:45"
    assert format_duration(3723) == "1:02:03"
    assert format_duration(None) == "?"


class FakeYTMusic:
    def __init__(self):
        self.searches = []

    def search(self, query, filter, limit):
        self.searches.append(filter)
        if filter == "songs":
            return (
                [{"videoId": None, "title": "broken"}]
                if "cover" in query
                else [
                    {
                        "videoId": VID,
                        "title": "Get Lucky",
                        "duration_seconds": 248,
                        "artists": [{"name": "Daft Punk"}, {"name": "Pharrell Williams"}],
                    }
                ]
            )
        return [{"videoId": "abcdefghijk", "title": "Cover", "duration": "4:01", "artists": []}]

    def get_song(self, video_id):
        return {"videoDetails": {"title": "Linked", "author": "Someone", "lengthSeconds": "61"}}

    def get_playlist(self, playlist_id, limit):
        assert playlist_id == "PL1"
        return {
            "tracks": [
                {"videoId": VID, "title": "A", "length": "1:00", "artists": [{"name": "X"}]},
                {"videoId": "abcdefghijk", "title": "Gone", "isAvailable": False},
            ]
        }

    def get_watch_playlist(self, videoId, radio, limit):
        return {
            "tracks": [
                {"videoId": videoId, "title": "Seed"},
                {"videoId": "abcdefghijk", "title": "Next", "length": "2:00"},
            ]
        }


def test_search_prefers_songs_then_videos():
    search = MusicSearch(FakeYTMusic())
    assert search.resolve("get lucky") == [
        Track(VID, "Get Lucky", "Daft Punk, Pharrell Williams", 248)
    ]
    cover = search.resolve("some cover")
    assert cover == [Track("abcdefghijk", "Cover", "", 241)]


def test_resolve_links_and_playlists():
    search = MusicSearch(FakeYTMusic())
    assert search.resolve(f"<https://youtu.be/{VID}>") == [Track(VID, "Linked", "Someone", 61)]
    assert search.resolve("https://music.youtube.com/playlist?list=VLPL1") == [
        Track(VID, "A", "X", 60)
    ]
    assert search.resolve("https://open.spotify.com/track/abc") == []
    assert [t.title for t in search.radio(VID)] == ["Next"]


def test_track_label():
    assert Track(VID, "T", "A", 61).label == "A - T (1:01)"
    assert Track(VID, "T").label == "T"
