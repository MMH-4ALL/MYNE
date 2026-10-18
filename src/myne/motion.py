"""Time-based presentation of measured audio, independent of render frequency.

The envelopes are display coordinates on Myne's normalized dBFS scale. They
do not generate audio samples, frequency bins, or amplitude-history entries.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def reduced_motion_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Respect explicit motion preferences without treating no-color as one."""
    environment = os.environ if environ is None else environ
    return (
        environment.get("MYNE_REDUCED_MOTION", "").strip().casefold() in {"1", "true", "yes", "on"}
        or environment.get("TERM", "").strip().casefold() == "dumb"
        or environment.get("TEXTUAL_ANIMATIONS", "").strip().casefold() == "none"
    )


def _normalized(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return min(1.0, max(0.0, number)) if math.isfinite(number) else None


def _sample(levels: Mapping[str, Any]) -> dict[str, Any]:
    """Copy metadata and retain only complete, finite measured channels."""
    sample = {**levels, "available": False, "rms": [], "peak": []}
    sample.setdefault("error", None)
    if levels.get("available") is not True or levels.get("paused") is True or levels.get("playing") is False:
        return sample
    rms, peak = levels.get("rms"), levels.get("peak")
    if not isinstance(rms, Sequence) or isinstance(rms, (str, bytes)):
        return sample
    if not isinstance(peak, Sequence) or isinstance(peak, (str, bytes)):
        return sample
    clean_rms: list[float] = []
    clean_peak: list[float] = []
    for raw_rms, raw_peak in list(zip(rms, peak))[:2]:
        channel_rms, channel_peak = _normalized(raw_rms), _normalized(raw_peak)
        if channel_rms is not None and channel_peak is not None:
            clean_rms.append(channel_rms)
            clean_peak.append(max(channel_rms, channel_peak))
    sample.update(available=bool(clean_rms), rms=clean_rms, peak=clean_peak)
    return sample


@dataclass(slots=True)
class _Envelope:
    rms: float
    peak: float
    hold_until: float


class AudioMotion:
    """Smooth fresh measurements with analytical attack and release envelopes.

    ``update`` must be called for newly received telemetry only; ``frame``
    can run at any rendering frequency. Missing telemetry expires promptly,
    while pause, stop, unavailable data and track changes clear old energy.
    Reduced motion presents each raw measurement without envelopes or hold.
    """

    _ATTACK = 0.045
    _RELEASE = 0.18
    _PEAK_HOLD = 0.3
    _PEAK_DECAY = 0.8

    def __init__(
        self,
        reduced_motion: bool = False,
        clock: Callable[[], float] = time.monotonic,
        *,
        stale_after: float = 0.5,
    ) -> None:
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after must be a finite positive duration")
        self.reduced_motion = reduced_motion
        self.stale_after = stale_after
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        """Forget the previous sample and every channel's display state."""
        self._sample: dict[str, Any] = {"available": False, "rms": [], "peak": [], "error": None}
        self._identity: tuple[Any, Any] | None = None
        self._channels: list[_Envelope] = []
        self._received_at: float | None = None
        self._advanced_at: float | None = None

    def _timestamp(self, now: float | None) -> float:
        timestamp = self._clock() if now is None else now
        if not math.isfinite(timestamp):
            raise ValueError("motion timestamps must be finite")
        # A late callback must not move an envelope backwards in time.
        return max(timestamp, self._advanced_at) if self._advanced_at is not None else timestamp

    def _clear_envelope(self) -> None:
        self._channels = []
        self._received_at = None
        self._advanced_at = None

    def _advance(self, now: float) -> None:
        if self._received_at is None or now - self._received_at >= self.stale_after:
            self._clear_envelope()
            return
        elapsed = max(0.0, now - self._advanced_at) if self._advanced_at is not None else 0.0
        previous = self._advanced_at if self._advanced_at is not None else now
        for channel, target_rms, target_peak in zip(
            self._channels, self._sample["rms"], self._sample["peak"]
        ):
            tau = self._ATTACK if target_rms > channel.rms else self._RELEASE
            channel.rms = target_rms + (channel.rms - target_rms) * math.exp(-elapsed / tau)
            decay_seconds = max(0.0, now - max(previous, channel.hold_until))
            channel.peak = max(target_peak, channel.rms, channel.peak - self._PEAK_DECAY * decay_seconds)
        self._advanced_at = now

    def update(self, levels: Mapping[str, Any], *, now: float | None = None) -> None:
        """Set new measured targets, preserving transport and error metadata."""
        timestamp = self._timestamp(now)
        self._advance(timestamp)
        sample = _sample(levels)
        identity = (sample.get("provider"), sample.get("track_id"))
        if identity != self._identity or len(sample["rms"]) != len(self._channels):
            self._clear_envelope()
        self._identity = identity
        self._sample = sample
        if not sample["available"]:
            self._clear_envelope()
            return
        if not self._channels:
            self._channels = [
                _Envelope(rms, peak, timestamp + self._PEAK_HOLD)
                for rms, peak in zip(sample["rms"], sample["peak"])
            ]
        else:
            for channel, peak in zip(self._channels, sample["peak"]):
                if peak >= channel.peak:
                    channel.peak = peak
                    channel.hold_until = timestamp + self._PEAK_HOLD
        self._received_at = timestamp
        self._advanced_at = timestamp

    def frame(self, *, now: float | None = None) -> dict[str, Any]:
        """Return a fresh display frame; never mutate a caller's sample."""
        self._advance(self._timestamp(now))
        frame = {**self._sample, "rms": [], "peak": []}
        if not self._channels:
            frame["available"] = False
        elif self.reduced_motion:
            frame["rms"] = list(self._sample["rms"])
            frame["peak"] = list(self._sample["peak"])
        else:
            frame["rms"] = [channel.rms for channel in self._channels]
            frame["peak"] = [channel.peak for channel in self._channels]
        return frame
