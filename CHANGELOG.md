# Changelog

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
