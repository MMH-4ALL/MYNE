# Validation record

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
