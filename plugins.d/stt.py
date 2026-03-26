"""Speech-to-text preprocessor plugin.

Transcribes audio attachments before the agent sees them.
The agent receives the transcription as text — it never sees raw audio.

Configuration: [stt] section in lucyd.toml.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_stt_config: dict = {}
_stt_backend: str = ""


def configure(config: Any) -> None:
    global _stt_config, _stt_backend
    _stt_config = config.raw("stt", default={})
    _stt_backend = _stt_config.get("backend", "")
    if _stt_backend == "local":
        validate_ffmpeg()


# ─── Transcription backends ──────────────────────────────────────


async def transcribe(config: dict, file_path: str, content_type: str) -> str:
    """Transcribe audio file using the configured STT backend.

    Args:
        config: Raw [stt] section from TOML config.
        file_path: Path to audio file on disk.
        content_type: MIME type of the audio file.

    Returns:
        Transcribed text.

    Raises:
        RuntimeError: On unknown backend or transcription failure.
    """
    backend = config.get("backend", "")
    if backend == "local":
        whisper_url = config.get("whisper_url", "http://whisper-server:8082")
        return await _transcribe_local(config.get("local", {}), file_path, whisper_url=whisper_url)
    if backend == "openai":
        return await _transcribe_openai(config.get("openai", {}), config, file_path, content_type)
    raise RuntimeError(f"Unknown STT backend: {backend!r}")


async def _transcribe_openai(
    openai_cfg: dict, stt_cfg: dict, file_path: str, content_type: str,
) -> str:
    """Transcribe audio via OpenAI-compatible Whisper API."""
    import httpx

    api_key_env = stt_cfg.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    if not api_key:
        log.debug("STT API key env var: %s", api_key_env)
        raise RuntimeError("Required STT API key not configured")

    api_url = openai_cfg.get(
        "api_url", "https://api.openai.com/v1/audio/transcriptions",
    )
    model = openai_cfg.get("model", "whisper-1")
    timeout = openai_cfg.get("timeout", 60)

    audio_data = Path(file_path).read_bytes()
    filename = Path(file_path).name

    retries = openai_cfg.get("retries", 2)
    async with httpx.AsyncClient(timeout=timeout) as client:
        last_err: Exception | None = None
        for attempt in range(1 + retries):
            try:
                resp = await client.post(
                    api_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, audio_data, content_type)},
                    data={"model": model},
                )
                resp.raise_for_status()
                text = resp.json().get("text", "").strip()
                if not text:
                    raise RuntimeError("Whisper returned empty transcription")
                return text
            except httpx.HTTPStatusError:
                raise  # 4xx errors are not transient
            except Exception as e:
                last_err = e
                if attempt < retries:
                    import asyncio
                    delay = 1.5 * (attempt + 1)
                    log.warning("STT retry %d/%d: %s — waiting %.0fs",
                                attempt + 1, retries, e, delay)
                    await asyncio.sleep(delay)
        raise last_err  # type: ignore[misc]


def validate_ffmpeg() -> None:
    """Check that ffmpeg is available. Called at startup when local STT is configured."""
    import shutil
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "Local STT requires ffmpeg but it is not installed. "
            "Install it (apt-get install ffmpeg) or use a cloud STT backend.",
        )


async def _transcribe_local(local_cfg: dict, file_path: str, *, whisper_url: str = "") -> str:
    """Transcribe audio via local whisper.cpp server.

    Converts audio to WAV (16kHz mono) via ffmpeg, then POSTs
    to the whisper.cpp HTTP inference endpoint.
    """
    import subprocess
    import tempfile

    import httpx

    base_url = whisper_url or "http://whisper-server:8082"
    endpoint = local_cfg.get("endpoint", f"{base_url}/inference")
    # Warn if non-localhost endpoint uses cleartext HTTP
    if endpoint.startswith("http://") and not any(
        h in endpoint for h in ("localhost", "127.0.0.1", "::1")
    ):
        log.warning("STT endpoint uses cleartext HTTP for non-localhost host: %s", endpoint)
    language = local_cfg.get("language", "auto")
    ffmpeg_timeout = local_cfg.get("ffmpeg_timeout", 30)
    request_timeout = local_cfg.get("request_timeout", 60)

    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    try:
        subprocess.run(
            ["ffmpeg", "-i", file_path, "-ar", "16000", "-ac", "1",
             "-f", "wav", "-y", wav_path],
            capture_output=True, timeout=ffmpeg_timeout, check=True,
        )

        async with httpx.AsyncClient(timeout=request_timeout) as client:
            with Path(wav_path).open("rb") as f:
                resp = await client.post(
                    endpoint,
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"response_format": "json", "language": language},
                )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            if not text:
                raise RuntimeError("Whisper returned empty transcription")
            return text
    finally:
        with contextlib.suppress(OSError):
            Path(wav_path).unlink()


# ─── Preprocessor hook ───────────────────────────────────────────


async def preprocess_audio(
    text: str, attachments: list, _config: Any,
) -> tuple[str, list]:
    """Transcribe audio attachments and append transcriptions to text.

    Claims audio/* attachments. Non-audio attachments pass through unchanged.
    """
    if not _stt_backend:
        return text, attachments

    remaining = []
    for att in attachments:
        if not att.content_type.startswith("audio/"):
            remaining.append(att)
            continue

        label = "voice message" if att.is_voice else "audio transcription"
        try:
            transcription = await transcribe(
                _stt_config, att.local_path, att.content_type,
            )
            result = f"[{label}, saved: {att.local_path}]: {transcription}"
        except Exception as e:
            log.error("STT failed (%s): %s", _stt_backend, e, exc_info=True)
            result = f"[{label} — transcription failed]"

        text = f"{text}\n{result}" if text else result

    return text, remaining


PREPROCESSORS = [
    {
        "name": "stt",
        "fn": preprocess_audio,
    },
]
