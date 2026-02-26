"""Text-to-speech tool — tts.

Optional: only registered if TTS API key is configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_api_key: str = ""
_provider: str = ""
_output_dir: str = "/tmp"  # noqa: S108 — default; overridden by configure() from lucyd.toml
_channel: Any = None
_timeout: int = 60
_api_url: str = ""

# Voice defaults — configurable via [tools.tts] in lucyd.toml
_default_voice_id: str = ""
_default_model_id: str = ""
_voice_speed: float = 1.0
_voice_stability: float = 0.5
_voice_similarity_boost: float = 0.75


def configure(api_key: str = "", provider: str = "",
              output_dir: str = "/tmp", channel: Any = None,  # noqa: S108 — default; caller passes configured path
              default_voice_id: str = "",
              default_model_id: str = "",
              speed: float = 1.0, stability: float = 0.5,
              similarity_boost: float = 0.75,
              timeout: int = 60, api_url: str = "",
              contact_names: list[str] | None = None) -> None:
    global _api_key, _provider, _output_dir, _channel
    global _default_voice_id, _default_model_id
    global _voice_speed, _voice_stability, _voice_similarity_boost
    global _timeout, _api_url
    _api_key = api_key
    _provider = provider
    _output_dir = output_dir
    _channel = channel
    _default_voice_id = default_voice_id
    _default_model_id = default_model_id
    _voice_speed = speed
    _voice_stability = stability
    _voice_similarity_boost = similarity_boost
    _timeout = timeout
    _api_url = api_url
    if contact_names:
        names = ", ".join(contact_names)
        TOOLS[0]["input_schema"]["properties"]["send_to"]["description"] = (
            f"Recipient contact name. Available contacts: {names}. If empty, saves to disk only."
        )


async def tool_tts(text: str, voice_id: str = "",
                   model_id: str = "",
                   output_file: str = "", send_to: str = "") -> str:
    """Generate speech audio from text, optionally send as voice message."""
    if not _api_key:
        return "Error: No TTS API key configured"
    if not _provider:
        return "Error: No TTS provider configured"

    # Use configured defaults if not specified
    voice_id = voice_id or _default_voice_id
    model_id = model_id or _default_model_id

    if not voice_id:
        return "Error: No voice_id specified and no default configured"

    # Validate explicit output_file against filesystem allowlist
    if output_file:
        from tools.filesystem import _check_path
        err = _check_path(output_file)
        if err:
            return f"Error: Output path not allowed: {output_file}"

    user_specified_output = bool(output_file)
    if not output_file:
        fd, output_file = tempfile.mkstemp(suffix=".mp3", prefix="lucyd-tts-", dir=_output_dir)
        os.close(fd)

    if _provider == "elevenlabs":
        url = _api_url.format(voice_id=voice_id) if _api_url else f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        el_model_id = model_id or "eleven_v3"  # ElevenLabs-specific default
        payload = json.dumps({
            "text": text,
            "model_id": el_model_id,
            "voice_settings": {
                "speed": _voice_speed,
                "stability": _voice_stability,
                "similarity_boost": _voice_similarity_boost,
            },
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={  # noqa: S310 — hardcoded https://api.elevenlabs.io URL
            "Content-Type": "application/json",
            "xi-api-key": _api_key,
        })
        audio = b""
        try:
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=_timeout)
            audio = resp.read()
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, "wb") as f:
                f.write(audio)
            os.chmod(output_file, 0o600)
        except Exception as e:
            log.error("TTS failed: %s", e, exc_info=True)
            return f"Error: TTS generation failed: {type(e).__name__}"
    else:
        return f"Error: Unknown TTS provider: {_provider}"

    # Send as attachment if requested
    if send_to and _channel:
        try:
            await _channel.send(send_to, "", [output_file])
            # Clean up tempfile after successful send (user didn't specify explicit path)
            if not user_specified_output:
                try:
                    os.unlink(output_file)
                except OSError:
                    pass
            return f"Voice message sent to {send_to} ({len(audio)} bytes)"
        except Exception as e:
            return f"Audio saved to {output_file} but delivery failed: {e}"

    return f"Audio saved to {output_file} ({len(audio)} bytes)"


TOOLS = [
    {
        "name": "tts",
        "description": "Generate speech audio from text using text-to-speech. Can send directly as a voice message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to convert to speech"},
                "voice_id": {"type": "string", "description": "Voice identifier (default: from config)"},
                "model_id": {"type": "string", "description": "TTS model (default: from config)"},
                "output_file": {"type": "string", "description": "Output file path (default: auto-generated in /tmp)"},
                "send_to": {"type": "string", "description": "Recipient — use a contact name from config. If empty, saves to disk only."},
            },
            "required": ["text"],
        },
        "function": tool_tts,
    },
]
