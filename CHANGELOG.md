# Changelog

## 0.4.0

- Settings screen from the masthead, F2, or command palette, with Appearance,
  Playback and Updates tabs. Theme, sidebar, lyrics, visualizer, reduced motion,
  saved volume and automatic update checks persist together in SQLite.
- Optional daily update checks on launch, off by default, with caching and
  failure backoff. Check now always retries immediately; checks never install.
- Install update button and `myne update --install` for supported, writable,
  non-editable virtual environments. The updater rechecks official main,
  prepares a wheel from a pinned commit, verifies package/version metadata,
  installs with that environment's pip, then verifies the installed version.
- Unsupported or editable installations show instructions for their original
  install method. Older and prerelease builds are not offered as updates.
  A successful update requires restarting the UI and background player.
- `myne settings show/set` and `myne update` provide matching CLI controls.
- Clear stale play/queue targets while a different playlist is loading, and
  replace loading placeholders after a library failure.
- Compact lyrics correctly hide both track panes and restore them on return.
  User choices made during startup survive a delayed preferences load.
- Player client rejects invalid response data with a normal playback error;
  idle volume reporting now reflects the saved setting.
- Modal screens retain keyboard focus and reject underlying music shortcuts.

## 0.3.1

- Toggleable playlist sidebar with direct collection navigation at all supported
  terminal sizes. Ctrl+B shows/hides it; Ctrl+O opens playlist link import.
- Paste public Spotify, YouTube Music, YouTube or SoundCloud playlist links into
  the import dialog or search box. Imports run away from the UI event loop with
  progress, cancellation, conservative matching, and no audio downloads.
- Optional Spotify user token uses the current playlist-items API with bounded
  pagination; public embed imports always disclose possible incompleteness.
- Imported playlists retain their order, duplicate entries, source link and
  unmatched-song notes in SQLite. Existing names receive a suffix, and saving
  a collection leaves the current song and upcoming queue untouched.
- Browse playlist tracks, play all, queue all, or inspect source/import details.
  Existing saved queues now open as collections; CLI queue restore is unchanged.
- New `myne playlist import/list/show/play/queue` commands and JSON output.

## 0.3.0

- A new Studio workspace with a library rail on wide terminals, compact view
  tabs, a persistent player dock, and Studio / Midnight / Ember themes.
- A searchable Ctrl+K command palette for library, playback, appearance and
  queue actions. All controls also have keyboard shortcuts and tooltips.
- Liked tracks persist in SQLite, with a separate recently played view drawn
  from committed listening history. Theme selection survives UI restarts.
- Save named upcoming queues in the UI, browse them and restore them without
  interrupting the current song or automatically starting playback.
- Queue reorder/remove/shuffle controls with stale-selection protection and
  transactions that preserve duplicate tracks and the current song.
- Repeat off/all/one with persisted settings and tested natural-end/manual-skip
  behavior. Radio turns repeat off; enabling repeat leaves radio mode.
- Ten-second rewind/forward controls, visible mute/output state, a Stop control,
  and improved empty/loading states. The compact layout supports 80×24 terminals.
- The terminal UI now requires Textual 6.12 or newer within the 6.x series.

## 0.2.2

- Visible volume, mute/unmute controls and output status in the TUI. Use `[` / `]`
  for volume and `m` for mute; raising the volume also unmutes the player.
- `myne mute` / `myne unmute`, volume adjustment while idle, and mute/output
  diagnostics in `myne now` / `myne --json now`. Loading tracks no longer appear
  as already playing. An optional `MYNE_AUDIO_OUTPUT` selects an mpv output driver.
- Playing an existing queue selection now skips to that entry without copying
  the queue tail or creating duplicate playback. Stale selections are rejected
  before interrupting the current song or changing the queue.
- Space and the transport's Start button start an idle saved queue; the control
  changes to Pause/Resume during playback.
- Stop, next and replacement playback recover if mpv disconnects before the
  service notices, while still recording the finished listening segment once.
- Socket creation/permission failures produce normal Myne errors instead of
  uncaught exceptions; status requests can still report an idle player.
- Corrected the repository URL in the installation commands. Restart an existing
  player with `myne shutdown` after installing this update.

## 0.2.1

- Smooth 30 FPS meter rendering with eased attack/release, peak hold and
  fractional terminal cells; audio polling keeps its existing bounded rate.
- Gentle lyric-follow scrolling and pane reveals; seeks, pause and buffering
  update immediately. Stale audio measurements clear promptly.
- Reduced-motion support through `MYNE_REDUCED_MOTION=1`,
  `TEXTUAL_ANIMATIONS=none`, or a dumb terminal. The standalone visualizer also
  supports `--no-animation` and configurable rendering up to 60 FPS.

## 0.2.0

- Live stereo RMS/peak visualizer and amplitude history from actual mpv audio
  measurements, available in the TUI and `myne visualize`.
- LRCLIB lyrics with provider fallback, synchronized line highlighting,
  recording matching and bounded private caching.
- Plain/LRC/JSON lyrics commands, optional query lookup and cache refresh.
- Responsive lyrics pane, visible lyrics/visualizer shortcuts, pause/seek
  synchronization and stale-request protection.
- Real mpv integration tests using generated audio and a silent output sink.

## 0.1.0

- Initial streaming player, YouTube/local providers, persistent queue,
  radio, listening statistics, Smart Mix, TUI and plugin contracts.
