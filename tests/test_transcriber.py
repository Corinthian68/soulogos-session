import numpy as np
import pytest
from soulogos_session.transcriber import _pcm_to_mono_float, _MIN_SAMPLES


def _make_pcm(duration_s: float, freq_hz: float = 440.0) -> bytes:
    """Generate a sine-wave tone as stereo 16-bit PCM at 48 kHz."""
    rate = 48_000
    n = int(rate * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False)
    wave = (np.sin(2 * np.pi * freq_hz * t) * 16_000).astype(np.int16)
    stereo = np.empty(n * 2, dtype=np.int16)
    stereo[0::2] = wave
    stereo[1::2] = wave
    return stereo.tobytes()


def test_pcm_to_mono_float_shape() -> None:
    pcm = _make_pcm(1.0)
    audio = _pcm_to_mono_float(pcm)
    assert abs(len(audio) - 16_000) <= 2


def test_pcm_to_mono_float_range() -> None:
    pcm = _make_pcm(0.5)
    audio = _pcm_to_mono_float(pcm)
    assert audio.dtype == np.float32
    assert float(np.max(np.abs(audio))) <= 1.0


def test_min_samples_constant() -> None:
    assert _MIN_SAMPLES == 1_600  # 100 ms at 16 kHz


def test_short_clip_returns_none(monkeypatch) -> None:
    from soulogos_session import transcriber as t_mod

    class _FakeModel:
        def __init__(self, *a, **kw): pass
        def transcribe(self, audio, **kw):
            return [], type("Info", (), {"language": "en"})()

    monkeypatch.setattr(t_mod, "WhisperModel", _FakeModel)

    from soulogos_session.transcriber import Transcriber
    tr = Transcriber.__new__(Transcriber)
    tr._model = _FakeModel()

    pcm = _make_pcm(0.05)  # 50 ms — below _MIN_SAMPLES after resampling
    assert tr.transcribe_pcm(pcm) is None
