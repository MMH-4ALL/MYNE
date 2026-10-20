# Validation record

## Version 0.3.1 playlist import and sidebar

Validated locally on 20 October 2026 using Python 3.12.14. The offline suite
checks link normalization, shared-link tracking removal, matching quality,
duplicate/order preservation, bounded Spotify pagination, private/malformed
responses, cancellation, persistence, concurrent name collisions, CLI commands,
and real Textual sidebar/import/browse/play/queue interactions. Filesystem socket
tests remain enabled in CI; the local environment restriction below still applies.

A live Spotify public playlist exposed metadata successfully, and one song was
matched to a YouTube Music result. A live YouTube Music playlist import returned
the requested two tracks and a limit warning. No Spotify audio was requested.
SoundCloud metadata and matching have injected-extractor regression coverage;
the live extractor check here could not access the public sample set. This does
not verify every upstream playlist, private access, or physical audio output.

The actual UI was captured at 144×42 and 80×24 with the playlist sidebar, including
the compact import modal, plus an Ember theme capture. The README preview uses
fictional sample songs, original sample lyrics and demo meter levels. The imports
are copies of exposed metadata; public Spotify embeds explicitly warn that the
visible track list may be incomplete, and imported songs may be unmatched.

## Version 0.3.0 Studio workspace

Validated locally on 20 October 2026 with **253 passing tests** on Python 3.12.14,
two real-mpv skips and five filesystem Unix-socket cases excluded for the local
restriction described below. Ruff and mypy pass across 26 application modules.
New regressions cover favorite persistence/provider identity, recent-history
ordering, stale queue edits, shuffle preservation, repeat modes, the command
palette, named queue save/restore, seek/stop controls, and theme restoration.

The real Textual interface was rendered and visually checked at 80×24 and
144×42, including Studio, Midnight and Ember palettes. The README image uses
explicitly labelled sample metadata, original sample lyrics, and demo telemetry.
It is a screenshot of the actual UI and does not demonstrate audible playback.
UI tests also exercise the intermediate 100×30 layout.

## Version 0.2.2 queue and recovery fixes

The follow-up sound-controls update completed **239 local tests** with two
real-mpv skips and five filesystem Unix-socket cases excluded for the same
environment restriction described below. New checks cover recovering from zero
volume/mute in the UI, loading and silent-output labels, bounded relative volume
changes and output configuration passed to the background service. Full CI also
checks mute/unmute and output reporting through real mpv using its silent test
sink. This does not establish the cause of silent audio on a user's device.

Validated on 20 October 2026 using Python 3.12.14 and Linux. The local run
completed with **231 passing tests**, including headless TUI interaction, real
SQLite queue transitions, stale/invalid selection protection, mpv disconnect
recovery and socket-permission error handling. All eight initial regression
cases failed before the fixes and passed afterward.

Two tests requiring an installed mpv were skipped. Five filesystem Unix-socket
tests were excluded locally because this execution environment blocks creating
those sockets; the CI suite keeps them enabled, including queue selection
through the actual background service and two independent clients. Existing
socket-pair IPC tests ran locally. Ruff, mypy across 23 source modules, and the
wheel/source package builds passed.

This update did not repeat live YouTube, lyric-service or physical audio-device
checks. The earlier live validation below is a historical record.

## Version 0.2.1 animation update

Validated on 18 October 2026 with **224 passing tests**, Ruff lint and mypy
across 23 application modules. Time-based motion tests compare 10/30/60/144 FPS
and cover peak hold, pause/stop, stale expiry, track/channel changes and reduced
motion. UI/CLI regressions verify that extra drawing frames never add telemetry
requests or fabricated history and that seeks snap while sequential lyrics ease.
The existing real-mpv integration tests also pass.

A four-second animated capture of the actual TUI was visually checked across
58 distinct frames, including fractional meter changes, lyric reveal/follow and
cleared meters during pause. Its telemetry and original lyrics are explicitly
labelled as demonstration data. The playback/provider stack is unchanged from
the live checks recorded below.

## Version 0.2.0 live audio and lyrics checks

Version 0.2.0 validated on 18 October 2026 using Python 3.13.5, Linux and mpv 0.40.0.

- **164 tests passed**, covering providers/extraction, SQLite queue recovery and
  concurrency, plugins, AI response handling, player process/IPC lifecycle,
  real audio measurements, lyrics/cache/LRC parsing, and headless TUI interaction.
- Ruff lint passed; mypy passed across all 22 application modules.
- Live YouTube playback through real mpv with a silent output sink succeeded:
  stereo audio measurements were available and playback time advanced.
- A generated stereo WAV with a 20 dB channel difference produced the expected
  measured RMS and peak difference. Real mpv tests cover pause/seek/stop and
  daemon IPC, without requiring sound hardware or copyrighted test audio.
- Live LRCLIB lookup returned timed lyrics for the streamed song. No API key was
  used, no lyric text was printed in validation logs, and no audio was uploaded.
- Lyrics tests cover missing/instrumental/plain/timed states, artist/title/duration
  matching, rate limits, caching, malformed responses and seek-safe timing.
- TUI tests cover forward/backward seek highlighting, pause/buffering, late lyrics
  for an old track, wrapping/autoscroll, and single in-flight audio polling.
- The actual Textual interface was rendered and visually checked at 120×36 and
  80×24. The documentation screenshot uses explicit sample lyrics/telemetry.

A physical speaker/sound-device path was not tested. Audio decoding, streaming,
mpv filtering, the playback clock and IPC controls were exercised with mpv's
real-time null output. Upstream song availability, account/region restrictions
and optional authentication still depend on the user's environment.

The GitHub Actions workflow installs mpv/FFmpeg and runs tests, lint, types and
package builds on Python 3.12 and 3.13. Its hosted result is separate from local
validation above.
