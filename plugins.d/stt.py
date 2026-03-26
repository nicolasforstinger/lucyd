"""Speech-to-text preprocessor plugin.

Transcribes audio attachments before the agent sees them.
The agent receives the transcription as text — it never sees raw audio.

Configuration: [stt] section in lucyd.toml.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_stt_config: dict = {}
_stt_backend: str = ""


def configure(config: Any) -> None:
    global _stt_config, _stt_backend
    _stt_config = config.raw("stt", default={})
    _stt_backend = config.stt_backend


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
            import stt as stt_mod
            transcription = await stt_mod.transcribe(
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
