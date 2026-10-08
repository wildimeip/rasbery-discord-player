"""Entry point: python -m player"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .config import ConfigError, Settings


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    # It warns that voice is not installed: this bot plays on the Pi, not in voice channels.
    logging.getLogger("discord.client").setLevel(logging.ERROR)
    logging.info("discord-yt-player %s", os.environ.get("APP_VERSION", "dev"))
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    from .bot import run

    async def runner() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        # docker stop sends SIGTERM: shut down cleanly (logs out, stops mpv).
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await run(settings)
        except asyncio.CancelledError:
            logging.info("Shutting down")

    try:
        asyncio.run(runner())
    except Exception as e:
        import discord

        if isinstance(e, discord.LoginFailure):
            print("Discord rejected the bot token: check secrets/discord_token", file=sys.stderr)
            return 3
        if isinstance(e, discord.PrivilegedIntentsRequired):
            print(
                "Turn on 'Message Content Intent' for the bot in the Discord Developer Portal "
                "(Bot -> Privileged Gateway Intents)",
                file=sys.stderr,
            )
            return 3
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
