"""Normalize measured mpv/FFmpeg audio levels for terminal meters.

These values describe per-frame RMS and sample peaks, not frequency bins.
The display scale maps -60 dBFS to zero and 0 dBFS to one. Decoding and
measurement remain inside mpv; Myne neither downloads nor copies audio.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TypedDict


class AudioLevels(TypedDict):
    available: bool
    rms: list[float]
    peak: list[float]
    error: str | None


def empty_levels(error: str | None = None) -> AudioLevels:
    """Represent disabled, paused, loading or unavailable measurements."""
    return {"available": False, "rms": [], "peak": [], "error": error}


def _decibels(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    # astats represents digital silence as -inf. Other nonfinite values
    # indicate an invalid measurement, and must not reach JSON or the UI.
    if number == -math.inf:
        return 0.0
    if not math.isfinite(number):
        return None
    return min(1.0, max(0.0, (number + 60.0) / 60.0))


def normalize_levels(metadata: object) -> AudioLevels:
    """Read the first two channels from native ``af-metadata`` output.

    Channels with malformed or incomplete measurements are ignored. An
    overall value is a fallback for mpv builds that omit channel metadata.
    No artificial stereo channel or animation is created for mono tracks.
    """
    if not isinstance(metadata, Mapping):
        return empty_levels()
    rms: list[float] = []
    peak: list[float] = []
    for channel in ("1", "2"):
        root = f"lavfi.astats.{channel}."
        channel_rms = _decibels(metadata.get(root + "RMS_level"))
        channel_peak = _decibels(metadata.get(root + "Peak_level"))
        if channel_rms is not None and channel_peak is not None:
            rms.append(channel_rms)
            peak.append(channel_peak)
    if not rms:
        overall_rms = _decibels(metadata.get("lavfi.astats.Overall.RMS_level"))
        overall_peak = _decibels(metadata.get("lavfi.astats.Overall.Peak_level"))
        if overall_rms is not None and overall_peak is not None:
            rms.append(overall_rms)
            peak.append(overall_peak)
    if not rms:
        return empty_levels()
    return {"available": True, "rms": rms, "peak": peak, "error": None}
