# Architecture

Myne separates immutable domain objects, application use cases, adapters and
terminal presentation. Concrete implementations are composed in `container.py`
and can be replaced with fakes without contacting external services.

## Incremental implementation stages

1. **Foundation:** immutable `Track`, `Album`, `Artist`, and `Stream` dataclasses;
   actionable domain errors; configuration; packaging and the `myne` executable.
2. **Persistence:** transactional SQLite upcoming queue, named snapshots, current
   song recovery, preferences, listening segments and aggregated statistics.
3. **Provider boundary:** the abstract `MusicProvider` contract; YouTube metadata
   mapping and local file adapter; explicit capabilities for lyrics and radio.
4. **Playback engine:** `yt-dlp` resolves a stream immediately before playback;
   mpv consumes its URL. A request-ID-aware JSON IPC driver retains asynchronous
   events while waiting for command replies.
5. **Player service:** detached per-user process, private socket, serialized
   state transitions, auto-advance, radio refill, pause-aware listening time and
   restart recovery. Independent CLI commands share this process.
6. **Experience and extensions:** Rich/Questionary command presentation, Textual
   full-screen UI, AI/mood mix strategies and explicit plugin activation.
7. **Verification:** offline provider contract tests, transactional persistence,
   fake-mpv IPC, service lifecycle tests and headless TUI interaction tests.

## Boundaries

| Layer | Modules | Responsibility |
| --- | --- | --- |
| Domain | `models`, `errors`, `providers.base` | Stable data and provider contract |
| Use cases | `service`, `mix` | Resolve, search, play, queue, mix and browse |
| Composition | `container`, `config`, `plugins` | Inject configured providers and extensions |
| Adapters | `providers.youtube`, `providers.local`, `storage` | Remote metadata, stream resolution, files and SQLite |
| Playback | `playback.mpv`, `playback.daemon`, `playback.client` | Audio process ownership and local IPC |
| Presentation | `cli`, `tui` | Commands, tables and asynchronous full-screen UI |

## Streaming flow

```mermaid
sequenceDiagram
    participant User
    participant CLI as CLI / TUI
    participant Provider
    participant DB as SQLite
    participant Player as Myne service
    participant MPV as mpv
    User->>CLI: play query
    CLI->>Provider: search / get_song
    Provider-->>CLI: Track metadata
    CLI->>Player: play serialized Track
    Player->>DB: enqueue + claim next
    Player->>Provider: resolve_stream(Track)
    Provider-->>Player: Stream URL + headers
    Player->>MPV: set headers; loadfile URL
    MPV-->>Player: JSON events / state
    Player->>DB: commit listening segment
    Player->>DB: claim next
```

Stream URLs are transient and can expire. Only provider IDs and track metadata
are queued; extraction happens when a track is about to play. Provider errors
are converted to actionable messages, and raw extractor logs are suppressed so
credentials and signed URLs do not leak to normal output. Playback never calls
`yt-dlp` with download enabled.

## Persistence and recovery

SQLite uses WAL and a busy timeout. Write methods use `BEGIN IMMEDIATE` and own
short-lived connections, so threads and independent CLI processes do not share
connection state. Queue ordering is stored as integer positions. Claiming a
track removes it from upcoming entries and writes the current track in the same
transaction. Only one current track can be claimed.

On a service restart, a persisted current track is atomically moved to the
front of the upcoming queue. Position recovery begins at zero seconds. Graceful
shutdown records the current listening segment and puts the song back. A force
kill loses uncommitted listening time but preserves the song metadata.

Queue snapshots are named copies of upcoming tracks. Snapshot restore replaces
the pending queue in one transaction and leaves the playing song alone. SQLite
schema version 1 is recorded; opening a newer schema fails with an upgrade
message. Future schema changes should use explicit migrations.

Listening time is elapsed monotonic time while mpv reports active unpaused
playback. Seeking does not create listening time. It is an estimate sampled by
the service loop, so very brief buffering/state transitions may introduce small
measurement error. History is local; no analytics or scrobbling is sent anywhere.

## Service and transport

A startup file lock prevents two launchers from creating competing services;
a lifetime lock keeps one service per runtime directory. Unix sockets live in an
owner-only directory. Requests and responses are newline-delimited JSON with
bounded message sizes. The Myne socket exposes a restricted set of player
commands; mpv's broader socket is also kept private. No TCP server is exposed.

The service serializes track changes and ticks with a lock. Provider calls can
block a command while extraction or recommendations run; clients use a bounded
timeout and the TUI performs requests in workers. This first release prioritizes
simple state correctness over speculative concurrent extraction. Radio filters
tracks already pending or in its bounded recent history. Exhausted/failed
recommendations stop refill with an actionable status. Consecutive playback
failures are bounded and remaining tracks persist.

## Extensibility

A provider implements metadata methods and stream resolution. Optional radio
and lyrics methods have safe defaults. Track identity is `(provider, id)`, so a
future provider can coexist in the same queue. Plugins are installed Python
packages discovered by entry point, loaded only when enabled by name. They can
register provider factories and mix strategies. A failed registration restores
the previous registry and reports a diagnostic. This is fault isolation, not a
sandbox for untrusted Python code.

Future Spotify support should distinguish metadata import from playback rights.
Presence/scrobbling/sync need explicit opt-in boundaries and credentials. Audio
visualization will need an mpv/libmpv audio-data interface; it is outside this
release's transport contract.
