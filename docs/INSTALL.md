# Installation guide

## 1. Install the system dependencies

Myne requires Python 3.12 or later and an `mpv` executable on PATH.
For full YouTube extractor support, install a supported Deno or Node.js runtime
as described in the [yt-dlp runtime guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
Myne enables Deno and Node detection and includes yt-dlp's default Python extras,
including its JavaScript solver package. Local music needs no JavaScript runtime.

On Debian/Ubuntu versions that offer Python 3.12+:

```sh
sudo apt update
sudo apt install python3 python3-venv mpv
python3 --version
```

On Fedora:

```sh
sudo dnf install python3 mpv
```

On macOS with Homebrew:

```sh
brew install python mpv
```

If your distribution's Python is older than 3.12, install a newer Python using
its supported package or Python version manager and use that interpreter below.
Native Windows playback is not supported. WSL can run Myne when Linux audio and
mpv are working. Search and queue functionality do not require a sound device.

## 2. Install Myne

From this source directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
myne doctor
```

Or install the checked-out project with `pipx install .` if you use pipx. The
executable is `myne`; no package has to be published to PyPI to install from
source. For development, use `python -m pip install -e '.[dev]'`.

## 3. First playback

```sh
myne search "Linkin Park"
myne play "Numb"
myne now
myne volume 60
myne queue add "Faint"
myne ui
```

`myne play` starts the detached service on demand. The service owns mpv and
continues until `myne shutdown`, logout or system shutdown. Closing the TUI does
not stop music. The service is a per-user process, not a system service or
login-startup installation.

## Lyrics and visualizer

```sh
myne play "Linkin Park Numb"
myne ui                   # live meters; l toggles lyrics, v toggles the visualizer
myne lyrics               # plain lyrics with their source
myne lyrics --lrc          # timestamped LRC when available
myne lyrics --refresh      # bypass the cache and look up this track again
myne visualize            # standalone audio meter; Ctrl+C closes the view
myne visualize --fps 60   # local rendering rate; audio polling stays bounded
myne visualize --no-animation
```

LRCLIB supplies plain/timed lyrics without a key. Myne sends the current song's
title, artist, album and duration to its public API, validates the match and
caches successful lookups locally for seven days. A missing match falls back to
the music provider. Missing lyrics are cached briefly; `--refresh` bypasses the
cache. Songs without available lyrics show an explicit empty/instrumental state.
The lyrics panel follows playback time, including backward seeking and pausing.

The visualizer uses mpv's FFmpeg `astats` audio filter. Standard Debian/Fedora/
Homebrew mpv packages generally provide it. There is no microphone capture,
audio upload, extra player process or full-song download. Meters show decoded
sample levels before system output mixing. If analysis is unavailable, audio
continues normally and the UI explains why the meters are quiet.

The TUI renders at 30 FPS and the standalone visualizer defaults to the same
rate, independently of its audio polling. Meters ease between actual measured
samples, retain peaks briefly, and clear on pause, buffering or stale data. Set
`MYNE_REDUCED_MOTION=1` to use static measured updates and disable decorative
transitions. `TEXTUAL_ANIMATIONS=none` and `TERM=dumb` also reduce motion;
`NO_COLOR` controls color without changing animation preferences.

After upgrading Myne, restart the existing player with `myne shutdown`, then
`myne play`. The background service must reload the installed code for updated
UI controls and playback fixes to take effect.

## Optional YouTube authentication

Anonymous search and playback are attempted by default. Account/region checks
or upstream restrictions may require authentication. Myne accepts a
`ytmusicapi` browser-auth JSON file for metadata and a Netscape-format cookies
file for `yt-dlp` stream extraction. They are separate integrations; configuring
one does not automatically authenticate the other.

Use the setup procedure in the installed version's
[ytmusicapi authentication guide](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html)
and [yt-dlp cookie instructions](https://github.com/yt-dlp/yt-dlp#authentication-options).
Keep auth files outside the repository and restrict access to your account:

```sh
export MYNE_AUTH_FILE="/private/path/browser.json"
export MYNE_COOKIES_FILE="/private/path/cookies.txt"
chmod 600 "$MYNE_AUTH_FILE" "$MYNE_COOKIES_FILE"
myne shutdown  # if a service is already running
myne play "Numb"
```

Myne does not print cookie contents or persist extracted signed stream URLs.
YouTube restrictions may also require a supported JavaScript runtime or an
updated extractor; see the installed
[yt-dlp requirements](https://github.com/yt-dlp/yt-dlp#dependencies). Install
optional extractor components only when your environment needs them. Myne does
not bypass account permissions.

## Local audio

An explicit local path can be played with the default provider:

```sh
myne play "/path/to/Music/song.flac"
```

For filename-based library search:

```sh
export MYNE_LIBRARY="/path/to/Music"
myne --provider local search "piano"
myne --provider local queue add "/path/to/Music/song.flac"
myne play
```

The local provider currently derives titles from filenames; it does not read
embedded artist, album, duration or genre tags. YouTube albums/artists/radio and
lyrics are provider-specific capabilities.

## AI Smart Mix

The default `myne mix "focus"` uses provider searches based on the mood. It does
not claim to be AI-generated. To use a model, configure a service that supports
the chat-completions JSON request format and JSON-object responses:

```sh
export MYNE_MIX_ENDPOINT="https://your-ai-service.example/v1/chat/completions"
export MYNE_MIX_MODEL="your-model-name"
# Set MYNE_MIX_API_KEY through your shell's secret manager when required.
myne mix "night drive" --ai --limit 15 --play
```

Use the complete endpoint URL. HTTPS is required except for localhost HTTP
servers. The request sends the mood and requested playlist size to this service.
It asks for JSON `{"queries":["artist - song title"]}` inside a normal
`choices[0].message.content` response. Myne resolves those suggestions against
the music provider and deduplicates tracks. Unavailable suggestions can produce
a shorter playlist. API billing/authentication belongs to your chosen service.
Redirects are rejected to protect the API key.

## Configuration reference

Configuration uses environment variables. No credentials are needed for the
local provider. Change player settings before starting the service; run
`myne shutdown` and start playback again after changing its environment.

| Variable | Meaning | Default |
| --- | --- | --- |
| `MYNE_DATA_DIR` | SQLite database and saved queue directory | OS user data directory for `myne` |
| `MYNE_RUNTIME_DIR` | Short, private Unix socket directory | Per-user temporary directory with data-path hash |
| `MYNE_PROVIDER` | Default provider | `youtube` |
| `MYNE_MPV` | mpv executable path/name | `mpv` |
| `MYNE_AUDIO_OUTPUT` | Optional mpv output driver (`--ao`), such as `pulse` | mpv automatic selection |
| `MYNE_VOLUME` | Initial volume, 0–100 | `70`; later changes persist |
| `MYNE_AUTH_FILE` | ytmusicapi authentication JSON | unset |
| `MYNE_COOKIES_FILE` | yt-dlp cookie file | unset |
| `MYNE_LIBRARY` | Local music library root | unset |
| `MYNE_PLUGINS` | Comma-separated enabled entry-point names | none |
| `MYNE_MIX_ENDPOINT` | Full AI chat endpoint | unset |
| `MYNE_MIX_MODEL` | Model to send to that endpoint | `myne-mix` |
| `MYNE_MIX_API_KEY` | Optional bearer credential | unset |

`myne --data-dir /path COMMAND` selects a separate queue and runtime. Directories
are owner-only, and Myne rejects a foreign-owned or symlink runtime directory.
Unix socket path lengths are limited, so use a short runtime path. A custom
`MYNE_RUNTIME_DIR` must be unique for each independent data directory/service.

## Troubleshooting

- `myne doctor` checks the interpreter, mpv executable, paths and plugin setup
  without contacting music services.
- If mpv is missing, install it using your OS package manager or set `MYNE_MPV`.
- If playback is silent, run `myne volume 70`, then `myne resume`, and inspect
  `myne --json now` while the UI remains open. The UI's − / + and Mute/Unmute
  controls do the same sound adjustments. Raising the volume clears mute.
- If `playing` is false or the timer stays at zero, check the reported error:
  a selected song may still be loading or the stream may have failed. If the
  timer advances, compare a local file played directly by mpv with the same
  file in Myne, and check the host's volume and selected speakers/headphones.
- An output of `null` intentionally sends audio nowhere. Remove a `null`
  `MYNE_AUDIO_OUTPUT` setting or a silent-output wrapper in `MYNE_MPV`, then
  restart the service. Meters measure decoded audio and cannot verify speakers.
- If direct mpv playback only works with a particular output driver, set the
  same driver using `MYNE_AUDIO_OUTPUT` before starting Myne. For example,
  `MYNE_AUDIO_OUTPUT=pulse myne ui` selects PulseAudio when it is available.
  Run `myne shutdown` first to apply a changed output to the background player.
- If YouTube playback fails, upgrade the extractor with
  `python -m pip install --upgrade yt-dlp`, check its runtime requirements, and
  review whether cookies or account access are needed. Search and stream
  extraction can fail independently.
- Run `myne shutdown`, then `myne play` to restart the service. The interrupted
  current song restarts from the beginning, followed by upcoming tracks.
- `myne queue show` works while the service is stopped. `myne queue restore`
  needs an existing snapshot; automatic persistence does not require snapshots.
- A failing plugin is reported by `myne plugins`. Remove its name from
  `MYNE_PLUGINS` and restart the service to disable it.

## Uninstall

Run `myne shutdown`, then uninstall the Python package or remove its virtual
environment. The data directory remains available for recovery; delete it
manually only if you want to erase your queue, snapshots and listening history.
