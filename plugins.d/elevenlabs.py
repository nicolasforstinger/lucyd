"""ElevenLabs text-to-speech plugin.

Generates speech audio from text using the ElevenLabs SDK.
Returns an audio file as an attachment — the framework delivers it via
the connected channel bridge.

Requires: ``pip install elevenlabs`` (declared as optional dep in pyproject.toml).
Configuration: ``plugins.d/elevenlabs.toml`` (see ``elevenlabs.toml.example``).

On failure raises :class:`plugins.PluginError` subclasses — the tool
registry translates to agent-safe text. Agent never sees SDK detail.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any  # Any: _client holds an optional SDK instance

import httpx

import metrics
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

try:
    from elevenlabs.client import AsyncElevenLabs
    from elevenlabs.types import VoiceSettings
except ImportError:
    AsyncElevenLabs = None  # type: ignore[misc,assignment]
    VoiceSettings = None  # type: ignore[misc,assignment]


# ─── Enrichment metric ──────────────────────────────────────────

if metrics.ENABLED:
    from prometheus_client import Counter

    CHARS_TOTAL = Counter(
        "lucyd_plugin_elevenlabs_chars_total",
        "Characters synthesized via ElevenLabs TTS",
        ["voice", "model"],
    )


# ─── Module config (set by configure()) ─────────────────────────

_client: Any = None  # elevenlabs.AsyncElevenLabs — optional SDK, None until configured
_api_url: str = ""
_default_voice_id: str = ""
_default_model_id: str = ""
_timeout: int = 60
_voice_speed: float = 1.0
_voice_stability: float = 0.5
_voice_similarity_boost: float = 0.75
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
    """Load plugin config from elevenlabs.toml and create SDK client."""
    global _client, _api_url, _default_voice_id, _default_model_id
    global _timeout, _voice_speed, _voice_stability, _voice_similarity_boost
    global _output_dir, _cost_per_1k_chars, _cost_currency
    global _metering, _converter

    _metering = metering
    _converter = converter

    toml_path = Path(__file__).parent / "elevenlabs.toml"
    if not toml_path.exists():
        log.debug("ElevenLabs plugin: no elevenlabs.toml found, plugin inactive")
        mark_unconfigured("elevenlabs")
        return

    with toml_path.open("rb") as f:
        cfg = tomllib.load(f)

    api_key_env = cfg.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""

    _api_url = cfg.get("api_url", "")
    _default_voice_id = cfg.get("default_voice_id", "")
    _default_model_id = cfg.get("default_model_id", "eleven_v3")
    _timeout = cfg.get("timeout", 60)

    voice_cfg = cfg.get("voice", {})
    _voice_speed = voice_cfg.get("speed", 1.0)
    _voice_stability = voice_cfg.get("stability", 0.5)
    _voice_similarity_boost = voice_cfg.get("similarity_boost", 0.75)

    cost_cfg = cfg.get("cost", {})
    _cost_per_1k_chars = cost_cfg.get("per_1k_chars", 0.0)
    _cost_currency = cost_cfg.get("currency", "USD")

    if config is not None:
        _output_dir = str(config.http_download_dir)
        Path(_output_dir).mkdir(parents=True, exist_ok=True)

    if AsyncElevenLabs is None:
        log.warning("ElevenLabs plugin: elevenlabs package not installed — "
                    "install with: pip install elevenlabs")
        mark_unconfigured("elevenlabs")
        return
    if not api_key:
        log.warning("ElevenLabs plugin: API key not configured "
                    "(env var: %s)", api_key_env)
        mark_unconfigured("elevenlabs")
        return

    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": _timeout}
    if _api_url and not _api_url.startswith("https://api.elevenlabs.io"):
        base = _api_url.split("/v1/")[0] if "/v1/" in _api_url else _api_url.rstrip("/")
        kwargs["base_url"] = base
    _client = AsyncElevenLabs(**kwargs)
    log.info("ElevenLabs plugin: SDK client initialized")
    mark_configured("elevenlabs", backend="elevenlabs")


# ─── Tool function ──────────────────────────────────────────────


async def tool_tts(
    text: str, voice_id: str = "",
    model_id: str = "", output_file: str = "",
) -> dict[str, Any]:
    """Generate speech audio from text. Returns dict with file attachment."""
    if _client is None:
        raise PluginNotConfigured("ElevenLabs not configured")

    voice_id = voice_id or _default_voice_id
    model_id = model_id or _default_model_id

    if not voice_id:
        raise PluginInvalidInput("No voice_id specified and no default configured")

    if output_file:
        from tools.filesystem import _check_path
        err = _check_path(output_file)
        if err:
            raise PluginInvalidInput(f"Output path not allowed: {output_file}")

    result = await run_plugin_op(
        "elevenlabs", "tts",
        _tts_impl, text, voice_id, model_id, output_file,
    )
    return result


async def _tts_impl(
    text: str, voice_id: str, model_id: str, output_file: str,
) -> dict[str, Any]:
    """Call ElevenLabs SDK, translate errors to PluginError subclasses."""
    if not output_file:
        fd, output_file = tempfile.mkstemp(
            suffix=".mp3", prefix="lucyd-tts-", dir=_output_dir or None,
        )
        os.close(fd)

    start = time.monotonic()
    try:
        voice_settings = VoiceSettings(
            speed=_voice_speed,
            stability=_voice_stability,
            similarity_boost=_voice_similarity_boost,
        )
        chunks: list[bytes] = []
        async for chunk in _client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id=model_id,
            output_format="mp3_44100_128",
            voice_settings=voice_settings,
        ):
            chunks.append(chunk)
        audio = b"".join(chunks)
    except httpx.TimeoutException as e:
        raise PluginTransient(f"ElevenLabs timed out: {e}") from e
    except httpx.ConnectError as e:
        raise PluginTransient(f"ElevenLabs unreachable: {e}") from e
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            raise PluginAuth(f"ElevenLabs auth rejected: {status}") from e
        if status == 429:
            raise PluginQuota(f"ElevenLabs rate limited: {status}") from e
        if status in (400, 422):
            raise PluginInvalidInput(f"ElevenLabs rejected request: {status}") from e
        raise PluginUpstream(f"ElevenLabs HTTP {status}") from e
    except Exception as e:  # noqa: BLE001 — elevenlabs SDK exception types unstable
        raise PluginUpstream(f"ElevenLabs call failed: {type(e).__name__}") from e

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    output_path.chmod(0o600)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    metrics.record_api_call(
        model=model_id, provider="elevenlabs",
        usage=Usage(), latency_ms=elapsed_ms,
    )

    if metrics.ENABLED:
        CHARS_TOTAL.labels(voice=voice_id, model=model_id).inc(len(text))

    if _metering and _cost_per_1k_chars > 0:
        cost = len(text) / 1000 * _cost_per_1k_chars
        await _metering.record(
            session_id="", model=model_id, provider="elevenlabs",
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
                "voice_id": {"type": "string",
                             "description": "Voice identifier (default: from config)"},
                "model_id": {"type": "string",
                             "description": "TTS model (default: from config)"},
                "output_file": {"type": "string",
                                "description": "Output file path (default: auto-generated)"},
            },
            "required": ["text"],
        },
        function=tool_tts,
    ),
]
