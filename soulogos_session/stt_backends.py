import asyncio
import io
import logging
import wave
from abc import ABC, abstractmethod

import aiohttp

from .config import Config
from .transcriber import _TTRPG_PROMPT, Transcriber, TranscriptionResult

log = logging.getLogger(__name__)

_ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
_ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
_ASSEMBLYAI_POLL_INTERVAL_S = 1.0

# Discord sends 48 kHz stereo 16-bit PCM; wrap it as-is in a WAV container.
_PCM_CHANNELS = 2
_PCM_SAMPLE_WIDTH = 2
_PCM_RATE = 48_000

# Word list from the Whisper TTRPG prompt, reused to bias AssemblyAI too.
_WORD_BOOST = [w.strip() for w in _TTRPG_PROMPT.rstrip(".").split(",") if w.strip()]


class STTBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    async def transcribe_pcm(self, pcm_bytes: bytes) -> TranscriptionResult | None:
        ...


class WhisperBackend(STTBackend):
    def __init__(self, model_size: str = "base", device: str = "cpu") -> None:
        self._transcriber = Transcriber(model_size, device)

    @property
    def name(self) -> str:
        return "cpu"

    @property
    def available(self) -> bool:
        return True

    async def transcribe_pcm(self, pcm_bytes: bytes) -> TranscriptionResult | None:
        return await asyncio.to_thread(self._transcriber.transcribe_pcm, pcm_bytes)


class RocmBackend(STTBackend):
    def __init__(self, model_size: str = "base") -> None:
        self._available = _cuda_available()
        self._transcriber = Transcriber(model_size, device="cuda") if self._available else None

    @property
    def name(self) -> str:
        return "rocm"

    @property
    def available(self) -> bool:
        return self._available

    async def transcribe_pcm(self, pcm_bytes: bytes) -> TranscriptionResult | None:
        if self._transcriber is None:
            return None
        return await asyncio.to_thread(self._transcriber.transcribe_pcm, pcm_bytes)


class AssemblyAIBackend(STTBackend):
    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "assemblyai"

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def transcribe_pcm(self, pcm_bytes: bytes) -> TranscriptionResult | None:
        if not self.available:
            return None

        wav_bytes = _pcm_to_wav(pcm_bytes)
        headers = {"authorization": self._api_key}

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(_ASSEMBLYAI_UPLOAD_URL, data=wav_bytes) as resp:
                resp.raise_for_status()
                upload = await resp.json()

            payload = {"audio_url": upload["upload_url"], "word_boost": _WORD_BOOST}
            async with session.post(_ASSEMBLYAI_TRANSCRIPT_URL, json=payload) as resp:
                resp.raise_for_status()
                transcript = await resp.json()

            poll_url = f"{_ASSEMBLYAI_TRANSCRIPT_URL}/{transcript['id']}"
            while True:
                async with session.get(poll_url) as resp:
                    resp.raise_for_status()
                    transcript = await resp.json()

                status = transcript["status"]
                if status == "completed":
                    break
                if status == "error":
                    log.error("AssemblyAI transcription failed: %s", transcript.get("error"))
                    return None
                await asyncio.sleep(_ASSEMBLYAI_POLL_INTERVAL_S)

        text = (transcript.get("text") or "").strip()
        if not text:
            return None

        words = transcript.get("words") or []
        if words:
            confidence = sum(w.get("confidence", 0.0) for w in words) / len(words)
        else:
            confidence = float(transcript.get("confidence") or 0.0)

        return TranscriptionResult(
            text=text,
            confidence=float(confidence),
            language=transcript.get("language_code") or "en",
        )


def _pcm_to_wav(pcm_bytes: bytes) -> bytes:
    """Wrap raw stereo 16-bit PCM at 48 kHz in an in-memory WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(_PCM_CHANNELS)
        wav_file.setsampwidth(_PCM_SAMPLE_WIDTH)
        wav_file.setframerate(_PCM_RATE)
        wav_file.writeframes(pcm_bytes)
    return buf.getvalue()


def _cuda_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        log.warning("torch is installed but CUDA/ROCm availability check failed", exc_info=True)
        return False


def get_available_backends(config: Config) -> dict[str, STTBackend]:
    return {
        "cpu": WhisperBackend(config.whisper_model, config.whisper_device),
        "assemblyai": AssemblyAIBackend(config.assemblyai_api_key),
        "rocm": RocmBackend(config.whisper_model),
    }


def load_backend(name: str, backends: dict[str, STTBackend]) -> STTBackend:
    backend = backends.get(name)
    if backend is not None and backend.available:
        return backend
    return backends["cpu"]
