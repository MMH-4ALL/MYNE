"""Presentation follows actual measurements and elapsed time, not FPS."""

import math
from dataclasses import dataclass
from typing import Any

import pytest

from myne.motion import AudioMotion, reduced_motion_requested


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def sample(rms: float = 0.6, peak: float = 0.9, **metadata: Any) -> dict[str, Any]:
    return {"available": True, "rms": [rms], "peak": [peak], "error": None, **metadata}


def test_analytical_attack_is_independent_of_render_frequency() -> None:
    frames = []
    for fps in (10, 30, 60, 144):
        motion = AudioMotion()
        motion.update(sample(0.0, 0.0), now=0.0)
        motion.update(sample(), now=0.0)
        for tick in range(1, fps + 1):
            timestamp = tick / fps * 0.4
            motion.frame(now=timestamp)
        frames.append(motion.frame(now=0.4))
    for frame in frames:
        assert frame["rms"] == pytest.approx([0.6 * (1 - math.exp(-0.4 / 0.045))])
        assert frame["peak"] == pytest.approx([0.9])


def test_release_and_peak_decay_are_independent_of_render_frequency() -> None:
    frames = []
    for fps in (10, 30, 60, 144):
        motion = AudioMotion(stale_after=1.0)
        motion.update(sample(), now=0.0)
        before = motion.frame(now=0.2)
        motion.update(sample(0.0, 0.0), now=0.2)
        for tick in range(1, fps + 1):
            motion.frame(now=0.2 + tick / fps * 0.4)
        frame = motion.frame(now=0.6)
        assert frame["rms"] == pytest.approx([before["rms"][0] * math.exp(-0.4 / 0.18)])
        assert frame["peak"] == pytest.approx([0.9 - 0.8 * 0.3])
        frames.append(frame)
    assert frames[0]["rms"] == pytest.approx(frames[-1]["rms"])


def test_injected_clock_and_new_transient_peak_hold() -> None:
    clock = Clock()
    motion = AudioMotion(clock=clock, stale_after=1.0)
    motion.update(sample(0.1, 0.7))
    motion.update(sample(0.5, 0.7))
    clock.now = 0.1
    assert 0.1 < motion.frame()["rms"][0] < 0.5
    motion.update(sample(0.5, 1.0))
    clock.now = 0.2
    motion.update(sample(0.0, 0.0))
    clock.now = 0.4
    assert motion.frame()["peak"] == pytest.approx([1.0])
    clock.now = 0.5
    assert motion.frame()["peak"] == pytest.approx([0.92])


@pytest.mark.parametrize("metadata", [{"paused": True}, {"playing": False}, {"available": False}])
def test_pause_stop_and_unavailable_clear_immediately(metadata: dict[str, Any]) -> None:
    motion = AudioMotion()
    motion.update(sample(), now=0.0)
    assert motion.frame(now=0.1)["rms"][0] > 0
    motion.update(sample(error="meter unavailable", **metadata), now=0.11)
    frame = motion.frame(now=0.11)
    assert frame["available"] is False
    assert frame["rms"] == frame["peak"] == []
    assert frame["error"] == "meter unavailable"
    for key, value in metadata.items():
        if key != "available":
            assert frame[key] is value
    motion.update(sample(0.0, 0.0), now=0.12)
    assert motion.frame(now=0.13)["rms"] == [0.0]
    assert motion.frame(now=0.13)["peak"] == [0.0]


def test_stale_measurement_expires_without_requiring_a_new_sample() -> None:
    motion = AudioMotion(stale_after=0.5)
    motion.update(sample(track_id="one", provider="youtube", position=12.0), now=1.0)
    assert motion.frame(now=1.499)["available"] is True
    frame = motion.frame(now=1.5)
    assert frame["available"] is False
    assert frame["rms"] == frame["peak"] == []
    assert frame["track_id"] == "one"
    assert frame["position"] == 12.0
    motion.update(sample(0.0, 0.0), now=2.0)
    assert motion.frame(now=2.1)["rms"] == [0.0]


def test_silence_releases_smoothly_and_peak_never_drops_below_rms() -> None:
    motion = AudioMotion(stale_after=1.0)
    motion.update(sample(1.0, 1.0), now=0.0)
    motion.frame(now=0.2)
    motion.update(sample(0.0, 0.0), now=0.2)
    frames = [motion.frame(now=timestamp) for timestamp in (0.2, 0.3, 0.5, 0.7)]
    assert all(frame["available"] for frame in frames)
    assert frames[-1]["rms"][0] < frames[0]["rms"][0] / 10
    assert all(frame["peak"][0] >= frame["rms"][0] >= 0 for frame in frames)


