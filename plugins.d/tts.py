"""Text-to-speech plugin — generates audio from text.

Channel-agnostic: produces an audio file and returns a structured result
with the file path as an attachment. The framework includes attachments
in the reply, and the connected bridge delivers them.

Configuration: [tools.tts] section in lucyd.toml + LUCYD_ELEVENLABS_KEY env var.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from async_utils import run_blocking

log = logging.getLogger(__name__)

_api_key: str = ""
_provider: str = ""
_output_dir: str = ""
_timeout: int = 60
_api_url: str = ""
_default_voice_id: str = ""
_default_model_id: str = ""
_voice_speed: float = 1.0
_voice_stability: float = 0.5
_voice_similarity_boost: float = 0.75


def configure(config: Any = None, **_: Any) -> None:
    global _api_key, _provider, _output_dir, _timeout, _api_url
    global _default_voice_id, _default_model_id
    global _voice_speed, _voice_stability, _voice_similarity_boost
    if config is None:
        return
    tts_cfg = config.raw("tools", "tts", default={})
    key_env = tts_cfg.get("api_key_env", "")
    _api_key = os.environ.get(key_env, "") if key_env else ""
    _provider = tts_cfg.get("provider", "")
    _timeout = tts_cfg.get("timeout", 60)
    _api_url = tts_cfg.get("api_url", "")
    _default_voice_id = tts_cfg.get("default_voice_id", "")
    _default_model_id = tts_cfg.get("default_model_id", "")
    _voice_speed = tts_cfg.get("speed", 1.0)
    _voice_stability = tts_cfg.get("stability", 0.5)
    _voice_similarity_boost = tts_cfg.get("similarity_boost", 0.75)
    _output_dir = str(config.http_download_dir)


async def tool_tts(text: str, voice_id: str = "",
                   model_id: str = "", output_file: str = "") -> dict[str, Any]:
    """Generate speech audio from text. Returns structured result with file attachment."""
    if not _api_key:
        return {"text": "Error: No TTS API key configured", "attachments": []}
    if not _provider:
        return {"text": "Error: No TTS provider configured", "attachments": []}

    voice_id = voice_id or _default_voice_id
    model_id = model_id or _default_model_id

    if not voice_id:
        return {"text": "Error: No voice_id specified and no default configured", "attachments": []}

    if output_file:
        from tools.filesystem import _check_path
        err = _check_path(output_file)
        if err:
            return {"text": f"Error: Output path not allowed: {output_file}", "attachments": []}

    if not output_file:
        fd, output_file = tempfile.mkstemp(suffix=".mp3", prefix="lucyd-tts-", dir=_output_dir or None)
        os.close(fd)

    if _provider == "elevenlabs":
        url = _api_url.format(voice_id=voice_id) if _api_url else f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        body = {
            "text": text,
            "model_id": model_id or "eleven_v3",
            "voice_settings": {
                "speed": _voice_speed,
                "stability": _voice_stability,
                "similarity_boost": _voice_similarity_boost,
            },
        }
        try:
            def _request() -> bytes:
                resp = httpx.post(url, json=body,
                                  headers={"xi-api-key": _api_key},
                                  timeout=_timeout)
                resp.raise_for_status()
                return resp.content

            audio = await run_blocking(_request)
            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio)
            output_path.chmod(0o600)
        except Exception as e:
            log.error("TTS failed: %s", e, exc_info=True)
            return {"text": f"Error: TTS generation failed: {type(e).__name__}", "attachments": []}
    else:
        return {"text": f"Error: Unknown TTS provider: {_provider}", "attachments": []}

    return {"text": f"Generated audio ({len(audio)} bytes)", "attachments": [output_file]}


TOOLS = [
    {
        "name": "tts",
        "description": "Generate speech audio from text. The audio file is included in the reply as an attachment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to convert to speech"},
                "voice_id": {"type": "string", "description": "Voice identifier (default: from config)"},
                "model_id": {"type": "string", "description": "TTS model (default: from config)"},
                "output_file": {"type": "string", "description": "Output file path (default: auto-generated)"},
            },
            "required": ["text"],
        },
        "function": tool_tts,
    },
]
