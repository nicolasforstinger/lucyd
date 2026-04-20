"""Mistral Voxtral text-to-speech plugin.

Generates speech audio from text using the Mistral /v1/audio/speech API.
Returns an audio file as an attachment — the framework delivers it via
the connected channel bridge.

Configuration: ``plugins.d/mistral_tts.toml`` (see ``mistral_tts.toml.example``).

On failure raises :class:`plugins.PluginError` subclasses — the tool
registry translates to agent-safe text.
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

import metrics
from async_utils import run_blocking
from plugins import (
    PluginAuth,
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
from tools import ToolSpec

if TYPE_CHECKING:
    from config import Config
    from conversion import CurrencyConverter
    from metering import MeteringDB

log = logging.getLogger(__name__)


# ─── Enrichment metric ──────────────────────────────────────────

if metrics.ENABLED:
    from prometheus_client import Counter

    CHARS_TOTAL = Counter(
        "lucyd_plugin_mistral_tts_chars_total",
        "Characters synthesized via Mistral TTS",
        ["voice", "model"],
    )


# ─── Module config (set by configure()) ─────────────────────────

_api_key: str = ""
_api_url: str = "https://api.mistral.ai/v1/audio/speech"
_default_voice: str = ""
_default_model: str = "voxtral-mini-tts-2603"
_timeout: int = 60
_output_dir: str = ""
_cost_per_1k_chars: float = 0.0
_cost_currency: str = "USD"
_metering: MeteringDB | None = None
_converter: CurrencyConverter | None = None


def configure(
    config: Config | None = None,
    metering: MeteringDB | None = None,
    converter: CurrencyConverter | None = None,
    **_: object,
) -> None:
    """Load plugin config from mistral_tts.toml."""
    global _api_key, _api_url, _default_voice, _default_model
    global _timeout, _output_dir, _cost_per_1k_chars, _cost_currency
    global _metering, _converter

    _metering = metering
    _converter = converter

    toml_path = Path(__file__).parent / "mistral_tts.toml"
    if not toml_path.exists():
        log.debug("Mistral TTS plugin: no mistral_tts.toml found, plugin inactive")
        mark_unconfigured("mistral_tts")
        return

    with toml_path.open("rb") as f:
        cfg = tomllib.load(f)

    api_key_env = cfg.get("api_key_env", "LUCYD_MISTRAL_KEY")
    _api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    _api_url = cfg.get("api_url", "https://api.mistral.ai/v1/audio/speech")
    _default_voice = cfg.get("default_voice", "")
    _default_model = cfg.get("default_model", "voxtral-mini-tts-2603")
    _timeout = cfg.get("timeout", 60)

    cost_cfg = cfg.get("cost", {})
    _cost_per_1k_chars = cost_cfg.get("per_1k_chars", 0.0)
    _cost_currency = cost_cfg.get("currency", "USD")

    if config is not None:
        _output_dir = str(config.http_download_dir)
        Path(_output_dir).mkdir(parents=True, exist_ok=True)

    if not _api_key:
        log.warning("Mistral TTS plugin: API key not configured "
                    "(env var: %s)", api_key_env)
        mark_unconfigured("mistral_tts")
        return

    log.info("Mistral TTS plugin: initialized (model: %s, voice: %s)",
             _default_model, _default_voice)
    mark_configured("mistral_tts", backend="mistral")


# ─── Tool function ──────────────────────────────────────────────


async def tool_tts(
    text: str, voice: str = "", model: str = "",
    output_file: str = "",
) -> dict[str, Any]:
    """Generate speech audio from text."""
    if not _api_key:
        raise PluginNotConfigured("Mistral TTS not configured")

    voice = voice or _default_voice
    model = model or _default_model

    if not voice:
        raise PluginInvalidInput("No voice specified and no default configured")

    if output_file:
        from tools.filesystem import _check_path
        err = _check_path(output_file)
        if err:
            raise PluginInvalidInput(f"Output path not allowed: {output_file}")

    result = await run_plugin_op(
        "mistral_tts", "tts",
        _tts_impl, text, voice, model, output_file,
    )
    return result


async def _tts_impl(
    text: str, voice: str, model: str, output_file: str,
) -> dict[str, Any]:
    """Call Mistral TTS API, translate errors to PluginError subclasses."""
    if not output_file:
        fd, output_file = tempfile.mkstemp(
            suffix=".mp3", prefix="lucyd-tts-", dir=_output_dir or None,
        )
        os.close(fd)

    start = time.monotonic()

    def _request() -> bytes:
        resp = httpx.post(
            _api_url,
            headers={
                "Authorization": f"Bearer {_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "input": text, "voice": voice},
            timeout=_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return base64.b64decode(data["audio_data"])

    try:
        audio: bytes = await run_blocking(_request)
    except httpx.TimeoutException as e:
        raise PluginTransient(f"Mistral TTS timed out: {e}") from e
    except httpx.ConnectError as e:
        raise PluginTransient(f"Mistral TTS unreachable: {e}") from e
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            raise PluginAuth(f"Mistral TTS auth rejected: {status}") from e
        if status == 429:
            raise PluginQuota(f"Mistral TTS rate limited: {status}") from e
        if status in (400, 422):
            raise PluginInvalidInput(f"Mistral TTS rejected request: {status}") from e
        raise PluginUpstream(f"Mistral TTS HTTP {status}") from e
    except httpx.HTTPError as e:
        raise PluginUpstream(f"Mistral TTS HTTP error: {e}") from e

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    output_path.chmod(0o600)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    metrics.record_api_call(
        model=model, provider="mistral",
        usage=Usage(), latency_ms=elapsed_ms,
    )

    if metrics.ENABLED:
        CHARS_TOTAL.labels(voice=voice, model=model).inc(len(text))

    if _metering and _cost_per_1k_chars > 0:
        cost = len(text) / 1000 * _cost_per_1k_chars
        await _metering.record(
            session_id="", model=model, provider="mistral",
            usage=Usage(), cost_rates=[],
            call_type="tts", cost_override=cost,
            currency=_cost_currency, converter=_converter,
        )

    return {"text": f"Voice message sent ({len(audio)} bytes)",
            "attachments": [output_file]}


# ─── Tool registration ──────────────────────────────────────────

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="tts",
        description="Generate and send a voice message.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to convert to speech"},
                "voice": {"type": "string",
                          "description": "Voice slug (default: from config)"},
                "model": {"type": "string",
                           "description": "Model ID (default: from config)"},
            },
            "required": ["text"],
        },
        function=tool_tts,
    ),
]
