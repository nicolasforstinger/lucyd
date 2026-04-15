"""Mistral Voxtral text-to-speech plugin.

Generates speech audio from text using the Mistral /v1/audio/speech API.
Returns an audio file as an attachment — the framework delivers it via
the connected channel bridge.

Configuration: ``plugins.d/mistral_tts.toml`` (see ``mistral_tts.toml.example``).
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import metrics
from providers import Usage
from tools import ToolSpec

log = logging.getLogger(__name__)

# ─── Module config (set by configure()) ─────────────────────────

_api_key: str = ""
_api_url: str = "https://api.mistral.ai/v1/audio/speech"
_default_voice: str = ""
_default_model: str = "voxtral-mini-tts-2603"
_timeout: int = 60
_output_dir: str = ""
_cost_per_1k_chars: float = 0.0
_cost_currency: str = "USD"
_metering: Any = None
_converter: Any = None


def configure(config: Any = None, metering: Any = None,
              converter: Any = None, **_: Any) -> None:
    """Load plugin config from mistral_tts.toml."""
    global _api_key, _api_url, _default_voice, _default_model
    global _timeout, _output_dir, _cost_per_1k_chars, _cost_currency
    global _metering, _converter

    _metering = metering
    _converter = converter

    toml_path = Path(__file__).parent / "mistral_tts.toml"
    if not toml_path.exists():
        log.debug("Mistral TTS plugin: no mistral_tts.toml found, plugin inactive")
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
        log.warning("Mistral TTS plugin: API key not configured (env var: %s)", api_key_env)
        return

    log.info("Mistral TTS plugin: initialized (model: %s, voice: %s)",
             _default_model, _default_voice)


# ─── Tool function ──────────────────────────────────────────────


async def tool_tts(text: str, voice: str = "", model: str = "",
                   output_file: str = "") -> dict[str, Any]:
    """Generate speech audio from text. Returns structured result with file attachment."""
    if not _api_key:
        return {"text": "Error: Mistral TTS not configured (check API key and mistral_tts.toml)",
                "attachments": []}

    voice = voice or _default_voice
    model = model or _default_model

    if not voice:
        return {"text": "Error: No voice specified and no default configured",
                "attachments": []}

    if output_file:
        from tools.filesystem import _check_path
        err = _check_path(output_file)
        if err:
            return {"text": f"Error: Output path not allowed: {output_file}",
                    "attachments": []}

    if not output_file:
        fd, output_file = tempfile.mkstemp(
            suffix=".mp3", prefix="lucyd-tts-", dir=_output_dir or None,
        )
        os.close(fd)

    start = time.monotonic()
    try:
        from async_utils import run_blocking

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

        audio: bytes = await run_blocking(_request)

        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio)
        output_path.chmod(0o600)
    except Exception as e:
        log.error("Mistral TTS failed: %s", e, exc_info=True)
        return {"text": f"Error: TTS generation failed: {type(e).__name__}",
                "attachments": []}

    # ── Metrics + cost ──────────────────────────────────────────
    elapsed_ms = int((time.monotonic() - start) * 1000)
    metrics.record_api_call(
        model=model, provider="mistral",
        usage=Usage(), latency_ms=elapsed_ms,
    )

    if _metering and _cost_per_1k_chars > 0:
        cost = len(text) / 1000 * _cost_per_1k_chars
        await _metering.record(
            session_id="", model=model, provider="mistral",
            usage=Usage(), cost_rates=[],
            call_type="tts", cost_override=cost,
            currency=_cost_currency, converter=_converter,
        )

    return {"text": f"Voice message sent ({len(audio)} bytes)", "attachments": [output_file]}


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
