"""Whisper speech-to-text preprocessor plugin.

Transcribes audio attachments before the agent sees them.
The agent receives the transcription as text — it never sees raw audio.

Two backends:
- ``openai``: OpenAI Whisper API via the ``openai`` SDK.
- ``local``: Self-hosted whisper.cpp server via httpx.

Requires: ``pip install openai`` for the cloud backend (declared as optional dep).
Configuration: ``plugins.d/whisper.toml`` (see ``whisper.toml.example``).

On failure raises :class:`plugins.PluginError` subclasses — the preprocessor
dispatch in ``pipeline.py`` logs, emits metrics, and falls back to the
registered ``fallback_text``. The agent never sees SDK exception detail.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any  # Any: _client holds an optional SDK instance

import httpx

import metrics
from plugins import (
    PluginAuth,
    PluginEmptyOutput,
    PluginInvalidInput,
    PluginNotConfigured,
    PluginQuota,
    PluginTransient,
    PluginUpstream,
    mark_configured,
    mark_unconfigured,
    run_plugin_op,
)
from providers import Usage

if TYPE_CHECKING:
    from config import Config
    from conversion import CurrencyConverter
    from metering import MeteringDB

log = logging.getLogger(__name__)

try:
    import openai as openai_sdk
except ImportError:
    openai_sdk = None  # type: ignore[assignment]


# ─── Enrichment metric ──────────────────────────────────────────

if metrics.ENABLED:
    from prometheus_client import Histogram

    AUDIO_DURATION = Histogram(
        "lucyd_plugin_whisper_audio_duration_seconds",
        "Duration of audio transcribed (seconds), from provider response",
        ["backend"],
        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
    )


# ─── Module config (set by configure()) ─────────────────────────

_backend: str = ""
_client: Any = None  # openai.AsyncOpenAI — optional SDK import, None until configured

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
_metering: MeteringDB | None = None
_converter: CurrencyConverter | None = None


def configure(
    config: Config | None = None,
    metering: MeteringDB | None = None,
    converter: CurrencyConverter | None = None,
    **_: object,
) -> None:
    """Load plugin config from whisper.toml and create SDK client."""
    global _backend, _client
    global _api_key, _api_url, _model, _timeout, _retries
    global _local_endpoint, _local_language, _local_ffmpeg_timeout, _local_request_timeout
    global _cost_per_minute, _cost_currency, _metering, _converter

    _metering = metering
    _converter = converter

    toml_path = Path(__file__).parent / "whisper.toml"
    if not toml_path.exists():
        log.debug("Whisper plugin: no whisper.toml found, plugin inactive")
        mark_unconfigured("whisper")
        return

    with toml_path.open("rb") as f:
        cfg = tomllib.load(f)

    _backend = cfg.get("backend", "")
    if not _backend:
        log.debug("Whisper plugin: no backend configured, plugin inactive")
        mark_unconfigured("whisper")
        return

    openai_cfg = cfg.get("openai", {})
    api_key_env = openai_cfg.get("api_key_env", "")
    _api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    _api_url = openai_cfg.get("api_url",
                              "https://api.openai.com/v1/audio/transcriptions")
    _model = openai_cfg.get("model", "whisper-1")
    _timeout = openai_cfg.get("timeout", 60)
    _retries = openai_cfg.get("retries", 2)

    local_cfg = cfg.get("local", {})
    _local_endpoint = local_cfg.get("endpoint",
                                    "http://whisper-server:8082/inference")
    _local_language = local_cfg.get("language", "auto")
    _local_ffmpeg_timeout = local_cfg.get("ffmpeg_timeout", 30)
    _local_request_timeout = local_cfg.get("request_timeout", 60)

    cost_cfg = cfg.get("cost", {})
    _cost_per_minute = cost_cfg.get("per_minute", 0.0)
    _cost_currency = cost_cfg.get("currency", "USD")

    if _backend == "openai":
        if openai_sdk is None:
            log.warning("Whisper plugin: openai package not installed — "
                        "install with: pip install openai")
            mark_unconfigured("whisper", backend="openai")
            return
        if not _api_key:
            log.warning("Whisper plugin: API key not configured "
                        "(env var: %s)", api_key_env)
            mark_unconfigured("whisper", backend="openai")
            return
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
        mark_configured("whisper", backend="openai")
    elif _backend == "local":
        _validate_ffmpeg()
        log.info("Whisper plugin: local backend configured (endpoint: %s)",
                 _local_endpoint)
        mark_configured("whisper", backend="local")
    else:
        log.warning("Whisper plugin: unknown backend %r", _backend)
        mark_unconfigured("whisper", backend=_backend)


# ─── Transcription ──────────────────────────────────────────────


async def transcribe(file_path: str, content_type: str) -> str:
    """Transcribe audio via the configured backend.

    Wraps the backend-specific implementation in :func:`run_plugin_op`
    so the call is counted, timed, and retried per framework policy.
    """
    if _backend == "openai":
        return await run_plugin_op(
            "whisper", "transcribe",
            _transcribe_openai, file_path, content_type,
        )
    if _backend == "local":
        return await run_plugin_op(
            "whisper", "transcribe",
            _transcribe_local, file_path,
        )
    raise PluginNotConfigured(f"Unknown backend: {_backend!r}")


async def _transcribe_openai(file_path: str, content_type: str) -> str:
    """Call OpenAI Whisper SDK, translate errors to PluginError subclasses."""
    if _client is None:
        raise PluginNotConfigured("OpenAI client not initialized")

    audio_data = Path(file_path).read_bytes()
    filename = Path(file_path).name

    start = time.monotonic()
    try:
        response = await _client.audio.transcriptions.create(
            file=(filename, audio_data, content_type),
            model=_model,
            response_format="verbose_json",
            timeout=_timeout,
        )
    except openai_sdk.AuthenticationError as e:
        raise PluginAuth(str(e)) from e
    except openai_sdk.PermissionDeniedError as e:
        raise PluginAuth(str(e)) from e
    except openai_sdk.RateLimitError as e:
        raise PluginQuota(str(e)) from e
    except (openai_sdk.APITimeoutError, openai_sdk.APIConnectionError) as e:
        raise PluginTransient(str(e)) from e
    except (openai_sdk.BadRequestError, openai_sdk.UnprocessableEntityError) as e:
        raise PluginInvalidInput(str(e)) from e
    except openai_sdk.APIError as e:
        raise PluginUpstream(str(e)) from e

    text = str(response.text).strip()
    if not text:
        raise PluginEmptyOutput("Whisper returned empty transcription")

    elapsed_ms = int((time.monotonic() - start) * 1000)
    duration_seconds = float(getattr(response, "duration", 0.0) or 0.0)

    if metrics.ENABLED and duration_seconds > 0:
        AUDIO_DURATION.labels(backend="openai").observe(duration_seconds)

    metrics.record_api_call(
        model=_model, provider="openai",
        usage=Usage(), latency_ms=elapsed_ms,
    )

    if _metering and _cost_per_minute > 0 and duration_seconds > 0:
        cost = (duration_seconds / 60) * _cost_per_minute
        await _metering.record(
            session_id="", model=_model, provider="openai",
            usage=Usage(), cost_rates=[],
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
    """Transcribe via local whisper.cpp, translate errors to PluginError."""
    endpoint = _local_endpoint
    if endpoint.startswith("http://") and not any(
        h in endpoint for h in ("localhost", "127.0.0.1", "::1")
    ):
        log.warning("STT endpoint uses cleartext HTTP for non-localhost: %s",
                    endpoint)

    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    try:
        try:
            subprocess.run(
                ["ffmpeg", "-i", file_path, "-ar", "16000", "-ac", "1",
                 "-f", "wav", "-y", wav_path],
                capture_output=True, timeout=_local_ffmpeg_timeout, check=True,
            )
        except subprocess.TimeoutExpired as e:
            raise PluginTransient(f"ffmpeg timed out: {e}") from e
        except subprocess.CalledProcessError as e:
            raise PluginUpstream(f"ffmpeg failed: {e}") from e

        try:
            async with httpx.AsyncClient(timeout=_local_request_timeout) as client:
                with Path(wav_path).open("rb") as f:
                    resp = await client.post(
                        endpoint,
                        files={"file": ("audio.wav", f, "audio/wav")},
                        data={"response_format": "json",
                              "language": _local_language},
                    )
                resp.raise_for_status()
        except httpx.TimeoutException as e:
            raise PluginTransient(f"local whisper timed out: {e}") from e
        except httpx.ConnectError as e:
            raise PluginTransient(f"local whisper unreachable: {e}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                raise PluginAuth(f"local whisper auth rejected: {status}") from e
            if status == 429:
                raise PluginQuota(f"local whisper rate limited: {status}") from e
            raise PluginUpstream(f"local whisper HTTP {status}") from e
        except httpx.HTTPError as e:
            raise PluginUpstream(f"local whisper HTTP error: {e}") from e

        text = str(resp.json().get("text", "")).strip()
        if not text:
            raise PluginEmptyOutput("local whisper returned empty transcription")

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
    Raises :class:`plugins.PluginError` on failure — preprocessor dispatch
    logs, emits metrics, and applies the registered ``fallback_text``.
    """
    if not _backend:
        return text, attachments

    remaining: list[Any] = []
    for att in attachments:
        if not att.content_type.startswith("audio/"):
            remaining.append(att)
            continue

        label = "voice message" if att.is_voice else "audio transcription"
        transcription = await transcribe(att.local_path, att.content_type)
        result = f"[{label}, saved: {att.local_path}]: {transcription}"
        text = f"{text}\n{result}" if text else result

    return text, remaining


PREPROCESSORS = [
    {
        "name": "stt",
        "fn": preprocess_audio,
        "critical": True,
        "fallback_text": "[voice message received — transcription unavailable]",
    },
]
