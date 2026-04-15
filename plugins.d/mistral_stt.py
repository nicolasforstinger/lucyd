"""Mistral Voxtral speech-to-text preprocessor plugin.

Transcribes audio attachments before the agent sees them.
The agent receives the transcription as text — it never sees raw audio.

Uses the Mistral SDK's ``audio.transcriptions`` API.

Requires: ``pip install mistralai`` (declared as optional dep in pyproject.toml).
Configuration: ``plugins.d/mistral_stt.toml`` (see ``mistral_stt.toml.example``).
"""

from __future__ import annotations

import logging
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import metrics
from providers import Usage

log = logging.getLogger(__name__)

try:
    from mistralai import Mistral
except ImportError:
    Mistral = None  # type: ignore[misc,assignment]

# ─── Module config (set by configure()) ─────────────────────────

_client: Any = None
_model: str = "voxtral-mini-latest"
_timeout: int = 60
_cost_per_minute: float = 0.0
_cost_currency: str = "USD"
_metering: Any = None
_converter: Any = None


def configure(config: Any = None, metering: Any = None,
              converter: Any = None, **_: Any) -> None:
    """Load plugin config from mistral_stt.toml and create SDK client."""
    global _client, _model, _timeout
    global _cost_per_minute, _cost_currency, _metering, _converter

    _metering = metering
    _converter = converter

    toml_path = Path(__file__).parent / "mistral_stt.toml"
    if not toml_path.exists():
        log.debug("Mistral STT plugin: no mistral_stt.toml found, plugin inactive")
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
        return
    if not api_key:
        log.warning("Mistral STT plugin: API key not configured (env var: %s)", api_key_env)
        return

    _client = Mistral(api_key=api_key)
    log.info("Mistral STT plugin: initialized (model: %s)", _model)


# ─── Transcription ──────────────────────────────────────────────


async def transcribe(file_path: str, content_type: str = "") -> str:
    """Transcribe audio file using Mistral Voxtral."""
    if _client is None:
        raise RuntimeError("Mistral STT not configured")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    start = time.monotonic()

    from async_utils import run_blocking
    from mistralai.models import File

    def _transcribe() -> str:
        with path.open("rb") as f:
            audio_data = f.read()
        file_obj = File(
            file_name=path.name,
            content=audio_data,
        )
        result = _client.audio.transcriptions.complete(
            model=_model,
            file=file_obj,
        )
        return result.text or ""

    text = await run_blocking(_transcribe)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if not text.strip():
        raise RuntimeError("Mistral STT returned empty transcription")

    # ── Metrics + cost ──────────────────────────────────────────
    metrics.record_api_call(
        model=_model, provider="mistral",
        usage=Usage(), latency_ms=elapsed_ms,
    )

    # Estimate duration from file size (rough: ~16kB/s for OGG voice)
    file_size = path.stat().st_size
    duration_seconds = file_size / 16000

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
    """Transcribe audio attachments and append transcriptions to text.

    Claims audio/* attachments. Non-audio attachments pass through unchanged.
    """
    if _client is None:
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
            log.error("Mistral STT failed: %s", e, exc_info=True)
            result = f"[{label} — transcription failed]"

        text = f"{text}\n{result}" if text else result

    return text, remaining


PREPROCESSORS = [
    {
        "name": "stt",
        "fn": preprocess_audio,
    },
]
