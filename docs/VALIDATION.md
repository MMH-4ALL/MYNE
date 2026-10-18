# Validation record

Validated on 18 October 2026 using Python 3.13.5 on Linux.

- **95 tests passed**, including injected provider/extractor tests, SQLite queue
  recovery and concurrency, plugin activation/rollback, AI response validation,
  background daemon and fake-mpv subprocess integration, transport controls,
  radio refill/failure handling, and headless TUI interaction.
- Ruff lint passed.
- mypy passed across all 20 application modules.
- Source distribution and Python wheel built successfully with Hatchling.
- Live anonymous YouTube Music search returned song metadata.
- Live yt-dlp extraction returned an HTTPS audio stream and headers with
  `download=False`; no full song was downloaded.
- The actual Textual interface was exported and visually checked at 120×36 and
  80×24 terminal sizes. The README screenshot uses sample metadata.

The environment did not contain an mpv executable or an audio output device, so
real mpv/audio-device playback was not verified. Fake-mpv integration exercises
process startup, JSON IPC, stream handoff, and controls. The inherited-FD crash
lifeline follows mpv's documented IPC-client behavior; its real-mpv exit behavior
also remains a manual integration check.

AI mixes were verified with simulated HTTP responses; no paid AI service was
called. Live cookies/authentication, region restrictions, and provider rate
limits require validation with the user's own account/environment.

The included GitHub Actions workflow runs the offline suite, lint, types and
package build on Python 3.12 and 3.13. Its hosted result is separate from the
local validation above.
