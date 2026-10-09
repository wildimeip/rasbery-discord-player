"""Entry point: python -m player"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

from .config import ConfigError, Settings

# Exit codes for problems only the owner can fix (config, token, Discord settings).
CONFIG_ERROR = 2
DISCORD_REFUSED = 3


def main() -> int:
    code = _run()
    if code in (CONFIG_ERROR, DISCORD_REFUSED):
        # Docker restarts the container right away; logging in to Discord every few seconds
        # can get the bot rate limited or its token reset. Wait before giving up instead.
        delay = int(os.environ.get("FATAL_RETRY_SECONDS", "300"))
        print(
            f"Trying again in {delay // 60} min (after fixing it, restart now with: "
            "sudo systemctl restart discord-player)",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
    return code


def _run() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    # It warns that voice is not installed: this bot plays on the Pi, not in voice channels.
    logging.getLogger("discord.client").setLevel(logging.ERROR)
    logging.info("rasbery-discord-player %s", os.environ.get("APP_VERSION", "dev"))
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return CONFIG_ERROR

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
            return DISCORD_REFUSED
        if isinstance(e, discord.PrivilegedIntentsRequired):
            print(
                "Turn on 'Message Content Intent' for the bot in the Discord Developer Portal "
                "(Bot -> Privileged Gateway Intents)",
                file=sys.stderr,
            )
            return DISCORD_REFUSED
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
