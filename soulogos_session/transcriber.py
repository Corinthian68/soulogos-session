from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel

# Discord sends 48 kHz stereo 16-bit PCM; Whisper expects 16 kHz mono float32.
_DISCORD_RATE = 48_000
_WHISPER_RATE = 16_000
_RESAMPLE_RATIO = _WHISPER_RATE / _DISCORD_RATE
_MIN_SAMPLES = int(_WHISPER_RATE * 0.1)  # skip clips shorter than 100 ms

# Bias Whisper toward fantasy / TTRPG vocabulary so proper nouns and game terms
# transcribe more reliably.
_TTRPG_PROMPT = (
    "Aglarion, Onadbyr, Crown of the Oathbreaker, Tarn, Zanthus, Kaelen, Everett, Lorzak, "
    "Thessaly, Marsden, Kyber, Ricio, Finia, Blister, Rythis, Markya, Belzir, "
    "paladin, rogue, wizard, cleric, fighter, ranger, barbarian, bard, druid, monk, sorcerer, warlock, "
    "tavern, dungeon, dragon, cultist, oathbreaker, undead, necromancer."
)


@dataclass
class TranscriptionResult:
    text: str
    confidence: float
    language: str


class Transcriber:
    def __init__(self, model_size: str = "base", device: str = "cpu") -> None:
        self._model = WhisperModel(model_size, device=device, compute_type="int8")

    def transcribe_pcm(self, pcm_bytes: bytes) -> TranscriptionResult | None:
        """
        Accept raw stereo 16-bit PCM at 48 kHz, return transcription or None
        if the clip is empty / too short.
        """
        audio = _pcm_to_mono_float(pcm_bytes)
        if len(audio) < _MIN_SAMPLES:
            return None

        segments, info = self._model.transcribe(
            audio, beam_size=5, initial_prompt=_TTRPG_PROMPT
        )

        texts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            t = seg.text.strip()
            if t:
                texts.append(t)
                logprobs.append(seg.avg_logprob)

        if not texts:
            return None

        # avg_logprob is negative; shift by +1 and clamp to [0, 1] as a rough confidence.
        avg_conf = float(np.clip(np.mean(logprobs) + 1.0, 0.0, 1.0))

        return TranscriptionResult(
            text=" ".join(texts),
            confidence=avg_conf,
            language=info.language,
        )


def _pcm_to_mono_float(pcm_bytes: bytes) -> np.ndarray:
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32_768.0
    # Average stereo channels to mono
    if len(samples) % 2 == 0:
        samples = samples.reshape(-1, 2).mean(axis=1)
    # Downsample 48 kHz -> 16 kHz via linear interpolation
    new_len = int(len(samples) * _RESAMPLE_RATIO)
    return np.interp(
        np.linspace(0.0, len(samples) - 1, new_len),
        np.arange(len(samples)),
        samples,
    ).astype(np.float32)
