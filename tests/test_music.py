import pytest

from player.music import (
    MusicSearch,
    Track,
    artist_named,
    clean_query,
    format_duration,
    normalize,
    parse_duration,
    parse_link,
    rank,
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
        if video_id != VID:
            return {"playabilityStatus": {"status": "ERROR"}}
        return {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "videoId": VID,
                "title": "Linked",
                "author": "Someone",
                "lengthSeconds": "61",
            },
        }

    def get_playlist(self, playlist_id, limit):
        if not playlist_id.startswith("PL1"):
            raise Exception("404")
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


def test_bare_ids():
    search = MusicSearch(FakeYTMusic())
    assert search.resolve(VID) == [Track(VID, "Linked", "Someone", 61)]
    # 11 characters but not a known id: searched as a song name.
    assert search.resolve("Bohemian_Rh")[0].title == "Get Lucky"
    assert search.resolve(f"https://youtu.be/{'x' * 11}") == []
    assert [t.title for t in search.resolve("PL1abcdefghijk")] == ["A"]
    assert [t.title for t in search.resolve("PLunknownlist1")] == ["Get Lucky"]


class ManyResults(FakeYTMusic):
    def search(self, query, filter, limit):
        if filter == "songs":
            return [
                {"videoId": f"song{i:07}", "title": f"Hello {i}", "artists": []}
                for i in range(limit)
            ]
        raise AssertionError("enough songs: videos are not searched")


def test_choices_for_names_only():
    search = MusicSearch(ManyResults())
    tracks, alternatives = search.resolve_choices("hello", choices=5)
    assert alternatives and [t.title for t in tracks] == [f"Hello {i}" for i in range(5)]
    assert search.resolve_choices(VID, choices=5) == ([Track(VID, "Linked", "Someone", 61)], False)
    assert search.resolve("hello") == [tracks[0]]
    # Only one song and one video match: both are offered.
    tracks, alternatives = MusicSearch(FakeYTMusic()).resolve_choices("get lucky", choices=5)
    assert alternatives and [t.title for t in tracks] == ["Get Lucky", "Cover"]


def test_more_skips_the_ones_already_offered():
    search = MusicSearch(ManyResults())
    first, _ = search.resolve_choices("hello", choices=5)
    shown = [t.video_id for t in first]
    assert [t.title for t in search.more("hello", shown)] == ["Hello 5", "Hello 6", "Hello 7"]
    assert search.more("<https://youtu.be/x>", shown) == []


def test_normalize_and_artist_named():
    assert normalize("Kabát - Pivrnec!") == "kabat pivrnec"
    kabat = Track(VID, "Pivrnec", "Kabát", 200)
    assert artist_named(kabat, "kabat pivrnec")
    assert artist_named(kabat, "Pivrnec KABÁT")
    assert not artist_named(kabat, "kabaty pivrnec")  # whole words only
    assert not artist_named(Track(VID, "Pivrnec", "", 200), "pivrnec")


def test_rank_prefers_the_band_in_the_query():
    cover = Track("a" * 11, "Pivrnec (cover)", "Some Band", 200)
    karaoke = Track("b" * 11, "Pivrnec karaoke", "", 200)
    original = Track("c" * 11, "Pivrnec", "Kabát", 200)
    other = Track("d" * 11, "Kabát live 2010", "Fan Channel", 200)
    ranked = rank([cover, karaoke, other, original], "kabat pivrnec")
    assert ranked == [original, cover, karaoke, other]
    # No band named: YouTube Music's order stays, apart from matching words.
    assert rank([cover, karaoke], "pivrnec") == [cover, karaoke]


class BandBuriedInVideos(FakeYTMusic):
    """The band's song is not among the songs, only among the videos."""

    def search(self, query, filter, limit):
        if filter == "songs":
            return [{"videoId": "a" * 11, "title": "Pivrnec", "artists": [{"name": "Cover Kids"}]}]
        return [
            {"videoId": "b" * 11, "title": "Unrelated", "artists": [{"name": "X"}]},
            {"videoId": "c" * 11, "title": "Pivrnec (official)", "artists": [{"name": "Kabát"}]},
        ]


def test_band_match_found_among_videos():
    tracks, alternatives = MusicSearch(BandBuriedInVideos()).resolve_choices(
        "kabát pivrnec", choices=5
    )
    assert alternatives and [t.artist for t in tracks] == ["Kabát", "Cover Kids", "X"]
