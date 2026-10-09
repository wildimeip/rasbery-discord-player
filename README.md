# rasbery-discord-player

A Discord bot that plays music on a Raspberry Pi's speakers, run entirely from Discord.
`!start` plays random songs; write a song name (or a YouTube Music id or link) in your `#music`
channel and the bot finds it on YouTube Music and adds it to the list of plays. If it can't
find the song, it answers and tags whoever asked. `!stop` stops.

Runs as a Docker image (`ghcr.io/wildimeip/rasbery-discord-player`, arm64 + amd64). The Pi itself is
prepared by **[rasbery-setup-player](https://github.com/wildimeip/rasbery-setup-player)**, which installs
Docker, sets up the sound output and installs this player to `/opt/discord-player`.

## In Discord

Any message in the music channel is a song request (turn that off with
`QUEUE_PLAIN_MESSAGES=false`; then only `!play` works):

```
get lucky daft punk                         -> searches YouTube Music, plays or queues it
5NV6Rdv1a3I                                 -> the song with that YouTube Music id
https://music.youtube.com/watch?v=...       -> that song
https://music.youtube.com/playlist?list=... -> the whole playlist (a bare PL... id works too)
```

Several matches for a name: the bot tags you and lists up to 5 (`SEARCH_CHOICES`) with number
buttons. Click one or type its number; with no answer in 60 s number 1 plays. None of them
right? Click **None of these** (or type `0`) for 3 more matches, as often as you like. A link or
id plays right away.

Write the band with the song (`kabát pivrnec`, `pivrnec kabát`): songs by that band are listed
first, ahead of covers and uploads by others. Accents and capitals don't matter.

Not found: the bot replies `@you Song not found on YouTube Music: <what you wrote>`.

| Command | What it does |
| --- | --- |
| `!start` | start playing: the queue first, then random songs (also resumes after `!pause`) |
| `!stop` | stop playing and empty the queue |
| `!play <song, id or link>` (`!p`) | add a song, link or playlist |
| `!skip` (`!s`, `!next`) | next song |
| `!pause` / `!resume` | pause / continue |
| `!queue` (`!q`) | what plays now and next |
| `!np` | the current song and how far it is |
| `!random` | random mode on/off: when the queue runs out, random songs keep playing (on by default, `RANDOM_MODE`) |
| `!random on` / `!random off` | the same, explicitly |
| `!random 10` | add 10 random songs to the queue |
| `!shuffle` | shuffle the queue |
| `!clear` | empty the queue (the current song keeps playing) |
| `!remove 3` | remove song 3 from the queue |
| `!volume 60` (`!vol`, `!vol +10`) | volume 0-130 |
| `!louder` / `!quieter` (`!up` / `!down`, `!+` / `!-`) | volume up / down by 10 (`!louder 20` for a bigger step) |
| `!ban` | never play the current song again (skips it) |
| `!ban <song>` | ban a song by name, id or link |
| `!banned` / `!unban 2` | list banned songs / allow number 2 again (`!unban <name>` works too) |
| `!help` | the list |

### Where random songs come from
1. **Songs like the ones you play** (`RANDOM_SIMILAR`, 70% of random songs by default):
   YouTube Music's radio for a song someone asked for. It starts from the last requested song,
   plays a few songs like it, then moves on to another song from the history, so random mode
   stays in the genres the channel listens to. No YouTube Music account is needed.
2. Every song anyone ever requested (saved in `data/player.db`). On its first start the bot also
   reads the last `HISTORY_SCAN_LIMIT` messages of the channel, so songs posted before it
   existed count too.
3. Optionally a YouTube Music playlist (`RANDOM_PLAYLIST_ID`).
4. When there is nothing new left, YouTube Music's radio for the last song.

Recently played songs are avoided until the pool runs out, and banned songs (`!ban`) never play,
not even when someone asks for them.

## Create the Discord bot (once)

1. https://discord.com/developers/applications -> **New Application**, name it.
2. **Bot** tab -> **Reset Token** -> copy the token (it goes in `secrets/discord_token` on
   the Pi). On the same tab switch on **Message Content Intent** and save.
3. **OAuth2** -> **URL Generator**: scope `bot`; permissions *View Channels*, *Send Messages*,
   *Read Message History*. Open the generated URL and add the bot to your
   server.
4. Create a text channel named `music` (or set `DISCORD_CHANNEL_NAME` / `DISCORD_CHANNEL_IDS`).

## Run on the Pi

Use [rasbery-setup-player](https://github.com/wildimeip/rasbery-setup-player): it prepares the
Pi, asks for the bot token and starts the player at every boot. By hand instead:

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
