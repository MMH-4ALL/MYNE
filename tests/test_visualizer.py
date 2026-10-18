"""Audio meters report measurements, silence and unavailable data honestly."""

import math
import os
import shutil
import struct
import sys
import time
import wave
from pathlib import Path

import pytest

from myne.models import Stream
from myne.playback.mpv import MpvEngine
from myne.visualizer import empty_levels, normalize_levels


def test_stereo_levels_use_decibel_display_scale() -> None:
    levels = normalize_levels(
        {
            "lavfi.astats.1.RMS_level": "-30",
            "lavfi.astats.1.Peak_level": "-6",
            "lavfi.astats.2.RMS_level": -45,
            "lavfi.astats.2.Peak_level": -12.0,
            "lavfi.astats.3.RMS_level": "-20",
            "lavfi.astats.3.Peak_level": "0",
        }
    )
    assert levels["available"] is True
    assert levels["rms"] == [0.5, 0.25]
    assert levels["peak"] == [0.9, 0.8]
    assert levels["error"] is None


def test_mono_and_digital_silence_are_not_fabricated_stereo() -> None:
    levels = normalize_levels({"lavfi.astats.1.RMS_level": "-inf", "lavfi.astats.1.Peak_level": "-inf"})
    assert levels == {"available": True, "rms": [0.0], "peak": [0.0], "error": None}


def test_overall_fallback_and_display_bounds() -> None:
    levels = normalize_levels(
        {"lavfi.astats.Overall.RMS_level": "-90", "lavfi.astats.Overall.Peak_level": "3"}
    )
    assert levels["rms"] == [0.0]
    assert levels["peak"] == [1.0]


@pytest.mark.parametrize("bad", [None, True, {}, [], "unknown", "NaN", "inf", math.nan])
def test_invalid_or_incomplete_values_do_not_escape_to_json(bad: object) -> None:
    assert (
        normalize_levels({"lavfi.astats.1.RMS_level": bad, "lavfi.astats.1.Peak_level": "-1"})
        == empty_levels()
    )


@pytest.mark.parametrize("metadata", [None, {}, [], "", 3])
def test_no_measurements_are_available_without_metadata(metadata: object) -> None:
    assert normalize_levels(metadata) == empty_levels()


def test_empty_levels_are_new_objects_without_stale_samples() -> None:
    first = empty_levels()
    first["rms"].append(1.0)
    assert empty_levels() == {"available": False, "rms": [], "peak": [], "error": None}


@pytest.mark.skipif(os.name == "nt", reason="Unix IPC")
def test_real_mpv_visualizer_measures_stereo_samples(tmp_path: Path) -> None:
    """An optional local smoke test uses a generated WAV and a silent sink."""
    binary = os.environ.get("MYNE_TEST_MPV") or shutil.which("mpv")
    if not binary:
        pytest.skip("mpv is not installed")
    wrapper = tmp_path / "mpv-silent"
    # This generated executable supplies a real-time null sink. It never
    # changes the production playback options or requires sound hardware.
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"os.execv({binary!r}, [{binary!r}, '--ao=null', *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    audio = tmp_path / "stereo.wav"
    rate = 48000
    frames = bytearray()
    for sample in range(rate * 4):
        wave_value = math.sin(2 * math.pi * 440 * sample / rate)
        frames.extend(struct.pack("<hh", int(16000 * wave_value), int(1600 * wave_value)))
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(frames)
    engine = MpvEngine(tmp_path / "ipc" / "mpv.sock", str(wrapper))
    try:
        engine.enable_visualizer(True)
        engine.play(Stream(audio.as_uri()))
        deadline = time.monotonic() + 3
        levels = empty_levels()
        while time.monotonic() < deadline:
            levels = engine.audio_levels()
            if levels["available"]:
                break
            time.sleep(0.05)
        assert levels["available"], levels
        assert len(levels["rms"]) == 2
        # Right channel is 20 dB quieter: compare the actual measured scale.
        assert levels["rms"][0] - levels["rms"][1] == pytest.approx(20 / 60, abs=0.02)
        assert levels["peak"][0] - levels["peak"][1] == pytest.approx(20 / 60, abs=0.02)
        engine.pause(True)
        assert engine.audio_levels() == empty_levels()
        engine.pause(False)
        engine.stop()
        assert engine.audio_levels() == empty_levels()
    finally:
        engine.close()
