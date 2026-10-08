# discord-yt-player

A Discord bot that plays music on a Raspberry Pi's speakers. Write a song name (or paste a
YouTube / YouTube Music link) in your `#music` channel: the bot finds it on YouTube Music and
plays it on the Pi. Songs queue up, and **random mode** keeps the music going with songs your
server has asked for before.

Runs as a Docker image (`ghcr.io/wildimeip/discord-yt-player`, arm64 + amd64). The Pi itself is
prepared by **[rasbery-setup](https://github.com/wildimeip/rasbery-setup)**, which installs
Docker, sets up the sound output and installs this player to `/opt/discord-player`.

## In Discord

Any message in the music channel is a song request (turn that off with
`QUEUE_PLAIN_MESSAGES=false`; then only `!play` works):

```
get lucky daft punk                         -> searches YouTube Music, plays or queues it
https://music.youtube.com/watch?v=...       -> that song
https://music.youtube.com/playlist?list=... -> the whole playlist
```

| Command | What it does |
| --- | --- |
| `!play <song or link>` (`!p`) | add a song, link or playlist |
| `!skip` (`!s`, `!next`) | next song |
| `!pause` / `!resume` | pause / continue |
| `!stop` | stop, empty the queue, random mode off |
| `!queue` (`!q`) | what plays now and next |
| `!np` | the current song and how far it is |
| `!random` | random mode on/off: when the queue runs out, random songs keep playing |
| `!random on` / `!random off` | the same, explicitly |
| `!random 10` | add 10 random songs to the queue |
| `!shuffle` | shuffle the queue |
| `!clear` | empty the queue (the current song keeps playing) |
| `!remove 3` | remove song 3 from the queue |
| `!volume 60` (`!vol`, `!vol +10`) | volume 0-130 |
| `!help` | the list |

### Where random songs come from
1. Every song anyone ever requested (saved in `data/player.db`). On its first start the bot also
   reads the last `HISTORY_SCAN_LIMIT` messages of the channel, so songs posted before it
   existed count too.
2. Optionally a YouTube Music playlist (`RANDOM_PLAYLIST_ID`).
3. When there is nothing new left, YouTube Music's radio for the last song.

Recently played songs are avoided until the pool runs out.

## Create the Discord bot (once)

1. https://discord.com/developers/applications -> **New Application**, name it.
2. **Bot** tab -> **Reset Token** -> copy the token (it goes in `secrets/discord_token` on
   the Pi). On the same tab switch on **Message Content Intent** and save.
3. **OAuth2** -> **URL Generator**: scope `bot`; permissions *View Channels*, *Send Messages*,
   *Read Message History*. Open the generated URL and add the bot to your
   server.
4. Create a text channel named `music` (or set `DISCORD_CHANNEL_NAME` / `DISCORD_CHANNEL_IDS`).

## Run on the Pi

Use rasbery-setup; it ends with these steps:

```sh
sudo nano /opt/discord-player/secrets/discord_token   # paste the token, one line
nano /opt/discord-player/.env                         # channel, sound output, volume
cd /opt/discord-player && docker compose up -d
docker compose logs -f
```

All settings are described in [`.env.example`](.env.example). The most important one is the
sound output, `MPV_AUDIO_DEVICE`:

| Output | Value |
| --- | --- |
| 3.5 mm headphone jack | `alsa/plughw:CARD=Headphones,DEV=0` |
| HDMI (TV / monitor) | `alsa/hdmi:CARD=vc4hdmi,DEV=0` |
| USB sound card | `alsa/plughw:CARD=<name>,DEV=0`, name from `aplay -l` |

List what mpv sees: `docker compose run --rm player mpv --audio-device=help`.
Test the speakers without Discord: `speaker-test -c2 -t wav -D plughw:CARD=Headphones` on the Pi.

### Troubleshooting
- **No sound:** check `MPV_AUDIO_DEVICE`, the volume on the Pi (`alsamixer`), and that the
  container sees the card: `docker compose exec player ls /dev/snd`.
- **"Turn on Message Content Intent"** in the logs: step 2 above.
- **Songs fail with "loading failed"**: YouTube changed something and yt-dlp needs an update.
  The image is rebuilt every week with the newest yt-dlp and the Pi pulls it every night; to
  update now: `docker compose pull && docker compose up -d`.
- **The bot ignores messages:** it only listens in the configured channel(s); see the
  `listening in [...]` line in the logs.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q && ruff check . && ruff format --check .
# run locally (needs mpv): DISCORD_TOKEN=... DATA_DIR=./data MPV_AUDIO_DEVICE= python -m player
```

Layout: `player/music.py` (YouTube Music search and links), `player/mpv.py` (mpv over its IPC
socket), `player/player.py` (queue and random mode), `player/commands.py` (chat commands),
`player/bot.py` (Discord), `player/store.py` (song history in SQLite).

Images: every push to `main` publishes `:stable`; a `v1.2.3` tag also publishes `:v1.2.3`, which
you can pin with `IMAGE_TAG` in `.env`. A weekly rebuild keeps yt-dlp current.
