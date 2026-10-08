import pytest

from player.config import ConfigError, Settings


def test_defaults_and_token_file(tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    s = Settings.from_env({"DISCORD_TOKEN_FILE": str(token)})
    assert s.discord_token == "abc"
    assert s.channel_ids == frozenset() and s.channel_name == "music"
    assert s.queue_plain_messages and s.volume == 70 and s.random_default


def test_values():
    s = Settings.from_env(
        {
            "DISCORD_TOKEN": "t",
            "DISCORD_CHANNEL_IDS": "1, 22",
            "DISCORD_CHANNEL_NAME": "#tunes",
            "QUEUE_PLAIN_MESSAGES": "no",
            "VOLUME": "40",
            "RANDOM_MODE": "false",
        }
    )
    assert s.channel_ids == {1, 22} and s.channel_name == "tunes"
    assert not s.queue_plain_messages and s.volume == 40 and not s.random_default


@pytest.mark.parametrize(
    "env",
    [
        {"DISCORD_TOKEN_FILE": "/nonexistent"},
        {"DISCORD_TOKEN": "t", "VOLUME": "loud"},
        {"DISCORD_TOKEN": "t", "VOLUME": "500"},
        {"DISCORD_TOKEN": "t", "DISCORD_CHANNEL_IDS": "music"},
    ],
)
def test_errors(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)
