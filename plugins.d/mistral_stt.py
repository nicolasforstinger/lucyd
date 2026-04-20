"""Mistral Voxtral speech-to-text preprocessor plugin.

Transcribes audio attachments before the agent sees them.
The agent receives the transcription as text — it never sees raw audio.

Uses the Mistral SDK's ``audio.transcriptions`` API.

Requires: ``pip install mistralai`` (declared as optional dep in pyproject.toml).
Configuration: ``plugins.d/mistral_stt.toml`` (see ``mistral_stt.toml.example``).

On failure raises :class:`plugins.PluginError` subclasses — the preprocessor
dispatch in ``pipeline.py`` logs, emits metrics, and applies the registered
``fallback_text``. Agent never sees SDK exception detail.
"""

from __future__ import annotations

import logging
import os
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any  # Any: _client holds an optional SDK instance

import httpx

import metrics
from async_utils import run_blocking
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
    from mistralai import Mistral
    from mistralai.models import File
except ImportError:
    Mistral = None  # type: ignore[misc,assignment]  # optional SDK fallback
    File = None  # type: ignore[misc,assignment]  # optional SDK fallback

try:
    from mistralai.models.sdkerror import SDKError as _MistralSDKError
except ImportError:
    _MistralSDKError = None  # type: ignore[misc,assignment]  # optional SDK fallback


# ─── Enrichment metric ──────────────────────────────────────────

if metrics.ENABLED:
    from prometheus_client import Histogram

    AUDIO_DURATION = Histogram(
        "lucyd_plugin_mistral_stt_audio_duration_seconds",
        "Duration of audio transcribed (seconds), estimated from file size",
        ["backend"],
        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
    )


# ─── Module config (set by configure()) ─────────────────────────

_client: Any = None  # mistralai.Mistral — optional SDK import, None until configured
_model: str = "voxtral-mini-latest"
_timeout: int = 60
_cost_per_minute: float = 0.0
_cost_currency: str = "USD"
_metering: MeteringDB | None = None
_converter: CurrencyConverter | None = None

# Audio-duration estimate constant (bytes/sec for typical OGG voice ~16kb/s)
# Used only when the SDK doesn't return duration and cost tracking needs
# a rough number. Kept separate from magic-in-the-function for clarity.
_OGG_VOICE_BYTES_PER_SECOND = 16000


def configure(
    config: Config | None = None,
    metering: MeteringDB | None = None,
    converter: CurrencyConverter | None = None,
    **_: object,
) -> None:
    """Load plugin config from mistral_stt.toml and create SDK client."""
    global _client, _model, _timeout
    global _cost_per_minute, _cost_currency, _metering, _converter

    _metering = metering
    _converter = converter

    toml_path = Path(__file__).parent / "mistral_stt.toml"
    if not toml_path.exists():
        log.debug("Mistral STT plugin: no mistral_stt.toml found, plugin inactive")
        mark_unconfigured("mistral_stt")
        return

    with toml_path.open("rb") as f:
        cfg = tomllib.load(f)

    api_key_env = cfg.get("api_key_env", "LUCYD_MISTRAL_KEY")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    _model = cfg.get("model", "voxtral-mini-latest")
    _timeout = cfg.get("timeout", 60)

    cost_cfg = cfg.get("cost", {})
    _cost_per_minute = cost_cfg.get("per_minute", 0.0)
    _cost_currency = cost_cfg.get("currency", "USD")

    if Mistral is None:
        log.warning("Mistral STT plugin: mistralai package not installed — "
                    "install with: pip install mistralai")
        mark_unconfigured("mistral_stt")
        return
    if not api_key:
        log.warning("Mistral STT plugin: API key not configured "
                    "(env var: %s)", api_key_env)
        mark_unconfigured("mistral_stt")
        return

    _client = Mistral(api_key=api_key)
    log.info("Mistral STT plugin: initialized (model: %s)", _model)
    mark_configured("mistral_stt", backend="mistral")


