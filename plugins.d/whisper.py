"""Whisper speech-to-text preprocessor plugin.

Transcribes audio attachments before the agent sees them.
The agent receives the transcription as text — it never sees raw audio.

Two backends:
- ``openai``: OpenAI Whisper API via the ``openai`` SDK.
- ``local``: Self-hosted whisper.cpp server via httpx.

Requires: ``pip install openai`` for the cloud backend (declared as optional dep).
Configuration: ``plugins.d/whisper.toml`` (see ``whisper.toml.example``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import tomllib
import types
from pathlib import Path
from typing import Any

import metrics

log = logging.getLogger(__name__)

try:
    import openai as openai_sdk
except ImportError:
    openai_sdk = None  # type: ignore[assignment]

# Sentinel for metering — no tokens for duration-billed Whisper calls.
_ZERO_USAGE = types.SimpleNamespace(
    input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
)

# ─── Module config (set by configure()) ─────────────────────────

_backend: str = ""
_client: Any = None

# OpenAI backend config
_api_key: str = ""
_api_url: str = ""
_model: str = "whisper-1"
_timeout: int = 60
_retries: int = 2

# Local backend config
_local_endpoint: str = ""
_local_language: str = "auto"
_local_ffmpeg_timeout: int = 30
_local_request_timeout: int = 60

# Cost config
_cost_per_minute: float = 0.0
_cost_currency: str = "USD"
_metering: Any = None
_converter: Any = None


def configure(config: Any = None, metering: Any = None,
              converter: Any = None, **_: Any) -> None:
    """Load plugin config from whisper.toml and create SDK client."""
    global _backend, _client
    global _api_key, _api_url, _model, _timeout, _retries
    global _local_endpoint, _local_language, _local_ffmpeg_timeout, _local_request_timeout
    global _cost_per_minute, _cost_currency, _metering, _converter

    _metering = metering
    _converter = converter

    # Load plugin-local TOML config
    toml_path = Path(__file__).parent / "whisper.toml"
    if not toml_path.exists():
        log.debug("Whisper plugin: no whisper.toml found, plugin inactive")
        return

    with toml_path.open("rb") as f:
        cfg = tomllib.load(f)

    _backend = cfg.get("backend", "")
    if not _backend:
        log.debug("Whisper plugin: no backend configured, plugin inactive")
        return

    # OpenAI backend config
    openai_cfg = cfg.get("openai", {})
    api_key_env = openai_cfg.get("api_key_env", "")
    _api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    _api_url = openai_cfg.get("api_url",
                              "https://api.openai.com/v1/audio/transcriptions")
    _model = openai_cfg.get("model", "whisper-1")
    _timeout = openai_cfg.get("timeout", 60)
    _retries = openai_cfg.get("retries", 2)

    # Local backend config
    local_cfg = cfg.get("local", {})
    _local_endpoint = local_cfg.get("endpoint",
                                    "http://whisper-server:8082/inference")
    _local_language = local_cfg.get("language", "auto")
    _local_ffmpeg_timeout = local_cfg.get("ffmpeg_timeout", 30)
    _local_request_timeout = local_cfg.get("request_timeout", 60)

    # Cost config
    cost_cfg = cfg.get("cost", {})
    _cost_per_minute = cost_cfg.get("per_minute", 0.0)
    _cost_currency = cost_cfg.get("currency", "USD")

    # Create SDK client for OpenAI backend
    if _backend == "openai":
        if openai_sdk is None:
            log.warning("Whisper plugin: openai package not installed — "
                        "install with: pip install openai")
            return
        if not _api_key:
            log.warning("Whisper plugin: API key not configured "
                        "(env var: %s)", api_key_env)
            return
        # Extract base URL from api_url (strip the /audio/transcriptions path)
        base_url = _api_url
        for suffix in ("/audio/transcriptions", "/v1/audio/transcriptions"):
            if base_url.endswith(suffix):
                base_url = base_url[:-len(suffix)]
                break
        kwargs: dict[str, Any] = {
            "api_key": _api_key,
            "max_retries": _retries,
        }
        if base_url:
            kwargs["base_url"] = base_url
        _client = openai_sdk.AsyncOpenAI(**kwargs)
        log.info("Whisper plugin: AsyncOpenAI client initialized (model: %s)",
                 _model)

    elif _backend == "local":
        _validate_ffmpeg()
        log.info("Whisper plugin: local backend configured (endpoint: %s)",
                 _local_endpoint)
    else:
        log.warning("Whisper plugin: unknown backend %r", _backend)


# ─── Transcription ──────────────────────────────────────────────


async def transcribe(file_path: str, content_type: str) -> str:
    """Transcribe audio file using the configured backend.

    Returns transcribed text.

    Raises:
        RuntimeError: On unknown backend, missing SDK, or transcription failure.
    """
    if _backend == "openai":
        return await _transcribe_openai(file_path, content_type)
    if _backend == "local":
        return await _transcribe_local(file_path)
    raise RuntimeError(f"Unknown STT backend: {_backend!r}")


async def _transcribe_openai(file_path: str, content_type: str) -> str:
    """Transcribe audio via OpenAI Whisper SDK."""
    if _client is None:
        if openai_sdk is None:
            raise RuntimeError("OpenAI SDK not installed")
        raise RuntimeError("Whisper OpenAI client not configured (check API key)")

    audio_data = Path(file_path).read_bytes()
    filename = Path(file_path).name

    start = time.monotonic()
    response = await _client.audio.transcriptions.create(
        file=(filename, audio_data, content_type),
        model=_model,
        response_format="verbose_json",
        timeout=_timeout,
    )

    text = str(response.text).strip()
    if not text:
        raise RuntimeError("Whisper returned empty transcription")

    # ── Metrics + cost ──────────────────────────────────────────
    elapsed_ms = int((time.monotonic() - start) * 1000)

    # Duration from SDK response (verbose_json always includes .duration)
    duration_seconds = getattr(response, "duration", 0.0) or 0.0

    metrics.record_api_call(
        model=_model, provider="openai",
        usage=_ZERO_USAGE, latency_ms=elapsed_ms,
    )

    if _metering and _cost_per_minute > 0 and duration_seconds > 0:
        cost = (duration_seconds / 60) * _cost_per_minute
        _metering.record(
            session_id="", model=_model, provider="openai",
            usage=_ZERO_USAGE, cost_rates=[],
            call_type="transcription", cost_override=cost,
            currency=_cost_currency, converter=_converter,
        )

    return text


def _validate_ffmpeg() -> None:
    """Check that ffmpeg is available. Called at startup for local backend."""
    import shutil
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "Local STT requires ffmpeg but it is not installed. "
            "Install it (apt-get install ffmpeg) or use a cloud STT backend.",
        )


async def _transcribe_local(file_path: str) -> str:
    """Transcribe audio via local whisper.cpp server.

    Converts audio to WAV (16kHz mono) via ffmpeg, then POSTs
    to the whisper.cpp HTTP inference endpoint.
    """
    import subprocess
    import tempfile

    import httpx

    endpoint = _local_endpoint
    # Warn if non-localhost endpoint uses cleartext HTTP
    if endpoint.startswith("http://") and not any(
        h in endpoint for h in ("localhost", "127.0.0.1", "::1")
    ):
        log.warning("STT endpoint uses cleartext HTTP for non-localhost: %s",
                    endpoint)

    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    try:
        subprocess.run(
            ["ffmpeg", "-i", file_path, "-ar", "16000", "-ac", "1",
             "-f", "wav", "-y", wav_path],
            capture_output=True, timeout=_local_ffmpeg_timeout, check=True,
        )

        async with httpx.AsyncClient(timeout=_local_request_timeout) as client:
            with Path(wav_path).open("rb") as f:
                resp = await client.post(
                    endpoint,
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"response_format": "json",
                          "language": _local_language},
                )
            resp.raise_for_status()
            text = str(resp.json().get("text", "")).strip()
            if not text:
                raise RuntimeError("Whisper returned empty transcription")
            return text
    finally:
        with contextlib.suppress(OSError):
            Path(wav_path).unlink()


# ─── Preprocessor hook ──────────────────────────────────────────


async def preprocess_audio(
    text: str, attachments: list[Any], _config: Any,
) -> tuple[str, list[Any]]:
    """Transcribe audio attachments and append transcriptions to text.

    Claims audio/* attachments. Non-audio attachments pass through unchanged.
    """
    if not _backend:
        return text, attachments

    remaining = []
    for att in attachments:
        if not att.content_type.startswith("audio/"):
            remaining.append(att)
            continue

        label = "voice message" if att.is_voice else "audio transcription"
        try:
            transcription = await transcribe(att.local_path, att.content_type)
            result = f"[{label}, saved: {att.local_path}]: {transcription}"
        except Exception as e:
            log.error("STT failed (%s): %s", _backend, e, exc_info=True)
            result = f"[{label} — transcription failed]"

        text = f"{text}\n{result}" if text else result

    return text, remaining


PREPROCESSORS = [
    {
        "name": "stt",
        "fn": preprocess_audio,
    },
]