@pytest.mark.parametrize("changed", [{"track_id": "two"}, {"provider": "local"}])
def test_track_or_provider_change_clears_previous_envelope(changed: dict[str, str]) -> None:
    motion = AudioMotion()
    metadata = {"track_id": "one", "provider": "youtube"}
    motion.update(sample(**metadata), now=0.0)
    motion.frame(now=0.1)
    motion.update(sample(0.0, 0.0, **{**metadata, **changed}), now=0.11)
    assert motion.frame(now=0.12)["rms"] == [0.0]
    assert motion.frame(now=0.12)["peak"] == [0.0]


def test_stereo_to_mono_does_not_retain_an_old_channel_or_peak() -> None:
    motion = AudioMotion()
    motion.update({"available": True, "rms": [0.9, 0.8], "peak": [1.0, 0.9]}, now=0.0)
    assert len(motion.frame(now=0.1)["rms"]) == 2
    motion.update(sample(0.0, 0.0), now=0.11)
    assert motion.frame(now=0.12)["rms"] == [0.0]
    assert motion.frame(now=0.12)["peak"] == [0.0]


def test_first_measurement_and_resumption_show_current_values_immediately() -> None:
    motion = AudioMotion()
    motion.update(sample(0.6, 0.9), now=0.0)
    assert motion.frame(now=0.0)["rms"] == [0.6]
    motion.update(sample(paused=True), now=0.1)
    motion.update(sample(0.2, 0.3), now=0.2)
    assert motion.frame(now=0.2)["rms"] == [0.2]
    assert motion.frame(now=0.2)["peak"] == [0.3]


def test_reduced_motion_reports_raw_samples_without_attack_release_or_hold() -> None:
    motion = AudioMotion(reduced_motion=True)
    motion.update(sample(0.6, 1.0), now=0.0)
    assert motion.frame(now=0.0)["rms"] == [0.6]
    motion.update(sample(0.1, 0.2), now=0.01)
    assert motion.frame(now=0.01)["rms"] == [0.1]
    assert motion.frame(now=0.1)["peak"] == [0.2]
    assert motion.frame(now=0.51)["available"] is False


@pytest.mark.parametrize("bad", [None, {}, [], True, "bad", "inf", "nan", math.inf, -math.inf, math.nan])
def test_invalid_channel_values_never_reach_display(bad: Any) -> None:
    motion = AudioMotion()
    motion.update({"available": True, "rms": [bad], "peak": [1.0]}, now=0.0)
    assert motion.frame(now=0.0)["available"] is False
    assert motion.frame(now=0.0)["rms"] == []


@pytest.mark.parametrize("rms,peak", [(None, None), ("0.5", "0.9"), ({}, {}), ([0.5], [])])
def test_malformed_channel_containers_are_unavailable(rms: Any, peak: Any) -> None:
    motion = AudioMotion()
    motion.update({"available": True, "rms": rms, "peak": peak}, now=0.0)
    assert motion.frame(now=0.0)["available"] is False


def test_finite_values_are_clamped_and_peak_cannot_underrun_rms() -> None:
    motion = AudioMotion(reduced_motion=True)
    motion.update({"available": True, "rms": ["1.2", -2], "peak": [0.3, 2]}, now=0.0)
    frame = motion.frame(now=0.0)
    assert frame["rms"] == [1.0, 0.0]
    assert frame["peak"] == [1.0, 1.0]


def test_samples_and_returned_frames_are_independent_objects() -> None:
    motion = AudioMotion(reduced_motion=True)
    raw = sample()
    motion.update(raw, now=0.0)
    raw["rms"][0] = 1.0
    motion.frame(now=0.0)["rms"].append(1.0)
    assert motion.frame(now=0.0)["rms"] == [0.6]
    motion.reset()
    assert motion.frame(now=0.1) == {"available": False, "rms": [], "peak": [], "error": None}


@pytest.mark.parametrize("duration", [0, -0.1, math.inf, math.nan])
def test_expiry_duration_must_be_positive_and_finite(duration: float) -> None:
    with pytest.raises(ValueError, match="stale_after"):
        AudioMotion(stale_after=duration)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_explicit_reduced_motion_preference(value: str) -> None:
    assert reduced_motion_requested({"MYNE_REDUCED_MOTION": value}) is True


@pytest.mark.parametrize("environment", [{"TERM": "dumb"}, {"TEXTUAL_ANIMATIONS": "none"}])
def test_terminal_and_textual_motion_preferences(environment: dict[str, str]) -> None:
    assert reduced_motion_requested(environment) is True


@pytest.mark.parametrize("environment", [{}, {"NO_COLOR": "1"}, {"MYNE_REDUCED_MOTION": "false"}])
def test_motion_preferences_do_not_conflate_color_and_animation(environment: dict[str, str]) -> None:
    assert reduced_motion_requested(environment) is False
