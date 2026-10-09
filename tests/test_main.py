import discord
import pytest

import player.__main__ as entry


@pytest.fixture
def slept(monkeypatch):
    calls = []
    monkeypatch.setattr(entry.time, "sleep", calls.append)
    monkeypatch.delenv("FATAL_RETRY_SECONDS", raising=False)
    return calls


def test_config_error_waits_before_exit(monkeypatch, slept, capsys):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN_FILE", "/nonexistent")
    assert entry.main() == entry.CONFIG_ERROR
    assert slept == [300]
    err = capsys.readouterr().err
    assert "No Discord bot token" in err and "Trying again in 5 min" in err


@pytest.mark.parametrize(
    "error, text",
    [
        (discord.PrivilegedIntentsRequired(None), "Message Content Intent"),
        (discord.LoginFailure("bad"), "rejected the bot token"),
    ],
)
def test_discord_refusal_waits_before_exit(monkeypatch, slept, capsys, error, text):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("FATAL_RETRY_SECONDS", "60")

    async def refuse(settings):
        raise error

    monkeypatch.setattr("player.bot.run", refuse)
    assert entry.main() == entry.DISCORD_REFUSED
    assert slept == [60]
    assert text in capsys.readouterr().err