# ─── Transcription ──────────────────────────────────────────────


async def transcribe(file_path: str, content_type: str = "") -> str:
    """Transcribe audio using Mistral Voxtral.

    Wraps the implementation in :func:`run_plugin_op` so the call is
    counted, timed, and retried per framework policy.
    """
    if _client is None:
        raise PluginNotConfigured("Mistral STT client not initialized")
    return await run_plugin_op(
        "mistral_stt", "transcribe",
        _transcribe_impl, file_path,
    )


def _translate_sdk_error(e: Exception) -> None:
    """Translate a Mistral SDK exception into a PluginError and raise.

    Uses ``status_code`` if the SDK surfaces it; otherwise falls back to
    :class:`PluginUpstream` (retryable). Never returns.
    """
    status = getattr(e, "status_code", 0)
    if status in (401, 403):
        raise PluginAuth(f"Mistral auth rejected: {status}") from e
    if status == 429:
        raise PluginQuota(f"Mistral rate limited: {status}") from e
    if status in (400, 422):
        raise PluginInvalidInput(f"Mistral rejected request: {status}") from e
    if 500 <= status < 600:
        raise PluginUpstream(f"Mistral server error: {status}") from e
    raise PluginUpstream(f"Mistral SDK error: {type(e).__name__}") from e


async def _transcribe_impl(file_path: str) -> str:
    """Call Mistral SDK, translate errors to PluginError subclasses."""
    path = Path(file_path)
    if not path.exists():
        raise PluginInvalidInput(f"Audio file not found: {file_path}")

    start = time.monotonic()

    def _call() -> str:
        assert File is not None  # established by _client is not None check above
        with path.open("rb") as f:
            audio_data = f.read()
        file_obj = File(file_name=path.name, content=audio_data)
        result = _client.audio.transcriptions.complete(
            model=_model,
            file=file_obj,
        )
        return str(result.text or "")

    try:
        text = await run_blocking(_call)
    except httpx.TimeoutException as e:
        raise PluginTransient(f"Mistral request timed out: {e}") from e
    except httpx.ConnectError as e:
        raise PluginTransient(f"Mistral unreachable: {e}") from e
    except Exception as e:  # noqa: BLE001 — mistralai SDK exception types unstable; see _translate_sdk_error
        if _MistralSDKError is not None and isinstance(e, _MistralSDKError):
            _translate_sdk_error(e)
        raise PluginUpstream(f"Mistral call failed: {type(e).__name__}") from e

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if not text.strip():
        raise PluginEmptyOutput("Mistral STT returned empty transcription")

    metrics.record_api_call(
        model=_model, provider="mistral",
        usage=Usage(), latency_ms=elapsed_ms,
    )

    # Audio duration estimate (SDK doesn't return it). Useful for cost
    # tracking and the enrichment histogram. Accuracy depends on codec —
    # reasonable for OGG voice, not accurate for FLAC/WAV.
    file_size = path.stat().st_size
    duration_seconds = file_size / _OGG_VOICE_BYTES_PER_SECOND

    if metrics.ENABLED and duration_seconds > 0:
        AUDIO_DURATION.labels(backend="mistral").observe(duration_seconds)

    if _metering and _cost_per_minute > 0 and duration_seconds > 0:
        cost = (duration_seconds / 60) * _cost_per_minute
        await _metering.record(
            session_id="", model=_model, provider="mistral",
            usage=Usage(), cost_rates=[],
            call_type="transcription", cost_override=cost,
            currency=_cost_currency, converter=_converter,
        )

    return text


# ─── Preprocessor hook ──────────────────────────────────────────


async def preprocess_audio(
    text: str, attachments: list[Any], _config: Any,
) -> tuple[str, list[Any]]:
    """Transcribe audio attachments; raise PluginError on failure.

    Dispatch logs, emits metrics, and applies registered fallback_text.
    """
    if _client is None:
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
    },
]
