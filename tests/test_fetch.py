import os
import time

import pytest
import yt_dlp

from player.fetch import Fetcher, FetchError, clean_error
from tests.fakes import track


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: "downloads" by writing a small file."""

    error: str | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        assert download and self.opts["format"] == "bestaudio/best"
        if self.error:
            raise yt_dlp.utils.DownloadError(self.error)
        video_id = url.rsplit("=", 1)[1]
        path = self.opts["outtmpl"].replace("%(id)s", video_id).replace("%(ext)s", "webm")
        with open(path, "wb") as f:
            f.write(b"audio")
        return {"id": video_id, "requested_downloads": [{"filepath": path}]}


@pytest.mark.parametrize(
    "message, expected",
    [
        (
            "ERROR: [youtube] zr-QdCcG3w8: This video is not available",
            "This video is not available",
        ),
        (
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "unable to download video data: HTTP Error 403: Forbidden",
        ),
        ("", "download failed"),
    ],
)
def test_clean_error(message, expected):
    assert clean_error(message) == expected


async def test_downloads_and_keeps_only_recent_files(tmp_path):
    (tmp_path / "left-over.webm").write_bytes(b"x")
    fetch = Fetcher(tmp_path, keep=2, ydl_factory=FakeYDL)
    assert list(tmp_path.iterdir()) == []  # cleaned on start
    paths = []
    for n in range(1, 4):
        paths.append(await fetch(track(n)))
        os.utime(paths[-1], (time.time() + n, time.time() + n))
    assert paths[0].endswith(f"{track(1).video_id}.webm")
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"{track(n).video_id}.webm" for n in (2, 3)
    )


async def test_error_becomes_fetch_error(tmp_path):
    class Blocked(FakeYDL):
        error = "ERROR: [youtube] zr-QdCcG3w8: This video is not available"

    fetch = Fetcher(tmp_path, ydl_factory=Blocked)
    with pytest.raises(FetchError, match="^This video is not available$"):
        await fetch(track(1))


async def test_bot_check_and_cookies(tmp_path):
    from player.fetch import BotCheck

    class Flagged(FakeYDL):
        error = (
            "ERROR: [youtube] aJ9usrpAPao: Sign in to confirm you’re not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )

    with pytest.raises(BotCheck):
        await Fetcher(tmp_path / "a", soundcloud=False, ydl_factory=Flagged)(track(1))

    assert "cookiefile" not in Fetcher(tmp_path / "b", cookies_file=tmp_path / "none").options()
    empty = tmp_path / "empty"
    empty.write_text("")
    assert "cookiefile" not in Fetcher(tmp_path / "c", cookies_file=empty).options()
    cookies = tmp_path / "cookies"
    cookies.write_text(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n")
    fetch = Fetcher(tmp_path / "d", cookies_file=cookies, ydl_factory=FakeYDL)
    copy = fetch.options()["cookiefile"]
    assert copy != str(cookies) and open(copy).read() == cookies.read_text()
    for n in range(1, 6):  # downloads never delete the cookies copy
        await fetch(track(n))
    assert open(copy).read() == cookies.read_text()


def test_clean_error_drops_cookie_advice():
    message = "ERROR: [youtube] abcdefghijk: Video unavailable. Use --cookies for it"
    assert clean_error(message) == "Video unavailable"


def test_soundcloud_match():
    from player.fetch import soundcloud_match
    from player.music import Track

    song = Track("x" * 11, "I'm Gonna Be (500 Miles) (Official Video)", "The Proclaimers", 219, "a")
    good = {"title": "I'm Gonna Be (500 Miles)", "uploader": "The Proclaimers", "duration": 221}
    assert soundcloud_match(song, good)
    assert not soundcloud_match(song, {**good, "duration": 30})  # a preview
    assert not soundcloud_match(song, {**good, "duration": 400})  # a mix or a cover
    assert not soundcloud_match(song, {**good, "title": "Letter from America"})


class Blocking(FakeYDL):
    """YouTube asks for the bot check; SoundCloud search finds the song."""

    searches: list[str] = []

    def extract_info(self, url, download):
        if "youtube.com" in url:
            raise yt_dlp.utils.DownloadError(
                "ERROR: [youtube] x: Sign in to confirm you're not a bot"
            )
        if url.startswith("scsearch"):
            Blocking.searches.append(url)
            assert not download
            return {
                "entries": [
                    {"title": "Something else", "duration": 200, "webpage_url": "sc://other"},
                    {
                        "title": "Song 1",
                        "uploader": "Artist",
                        "duration": 205,
                        "webpage_url": "https://soundcloud.com/artist/song-1",
                    },
                ]
            }
        assert download and url == "https://soundcloud.com/artist/song-1"
        path = self.opts["outtmpl"].replace("%(id)s", "123").replace("%(ext)s", "mp3")
        open(path, "wb").write(b"audio")
        return {"requested_downloads": [{"filepath": path}]}


async def test_soundcloud_fallback_and_youtube_pause(tmp_path, monkeypatch):
    from player.fetch import is_soundcloud

    Blocking.searches = []
    tries: list[str] = []
    fetch = Fetcher(tmp_path, ydl_factory=Blocking)
    original = fetch._extract
    monkeypatch.setattr(
        fetch, "_extract", lambda url, *a, **k: tries.append(url) or original(url, *a, **k)
    )
    path = await fetch(track(1))
    assert is_soundcloud(path) and open(path, "rb").read() == b"audio"
    assert fetch.youtube_blocked
    tries.clear()
    await fetch(track(1))
    assert not any("youtube.com" in url for url in tries)  # YouTube is left alone for a while
    assert Blocking.searches == ["scsearch5:Artist Song 1"] * 2


async def test_not_on_soundcloud(tmp_path):
    class Nothing(Blocking):
        def extract_info(self, url, download):
            if url.startswith("scsearch"):
                return {"entries": []}
            return super().extract_info(url, download)

    with pytest.raises(FetchError, match="isn't on SoundCloud"):
        await Fetcher(tmp_path, ydl_factory=Nothing)(track(1))


def test_pot_provider_option(tmp_path):
    assert "extractor_args" not in Fetcher(tmp_path / "a").options()
    options = Fetcher(tmp_path / "b", pot_provider_url="http://pot-provider:4416").options()
    assert options["extractor_args"] == {
        "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]}
    }
