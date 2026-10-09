"""Plays audio with an mpv process, controlled over its JSON IPC socket.

The player downloads each song first (see fetch.py), so mpv mostly plays local files; a URL
still works through mpv's yt-dlp hook.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


class MpvError(Exception):
    pass


class Mpv:
    def __init__(
        self,
        socket_path: Path,
        audio_device: str = "",
        volume: int = 70,
        extra_args: list[str] | None = None,
        binary: str = "mpv",
    ):
        self.socket_path = Path(socket_path)
        self.audio_device = audio_device
        self.volume = volume
        self.extra_args = extra_args or []
        self.binary = binary
        self.on_event: EventHandler | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0

    def args(self) -> list[str]:
        args = [
            self.binary,
            "--idle=yes",
            "--no-video",
            "--vo=null",
            # Keep mpv's warnings and errors (e.g. a sound card that won't open, or yt-dlp
            # failing) in the container logs; --no-terminal would hide them.
            "--input-terminal=no",
            "--quiet",
            "--msg-level=all=warn",
            f"--input-ipc-server={self.socket_path}",
            f"--volume={self.volume}",
            "--volume-max=130",
            "--ytdl-format=bestaudio/best",
            "--cache=yes",
            # Pi 3B has 1 GB: keep the read-ahead buffer small.
            "--demuxer-max-bytes=20MiB",
            "--demuxer-max-back-bytes=5MiB",
        ]
        if self.audio_device:
            args.append(f"--audio-device={self.audio_device}")
        return args + self.extra_args

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self, timeout: float = 15) -> None:
        self.socket_path.unlink(missing_ok=True)
        log.info("Starting %s", " ".join(self.args()))
        self.process = await asyncio.create_subprocess_exec(*self.args())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self.process.returncode is not None:
                raise MpvError(f"mpv exited with code {self.process.returncode} on start")
            try:
                reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if loop.time() > deadline:
                    await self.close()
                    raise MpvError("mpv did not open its IPC socket") from None
                await asyncio.sleep(0.1)
        self._reader_task = asyncio.create_task(self._read(reader))

    async def wait(self) -> int:
        """Waits until the mpv process exits; returns its exit code."""
        assert self.process is not None
        return await self.process.wait()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer:
            self._writer.close()
            self._writer = None
        if self.running:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self._fail_pending(MpvError("mpv closed"))

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg and "event" not in msg:
                    future = self._pending.pop(msg["request_id"], None)
                    if future and not future.done():
                        if msg.get("error") == "success":
                            future.set_result(msg.get("data"))
                        else:
                            future.set_exception(MpvError(msg.get("error", "unknown error")))
                elif "event" in msg and self.on_event:
                    try:
                        await self.on_event(msg)
                    except Exception:
                        log.exception("Error handling mpv event %s", msg)
        finally:
            self._fail_pending(MpvError("mpv connection lost"))

    async def command(self, *args, timeout: float = 10):
        if not self._writer:
            raise MpvError("mpv is not running")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"command": list(args), "request_id": request_id})
        self._writer.write(payload.encode() + b"\n")
        await self._writer.drain()
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def play(self, url: str) -> None:
        await self.command("loadfile", url, "replace")
        await self.set_pause(False)

    async def stop(self) -> None:
        await self.command("stop")

    async def set_pause(self, paused: bool) -> None:
        await self.command("set_property", "pause", paused)

    async def set_volume(self, volume: int) -> None:
        await self.command("set_property", "volume", volume)
        self.volume = volume

    async def position(self) -> float | None:
        try:
            return await self.command("get_property", "time-pos")
        except MpvError:
            return None  # nothing playing
