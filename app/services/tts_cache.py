"""Text-to-speech via edge-tts, cached as MP3 files under app/static/audio/."""

import hashlib
import unicodedata
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import edge_tts
from edge_tts.exceptions import EdgeTTSException

AUDIO_DIR = Path(__file__).resolve().parent.parent / "static" / "audio"

VOICES = {"fr": "fr-FR-HenriNeural"}
# edge-tts speaking rates. "slow" is the voice actually speaking slower, not stretched audio.
SPEEDS = {"normal": "+0%", "slow": "-40%"}

TTS_ERRORS = (EdgeTTSException, aiohttp.ClientError, TimeoutError)


class TTSError(Exception):
    """edge-tts could not produce audio."""


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def cache_path(text: str, voice: str, speed: str = "normal") -> Path:
    """Where the MP3 for this text/voice/speed lives.

    Filenames are hashes so arbitrary text (slashes, very long sentences,
    case-insensitive filesystems) can't collide or escape the audio directory.
    """
    digest = hashlib.sha256(normalize_text(text).encode()).hexdigest()[:32]
    folder = AUDIO_DIR / voice if speed == "normal" else AUDIO_DIR / voice / speed
    return folder / f"{digest}.mp3"


async def _synthesize(text: str, voice: str, speed: str = "normal") -> AsyncIterator[bytes]:
    async for message in edge_tts.Communicate(normalize_text(text), voice, rate=SPEEDS[speed]).stream():
        if message["type"] == "audio":
            yield message["data"]


async def open_audio_stream(text: str, voice: str, path: Path, speed: str = "normal") -> AsyncIterator[bytes]:
    """Start synthesizing and return an iterator of MP3 chunks that also saves them to `path`.

    The first chunk is fetched up front so connection failures raise TTSError
    here, while an error response can still be sent, rather than mid-stream.
    """
    chunks = _synthesize(text, voice, speed)
    try:
        first = await anext(chunks)
    except StopAsyncIteration:
        raise TTSError("edge-tts returned no audio")
    except TTS_ERRORS as exc:
        await chunks.aclose()
        raise TTSError(f"edge-tts failed: {exc!r}") from exc
    return _save_while_streaming(first, chunks, path)


async def _save_while_streaming(first: bytes, chunks: AsyncIterator[bytes], path: Path) -> AsyncIterator[bytes]:
    # Write to a temp file and rename at the end, so a failed or abandoned
    # stream never leaves a truncated MP3 in the cache.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as f:
            f.write(first)
            yield first
            async for chunk in chunks:
                f.write(chunk)
                yield chunk
        tmp.replace(path)
    finally:
        await chunks.aclose()
        tmp.unlink(missing_ok=True)
