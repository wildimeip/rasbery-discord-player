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
