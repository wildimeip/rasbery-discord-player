"""Runs the real mpv (when installed) on a generated sound file, with no audio output."""

import asyncio
import shutil
import subprocess

import pytest

from player.mpv import Mpv

pytestmark = pytest.mark.skipif(
    not (shutil.which("mpv") and shutil.which("ffmpeg")), reason="needs mpv and ffmpeg"
)


@pytest.fixture
def tone(tmp_path):
    path = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(path)],
        check=True,
    )
    return path


async def test_plays_file_and_reports_end(tmp_path, tone):
    mpv = Mpv(tmp_path / "mpv.sock", extra_args=["--ao=null"])
    events = []
    ended = asyncio.Event()

    async def on_event(msg):
        events.append(msg)
        if msg.get("event") == "end-file":
            ended.set()

    mpv.on_event = on_event
    await mpv.start()
    try:
        await mpv.set_volume(50)
        await mpv.play(str(tone))
        await asyncio.wait_for(ended.wait(), 10)
        start = next(e for e in events if e["event"] == "start-file")
        end = next(e for e in events if e["event"] == "end-file")
        assert end["reason"] == "eof"
        assert end["playlist_entry_id"] == start["playlist_entry_id"]
        assert await mpv.position() is None  # idle again
        await mpv.play(str(tmp_path / "missing.wav"))
        ended.clear()
        await asyncio.wait_for(ended.wait(), 10)
        assert [e for e in events if e["event"] == "end-file"][-1]["reason"] == "error"
    finally:
        await mpv.close()
    assert not mpv.running
